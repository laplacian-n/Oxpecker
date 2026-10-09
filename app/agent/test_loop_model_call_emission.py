"""A1: the agent loop emits a §4.2 model_call per model turn, feeding the flow view's model
roster (§6.3). Break-it-first: run one turn with a stubbed client and assert the event, with its
model id and token usage, landed on the engagement log.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import AgentLoop
from agent.engagement.event_log import EngagementEventLog


class LoopModelCallEmissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-model-call-"))
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

    def test_a_model_turn_emits_model_call_with_usage(self):
        loop = AgentLoop(
            workspace_root=self.tmp / "workspace",
            session_id="s-mc",
            engagement_id="eng-mc",
        )
        self.addCleanup(loop.close)

        # Stub the client so one turn completes with no tool calls, carrying a model id + usage.
        patch.object(loop.client, "n_ctx", return_value=16384).start()
        patch.object(loop.client, "token_count", return_value=10).start()
        patch.object(loop.client, "rendered_token_count", return_value=100).start()
        patch.object(
            loop.client, "chat_completions",
            return_value={
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
                "model": "qwen-test",
            },
        ).start()

        loop.run_task("have a look")

        log = EngagementEventLog.open_if_exists(self.tmp / "engagements" / "eng-mc")
        self.assertIsNotNone(log, "no engagement event log was created")
        model_calls = [e for e in log.read_since(0) if e["kind"] == "model_call"]
        self.assertTrue(model_calls, "no model_call event was emitted for a model turn")
        p = model_calls[0]["payload"]
        self.assertEqual(p["model_id"], "qwen-test")
        self.assertEqual(p["prompt_tokens"], 12)
        self.assertEqual(p["completion_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
