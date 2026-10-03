"""Tests for M5.5 dispatcher. `target_http` is fully wired (verified via mocking the network-
touching internals rather than the wiring itself). `knowledge_search`/`knowledge_fetch` are now
live (ADR-0007) — most tests here mock `do_search`/`do_fetch` to verify the surrounding
policy/budget/cache/quarantine wiring deterministically and without depending on network
availability for every run, but `TestLiveEgress` makes real calls against the real local SearXNG
container and a real external site, mirroring this project's existing preference for real-over-
mocked wherever feasible (e.g. `agent/security_tools/test_http_recon_https.py`'s real TLS
servers, Layer 4's real Juice Shop/DVWA containers)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ..injection_guard import ScanResult
from .dispatcher import knowledge_fetch, knowledge_search, target_http
from .policy import FetchPolicyError, SearchPolicyError


class TestDispatcherBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="internet-dispatcher-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        budget_patcher = patch("agent.internet.budget.BUDGETS_STATE_DIR", self.tmp / "budgets")
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)
        cache_patcher = patch("agent.internet.cache.CACHE_DIR", self.tmp / "cache")
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)
        taint_patcher = patch("agent.internet.dispatcher.TaintStore")
        self.mock_taint_cls = taint_patcher.start()
        self.addCleanup(taint_patcher.stop)


class TestTargetHttp(TestDispatcherBase):
    def test_calls_real_http_recon_and_scans_result(self):
        fake_result = {"ok": True, "hops": [{"status": 200}]}
        with patch("agent.security_tools.http_recon.run", return_value=fake_result) as mock_run, \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")) as mock_scan:
            result = target_http("session-a", "http://127.0.0.1:3000/", policy=object())
        mock_run.assert_called_once()
        mock_scan.assert_called_once()
        self.assertTrue(result.ok)
        self.assertEqual(result.channel, "target_http")
        self.assertEqual(result.scan_verdict, "clean")

    def test_malicious_content_taints_session(self):
        fake_result = {"ok": True, "hops": []}
        with patch("agent.security_tools.http_recon.run", return_value=fake_result), \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=True, verdict="malicious")):
            target_http("session-b", "http://127.0.0.1:3000/", policy=object())
        self.mock_taint_cls.return_value.mark.assert_called_once()

    def test_clean_content_does_not_taint(self):
        fake_result = {"ok": True, "hops": []}
        with patch("agent.security_tools.http_recon.run", return_value=fake_result), \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")):
            target_http("session-c", "http://127.0.0.1:3000/", policy=object())
        self.mock_taint_cls.return_value.mark.assert_not_called()


class TestKnowledgeSearchWired(TestDispatcherBase):
    def test_unlisted_provider_still_denied_by_policy(self):
        with self.assertRaises(SearchPolicyError):
            knowledge_search("session-a", "site:example.test admin panel", provider="acme")

    def test_default_provider_calls_do_search_and_caches(self):
        fake_results = [{"title": "t", "url": "http://example.test/", "content": "c"}]
        with patch("agent.internet.search.do_search", return_value=fake_results) as mock_search, \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")):
            result = knowledge_search("session-a", "OWASP top 10")
        mock_search.assert_called_once_with("OWASP top 10")
        self.assertTrue(result.ok)
        self.assertEqual(result.provenance["result_count"], 1)

        from .cache import InternetCache

        cached = InternetCache().get("knowledge_search", "OWASP top 10")
        self.assertIsNotNone(cached)

    def test_malicious_result_content_taints_session(self):
        with patch("agent.internet.search.do_search", return_value=[]), \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=True, verdict="malicious")):
            knowledge_search("session-b", "some query")
        self.mock_taint_cls.return_value.mark.assert_called_once()

    def test_budget_exhaustion_stops_further_calls(self):
        from .budget import BudgetExceededError

        with patch("agent.internet.search.do_search", return_value=[]), \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")):
            for _ in range(20):
                knowledge_search("session-c", "query")
            with self.assertRaises(BudgetExceededError):
                knowledge_search("session-c", "one more")


class TestKnowledgeFetchWired(TestDispatcherBase):
    def test_ssrf_policy_still_enforced(self):
        with self.assertRaises(FetchPolicyError):
            knowledge_fetch("session-a", "http://169.254.169.254/latest/meta-data/")

    def test_allowed_url_calls_do_fetch_with_validated_ip(self):
        fake_result = {"ok": True, "status": 200, "body_excerpt": "hello", "body_truncated": False}
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             patch("agent.internet.fetch.do_fetch", return_value=fake_result) as mock_fetch, \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")):
            result = knowledge_fetch("session-a", "http://example.test/")
        mock_fetch.assert_called_once_with("http://example.test/", validated_ip="93.184.216.34")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hello")

    def test_fetch_result_cached(self):
        fake_result = {"ok": True, "status": 200, "body_excerpt": "hello"}
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), \
             patch("agent.internet.fetch.do_fetch", return_value=fake_result), \
             patch("agent.internet.dispatcher.injection_guard.scan", return_value=ScanResult(matched=False, verdict="clean")):
            knowledge_fetch("session-b", "http://example.test/")

        from .cache import InternetCache

        cached = InternetCache().get("knowledge_fetch", "http://example.test/")
        self.assertEqual(cached["content"], "hello")


class TestLiveEgress(unittest.TestCase):
    """Real network calls — the actual point of this milestone. Skipped gracefully if the local
    SearXNG container isn't reachable (e.g. running this suite on a machine that hasn't stood one
    up), rather than failing the whole suite on an environment difference."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="internet-live-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        budget_patcher = patch("agent.internet.budget.BUDGETS_STATE_DIR", self.tmp / "budgets")
        budget_patcher.start()
        self.addCleanup(budget_patcher.stop)
        cache_patcher = patch("agent.internet.cache.CACHE_DIR", self.tmp / "cache")
        cache_patcher.start()
        self.addCleanup(cache_patcher.stop)

    def test_real_searxng_query(self):
        import urllib.error
        import urllib.request

        try:
            urllib.request.urlopen("http://127.0.0.1:8888/search?q=test&format=json", timeout=3)
        except (urllib.error.URLError, ConnectionRefusedError):
            self.skipTest("local SearXNG container not reachable at 127.0.0.1:8888")

        result = knowledge_search("live-test-session", "OWASP top 10 web application security")
        self.assertTrue(result.ok)
        self.assertGreater(result.provenance["result_count"], 0)
        self.assertIn("owasp", result.content.lower())

    def test_real_external_fetch(self):
        try:
            result = knowledge_fetch("live-test-session", "https://example.com/")
        except Exception as e:
            self.skipTest(f"external network not reachable: {e}")
            return
        self.assertTrue(result.ok)
        self.assertEqual(result.provenance["status"], 200)
        self.assertIn("example", result.content.lower())


if __name__ == "__main__":
    unittest.main()
