"""Cooperative stop for a wave worker (§2.5 / A6). An AgentLoop given a stop event returns a
'cancelled' result at the next turn boundary — the mechanism the wave orchestrator's wall-clock
hard-stop drives on a real worker, so what the worker already recorded stays on the trajectory
rather than the thread being force-killed mid-generation.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.loop import AgentLoop


class LoopStopEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="loop-stop-"))
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

    def test_an_already_set_stop_event_cancels_before_any_model_call(self):
        stop = threading.Event()
        stop.set()  # the wall-clock already fired
        loop = AgentLoop(
            workspace_root=self.tmp / "workspace",
            session_id="s-stop",
            engagement_id="eng-stop",
            stop_event=stop,
        )
        self.addCleanup(loop.close)

        # The client should never be called — the loop returns at the first turn boundary. If it
        # were called, this no-server LlamaClient would raise, so a 'cancelled' result is itself
        # the proof no model call happened.
        called = {"n": 0}
        patch.object(loop.client, "chat_completions",
                     side_effect=lambda *a, **k: called.__setitem__("n", called["n"] + 1)).start()

        result = loop.run_task("probe the login form")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(called["n"], 0)

    def test_without_a_stop_event_the_loop_is_unaffected(self):
        # No stop event -> the stop check is skipped entirely; the loop proceeds to its first
        # model call (stubbed to end the turn with no tool calls).
        loop = AgentLoop(
            workspace_root=self.tmp / "workspace",
            session_id="s-nostop",
            engagement_id="eng-nostop",
        )
        self.addCleanup(loop.close)
        patch.object(loop.client, "n_ctx", return_value=16384).start()
        patch.object(loop.client, "token_count", return_value=10).start()
        patch.object(loop.client, "rendered_token_count", return_value=100).start()
        patch.object(loop.client, "chat_completions", return_value={
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "model": "m",
        }).start()

        result = loop.run_task("hello")
        self.assertNotEqual(result.status, "cancelled")  # ran normally


if __name__ == "__main__":
    unittest.main()
