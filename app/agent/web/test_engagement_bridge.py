"""The engagement-representation bridge: a UI-created engagement is also materialised as the
engine's roe.json/scope.txt/deny.txt, so the autonomous pipeline (which reads an EngagementStore
from config.ENGAGEMENTS_ROOT/<id>) can run an engagement dev_server created.

This is the foundation of wiring the autonomous pipeline into dev_server. The files land in the
same directory the UI's graph/notebook stores use (main() aligns ENGAGEMENTS_ROOT to DATA_DIR), so
the agent and the UI share one graph.

Run directly: `python3 -m unittest agent.web.test_engagement_bridge`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class EngagementBridgeTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="engagement-bridge-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions", "_engagements")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            # The bridge writes to config.ENGAGEMENTS_ROOT; main() aligns it to DATA_DIR at launch,
            # so point both at the one tmp dir here to mirror that.
            patch.object(config, "ENGAGEMENTS_ROOT", self.tmp),
            patch.object(dev_server._config, "ENGAGEMENTS_ROOT", self.tmp),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def tearDown(self):
        for n, saved in self._saved.items():
            s = getattr(dev_server, n)
            s.clear()
            s.update(saved)

    def _create(self, **over):
        body = {"engagement_id": "eng-bridge", "description": "test",
                "allow_targets": ["127.0.0.1"], "deny_targets": [],
                "allowed_action_classes": ["passive_recon"], "authorized_by": "operator",
                "valid_hours": 24}
        body.update(over)
        return self.client.post("/api/engagements", json=body)

    def test_creating_an_engagement_writes_the_engine_files(self):
        r = self._create()
        self.assertEqual(r.status_code, 200, r.text)
        d = self.tmp / "eng-bridge"
        self.assertTrue((d / "roe.json").exists(), "roe.json should be materialised")
        self.assertTrue((d / "scope.txt").exists())
        self.assertTrue((d / "deny.txt").exists())
        roe = json.loads((d / "roe.json").read_text())
        self.assertEqual(roe["engagement_id"], "eng-bridge")
        self.assertIn("passive_recon", roe["allowed_action_classes"])
        self.assertIn("127.0.0.1", (d / "scope.txt").read_text())

    def test_the_engine_can_open_the_materialised_engagement(self):
        self._create()
        # The EngagementStore the autonomous driver uses must open the dir without error.
        from ..engagement.store import EngagementStore
        store = EngagementStore(self.tmp / "eng-bridge")
        self.assertIsNotNone(store)

    def test_cloud_metadata_is_denied_in_the_materialised_deny_list(self):
        self._create()
        self.assertIn("169.254.169.254", (self.tmp / "eng-bridge" / "deny.txt").read_text())

    def test_bridge_is_best_effort_incomplete_fields_do_not_break_creation(self):
        # No action classes: the engine's intake rejects it, but UI creation must still succeed and
        # simply leave no engine files (autonomous run unavailable until completed).
        r = self._create(engagement_id="eng-incomplete", allowed_action_classes=[])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((self.tmp / "eng-incomplete" / "roe.json").exists())

    def test_bridge_is_idempotent(self):
        self._create()
        eng = dev_server._engagements["eng-bridge"]
        # A second materialise call must not raise on the existing dir.
        self.assertTrue(dev_server._materialize_engine_engagement(eng))


if __name__ == "__main__":
    unittest.main()
