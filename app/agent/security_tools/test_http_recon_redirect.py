"""http_recon must keep the port in the Host header on a non-default port, or redirect-following
breaks against any app that builds its redirect from Host (Nextcloud, many frameworks): the root
302s to the login page, the Location comes back with the port dropped, and the next hop hits
:80/:443 and is refused — recon of the app silently fails. Found testing a self-hosted Nextcloud
on :8080. This pins it with a stand-in server that, like Nextcloud, echoes the Host it was given
into the redirect Location.
"""
from __future__ import annotations

import ipaddress
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from ..broker.policy import Policy
from . import http_recon


class _NextcloudishHandler(BaseHTTPRequestHandler):
    """`/` 302s to `http://<Host>/login` — the Location is built from the request's Host header,
    exactly how the real bug surfaced. `/login` is a 200. So the redirect only resolves back to
    this server if the client sent the port in Host."""
    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's required name
        host = self.headers.get("Host", "")
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", f"http://{host}/login")
            self.end_headers()
        elif self.path == "/login":
            body = b"<html><body>login page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence the test server
        pass


def _policy() -> Policy:
    return Policy(
        engagement_id="t", allowed_action_classes={"passive_recon"},
        allow_networks=[ipaddress.ip_network("127.0.0.1/32")], allow_hostnames=set(),
        deny_networks=[], deny_hostnames=set(), policy_version="t", valid_until=time.time() + 3600,
    )


class RedirectPortTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), _NextcloudishHandler)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_redirect_on_a_nonstandard_port_is_followed(self):
        # The port is non-default, so the fix must put it in Host; otherwise the stand-in emits
        # http://127.0.0.1/login (no port), hop 2 hits :80 and the whole recon returns ok=False.
        r = http_recon.run(f"http://127.0.0.1:{self.port}/", _policy())
        self.assertTrue(r.get("ok"), f"recon failed: {r.get('error')}")
        self.assertEqual(r.get("final_status"), 200)
        self.assertTrue(r.get("final_url", "").endswith("/login"))
        # and the redirect Location came back WITH the port (proof the Host carried it)
        self.assertIn(f":{self.port}", r["hops"][0]["redirect_location"])


if __name__ == "__main__":
    unittest.main()
