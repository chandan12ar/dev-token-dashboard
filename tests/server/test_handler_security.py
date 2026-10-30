"""The loopback-only guard on the HTTP handler (DNS-rebinding / cross-origin defence)."""

import http.client
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from ai_fluency_insights.core.scanner import Scanner
from ai_fluency_insights.server.handler import Handler, _hostname


class HostnameParsingTests(unittest.TestCase):
    def test_strips_port(self):
        self.assertEqual(_hostname("127.0.0.1:8787"), "127.0.0.1")
        self.assertEqual(_hostname("localhost:9000"), "localhost")

    def test_handles_origin_url_and_ipv6(self):
        self.assertEqual(_hostname("http://localhost:8787"), "localhost")
        self.assertEqual(_hostname("[::1]:8787"), "::1")

    def test_empty(self):
        self.assertEqual(_hostname(""), "")
        self.assertEqual(_hostname(None), "")


class LoopbackGuardTests(unittest.TestCase):
    """A live server on an ephemeral port, driven with raw Host/Origin headers."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(cls.tmp, "proj"), exist_ok=True)
        Handler.scanner = Scanner(cls.tmp)
        Handler.root = cls.tmp
        Handler.watcher = None
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _get(self, path, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status

    def test_loopback_host_is_allowed(self):
        self.assertEqual(self._get("/api/stats?days=7", {"Host": f"127.0.0.1:{self.port}"}), 200)
        self.assertEqual(self._get("/api/stats?days=7", {"Host": f"localhost:{self.port}"}), 200)

    def test_foreign_host_is_rejected(self):
        # DNS-rebinding: attacker domain repointed at 127.0.0.1 still sends its own Host.
        self.assertEqual(self._get("/api/stats?days=7", {"Host": "evil.example.com"}), 403)

    def test_cross_origin_fetch_is_rejected(self):
        status = self._get("/api/stats?days=7",
                           {"Host": f"127.0.0.1:{self.port}", "Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)

    def test_same_origin_fetch_is_allowed(self):
        status = self._get("/api/stats?days=7",
                           {"Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_guard_also_covers_the_token_spending_routes(self):
        # The whole point of F5: no foreign page may reach /api/reflect or /api/ai_summary.
        self.assertEqual(self._get("/api/reflect?week=x&q=hi", {"Host": "evil.example.com"}), 403)


if __name__ == "__main__":
    unittest.main()
