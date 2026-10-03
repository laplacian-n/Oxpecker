"""Tests for M5.5 osint_discovery — a recording-only channel with no outbound network call."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..engagement.store import EngagementStore
from .osint import record_out_of_scope


class TestOsintDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="osint-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = EngagementStore(self.tmp / "eng-1")

    def test_records_as_observed_out_of_scope(self):
        record_out_of_scope(
            self.store, "subdomain", "beta.otherapp.example.test", source="page_content_scan"
        )
        observations = self.store.list_observations()
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0]["observation_type"].startswith("observed_out_of_scope:"))
        self.assertEqual(observations[0]["content"], "beta.otherapp.example.test")

    def test_module_exposes_no_fetch_function(self):
        import agent.internet.osint as osint_module

        public_names = [n for n in dir(osint_module) if not n.startswith("_")]
        for name in public_names:
            self.assertNotIn("fetch", name.lower())
            self.assertNotIn("request", name.lower())
            self.assertNotIn("probe", name.lower())


if __name__ == "__main__":
    unittest.main()
