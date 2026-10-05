"""解析、故障保留与归属边界验证；不触发真实重任务。"""
import copy
import json
import unittest
from unittest.mock import patch

import service

VM = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free: 6553600.
Pages active: 200.
Pages inactive: 100.
Pages wired down: 100.
Pages occupied by compressor: 100.
Pages stored in compressor: 300.
Compressions: 10.
Decompressions: 4.
Swapins: 2.
Swapouts: 5.
'''
PS = ''' 42 1 2048 12.5 01:23 Fri Oct  2 20:00:00 2026 /opt/python
 43 42 1024 0.0 00:20 Fri Oct  2 20:01:00 2026 /opt/a tool
'''


def sample():
    processes, _ = service.parse_ps(PS)
    return {"schema": service.SCHEMA, "status": "ok", "captured_at": "2026-10-02T12:00:00+00:00", "memory": {"total_bytes": 256 * service.GIB, "vm": service.parse_vm(VM), "swap": {"used_bytes": 0}, "pressure_status": "normal"}, "processes": {"status": "ok", "items": processes}, "listeners": {"status": "ok", "items": [{"pid": 42, "owner_started_local": processes[0]["started_local"], "endpoint": "127.0.0.1:8000"}]}}


def result(output, status="ok"):
    return {"status": status, "output": output, "error": None, "command": ["test"], "started_at": "test", "duration_ms": 0, "exit_code": 0}


class RuntimeTests(unittest.TestCase):
    def test_actual_16k_page_units_and_compressor_distinction(self):
        parsed = service.parse_vm(VM)
        self.assertEqual(parsed["free_bytes"], 100 * service.GIB)
        self.assertEqual(parsed["compressed_bytes"], 100 * 16384)
        self.assertEqual(parsed["stored_in_compressor_bytes"], 300 * 16384)
        with self.assertRaises(ValueError):
            service.parse_vm("Pages free: 0.")

    def test_zero_is_valid_swap_missing_is_unknown(self):
        self.assertEqual(service.parse_swap("total = 1.00G used = 0.00M free = 1.00G")["used_bytes"], 0)
        with self.assertRaises(ValueError):
            service.parse_swap("used = 0M")
        failed = result("", "unknown")
        self.assertIsNone(service.parsed(failed, service.parse_swap))

    def test_process_identity_and_rss_units(self):
        processes, skipped = service.parse_ps(PS)
        self.assertEqual(skipped, 0)
        self.assertEqual(processes[0]["pid"], 42)
        self.assertEqual(processes[0]["ppid"], 1)
        self.assertEqual(processes[0]["rss_bytes"], 2048 * 1024)
        self.assertEqual(processes[1]["command"], "/opt/a tool")
        with self.assertRaises(ValueError):
            service.parse_ps("unexpected output")

    def test_listener_pid_ipv6_and_duplicates(self):
        listeners = service.parse_listeners("p42\ncPython\nf8\nn127.0.0.1:8000\nn127.0.0.1:8000\np43\ncnode\nn[::1]:8877\nn*:3000\n")
        self.assertEqual(len(listeners), 3)
        self.assertTrue(next(x for x in listeners if x["port"] == 8877)["loopback"])
        self.assertFalse(next(x for x in listeners if x["port"] == 3000)["loopback"])
        self.assertEqual(next(x for x in listeners if x["port"] == 8000)["pid"], 42)

    def test_missing_commands_remain_unknown_not_healthy(self):
        with patch.object(service.shutil, "which", return_value=None):
            snap = service.snapshot()
        self.assertEqual(snap["status"], "partial")
        self.assertIsNone(snap["memory"]["total_bytes"])
        self.assertIsNone(snap["processes"]["items"])
        self.assertIsNone(snap["listeners"]["items"])
        self.assertEqual(snap["memory"]["pressure_status"], "unknown")
        check = service.check_preflight(snap, 8, "inference")
        self.assertEqual(check["status"], "unknown")
        self.assertIsNone(check["projected_free_bytes"])

    def test_timeout_is_unknown(self):
        with patch.object(service.shutil, "which", return_value="/usr/bin/ps"), patch.object(service.subprocess, "run", side_effect=service.subprocess.TimeoutExpired(["ps"], 4)):
            value = service.run(["ps"])
        self.assertEqual(value["status"], "unknown")
        self.assertIsNone(value["output"])

    def test_guardrail_threshold_and_no_safe_claim(self):
        current = sample()
        self.assertEqual(service.check_preflight(current, 8, "tts")["status"], "within_rule")
        self.assertEqual(service.check_preflight(current, 80, "tts")["status"], "warning")
        current["memory"]["pressure_status"] = "warning"
        self.assertEqual(service.check_preflight(current, 0, "render")["status"], "warning")
        current["memory"]["vm"] = None
        self.assertEqual(service.check_preflight(current, 0, "render")["status"], "warning")
        for bad in (-1, float("inf"), float("nan"), True, "8"):
            with self.assertRaises(ValueError):
                service.check_preflight(sample(), bad, "tts")

    def test_pid_reuse_does_not_match_rss(self):
        left, right = sample(), sample()
        right["processes"]["items"][0]["started_local"] = "2026-10-02T22:00:00"
        comparison = service.compare_samples(left, right)
        self.assertEqual(len(comparison["processes"]["new"]), 1)
        self.assertEqual(len(comparison["processes"]["gone"]), 1)
        self.assertNotIn(42, [x["pid"] for x in comparison["processes"]["matched_rss_changes"]])

    def test_unavailable_process_list_is_not_all_gone(self):
        left, right = sample(), sample()
        right["processes"] = {"status": "unknown", "items": None}
        right["memory"]["vm"] = None
        comparison = service.compare_samples(left, right)
        self.assertIsNone(comparison["processes"]["gone"])
        self.assertEqual(comparison["memory_changes"][0]["status"], "unknown")

    def test_cwd_pid_reuse_ownership_is_unknown(self):
        newer = PS.replace("20:00:00", "21:00:00")
        with patch.object(service, "run", side_effect=[result(PS), result("p42\nn/a/project\n"), result(newer)]):
            detail = service.process_detail(42)
        self.assertFalse(detail["identity_confirmed"])
        self.assertIsNone(detail["cwd"])
        self.assertEqual(detail["status"], "unknown")

    def test_current_parent_and_cwd_chain(self):
        with patch.object(service, "run", side_effect=[result(PS), result("p43\nn/a/project\n"), result(PS.splitlines()[1])]):
            detail = service.process_detail(43)
        self.assertTrue(detail["identity_confirmed"])
        self.assertEqual(detail["cwd"], "/a/project")
        self.assertEqual([x["pid"] for x in detail["parent_chain"]], [43, 42])

    def test_incident_short_fields_and_time(self):
        text = json.dumps({"timestamp": "2026-09-23 11:00:00 +0800"}) + "\n" + json.dumps({"panicString": "watchdog timeout: no checkins\nPRIVATE OTHER DATA", "largestProcess": "python", "processes": [{"reason": "vm-compressor-space-shortage"}]})
        data = service.incident_extract(text)
        self.assertEqual(data["reported_time"], "2026-09-23 11:00:00 +0800")
        self.assertNotIn("PRIVATE OTHER DATA", str(data))
        self.assertTrue(any("vm-compressor-space-shortage" in x["text"] for x in data["snippets"]))

    def test_jetsam_reason_after_large_process_list_and_panic_alias(self):
        processes = [{"name": "irrelevant"}] * 900 + [{"name": "python", "reason": "vm-compressor-space-shortage"}]
        data = service.incident_extract(json.dumps({"largestProcess": "python", "processes": processes}))
        self.assertEqual(data["reason_status"], "observed")
        self.assertTrue(any(x["text"] == "vm-compressor-space-shortage" for x in data["snippets"]))
        panic = service.incident_extract(json.dumps({"panic_string": "watchdog timeout: no checkins"}))
        self.assertEqual(panic["snippets"][0]["field"], "panic_string")

    def test_ids_cannot_escape_module_data(self):
        for bad in ("../secret", "", "x/../../auth", None):
            self.assertFalse(service.valid_id(bad))
            with self.assertRaises(ValueError):
                service.load_saved(bad)

    def test_comm_secret_redaction(self):
        text = service.scrub("python --api-key=SECRET123 --token BEARER123 password=PASS123 Authorization: Bearer ABC123 sk-abcdefghijk")
        for secret in ("SECRET123", "BEARER123", "PASS123", "ABC123", "sk-abcdefghijk"):
            self.assertNotIn(secret, text)

    def test_api_validation(self):
        self.assertEqual(service.api("GET", "/process", {"pid": ["42; kill 1"]}, {})[0], 400)
        self.assertEqual(service.api("POST", "/snapshots", {}, {"name": "x" * 101})[0], 400)
        self.assertEqual(service.api("GET", "/snapshots/../../auth", {}, {})[0], 400)
        self.assertEqual(service.api("DELETE", "/snapshot", {}, {})[0], 405)


class PersistAndPreflightTest(unittest.TestCase):
    """保存来源标记与开工前检查同源保存。"""

    def test_persist_source_and_preflight_save(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(service, "DATA", pathlib.Path(tmp)):
                value = service.persist_sample(sample(), "检查前", source="preflight")
                self.assertEqual(value["source"], "preflight")
                listing = service.saved_list()
                self.assertEqual(listing["items"][0]["source"], "preflight")
            with patch.object(service, "DATA", pathlib.Path(tmp)), patch.object(service, "snapshot", return_value=sample()):
                status, payload = service.api("POST", "/preflight", {}, {"extra_gib": 8, "task": "inference", "save": True, "name": "推理前"})
                self.assertEqual(status, 200)
                self.assertEqual(payload["saved"]["name"], "推理前")
                self.assertEqual(payload["check"]["captured_at"], payload["snapshot"]["captured_at"])
                self.assertEqual(service.saved_list()["items"][0]["source"], "preflight")


if __name__ == "__main__":
    unittest.main(verbosity=2)
