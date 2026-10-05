"""只读本机运行台。无网络探测，无后台监控，无进程控制。"""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from apps import _shared

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "runtime"
GIB = 1024 ** 3
TIMEOUT = 4
MAX_OUTPUT = 2 * 1024 * 1024
SCHEMA = "runtime-snapshot/1"
LOG_ROOTS = (pathlib.Path("/Library/Logs/DiagnosticReports"), pathlib.Path.home() / "Library/Logs/DiagnosticReports")
LOG_DIRS = tuple(directory for root in LOG_ROOTS for directory in (root, root / "Retired"))
LIMITS = [
    "这是顺序略有重叠的短时采样，各命令有独立时间；进程可能在采样期间退出。",
    "进程 RSS 是驻留页指标，包含共享页，不能相加代表物理占用，也不能当作 MLX/Metal 统一内存 footprint。",
    "空闲物理页不等于可分配上限；系统回收、缓存、GPU 峰值和未来并发都未测量。",
    "监听端口只证明当时存在监听者，不证明服务健康、模型已加载、配置生效或正在推理。",
    "进程名称与 cwd 可帮助追踪归属，无法直接证明是哪次聊天启动或哪个请求导致峰值。",
    "权限不足、工具缺失、超时和解析失败会保留为未知；不读取进程环境、完整参数或凭据文件。",
]


def now():
    return _shared.utc_now("milliseconds")


def scrub(text):
    """额外保护可被进程自行修改的 comm 和系统日志短片段。"""
    return _shared.redact(text, mode="light")


def run(args, *, allow_empty=False, include_output=True):
    started = now()
    t = time.monotonic()
    binary = shutil.which(args[0], path="/usr/bin:/bin:/usr/sbin:/sbin")
    result = {"command": args, "started_at": started, "status": "unknown", "duration_ms": None, "exit_code": None, "error": None, "output": None}
    if not binary:
        result["error"] = "工具缺失"
        return result
    try:
        proc = subprocess.run([binary, *args[1:]], capture_output=True, text=True, errors="replace", timeout=TIMEOUT, env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"})
        result["exit_code"] = proc.returncode
        if len(proc.stdout.encode("utf-8")) > MAX_OUTPUT:
            result["error"] = "输出超出采样上限"
        elif proc.returncode == 0:
            result["status"] = "ok"
        elif allow_empty and proc.returncode == 1 and not proc.stdout.strip() and not proc.stderr.strip():
            result["status"] = "empty"
        else:
            result["error"] = scrub(proc.stderr.strip()[:400]) or "命令未成功完成"
        if result["status"] in ("ok", "empty"):
            result["output"] = proc.stdout if include_output else None
    except subprocess.TimeoutExpired:
        result["error"] = "采样超时（4 秒）"
    except OSError as exc:
        result["error"] = scrub(str(exc))[:400]
    result["duration_ms"] = round((time.monotonic() - t) * 1000)
    return result


def parse_vm(text):
    page_match = re.search(r"page size of\s+(\d+)\s+bytes", text)
    if not page_match or int(page_match[1]) <= 0:
        raise ValueError("vm_stat 未提供有效页大小")
    size = int(page_match[1])
    pages = {}
    for line in text.splitlines():
        match = re.match(r'\s*"?([^:"]+)"?:\s*(\d+)\.?(?:\s*)$', line)
        if match:
            pages[match[1].strip()] = int(match[2])
    expected = ("Pages free", "Pages active", "Pages inactive", "Pages wired down", "Pages occupied by compressor")
    if not all(k in pages for k in expected):
        raise ValueError("vm_stat 核心字段不完整")
    return {"page_size_bytes": size, "pages": pages, "free_bytes": pages["Pages free"] * size, "active_bytes": pages["Pages active"] * size, "inactive_bytes": pages["Pages inactive"] * size, "wired_bytes": pages["Pages wired down"] * size, "compressed_bytes": pages["Pages occupied by compressor"] * size, "stored_in_compressor_bytes": pages.get("Pages stored in compressor", 0) * size if "Pages stored in compressor" in pages else None}


def parse_swap(text):
    values = {}
    scale = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": GIB, "T": GIB * 1024}
    for key in ("total", "used", "free"):
        match = re.search(r"\b" + key + r"\s*=\s*([0-9]+(?:\.[0-9]+)?)([BKMGT])\b", text)
        if not match:
            raise ValueError("交换空间字段不完整")
        values[key + "_bytes"] = round(float(match[1]) * scale[match[2]])
    return values


def parse_ps(text):
    processes, skipped = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        columns = line.split(None, 10)
        try:
            if len(columns) != 11:
                raise ValueError("列数不完整")
            pid, ppid, rss = map(int, columns[:3])
            cpu = float(columns[3])
            if pid <= 0 or ppid < 0 or rss < 0 or not math.isfinite(cpu) or cpu < 0:
                raise ValueError("进程数值无效")
            started = dt.datetime.strptime(" ".join(columns[5:10]), "%a %b %d %H:%M:%S %Y").isoformat()
            command = scrub(columns[10])[:700]
            processes.append({"pid": pid, "ppid": ppid, "rss_bytes": rss * 1024, "cpu_percent": cpu, "elapsed": columns[4], "started_local": started, "command": command, "name": command.rsplit("/", 1)[-1]})
        except (ValueError, OverflowError):
            skipped += 1
    if text.strip() and not processes:
        raise ValueError("无法解析进程摘要")
    return sorted(processes, key=lambda p: p["rss_bytes"], reverse=True), skipped


def parse_listeners(text):
    listeners, pid, command = [], None, None
    for line in text.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else None
            command = None
        elif line.startswith("c"):
            command = scrub(line[1:])[:200]
        elif line.startswith("n") and pid:
            match = re.match(r"^(.*):(\d+)$", line[1:])
            if match and 0 < int(match[2]) <= 65535:
                address = match[1]
                listeners.append({"pid": pid, "command": command, "address": address, "port": int(match[2]), "loopback": address in ("127.0.0.1", "[::1]", "::1", "localhost"), "endpoint": line[1:]})
    unique = {(x["pid"], x["endpoint"]): x for x in listeners}
    if text.strip() and not unique:
        raise ValueError("无法解析 TCP 监听输出")
    return sorted(unique.values(), key=lambda p: (p["port"], p["pid"]))


def parsed(command_result, parser):
    if command_result["status"] not in ("ok", "empty"):
        return None
    try:
        value = parser(command_result["output"] or "")
        return value
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        command_result["status"] = "unknown"
        command_result["error"] = str(exc)
        return None


def last_number(text):
    match = re.search(r":\s*(\d+)\s*$", text.strip())
    if not match:
        raise ValueError("sysctl 数值缺失")
    return int(match[1])


def pressure_percent(text):
    match = re.search(r"System-wide memory free percentage:\s*(\d+)%", text)
    if not match or not 0 <= int(match[1]) <= 100:
        raise ValueError("压力查询未返回有效比例")
    return int(match[1])


def snapshot():
    start = now()
    commands = {
        "vm": ["vm_stat"], "total": ["sysctl", "hw.memsize"],
        "swap": ["sysctl", "vm.swapusage"], "pressure_level": ["sysctl", "kern.memorystatus_vm_pressure_level"],
        "pressure_query": ["memory_pressure", "-Q"],
        "processes": ["ps", "-axo", "pid=,ppid=,rss=,pcpu=,etime=,lstart=,comm="],
        "listeners": ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpcn"],
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = {key: pool.submit(run, args, allow_empty=key == "listeners") for key, args in commands.items()}
        results = {key: job.result() for key, job in futures.items()}
    vm = parsed(results["vm"], parse_vm)
    total = parsed(results["total"], last_number)
    if total is not None and total <= 0:
        results["total"]["status"] = "unknown"
        results["total"]["error"] = "物理内存总量无效"
        total = None
    swap = parsed(results["swap"], parse_swap)
    level = parsed(results["pressure_level"], last_number)
    if level is not None and level not in (1, 2, 4):
        results["pressure_level"].update(status="unknown", error="系统压力等级未识别")
    percent = parsed(results["pressure_query"], pressure_percent)
    proc_data = parsed(results["processes"], parse_ps)
    listeners = parsed(results["listeners"], parse_listeners)
    processes, skipped = proc_data if proc_data is not None else (None, None)
    if skipped:
        results["processes"]["status"] = "partial"
        results["processes"]["error"] = f"{skipped} 行进程记录无法解析"
    pmap = {p["pid"]: p for p in processes or []}
    for listener in listeners or []:
        owner = pmap.get(listener["pid"])
        listener.update({"owner_status": "observed" if owner else "unknown", "owner_name": owner["name"] if owner else None, "owner_started_local": owner["started_local"] if owner else None, "owner_ppid": owner["ppid"] if owner else None})
    # 不保留原始进程表，避免把进程自行改写的名称当成安全参数输出。
    results["processes"]["output"] = None
    results["processes"]["parsed_count"] = len(processes) if processes is not None else None
    for value in results.values():
        if value["output"] is not None:
            value["output"] = scrub(value["output"])[:7000]
    pressure_label = {1: "normal", 2: "warning", 4: "critical"}.get(level, "unknown")
    return {"schema": SCHEMA, "captured_at": start, "completed_at": now(), "platform": os.uname().sysname, "status": "ok" if all(x["status"] in ("ok", "empty") for x in results.values()) else "partial", "memory": {"total_bytes": total, "vm": vm, "swap": swap, "pressure_level": level, "pressure_status": pressure_label, "pressure_query_free_percent": percent, "metric_notes": {"free": "vm_stat 的空闲物理页；不含系统可能回收的缓存。", "compressed": "压缩器当前占用物理页，压缩前保存的页数量另列，不相加。", "swap": "已用交换空间可含之前工作留下的页；单次值无法证明当前频繁换页。", "pressure_query": "memory_pressure -Q 的系统报告比例，与空闲物理页比例口径不同，不参与容量计算。"}}, "processes": {"status": results["processes"]["status"], "items": processes, "skipped": skipped}, "listeners": {"status": results["listeners"]["status"], "items": listeners, "scope": "当前权限可见的 TCP LISTEN；没有 UDP 检查，也没有健康探测。"}, "evidence": results, "limits": LIMITS}


def check_preflight(sample, extra_gib, task):
    if isinstance(extra_gib, bool) or not isinstance(extra_gib, (int, float)) or not math.isfinite(extra_gib) or not 0 <= extra_gib <= 4096:
        raise ValueError("预计新增内存需为 0 至 4096 的有限数值（GiB）")
    if task not in ("inference", "tts", "render"):
        raise ValueError("任务类型需为 inference、tts 或 render")
    mem = sample.get("memory") or {}
    total, vm, swap = mem.get("total_bytes"), mem.get("vm"), mem.get("swap")
    level = mem.get("pressure_status", "unknown")
    missing, triggers, notes = [], [], []
    if total is None:
        missing.append("物理内存总量")
    if not vm or vm.get("free_bytes") is None or vm.get("compressed_bytes") is None:
        missing.append("空闲页与压缩区")
    if level == "unknown":
        missing.append("内存压力等级")
    if not swap or swap.get("used_bytes") is None:
        missing.append("交换空间")
    reserve = max(8 * GIB, total * 0.1) if total else None
    projected = vm["free_bytes"] - extra_gib * GIB if vm and vm.get("free_bytes") is not None else None
    if level in ("warning", "critical"):
        triggers.append("系统当前已报告内存压力告警" if level == "warning" else "系统当前已报告严重内存压力")
    if reserve is not None and projected is not None and projected < reserve:
        triggers.append("按空闲页扣除预计新增用量后，低于保守预留线")
    if total and vm and vm.get("compressed_bytes") is not None and vm["compressed_bytes"] / total >= 0.15:
        triggers.append("压缩器当前占用达到物理内存的 15%")
    if total and swap and swap.get("used_bytes") is not None and swap["used_bytes"] / total >= 0.05:
        triggers.append("已用交换空间达到物理内存的 5%；需结合两次快照核对趋势")
    if swap and swap.get("used_bytes", 0) > 0:
        notes.append("已有交换空间占用，可能来自此前工作；这一数值不能单独证明当前换页严重。")
    status = "warning" if triggers else "unknown" if missing else "within_rule"
    labels = {"warning": "触及保守警示线", "unknown": "证据不足，无法判断", "within_rule": "本次采样暂未触及规则线"}
    recommendation = "先推迟新增重任务，核对现有模型、TTS 和渲染的实际运行状态，再手动复查。" if triggers else "补齐采样失败项后再判断；不要把未知当成零占用。" if missing else "采用串行、短任务试跑，并手动比较前后快照；仍需为未测得的峰值留余量。"
    task_note = {"inference": "推理的模型、上下文、KV 缓存和 Metal 峰值均未测量。", "tts": "TTS 的驻留大小无法代表长文本生成峰值；当前未核验请求长度和生成参数。", "render": "渲染的分辨率、并发、素材解码和 GPU 峰值均未测量。"}[task]
    return {"status": status, "label": labels[status], "task": task, "extra_gib": extra_gib, "captured_at": sample["captured_at"], "projected_free_bytes": projected, "reserve_bytes": reserve, "triggers": triggers, "missing": missing, "notes": notes + [task_note], "recommendation": recommendation, "rule": "仅用 vm_stat 空闲页作为严格起点，预留 max(8 GiB, 物理内存 10%)；压缩区 15%、已用交换空间 5% 或系统压力告警触发提示。规则是本工具的保守筛查策略，未经容量实验校准。", "limit": "这不是安全承诺、可用内存预测或模型容量验证；快照之后的状态可能变化。"}


def process_detail(pid):
    result = run(["ps", "-axo", "pid=,ppid=,rss=,pcpu=,etime=,lstart=,comm="])
    parsed_data = parsed(result, parse_ps)
    processes, _ = parsed_data if parsed_data else ([], 0)
    pmap = {p["pid"]: p for p in processes}
    current = pmap.get(pid)
    result["output"] = None
    if not current:
        return {"status": "unknown", "pid": pid, "error": "采样中未找到此 PID，可能已退出或不可见", "evidence": {"process": result}}
    cwd_result = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], allow_empty=True)
    cwd = next((scrub(line[1:]) for line in (cwd_result["output"] or "").splitlines() if line.startswith("n")), None)
    # 再读启动时间，避免把已退出后重用的 PID 归给之前的进程。
    confirm = run(["ps", "-p", str(pid), "-o", "pid=,ppid=,rss=,pcpu=,etime=,lstart=,comm="])
    confirm_data = parsed(confirm, parse_ps)
    confirm["output"] = None
    same = bool(confirm_data and confirm_data[0] and confirm_data[0][0]["started_local"] == current["started_local"] and confirm_data[0][0]["command"] == current["command"])
    chain, seen, item = [], set(), current
    while item and item["pid"] not in seen and len(chain) < 10:
        chain.append(item)
        seen.add(item["pid"])
        item = pmap.get(item["ppid"])
    if not same:
        cwd = None
    if cwd_result["output"] is not None:
        cwd_result["output"] = scrub(cwd_result["output"])[:1000]
    return {"status": "observed" if same and cwd else "partial" if same else "unknown", "pid": pid, "process": current, "cwd": cwd, "cwd_status": "observed" if cwd else "unknown", "identity_confirmed": same, "parent_chain": chain, "error": None if same else "进程已退出或 PID 身份变化，目录归属未确认", "evidence": {"process": result, "cwd": cwd_result, "identity_recheck": confirm}, "limits": ["父进程链最多 10 层，记录采样时关系，不能追溯已退出的启动进程或指定历史聊天。", "cwd 只是当前工作目录，不证明目录中的配置已经在此进程生效。"]}


def incident_extract(text):
    objects = []
    try:
        objects.append(json.loads(text))
    except json.JSONDecodeError:
        first, _, rest = text.partition("\n")
        for part in (first, rest):
            try:
                objects.append(json.loads(part))
            except json.JSONDecodeError:
                pass
    fields, timestamps = [], []
    wanted = {"panicstring", "panic_string", "reason", "largestprocess", "boot faults", "reset count", "terminationreason"}
    def walk(obj, depth=0):
        if depth > 8 or len(fields) >= 10:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key.lower() in ("timestamp", "capturetime", "date") and isinstance(value, str):
                    timestamps.append(value[:100])
                if key.lower() in wanted and isinstance(value, (str, int, float)):
                    snippet = scrub(str(value).split("\n")[0])[:350]
                    if snippet and {"field": key, "text": snippet} not in fields:
                        fields.append({"field": key, "text": snippet})
                if key.lower() in ("memorypressure", "memorystatus") and isinstance(value, (str, int, float)):
                    fields.append({"field": key, "text": scrub(str(value))[:120]})
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
                if len(fields) >= 10:
                    break
        elif isinstance(obj, list):
            # Jetsam 的被终止进程可能排在千条记录之后；只读小于 2 MiB 的已加载 JSON。
            for value in obj[:5000]:
                walk(value, depth + 1)
    for obj in objects:
        walk(obj)
    if not fields:
        for line in text.splitlines():
            if re.search(r"(?i)panicString|watchdog timeout|vm-compressor-space-shortage|Boot faults|Reset count", line):
                fields.append({"field": "原文短片段", "text": scrub(line.strip())[:350]})
                if len(fields) >= 5:
                    break
    reason_seen = any(x["field"].lower() in ("panicstring", "panic_string", "reason", "boot faults", "terminationreason", "原文短片段") for x in fields)
    return {"reported_time": timestamps[0] if timestamps else None, "snippets": fields[:10], "extraction_status": "observed" if fields else "unknown", "reason_status": "observed" if reason_seen else "unknown"}


def incidents():
    candidates, coverage = [], []
    for directory in LOG_DIRS:
        entry = {"directory": str(directory), "status": "ok", "error": None}
        try:
            for path in directory.iterdir():
                if not re.search(r"(?i)panic|jetsam|resetcounter|kernel", path.name) or path.suffix.lower() not in (".ips", ".panic", ".log", ".diag"):
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                candidates.append((stat.st_mtime, stat.st_size, path))
        except OSError as exc:
            entry.update(status="unknown", error=scrub(str(exc))[:250])
        coverage.append(entry)
    reports = []
    for modified, size, path in sorted(candidates, reverse=True)[:20]:
        record = {"file": path.name, "directory": str(path.parent), "modified_at": dt.datetime.fromtimestamp(modified, dt.timezone.utc).isoformat(), "size_bytes": size, "status": "unknown", "reported_time": None, "snippets": []}
        if size > MAX_OUTPUT:
            record["error"] = "报告超过 2 MiB，保留文件信息，未读取正文"
        else:
            try:
                # 不跟随枚举之后被替换的符号链接，也不读取选定目录外的文件。
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as handle:
                    data = handle.read(MAX_OUTPUT + 1)
                if len(data) > MAX_OUTPUT:
                    raise ValueError("读取期间报告超过上限")
                record.update(incident_extract(data.decode("utf-8", errors="replace")))
                record["status"] = record["extraction_status"]
            except (OSError, ValueError) as exc:
                record["error"] = scrub(str(exc))[:250]
        reports.append(record)
    return {"captured_at": now(), "status": "partial" if any(x["status"] == "unknown" for x in coverage) else "ok", "items": reports, "coverage": coverage, "matched_count": len(candidates), "limit": "仅枚举两个 DiagnosticReports 根目录及各自 Retired 子目录的顶层匹配报告，不进一步递归。最多读取最新 20 个、每个不超过 2 MiB。文件时间可用作线索，报告内时间优先。历史报告不能解释当前状态，也不能仅凭最大进程判定责任。"}


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r"\d{8}T\d{6}Z-[a-f0-9]{12}", value))


def load_saved(identifier):
    if not valid_id(identifier):
        raise ValueError("快照标识无效")
    path = DATA / (identifier + ".json")
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("未找到保存的快照")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if value.get("schema") != SCHEMA or not isinstance(value.get("snapshot"), dict):
        raise ValueError("保存文件格式不受支持")
    return value


def persist_sample(sample, name, source="manual"):
    if not isinstance(name, str) or len(name) > 100:
        raise ValueError("快照名称需为不超过 100 字符的文本")
    identifier = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    value = {"schema": SCHEMA, "id": identifier, "name": name.strip() or "手动采样", "source": source, "saved_at": now(), "snapshot": sample}
    _shared.write_json_atomic(DATA / (identifier + ".json"), value)
    return value


def save_sample(name):
    return persist_sample(snapshot(), name)


def saved_list():
    items, errors = [], []
    if DATA.exists():
        for path in sorted(DATA.glob("*.json"), reverse=True)[:200]:
            if not valid_id(path.stem):
                continue
            try:
                value = load_saved(path.stem)
                sample = value["snapshot"]
                items.append({"id": value["id"], "name": value["name"], "source": value.get("source", "manual"), "saved_at": value["saved_at"], "captured_at": sample["captured_at"], "status": sample["status"], "pressure_status": sample["memory"]["pressure_status"]})
            except (ValueError, OSError, KeyError) as exc:
                errors.append({"id": path.stem, "error": scrub(str(exc))[:200]})
    items.sort(key=lambda value: (value["captured_at"], value["saved_at"], value["id"]), reverse=True)
    return {"items": items, "errors": errors, "directory": str(DATA), "limit": "最多列出最新 200 个文件；快照为手动保存，不自动监控。"}


def compare_samples(before, after):
    def metric(sample, path):
        value = sample
        for key in path:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    metrics = {"free_bytes": ("memory", "vm", "free_bytes"), "compressed_bytes": ("memory", "vm", "compressed_bytes"), "wired_bytes": ("memory", "vm", "wired_bytes"), "swap_used_bytes": ("memory", "swap", "used_bytes"), "compressions": ("memory", "vm", "pages", "Compressions"), "decompressions": ("memory", "vm", "pages", "Decompressions"), "swapins": ("memory", "vm", "pages", "Swapins"), "swapouts": ("memory", "vm", "pages", "Swapouts")}
    changes = []
    for name, path in metrics.items():
        left, right = metric(before, path), metric(after, path)
        delta = right - left if left is not None and right is not None else None
        changes.append({"metric": name, "before": left, "after": right, "delta": delta, "status": "unknown" if delta is None or name in ("compressions", "decompressions", "swapins", "swapouts") and delta < 0 else "observed"})
    def pmap(sample):
        return {(p["pid"], p.get("started_local"), p["command"]): p for p in sample.get("processes", {}).get("items") or []}
    left_map, right_map = pmap(before), pmap(after)
    process_changes = [{"pid": key[0], "name": right_map[key]["name"], "started_local": key[1], "rss_delta_bytes": right_map[key]["rss_bytes"] - left_map[key]["rss_bytes"]} for key in left_map.keys() & right_map.keys()]
    process_changes.sort(key=lambda p: abs(p["rss_delta_bytes"]), reverse=True)
    proc_known = before.get("processes", {}).get("status") == "ok" and after.get("processes", {}).get("status") == "ok"
    def lmap(sample):
        return {(x["pid"], x.get("owner_started_local"), x["endpoint"]): x for x in sample.get("listeners", {}).get("items") or []}
    left_ports, right_ports = lmap(before), lmap(after)
    ports_known = before.get("listeners", {}).get("status") in ("ok", "empty") and after.get("listeners", {}).get("status") in ("ok", "empty")
    return {"before_captured_at": before["captured_at"], "after_captured_at": after["captured_at"], "memory_changes": changes, "processes": {"status": "observed" if proc_known else "unknown", "matched_rss_changes": process_changes[:50], "new": [right_map[k] for k in right_map.keys() - left_map.keys()] if proc_known else None, "gone": [left_map[k] for k in left_map.keys() - right_map.keys()] if proc_known else None}, "listeners": {"status": "observed" if ports_known else "unknown", "new": [right_ports[k] for k in right_ports.keys() - left_ports.keys()] if ports_known else None, "gone": [left_ports[k] for k in left_ports.keys() - right_ports.keys()] if ports_known else None}, "limit": "差异按 PID、启动时间和执行名称匹配；进程 RSS 不累加为系统用量。采样中间的瞬时峰值未记录。累计页计数倒退时可能重启或计数重置，该项记为未知。"}


def api(method, path, query, body):
    try:
        if method == "GET" and path in ("/", "/snapshot"):
            return 200, snapshot()
        if method == "GET" and path == "/process":
            pid_string = (query.get("pid") or [""])[0]
            if not isinstance(pid_string, str) or not re.fullmatch(r"[1-9]\d{0,8}", pid_string):
                return 400, {"error": "PID 需为有效正整数"}
            return 200, process_detail(int(pid_string))
        if method == "GET" and path == "/incidents":
            return 200, incidents()
        if method == "GET" and path == "/snapshots":
            return 200, saved_list()
        if method == "GET" and path.startswith("/snapshots/"):
            return 200, load_saved(path.removeprefix("/snapshots/"))
        if method == "POST" and path == "/snapshots":
            return 201, save_sample(body.get("name", ""))
        if method == "POST" and path == "/preflight":
            sample = snapshot()
            result = {"check": check_preflight(sample, body.get("extra_gib"), body.get("task")), "snapshot": sample}
            if body.get("save") is True:
                # 同一次采样既出检查结论又落快照，避免「检查与留底不同源」。
                saved = persist_sample(sample, body.get("name", ""), source="preflight")
                result["saved"] = {"id": saved["id"], "name": saved["name"], "saved_at": saved["saved_at"]}
            return 200, result
        if method == "POST" and path == "/compare":
            left, right = load_saved(body.get("before")), load_saved(body.get("after"))
            if left["snapshot"]["captured_at"] > right["snapshot"]["captured_at"]:
                return 400, {"error": "后一次快照的采样时间早于前一次，请交换选择"}
            return 200, {"before_id": left["id"], "after_id": right["id"], "comparison": compare_samples(left["snapshot"], right["snapshot"])}
        return _shared.not_allowed_or_missing(path, {"/", "/snapshot", "/process", "/incidents", "/snapshots", "/preflight", "/compare"})
    except Exception as exc:
        return _shared.api_error(exc, scrub=scrub)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="本机运行台只读采样，不启动服务器")
    parser.add_argument("command", choices=("snapshot", "incidents", "process", "preflight"), nargs="?", default="snapshot")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--extra-gib", type=float, default=0)
    parser.add_argument("--task", choices=("inference", "tts", "render"), default="inference")
    args = parser.parse_args()
    status, payload = api("POST" if args.command == "preflight" else "GET", "/" + args.command, {"pid": [str(args.pid)]}, {"extra_gib": args.extra_gib, "task": args.task})
    print(json.dumps({"http_status": status, "data": payload}, ensure_ascii=False, indent=2, allow_nan=False))
