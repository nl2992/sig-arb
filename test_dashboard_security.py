import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from dashboard import RELAY_ORIGIN, RELAY_PATH, Source, handler

TOKEN = 'test-token'
SNAPSHOT = json.dumps({
    'ts': '2026-09-30T00:00:00Z',
    'markets': [{'id': 1, 'title': 'Will the Republican Party win the Test Senate?'}],
    'levels': {'1': [{'price': 0.4, 'quantity': 10, 'side': 'BUY', 'isYes': True}]},
})


class DashboardSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        source = Source(replay='fixtures/sample_snapshot.json',
                        browser_snapshot_path=f'{self.tmp.name}/browser_snapshot.json')
        self.calls = []

        def action(h):
            self.calls.append(h.path)
            h.send_body(200, b'{"ok": true}', 'application/json')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(source, TOKEN, {'/api/test_action': action}))
        self.port = self.server.server_address[1]
        self.local = f'http://127.0.0.1:{self.port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_page_carries_action_token_and_no_wildcard_cors(self):
        resp, body = self.request('GET', '/')
        self.assertEqual(resp.status, 200)
        self.assertIn(f'<meta name="action-token" content="{TOKEN}">'.encode(), body)
        self.assertIsNone(resp.getheader('Access-Control-Allow-Origin'))
        self.assertEqual(resp.getheader('X-Frame-Options'), 'DENY')

    def test_foreign_host_is_refused(self):
        resp, _ = self.request('GET', '/', {'Host': 'attacker.example'})
        self.assertEqual(resp.status, 403)
        resp, _ = self.request('POST', '/api/test_action', {'Host': 'attacker.example', 'Origin': 'http://attacker.example',
                                                           'X-Action-Token': TOKEN})
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.calls, [])

    def test_action_requires_local_origin_and_token(self):
        cases = [
            ({}, 403),
            ({'Origin': self.local}, 403),
            ({'Origin': self.local, 'X-Action-Token': 'wrong'}, 403),
            ({'Origin': 'https://evil.example', 'X-Action-Token': TOKEN}, 403),
            ({'Origin': RELAY_ORIGIN, 'X-Action-Token': TOKEN}, 403),
            ({'Origin': self.local, 'X-Action-Token': TOKEN}, 200),
        ]
        for headers, status in cases:
            resp, _ = self.request('POST', '/api/test_action', headers)
            self.assertEqual(resp.status, status, headers)
        self.assertEqual(self.calls, ['/api/test_action'])

    def test_relay_accepts_sig_origin_with_scoped_cors(self):
        resp, _ = self.request('OPTIONS', RELAY_PATH, {'Origin': RELAY_ORIGIN})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader('Access-Control-Allow-Origin'), RELAY_ORIGIN)
        resp, body = self.request('POST', RELAY_PATH, {'Origin': RELAY_ORIGIN, 'Content-Type': 'application/json'}, SNAPSHOT)
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(resp.getheader('Access-Control-Allow-Origin'), RELAY_ORIGIN)

    def test_relay_refuses_other_origins(self):
        resp, _ = self.request('OPTIONS', RELAY_PATH, {'Origin': 'https://evil.example'})
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.getheader('Access-Control-Allow-Origin'))
        resp, _ = self.request('POST', RELAY_PATH, {'Origin': 'https://evil.example'}, SNAPSHOT)
        self.assertEqual(resp.status, 403)

    def test_other_endpoints_get_no_cors_even_for_sig_origin(self):
        resp, _ = self.request('OPTIONS', '/api/portfolio', {'Origin': RELAY_ORIGIN})
        self.assertEqual(resp.status, 403)
        resp, _ = self.request('GET', '/api/portfolio', {'Origin': RELAY_ORIGIN})
        self.assertIsNone(resp.getheader('Access-Control-Allow-Origin'))


if __name__ == '__main__':
    unittest.main()
