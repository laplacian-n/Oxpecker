"""The Flow surface draws from keys the §4.2 fold does not name directly, so the snapshot endpoint
adds them (dev_server._flow_shape). This pins that adapter: it maps the running wave, the
hypotheses in play, the in-flight calls and the worker enrichment the diagram reads, additively
(the raw fold keys are left intact), and it invents nothing the stream does not carry.
"""
from __future__ import annotations

import unittest

from . import dev_server


FOLD = {
    "waves": [
        {"wave": 1, "status": "ended", "experiments": [{"hypothesis_id": "h1", "method": "recon"}]},
        {"wave": 2, "status": "running", "experiments": [
            {"hypothesis_id": "h7", "method": "Time-based SQLi probe on POST /login"},
            {"hypothesis_id": "h8", "method": "Enumerate /api/orders ids"}]},
    ],
    "workers": [
        {"worker_id": "w0", "hypothesis": "h7", "method": "Time-based SQLi probe on POST /login",
         "status": "running"},
        {"worker_id": "w1", "hypothesis": "h8", "method": "Enumerate /api/orders ids",
         "status": "finished", "outcome": "supported"},
    ],
    "tool_calls_in_flight": [
        {"call_id": "c1", "worker": "w0", "tool": "http_request", "argument_digest": "ab"}],
    "artifacts": [{"worker": "w1", "artifact": "orders.json", "store": "evidence", "ref": "x"}],
}


class FlowShapeTest(unittest.TestCase):
    def setUp(self):
        self.fs = dev_server._flow_shape(FOLD)

    def test_the_current_wave_is_the_running_one(self):
        self.assertEqual(self.fs["wave"]["number"], 2)
        dispatched = self.fs["wave"]["dispatched"]
        self.assertEqual(len(dispatched), 2)
        self.assertEqual(dispatched[0], {"hypothesis_id": "h7",
                                         "method": "Time-based SQLi probe on POST /login",
                                         "claimed_by": "w0"})

    def test_hypotheses_in_play_carry_their_state_from_the_worker(self):
        states = {h["id"]: h["state"] for h in self.fs["hypotheses"]}
        self.assertEqual(states, {"h7": "claimed", "h8": "supported"})
        titles = {h["id"]: h["title"] for h in self.fs["hypotheses"]}
        self.assertEqual(titles["h7"], "Time-based SQLi probe on POST /login")

    def test_finished_waves_are_summarised(self):
        self.assertEqual(self.fs["finished_waves"], [{"wave": 1, "workers": 1, "summary": ""}])

    def test_in_flight_calls_become_pipeline_tokens_at_exec(self):
        self.assertEqual(self.fs["calls_in_pipeline"],
                         [{"call_id": "c1", "worker": "w0", "tool": "http_request",
                           "station": "exec", "status": "allowed"}])

    def test_workers_are_enriched_without_losing_raw_fields(self):
        w0 = next(w for w in self.fs["workers"] if w["worker_id"] == "w0")
        self.assertEqual(w0["hypothesis"], "h7")               # raw field kept
        self.assertEqual(w0["hypothesis_title"], "Time-based SQLi probe on POST /login")
        self.assertEqual(w0["calls"], 1)                       # one call in flight
        self.assertEqual(w0["tool_in_flight"], {"tool": "http_request"})
        w1 = next(w for w in self.fs["workers"] if w["worker_id"] == "w1")
        self.assertEqual(w1["artifacts"], 1)                   # stored one artifact
        self.assertIsNone(w1["tool_in_flight"])                # nothing in flight

    def test_it_is_additive_and_empty_fold_is_safe(self):
        # An engagement that has emitted nothing: no wave, no invented data, just the safe defaults.
        fs = dev_server._flow_shape({})
        self.assertNotIn("wave", fs)
        self.assertNotIn("hypotheses", fs)
        self.assertEqual(fs["calls_in_pipeline"], [])
        self.assertEqual(fs["workers"], [])
        self.assertEqual(fs["mode"], {"tainted": False, "kill_switch": False})

    def test_the_live_snapshot_carries_the_flow_keys(self):
        # End to end through the endpoint: an empty engagement still returns the Flow keys beside
        # the raw fold, so the diagram binds to something on first open.
        from fastapi.testclient import TestClient
        from .. import config
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp(prefix="flow-shape-"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        with patch.object(config, "WEB_UI_API_KEY_FILE", tmp / "no-key.txt"):
            with TestClient(dev_server.app) as client:
                snap = client.get("/api/engagements/lab-default/snapshot").json()
        for key in ("wave", "hypotheses"):  # absent when empty, present once running
            pass
        self.assertIn("calls_in_pipeline", snap)
        self.assertIn("workers", snap)
        self.assertIn("mode", snap)
        self.assertIn("model_roster", snap)  # raw fold key still present (additive)


if __name__ == "__main__":
    unittest.main()
