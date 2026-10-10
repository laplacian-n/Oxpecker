"""Wiring the autonomous pipeline into dev_server: /pipeline/start runs the real AutonomousDriver
on a thread with its events streamed to the session SSE, and /pipeline/stop ends it. The driver
itself is validated by agent/pipeline/test_autonomous_driver*.py (and a live OpenRouter run); here
we check dev_server's side of the seam with the driver mocked — the engagement is prepared, the
driver is constructed with the session's engagement/model wiring, events reach the session queue,
and stop sets the driver's stop_event.

Run directly: `python3 -m unittest agent.web.test_pipeline_start`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from .. import config
from . import dev_server


class _FakeDriver:
    constructed: dict = {}
    emitted = threading.Event()  # set once run() has emitted its event — deterministic sync

    def __init__(self, **kwargs):
        _FakeDriver.constructed = kwargs
        _FakeDriver.emitted.clear()
        self.stop_event = kwargs.get("stop_event")
        self._on_event = kwargs.get("on_event")

    def run(self):
        self._on_event({"type": "phase_entered", "phase": "RECON", "engagement_id": "e"})
        _FakeDriver.emitted.set()
        # Block until stopped so the stop test can observe the event being set.
        self.stop_event.wait(timeout=5)
        return type("R", (), {"status": "closed_out", "reason": "done",
                              "final_phase": "CLOSEOUT", "phases_completed": ["RECON"]})()


class PipelineStartTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="pipeline-start-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions", "_engagements")}
        for n in self._saved:
            getattr(dev_server, n).clear()
        self._patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(config, "ENGAGEMENTS_ROOT", self.tmp),
            patch.object(dev_server._config, "ENGAGEMENTS_ROOT", self.tmp),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
            # The driver lives in agent.pipeline.autonomous_driver; the handler imports it there.
            patch("agent.pipeline.autonomous_driver.AutonomousDriver", _FakeDriver),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)
        # An engagement + session to run.
        dev_server._engagements["eng1"] = dev_server.Engagement(
            engagement_id="eng1", description="t", allow_targets=["127.0.0.1"],
            allowed_action_classes=["passive_recon"], authorized_by="op",
            valid_until=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)))
        dev_server._sessions["s1"] = dev_server.Session(session_id="s1", engagement_id="eng1")

    def tearDown(self):
        ev = dev_server._sessions.get("s1")
        if ev is not None and ev.autonomous_stop_event is not None:
            ev.autonomous_stop_event.set()
        for n, saved in self._saved.items():
            st = getattr(dev_server, n)
            st.clear()
            st.update(saved)

    def _drain(self, session_id, timeout=3.0):
        s = dev_server._sessions[session_id]
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            try:
                seen.append(s.events.get(timeout=0.2))
            except Exception:
                if not s.running and s.events.empty():
                    break
        return seen

    def test_start_prepares_engagement_and_streams_driver_events(self):
        r = self.client.post("/api/sessions/s1/pipeline/start",
                             json={"engagement_id": "eng1", "profile": "web_api", "mode": "autonomous"})
        self.assertEqual(r.status_code, 200, r.text)
        # The engine files were materialised with the high tier set.
        roe = json.loads((self.tmp / "eng1" / "roe.json").read_text())
        self.assertEqual(roe["tier"], "high")
        # Wait deterministically for the driver thread to have emitted, not a fixed sleep.
        self.assertTrue(_FakeDriver.emitted.wait(timeout=5), "driver run() never emitted")
        self.assertEqual(_FakeDriver.constructed.get("engagement_id"), "eng1")
        self.assertIsNotNone(_FakeDriver.constructed.get("on_event"))
        # Its emitted event reached the session SSE queue wrapped as autonomous_event.
        evs = self._drain("s1")
        kinds = [e.get("type") for e in evs]
        self.assertIn("autonomous_event", kinds)
        inner = [e["event"]["type"] for e in evs if e.get("type") == "autonomous_event"]
        self.assertIn("phase_entered", inner)
        # Clean up the blocked fake run.
        self.client.post("/api/sessions/s1/pipeline/stop")

    def test_stop_sets_the_driver_stop_event(self):
        self.client.post("/api/sessions/s1/pipeline/start",
                         json={"engagement_id": "eng1", "profile": "web_api", "mode": "autonomous"})
        self.assertTrue(_FakeDriver.emitted.wait(timeout=5), "driver thread never started")
        ev = dev_server._sessions["s1"].autonomous_stop_event
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_set())
        r = self.client.post("/api/sessions/s1/pipeline/stop")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ev.is_set())

    def test_start_without_an_engagement_is_400(self):
        dev_server._sessions["s2"] = dev_server.Session(session_id="s2", engagement_id="missing")
        r = self.client.post("/api/sessions/s2/pipeline/start",
                             json={"engagement_id": "missing", "profile": "web_api", "mode": "autonomous"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
