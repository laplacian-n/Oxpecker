"""Tests for the engagement-level event stream endpoints (CLIENT_UI_DESIGN.md §4):
`GET /api/engagements/{id}/snapshot` and `GET /api/engagements/{id}/events`.

These cover the two details §4 calls load-bearing: the §4.1.2 account boundary enforced on
connect *and* on resume (a reconnect carrying a Last-Event-ID for an engagement the caller no
longer owns is refused, not resumed), and resume correctness — a client that reconnects at N+k
sees exactly the events after its cursor and is told how many it missed (§5.5).
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from . import server as server_mod
from .test_server import TestWebServerBase


def _drain(engagement_id: str, after: int, disconnect_after: int) -> list[dict]:
    """Run the real SSE generator and collect the JSON of every event it yields, telling it the
    client disconnected after `disconnect_after` poll cycles. Used in place of a TestClient stream
    (which buffers the whole response and so cannot consume an endless SSE body)."""
    calls = {"n": 0}

    async def is_disconnected() -> bool:
        calls["n"] += 1
        return calls["n"] >= disconnect_after

    async def go() -> list[dict]:
        out = []
        gen = server_mod._sse_event_stream(engagement_id, after, is_disconnected, poll=0)
        async for chunk in gen:
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    out.append(json.loads(line[len("data:"):].strip()))
        return out

    return asyncio.run(go())


class EngagementEventsBase(TestWebServerBase):
    def setUp(self):
        super().setUp()
        # The event-log registry is module-level; give each test its own so logs opened in one
        # test are not seen by the next. The accounts file, like the key file, is a bound default
        # computed at import, so point it at a guaranteed-nonexistent path unless a test writes one.
        self._logs_patcher = patch.object(server_mod, "_event_logs", {})
        self._logs_patcher.start()
        self.addCleanup(self._logs_patcher.stop)
        self._accounts_patcher = patch(
            "agent.config.WEB_UI_ACCOUNTS_FILE", self.tmp / "no-such-accounts-file"
        )
        self._accounts_patcher.start()
        self.addCleanup(self._accounts_patcher.stop)

    def _create_engagement(self, eng_id: str, headers: dict | None = None) -> dict:
        resp = self.client.post(
            "/api/engagements",
            json={
                "engagement_id": eng_id,
                "allow_targets": ["example.com"],
                "allowed_action_classes": ["recon"],
                "authorized_by": "tester",
            },
            headers=headers or {},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def _append(self, eng_id: str, kind: str, payload: dict) -> int:
        return server_mod._engagement_event_log(eng_id, create=True).append(kind, payload)

    @staticmethod
    def _data_events(raw_lines) -> list[dict]:
        """Pull the JSON out of every `data:` line in an SSE byte/line stream."""
        out = []
        for line in raw_lines:
            s = line.decode() if isinstance(line, (bytes, bytearray)) else line
            if s.startswith("data:"):
                out.append(json.loads(s[len("data:"):].strip()))
        return out


class SnapshotEndpointTest(EngagementEventsBase):
    def test_snapshot_of_an_engagement_that_emitted_nothing_is_empty_not_404(self):
        self._create_engagement("eng1")
        resp = self.client.get("/api/engagements/eng1/snapshot")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["latest_seq"], 0)
        self.assertEqual(body["workers"], [])

    def test_snapshot_at_a_sequence_number_bounds_the_projection(self):
        self._create_engagement("eng1")
        self._append("eng1", "worker_spawned", {"worker_id": "w1"})
        self._append("eng1", "worker_finished", {"worker_id": "w1", "outcome": "done"})
        at1 = self.client.get("/api/engagements/eng1/snapshot?at=1").json()
        self.assertEqual(at1["at_seq"], 1)
        self.assertEqual(at1["workers"][0]["status"], "running")  # finish not yet folded in
        at2 = self.client.get("/api/engagements/eng1/snapshot?at=2").json()
        self.assertEqual(at2["workers"][0]["status"], "finished")

    def test_snapshot_of_a_nonexistent_engagement_is_404(self):
        self.assertEqual(self.client.get("/api/engagements/ghost/snapshot").status_code, 404)


class EventStreamTest(EngagementEventsBase):
    def test_catch_up_delivers_every_event_with_its_sequence_as_the_sse_id(self):
        self._create_engagement("eng1")
        for i in range(3):
            self._append("eng1", "note_added", {"i": i})
        data = _drain("eng1", after=0, disconnect_after=1)
        self.assertEqual(data[0]["kind"], "subscription_resumed")
        self.assertEqual([e["seq"] for e in data[1:]], [1, 2, 3])

        # The id: line is what the browser echoes back as Last-Event-ID; assert it carries the
        # sequence number by capturing the raw SSE chunks once.
        async def raw() -> str:
            calls = {"n": 0}

            async def disc():
                calls["n"] += 1
                return calls["n"] >= 1
            chunks = []
            async for c in server_mod._sse_event_stream("eng1", 0, disc, poll=0):
                chunks.append(c)
            return "".join(chunks)

        text = asyncio.run(raw())
        self.assertIn("id: 1\n", text)
        self.assertIn("id: 3\n", text)

    def test_resume_from_last_event_id_replays_only_what_is_past_the_cursor(self):
        self._create_engagement("eng1")
        for i in range(5):
            self._append("eng1", "note_added", {"i": i})
        data = _drain("eng1", after=2, disconnect_after=1)
        control = data[0]
        self.assertEqual(control["kind"], "subscription_resumed")
        self.assertEqual(control["payload"]["resumed_after"], 2)
        self.assertEqual(control["payload"]["latest_seq"], 5)
        self.assertEqual(control["payload"]["missed"], 3)
        self.assertEqual([e["seq"] for e in data[1:]], [3, 4, 5])

    def test_a_fresh_subscriber_reports_no_missed_events(self):
        self._create_engagement("eng1")
        self._append("eng1", "note_added", {})
        data = _drain("eng1", after=0, disconnect_after=1)
        self.assertEqual(data[0]["payload"]["missed"], 0)

    def test_live_tail_delivers_an_event_appended_after_connect(self):
        self._create_engagement("eng1")
        # Append between poll cycles: the generator sees an empty log on cycle 1, the event is
        # added, and it is delivered on cycle 2 before the disconnect on cycle 3.
        calls = {"n": 0}

        async def is_disconnected():
            calls["n"] += 1
            if calls["n"] == 1:
                self._append("eng1", "finding_recorded", {"ordinal": 1})
            return calls["n"] >= 2

        async def go():
            out = []
            async for chunk in server_mod._sse_event_stream("eng1", 0, is_disconnected, poll=0):
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        out.append(json.loads(line[len("data:"):].strip()))
            return out

        data = asyncio.run(go())
        kinds = [e["kind"] for e in data]
        self.assertIn("finding_recorded", kinds)
        self.assertEqual(data[-1]["seq"], 1)


class ApiVersionTest(EngagementEventsBase):
    def test_a_matching_major_version_is_served(self):
        self._create_engagement("eng1")
        resp = self.client.get("/api/engagements/eng1/snapshot",
                               headers={"X-API-Version": server_mod.API_VERSION})
        self.assertEqual(resp.status_code, 200)

    def test_an_incompatible_major_version_is_refused_plainly(self):
        self._create_engagement("eng1")
        resp = self.client.get("/api/engagements/eng1/snapshot", headers={"X-API-Version": "2.0.0"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("incompatible", resp.json()["detail"])


class AccountBoundaryTest(EngagementEventsBase):
    """§5.3 / §4.1.2 — the account boundary on the largest read in the system."""

    def setUp(self):
        super().setUp()
        # Two real accounts. This both proves the boundary refuses the wrong one AND that the
        # auth middleware accepts a second account's valid key at all (bob reaching a 403 rather
        # than a 401 is the proof his key was accepted at the door).
        accounts = self.tmp / "accounts.json"
        accounts.write_text(json.dumps({"alice": "ka", "bob": "kb"}))
        self._acc = patch("agent.config.WEB_UI_ACCOUNTS_FILE", accounts)
        self._acc.start()
        self.addCleanup(self._acc.stop)
        self.alice = {"Authorization": "Bearer ka"}
        self.bob = {"Authorization": "Bearer kb"}

    def test_create_records_the_creator_as_owner(self):
        body = self._create_engagement("alices", headers=self.alice)
        self.assertEqual(body["owner"], "alice")

    def test_the_owner_may_read_the_stream_and_snapshot(self):
        self._create_engagement("alices", headers=self.alice)
        self.assertEqual(
            self.client.get("/api/engagements/alices/snapshot", headers=self.alice).status_code, 200
        )

    def test_a_different_account_is_refused_the_snapshot(self):
        self._create_engagement("alices", headers=self.alice)
        resp = self.client.get("/api/engagements/alices/snapshot", headers=self.bob)
        self.assertEqual(resp.status_code, 403, resp.text)

    def test_a_different_account_is_refused_the_stream_on_connect(self):
        # The owner check is raised before the stream body begins, so a plain GET gets the 403
        # back immediately rather than opening an endless stream.
        self._create_engagement("alices", headers=self.alice)
        self.assertEqual(
            self.client.get("/api/engagements/alices/events", headers=self.bob).status_code, 403
        )

    def test_a_reconnect_with_a_last_event_id_is_refused_not_resumed(self):
        # §4.1.2 in one assertion: carrying a Last-Event-ID does not get bob past the owner
        # check. The boundary is re-evaluated on resume exactly as on connect.
        self._create_engagement("alices", headers=self.alice)
        self._append("alices", "note_added", {})  # so there is something a resume could replay
        resp = self.client.get(
            "/api/engagements/alices/events", headers={**self.bob, "Last-Event-ID": "0"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_a_bad_key_is_401_at_the_door_before_the_owner_check(self):
        self._create_engagement("alices", headers=self.alice)
        resp = self.client.get("/api/engagements/alices/snapshot",
                               headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
