import http.client
import importlib.util
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workbench', ROOT / 'workbench.py')
workbench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workbench)


class ServerBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = workbench.Server(('127.0.0.1', 0), workbench.Handler)
        cls.server.identity = {'app': 'local-workbench', 'port': cls.server.server_port}
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method='GET', path='/', body=None, headers=None):
        con = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        con.request(method, path, body, headers or {})
        response = con.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        con.close()
        return result

    def test_home_is_real_html(self):
        status, headers, content = self.request()
        self.assertEqual(status, 200)
        self.assertIn('工作工具箱'.encode(), content)
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_health(self):
        status, _, content = self.request(path='/api/system/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)['app'], 'local-workbench')

    def test_wrong_host_rejected(self):
        self.assertEqual(self.request(headers={'Host': 'example.com'})[0], 403)

    def test_foreign_origin_rejected(self):
        self.assertEqual(self.request(headers={'Origin': 'https://example.com'})[0], 403)

    def test_cross_site_fetch_rejected(self):
        self.assertEqual(self.request(headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)

    def test_post_requires_intent_header(self):
        self.assertEqual(self.request('POST', '/api/history/index', '{}', {'Content-Type': 'application/json'})[0], 403)

    def test_bad_json(self):
        headers = {'Content-Type': 'application/json', 'X-Workbench-Request': '1'}
        for value in ['{', '[]', '{"x":NaN}', '{"x":'+'['*1200+'0'+']'*1200+'}']:
            self.assertEqual(self.request('POST', '/api/absent/status', value, headers)[0], 400)

    def test_oversized_body_rejected(self):
        headers = {'Content-Type': 'application/json', 'X-Workbench-Request': '1', 'Content-Length': str(workbench.MAX_BODY + 1)}
        self.assertEqual(self.request('POST', '/api/history/index', '', headers)[0], 413)

    def test_content_type_rejected(self):
        self.assertEqual(self.request('POST', '/api/history/index', '{}', {'X-Workbench-Request': '1', 'Content-Type': 'text/plain'})[0], 415)

    def test_private_and_source_files_not_served(self):
        for path in ['/data/server.json', '/workbench.py', '/apps/runtime/service.py', '/evidence/cloud-history-sample.json', '/apps/history/../../web/index.html']:
            with self.subTest(path=path):
                self.assertIn(self.request(path=path)[0], [403, 404])

    def test_unknown_module(self):
        self.assertEqual(self.request(path='/api/absent/status')[0], 404)

    def test_head_has_no_body(self):
        status, headers, body = self.request('HEAD')
        self.assertEqual(status, 200)
        self.assertGreater(int(headers['Content-Length']), 0)
        self.assertEqual(body, b'')

    def test_stop_does_not_claim_success_for_live_pid(self):
        cmd = f'python3 {ROOT / "workbench.py"} serve --port 8899'
        with patch.object(workbench, 'live_state', return_value={'pid': 999999}), patch.object(workbench.subprocess, 'run', return_value=Mock(returncode=0, stdout=cmd)), patch.object(workbench.os, 'kill') as kill, patch.object(workbench.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                workbench.stop()
            kill.assert_called_once()

    def test_stop_refuses_foreign_process(self):
        with patch.object(workbench, 'live_state', return_value={'pid': 999999}), patch.object(workbench.subprocess, 'run', return_value=Mock(returncode=0, stdout='python3 unrelated.py')), patch.object(workbench.os, 'kill') as kill:
            with self.assertRaises(RuntimeError):
                workbench.stop()
            kill.assert_not_called()

    def test_saved_state_non_object_is_invalid(self):
        with patch.object(workbench, 'STATE') as state:
            state.read_text.return_value = '[]'
            self.assertIsNone(workbench.saved_state())


if __name__ == '__main__':
    unittest.main()
