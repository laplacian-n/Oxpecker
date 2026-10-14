"""The engagement-level event stream on dev_server (CLIENT_UI_DESIGN.md §4): the snapshot and SSE
endpoints the new operator client draws every surface from. The log itself is tested in
agent/engagement/test_event_log.py; these pin that dev_server exposes it — version-gated, keyed by
engagement, created-on-GET-safe — so the client has a backend on the server the operator launches.

Run directly: `python3 -m unittest agent.web.test_engagement_stream`.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from ..engagement import emit as _emit
from . import dev_server


class EngagementStreamTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="engagement-stream-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._patchers = [
            patch.object(config, "ENGAGEMENTS_ROOT", self.tmp / "eng"),
            patch.object(dev_server._config, "ENGAGEMENTS_ROOT", self.tmp / "eng"),
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def test_snapshot_of_an_engagement_that_never_emitted_is_empty_not_404(self):
        resp = self.client.get("/api/engagements/eng-x/snapshot?api_version=1.0.0")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["latest_seq"], 0)
        self.assertEqual(body["workers"], [])
        self.assertIn("tier", body)

    def test_snapshot_folds_emitted_events(self):
        _emit.emit("eng-x", "worker_spawned",
                   {"worker_id": "w1", "hypothesis": "h1", "method": "probe"})
        _emit.emit("eng-x", "finding_recorded", {"finding_id": "f1"})
        body = self.client.get("/api/engagements/eng-x/snapshot?api_version=1.0.0").json()
        self.assertEqual(body["latest_seq"], 2)
        self.assertEqual(len(body["workers"]), 1)
        self.assertEqual(body["workers"][0]["worker_id"], "w1")
        self.assertEqual(body["counts"]["finding_recorded"], 1)

    def test_an_incompatible_major_version_is_refused(self):
        resp = self.client.get("/api/engagements/eng-x/snapshot?api_version=2.0.0")
        self.assertEqual(resp.status_code, 409)

    def test_absent_version_is_allowed_additive(self):
        self.assertEqual(self.client.get("/api/engagements/eng-x/snapshot").status_code, 200)

    def test_sse_body_emits_control_then_replays_events(self):
        _emit.emit("eng-x", "wave_started", {"wave": 1, "experiments": []})
        _emit.emit("eng-x", "wave_ended", {"wave": 1})

        async def already_done():
            return True  # one pass of the loop: control + replay, then the disconnect check ends it

        async def drain():
            return [chunk async for chunk in
                    dev_server._engagement_sse("eng-x", 0, already_done, poll=0.0)]

        chunks = asyncio.run(drain())
        blob = "".join(chunks)
        self.assertIn("event: control", blob)
        self.assertIn("subscription_resumed", blob)
        self.assertIn("wave_started", blob)
        self.assertIn("wave_ended", blob)
        # the control event states the gap for a fresh subscriber (missed=0 at after=0)
        control = json.loads(blob.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(control["payload"]["latest_seq"], 2)

    def test_sse_resume_reports_the_missed_count(self):
        for i in range(5):
            _emit.emit("eng-x", "model_call", {"model_id": "m", "prompt_tokens": i})

        async def already_done():
            return True

        async def drain():
            return [chunk async for chunk in
                    dev_server._engagement_sse("eng-x", 2, already_done, poll=0.0)]

        blob = "".join(asyncio.run(drain()))
        control = json.loads(blob.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(control["payload"]["missed"], 3)  # 5 total, resumed after 2


if __name__ == "__main__":
    unittest.main()
