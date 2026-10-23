"""http_request sends arbitrary methods + bodies through the same scope/pin spine as http_recon —
the active verb a worker needs for POST-based exploitation (the Juice Shop login SQLi it couldn't
reach with GET-only http_recon). Pins: the method and body actually arrive, a non-2xx is a result
(ok=True) not a failure, and the scope/path/method gates refuse before anything is sent.
"""
from __future__ import annotations

import ipaddress
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..broker.policy import Policy
from . import http_request


class _EchoHandler(BaseHTTPRequestHandler):
    """Echoes the method and body back as JSON; GET /boom returns a 500 (a SQLi-style error)."""
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path == "/boom":
            self.send_response(500); self.end_headers(); self.wfile.write(b"SQLITE_ERROR"); return
        payload = json.dumps({"method": self.command, "body": body,
                              "ctype": self.headers.get("Content-Type")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *a):
        pass


def _policy(allow_paths=(), deny_paths=()) -> Policy:
    return Policy(
        engagement_id="t", allowed_action_classes={"active_web_request"},
        allow_networks=[ipaddress.ip_network("127.0.0.1/32")], allow_hostnames=set(),
        deny_networks=[], deny_hostnames=set(), policy_version="t", valid_until=time.time() + 3600,
        allow_paths=tuple(allow_paths), deny_paths=tuple(deny_paths),
    )


class HttpRequestTest(unittest.TestCase):
    def setUp(self):
        self.srv = HTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.port = self.srv.server_address[1]
        t = threading.Thread(target=self.srv.serve_forever, daemon=True); t.start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def _url(self, path="/"):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_post_with_a_body_actually_sends_both(self):
        r = http_request.run(self._url("/login"), _policy(), method="POST",
                             headers={"Content-Type": "application/json"},
                             body='{"email":"a\' OR 1=1--","password":"x"}')
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], 200)
        echoed = json.loads(r["body_excerpt"])
        self.assertEqual(echoed["method"], "POST")
        self.assertIn("OR 1=1", echoed["body"])          # the injection body reached the server
        self.assertEqual(echoed["ctype"], "application/json")  # the custom header reached it

    def test_a_500_is_a_result_not_a_failure(self):
        # A server error is exactly the signal error-based SQLi looks for — it must come back ok.
        r = http_request.run(self._url("/boom"), _policy())
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], 500)
        self.assertIn("SQLITE_ERROR", r["body_excerpt"])

    def test_out_of_scope_host_is_refused_before_sending(self):
        r = http_request.run("http://169.254.169.254/latest/meta-data/", _policy(), method="POST",
                             body="x")
        self.assertFalse(r["ok"])
        self.assertIn("not in scope", r["error"])

    def test_path_scope_is_enforced(self):
        r = http_request.run(self._url("/admin"), _policy(deny_paths=["/admin"]), method="DELETE")
        self.assertFalse(r["ok"])
        self.assertIn("path not in scope", r["error"])

    def test_a_disallowed_method_is_rejected(self):
        r = http_request.run(self._url("/"), _policy(), method="TRACE")
        self.assertFalse(r["ok"])
        self.assertIn("not allowed", r["error"])

    def test_reserved_headers_cannot_be_overridden(self):
        # A model-supplied Host must not defeat the scope pin; it is dropped.
        r = http_request.run(self._url("/"), _policy(), method="GET",
                             headers={"Host": "evil.example.com", "X-Test": "ok"})
        self.assertTrue(r["ok"])  # still reaches the pinned server, Host override ignored


if __name__ == "__main__":
    unittest.main()
