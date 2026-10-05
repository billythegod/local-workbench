"""只在模块的 fixtures 子目录生成测试历史，不触碰真实来源。"""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("history_service", HERE / "service.py")
import sys
service = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = service
spec.loader.exec_module(service)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        (HERE / "fixtures").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=HERE / "fixtures")
        self.root = Path(self.temp.name)
        self.logs = self.root / "codex"
        self.archive = self.root / "archive"
        self.claude = self.root / "claude"
        for p in (self.logs, self.archive, self.claude):
            p.mkdir()
        self.codex = self.root / "codex.sqlite"
        c = sqlite3.connect(self.codex)
        c.execute("CREATE TABLE threads(id TEXT,rollout_path TEXT,cwd TEXT,title TEXT,created_at INT,updated_at INT,source TEXT,thread_source TEXT,archived INT)")
        c.commit()
        c.close()
        self.open = self.root / "open.sqlite"
        c = sqlite3.connect(self.open)
        for table in ("session", "session_v2"):
            c.execute("CREATE TABLE " + table + "(id TEXT,parent_id TEXT,directory TEXT,title TEXT,time_created INT,time_updated INT,time_archived INT)")
        c.execute("CREATE TABLE session_message(id TEXT,session_id TEXT,type TEXT,time_created INT,data TEXT)")
        c.execute("CREATE TABLE message(id TEXT,session_id TEXT,time_created INT,data TEXT)")
        c.execute("CREATE TABLE part(id TEXT,message_id TEXT,time_created INT,data TEXT)")
        c.commit()
        c.close()
        self.sources = service.Sources(self.codex, (self.logs, self.archive), self.claude, self.open, None, None, None, None, None)
        self.db = self.root / "out" / "index.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def log(self, lines, name="a", sub=False):
        path = self.logs / (name + ".jsonl")
        meta = {"timestamp": "2026-10-01T01:00:00Z", "type": "session_meta", "payload": {"id": name, "cwd": "/项目/中文", "thread_source": "subagent" if sub else "user", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "a"}}} if sub else "cli"}}
        records = [meta] + lines
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")
        c = sqlite3.connect(self.codex)
        c.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)", (name, str(path), "/项目/中文", "中文工作", 1790816400, 1790817000, json.dumps(meta["payload"]["source"]), meta["payload"]["thread_source"], 0))
        c.commit()
        c.close()
        return path

    def msg(self, text, role="user", phase=None, at="2026-10-01T01:01:00Z"):
        payload = {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}
        if phase:
            payload["phase"] = phase
        return {"timestamp": at, "type": "response_item", "payload": payload}

    def index(self, **kw):
        return service.run_index(sources=self.sources, db_path=self.db, **kw)

    def query(self, q, **kw):
        return service.search({"q": [q], **{k: [v] for k, v in kw.items()}}, self.db)

    def test_chinese_casefold_and_literal_sql(self):
        self.log([self.msg("请复核中文视频验收与本地模型推理性能，进度100%_done")])
        self.index()
        self.assertEqual(self.query("中文 验收")["total"], 1)
        self.assertEqual(self.query("本地模型")["total"], 1)
        self.assertEqual(self.query("100%_done")["total"], 1)
        self.assertEqual(self.query("' OR 1=1 --")["total"], 0)
        self.assertEqual(self.query("视频", project="/不相干")["total"], 0)

    def test_dedup_preserves_real_repeated_request(self):
        user = self.msg("继续验收")
        mirrored = {"timestamp": "2026-10-01T01:01:00.010Z", "type": "event_msg", "payload": {"type": "user_message", "message": "继续验收"}}
        final = self.msg("验收完成", "assistant", "final_answer", "2026-10-01T01:02:00Z")
        complete = {"timestamp": "2026-10-01T01:02:00.010Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "验收完成"}}
        self.log([user, mirrored, self.msg("继续验收", at="2026-10-01T01:05:00Z"), final, complete])
        self.index()
        self.assertEqual(self.query("继续")["total"], 2)
        self.assertEqual(self.query("验收完成")["total"], 1)
        self.assertEqual(service.manifest({}, self.db)["coverage"]["duplicates_removed"], 2)

    def test_old_codex_final_and_skip_tools(self):
        final = self.msg("早期结论", "assistant")
        self.log([self.msg("进度消息", "assistant", "commentary"), self.msg("系统原文", "developer"), {"type": "response_item", "payload": {"type": "function_call_output", "output": "工具秘密"}}, final, {"timestamp": "2026-10-01T01:03:00Z", "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "早期结论"}}])
        self.index()
        self.assertEqual(self.query("早期结论")["total"], 1)
        self.assertEqual(self.query("进度")["total"], 0)
        self.assertEqual(self.query("系统")["total"], 0)
        self.assertEqual(self.query("工具秘密")["total"], 0)
        self.assertEqual(self.query("早期结论")["items"][0]["line_no"], 5)

    def test_corrupt_and_oversize_have_manifest(self):
        p = self.log([self.msg("有效结论")])
        with p.open("a", encoding="utf-8") as f:
            f.write("{坏日志\n" + json.dumps({"text": "x" * 4000}) + "\n")
            f.write(json.dumps(self.msg("后续有效"), ensure_ascii=False) + "\n")
        self.index(max_line=1000)
        report = service.manifest({}, self.db)
        self.assertEqual(report["coverage"]["malformed_lines"], 1)
        self.assertEqual(report["coverage"]["skipped_large_lines"], 1)
        self.assertEqual(self.query("后续有效")["total"], 1)
        self.assertEqual(report["statuses"]["partial"], 1)

    def test_redaction(self):
        raw = 'api_key="sk-testabc1234567890" password=demoSecret Bearer abcdefghijklmnop credentials={"anything":{"nested":"opaqueCredential"}} https://name:pass@example.test 密码：示例密码 OPENAI_API_KEY=envPlainSecret'
        self.log([self.msg(raw)])
        self.index()
        result = service.session_detail("codex:a", {}, self.db)["messages"][0]["text"]
        for secret in ["sk-testabc1234567890", "demoSecret", "abcdefghijklmnop", "opaqueCredential", "name:pass", "示例密码", "envPlainSecret"]:
            self.assertNotIn(secret, result)
        self.assertIn("已脱敏", result)
        self.assertEqual(service.redact(raw), service.redact(service.redact(raw)))

    def test_incremental_append_unchanged_delete(self):
        p = self.log([self.msg("第一段")])
        self.index()
        second = self.index()
        self.assertEqual(second["unchanged_files"], 1)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.msg("新增段", at="2026-10-01T01:09:00Z"), ensure_ascii=False) + "\n")
        self.index()
        self.assertEqual(self.query("新增段")["total"], 1)
        p.unlink()
        result = self.index()
        self.assertEqual(self.query("第一段")["total"], 0)
        self.assertEqual(result["counts"]["sessions"], 1)
        self.assertEqual(service.manifest({}, self.db)["statuses"]["missing"], 1)

    def test_subagent_and_date_filters(self):
        self.log([self.msg("主会话验收")])
        self.log([self.msg("子任务验收")], "b", True)
        self.index()
        self.assertEqual(self.query("验收")["total"], 1)
        self.assertEqual(self.query("验收", main="all")["total"], 2)
        self.assertEqual(self.query("验收", main="sub")["total"], 1)
        self.assertEqual(self.query("验收", **{"from": "2026-10-01", "to": "2026-10-01"})["total"], 1)
        self.assertEqual(self.query("验收", **{"from": "2026-10-02"})["total"], 0)

    def test_claude_only_visible_final(self):
        p = self.claude / "claude-main.jsonl"
        records = [
            {"type": "user", "sessionId": "inherited-id", "cwd": "/项目/克劳德", "timestamp": "2026-10-01T01:00:00Z", "message": {"role": "user", "content": [{"type": "tool_result", "content": "工具原文"}, {"type": "text", "text": "请制作视频"}]}},
            {"type": "assistant", "timestamp": "2026-10-01T01:01:00Z", "message": {"role": "assistant", "stop_reason": "tool_use", "content": [{"type": "text", "text": "执行进度"}]}},
            {"type": "assistant", "timestamp": "2026-10-01T01:02:00Z", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "thinking", "thinking": "隐藏思考"}, {"type": "text", "text": "视频已交付"}]}},
        ]
        p.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")
        self.index()
        self.assertEqual(self.query("视频", source="claude")["total"], 2)
        self.assertEqual(self.query("执行进度", source="claude")["total"], 0)
        self.assertEqual(service.session_detail("claude:claude-main", {}, self.db)["session"]["project"], "/项目/克劳德")

    def test_opencode_old_new_identity_and_row_provenance(self):
        c = sqlite3.connect(self.open)
        for t in ("session", "session_v2"):
            c.execute("INSERT INTO " + t + " VALUES('ses_same',NULL,'/项目/开源','会话',1790816400000,1790816500000,NULL)")
        text = "合并视频需求"
        c.execute("INSERT INTO session_message VALUES('msg1','ses_same','user',1790816400000,?)", (json.dumps({"text": text}),))
        c.execute("INSERT INTO message VALUES('msg1','ses_same',1790816400000,?)", (json.dumps({"role": "user"}),))
        c.execute("INSERT INTO part VALUES('part1','msg1',1790816400000,?)", (json.dumps({"type": "text", "text": text}),))
        c.execute("INSERT INTO session_message VALUES('msg2','ses_same','assistant',1790816500000,?)", (json.dumps({"finish": "stop", "content": [{"type": "text", "text": "交付结论"}]}),))
        c.commit()
        c.close()
        job = self.index()
        self.assertEqual(job["counts"]["sessions"], 1)
        self.assertEqual(self.query("合并视频", source="opencode")["total"], 1)
        item = self.query("交付结论", source="opencode")["items"][0]
        self.assertIsNone(item["line_no"])
        self.assertEqual(item["row_locator"], "session_message.id=msg2")

    def test_rebuild_and_anchor_context(self):
        self.log([self.msg("段落" + str(i), at="2026-10-01T01:%02d:00Z" % i) for i in range(12)])
        self.index(mode="rebuild")
        item = self.query("段落11")["items"][0]
        detail = service.session_detail("codex:a", {"anchor": [str(item["id"])], "limit": ["5"]}, self.db)
        self.assertEqual(detail["page"], 3)
        self.assertIn("段落11", [m["text"] for m in detail["messages"]])

    def test_lock_prevents_concurrent_writers(self):
        held = service.acquire_file_lock(self.db.parent)
        try:
            with self.assertRaises(RuntimeError):
                self.index()
        finally:
            held.close()

    def test_source_failure_keeps_existing_results(self):
        self.log([self.msg("应保留的结论")])
        self.index()
        self.logs.rename(self.root / "temporarily-unavailable")
        result = self.index()
        self.assertEqual(self.query("应保留")["total"], 1)
        self.assertEqual(result["state"], "completed_with_limits")
        self.assertTrue(result["errors"])

    def test_codex_sqlite_supplement_is_not_double_counted(self):
        self.log([self.msg("镜像需求"), self.msg("镜像结论", "assistant", "final_answer")])
        source = self.root / "thread-history.sqlite"
        c = sqlite3.connect(source)
        c.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INT,created_at_ms INT,item_type TEXT,item_json TEXT)")
        c.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,final_agent_item_id TEXT)")
        c.execute("INSERT INTO thread_turns VALUES('a','turn1','final1')")
        rows = [
            ('a', 'turn1', 'user1', 1, 1790816460000, 'userMessage', {'content': [{'type': 'text', 'text': '镜像需求'}]}),
            ('a', 'turn1', 'final1', 2, 1790816460000, 'agentMessage', {'text': '镜像结论', 'phase': 'final_answer'}),
            ('a', 'turn2', 'only1', 3, 1790816490000, 'agentMessage', {'text': 'SQLite独有结论', 'phase': 'final_answer'}),
            ('a', 'turn3', 'progress', 4, 1790816500000, 'agentMessage', {'text': '不能索引的进度', 'phase': 'commentary'}),
        ]
        for row in rows:
            c.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", row[:-1] + (json.dumps(row[-1]),))
        c.commit()
        c.close()
        self.sources.codex_history_db = source
        result = self.index()
        self.assertEqual(result['sources']['codex']['sqlite_supplement']['mirrors_removed'], 2)
        self.assertEqual(self.query('镜像结论')['total'], 1)
        self.assertEqual(self.query('SQLite独有')['total'], 1)
        self.assertEqual(self.query('不能索引')['total'], 0)
        self.index()
        self.assertEqual(self.query('SQLite独有')['total'], 1)

    def test_pi_hermes_and_metadata_only_tasks(self):
        pi = self.root / "pi"
        pi.mkdir()
        records = [
            {"type": "session", "id": "pi1", "cwd": "/项目/pi", "timestamp": "2026-10-01T01:00:00Z"},
            {"type": "message", "timestamp": "2026-10-01T01:01:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "Pi视频需求"}]}},
            {"type": "message", "timestamp": "2026-10-01T01:02:00Z", "message": {"role": "assistant", "stopReason": "stop", "content": [{"type": "text", "text": "Pi终答"}]}},
            {"type": "message", "message": {"role": "toolResult", "content": [{"type": "text", "text": "工具数据不导入"}]}},
        ]
        (pi / "pi1.jsonl").write_text(''.join(json.dumps(x) + '\n' for x in records))
        hermes = self.root / 'hermes.sqlite'
        c = sqlite3.connect(hermes)
        c.execute('CREATE TABLE sessions(id TEXT,title TEXT,cwd TEXT,parent_session_id TEXT,started_at REAL,ended_at REAL,archived INT)')
        c.execute("INSERT INTO sessions VALUES('h1','Hermes记录','/项目/hermes',NULL,1790816400,NULL,0)")
        c.execute('CREATE TABLE messages(id INT,session_id TEXT,role TEXT,timestamp REAL,finish_reason TEXT,content TEXT,tool_calls TEXT)')
        c.execute("INSERT INTO messages VALUES(1,'h1','assistant',1790816500,'stop','Hermes终答',NULL)")
        c.execute("INSERT INTO messages VALUES(2,'h1','assistant',1790816501,'tool_calls','执行进度','[]')")
        c.commit();c.close()
        tasks = self.root / 'tasks.sqlite'
        c = sqlite3.connect(tasks)
        c.execute('CREATE TABLE tasks(workspace_path TEXT,task_id TEXT,title TEXT,task_status TEXT,provider TEXT,created_at INT,updated_at INT,archived INT,deleted INT)')
        c.execute("INSERT INTO tasks VALUES('/项目/tasks','t1','目录任务','done','glm',1790816400,1790816500,0,0)")
        c.commit();c.close()
        self.sources.pi_root=pi;self.sources.hermes_db=hermes;self.sources.zcode_tasks_db=tasks
        result=self.index()
        self.assertEqual(result['counts']['sessions'],2)
        self.assertEqual(result['counts']['metadata_tasks'],1)
        self.assertEqual(self.query('Pi',source='pi')['total'],2)
        self.assertEqual(self.query('Hermes终答',source='hermes')['total'],1)
        self.assertEqual(self.query('执行进度')['total'],0)
        self.assertEqual(self.query('目录任务',source='zcode_tasks')['sessions_total'],1)
        self.assertEqual(self.query('目录任务',source='zcode_tasks')['total'],0)


class StatusAndContractTest(unittest.TestCase):
    """状态年龄、来源计数与模块路径契约（不再二次 unquote、405 对齐）。"""

    def test_status_age_sources_and_key_literal(self):
        with tempfile.TemporaryDirectory(dir=HERE / "fixtures") as tmp:
            db = Path(tmp) / "index.sqlite"
            c = service.connect(db)
            c.execute("INSERT INTO sessions(key,source,native_id,title,is_subagent) VALUES('claude:%41%2F','claude','native','',0)")
            c.execute("INSERT INTO meta VALUES('last_index',?)", (service.now(),))
            c.commit()
            c.close()
            result = service.status(db)
            self.assertIsNotNone(result["last_index_age_seconds"])
            self.assertGreaterEqual(result["last_index_age_seconds"], 0)
            self.assertEqual(result["counts"]["sources"], 1)
            # 会话 key 中的 % 字面量必须原样传递，不得被二次解码。
            detail = service.session_detail("claude:%41%2F", {}, db)
            self.assertEqual(detail["session"]["key"], "claude:%41%2F")

    def test_api_method_not_allowed(self):
        self.assertEqual(service.api("GET", "/index", {}, {})[0], 405)
        self.assertEqual(service.api("DELETE", "/status", {}, {})[0], 405)
        self.assertEqual(service.api("GET", "/nope", {}, {})[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
