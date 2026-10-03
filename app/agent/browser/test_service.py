"""Tests for the Phase 6 isolated browser service — real Playwright/Chromium launches against
real local HTTP servers (not mocked), since the entire point of this module is what actually
crosses the process boundary onto the network, and a mocked test couldn't show that."""
from __future__ import annotations

import ipaddress
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..broker.policy import Policy
from .service import browser_session


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/with-remote-image":
            body = (
                b"<html><body><h1>in-scope page</h1>"
                b'<img src="http://127.0.0.2:%d/tracker.png"></body></html>'
                % self.server.other_port  # type: ignore[attr-defined]
            )
        else:
            body = b"<html><body><h1>hello from in-scope target</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _start_server(host: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _policy_allowing_only(host: str, port: int) -> Policy:
    return Policy(
        engagement_id="test-eng",
        allowed_action_classes={"passive_recon"},
        allow_networks=[ipaddress.ip_network(f"{host}/32")],
        allow_hostnames=set(),
        deny_networks=[],
        deny_hostnames=set(),
        policy_version="v1",
        valid_until=time.time() + 3600,
    )


class TestBrowserService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.in_scope = _start_server("127.0.0.1")
        cls.out_of_scope = _start_server("127.0.0.2")
        cls.in_scope.other_port = cls.out_of_scope.server_address[1]  # type: ignore[attr-defined]

    @classmethod
    def tearDownClass(cls):
        cls.in_scope.shutdown()
        cls.out_of_scope.shutdown()

    def test_in_scope_navigation_succeeds(self):
        policy = _policy_allowing_only("127.0.0.1", self.in_scope.server_address[1])
        with browser_session(policy) as (page, denied):
            page.goto(f"http://127.0.0.1:{self.in_scope.server_address[1]}/")
            self.assertIn("hello from in-scope target", page.content())
            self.assertEqual(denied, [])

    def test_out_of_scope_navigation_is_blocked(self):
        policy = _policy_allowing_only("127.0.0.1", self.in_scope.server_address[1])
        with browser_session(policy) as (page, denied):
            try:
                page.goto(
                    f"http://127.0.0.2:{self.out_of_scope.server_address[1]}/", timeout=5000
                )
            except Exception:
                pass  # an aborted navigation raising is expected; the real assertion is below
            self.assertTrue(any(d["url"].startswith("http://127.0.0.2") for d in denied))

    def test_out_of_scope_subresource_is_blocked_while_page_still_loads(self):
        """The real target-only-egress claim: a page fetched from the in-scope target that
        itself references an out-of-scope resource must not have that resource fetched."""
        policy = _policy_allowing_only("127.0.0.1", self.in_scope.server_address[1])
        with browser_session(policy) as (page, denied):
            page.goto(f"http://127.0.0.1:{self.in_scope.server_address[1]}/with-remote-image")
            self.assertIn("in-scope page", page.content())
            self.assertTrue(any("tracker.png" in d["url"] for d in denied))

    def test_ephemeral_context_does_not_leak_cookies_across_sessions(self):
        port = self.in_scope.server_address[1]
        policy = _policy_allowing_only("127.0.0.1", port)
        with browser_session(policy) as (page, _denied):
            page.goto(f"http://127.0.0.1:{port}/")
            page.context.add_cookies(
                [{"name": "session", "value": "leaked-if-shared", "url": f"http://127.0.0.1:{port}/"}]
            )
            self.assertEqual(len(page.context.cookies()), 1)

        with browser_session(policy) as (page2, _denied2):
            page2.goto(f"http://127.0.0.1:{port}/")
            self.assertEqual(page2.context.cookies(), [])  # fresh context, nothing carried over


if __name__ == "__main__":
    unittest.main()
