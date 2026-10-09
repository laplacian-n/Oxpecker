"""Budget recording through the atomic reserve/settle interface (§14.1 D / §7.2 / owner C12). A
model turn reserves a spend slot before the call and settles the provider's actual cost after,
emitting budget_updated. A crossed cap stops the next call (stop, not degrade) before the model
is hit. Default-unmetered means no behaviour change — proven by the happy-path test running at
all with no cap set.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import AgentLoop
from agent.engagement.event_log import EngagementEventLog
from agent.engagement.spend import SpendLedger


class LoopBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-budget-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "workspace").mkdir()
        for attr, val in (
            ("ENGAGEMENTS_ROOT", self.tmp / "engagements"),
            ("SESSIONS_DIR", self.tmp / "sessions"),
            ("AUDIT_DIR", self.tmp / "audit"),
        ):
            p = patch(f"agent.config.{attr}", val)
            p.start()
            self.addCleanup(p.stop)

    def _loop(self, engagement_id):
        loop = AgentLoop(workspace_root=self.tmp / "workspace", session_id=f"s-{engagement_id}",
                         engagement_id=engagement_id)
        self.addCleanup(loop.close)
        patch.object(loop.client, "n_ctx", return_value=16384).start()
        patch.object(loop.client, "token_count", return_value=10).start()
        patch.object(loop.client, "rendered_token_count", return_value=100).start()
        return loop

    def _events(self, engagement_id):
        log = EngagementEventLog.open_if_exists(self.tmp / "engagements" / engagement_id)
        return log.read_since(0) if log else []

    def test_a_model_turn_settles_the_actual_cost_and_emits_budget_updated(self):
        loop = self._loop("eng-cost")
        patch.object(loop.client, "chat_completions", return_value={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 14, "completion_tokens": 2, "cost": 0.0123},
            "model": "openai/gpt-4o-mini",
        }).start()

        loop.run_task("probe")

        budget_events = [e for e in self._events("eng-cost") if e["kind"] == "budget_updated"]
        self.assertTrue(budget_events, "no budget_updated was emitted")
        self.assertAlmostEqual(budget_events[-1]["payload"]["spent"], 0.0123)
        # and the ledger on disk reflects it (reserve+settle, not lost).
        self.assertAlmostEqual(SpendLedger(self.tmp / "engagements" / "eng-cost").snapshot()["spent"], 0.0123)
        # model_call carries the cost too.
        mc = next(e for e in self._events("eng-cost") if e["kind"] == "model_call")
        self.assertAlmostEqual(mc["payload"]["cost"], 0.0123)

    def test_a_crossed_cap_stops_the_next_call_before_the_model(self):
        # Pre-spend past a small cap, so the loop's reserve-before refuses and the turn stops
        # without ever calling the model (§7.2 stop, not degrade).
        ledger = SpendLedger(self.tmp / "engagements" / "eng-cap")
        ledger.set_budget(1.0)
        rid = ledger.reserve(0.02)
        ledger.settle(rid, 0.02)
        ledger.set_budget(0.01)  # lower the cap below what is already committed (0.02 > 0.01)

        loop = self._loop("eng-cap")
        called = {"n": 0}
        patch.object(loop.client, "chat_completions",
                     side_effect=lambda *a, **k: called.__setitem__("n", called["n"] + 1)).start()

        result = loop.run_task("probe")
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(called["n"], 0)  # the model was never called


if __name__ == "__main__":
    unittest.main()
