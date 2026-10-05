"""跨项目文件证据台。只读取选定目录，写入仅发生在 data/delivery。"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apps import _shared

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR.parents[1] / "data" / "delivery"
MANIFEST_DIR = DATA_DIR / "manifests"
ACCEPT_DIR = DATA_DIR / "acceptances"
SCHEMA = "delivery-evidence/1"
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "env", ".venv", "vendor", "credentials", "secrets", "keychains", "sessions", "automations"}
SENSITIVE = re.compile(r"(^\.env($|[.\-_])|(^|[.\-_])(credentials?|secrets?|tokens?|passwords?|private[.\-_]?keys?|api[.\-_]?keys?)([.\-_]|$)|^id_(rsa|ed25519|ecdsa)|\.(pem|key|p12|pfx|keystore)$)", re.I)
KINDS = {
    "media": {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"},
    "subtitle": {".srt", ".vtt"}, "markdown": {".md", ".markdown"},
    "html": {".html", ".htm"}, "pdf": {".pdf"},
    "office": {".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"},
    "legacy_office": {".doc", ".xls", ".ppt"}, "json": {".json"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"},
    "table": {".csv", ".tsv"},
}
DEFAULTS = {"max_files": 1500, "max_entries": 6000, "max_depth": 10,
            "seconds": 20, "hash_total_mb": 128, "hash_file_mb": 32,
            "text_file_mb": 2, "check_total_mb": 64, "office_uncompressed_mb": 64}
MAXIMUMS = {"max_files": 10000, "max_entries": 30000, "max_depth": 30,
            "seconds": 45, "hash_total_mb": 8192, "hash_file_mb": 4096,
            "text_file_mb": 8, "check_total_mb": 256, "office_uncompressed_mb": 256}
LIMITATIONS = [
    "只扫描所选目录；隐藏项、凭据名、依赖目录及所有符号链接均跳过。",
    "Office 仅检查容器、必要成员及有限预算内的 CRC；未运行原生 Office 渲染、公式重算或排版验收。",
    "媒体仅读取技术元数据；未观看视频、试听音频或判断字幕语义与视觉质量。",
    "Markdown 仅核对目录内相对链接是否存在；未请求外部链接。HTML 未执行或渲染；PDF 仅检查基本文件标记。",
    "人工验收必须显式填写并保存，绑定文件快照哈希；发布状态始终未记录。",
    "快照描述扫描时的文件；读取快照不会重新检查当前目录。再次扫描可发现变化并使旧验收过期。",
]


class InputError(ValueError):
    pass


def utc_now():
    return _shared.utc_now()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def sensitive(name):
    return name.startswith(".") or bool(SENSITIVE.search(name)) or name.lower() in SKIP_DIRS


def directory(value):
    if not isinstance(value, str) or not value.strip():
        raise InputError("请填写目录的绝对路径。")
    p = Path(value.strip()).expanduser()
    if not p.is_absolute():
        raise InputError("目录必须使用绝对路径。")
    if p.is_symlink():
        raise InputError("扫描入口不能是符号链接，请直接选择真实目录。")
    try:
        p = p.resolve(strict=True)
    except FileNotFoundError:
        raise InputError("目录不存在，请确认绝对路径后重试。")
    except PermissionError:
        raise InputError("目录没有读取权限，请选择可读取的成品目录。")
    if not p.is_dir():
        raise InputError("指定路径不是目录。")
    if p in {Path("/"), Path("/Users"), Path.home(), Path("/private"), Path("/etc"), Path("/System"), Path("/Library")}:
        raise InputError("目录范围过大或属于系统目录，请选择具体项目或成品目录。")
    # 敏感名只校验扫描入口的最后两级；祖先路径只拦隐藏段，避免 ~/venv/proj/out 这类目录被整目录拒绝。
    if any(x.startswith(".") for x in p.parts[1:]):
        raise InputError("目录路径包含隐藏目录，已拒绝读取。")
    if any(sensitive(x) for x in p.parts[-2:]):
        raise InputError("扫描入口或其父级包含隐藏项或敏感目录名，已拒绝读取。")
    return p


def relative(value):
    if not isinstance(value, str) or not value.strip():
        raise InputError("期望文件路径不能为空。")
    value = value.strip()
    if "\\" in value or "\x00" in value:
        raise InputError("相对路径不能包含反斜杠或空字符。")
    p = PurePosixPath(value)
    if not p.parts:
        raise InputError("期望清单必须填写文件路径，不能只写 . 或 ./。")
    if p.is_absolute() or ".." in p.parts or ":" in p.parts[0]:
        raise InputError("期望清单只允许目录内相对路径，不能使用绝对路径或 ..。")
    if any(sensitive(x) for x in p.parts):
        raise InputError("期望清单包含隐藏项或敏感文件名，已拒绝读取。")
    return p.as_posix()


def parse_expected(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [x for x in value.splitlines() if x.strip()]
    if not isinstance(value, list) or len(value) > 1000:
        raise InputError("期望清单须为每行一个路径，最多 1000 项。")
    return sorted(set(relative(x) for x in value))


def budgets(value):
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict):
        raise InputError("预算参数必须是对象。")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise InputError("未知预算参数：" + "、".join(sorted(unknown)))
    result = dict(DEFAULTS)
    for key, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= MAXIMUMS[key]:
            raise InputError(f"{key} 必须大于 0，且不超过 {MAXIMUMS[key]}。")
        result[key] = float(v) if key == "seconds" else int(v)
        if result[key] == 0:
            raise InputError(f"{key} 的最小值为 1。")
    return result


def secure_open(root, rel):
    """目录 fd 逐层打开；最后一层与所有父层均不跟随符号链接。"""
    rel = relative(rel)
    parts = PurePosixPath(rel).parts
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            new = os.open(part, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = new
        target = os.open(parts[-1], flags, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(target).st_mode):
            os.close(target)
            raise InputError("目标不是普通文件。")
        return os.fdopen(target, "rb")
    finally:
        os.close(fd)


def file_kind(path):
    ext = Path(path).suffix.lower()
    return next((k for k, suffixes in KINDS.items() if ext in suffixes), "other")


def issue(ctx, severity, code, path, message, action):
    ctx["issues"].append({"severity": severity, "code": code, "path": path, "message": message, "action": action})


def check(entry, name, status, summary):
    entry["checks"].append({"check": name, "status": status, "summary": summary})


def unchanged(st, entry):
    return st.st_size == entry["size"] and st.st_mtime_ns == entry["mtime_ns"]


def hash_entry(root, entry, ctx):
    remaining = ctx["budget"]["hash_total_mb"] * 1024 * 1024 - ctx["hash_bytes"]
    if entry["size"] > ctx["budget"]["hash_file_mb"] * 1024 * 1024:
        entry["hash_state"] = "not_run_file_budget"
    elif entry["size"] > remaining:
        entry["hash_state"] = "not_run_total_budget"
    elif time.monotonic() >= ctx["deadline"]:
        entry["hash_state"] = "not_run_time_budget"
    else:
        try:
            with secure_open(root, entry["path"]) as f:
                if not unchanged(os.fstat(f.fileno()), entry):
                    raise InputError("文件在扫描期间发生变化。")
                h = hashlib.sha256()
                while True:
                    if time.monotonic() >= ctx["deadline"]:
                        entry["hash_state"] = "not_run_time_budget"
                        break
                    chunk = f.read(min(1024 * 1024, max(1, remaining)))
                    if not chunk:
                        if not unchanged(os.fstat(f.fileno()), entry):
                            raise InputError("文件在计算哈希期间发生变化。")
                        entry["sha256"] = h.hexdigest()
                        entry["hash_state"] = "hashed"
                        break
                    if len(chunk) > remaining:
                        entry["hash_state"] = "not_run_total_budget"
                        break
                    remaining -= len(chunk)
                    ctx["hash_bytes"] += len(chunk)
                    h.update(chunk)
        except (OSError, ValueError) as exc:
            entry["hash_state"] = "read_error"
            issue(ctx, "error", "file_read", entry["path"], "无法安全读取文件或文件在扫描时变化。", "确认文件权限与生成进程已停止，再重新扫描。")
    if entry["hash_state"] != "hashed":
        reason = {"not_run_file_budget": "超出单文件预算", "not_run_total_budget": "超出总读取预算", "not_run_time_budget": "时间预算耗尽", "read_error": "读取失败或文件变化"}.get(entry["hash_state"], "未运行")
        check(entry, "sha256", "not_run", "哈希未完成：" + reason)
        issue(ctx, "warning", "hash_unrun", entry["path"], "文件哈希未完成，无法证明内容一致。", "提高单文件/总哈希预算后重新扫描，或缩小目录。")
    else:
        check(entry, "sha256", "pass", "已按流式读取计算 SHA-256。")


def read_text(root, entry, ctx):
    maximum = ctx["budget"]["text_file_mb"] * 1024 * 1024
    if entry["size"] > maximum or ctx["check_bytes"] + entry["size"] > ctx["budget"]["check_total_mb"] * 1024 * 1024:
        check(entry, "content", "not_run", "文本检查超出读取预算。")
        issue(ctx, "warning", "text_budget", entry["path"], "文本结构检查未执行。", "提高文本/检查预算或缩小目录后重试。")
        return None
    with secure_open(root, entry["path"]) as f:
        if not unchanged(os.fstat(f.fileno()), entry):
            raise InputError("文件在检查前发生变化。")
        raw = f.read(maximum + 1)
        ctx["check_bytes"] += len(raw)
        if not unchanged(os.fstat(f.fileno()), entry) or len(raw) > maximum:
            raise InputError("文件在读取时发生变化。")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        check(entry, "encoding", "not_run", "不是 UTF-8，未猜测编码。")
        issue(ctx, "warning", "encoding", entry["path"], "文本编码不支持，内容检查未执行。", "确认编码；可生成 UTF-8 副本后再检查。")
        return None


def probe_media(root, entry, ctx):
    executable = shutil.which("ffprobe")
    if not executable:
        entry["media"] = {"status": "unknown", "reason": "ffprobe 不可用"}
        check(entry, "media_metadata", "not_run", "ffprobe 不可用，时长、分辨率与音轨未知。")
        return
    try:
        with secure_open(root, entry["path"]) as f:
            if not unchanged(os.fstat(f.fileno()), entry):
                raise InputError("媒体文件在扫描期间发生变化。")
            cmd = [executable, "-v", "error", "-probesize", "8388608", "-analyzeduration", "3000000", "-show_entries",
                   "format=duration:stream=codec_type,codec_name,width,height,duration", "-of", "json", f"/dev/fd/{f.fileno()}"]
            timeout = min(4, max(0.1, ctx["deadline"] - time.monotonic()))
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, pass_fds=(f.fileno(),), check=False,
                                    env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LC_ALL": "C"})
            if not unchanged(os.fstat(f.fileno()), entry):
                raise InputError("媒体文件在探测期间发生变化。")
        if result.returncode != 0 or len(result.stdout) > 512000:
            raise InputError("ffprobe 无法解析该文件。")
        data = json.loads(result.stdout)
        raw_duration = data.get("format", {}).get("duration")
        duration = float(raw_duration) if raw_duration not in (None, "N/A") else None
        if duration is not None and (duration < 0 or not math.isfinite(duration)):
            duration = None
        streams = data.get("streams", [])[:100]
        entry["media"] = {"status": "read", "duration": duration,
                          "video": [{"codec": x.get("codec_name"), "width": x.get("width"), "height": x.get("height")} for x in streams if x.get("codec_type") == "video"],
                          "audio_tracks": sum(x.get("codec_type") == "audio" for x in streams)}
        check(entry, "media_metadata", "pass", "已读取技术元数据；时长未知时不检查字幕上界。")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        entry["media"] = {"status": "unknown", "reason": "探测失败或超时"}
        check(entry, "media_metadata", "not_run", "媒体元数据未知：探测失败或超时。")
        issue(ctx, "warning", "media_unknown", entry["path"], "媒体时长、分辨率或音轨无法确认。", "确认文件可播放；可缩小扫描目录后重试。")


def timestamp(raw, vtt):
    raw = raw.strip()
    sep = "." if vtt else ","
    parts = raw.split(":")
    if len(parts) == 2 and vtt:
        parts.insert(0, "0")
    if (len(parts) != 3 or not re.fullmatch(r"\d+", parts[0])
            or not re.fullmatch(r"\d{2}", parts[1])
            or not re.fullmatch(r"\d{2}" + re.escape(sep) + r"\d{3}", parts[2])):
        raise ValueError("时间格式不合法")
    h, m = int(parts[0]), int(parts[1])
    s, ms = [int(x) for x in parts[2].split(sep)]
    if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
        raise ValueError("分秒超出范围")
    return h * 3600 + m * 60 + s + ms / 1000


def subtitles(text_value, entry, media_entries, ctx):
    vtt = entry["path"].lower().endswith(".vtt")
    errors, warnings, cues = [], [], []
    if vtt and not text_value.lstrip().startswith("WEBVTT"):
        errors.append("缺少 WEBVTT 文件头")
    previous_start, previous_end = -1.0, -1.0
    for line_no, line in enumerate(text_value.splitlines(), 1):
        if "-->" not in line:
            continue
        try:
            parts = line.split("-->")
            if len(parts) != 2:
                raise ValueError("分隔符重复")
            start = timestamp(parts[0].strip(), vtt)
            end = timestamp(parts[1].strip().split()[0], vtt)
            if end <= start:
                raise ValueError("结束时间必须晚于开始时间")
            if start < previous_start:
                errors.append(f"第 {line_no} 行时间倒序")
            if start < previous_end:
                warnings.append(f"第 {line_no} 行与前一条字幕重叠")
            previous_start, previous_end = start, end
            cues.append({"start": start, "end": end, "line": line_no})
        except (ValueError, IndexError):
            errors.append(f"第 {line_no} 行时间格式或区间不合法")
    if not cues:
        errors.append("没有可解析的字幕时间行")
    p = Path(entry["path"])
    exact = [x for x in media_entries if Path(x["path"]).with_suffix("") == p.with_suffix("")]
    chosen = exact[0] if len(exact) == 1 else (media_entries[0] if not exact and len(media_entries) == 1 else None)
    duration = chosen.get("media", {}).get("duration") if chosen else None
    if duration is not None:
        for cue in cues:
            if cue["end"] > duration + 0.15:
                errors.append(f"第 {cue['line']} 行结束时间超出媒体时长 {duration:.3f} 秒")
        check(entry, "subtitle_duration", "error" if any("超出媒体" in x for x in errors) else "pass", f"按 {chosen['path']} 的 {duration:.3f} 秒时长检查，容差 0.15 秒。")
    else:
        check(entry, "subtitle_duration", "not_run", "关联媒体不唯一或时长未知，未检查字幕是否超出媒体时长。")
        issue(ctx, "warning", "subtitle_duration_unknown", entry["path"], "字幕的媒体时长上界未核验。", "让字幕与媒体同目录同名，或缩小到仅一份媒体的目录后重试。")
    entry["subtitle"] = {"cues": len(cues), "last_end": max((x["end"] for x in cues), default=None), "media_path": chosen["path"] if chosen else None, "duration_checked": duration is not None}
    check(entry, "subtitle_timing", "error" if errors else ("warning" if warnings else "pass"), f"解析 {len(cues)} 条字幕；{len(errors)} 个错误，{len(warnings)} 个重叠提醒。")
    for message in errors[:100]:
        issue(ctx, "error", "subtitle_invalid", entry["path"], message, "修正字幕时间或重新导出字幕后扫描。")
    for message in warnings[:20]:
        issue(ctx, "warning", "subtitle_overlap", entry["path"], message, "播放对应片段，判断重叠是否符合预期。")


def local_link(root, source, href):
    parsed = urlsplit(html.unescape(href))
    if parsed.scheme or parsed.netloc or href.startswith("#"):
        return "external"
    raw = unquote(parsed.path)
    if not raw:
        return "anchor"
    if raw.startswith(("/", "\\")) or "\\" in raw or "\x00" in raw:
        return "escape"
    parts = list(PurePosixPath(source).parent.parts)
    for part in PurePosixPath(raw).parts:
        if part == "..":
            if not parts:
                return "escape"
            parts.pop()
        elif part != ".":
            parts.append(part)
    if not parts or any(sensitive(x) for x in parts):
        return "blocked"
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return "blocked"
    return "exists" if current.exists() else "missing"


def markdown_links(root, text_value, entry, ctx):
    # 基本 inline 链接与引用定义；代码围栏不作为正文链接。
    cleaned = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$", "", text_value)
    links = re.findall(r"!?\[[^\[\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)", cleaned)
    links += re.findall(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)", cleaned)
    counts = {"exists": 0, "missing": 0, "escape": 0, "blocked": 0, "external": 0, "anchor": 0}
    for href in links[:2000]:
        href = href.strip("<>")
        try:
            state = local_link(root, entry["path"], href)
        except ValueError:
            state = "escape"
        counts[state] += 1
        if state in {"missing", "escape", "blocked"}:
            label = {"missing": "本地链接目标不存在", "escape": "链接越出扫描目录", "blocked": "链接指向敏感项或符号链接"}[state]
            try:
                visible_path = urlsplit(href).path
            except ValueError:
                visible_path = "链接格式不合法"
            issue(ctx, "error", "markdown_link_" + state, entry["path"], label + "：" + visible_path[:250], "修正链接，或将所需成品放入所选目录后重试。")
    entry["links"] = counts
    check(entry, "markdown_local_links", "error" if sum(counts[x] for x in ("missing", "escape", "blocked")) else "pass", f"检查 {len(links[:2000])} 个基本 Markdown 链接；未验证锚点、外部 URL 或复杂 Markdown 语法。")
    if len(links) > 2000:
        check(entry, "markdown_link_coverage", "not_run", "链接超过 2000 个，后续链接未检查。")
        issue(ctx, "warning", "markdown_link_budget", entry["path"], "Markdown 链接覆盖不完整。", "缩短或拆分文件后再次检查。")


def office(root, entry, ctx):
    required = {".docx": "word/document.xml", ".docm": "word/document.xml", ".xlsx": "xl/workbook.xml", ".xlsm": "xl/workbook.xml", ".pptx": "ppt/presentation.xml", ".pptm": "ppt/presentation.xml"}
    errors = []
    checked = 0
    compressed_limit = min(ctx["budget"]["office_uncompressed_mb"], ctx["budget"]["check_total_mb"]) * 1024 * 1024
    if entry["size"] > compressed_limit:
        check(entry, "office_structure", "not_run", "Office 容器大小超过结构读取预算，未打开容器。")
        issue(ctx, "warning", "office_container_budget", entry["path"], "Office 容器结构检查未运行。", "提高结构读取预算或缩小文件后重新扫描。")
        return
    try:
        with secure_open(root, entry["path"]) as f, zipfile.ZipFile(f) as z:
            infos = z.infolist()
            if len(infos) > 10000:
                raise InputError("Office 成员超过 10000，已停止读取。")
            names = [x.filename for x in infos]
            for info in infos:
                p = PurePosixPath(info.filename.replace("\\", "/"))
                if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", info.filename) or "\\" in info.filename:
                    errors.append("容器包含危险成员路径")
                    break
            if len(names) != len(set(names)):
                errors.append("容器成员名称重复")
            for name in ["[Content_Types].xml", "_rels/.rels", required[Path(entry["path"]).suffix.lower()]]:
                if name not in names:
                    errors.append("缺少必要成员 " + name)
            if any(x.flag_bits & 1 for x in infos):
                errors.append("容器含加密成员，无法完成结构检查")
            total = sum(x.file_size for x in infos)
            remaining = min(ctx["budget"]["office_uncompressed_mb"] * 1024 * 1024, ctx["budget"]["check_total_mb"] * 1024 * 1024 - ctx["check_bytes"])
            crc_complete = False
            if not errors and total <= remaining and time.monotonic() < ctx["deadline"]:
                for info in infos:
                    if time.monotonic() >= ctx["deadline"]:
                        break
                    with z.open(info) as member:
                        count = 0
                        while True:
                            if time.monotonic() >= ctx["deadline"] or ctx["check_bytes"] >= ctx["budget"]["check_total_mb"] * 1024 * 1024:
                                break
                            chunk = member.read(1024 * 1024)
                            if not chunk:
                                break
                            count += len(chunk)
                            ctx["check_bytes"] += len(chunk)
                        if count != info.file_size:
                            break
                    checked += 1
                crc_complete = checked == len(infos)
            entry["office"] = {"members": len(infos), "uncompressed_bytes": total, "crc_members_checked": checked, "crc_complete": crc_complete, "rendering": "not_run"}
            if not crc_complete and not errors:
                issue(ctx, "warning", "office_crc_budget", entry["path"], "必要成员已检查；CRC 读取超出预算，尚未完成。", "提高结构检查预算或缩小目录后重试。")
                check(entry, "office_crc", "not_run", f"CRC 只检查 {checked}/{len(infos)} 个成员。")
            elif crc_complete:
                check(entry, "office_crc", "pass", "所有成员 CRC 已读取校验。")
            if not unchanged(os.fstat(f.fileno()), entry):
                errors.append("Office 文件在扫描期间发生变化")
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        errors.append("Office ZIP 容器损坏、无法读取或格式不支持")
    check(entry, "office_structure", "error" if errors else "pass", "；".join(errors) if errors else "必要成员与路径检查通过；不代表文档内容、渲染或重算通过。")
    for message in errors:
        issue(ctx, "error", "office_invalid", entry["path"], message, "用原生应用打开确认，重新保存或导出后扫描。")


def inspect_file(root, entry, media_entries, ctx):
    if time.monotonic() >= ctx["deadline"]:
        check(entry, "content", "not_run", "扫描时间预算耗尽。")
        issue(ctx, "warning", "check_time_budget", entry["path"], "文件内容检查未完成。", "缩小目录或提高时间预算后重试。")
        return
    try:
        kind = entry["kind"]
        if kind in {"json", "subtitle", "markdown"}:
            text_value = read_text(root, entry, ctx)
            if text_value is None:
                return
            if kind == "json":
                try:
                    json.loads(text_value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非标准常量")))
                    check(entry, "json_parse", "pass", "JSON 语法解析通过；未校验业务 schema。")
                except (ValueError, RecursionError):
                    check(entry, "json_parse", "error", "JSON 语法不合法或嵌套过深。")
                    issue(ctx, "error", "json_invalid", entry["path"], "JSON 语法解析失败。", "检查括号、逗号及转义；修正后再次扫描。")
            elif kind == "subtitle":
                subtitles(text_value, entry, media_entries, ctx)
            else:
                markdown_links(root, text_value, entry, ctx)
        elif kind == "office":
            office(root, entry, ctx)
        elif kind == "pdf":
            with secure_open(root, entry["path"]) as f:
                if not unchanged(os.fstat(f.fileno()), entry):
                    raise InputError("PDF 文件在扫描期间变化。")
                header = f.read(8)
                f.seek(max(0, entry["size"] - 1024))
                ending = f.read(1024)
                if not unchanged(os.fstat(f.fileno()), entry):
                    raise InputError("PDF 文件在读取期间变化。")
            ok = header.startswith(b"%PDF-") and b"%%EOF" in ending
            check(entry, "pdf_markers", "pass" if ok else "error", "PDF 文件头与结束标记检查通过；未解析页面或渲染。" if ok else "PDF 文件头或结束标记缺失。")
            if not ok:
                issue(ctx, "error", "pdf_markers", entry["path"], "PDF 基本标记不完整。", "打开文件并重新导出 PDF，再重新扫描。")
        elif kind == "html":
            check(entry, "html_rendering", "not_run", "已登记文件；未执行脚本、加载页面或渲染。")
        elif kind == "legacy_office":
            check(entry, "legacy_office", "not_run", "旧版二进制 Office 格式仅登记与哈希，不支持容器检查。")
            issue(ctx, "warning", "legacy_office", entry["path"], "旧版 Office 结构未核验。", "用原生 Office 打开确认，或另存为新版格式后再扫描。")
        else:
            check(entry, "native_review", "not_run", "已登记文件；未执行内容或原生渲染检查。")
    except (OSError, ValueError, RecursionError):
        check(entry, "content", "error", "无法安全读取文件或文件在检查时变化。")
        issue(ctx, "error", "content_read", entry["path"], "文件内容检查失败。", "确认权限及文件已写入完成，再重新扫描。")


def manifest_payload(manifest):
    return {"schema": SCHEMA, "directory": manifest["directory"], "expected": manifest["expected"],
            "coverage": {k: manifest["coverage"][k] for k in ("inventory_complete", "stop_reasons")},
            "files": [{k: x.get(k) for k in ("path", "kind", "size", "mtime_ns", "sha256", "hash_state")} for x in manifest["files"]]}


def evidence_payload(manifest):
    # 人工记录查询可刷新；其余自动检查结果也须纳入存储完整性检查。
    return {k: v for k, v in manifest.items() if k not in {"human", "evidence_hash"}}


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{5,100}", value):
        raise InputError("快照编号不合法。")
    return value


def write_json(path, value):
    _shared.write_json_atomic(path, value)


def ledger_path():
    # 跟随 MANIFEST_DIR（测试会整体重定向存储目录），登记簿不与快照分离。
    return MANIFEST_DIR.parent / "ledger.jsonl"


def operator_name():
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def append_ledger(action, **fields):
    """只追加的登记簿：事后可发现快照或验收记录被删除或篡改。"""
    entry = {"time": utc_now(), "action": action, "operator": operator_name()}
    entry.update(fields)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger():
    entries = []
    try:
        for line in ledger_path().read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    entries.append(value)
            except ValueError:
                continue
    except OSError:
        pass
    return entries


def read_manifest(manifest_id):
    path = MANIFEST_DIR / (valid_id(manifest_id) + ".json")
    if not path.exists():
        raise InputError("快照不存在。")
    if path.is_symlink() or path.stat().st_size > 30 * 1024 * 1024:
        raise InputError("快照存储不安全或超出读取预算。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("schema") != SCHEMA or value.get("manifest_hash") != digest(manifest_payload(value))
                or value.get("evidence_hash") != digest(evidence_payload(value))):
            raise InputError("快照内容或哈希不一致，已拒绝使用。")
        # 哈希字段是前端回显内容；服务端固化格式，防止伪造成可执行属性。
        for item in value.get("files", []):
            sha = item.get("sha256") if isinstance(item, dict) else None
            if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
                raise InputError("快照内容或哈希不一致，已拒绝使用。")
        return value
    except (ValueError, KeyError, TypeError):
        raise InputError("快照格式损坏或哈希不一致。")


def acceptance_for(manifest):
    if not ACCEPT_DIR.exists():
        return {"status": "not_recorded", "message": "尚未记录人工验收。", "current_files": "not_rechecked"}
    records = []
    corrupt = []
    for p in sorted(ACCEPT_DIR.glob("*.json"), reverse=True)[:300]:
        try:
            if p.is_symlink() or p.stat().st_size > 16384:
                continue
            record = json.loads(p.read_text(encoding="utf-8"))
            if (record.get("directory") == manifest["directory"]
                    and record.get("explicit_confirmation") is True
                    and record.get("decision") in {"accepted", "rejected"}
                    and isinstance(record.get("manifest_hash"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", record["manifest_hash"])):
                if record.get("record_hash") != digest({k: v for k, v in record.items() if k != "record_hash"}):
                    corrupt.append(p.stem)
                    continue
                records.append(record)
        except (OSError, ValueError):
            corrupt.append(p.stem)
    if corrupt:
        return {"status": "not_recorded", "message": f"发现 {len(corrupt)} 条人工验收记录损坏或哈希不一致，已忽略；详见审计登记簿核对结果。", "current_files": "not_rechecked", "integrity_corrupt": corrupt}
    if not records:
        return {"status": "not_recorded", "message": "尚未记录人工验收。", "current_files": "not_rechecked"}
    record = max(records, key=lambda x: x.get("created_at", ""))
    stale = record.get("manifest_hash") != manifest["manifest_hash"]
    return {"status": "stale" if stale else record["decision"], "message": "文件快照已变化，旧人工验收记录已过期。" if stale else "人工记录仅针对本快照；当前目录需再次扫描确认。",
            "record": record, "current_files": "not_rechecked"}


def scan(directory_value, expected=None, budget_values=None, save=True):
    root = directory(directory_value)
    expected = parse_expected(expected)
    budget = budgets(budget_values)
    start = time.monotonic()
    ctx = {"budget": budget, "deadline": start + budget["seconds"], "hash_bytes": 0, "check_bytes": 0, "issues": []}
    files, skipped, stop_reasons = [], [], []
    counters = {"visited_entries": 0, "skipped_entries": 0, "unsupported_files": 0}

    def skip(path, reason):
        counters["skipped_entries"] += 1
        if len(skipped) < 200:
            skipped.append({"path": path, "reason": reason})

    def walk(current, depth):
        if depth > budget["max_depth"]:
            stop_reasons.append("max_depth")
            return
        try:
            iterator = os.scandir(current)
        except OSError:
            stop_reasons.append("unreadable_directory")
            issue(ctx, "error", "directory_read", str(current.relative_to(root)), "目录无法读取。", "确认目录权限后重试。")
            return
        with iterator:
            for item in iterator:
                if time.monotonic() >= ctx["deadline"]:
                    stop_reasons.append("time_budget")
                    return
                if counters["visited_entries"] >= budget["max_entries"]:
                    stop_reasons.append("max_entries")
                    return
                counters["visited_entries"] += 1
                rel = str(Path(item.path).relative_to(root)).replace(os.sep, "/")
                if sensitive(item.name):
                    skip(rel, "隐藏项、敏感名或依赖目录")
                    continue
                try:
                    if item.is_symlink():
                        skip(rel, "符号链接不读取")
                    elif item.is_dir(follow_symlinks=False):
                        walk(Path(item.path), depth + 1)
                        if set(stop_reasons) & {"time_budget", "max_entries", "max_files"}:
                            return
                    elif item.is_file(follow_symlinks=False):
                        if len(files) >= budget["max_files"]:
                            stop_reasons.append("max_files")
                            return
                        kind = file_kind(rel)
                        # 所有普通非敏感文件登记元数据；仅支持的格式做内容检查。
                        if kind == "other":
                            counters["unsupported_files"] += 1
                        st = item.stat(follow_symlinks=False)
                        files.append({"path": rel, "kind": kind, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                      "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                                      "sha256": None, "hash_state": "not_run", "checks": []})
                    else:
                        skip(rel, "非普通文件")
                except OSError:
                    skip(rel, "无权读取或文件已消失")
                    issue(ctx, "error", "entry_read", rel, "目录项无法读取。", "确认权限后重试。")
    walk(root, 0)
    files.sort(key=lambda x: x["path"])
    if not files:
        issue(ctx, "warning", "empty_directory", "", "排除规则内没有普通文件，尚无可核验的成品证据。", "确认目录位置、排除范围及成品是否已生成。")
    for entry in files:
        hash_entry(root, entry, ctx)
    media_entries = [x for x in files if x["kind"] == "media"]
    for entry in media_entries:
        if time.monotonic() < ctx["deadline"]:
            probe_media(root, entry, ctx)
        else:
            entry["media"] = {"status": "unknown", "reason": "扫描时间预算耗尽"}
            check(entry, "media_metadata", "not_run", "时间预算耗尽，媒体元数据未知。")
            issue(ctx, "warning", "media_unknown", entry["path"], "媒体元数据未读取。", "缩小目录或提高时间预算后重试。")
    for entry in files:
        if entry["kind"] != "media":
            inspect_file(root, entry, media_entries, ctx)
    inventory_complete = not stop_reasons
    indexed = {x["path"]: x for x in files}
    expectations = []
    for rel in expected:
        if rel in indexed:
            state = "present"
        else:
            try:
                with secure_open(root, rel):
                    state = "not_scanned"
            except FileNotFoundError:
                state = "missing"
            except (OSError, ValueError):
                state = "blocked"
        expectations.append({"path": rel, "status": state})
        if state != "present":
            issue(ctx, "error" if state in {"missing", "blocked"} else "warning", "expected_" + state, rel,
                  {"missing": "期望文件缺失。", "blocked": "期望路径不可安全读取。", "not_scanned": "期望文件存在，但未纳入本次扫描。"}[state], "修正期望清单、补齐文件，或扩大扫描预算后重试。")
    if not inventory_complete:
        issue(ctx, "warning", "inventory_partial", "", "目录清单覆盖不完整：" + "、".join(sorted(set(stop_reasons))), "缩小目录或提高对应预算后再次扫描。")
    errors = sum(x["severity"] == "error" for x in ctx["issues"])
    warnings = sum(x["severity"] == "warning" for x in ctx["issues"])
    manifest = {"schema": SCHEMA, "id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8],
                "created_at": utc_now(), "directory": str(root), "expected": expected, "expectations": expectations,
                "budget": budget, "coverage": {"inventory_complete": inventory_complete, "stop_reasons": sorted(set(stop_reasons)),
                    **counters, "skipped_sample": skipped, "skip_sample_truncated": counters["skipped_entries"] > len(skipped),
                    "hash_bytes_read": ctx["hash_bytes"], "check_bytes_read": ctx["check_bytes"], "elapsed_seconds": round(time.monotonic() - start, 3)},
                "files": files, "issues": ctx["issues"], "summary": {"files": len(files), "bytes": sum(x["size"] for x in files),
                    "hashed": sum(x["hash_state"] == "hashed" for x in files), "errors": errors, "warnings": warnings,
                    "missing": sum(x["status"] == "missing" for x in expectations), "by_kind": {k: sum(x["kind"] == k for x in files) for k in list(KINDS) + ["other"]}},
                "levels": {"inventory": "complete" if inventory_complete else "partial", "automatic": "issues" if errors else ("incomplete" if warnings else "pass"),
                    "native_rendering": "not_run", "publishing": "not_recorded"}, "limitations": LIMITATIONS}
    manifest["manifest_hash"] = digest(manifest_payload(manifest))
    manifest["evidence_hash"] = digest(evidence_payload(manifest))
    manifest["human"] = acceptance_for(manifest)
    if save:
        write_json(MANIFEST_DIR / (manifest["id"] + ".json"), manifest)
        append_ledger("scan", id=manifest["id"], directory=manifest["directory"],
                      manifest_hash=manifest["manifest_hash"], evidence_hash=manifest["evidence_hash"],
                      files=manifest["summary"]["files"], bytes=manifest["summary"]["bytes"])
    return manifest


def estimate(directory_value):
    """扫描前的快速体积预估：只 stat，不读文件内容，用于建议哈希预算。"""
    root = directory(directory_value)
    deadline = time.monotonic() + 5
    files_count, total_bytes, max_file, entries = 0, 0, 0, 0
    truncated = False
    for dirpath, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not sensitive(d)]
        for name in names:
            if sensitive(name):
                continue
            entries += 1
            if entries > 30000 or time.monotonic() > deadline:
                truncated = True
                break
            try:
                st = os.stat(Path(dirpath) / name, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files_count += 1
            total_bytes += st.st_size
            max_file = max(max_file, st.st_size)
        if truncated:
            break
    mib = 1024 * 1024
    suggested = None
    if files_count:
        suggested = {"hash_total_mb": min(MAXIMUMS["hash_total_mb"], math.ceil(total_bytes * 1.05 / mib) or 1),
                     "hash_file_mb": min(MAXIMUMS["hash_file_mb"], math.ceil(max_file * 1.05 / mib) or 1)}
    return {"directory": str(root), "files": files_count, "bytes": total_bytes, "max_file_bytes": max_file,
            "entries_visited": entries, "truncated": truncated, "suggested_budgets": suggested,
            "note": "预估只读取目录项与元数据，不读文件内容；建议预算按总量和最大文件各上浮 5% 取整。"}


def compare(baseline, current):
    if baseline["id"] == current["id"]:
        raise InputError("请选择两个不同的快照进行对比，不能与自身比较。")
    if baseline["directory"] != current["directory"]:
        raise InputError("只能对比同一个目录的快照。")
    old = {x["path"]: x for x in baseline["files"]}
    new = {x["path"]: x for x in current["files"]}
    added, missing, changed, metadata, uncertain, same = [], [], [], [], [], []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            (added if baseline["coverage"]["inventory_complete"] else uncertain).append(path)
        elif path not in new:
            (missing if current["coverage"]["inventory_complete"] else uncertain).append(path)
        elif old[path]["sha256"] and new[path]["sha256"]:
            if old[path]["sha256"] != new[path]["sha256"]:
                changed.append(path)
            elif old[path]["mtime_ns"] != new[path]["mtime_ns"]:
                metadata.append(path)
            else:
                same.append(path)
        elif old[path]["size"] != new[path]["size"] or old[path]["mtime_ns"] != new[path]["mtime_ns"]:
            changed.append(path)
            uncertain.append(path)
        else:
            uncertain.append(path)
    complete = baseline["coverage"]["inventory_complete"] and current["coverage"]["inventory_complete"] and not uncertain
    return {"schema": "delivery-comparison/1", "baseline_id": baseline["id"], "current_id": current["id"],
            "directory": current["directory"], "baseline_hash": baseline["manifest_hash"], "current_hash": current["manifest_hash"],
            "same_manifest": baseline["manifest_hash"] == current["manifest_hash"], "content_comparison_complete": complete,
            "added": added, "missing": missing, "changed": changed, "metadata_changed": metadata, "uncertain": sorted(set(uncertain)), "same": same,
            "expected_added": sorted(set(current["expected"]) - set(baseline["expected"])),
            "expected_removed": sorted(set(baseline["expected"]) - set(current["expected"])),
            "human": acceptance_for(current), "message": "对比仅针对两次快照；目录当前状态未在此操作中重新扫描。"}


def accept(body):
    manifest = read_manifest(body.get("manifest_id"))
    if body.get("manifest_hash") != manifest["manifest_hash"]:
        raise InputError("快照哈希不一致，请重新选择快照。")
    if body.get("decision") not in {"accepted", "rejected"} or body.get("acknowledgment") is not True:
        raise InputError("请显式确认已经人工查看成品，并选择验收结果。")
    note = body.get("note", "")
    if not isinstance(note, str) or not note.strip() or len(note) > 2000:
        raise InputError("请填写人工查看的范围和结论（1–2000 字）。")
    if not manifest["files"] or not manifest["coverage"]["inventory_complete"] or any(x["hash_state"] != "hashed" for x in manifest["files"]):
        raise InputError("目录覆盖或文件哈希不完整，无法绑定人工验收。请提高预算并重新扫描。")
    record = {"id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8], "created_at": utc_now(),
              "directory": manifest["directory"], "manifest_id": manifest["id"], "manifest_hash": manifest["manifest_hash"],
              "operator": operator_name(),
              "snapshot_summary": {"files": manifest["summary"]["files"], "bytes": manifest["summary"]["bytes"],
                                   "directory_name": PurePosixPath(manifest["directory"].rstrip("/")).name or manifest["directory"]},
              "decision": body["decision"], "note": note.strip(), "explicit_confirmation": True, "published": False}
    record["record_hash"] = digest(record)
    write_json(ACCEPT_DIR / (record["id"] + ".json"), record)
    append_ledger("accept", id=record["id"], manifest_id=manifest["id"], manifest_hash=manifest["manifest_hash"],
                  decision=record["decision"], note_sha256=hashlib.sha256(record["note"].encode("utf-8")).hexdigest())
    return {"record": record, "human": acceptance_for(manifest), "message": "人工记录已保存并写入登记簿，绑定本次快照；发布未记录。"}


def list_manifests():
    values, corrupt = [], []
    if MANIFEST_DIR.exists():
        for p in sorted(MANIFEST_DIR.glob("*.json"), reverse=True)[:100]:
            try:
                m = read_manifest(p.stem)
                values.append({k: m[k] for k in ("id", "created_at", "directory", "manifest_hash", "summary", "levels")})
            except (OSError, ValueError) as exc:
                corrupt.append({"id": p.stem, "error": str(exc)[:200]})
    return values, corrupt


def audit():
    """登记簿与磁盘双向比对：发现被删除或被篡改的快照 / 人工验收记录。"""
    entries = read_ledger()
    scans = [e for e in entries if e.get("action") == "scan" and isinstance(e.get("id"), str)]
    accepts = [e for e in entries if e.get("action") == "accept" and isinstance(e.get("id"), str)]
    disk_manifests = {p.stem for p in MANIFEST_DIR.glob("*.json")} if MANIFEST_DIR.exists() else set()
    disk_accepts = {p.stem for p in ACCEPT_DIR.glob("*.json")} if ACCEPT_DIR.exists() else set()
    missing = [{"kind": "快照", "id": e["id"], "time": e.get("time", "")} for e in scans if e["id"] not in disk_manifests]
    missing += [{"kind": "人工验收", "id": e["id"], "time": e.get("time", "")} for e in accepts if e["id"] not in disk_accepts]
    tampered = []
    for e in accepts:
        p = ACCEPT_DIR / (e["id"] + ".json")
        if not p.exists():
            continue
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
            if record.get("manifest_hash") != e.get("manifest_hash") or record.get("decision") != e.get("decision"):
                tampered.append({"kind": "人工验收", "id": e["id"], "time": e.get("time", "")})
        except (OSError, ValueError):
            tampered.append({"kind": "人工验收", "id": e["id"], "time": e.get("time", "")})
    registered = {e["id"] for e in scans} | {e["id"] for e in accepts}
    unregistered = sorted((disk_manifests | disk_accepts) - registered)
    _, corrupt = list_manifests()
    return {"ledger_path": str(ledger_path()), "ledger_entries": len(entries), "missing": missing, "tampered": tampered,
            "corrupt": corrupt, "unregistered": unregistered[:50], "operator": operator_name(),
            "note": "登记簿只追加。missing=登记过但文件已不在；tampered=文件内容与登记不一致；unregistered=早于登记簿启用时写入的文件。"}


def api(method, path, query, body):
    try:
        if method == "GET" and path in {"/", "/config"}:
            return 200, {"name": "交付核验台", "schema": SCHEMA, "defaults": DEFAULTS, "maximums": MAXIMUMS,
                         "ffprobe_available": bool(shutil.which("ffprobe")), "limitations": LIMITATIONS,
                         "example_directory": None}
        if method == "GET" and path == "/manifests":
            values, corrupt = list_manifests()
            return 200, {"manifests": values, "corrupt": corrupt}
        if method == "GET" and path == "/audit":
            return 200, audit()
        if method == "GET" and path == "/estimate":
            return 200, estimate(query.get("directory", [""])[0])
        if method == "GET" and path == "/manifest":
            m = read_manifest(query.get("id", [""])[0])
            m["human"] = acceptance_for(m)
            return 200, m
        if method == "POST" and path == "/scan":
            return 200, scan(body.get("directory"), body.get("expected"), body.get("budgets"))
        if method == "POST" and path == "/compare":
            return 200, compare(read_manifest(body.get("baseline_id")), read_manifest(body.get("current_id")))
        if method == "POST" and path == "/accept":
            return 200, accept(body)
        return _shared.not_allowed_or_missing(path, {"/", "/config", "/manifests", "/audit", "/estimate", "/manifest", "/scan", "/compare", "/accept"})
    except Exception as exc:
        return _shared.api_error(exc)


def exit_status(value, comparison=False):
    if comparison:
        if (value["missing"] or value["changed"] or value["added"] or value["metadata_changed"]
                or value["expected_added"] or value["expected_removed"]):
            return 1
        return 0 if value["content_comparison_complete"] else 3
    if value["summary"]["errors"]:
        return 1
    return 3 if value["summary"]["warnings"] or not value["coverage"]["inventory_complete"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="通用文件证据台：不修改扫描目录，不推断人工验收或发布。")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "check"):
        p = sub.add_parser(name)
        p.add_argument("directory")
        p.add_argument("--expected", action="append", default=[])
        p.add_argument("--no-save", action="store_true")
        p.add_argument("--large-hash", action="store_true", help="显式允许单文件 4 GiB、总计 8 GiB；仍受时间预算约束")
        p.add_argument("--seconds", type=float, default=DEFAULTS["seconds"])
    p = sub.add_parser("compare")
    p.add_argument("baseline_id")
    p.add_argument("current_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare(read_manifest(args.baseline_id), read_manifest(args.current_id))
        else:
            options = {"seconds": args.seconds}
            if args.large_hash:
                options.update(hash_file_mb=4096, hash_total_mb=8192)
            result = scan(args.directory, args.expected, options, save=not args.no_save and args.command == "scan")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_status(result, args.command == "compare")
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
