"""§14.1 C — taint escalates to the engagement when the vector is engagement-shared state.

The bug: `TaintStore(session_id)` keys taint on the session, but §6.1 shares one browser profile
across all of an engagement's workers. So worker A can process malicious content through the
shared browser, be marked tainted, and worker B — untainted, same cookie jar — carry on with the
material that tainted A.

This checks BOTH directions, because a one-directional check catches half of what its name
claims (handoff §6): a shared-vector taint on one session MUST escalate to a sibling, and a
session-local taint MUST NOT — an escalation that fired for everything would "pass" the first
assertion while quietly breaking the isolation the second one guards.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.broker.taint import ENGAGEMENT_SHARED_TAINT_SOURCES, TaintStore


class TaintEngagementEscalationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, session_id: str, engagement_id: str | None):
        return TaintStore(session_id, engagement_id=engagement_id, taint_dir=self.dir)

    def test_browser_fetch_is_an_engagement_shared_vector(self):
        # The concrete vector §6.1 shares; if this set is ever emptied the escalation below is
        # unreachable, so pin it.
        self.assertIn("browser_fetch", ENGAGEMENT_SHARED_TAINT_SOURCES)

    def test_a_shared_vector_taint_escalates_to_a_sibling_session(self):
        a = self._store("worker-A", "eng1")
        a.mark(reason="prompt injection in page", verdict="malicious",
               source="browser_fetch", shared=True)

        # The sibling's own session record is clean, yet it is tainted — via the engagement,
        # because it shares the browser profile. This is the assertion that fails against the
        # pre-fix per-session store.
        b = self._store("worker-B", "eng1")
        tainted, info = b.is_tainted()
        self.assertTrue(tainted)
        self.assertEqual(info.get("scope"), "engagement")

        # And B's session file really was never written — the taint came purely from the
        # engagement record, not from some accidental cross-write.
        self.assertFalse((self.dir / "worker-B.json").exists())

    def test_a_session_local_taint_does_not_reach_a_sibling(self):
        a = self._store("worker-A", "eng1")
        a.mark(reason="suspicious response body", verdict="suspicious",
               source="target_http", shared=False)  # a session-local read, not shared state
        b = self._store("worker-B", "eng1")
        tainted, _ = b.is_tainted()
        self.assertFalse(tainted, "a session-local taint leaked to a sibling — isolation broken")
        # A himself is still tainted, as before.
        self.assertTrue(a.is_tainted()[0])

    def test_a_shared_taint_stays_within_its_engagement(self):
        self._store("worker-A", "eng1").mark(
            reason="x", verdict="malicious", source="browser_fetch", shared=True)
        other = self._store("worker-C", "eng2")  # different engagement, different browser profile
        self.assertFalse(other.is_tainted()[0])

    def test_clear_does_not_wipe_the_shared_engagement_taint(self):
        a = self._store("worker-A", "eng1")
        a.mark(reason="x", verdict="malicious", source="browser_fetch", shared=True)
        a.clear()  # A decides it is done
        # A's own session is clean again, but the shared profile is still suspect for siblings.
        self.assertFalse((self.dir / "worker-A.json").exists() and a._check_path(a.path)[0])
        self.assertTrue(self._store("worker-B", "eng1").is_tainted()[0])

    def test_session_only_construction_is_unchanged(self):
        # No engagement_id: behaves exactly as the pre-§14.1-C store — no engagement file, and a
        # shared=True mark has nowhere to escalate to.
        s = TaintStore("solo", taint_dir=self.dir)
        self.assertIsNone(s.engagement_path)
        s.mark(reason="x", verdict="malicious", source="browser_fetch", shared=True)
        self.assertTrue(s.is_tainted()[0])
        self.assertFalse((self.dir / "engagement").exists())


if __name__ == "__main__":
    unittest.main()
