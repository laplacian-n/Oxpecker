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

    def test_the_mirrored_kinds_cover_the_wave_and_the_tool_calls(self):
        # The structural wave/worker events plus each worker's tool calls, so the chat narrates both
        # the dispatch and what every worker actually does. The experiment_started/ended events and
        # anything carrying raw output are deliberately left to the Flow view / audit log.
        self.assertEqual(
            dev_server._FORWARD_PIPELINE_KINDS,
            {"wave_started", "worker_spawned", "worker_finished", "wave_ended",
             "tool_call_started", "tool_call_finished"})


class ToolCallNarrationTest(unittest.TestCase):
    def test_a_wave_worker_keeps_its_id_the_strategist_is_named(self):
        self.assertEqual(dev_server._worker_label("w0"), "w0")
        self.assertEqual(dev_server._worker_label("w12"), "w12")
        # A session uuid on the strategist/assistant path is named, not printed raw.
        self.assertEqual(dev_server._worker_label("fe06f33d-6f2"), "strategist")
        self.assertEqual(dev_server._worker_label(""), "strategist")

    def test_tool_call_started_shows_the_preview(self):
        d = dev_server._pipeline_event_detail(
            "tool_call_started",
            {"worker": "w1", "tool": "http_request", "preview": "GET https://laplacian.site/login"})
        self.assertEqual(d, "w1 → GET https://laplacian.site/login")

    def test_tool_call_started_falls_back_to_the_tool_name(self):
        d = dev_server._pipeline_event_detail(
            "tool_call_started", {"worker": "w2", "tool": "run_command", "preview": ""})
        self.assertEqual(d, "w2 → run_command")

    def test_tool_call_finished_marks_success_and_latency(self):
        self.assertEqual(
            dev_server._pipeline_event_detail(
                "tool_call_finished", {"worker": "w1", "tool": "http_request", "ok": True, "latency_ms": 320}),
            "w1 · http_request ✓ 320ms")

    def test_tool_call_finished_marks_failure(self):
        self.assertEqual(
            dev_server._pipeline_event_detail(
                "tool_call_finished", {"worker": "w3", "tool": "run_command", "ok": False, "latency_ms": 12}),
            "w3 · run_command ✗ 12ms")

    def test_tool_call_finished_when_the_dispatch_raised(self):
        # ok is None → neither tick nor cross, just a neutral marker.
        self.assertEqual(
            dev_server._pipeline_event_detail(
                "tool_call_finished", {"worker": "w0", "tool": "http_request", "ok": None}),
            "w0 · http_request ·")


if __name__ == "__main__":
    unittest.main()
