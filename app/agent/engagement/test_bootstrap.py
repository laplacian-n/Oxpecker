"""bootstrap_engagement() — shared by the autonomous driver and AgentLoop to make an engagement
usable from a bare id (state.db, lab-default RoE import, host assets seeded from scope)."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from .bootstrap import bootstrap_engagement
from .store import EngagementStore

_ROE = {
    "engagement_id": "x", "client": "c", "authorized_by": "a",
    "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2999-01-01T00:00:00Z",
    "allowed_action_classes": ["recon"],
}


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bootstrap-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._root = patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements")
        self._root.start()
        self.addCleanup(self._root.stop)
        self._legacy = patch("agent.config.ENGAGEMENT_DIR", self.tmp / "legacy")
        self._legacy.start()
        self.addCleanup(self._legacy.stop)

    def _write_roe(self, into: Path, scope: str = "example.test\n") -> None:
        into.mkdir(parents=True, exist_ok=True)
        (into / "roe.json").write_text(json.dumps(_ROE))
        (into / "scope.txt").write_text(scope)
        (into / "deny.txt").write_text("")

    def test_creates_state_db_and_seeds_hostnames_from_scope(self):
        eng = self.tmp / "engagements" / "acme"
        self._write_roe(eng, "acme.example\nwww.acme.example\n")
        events = []
        store = bootstrap_engagement("acme", on_event=events.append)

        self.assertTrue((eng / "state.db").exists())
        self.assertEqual(
            sorted(a["identifier"] for a in store.list_assets()),
            ["acme.example", "www.acme.example"],
        )
        self.assertTrue(any("seeded 2 host asset" in e.get("detail", "") for e in events))

    def test_lab_default_imports_the_legacy_roe(self):
        self._write_roe(self.tmp / "legacy", "legacy.example\n")
        eng = self.tmp / "engagements" / "lab-default"
        self.assertFalse((eng / "roe.json").exists())

        store = bootstrap_engagement("lab-default")

        for f in ("roe.json", "scope.txt", "deny.txt"):
            self.assertTrue((eng / f).exists(), f)
        self.assertEqual([a["identifier"] for a in store.list_assets()], ["legacy.example"])

    def test_idempotent_and_does_not_reseed_when_assets_exist(self):
        eng = self.tmp / "engagements" / "acme"
        self._write_roe(eng, "acme.example\n")
        store = EngagementStore(eng)
        store.upsert_asset("host", "10.0.0.5")

        bootstrap_engagement("acme", store=store)

        self.assertEqual([a["identifier"] for a in store.list_assets()], ["10.0.0.5"])

    def test_no_policy_is_not_an_error(self):
        store = bootstrap_engagement("bare")  # no roe/scope/deny anywhere
        self.assertTrue((self.tmp / "engagements" / "bare" / "state.db").exists())
        self.assertEqual(store.list_assets(), [])


if __name__ == "__main__":
    unittest.main()
