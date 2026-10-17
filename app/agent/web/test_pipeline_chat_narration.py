"""Autonomous runs narrate their wave/worker activity into the session chat.

The driver already streams phase events to the chat, but the parallel workers are emitted to the
engagement event log (agent/pipeline/wave.py), which only the Flow view reads — so the chat went
silent mid-run while five workers were busy, which read as "it stopped working". start_pipeline now
subscribes to the engagement event log (via emit's in-process listener hook) and mirrors the
wave/worker structural events into the session as `autonomous_event` activity rows.

This pins the row text the operator sees for each mirrored kind. The subscribe/unsubscribe hook
itself is covered by agent/engagement/test_emit_listeners.py.
"""
from __future__ import annotations

import unittest

from . import dev_server


class PipelineEventDetailTest(unittest.TestCase):
    def test_wave_started_counts_the_workers(self):
        d = dev_server._pipeline_event_detail(
            "wave_started", {"wave": 2, "experiments": [{}, {}, {}]})
        self.assertEqual(d, "wave 2 · 3 workers")

    def test_a_single_worker_wave_is_not_pluralised(self):
        self.assertEqual(
            dev_server._pipeline_event_detail("wave_started", {"wave": 1, "experiments": [{}]}),
            "wave 1 · 1 worker")

    def test_worker_spawned_shows_the_method_verbatim(self):
        d = dev_server._pipeline_event_detail(
            "worker_spawned", {"worker_id": "w0", "method": "Verify no CSP header; inject <script>"})
        self.assertEqual(d, "w0 → Verify no CSP header; inject <script>")

    def test_worker_spawned_without_a_method_is_just_the_id(self):
        self.assertEqual(
            dev_server._pipeline_event_detail("worker_spawned", {"worker_id": "w3"}), "w3")

    def test_worker_finished_carries_outcome_and_detail(self):
        d = dev_server._pipeline_event_detail(
            "worker_finished", {"worker_id": "w2", "outcome": "supported", "detail": "HSTS missing"})
        self.assertEqual(d, "w2 · supported — HSTS missing")

    def test_worker_finished_without_detail(self):
        self.assertEqual(
            dev_server._pipeline_event_detail(
                "worker_finished", {"worker_id": "w4", "outcome": "timeout"}),
            "w4 · timeout")

    def test_wave_ended_counts_the_workers(self):
        self.assertEqual(
            dev_server._pipeline_event_detail("wave_ended", {"wave": 2, "workers": 5}),
            "wave 2 complete · 5 workers")

    def test_the_mirrored_kinds_are_the_wave_structural_events(self):
        # Exactly the structural wave/worker events wave.py emits that the chat should narrate; the
        # per-experiment and payload-bearing events are deliberately left to the Flow view.
        self.assertEqual(
            dev_server._FORWARD_PIPELINE_KINDS,
            {"wave_started", "worker_spawned", "worker_finished", "wave_ended"})


if __name__ == "__main__":
    unittest.main()
