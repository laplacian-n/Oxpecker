"""A provider 402 (out of credits) or 429 (rate limit) must PAUSE the run, not crash it.

Live, an OpenRouter 402 mid-run unwound as a generic exception and the run reported "crashed"
— alarming, and it hid that the fix is just "top up and run again". The driver now catches
ProviderQuotaError distinctly: a `run_paused` event with an actionable reason, a "paused" result,
and the graph/phase state left where it is so a restart resumes. This pins that, without a model.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from ..engagement.store import EngagementStore
from ..llm.openrouter import ProviderQuotaError
from .autonomous_driver import AutonomousDriver


class DriverQuotaPauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="driver-quota-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.eid = f"quota-{time.time_ns()}"
        p = patch("agent.pipeline.autonomous_driver.config.ENGAGEMENTS_ROOT", self.tmp)
        p.start(); self.addCleanup(p.stop)
        (self.tmp / self.eid).mkdir(parents=True)
        (self.tmp / self.eid / "roe.json").write_text(
            f'{{"engagement_id": "{self.eid}", "valid_until": "2099-01-01T00:00:00Z"}}')
        EngagementStore(self.tmp / self.eid)  # so get_phase() has a db to read
        self.events = []

    def _driver(self):
        return AutonomousDriver(engagement_id=self.eid, profile_name="web_api", mode="autonomous",
                                session_id="s", on_event=self.events.append, tier="high")

    def _run_raising(self, exc):
        d = self._driver()
        with patch.object(d, "_run_inner", side_effect=exc):
            return d.run()

    def test_402_pauses_with_an_out_of_credits_reason(self):
        res = self._run_raising(ProviderQuotaError("OpenRouter HTTP 402: no credits", 402))
        self.assertEqual(res.status, "paused")
        kinds = [e.get("type") for e in self.events]
        self.assertIn("run_paused", kinds)
        self.assertNotIn("run_error", kinds)   # NOT reported as a crash
        paused = next(e for e in self.events if e.get("type") == "run_paused")
        self.assertEqual(paused["reason"], "out of provider credits")
        self.assertEqual(paused["status"], 402)

    def test_429_pauses_with_a_rate_limit_reason(self):
        res = self._run_raising(ProviderQuotaError("OpenRouter HTTP 429: slow down", 429))
        self.assertEqual(res.status, "paused")
        paused = next(e for e in self.events if e.get("type") == "run_paused")
        self.assertEqual(paused["reason"], "provider rate limit")

    def test_a_real_error_still_crashes_loudly(self):
        # A non-quota failure must NOT be swallowed as a pause — it is a run_error as before.
        res = self._run_raising(RuntimeError("boom in a phase"))
        self.assertEqual(res.status, "error")
        kinds = [e.get("type") for e in self.events]
        self.assertIn("run_error", kinds)
        self.assertNotIn("run_paused", kinds)


if __name__ == "__main__":
    unittest.main()
