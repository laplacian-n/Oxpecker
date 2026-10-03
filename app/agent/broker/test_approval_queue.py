"""Tests for the Phase 6 approval queue."""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from .approval_queue import (
    AlreadyResolvedError,
    ApprovalQueue,
    ApprovalRequestNotFoundError,
)


class TestApprovalQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="approval-queue-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.queue = ApprovalQueue(queue_dir=self.tmp)

    def test_submit_and_get(self):
        rid = self.queue.submit(
            session_id="s1", tool="run_command", arguments={"argv": ["ls"]}, reason="test"
        )
        record = self.queue.get(rid)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["tool"], "run_command")

    def test_get_unknown_raises(self):
        with self.assertRaises(ApprovalRequestNotFoundError):
            self.queue.get("nonexistent")

    def test_list_pending_excludes_resolved(self):
        rid1 = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        rid2 = self.queue.submit(session_id="s1", tool="t2", arguments={}, reason="r")
        self.queue.resolve(rid1, approved=True, resolved_by="owner")
        pending = self.queue.list_pending()
        self.assertEqual([p["request_id"] for p in pending], [rid2])

    def test_list_pending_filters_by_session(self):
        self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        self.queue.submit(session_id="s2", tool="t2", arguments={}, reason="r")
        pending_s1 = self.queue.list_pending(session_id="s1")
        self.assertEqual(len(pending_s1), 1)
        self.assertEqual(pending_s1[0]["session_id"], "s1")

    def test_resolve_approved(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        record = self.queue.resolve(rid, approved=True, resolved_by="owner")
        self.assertEqual(record["status"], "approved")
        self.assertIsNotNone(record["resolved_at"])

    def test_resolve_declined(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        record = self.queue.resolve(rid, approved=False, resolved_by="owner")
        self.assertEqual(record["status"], "declined")

    def test_double_resolve_rejected(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        self.queue.resolve(rid, approved=True, resolved_by="owner")
        with self.assertRaises(AlreadyResolvedError):
            self.queue.resolve(rid, approved=False, resolved_by="owner")

    def test_wait_for_resolution_times_out(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        with self.assertRaises(TimeoutError):
            self.queue.wait_for_resolution(rid, timeout_s=0.3, poll_interval_s=0.1)

    def test_wait_for_resolution_returns_once_resolved(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")

        def resolve_later():
            time.sleep(0.2)
            self.queue.resolve(rid, approved=True, resolved_by="owner")

        threading.Thread(target=resolve_later).start()
        record = self.queue.wait_for_resolution(rid, timeout_s=2, poll_interval_s=0.05)
        self.assertEqual(record["status"], "approved")

    def test_persists_across_instances(self):
        rid = self.queue.submit(session_id="s1", tool="t1", arguments={}, reason="r")
        reopened = ApprovalQueue(queue_dir=self.tmp)
        self.assertEqual(reopened.get(rid)["status"], "pending")

    def test_concurrent_submit_and_poll_never_sees_a_partial_write(self):
        """Regression test for a real race found via agent/web/test_server.py: list_pending()
        polling from one thread while submit() writes from another hit a JSONDecodeError on a
        truncated file, because the original write wasn't atomic. Hammers both sides
        concurrently — if this flakes even once, the write isn't actually atomic."""
        errors = []
        stop = threading.Event()

        def poller():
            while not stop.is_set():
                try:
                    self.queue.list_pending()
                except Exception as e:  # noqa: BLE001 - any exception here is the bug
                    errors.append(e)

        threads = [threading.Thread(target=poller) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(200):
            self.queue.submit(session_id="s1", tool=f"t{i}", arguments={}, reason="r")
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [], f"list_pending() raised during concurrent writes: {errors}")


if __name__ == "__main__":
    unittest.main()
