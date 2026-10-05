#!/usr/bin/env python3
"""本地工作工具箱：单一入口、模块路由和可核对归属的服务生命周期。"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib
import json
import mimetypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import traceback
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
if __package__ in (None, "") and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps import _shared

DATA = ROOT / "data"
STATE = DATA / "server.json"
APPS = {"history", "runtime", "delivery"}
VERSION = "1.0.0"
MAX_BODY = 2 * 1024 * 1024
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def invalid_constant(value):
    raise ValueError("JSON 不允许非有限数值。")


def validate_json_shape(value):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("JSON 结构超出预算。")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)


def write_json(path, value):
    _shared.write_json_atomic(path, value)


def saved_state():
    try:
        value = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("root") == str(ROOT) and isinstance(value.get("port"), int) and 1024 <= value["port"] <= 65535:
            return value
    except (OSError, ValueError, TypeError):
        pass
    return None


def live_state(state=None):
    state = state or saved_state()
    if not state:
        return None
    try:
        with OPENER.open(f"http://127.0.0.1:{state['port']}/api/system/health", timeout=2) as r:
            current = json.load(r)
        if isinstance(current, dict) and all(current.get(k) == state.get(k) for k in ("root", "instance", "pid", "port")) and current.get("app") == "local-workbench":
            return current
    except (OSError, URLError, ValueError):
        pass
    return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalWorkbench/1.0"
    # 半开连接或谎报 Content-Length 的请求最多挂起 30 秒，随后由基类关闭连接。
    timeout = 30

    def log_message(self, fmt, *args):
        # 查询文本、目录和原始请求不进入服务日志。
        if len(args) > 1 and str(args[1]).isdigit():
            print(f"HTTP {args[1]}", flush=True)

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(data)

    def json(self, status, value):
        try:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            status, payload = 500, b'{"error":"invalid_module_response"}'
        self.send_bytes(status, payload, "application/json; charset=utf-8")

    def allowed(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.json(403, {"error": "仅允许本机入口访问。"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.json(403, {"error": "不允许跨站请求。"})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.json(403, {"error": "不允许跨站请求。"})
            return False
        if self.command == "POST" and self.headers.get("X-Workbench-Request") != "1":
            self.json(403, {"error": "操作请求缺少本机工作台标记。"})
            return False
        return True

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.json(405, {"error": "不支持跨站预检。"})

    def dispatch(self):
        if not self.allowed():
            return
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if "\x00" in path:
            self.json(400, {"error": "路径格式无效。"})
            return
        body = {}
        if self.command == "POST":
            if self.headers.get("Transfer-Encoding"):
                self.json(400, {"error": "不支持分块请求。"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > MAX_BODY:
                    self.json(413, {"error": "请求过大。"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.json(415, {"error": "请使用 JSON 请求。"})
                    return
                raw = self.rfile.read(length)
                if len(raw) != length:
                    self.json(400, {"error": "请求体不完整或连接中断。"})
                    return
                body = json.loads(raw, parse_constant=invalid_constant)
                validate_json_shape(body)
                if not isinstance(body, dict):
                    raise ValueError("object required")
            except (ValueError, UnicodeError, RecursionError):
                self.json(400, {"error": "JSON 格式不正确，须为对象。"})
                return
        if path == "/api/system/health" and self.command in {"GET", "HEAD"}:
            self.json(200, self.server.identity)
            return
        if path == "/favicon.ico" and self.command in {"GET", "HEAD"}:
            self.send_bytes(204, b"", "image/x-icon")
            return
        if path == "/api/system/research" and self.command in {"GET", "HEAD"}:
            p = ROOT / "evidence" / "research-summary.json"
            try:
                self.json(200, json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                self.json(503, {"error": "检索记录尚未生成。"})
            return
        if path.startswith("/api/"):
            parts = path.split("/")
            if len(parts) < 3 or parts[2] not in APPS:
                self.json(404, {"error": "接口不存在。"})
                return
            name = parts[2]
            try:
                module = importlib.import_module(f"apps.{name}.service")
                status, value = module.api("GET" if self.command == "HEAD" else self.command, "/" + "/".join(parts[3:]), parse_qs(parsed.query), body)
                self.json(status, value)
            except ImportError:
                self.json(503, {"error": "模块未就绪。请查看模块安装说明。"})
            except Exception:
                # 堆栈只进本机日志，帮助定位；查询路径与请求体仍不入日志。
                print(f"module={name} error=", end="", flush=True)
                traceback.print_exc(file=sys.stderr)
                self.json(500, {"error": "操作未完成，请重试或查看该模块的命令行检查结果。"})
            return
        if self.command == "POST":
            self.json(405, {"error": "此路径不接受操作请求。"})
            return
        if path == "/":
            target = ROOT / "web" / "index.html"
        elif path == "/favicon.svg":
            target = ROOT / "web" / "favicon.svg"
        elif path.startswith("/apps/"):
            parts = path.split("/")
            if len(parts) < 3 or parts[2] not in APPS:
                self.json(404, {"error": "应用不存在。"})
                return
            target = ROOT / path.lstrip("/")
            if path.endswith("/") or len(parts) == 3:
                target = target / "index.html"
            if target.suffix not in {".html", ".css", ".js", ".svg", ".png", ".ico"}:
                self.json(404, {"error": "文件不存在。"})
                return
            scope = ROOT / "apps" / parts[2]
            if not target.resolve().is_relative_to(scope.resolve()):
                self.json(403, {"error": "路径不可访问。"})
                return
        else:
            self.json(404, {"error": "页面不存在。"})
            return
        try:
            data = target.read_bytes()
        except OSError:
            self.json(404, {"error": "页面文件尚未就绪。"})
            return
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_bytes(200, data, mime + ("; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else ""))


def auto_incremental_index():
    # 启动即补一次增量，让「搜不到最新会话」的窗口缩短；失败只记日志，不影响服务。
    time.sleep(2)
    try:
        importlib.import_module("apps.history.service").start_index("incremental")
    except Exception as exc:
        print(f"auto-index skipped: {type(exc).__name__}", flush=True)


def serve(port):
    DATA.mkdir(exist_ok=True)
    os.chmod(DATA, 0o700)
    existing = live_state()
    if existing and existing.get("pid") != os.getpid():
        print(f"端口 {existing['port']} 已有工具箱实例在运行；本次启动退出，未覆盖状态。", flush=True)
        return
    server = Server(("127.0.0.1", port), Handler)
    server.identity = {"app": "local-workbench", "version": VERSION, "root": str(ROOT), "pid": os.getpid(), "port": server.server_port, "instance": str(uuid.uuid4()), "started_at": time.time()}
    write_json(STATE, server.identity)
    print(f"工作工具箱 http://127.0.0.1:{server.server_port}/", flush=True)
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=auto_incremental_index, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        current = saved_state()
        if current and current.get("instance") == server.identity["instance"]:
            STATE.unlink(missing_ok=True)


def launch(port, log):
    proc = subprocess.Popen([sys.executable, str(ROOT / "workbench.py"), "serve", "--port", str(port)], cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(80):
        state = live_state()
        if state:
            return state
        if proc.poll() is not None:
            return None
        time.sleep(0.1)
    return None


def start(port, open_browser=False):
    DATA.mkdir(exist_ok=True)
    os.chmod(DATA, 0o700)
    with (DATA / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = live_state()
        if not state:
            with (DATA / "server.log").open("ab") as log:
                state = launch(port, log)
                if state is None and port != 0:
                    print(f"端口 {port} 启动失败，自动改用空闲端口重试。", flush=True)
                    state = launch(0, log)
            if not state:
                raise RuntimeError("服务未在等待时间内就绪，请查看 data/server.log。")
        url = f"http://127.0.0.1:{state['port']}/"
        print(url)
        if open_browser:
            import webbrowser
            webbrowser.open(url)


def stop():
    DATA.mkdir(exist_ok=True)
    os.chmod(DATA, 0o700)
    # 与 start() 用同一把生命周期锁，避免「刚启动即被停」的竞态。
    with (DATA / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = live_state()
        if not state:
            print("本工具箱未运行；未操作其他服务。")
            return
        command = subprocess.run(["ps", "-p", str(state["pid"]), "-o", "args="], capture_output=True, text=True, timeout=3).stdout
        if str(ROOT / "workbench.py") not in command or " serve " not in command:
            raise RuntimeError("进程归属核对未通过，未发送停止信号。")
        os.kill(state["pid"], signal.SIGTERM)
        for _ in range(50):
            # 健康接口失联不等于进程退出，独立确认原服务进程已经消失。
            check = subprocess.run(["ps", "-p", str(state["pid"]), "-o", "args="], capture_output=True, text=True, timeout=3)
            running = check.returncode == 0 and str(ROOT / "workbench.py") in check.stdout and " serve " in check.stdout
            gone = (check.returncode == 1 and not check.stdout.strip()) or (check.returncode == 0 and check.stdout.strip() and not running)
            if gone:
                print("本工具箱已停止。")
                return
            time.sleep(0.1)
        raise RuntimeError("已请求停止，但尚未确认退出。")


def main():
    parser = argparse.ArgumentParser(description="本地工作工具箱")
    parser.add_argument("action", choices=["start", "serve", "status", "stop", "open"], nargs="?", default="start")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if args.port != 0 and not 1024 <= args.port <= 65535:
        parser.error("端口须为 1024–65535；0 表示自动分配。")
    try:
        if args.action == "serve":
            serve(args.port)
        elif args.action in {"start", "open"}:
            start(args.port, args.open or args.action == "open")
        elif args.action == "stop":
            stop()
        else:
            state = live_state()
            print(json.dumps(state or {"running": False}, ensure_ascii=False, indent=2))
            return 0 if state else 1
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
