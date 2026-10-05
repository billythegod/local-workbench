"""只读历史适配器、可增量的本地索引，以及父服务器调用的 API。"""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apps import _shared

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "history"
DB = DATA / "index.sqlite"
MAX_LINE = 8 * 1024 * 1024
MAX_TEXT = 65536
SCHEMA_VERSION = "1"
LOCK = threading.Lock()


@dataclass
class Sources:
    codex_db: Path = Path.home() / ".codex" / "state_5.sqlite"
    codex_roots: tuple = (Path.home() / ".codex" / "sessions", Path.home() / ".codex" / "archived_sessions")
    claude_root: Path = Path.home() / ".claude" / "projects"
    opencode_db: Path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    codex_history_db: Path | None = Path.home() / ".codex" / "thread_history_1.sqlite"
    pi_root: Path | None = Path.home() / ".pi" / "agent" / "sessions"
    hermes_db: Path | None = Path.home() / ".hermes" / "state.db"
    zcode_db: Path | None = Path.home() / ".zcode" / "cli" / "db" / "db.sqlite"
    zcode_tasks_db: Path | None = Path.home() / ".zcode" / "v2" / "tasks-index.sqlite"


def now():
    return _shared.utc_now()


def timestamp(value):
    if value is None:
        return ""
    try:
        if isinstance(value, (int, float)):
            if value > 10**11:
                value /= 1000
            return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="milliseconds")
        value = str(value)
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError, TypeError):
        return ""


# 脱敏规则的唯一实现在 apps/_shared.py；full 模式用于入库正文与标题。
redact = _shared.redact


def clipped(text):
    text = redact(text)
    n = len(text)
    if n > MAX_TEXT:
        text = text[:49152] + "\n[中间部分超出索引上限，未纳入搜索]\n" + text[-16384:]
    return text, n, int(n > MAX_TEXT)


def connect(path=DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path), timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=20000")
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
        row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row and row[0] == SCHEMA_VERSION:
            return c
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      key TEXT PRIMARY KEY,source TEXT NOT NULL,native_id TEXT NOT NULL,title TEXT NOT NULL DEFAULT '',
      title_fold TEXT NOT NULL DEFAULT '',project TEXT NOT NULL DEFAULT '',created TEXT DEFAULT '',updated TEXT DEFAULT '',
      is_subagent INTEGER NOT NULL DEFAULT 0,parent_key TEXT DEFAULT '',agent_path TEXT DEFAULT '',
      archived INTEGER DEFAULT 0,source_path TEXT DEFAULT '',storage TEXT DEFAULT '',last_seen_job TEXT DEFAULT '');
    CREATE INDEX IF NOT EXISTS sessions_filter ON sessions(source,is_subagent,updated);
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY,session_key TEXT NOT NULL REFERENCES sessions(key) ON DELETE CASCADE,
      role TEXT NOT NULL,time TEXT NOT NULL DEFAULT '',text TEXT NOT NULL,text_fold TEXT NOT NULL,
      source_path TEXT NOT NULL,line_no INTEGER,row_locator TEXT NOT NULL DEFAULT '',
      raw_chars INTEGER NOT NULL,truncated INTEGER NOT NULL DEFAULT 0,content_hash TEXT NOT NULL,
      fingerprint TEXT NOT NULL UNIQUE,kind TEXT DEFAULT 'final');
    CREATE INDEX IF NOT EXISTS messages_session ON messages(session_key,time,id);
    CREATE INDEX IF NOT EXISTS messages_path ON messages(source_path);
    CREATE TABLE IF NOT EXISTS files(
      path TEXT PRIMARY KEY,source TEXT NOT NULL,size INTEGER,mtime_ns INTEGER,indexed_size INTEGER,indexed_mtime_ns INTEGER,
      lines INTEGER DEFAULT 0,indexed_messages INTEGER DEFAULT 0,skipped_large INTEGER DEFAULT 0,
      malformed INTEGER DEFAULT 0,duplicates INTEGER DEFAULT 0,truncated INTEGER DEFAULT 0,
      status TEXT DEFAULT '',error TEXT DEFAULT '',last_seen_job TEXT DEFAULT '',updated TEXT DEFAULT '');
    """)
    c.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (SCHEMA_VERSION,))
    c.commit()
    return c


def read_db(path):
    # mode=ro 仍读取 WAL 中已提交的记录；不对历史库设置 PRAGMA、不备份凭据。
    c = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def json_object(value):
    if isinstance(value, dict):
        return value
    try:
        obj = json.loads(value or "{}")
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def subagent_info(value, thread_source=""):
    value = json_object(value)
    sub = value.get("subagent")
    info = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
    return bool(sub or thread_source in ("subagent", "guardian_review")), str(info.get("parent_thread_id") or ""), str(info.get("agent_path") or "")


def upsert_session(c, s, job):
    s = dict(s)
    s["title"] = redact(s.get("title", ""))[:500]
    s["project"] = redact(s.get("project", ""))[:1500]
    s["title_fold"] = s["title"].casefold()
    fields = ["key", "source", "native_id", "title", "title_fold", "project", "created", "updated", "is_subagent", "parent_key", "agent_path", "archived", "source_path", "storage"]
    values = [s.get(k, 0 if k in ("is_subagent", "archived") else "") for k in fields] + [job]
    # 文件元数据不含标题时，保留原生数据库中的标题。
    updates = [f"{k}=CASE WHEN excluded.{k}='' THEN sessions.{k} ELSE excluded.{k} END" if k in ("title", "title_fold", "project", "created", "updated", "agent_path", "storage", "parent_key") else f"{k}=excluded.{k}" for k in fields if k != "key"]
    c.execute("INSERT INTO sessions(" + ",".join(fields) + ",last_seen_job) VALUES(" + ",".join("?" for _ in values) + ") ON CONFLICT(key) DO UPDATE SET " + ",".join(updates) + ",last_seen_job=excluded.last_seen_job", values)


def blocks_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text") and isinstance(x.get("text"), str))


def user_text(text):
    # 自动注入的工作目录/规则块不当成实际需求；不索引工具输出块。
    if text.lstrip().startswith(("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>", "<system-reminder>")):
        return ""
    return text


def seconds(value):
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return 0


def insert_message(c, candidate, seen):
    raw = candidate.get("text") or ""
    if not raw.strip():
        return 0, 0
    role = candidate["role"]
    digest = hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()
    key = (candidate["session_key"], role, digest)
    previous = seen.get(key)
    variant = candidate.get("variant", "")
    when = timestamp(candidate.get("time"))
    if previous and previous[0] != variant and abs(seconds(when) - seconds(previous[1])) <= 15:
        return 0, 1
    seen[key] = (variant, when)
    text, raw_chars, truncated = clipped(raw)
    fingerprint = hashlib.sha256((candidate["session_key"] + role + digest + when).encode()).hexdigest()
    cur = c.execute("""INSERT OR IGNORE INTO messages(session_key,role,time,text,text_fold,source_path,line_no,row_locator,raw_chars,truncated,content_hash,fingerprint,kind)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (candidate["session_key"], role, when, text, text.casefold(), str(candidate["source_path"]), candidate.get("line_no"), candidate.get("row_locator", ""), raw_chars, truncated, digest, fingerprint, candidate.get("kind", "final")))
    return cur.rowcount, int(cur.rowcount == 0)


def bounded_lines(path, max_line=MAX_LINE):
    with Path(path).open("rb") as f:
        n = 0
        while True:
            line = f.readline(max_line + 1)
            if not line:
                break
            n += 1
            if len(line) > max_line:
                while line and not line.endswith(b"\n"):
                    line = f.readline(max_line + 1)
                yield n, None, "large"
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    raise ValueError("not an object")
                yield n, d, ""
            except (ValueError, UnicodeDecodeError):
                yield n, None, "malformed"


def file_metadata(source, path, d):
    if source == "codex":
        p = d.get("payload", {})
        if d.get("type") != "session_meta" or not isinstance(p, dict) or not p.get("id"):
            return None
        native = str(p["id"])
        sub, parent, agent_path = subagent_info(p.get("source"), p.get("thread_source"))
        return {"key": "codex:" + native, "source": source, "native_id": native, "project": p.get("cwd", ""), "created": timestamp(p.get("timestamp") or d.get("timestamp")), "is_subagent": int(sub), "parent_key": "codex:" + parent if parent else "", "agent_path": agent_path, "source_path": str(path), "archived": int("archived_sessions" in Path(path).parts), "storage": "jsonl"}
    if source == "pi":
        if d.get("type") != "session" or not d.get("id"):
            return None
        return {"key": "pi:" + str(d["id"]), "source": source, "native_id": str(d["id"]), "project": d.get("cwd", ""), "created": timestamp(d.get("timestamp")), "source_path": str(path), "storage": "jsonl"}
    native = str(d.get("sessionId") or Path(path).stem)
    is_sub = "subagents" in Path(path).parts or bool(d.get("isSidechain"))
    if is_sub:
        leaf = str(d.get("agentId") or Path(path).stem.removeprefix("agent-"))
        key = "claude:" + native + ":agent:" + leaf
    else:
        # 主日志可能含从父会话继承的记录，以本文件身份而非首条继承消息的 sessionId 定位。
        native = Path(path).stem
        key = "claude:" + native
    return {"key": key, "source": source, "native_id": native, "project": d.get("cwd", ""), "created": timestamp(d.get("timestamp")), "is_subagent": int(is_sub), "parent_key": "claude:" + native if is_sub else "", "agent_path": str(d.get("agentId") or ""), "source_path": str(path), "storage": "jsonl"}


def file_candidates(source, path, line, d, session_key, pending):
    base = {"session_key": session_key, "source_path": str(path), "line_no": line, "time": d.get("timestamp"), "variant": d.get("type", "")}
    if source == "codex":
        p = d.get("payload", {})
        if not isinstance(p, dict):
            return []
        if d.get("type") == "response_item" and p.get("type") == "message":
            role = p.get("role")
            text = blocks_text(p.get("content"))
            if role == "user":
                # 同一消息中的环境和 AGENTS 块常独立存在，逐块排除。
                content = p.get("content")
                text = "\n".join(user_text(x.get("text", "")) for x in content if isinstance(x, dict) and x.get("type") in ("input_text", "text")) if isinstance(content, list) else user_text(text)
                return [dict(base, role="user", text=text, kind="user")]
            if role == "assistant" and p.get("phase") in ("final", "final_answer"):
                return [dict(base, role="assistant", text=text)]
            if role == "assistant" and not p.get("phase") and text:
                # 早期记录没有 phase，等待 task_complete 明确标定最终回答。
                pending[hashlib.sha256(text.strip().encode()).hexdigest()] = dict(base, role="assistant", text=text)
                if len(pending) > 24:
                    pending.pop(next(iter(pending)))
        if d.get("type") == "event_msg":
            typ = p.get("type")
            if typ == "user_message":
                return [dict(base, role="user", text=user_text(p.get("message", "")), kind="user")]
            if typ == "agent_message" and p.get("phase") in ("final", "final_answer"):
                return [dict(base, role="assistant", text=p.get("message", ""))]
            if typ == "task_complete" and p.get("last_agent_message"):
                text = p["last_agent_message"]
                previous = pending.pop(hashlib.sha256(text.strip().encode()).hexdigest(), None)
                return [previous or dict(base, role="assistant", text=text)]
        return []
    m = d.get("message", {})
    if not isinstance(m, dict):
        return []
    if source == "pi":
        if d.get("type") != "message":
            return []
        if m.get("role") == "user":
            return [dict(base, role="user", text=user_text(blocks_text(m.get("content"))), kind="user")]
        if m.get("role") == "assistant" and m.get("stopReason") == "stop":
            return [dict(base, role="assistant", text=blocks_text(m.get("content")))]
        return []
    typ = d.get("type")
    text = blocks_text(m.get("content"))
    if typ == "user" and m.get("role") == "user":
        return [dict(base, role="user", text=user_text(text), kind="user")]
    if typ == "assistant" and m.get("role") == "assistant" and m.get("stop_reason") in ("end_turn", "stop_sequence"):
        return [dict(base, role="assistant", text=text)]
    return []


def scan_file(c, source, path, job, previous=None, max_line=MAX_LINE):
    initial = path.stat()
    if previous and previous["indexed_size"] == initial.st_size and previous["indexed_mtime_ns"] == initial.st_mtime_ns and previous["status"] in ("indexed", "partial"):
        c.execute("UPDATE files SET last_seen_job=? WHERE path=?", (job, str(path)))
        c.execute("UPDATE sessions SET last_seen_job=? WHERE source_path=?", (job, str(path)))
        return "unchanged"
    c.execute("DELETE FROM messages WHERE source_path=?", (str(path),))
    counts = collections.Counter()
    seen, pending = {}, {}
    session = None
    latest_time = ""
    earliest_time = ""
    first_title = ""
    for line, d, error in bounded_lines(path, max_line):
        counts["lines"] = line
        if error:
            counts["skipped_large" if error == "large" else "malformed"] += 1
            continue
        meta = file_metadata(source, path, d)
        if meta:
            if session is None:
                session = meta
                upsert_session(c, session, job)
            elif source == "claude" and not session.get("project") and meta.get("project"):
                session["project"] = meta["project"]
                upsert_session(c, session, job)
        if session is None:
            continue
        current_time = timestamp(d.get("timestamp"))
        if current_time:
            earliest_time = min(earliest_time or current_time, current_time)
            latest_time = max(latest_time, current_time)
        for msg in file_candidates(source, path, line, d, session["key"], pending):
            if msg["role"] == "user" and msg["text"].strip() and not first_title:
                first_title = msg["text"].strip().splitlines()[0][:160]
            added, duplicate = insert_message(c, msg, seen)
            counts["indexed_messages"] += added
            counts["duplicates"] += duplicate
            counts["truncated"] += int(added and len(redact(msg["text"])) > MAX_TEXT)
    # 元数据缺失的日志仍登记，不冒充内容覆盖。
    if session is None:
        session = {"key": source + ":file:" + hashlib.sha256(str(path).encode()).hexdigest()[:24], "source": source, "native_id": path.stem, "title": "日志缺少会话元数据", "source_path": str(path), "is_subagent": int("subagents" in path.parts), "storage": "jsonl"}
        upsert_session(c, session, job)
        counts["missing_metadata"] = 1
    if first_title:
        c.execute("UPDATE sessions SET title=?,title_fold=? WHERE key=? AND title=''", (redact(first_title), redact(first_title).casefold(), session["key"]))
    c.execute("UPDATE sessions SET updated=CASE WHEN updated<? THEN ? ELSE updated END,created=CASE WHEN created='' THEN ? ELSE created END WHERE key=?", (latest_time, latest_time, earliest_time, session["key"]))
    final = path.stat()
    changed = initial.st_size != final.st_size or initial.st_mtime_ns != final.st_mtime_ns
    status = "partial" if changed or counts["skipped_large"] or counts["malformed"] or counts["missing_metadata"] else "indexed"
    warnings = []
    if changed:
        warnings.append("读取期间文件发生变化，下次增量会重新读取")
    if counts["missing_metadata"]:
        warnings.append("未找到有效会话元数据，正文未纳入索引")
    if counts["skipped_large"]:
        warnings.append("超过 8 MiB 的单行已跳过")
    if counts["malformed"]:
        warnings.append("损坏或非 JSON 对象行已跳过")
    c.execute("""INSERT OR REPLACE INTO files(path,source,size,mtime_ns,indexed_size,indexed_mtime_ns,lines,indexed_messages,skipped_large,malformed,duplicates,truncated,status,error,last_seen_job,updated)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(path), source, final.st_size, final.st_mtime_ns, initial.st_size, initial.st_mtime_ns, counts["lines"], counts["indexed_messages"], counts["skipped_large"], counts["malformed"], counts["duplicates"], counts["truncated"], status, "；".join(warnings), job, now()))
    return status


def discover(root):
    if not root.is_dir():
        raise FileNotFoundError("历史目录不存在：" + str(root))
    files = []
    errors = []
    def failed(exc):
        errors.append(redact(str(exc)))
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
        dirs[:] = [x for x in dirs if not Path(directory, x).is_symlink()]
        for name in names:
            p = Path(directory, name)
            if name.endswith(".jsonl") and not p.is_symlink():
                files.append(p)
    if errors:
        raise OSError("历史目录遍历不完整：" + "; ".join(errors[:3]))
    return sorted(files)


def codex_metadata(c, path, job):
    with contextlib.closing(read_db(path)) as source:
        columns = {row[1] for row in source.execute("PRAGMA table_info(threads)")}
        wanted = ["id", "rollout_path", "cwd", "title", "created_at", "updated_at", "created_at_ms", "updated_at_ms", "source", "thread_source", "agent_path", "archived"]
        selected = [x for x in wanted if x in columns]
        if not {"id", "rollout_path"}.issubset(columns):
            raise ValueError("Codex threads 表缺少 id 或 rollout_path")
        total = 0
        for row in source.execute("SELECT " + ",".join(selected) + " FROM threads"):
            r = dict(row)
            sub, parent, agent_path = subagent_info(r.get("source"), r.get("thread_source"))
            upsert_session(c, {"key": "codex:" + r["id"], "source": "codex", "native_id": r["id"], "title": r.get("title", ""), "project": r.get("cwd", ""), "created": timestamp(r.get("created_at_ms") or r.get("created_at")), "updated": timestamp(r.get("updated_at_ms") or r.get("updated_at")), "source_path": r.get("rollout_path", ""), "is_subagent": int(sub), "parent_key": "codex:" + parent if parent else "", "agent_path": r.get("agent_path") or agent_path, "archived": r.get("archived", 0), "storage": "threads + jsonl"}, job)
            total += 1
        return total


def opencode_index(c, path, job, provider="opencode"):
    info = {"tables": {}, "duplicates": 0, "malformed": 0, "oversize_rows": 0, "indexed_messages": 0}
    with contextlib.closing(read_db(path)) as source:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"session", "session_v2"}.intersection(tables):
            raise ValueError("OpenCode 库中没有已支持的 session 表")
        # SQLite 没有行号，保留表名和主键；同原生会话 ID 合并。
        c.execute("DELETE FROM messages WHERE source_path=?", (str(path),))
        seen = {}
        for table in ("session", "session_v2"):
            if table not in tables:
                continue
            cols = {r[1] for r in source.execute("PRAGMA table_info(" + table + ")")}
            wanted = ["id", "parent_id", "directory", "title", "time_created", "time_updated", "time_archived"]
            selected = [x for x in wanted if x in cols]
            n = 0
            for row in source.execute("SELECT " + ",".join(selected) + " FROM " + table):
                r = dict(row)
                parent = r.get("parent_id") or ""
                upsert_session(c, {"key": provider + ":" + r["id"], "source": provider, "native_id": r["id"], "title": r.get("title", ""), "project": r.get("directory", ""), "created": timestamp(r.get("time_created")), "updated": timestamp(r.get("time_updated")), "is_subagent": int(bool(parent)), "parent_key": provider + ":" + parent if parent else "", "archived": int(bool(r.get("time_archived"))), "source_path": str(path), "storage": table}, job)
                n += 1
            info["tables"][table] = n

        if "session_message" in tables:
            query = "SELECT id,session_id,type,time_created,length(data) AS data_size,CASE WHEN length(data)<=? THEN data ELSE NULL END AS data FROM session_message WHERE type IN ('user','assistant') ORDER BY time_created,id"
            for row in source.execute(query, (MAX_LINE,)):
                r = dict(row)
                if r["data"] is None:
                    info["oversize_rows"] += 1
                    continue
                d = json_object(r["data"])
                if not d:
                    info["malformed"] += 1
                    continue
                role = r["type"]
                if role == "assistant" and d.get("finish") != "stop":
                    continue
                text = user_text(d.get("text", "")) if role == "user" else blocks_text(d.get("content"))
                key = provider + ":" + r["session_id"]
                if not c.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                    info["malformed"] += 1
                    continue
                added, dup = insert_message(c, {"session_key": key, "role": role, "text": text, "time": r["time_created"], "source_path": str(path), "row_locator": "session_message.id=" + r["id"], "variant": "v2", "kind": "user" if role == "user" else "final"}, seen)
                info["duplicates"] += dup
                info["indexed_messages"] += added
        if {"message", "part"}.issubset(tables):
            # 不加载 tool/reasoning 原文。过滤在 SQL 层完成，只取可见 text。
            query = """SELECT m.id,m.session_id,m.time_created,m.data AS message_data,p.id AS part_id,
               length(p.data) AS data_size,CASE WHEN length(p.data)<=? THEN p.data ELSE NULL END AS data
               FROM message m JOIN part p ON p.message_id=m.id
               WHERE json_valid(m.data) AND json_extract(m.data,'$.role') IN ('user','assistant')
               AND json_valid(p.data) AND json_extract(p.data,'$.type')='text' ORDER BY m.time_created,m.id,p.time_created,p.id"""
            grouped = {}
            for row in source.execute(query, (MAX_LINE,)):
                r = dict(row)
                if r["data"] is None:
                    info["oversize_rows"] += 1
                    continue
                m, p = json_object(r["message_data"]), json_object(r["data"])
                role = m.get("role")
                if role == "assistant" and m.get("finish") not in ("stop", "end_turn", "completed"):
                    continue
                item = grouped.setdefault(r["id"], {"session_key": provider + ":" + r["session_id"], "role": role, "parts": [], "time": r["time_created"], "source_path": str(path), "row_locator": "message.id=" + r["id"], "variant": "v1", "kind": "user" if role == "user" else "final"})
                if isinstance(p.get("text"), str):
                    item["parts"].append(p["text"])
            for item in grouped.values():
                item["text"] = "\n".join(item.pop("parts"))
                if item["role"] == "user":
                    item["text"] = user_text(item["text"])
                if not c.execute("SELECT 1 FROM sessions WHERE key=?", (item["session_key"],)).fetchone():
                    info["malformed"] += 1
                    continue
                added, dup = insert_message(c, item, seen)
                info["duplicates"] += dup
                info["indexed_messages"] += added
    stat = path.stat()
    count = c.execute("SELECT count(*) FROM messages WHERE source_path=?", (str(path),)).fetchone()[0]
    c.execute("""INSERT OR REPLACE INTO files(path,source,size,mtime_ns,indexed_size,indexed_mtime_ns,indexed_messages,duplicates,malformed,skipped_large,status,error,last_seen_job,updated)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(path), provider, stat.st_size, stat.st_mtime_ns, stat.st_size, stat.st_mtime_ns, count, info["duplicates"], info["malformed"], info["oversize_rows"], "partial" if info["malformed"] or info["oversize_rows"] else "indexed", "SQLite 出处以表名和主键定位；每次增量刷新数据库消息", job, now()))
    return info


def database_manifest(c, path, provider, job, count, skipped=0, duplicates=0, note=""):
    stat = path.stat()
    c.execute("""INSERT OR REPLACE INTO files(path,source,size,mtime_ns,indexed_size,indexed_mtime_ns,indexed_messages,skipped_large,duplicates,status,error,last_seen_job,updated)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (str(path), provider, stat.st_size, stat.st_mtime_ns, stat.st_size, stat.st_mtime_ns, count, skipped, duplicates, "partial" if skipped else "indexed", note, job, now()))


def codex_sqlite_index(c, path, job):
    """补充现代持久历史，逐内容出现次数与 JSONL 对齐，避免镜像双算。"""
    info = {"eligible_rows": 0, "supplemented": 0, "mirrors_removed": 0, "oversize_rows": 0, "metadata_fallbacks": 0}
    c.execute("DELETE FROM messages WHERE source_path=?", (str(path),))
    mirror_counts = collections.Counter({(r["session_key"], r["role"], r["content_hash"]): r["n"] for r in c.execute("SELECT session_key,role,content_hash,count(*) AS n FROM messages WHERE session_key LIKE 'codex:%' AND source_path<>? GROUP BY session_key,role,content_hash", (str(path),))})
    seen = {}
    with contextlib.closing(read_db(path)) as source:
        query = """SELECT i.thread_id,i.turn_id,i.item_id,i.rollout_ordinal,i.created_at_ms,i.item_type,
           CASE WHEN length(i.item_json)<=? THEN i.item_json ELSE NULL END AS data
           FROM thread_items i LEFT JOIN thread_turns t ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id
           WHERE i.item_type='userMessage' OR (i.item_type='agentMessage' AND
             (i.item_id=t.final_agent_item_id OR (json_valid(i.item_json) AND json_extract(i.item_json,'$.phase') IN ('final','final_answer'))))
           ORDER BY i.created_at_ms,i.item_id"""
        for row in source.execute(query, (MAX_LINE,)):
            r = dict(row)
            info["eligible_rows"] += 1
            if r["data"] is None:
                info["oversize_rows"] += 1
                continue
            d = json_object(r["data"])
            role = "user" if r["item_type"] == "userMessage" else "assistant"
            if role == "user":
                blocks = d.get("content", [])
                text = "\n".join(user_text(x.get("text", "")) for x in blocks if isinstance(x, dict) and x.get("type") in ("text", "input_text")) if isinstance(blocks, list) else user_text(blocks_text(blocks))
            else:
                text = d.get("text", "")
            if not isinstance(text, str) or not text.strip():
                continue
            key = "codex:" + r["thread_id"]
            exists = c.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone()
            if not exists:
                # 明确父子关系未知，不冒充可确认的主会话。
                upsert_session(c, {"key": key, "source": "codex", "native_id": r["thread_id"], "title": "持久历史记录（会话元数据缺失）", "created": timestamp(r["created_at_ms"]), "source_path": str(path), "is_subagent": -1, "storage": "thread_items，父子关系未知"}, job)
                info["metadata_fallbacks"] += 1
            else:
                c.execute("UPDATE sessions SET last_seen_job=?,is_subagent=CASE WHEN storage='thread_items，父子关系未知' THEN -1 ELSE is_subagent END WHERE key=?", (job, key))
            digest = hashlib.sha256(text.strip().encode()).hexdigest()
            mirror_key = (key, role, digest)
            if mirror_counts[mirror_key] > 0:
                mirror_counts[mirror_key] -= 1
                info["mirrors_removed"] += 1
                continue
            locator = "thread_items(thread_id=" + r["thread_id"] + ",item_id=" + r["item_id"] + ")"
            added, dup = insert_message(c, {"session_key": key, "role": role, "text": text, "time": r["created_at_ms"], "source_path": str(path), "row_locator": locator, "variant": "sqlite", "kind": "user" if role == "user" else "final"}, seen)
            info["supplemented"] += added
            info["mirrors_removed"] += dup
    database_manifest(c, path, "codex", job, info["supplemented"], info["oversize_rows"], info["mirrors_removed"], "只补充 JSONL 未覆盖的用户需求与最终回答；出处为 thread_items 复合主键")
    return info


def hermes_index(c, path, job):
    info = {"metadata_rows": 0, "indexed_messages": 0, "oversize_rows": 0}
    c.execute("DELETE FROM messages WHERE source_path=?", (str(path),))
    seen = {}
    with contextlib.closing(read_db(path)) as source:
        # 禁止 SELECT *，不读取 system_prompt/model_config/reasoning/API 内容。
        for r in source.execute("SELECT id,title,cwd,parent_session_id,started_at,ended_at,archived FROM sessions"):
            parent = r["parent_session_id"] or ""
            upsert_session(c, {"key": "hermes:" + r["id"], "source": "hermes", "native_id": r["id"], "title": r["title"], "project": r["cwd"], "created": timestamp(r["started_at"]), "updated": timestamp(r["ended_at"] or r["started_at"]), "is_subagent": int(bool(parent)), "parent_key": "hermes:" + parent if parent else "", "archived": r["archived"], "source_path": str(path), "storage": "sessions/messages"}, job)
            info["metadata_rows"] += 1
        query = """SELECT id,session_id,role,timestamp,finish_reason,
          CASE WHEN length(content)<=? THEN content ELSE NULL END AS content
          FROM messages WHERE role='user' OR (role='assistant' AND finish_reason='stop' AND (tool_calls IS NULL OR tool_calls IN ('','[]','null'))) ORDER BY timestamp,id"""
        for r in source.execute(query, (MAX_LINE,)):
            if r["content"] is None:
                info["oversize_rows"] += 1
                continue
            text = r["content"]
            if isinstance(text, str) and text[:1] == "[":
                try:
                    text = blocks_text(json.loads(text))
                except ValueError:
                    pass
            if r["role"] == "user":
                text = user_text(text)
            key = "hermes:" + r["session_id"]
            if not c.execute("SELECT 1 FROM sessions WHERE key=?", (key,)).fetchone():
                continue
            added, _ = insert_message(c, {"session_key": key, "role": r["role"], "text": text, "time": r["timestamp"], "source_path": str(path), "row_locator": "messages.id=" + str(r["id"]), "variant": "hermes", "kind": "user" if r["role"] == "user" else "final"}, seen)
            info["indexed_messages"] += added
    database_manifest(c, path, "hermes", job, info["indexed_messages"], info["oversize_rows"], note="显式读取用户需求及 finish_reason=stop 且无工具调用的回答")
    return info


def zcode_tasks_index(c, path, job):
    count = 0
    with contextlib.closing(read_db(path)) as source:
        for r in source.execute("SELECT workspace_path,task_id,title,task_status,provider,created_at,updated_at,archived,deleted FROM tasks"):
            # 任务目录可能对应已索引的原生会话，不并入会话总数，也不导入 searchable_text/meta_json。
            key = "zcode_tasks:" + hashlib.sha256((str(r["workspace_path"]) + str(r["task_id"])).encode()).hexdigest()[:24]
            upsert_session(c, {"key": key, "source": "zcode_tasks", "native_id": str(r["task_id"]), "title": r["title"], "project": r["workspace_path"], "created": timestamp(r["created_at"]), "updated": timestamp(r["updated_at"]), "archived": int(bool(r["archived"] or r["deleted"])), "source_path": str(path), "storage": "tasks 目录元数据；正文未导入；状态=" + str(r["task_status"]) + "；provider=" + str(r["provider"])}, job)
            count += 1
    database_manifest(c, path, "zcode_tasks", job, 0, note="仅导入任务目录元数据；正文未导入，可能与原生会话重叠，不并入会话总数")
    return {"metadata_tasks": count, "indexed_messages": 0, "coverage": "metadata_only"}


def write_job(data, data_dir=DATA):
    _shared.write_json_atomic(Path(data_dir) / "job.json", data)


def stored_job(data_dir=DATA):
    try:
        return json.loads((Path(data_dir) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle", "message": "尚未建立索引"}


def acquire_file_lock(data_dir=DATA):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    f = (Path(data_dir) / "index.lock").open("a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("已有索引任务正在运行")
    return f


def run_index(mode="incremental", sources=None, db_path=DB, progress=None, max_line=MAX_LINE, held_lock=None):
    if mode not in ("incremental", "rebuild"):
        raise ValueError("mode 只支持 incremental 或 rebuild")
    sources = sources or Sources()
    data_dir = Path(db_path).parent
    lock = held_lock or acquire_file_lock(data_dir)
    job = {"id": uuid.uuid4().hex, "state": "running", "mode": mode, "started": now(), "pid": os.getpid(), "done": 0, "total": 0, "current": "读取会话元数据", "errors": [], "sources": {}, "unchanged_files": 0, "deleted_files": 0}
    target = data_dir / (".rebuild-" + job["id"] + ".sqlite") if mode == "rebuild" else Path(db_path)
    c = None
    last_emit = 0
    def emit(force=False):
        nonlocal last_emit
        if force or time.monotonic() - last_emit > .4:
            write_job(job, data_dir)
            if progress:
                progress(dict(job))
            last_emit = time.monotonic()
    def fail(source, stage, exc):
        job["errors"].append({"source": source, "stage": stage, "error": redact(str(exc))[:800]})
    try:
        emit(True)
        c = connect(target)
        c.execute("PRAGMA journal_mode=WAL")
        codex_ok = True
        try:
            job["sources"]["codex"] = {"metadata_rows": codex_metadata(c, sources.codex_db, job["id"])}
            c.commit()
        except (OSError, sqlite3.Error, ValueError) as exc:
            c.rollback()
            codex_ok = False
            fail("codex", "threads 元数据", exc)
        files = []
        root_ok = {"codex": True, "claude": True}
        roots = [("codex", x) for x in sources.codex_roots] + [("claude", sources.claude_root)]
        if sources.pi_root is not None:
            roots.append(("pi", sources.pi_root))
            root_ok["pi"] = True
        for provider, root in roots:
            try:
                found = discover(root)
                files.extend((provider, x) for x in found)
                item = job["sources"].setdefault(provider, {})
                item["discovered_files"] = item.get("discovered_files", 0) + len(found)
                item.setdefault("roots", []).append(str(root))
            except OSError as exc:
                root_ok[provider] = False
                fail(provider, "发现日志", exc)
        extra_dbs = []
        if sources.codex_history_db is not None:
            extra_dbs.append(("codex", sources.codex_history_db, codex_sqlite_index))
        if sources.hermes_db is not None:
            extra_dbs.append(("hermes", sources.hermes_db, hermes_index))
        if sources.zcode_db is not None:
            extra_dbs.append(("zcode", sources.zcode_db, lambda conn, path, jid: opencode_index(conn, path, jid, "zcode")))
        if sources.zcode_tasks_db is not None:
            extra_dbs.append(("zcode_tasks", sources.zcode_tasks_db, zcode_tasks_index))
        job["total"] = len(files) + 1 + len(extra_dbs)
        emit(True)
        for provider, path in files:
            job["current"] = str(path)
            try:
                previous = c.execute("SELECT * FROM files WHERE path=?", (str(path),)).fetchone()
                state = scan_file(c, provider, path, job["id"], previous, max_line)
                if state == "unchanged":
                    job["unchanged_files"] += 1
                c.commit()
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                c.rollback()
                fail(provider, str(path), exc)
                c.execute("INSERT OR REPLACE INTO files(path,source,status,error,last_seen_job,updated) VALUES(?,?,'error',?,?,?)", (str(path), provider, redact(str(exc))[:800], job["id"], now()))
                c.commit()
            job["done"] += 1
            emit()
        job["current"] = str(sources.opencode_db)
        opencode_ok = True
        try:
            job["sources"]["opencode"] = opencode_index(c, sources.opencode_db, job["id"])
            c.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            c.rollback()
            opencode_ok = False
            fail("opencode", "数据库消息", exc)
        job["done"] += 1
        database_ok = {}
        for provider, path, runner in extra_dbs:
            job["current"] = str(path)
            emit(True)
            try:
                result = runner(c, path, job["id"])
                if provider == "codex":
                    job["sources"].setdefault(provider, {})["sqlite_supplement"] = result
                else:
                    job["sources"][provider] = result
                database_ok[provider] = True
                c.commit()
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                c.rollback()
                database_ok[provider] = False
                fail(provider, "数据库适配 " + str(path), exc)
                c.execute("INSERT OR REPLACE INTO files(path,source,status,error,last_seen_job,updated) VALUES(?,?,'error',?,?,?)", (str(path), provider, redact(str(exc))[:800], job["id"], now()))
                c.commit()
            job["done"] += 1
        # 只在成功遍历该来源时清除已删除的文件，不因权限故障误删既有索引。
        for provider in root_ok:
            if not root_ok[provider]:
                continue
            gone = c.execute("SELECT path FROM files WHERE source=? AND last_seen_job<>?", (provider, job["id"])).fetchall()
            for row in gone:
                c.execute("DELETE FROM messages WHERE source_path=?", (row[0],))
                c.execute("DELETE FROM files WHERE path=?", (row[0],))
            job["deleted_files"] += len(gone)
        for provider, good in (("codex", codex_ok and root_ok["codex"]), ("claude", root_ok["claude"]), ("opencode", opencode_ok)):
            if good:
                c.execute("DELETE FROM sessions WHERE source=? AND last_seen_job<>?", (provider, job["id"]))
        for provider in ("pi", "hermes", "zcode", "zcode_tasks"):
            good = root_ok.get(provider, database_ok.get(provider, False))
            if good:
                c.execute("DELETE FROM sessions WHERE source=? AND last_seen_job<>?", (provider, job["id"]))
        # 保留数据库里能看到、但原日志缺失的 Codex 会话。消息覆盖不冒充元数据覆盖。
        for row in c.execute("SELECT key,source_path FROM sessions WHERE source='codex' AND source_path<>''").fetchall():
            if not c.execute("SELECT 1 FROM files WHERE path=?", (row["source_path"],)).fetchone():
                c.execute("DELETE FROM messages WHERE source_path=?", (row["source_path"],))
                c.execute("INSERT OR IGNORE INTO files(path,source,status,error,last_seen_job,updated) VALUES(?,'codex','missing','会话元数据存在，原日志未发现',?,?)", (row["source_path"], job["id"], now()))
        partial = c.execute("SELECT count(*) FROM files WHERE status<>'indexed'").fetchone()[0]
        job["state"] = "completed_with_limits" if partial or job["errors"] else "completed"
        job["finished"] = now()
        job["current"] = "索引已更新"
        job["limits"] = partial
        job["counts"] = {"sessions": c.execute("SELECT count(*) FROM sessions WHERE source<>'zcode_tasks'").fetchone()[0], "metadata_tasks": c.execute("SELECT count(*) FROM sessions WHERE source='zcode_tasks'").fetchone()[0], "messages": c.execute("SELECT count(*) FROM messages").fetchone()[0], "files": c.execute("SELECT count(*) FROM files").fetchone()[0]}
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_index',?)", (job["finished"],))
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_job',?)", (job["id"],))
        c.commit()
        for _ in range(6):
            row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0] == 0:
                break
            # busy=1 表示仍有读者（如 /status 轮询）持有页；等待后重试，结果不能丢弃。
            time.sleep(0.3)
        c.close()
        c = None
        if mode == "rebuild":
            os.replace(target, db_path)
            # 旧库的 WAL/SHM 伴生文件属于旧 inode；不清掉会被应用到替换后的新库，造成静默损坏。
            for suffix in ("-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(db_path) + suffix).unlink()
        emit(True)
        return job
    except Exception as exc:
        job["state"] = "failed"
        job["finished"] = now()
        fail("index", "建立索引", exc)
        emit(True)
        raise
    finally:
        if c:
            c.close()
        lock.close()
        if target != Path(db_path):
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(target) + suffix).unlink()


def first(query, key, default=""):
    value = query.get(key, [default])
    return str(value[0]) if isinstance(value, list) and value else str(value or default)


def filters(query, alias="s", message=False):
    where, args = [], []
    source = first(query, "source")
    if source:
        if source not in ("codex", "claude", "opencode", "pi", "hermes", "zcode", "zcode_tasks"):
            raise ValueError("未知记录来源")
        where.append(alias + ".source=?")
        args.append(source)
    project = first(query, "project")
    if project:
        where.append(alias + ".project=?")
        args.append(project)
    main = first(query, "main", "1")
    if main == "1":
        where.append(alias + ".is_subagent=0")
    elif main == "sub":
        where.append(alias + ".is_subagent=1")
    elif main not in ("0", "all", ""):
        raise ValueError("main 只支持 1、sub 或 all")
    for field, op in (("from", ">="), ("to", "<")):
        value = first(query, field)
        if value:
            try:
                date = dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError("日期须使用 YYYY-MM-DD")
            if field == "to":
                date += dt.timedelta(days=1)
            # 用户日期以本机中国时区日界查询，SQLite 里统一储存 UTC。
            date_time = dt.datetime.combine(date, dt.time(), dt.timezone(dt.timedelta(hours=8))).astimezone(dt.timezone.utc).isoformat(timespec="milliseconds")
            where.append(("m.time" if message else alias + ".updated") + op + "?")
            args.append(date_time)
    return where, args


def page_size(query, default=30, max_limit=100):
    try:
        return max(1, min(100000, int(first(query, "page", "1")))), max(1, min(max_limit, int(first(query, "limit", str(default)))))
    except ValueError:
        raise ValueError("page 和 limit 须为整数")


def snippet(text, terms, length=300):
    folded = text.casefold()
    positions = [folded.find(t) for t in terms if folded.find(t) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - 90)
    return ("…" if start else "") + text[start:start + length] + ("…" if start + length < len(text) else "")


def search(query, db_path=DB):
    q = first(query, "q").strip()
    if len(q) > 300:
        raise ValueError("检索词最多 300 个字符")
    if len(q.split()) > 8:
        raise ValueError("最多同时查询 8 个关键词")
    terms = [x.casefold() for x in q.split()]
    page, limit = page_size(query)
    where, args = filters(query, message=True)
    role = first(query, "role")
    if role:
        if role not in ("user", "assistant"):
            raise ValueError("未知消息角色")
        where.append("m.role=?")
        args.append(role)
    for term in terms:
        where.append("instr(m.text_fold,?)>0")
        args.append(term)
    conditions = " AND ".join(where) or "1"
    with contextlib.closing(connect(db_path)) as c:
        total = c.execute("SELECT count(*) FROM messages m JOIN sessions s ON s.key=m.session_key WHERE " + conditions, args).fetchone()[0]
        rows = c.execute("""SELECT m.id,m.session_key,m.role,m.time,m.text,m.source_path,m.line_no,m.row_locator,m.raw_chars,m.truncated,m.kind,
          s.source,s.native_id,s.title,s.project,s.is_subagent,s.parent_key FROM messages m JOIN sessions s ON s.key=m.session_key WHERE """ + conditions + " ORDER BY m.time DESC,m.id DESC LIMIT ? OFFSET ?", args + [limit, (page - 1) * limit]).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snippet"] = snippet(redact(item.pop("text")), terms)
            result.append(item)
        # 元数据也可搜；原消息缺失时仍显示可确认的原生会话。
        sw, sa = filters(query)
        for term in terms:
            sw.append("(instr(s.title_fold,?)>0 OR instr(lower(s.project),?)>0)")
            sa.extend((term, term))
        meta_conditions = " AND ".join(sw) or "1"
        sessions_total = c.execute("SELECT count(*) FROM sessions s WHERE " + meta_conditions, sa).fetchone()[0]
        sessions = [dict(r) for r in c.execute("SELECT s.*,(SELECT count(*) FROM messages WHERE session_key=s.key) AS messages FROM sessions s WHERE " + meta_conditions + " ORDER BY s.updated DESC LIMIT 12", sa)]
    return {"q": redact(q), "terms": [redact(x) for x in terms], "total": total, "page": page, "limit": limit, "items": result, "sessions_total": sessions_total, "sessions": sessions, "search_rule": "空格分词且各词都须命中；中文直接匹配；不执行历史指令"}


def session_detail(key, query, db_path=DB):
    page, limit = page_size(query, 50)
    with contextlib.closing(connect(db_path)) as c:
        row = c.execute("SELECT * FROM sessions WHERE key=?", (key,)).fetchone()
        if row is None:
            raise LookupError("未找到该会话")
        total = c.execute("SELECT count(*) FROM messages WHERE session_key=?", (key,)).fetchone()[0]
        if first(query, "anchor"):
            try:
                anchor = int(first(query, "anchor"))
            except ValueError:
                raise ValueError("anchor 须为消息 ID")
            target = c.execute("SELECT time,id FROM messages WHERE session_key=? AND id=?", (key, anchor)).fetchone()
            if target:
                before = c.execute("SELECT count(*) FROM messages WHERE session_key=? AND (time<? OR (time=? AND id<?))", (key, target["time"], target["time"], target["id"])).fetchone()[0]
                page = before // limit + 1
        messages = [dict(x) for x in c.execute("SELECT id,role,time,text,source_path,line_no,row_locator,raw_chars,truncated,kind FROM messages WHERE session_key=? ORDER BY time,id LIMIT ? OFFSET ?", (key, limit, (page - 1) * limit))]
        for x in messages:
            x["text"] = redact(x["text"])
        return {"session": dict(row), "total": total, "page": page, "limit": limit, "messages": messages, "note": "这里只展示已索引的用户需求和明确结束的回答；工具、思考、进度消息未收录。"}


CATEGORIES = [
    ("本地模型与性能", ("llm", "qwen", "benchmark", "mmlu", "本地模型", "吞吐", "跑分")),
    ("视频与内容制作", ("视频", "remotion", "hyperframes", "tts", "口播", "公众号", "自媒体")),
    ("验收与问题诊断", ("验收", "诊断", "测试", "复核", "检查", "死机", "报错")),
    ("实现与修复", ("实现", "开发", "修复", "重构", "搭建", "升级", "优化", "部署")),
    ("研究与方案判断", ("调研", "研究", "论文", "对比", "分析", "方案", "评估")),
    ("技能与协作", ("skill", "技能", "agent", "并行", "任务书", "工作流")),
]


def stats(query=None, db_path=DB):
    query = query or {}
    where, args = filters(query)
    condition = " AND ".join(where) or "1"
    demand_where, demand_args = filters(query, message=True)
    demand_condition = " AND ".join(demand_where) or "1"
    with contextlib.closing(connect(db_path)) as c:
        sources = [dict(r) for r in c.execute("SELECT s.source,count(*) AS sessions,sum(CASE WHEN s.is_subagent=1 THEN 1 ELSE 0 END) AS subagents,sum(CASE WHEN s.is_subagent=-1 THEN 1 ELSE 0 END) AS unknown_relations,(SELECT count(*) FROM messages m JOIN sessions t ON t.key=m.session_key WHERE t.source=s.source) AS messages FROM sessions s GROUP BY s.source ORDER BY s.source")]
        for item in sources:
            item["metadata_only"] = item["source"] == "zcode_tasks"
        projects = [dict(r) for r in c.execute("SELECT s.project,count(*) AS sessions,max(s.updated) AS updated FROM sessions s WHERE " + condition + " GROUP BY s.project ORDER BY updated DESC LIMIT 20", args)]
        categories = collections.Counter()
        user_count = 0
        for row in c.execute("SELECT m.text_fold FROM messages m JOIN sessions s ON s.key=m.session_key WHERE m.role='user' AND " + demand_condition, demand_args):
            user_count += 1
            matches = 0
            for title, keywords in CATEGORIES:
                if any(word in row[0] for word in keywords):
                    categories[title] += 1
                    matches += 1
            if not matches:
                categories["其他需求"] += 1
        all_projects = [r[0] for r in c.execute("SELECT DISTINCT project FROM sessions WHERE project<>'' ORDER BY project")]
        roles = {r[0]: r[1] for r in c.execute("SELECT role,count(*) FROM messages GROUP BY role")}
    return {"sources": sources, "recent_projects": projects, "projects": all_projects, "demands": [{"name": title, "count": count} for title, count in categories.most_common()], "user_messages_in_scope": user_count, "roles": roles, "classification_rule": "按已索引用户消息中的关键词计数，一条消息可同时归入多类；不是独立需求数，也不代表全部历史。", "category_keywords": [{"name": title, "keywords": list(words)} for title, words in CATEGORIES]}


def manifest(query=None, db_path=DB):
    query = query or {}
    page, limit = page_size(query, 50)
    conditions, args = [], []
    if first(query, "source"):
        conditions.append("source=?")
        args.append(first(query, "source"))
    if first(query, "limited") == "1":
        conditions.append("status<>'indexed'")
    where = " AND ".join(conditions) or "1"
    with contextlib.closing(connect(db_path)) as c:
        total = c.execute("SELECT count(*) FROM files WHERE " + where, args).fetchone()[0]
        items = [dict(r) for r in c.execute("SELECT * FROM files WHERE " + where + " ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'missing' THEN 1 WHEN 'partial' THEN 2 ELSE 3 END,path LIMIT ? OFFSET ?", args + [limit, (page - 1) * limit])]
        totals = dict(c.execute("SELECT status,count(*) FROM files GROUP BY status"))
        aggregate = dict(c.execute("SELECT coalesce(sum(lines),0) AS scanned_lines,coalesce(sum(skipped_large),0) AS skipped_large_lines,coalesce(sum(malformed),0) AS malformed_lines,coalesce(sum(duplicates),0) AS duplicates_removed,coalesce(sum(truncated),0) AS truncated_messages FROM files").fetchone())
        aggregate["truncated_messages"] = c.execute("SELECT coalesce(sum(truncated),0) FROM messages").fetchone()[0]
    return {"total": total, "page": page, "limit": limit, "items": items, "statuses": totals, "coverage": aggregate, "limits": ["JSONL 按行流式读取，单行最多 8 MiB；超过上限的行跳过并计数。", "每条可检索正文最多 65,536 字符，保留开头和末尾，省略部分不参与搜索。", "Codex 仅明确 final/final_answer、task_complete 或持久历史 final_agent_item_id 的回答；Claude 仅 end_turn/stop_sequence；OpenCode/ZCode 仅 stop/end_turn/completed；Pi 仅 stop；Hermes 仅 stop 且无工具调用。", "ZCode V2 只导入任务目录元数据，未导入正文；可能与原生会话重叠，单独计数。Gemini Antigravity 记录未导入。", "只读访问指定会话目录和本机数据库，不读取配置、凭据、环境文件；未知结构、损坏行和缺失日志会显示在清单中。", "本页的大小、行数和计数是最近一次读取的记录；运行中的会话可能继续追加。", "SQLite 记录以数据库路径、表名和主键定位，没有伪造的文件行号。"]}


def status(db_path=DB):
    job = stored_job(Path(db_path).parent)
    if job.get("state") == "running":
        try:
            f = acquire_file_lock(Path(db_path).parent)
        except RuntimeError:
            pass
        else:
            f.close()
            job = dict(job, state="interrupted", current="索引任务已中断，可以重新增量建立")
    with contextlib.closing(connect(db_path)) as c:
        counts = {"sessions": c.execute("SELECT count(*) FROM sessions WHERE source<>'zcode_tasks'").fetchone()[0], "metadata_tasks": c.execute("SELECT count(*) FROM sessions WHERE source='zcode_tasks'").fetchone()[0], "messages": c.execute("SELECT count(*) FROM messages").fetchone()[0], "files": c.execute("SELECT count(*) FROM files").fetchone()[0]}
        last = c.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        counts["subagents"] = c.execute("SELECT count(*) FROM sessions WHERE is_subagent=1").fetchone()[0]
        counts["unknown_relations"] = c.execute("SELECT count(*) FROM sessions WHERE is_subagent=-1").fetchone()[0]
        counts["main_sessions"] = c.execute("SELECT count(*) FROM sessions WHERE is_subagent=0 AND source<>'zcode_tasks'").fetchone()[0]
        counts["limits"] = c.execute("SELECT count(*) FROM files WHERE status<>'indexed'").fetchone()[0]
        counts["sources"] = c.execute("SELECT count(DISTINCT source) FROM sessions").fetchone()[0]
    age = None
    if last:
        stamp = seconds(last[0])
        age = round(time.time() - stamp, 1) if stamp else None
    return {"job": job, "counts": counts, "last_index": last[0] if last else "", "last_index_age_seconds": age, "index_path": str(db_path), "privacy": "本机同源读取与检索；不上传，不执行历史文本。"}


def start_index(mode):
    if mode not in ("incremental", "rebuild"):
        raise ValueError("mode 只支持 incremental 或 rebuild")
    if not LOCK.acquire(blocking=False):
        raise RuntimeError("已有索引任务正在运行")
    try:
        held = acquire_file_lock()
    except Exception:
        LOCK.release()
        raise
    def worker():
        try:
            run_index(mode, held_lock=held)
        except Exception:
            pass  # run_index 已写入可见失败记录。
        finally:
            LOCK.release()
    threading.Thread(target=worker, name="history-index", daemon=True).start()
    return {"accepted": True, "mode": mode, "message": "索引任务已启动，请通过 /status 读取进度"}


def api(method, path, query, body):
    try:
        if method == "GET" and path in ("/", "/status"):
            return 200, status()
        if method == "GET" and path == "/search":
            return 200, search(query)
        if method == "GET" and path == "/stats":
            return 200, stats(query)
        if method == "GET" and path == "/manifest":
            return 200, manifest(query)
        if method == "GET" and path.startswith("/sessions/"):
            # 父服务器已对 path 做过一次 unquote；模块内不再二次解码，避免含 % 的会话 key 被损坏。
            return 200, session_detail(path[len("/sessions/"):], query)
        if method == "POST" and path == "/index":
            return 202, start_index(body.get("mode", "incremental"))
        return _shared.not_allowed_or_missing(path, {"/", "/status", "/search", "/stats", "/manifest", "/sessions/", "/index"})
    except sqlite3.Error as exc:
        return 500, {"error": "本地索引读取失败：" + redact(str(exc))}
    except Exception as exc:
        return _shared.api_error(exc, scrub=redact)


def main():
    parser = argparse.ArgumentParser(description="只读历史检索：本机 Codex、Claude、OpenCode")
    sub = parser.add_subparsers(dest="command", required=True)
    idx = sub.add_parser("index", help="显式建立或增量更新索引")
    idx.add_argument("--mode", choices=("incremental", "rebuild"), default="incremental")
    s = sub.add_parser("search", help="查询已建立的索引")
    s.add_argument("q")
    s.add_argument("--source", choices=("codex", "claude", "opencode", "pi", "hermes", "zcode", "zcode_tasks"))
    s.add_argument("--project")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--include-subagents", action="store_true")
    s.add_argument("--limit", type=int, default=10)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "index":
        def progress(job):
            print(json.dumps({k: job.get(k) for k in ("state", "done", "total", "current", "counts")}, ensure_ascii=False), flush=True)
        output = run_index(args.mode, progress=progress)
    elif args.command == "search":
        output = search({"q": [args.q], "source": [args.source or ""], "project": [args.project or ""], "from": [args.date_from or ""], "to": [args.date_to or ""], "main": ["all" if args.include_subagents else "1"], "limit": [str(args.limit)]})
    else:
        output = status()
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
