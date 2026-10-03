"""Tests for the Phase 6 self-hosted OOB catcher — real HTTP requests against a real (loopback)
listener, not mocked, since the whole point of this module is what a real network interaction
does and doesn't prove."""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from .server import OOBServer


class TestOOBServer(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oob-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.server = OOBServer(interactions_dir=self.tmp)
        self.server.start()
        self.addCleanup(self.server.stop)

    def test_binds_loopback_only(self):
        # server_address host as bound
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")

    def test_issue_token_returns_working_url(self):
        token, url = self.server.issue_token()
        self.assertIn(token, url)
        self.assertTrue(url.startswith("http://127.0.0.1:"))

    def test_real_request_to_callback_url_is_recorded(self):
        token, url = self.server.issue_token()
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertTrue(self.server.is_valid_interaction(token))
        interactions = self.server.get_interactions(token)
        self.assertEqual(len(interactions), 1)
        self.assertEqual(interactions[0]["source_addr"], "127.0.0.1")

    def test_unknown_token_gets_404_and_is_not_recorded(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.port}/oob/not-a-real-token")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
        self.assertFalse(self.server.is_valid_interaction("not-a-real-token"))

    def test_validation_ignores_spoofed_forwarded_for_header(self):
        """The whole point of correlation-token validation: a request claiming to be from an
        arbitrary source (via a spoofable header) is validated purely by knowing the token, and
        the spoofed header changes nothing about that decision."""
        token, url = self.server.issue_token()
        req = urllib.request.Request(url, headers={"X-Forwarded-For": "8.8.8.8"})
        urllib.request.urlopen(req, timeout=5)
        self.assertTrue(self.server.is_valid_interaction(token))
        interaction = self.server.get_interactions(token)[0]
        # the header is recorded as metadata...
        self.assertEqual(interaction["headers"].get("X-Forwarded-For"), "8.8.8.8")
        # ...but source_addr (what's actually trustworthy) is the real loopback connection,
        # and is_valid_interaction() never even looks at either field.
        self.assertEqual(interaction["source_addr"], "127.0.0.1")

    def test_different_tokens_dont_cross_contaminate(self):
        token_a, url_a = self.server.issue_token()
        token_b, _url_b = self.server.issue_token()
        urllib.request.urlopen(url_a, timeout=5)
        self.assertTrue(self.server.is_valid_interaction(token_a))
        self.assertFalse(self.server.is_valid_interaction(token_b))

    def test_wait_for_interaction_returns_after_real_request(self):
        token, url = self.server.issue_token()

        def fire_later():
            time.sleep(0.2)
            urllib.request.urlopen(url, timeout=5)

        threading.Thread(target=fire_later).start()
        interaction = self.server.wait_for_interaction(token, timeout_s=3, poll_interval_s=0.05)
        self.assertIsNotNone(interaction)

    def test_wait_for_interaction_times_out_with_no_request(self):
        token, _url = self.server.issue_token()
        interaction = self.server.wait_for_interaction(token, timeout_s=0.3, poll_interval_s=0.05)
        self.assertIsNone(interaction)


if __name__ == "__main__":
    unittest.main()
