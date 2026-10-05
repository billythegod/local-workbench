"""三个应用模块共享的最小公共层：原子写、时间戳、脱敏与异常映射。

只用 Python 标准库。模块契约详见 docs/模块契约.md。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---- 脱敏（full=入库正文用；light=进程名与命令短输出用）----

_SECRET_KEYS = r"(?:(?:[A-Za-z][A-Za-z0-9]*_)*(?:api[_-]?key|access[_-]?(?:key|token)|secret[_-]?(?:key|access[_-]?key)?|client[_-]?secret|refresh[_-]?token|id[_-]?token|password|passwd|pwd|token|authorization|credentials?)|密钥|密码|令牌)"
_ASSIGN = re.compile(r'(?i)([\"\']?\b' + _SECRET_KEYS + r'[\"\']?\s*[:=]\s*)(\[已脱敏\]|\"[^\"\n]*\"|\'[^\'\n]*\'|[^\s,;\}\]\n]+)')
_CREDENTIAL_START = re.compile(r'(?is)([\"\']?\bcredentials?[\"\']?\s*[:=]\s*)([\{\[])')
_TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b")

_LIGHT_ASSIGN = re.compile(r"(?i)((?:--)?(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|authorization|token)\s*[=:]?\s*)[^\s,;\"']+")
_LIGHT_BEARER = re.compile(r"(?i)(bearer\s+)[^\s,;\"']+")
# 轻量模式沿用运行台原阈值：sk- 前缀 8 个字符起即隐藏。
_LIGHT_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def redact(value, mode="full"):
    text = str(value or "")
    if mode == "light":
        text = text.replace("\x00", "")
        text = _LIGHT_BEARER.sub(r"\1[已隐藏]", text)
        text = _LIGHT_ASSIGN.sub(r"\1[已隐藏]", text)
        text = _LIGHT_TOKEN.sub("[已隐藏]", text)
        text = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[已隐藏]@", text)
        return text
    text = re.sub(r"(?s)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "[私钥已脱敏]", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 [已脱敏]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[凭据已脱敏]@", text)
    # credentials 对象可嵌套；按括号与引号平衡删除整段，不解析或打印凭据值。
    pieces, end = [], 0
    for match in _CREDENTIAL_START.finditer(text):
        if match.start() < end:
            continue
        start = match.end() - 1
        depth, quote, escaped, stop = 0, "", False, len(text)
        for i in range(start, len(text)):
            char = text[i]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in ('"', "'"):
                quote = char
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    stop = i + 1
                    break
        pieces.extend((text[end:match.start()], match.group(1), "[已脱敏]"))
        end = stop
    if pieces:
        pieces.append(text[end:])
        text = "".join(pieces)
    text = _ASSIGN.sub(r"\1[已脱敏]", text)
    text = _TOKEN.sub("[已脱敏]", text)
    # 中文字段没有单词边界。
    text = re.sub(r"((?:密钥|密码|令牌)\s*[:：=]\s*)([^\s，,;；\n]+)", r"\1[已脱敏]", text)
    return text


# ---- 时间与原子写 ----

def utc_now(timespec="seconds"):
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def write_json_atomic(path, value, chmod=0o600):
    """临时文件 + 原子替换；fsync 保证崩溃后不留半截内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, chmod)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ---- HTTP 契约 ----

def api_error(exc, scrub=None):
    """模块 api() 的统一异常映射；scrub 用于清理错误消息里的敏感内容。"""
    clean = (scrub or str)
    if isinstance(exc, FileNotFoundError):
        return 404, {"error": clean(str(exc))}
    if isinstance(exc, LookupError):
        return 404, {"error": clean(str(exc))}
    if isinstance(exc, (ValueError, TypeError)):
        return 400, {"error": clean(str(exc))}
    if isinstance(exc, RuntimeError):
        return 409, {"error": clean(str(exc))}
    if isinstance(exc, OSError):
        return 500, {"error": "本地文件读写失败，请确认目录权限后重试。"}
    return 500, {"error": "操作未完成，请重试。", "type": type(exc).__name__}


def not_allowed_or_missing(path, known):
    """known=模块全部已注册路径（含前缀，如 /sessions/）。命中但方法不符给 405。"""
    def hit(prefix):
        if prefix == "/":
            return path == "/"
        return path == prefix or path.startswith(prefix if prefix.endswith("/") else prefix + "/")
    if any(hit(prefix) for prefix in known):
        return 405, {"error": "该接口不接受此请求方法。"}
    return 404, {"error": "没有这个接口"}
