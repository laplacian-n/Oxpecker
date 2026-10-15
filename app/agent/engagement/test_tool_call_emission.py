"""tool_call_started/finished land on the engagement stream and fold correctly.

The AgentLoop emits these around every tool dispatch (agent/loop.py), tagged with the worker the
thread runs as (emit.current_worker, set by the wave). This checks the emit → project contract the
loop relies on: a started call shows up in-flight attributed to its worker; the matching finished
removes it, so what remains is "running now" — exactly what the flow view and worker rail draw.

Run:  python3 -m unittest agent.engagement.test_tool_call_emission
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import emit as event_emit
from .event_log import EngagementEventLog, project


class ToolCallEmissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="toolcall-emit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._p = patch.object(config, "ENGAGEMENTS_ROOT", self.tmp)
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(event_emit.set_current_worker, None)

    def _project(self):
        log = EngagementEventLog(self.tmp / "eng")
        evs = log.read_since(0)
        return project(evs, at_seq=log.latest_seq(), latest_seq=log.latest_seq())

    def test_started_is_in_flight_with_its_worker_then_finished_removes_it(self):
        event_emit.set_current_worker("w2")
        event_emit.emit("eng", "tool_call_started", {
            "call_id": "c1", "worker": event_emit.current_worker(),
            "tool": "http_request", "argument_digest": "abc123"})

        snap = self._project()
        inflight = snap["tool_calls_in_flight"]
        self.assertEqual(len(inflight), 1)
        self.assertEqual(inflight[0]["call_id"], "c1")
        self.assertEqual(inflight[0]["worker"], "w2")      # attributed to the worker
        self.assertEqual(inflight[0]["tool"], "http_request")

        event_emit.emit("eng", "tool_call_finished", {"call_id": "c1"})
        self.assertEqual(self._project()["tool_calls_in_flight"], [])  # no longer "running now"

    def test_assistant_path_without_a_worker_context_still_emits(self):
        # The chat/assistant path never sets a worker; the loop falls back to the session id, so the
        # call is still attributed to *something* and never anonymous.
        self.assertIsNone(event_emit.current_worker())
        event_emit.emit("eng", "tool_call_started", {
            "call_id": "c9", "worker": event_emit.current_worker() or "session-xyz",
            "tool": "port_discovery", "argument_digest": "deadbeef"})
        inflight = self._project()["tool_calls_in_flight"]
        self.assertEqual(inflight[0]["worker"], "session-xyz")


if __name__ == "__main__":
    unittest.main()
