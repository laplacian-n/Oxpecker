"""§12 step 3 — the engine's internet / OSINT / reference tools reachable from the dev_server
runtime, closing tool parity (these were the last four in KNOWN_MISSING_FROM_UI).

- security_reference_search reads the same local corpus knowledge_search does (no network).
- osint_record files an incidental out-of-scope observation (local, no network).
- knowledge_fetch (allowed off-target URL) and browser_fetch (in-scope page render) are
  broker-mediated network/browser channels; here we check they are reachable and DEGRADE CLEANLY
  (a `{"ok": False, "error": ...}` tool result, never an exception) when the network or a browser
  binary is absent — the behaviour parity needs, without requiring live infra in CI.

Driven through dev_server's `_run_tool` seam (no model). Run directly:
`python3 -m unittest agent.web.test_internet_tools_ui`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import dev_server


class InternetToolsUiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="internet-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions", "_engagements", "_osint")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [patch.object(dev_server, "DATA_DIR", self.tmp),
                          patch.object(dev_server, "_persist", lambda: None)]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.session = dev_server.Session(session_id="s1", engagement_id="eng1")
        dev_server._sessions["s1"] = self.session
        dev_server._engagements["eng1"] = dev_server.Engagement(
            engagement_id="eng1", allow_targets=["127.0.0.1"])

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _run(self, name, **args):
        return dev_server._run_tool(name, args, self.session)

    def test_all_four_are_in_the_ui_schema(self):
        names = {s["function"]["name"] for s in dev_server.TOOL_SCHEMAS + dev_server.SECURITY_TOOL_SCHEMAS}
        for t in ("security_reference_search", "osint_record", "knowledge_fetch", "browser_fetch"):
            self.assertIn(t, names, t)

    def test_known_missing_is_now_empty_full_parity(self):
        from . import test_engine_ui_tool_parity as p
        self.assertEqual(p.KNOWN_MISSING_FROM_UI, set())

    def test_security_reference_search_reads_the_local_corpus(self):
        res = self._run("security_reference_search", query="sql injection")
        self.assertTrue(res.get("ok"), res)
        self.assertIn("results", res)

    def test_osint_record_files_an_observation(self):
        res = self._run("osint_record", observation="admin.staging.example.com seen in a comment",
                        kind="subdomain", source="/index.html")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(dev_server._osint["eng1"]), 1)
        self.assertEqual(dev_server._osint["eng1"][0]["kind"], "subdomain")

    def test_osint_record_rejects_empty(self):
        self.assertFalse(self._run("osint_record", observation="   ").get("ok"))

    def test_knowledge_fetch_degrades_cleanly_without_a_result(self):
        # No network / not an allowed knowledge URL in this env: must return ok:False, not raise.
        res = self._run("knowledge_fetch", url="http://example.com/advisory")
        self.assertIn("ok", res)
        self.assertIsInstance(res, dict)
        if not res["ok"]:
            self.assertIn("error", res)

    def test_browser_fetch_degrades_cleanly_without_a_browser(self):
        res = self._run("browser_fetch", url="http://127.0.0.1:3000/")
        self.assertIn("ok", res)
        self.assertIsInstance(res, dict)
        if not res["ok"]:
            self.assertIn("error", res)

    def test_knowledge_fetch_and_browser_fetch_need_a_url(self):
        self.assertFalse(self._run("knowledge_fetch").get("ok"))
        self.assertFalse(self._run("browser_fetch").get("ok"))


if __name__ == "__main__":
    unittest.main()
