"""有意义的合约测试：缺失/变化、字幕、Office、路径边界与人工记录。"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("delivery_service", HERE / "service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery-test-", dir=HERE)
        self.root = Path(self.temp.name) / "outputs"
        self.root.mkdir()
        store = Path(self.temp.name) / "store"
        self.patches = [patch.object(service, "MANIFEST_DIR", store / "manifests"), patch.object(service, "ACCEPT_DIR", store / "acceptances")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def write(self, path, text):
        p = self.root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def codes(self, result):
        return {x["code"] for x in result["issues"]}

    def test_missing_changed_and_expired_acceptance(self):
        self.write("report.md", "内容一")
        self.write("evidence.json", "{}")
        old = service.scan(str(self.root), ["report.md", "evidence.json"])
        self.assertEqual(old["human"]["status"], "not_recorded")
        self.assertEqual(old["levels"]["publishing"], "not_recorded")
        result = service.accept({"manifest_id": old["id"], "manifest_hash": old["manifest_hash"], "decision": "accepted", "acknowledgment": True, "note": "已逐项打开此测试快照对应文件。"})
        self.assertEqual(result["human"]["status"], "accepted")
        self.write("report.md", "内容二，有修改")
        (self.root / "evidence.json").unlink()
        new = service.scan(str(self.root), ["report.md", "evidence.json"])
        comparison = service.compare(old, new)
        self.assertEqual(comparison["changed"], ["report.md"])
        self.assertEqual(comparison["missing"], ["evidence.json"])
        self.assertEqual(new["human"]["status"], "stale")
        self.assertIn("expected_missing", self.codes(new))
        self.assertEqual(service.exit_status(new), 1)
        self.assertEqual(service.exit_status(comparison, True), 1)

    def test_cannot_infer_human_acceptance_or_accept_without_confirmation(self):
        self.write("valid.json", '{"value":1}')
        value = service.scan(str(self.root))
        self.assertEqual(value["levels"]["automatic"], "pass")
        self.assertEqual(value["human"]["status"], "not_recorded")
        self.assertEqual(value["levels"]["native_rendering"], "not_run")
        body = {"manifest_id": value["id"], "manifest_hash": value["manifest_hash"], "decision": "accepted", "note": "没有确认"}
        status, response = service.api("POST", "/accept", {}, body)
        self.assertEqual(status, 400)
        self.assertIn("显式确认", response["error"])
        self.assertEqual(service.acceptance_for(value)["status"], "not_recorded")
        body.update(acknowledgment=True, manifest_hash="wrong")
        self.assertEqual(service.api("POST", "/accept", {}, body)[0], 400)

    def test_subtitle_invalid_and_duration_overrun(self):
        entry = {"path": "video.srt", "checks": []}
        ctx = {"issues": []}
        text = "1\n00:00:00,500 --> 00:00:03,000\n第一条\n\n2\n00:00:02,500 --> 00:00:02,000\n非法\n\n3\n00:62:00,000 --> 00:63:00,000\n非法分钟\n"
        media = [{"path": "video.mp4", "media": {"duration": 2.0}}]
        service.subtitles(text, entry, media, ctx)
        self.assertTrue(any("超出媒体时长" in x["message"] for x in ctx["issues"]))
        self.assertTrue(any("时间格式或区间不合法" in x["message"] for x in ctx["issues"]))
        self.assertEqual(entry["subtitle"]["cues"], 1)
        self.assertTrue(entry["subtitle"]["duration_checked"])
        vtt = {"path": "video.vtt", "checks": []}
        service.subtitles("WEBVTT\n\n00:00.000 --> 00:01.900\n测试", vtt, media, {"issues": []})
        self.assertEqual(vtt["subtitle"]["cues"], 1)
        with self.assertRaises(ValueError):
            service.timestamp("00:1:00,000", False)

    def test_media_probe_and_subtitle_real_audio(self):
        path = self.root / "audio.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000)
        self.write("audio.srt", "1\n00:00:00,000 --> 00:00:02,000\n字幕越界\n")
        value = service.scan(str(self.root), save=False)
        audio = next(x for x in value["files"] if x["kind"] == "media")
        if service.shutil.which("ffprobe"):
            self.assertEqual(audio["media"]["audio_tracks"], 1)
            self.assertAlmostEqual(audio["media"]["duration"], 1.0, places=2)
            self.assertIn("subtitle_invalid", self.codes(value))
        else:
            self.assertEqual(audio["media"]["status"], "unknown")
            self.assertIn("subtitle_duration_unknown", self.codes(value))

    def test_office_missing_members_dangerous_paths_and_broken_zip(self):
        with zipfile.ZipFile(self.root / "missing.docx", "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        with zipfile.ZipFile(self.root / "escape.xlsx", "w") as z:
            z.writestr("../escape.xml", "bad")
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("_rels/.rels", "<Relationships/>")
            z.writestr("xl/workbook.xml", "<workbook/>")
        (self.root / "broken.pptx").write_bytes(b"not a ZIP")
        with zipfile.ZipFile(self.root / "valid.docx", "w") as z:
            for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                z.writestr(name, "<root/>")
        value = service.scan(str(self.root), save=False)
        errors = [x["message"] for x in value["issues"] if x["code"] == "office_invalid"]
        self.assertTrue(any("缺少必要成员" in x for x in errors))
        self.assertTrue(any("危险成员路径" in x for x in errors))
        self.assertTrue(any("容器损坏" in x for x in errors))
        good = next(x for x in value["files"] if x["path"] == "valid.docx")
        self.assertTrue(good["office"]["crc_complete"])
        self.assertEqual(good["office"]["rendering"], "not_run")

    def test_office_crc_damage_and_container_budget(self):
        target = self.root / "crc.docx"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as z:
            for name in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                z.writestr(name, "<root/>")
        raw = bytearray(target.read_bytes())
        index = raw.index(b"<root/>")
        raw[index] = ord("X")
        target.write_bytes(raw)
        value = service.scan(str(self.root), save=False)
        self.assertIn("office_invalid", self.codes(value))
        with zipfile.ZipFile(self.root / "large.docx", "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("word/document.xml", "x" * (1024 * 1024 + 1))
        value = service.scan(str(self.root), budget_values={"office_uncompressed_mb": 1}, save=False)
        self.assertIn("office_container_budget", self.codes(value))

    def test_path_escape_symlinks_sensitive_names_and_markdown_links(self):
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text('{"do_not_read":true}')
        self.write("README.md", "[缺失](missing.pdf)\n[越界](../outside.json)\n[链接](alias.json)\n[密钥](credentials.json)\n[正常](good.json)\n[外部](https://example.com)\n")
        self.write("good.json", "{}")
        self.write("credentials.json", "not JSON; must never parse")
        self.write(".env", "not read")
        self.write("node_modules/leak.json", "not JSON; must never parse")
        os.symlink(outside, self.root / "alias.json")
        value = service.scan(str(self.root), save=False)
        paths = {x["path"] for x in value["files"]}
        self.assertEqual(paths, {"README.md", "good.json"})
        self.assertNotIn("json_invalid", self.codes(value))
        self.assertIn("markdown_link_escape", self.codes(value))
        self.assertIn("markdown_link_missing", self.codes(value))
        self.assertIn("markdown_link_blocked", self.codes(value))
        for target in ("../outside.json", "/etc/passwd", "credentials.json", "folder\\file.json"):
            with self.assertRaises(service.InputError):
                service.scan(str(self.root), [target], save=False)
        status, _ = service.api("GET", "/manifest", {"id": ["../x"]}, {})
        self.assertEqual(status, 400)
        for path in (".", "./"):
            status, response = service.api("POST", "/scan", {}, {"directory": str(self.root), "expected": [path]})
            self.assertEqual(status, 400)
            self.assertIn("文件路径", response["error"])

    def test_hash_budget_unrun_uncertain_and_manual_blocked(self):
        (self.root / "big.bin").write_bytes(b"x" * (1024 * 1024 + 1))
        value = service.scan(str(self.root), budget_values={"hash_file_mb": 1})
        self.assertEqual(value["files"][0]["hash_state"], "not_run_file_budget")
        self.assertEqual(service.exit_status(value), 3)
        # 同目录再扫一次得到另一个快照；两侧都缺哈希时差异应归为「无法确认」而非一致。
        again = service.scan(str(self.root), budget_values={"hash_file_mb": 1})
        comparison = service.compare(value, again)
        self.assertEqual(comparison["uncertain"], ["big.bin"])
        self.assertFalse(comparison["content_comparison_complete"])
        with self.assertRaises(service.InputError):
            service.compare(value, value)
        with self.assertRaises(service.InputError):
            service.accept({"manifest_id": value["id"], "manifest_hash": value["manifest_hash"], "decision": "accepted", "acknowledgment": True, "note": "应被拒绝"})

    def test_inventory_partial_does_not_claim_missing(self):
        self.write("a.json", "{}")
        self.write("b.json", "{}")
        old = service.scan(str(self.root), save=False)
        new = service.scan(str(self.root), budget_values={"max_files": 1}, save=False)
        self.assertFalse(new["coverage"]["inventory_complete"])
        comparison = service.compare(old, new)
        self.assertEqual(comparison["missing"], [])
        self.assertEqual(len(comparison["uncertain"]), 1)
        inverse = service.compare(new, old)
        self.assertEqual(inverse["added"], [])
        self.assertEqual(len(inverse["uncertain"]), 1)
        self.assertFalse(inverse["content_comparison_complete"])

    def test_invalid_json_and_snapshot_tamper(self):
        self.write("invalid.json", '{"bad": NaN}')
        value = service.scan(str(self.root))
        self.assertIn("json_invalid", self.codes(value))
        saved = service.MANIFEST_DIR / (value["id"] + ".json")
        corrupted = json.loads(saved.read_text())
        corrupted["files"][0]["sha256"] = "wrong"
        saved.write_text(json.dumps(corrupted))
        with self.assertRaises(service.InputError):
            service.read_manifest(value["id"])

    def test_check_result_tamper_is_also_detected(self):
        self.write("invalid.json", "{")
        value = service.scan(str(self.root))
        saved = service.MANIFEST_DIR / (value["id"] + ".json")
        corrupted = json.loads(saved.read_text())
        corrupted["issues"] = []
        corrupted["summary"]["errors"] = 0
        saved.write_text(json.dumps(corrupted))
        with self.assertRaises(service.InputError):
            service.read_manifest(value["id"])

    def test_expected_list_change_is_visible_in_compare(self):
        self.write("a.json", "{}")
        old = service.scan(str(self.root), [], save=False)
        new = service.scan(str(self.root), ["a.json"], save=False)
        value = service.compare(old, new)
        self.assertEqual(value["expected_added"], ["a.json"])
        self.assertEqual(service.exit_status(value, True), 1)

    def test_cli_error_and_issue_exit_codes(self):
        self.write("invalid.json", "{")
        command = [sys.executable, str(HERE / "service.py"), "check", str(self.root), "--no-save"]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        json.loads(result.stdout)
        invalid = subprocess.run([sys.executable, str(HERE / "service.py"), "check", "relative-path"], capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("error", json.loads(invalid.stdout))

    def test_empty_and_nonexistent_directories_have_truthful_states(self):
        value = service.scan(str(self.root))
        self.assertIn("empty_directory", self.codes(value))
        self.assertNotEqual(value["levels"]["automatic"], "pass")
        with self.assertRaises(service.InputError):
            service.accept({"manifest_id": value["id"], "manifest_hash": value["manifest_hash"], "decision": "accepted", "acknowledgment": True, "note": "没有成品应被拒绝"})
        status, result = service.api("POST", "/scan", {}, {"directory": str(self.root / "absent")})
        self.assertEqual(status, 400)
        self.assertIn("目录不存在", result["error"])


class LedgerAndAuditTest(unittest.TestCase):
    """登记簿、人工验收防篡改、预算预估与伪哈希快照拒绝。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery-ledger-", dir=HERE)
        self.root = Path(self.temp.name) / "out"
        self.root.mkdir()
        store = Path(self.temp.name) / "store"
        self.patches = [patch.object(service, "MANIFEST_DIR", store / "manifests"), patch.object(service, "ACCEPT_DIR", store / "acceptances")]
        for p in self.patches:
            p.start()
        (self.root / "a.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_scan_registers_ledger_and_audit_detects_removal(self):
        value = service.scan(str(self.root))
        audit = service.audit()
        self.assertEqual(audit["missing"], [])
        self.assertEqual(audit["tampered"], [])
        self.assertEqual(audit["unregistered"], [])
        (service.MANIFEST_DIR / (value["id"] + ".json")).unlink()
        audit = service.audit()
        self.assertEqual(len(audit["missing"]), 1)
        self.assertEqual(audit["missing"][0]["id"], value["id"])

    def test_accept_records_operator_summary_and_detects_tamper(self):
        value = service.scan(str(self.root))
        result = service.accept({"manifest_id": value["id"], "manifest_hash": value["manifest_hash"], "decision": "accepted", "acknowledgment": True, "note": "全部打开查看"})
        record = result["record"]
        self.assertTrue(record["operator"])
        self.assertEqual(record["snapshot_summary"]["files"], 1)
        self.assertFalse(record["published"])
        path = service.ACCEPT_DIR / (record["id"] + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["note"] = "被篡改的记录"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        human = service.acceptance_for(value)
        self.assertIn("损坏", human["message"])

    def test_estimate_suggests_budgets(self):
        est = service.estimate(str(self.root))
        self.assertEqual(est["files"], 1)
        self.assertEqual(est["suggested_budgets"]["hash_total_mb"], 1)

    def test_fake_sha256_manifest_rejected(self):
        value = service.scan(str(self.root))
        path = service.MANIFEST_DIR / (value["id"] + ".json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["files"][0]["sha256"] = 'x" onmouseover="alert(1)'
        data["manifest_hash"] = service.digest(service.manifest_payload(data))
        data["evidence_hash"] = service.digest(service.evidence_payload(data))
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(service.InputError):
            service.read_manifest(value["id"])
        _, corrupt = service.list_manifests()
        self.assertEqual(len(corrupt), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
