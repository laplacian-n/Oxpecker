"""GET /api/sessions must say which engagement each conversation belongs to.

Session.engagement_id has existed since engagements did, and this row never carried it. A client
could therefore list sessions and not tell which belonged to the engagement it had open: the only
ways out were to GET every session one by one, or to show every conversation in the install under
whichever engagement happened to be selected. The classic UI took the second route and got away
with it because it only ever had one engagement selected at a time and never cross-checked.

The React chat surface needs the field to scope its session picker, which is what made the gap
worth closing rather than documenting.

Asserted with two sessions in two different engagements, not one: a row hardcoded to
"lab-default", or to whichever engagement was created first, passes any single-session check.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class SessionListRowTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="session-list-row-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = {n: dict(getattr(dev_server, n)) for n in ("_sessions", "_engagements")}
        for name, saved in self._saved.items():
            getattr(dev_server, name).clear()
            self.addCleanup(lambda n=name, s=saved: (getattr(dev_server, n).clear(),
                                                     getattr(dev_server, n).update(s)))
        patchers = [
            patch.object(dev_server, "DATA_DIR", self.tmp),
            patch.object(config, "ENGAGEMENTS_ROOT", self.tmp),
            patch.object(dev_server._config, "ENGAGEMENTS_ROOT", self.tmp),
            # No key file -> the endpoints are open, which is what this test is about.
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-such-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)

    def _make(self, engagement_id: str) -> str:
        resp = self.client.post("/api/sessions", json={"engagement_id": engagement_id})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["session_id"]

    def test_each_row_names_its_own_engagement(self):
        a = self._make("alpha-engagement")
        b = self._make("beta-engagement")

        resp = self.client.get("/api/sessions")
        self.assertEqual(resp.status_code, 200, resp.text)
        rows = {r["session_id"]: r for r in resp.json()}

        self.assertIn("engagement_id", rows[a],
                      "GET /api/sessions rows no longer carry engagement_id; a client cannot "
                      "scope its session list to the engagement it has open without it")
        self.assertEqual(rows[a]["engagement_id"], "alpha-engagement")
        self.assertEqual(rows[b]["engagement_id"], "beta-engagement")

    def test_the_field_survives_a_session_that_has_messages(self):
        """The row is built from two different sources depending on history.

        `last_modified` reads from s.messages[-1] when there are messages and s.created_at when
        there are none -- two branches through the same dict literal. A field added to one branch
        only would be present on fresh sessions and missing the moment someone spoke, which is the
        harder bug to see.
        """
        sid = self._make("gamma-engagement")
        dev_server._sessions[sid].append("user", "hello")

        row = next(r for r in self.client.get("/api/sessions").json() if r["session_id"] == sid)
        self.assertEqual(row["engagement_id"], "gamma-engagement")
        self.assertEqual(row["message_count"], 1)


if __name__ == "__main__":
    unittest.main()
