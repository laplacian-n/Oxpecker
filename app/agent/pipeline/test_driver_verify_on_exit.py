"""A run stopped before REPORT still verifies what it found.

The finding gate ran only at the VALIDATION->REPORT boundary, so the realistic pattern — start a
run, let it confirm real weaknesses, stop it to save budget mid-ANALYSIS — left those findings
`confirmed_by: model`, never independently verified. The driver now promotes + verifies on exit
(operator stop, wall-clock) too, both idempotent. Pins that the exit paths verify, that it only
does so for the graph tier, and that a flat run is untouched.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ..engagement.store import EngagementStore
from .autonomous_driver import AutonomousDriver


class VerifyOnExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="verify-exit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eid = f"ve-{time.time_ns()}"
        p = patch("agent.pipeline.autonomous_driver.config.ENGAGEMENTS_ROOT", self.tmp)
        p.start(); self.addCleanup(p.stop)
        (self.tmp / self.eid).mkdir(parents=True)
        (self.tmp / self.eid / "roe.json").write_text(
            f'{{"engagement_id": "{self.eid}", "valid_until": "2099-01-01T00:00:00Z"}}')
        EngagementStore(self.tmp / self.eid)

    def _driver(self, tier="high"):
        return AutonomousDriver(engagement_id=self.eid, profile_name="web_api", mode="autonomous",
                                session_id="s", on_event=lambda e: None, tier=tier)

    def test_helper_promotes_and_verifies_for_a_graph_run(self):
        d = self._driver(tier="high")
        with patch.object(d, "store") as store, \
             patch.object(d, "_promote_findings") as promote, \
             patch.object(d, "_run_finding_gate") as gate:
            store.get_phase.return_value = {"current_phase": "ANALYSIS"}
            d._verify_findings_so_far()
        promote.assert_called_once()
        gate.assert_called_once()

    def test_helper_is_a_noop_for_a_flat_run(self):
        d = self._driver(tier="medium")  # medium = flat engine, no graph/gate step
        with patch.object(d, "store") as store, \
             patch.object(d, "_promote_findings") as promote, \
             patch.object(d, "_run_finding_gate") as gate:
            store.get_phase.return_value = {"current_phase": "ANALYSIS"}
            d._verify_findings_so_far()
        promote.assert_not_called()
        gate.assert_not_called()

    def test_operator_stop_verifies_before_returning(self):
        d = self._driver(tier="high")
        d.stop_event.set()  # stop is seen at the top of the run loop, before any phase work
        with patch.object(d, "_bootstrap_engagement"), \
             patch.object(d, "_verify_findings_so_far") as verify:
            result = d._run_inner()
        verify.assert_called_once()
        self.assertEqual(result.status, "stopped_by_operator")


if __name__ == "__main__":
    unittest.main()
