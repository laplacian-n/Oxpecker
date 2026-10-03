"""Tests for the ConsultQueue primitive (autonomous pipeline driver's "consult" mode check-in
mechanism) — mirrors test_approval_queue.py exactly, including the concurrent-write regression
test, since consult_queue.py copies approval_queue.py's file-based/atomic-write/poll pattern.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from .consult_queue import (
    AlreadyAnsweredError,
    ConsultQueue,
    ConsultRequestNotFoundError,
)


class TestConsultQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="consult-queue-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.queue = ConsultQueue(queue_dir=self.tmp)

    def test_submit_and_get(self):
        rid = self.queue.submit(
            session_id="s1", question="continue to VALIDATION?", context={"phase": "ANALYSIS"},
            options=["continue", "stop"],
        )
        record = self.queue.get(rid)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["question"], "continue to VALIDATION?")
        self.assertEqual(record["options"], ["continue", "stop"])
        self.assertEqual(record["context"], {"phase": "ANALYSIS"})

    def test_submit_without_options_or_context_defaults_to_empty(self):
        rid = self.queue.submit(session_id="s1", question="q")
        record = self.queue.get(rid)
        self.assertEqual(record["options"], [])
        self.assertEqual(record["context"], {})

    def test_get_unknown_raises(self):
        with self.assertRaises(ConsultRequestNotFoundError):
            self.queue.get("nonexistent")

    def test_list_pending_excludes_answered(self):
        rid1 = self.queue.submit(session_id="s1", question="q1")
        rid2 = self.queue.submit(session_id="s1", question="q2")
        self.queue.resolve(rid1, answer="continue", resolved_by="owner")
        pending = self.queue.list_pending()
        self.assertEqual([p["request_id"] for p in pending], [rid2])

    def test_list_pending_filters_by_session(self):
        self.queue.submit(session_id="s1", question="q1")
        self.queue.submit(session_id="s2", question="q2")
        pending_s1 = self.queue.list_pending(session_id="s1")
        self.assertEqual(len(pending_s1), 1)
        self.assertEqual(pending_s1[0]["session_id"], "s1")

    def test_resolve_records_answer(self):
        rid = self.queue.submit(session_id="s1", question="q")
        record = self.queue.resolve(rid, answer="redirect to check auth instead", resolved_by="owner")
        self.assertEqual(record["status"], "answered")
        self.assertEqual(record["answer"], "redirect to check auth instead")
        self.assertIsNotNone(record["answered_at"])

    def test_double_resolve_rejected(self):
        rid = self.queue.submit(session_id="s1", question="q")
        self.queue.resolve(rid, answer="continue", resolved_by="owner")
        with self.assertRaises(AlreadyAnsweredError):
            self.queue.resolve(rid, answer="stop", resolved_by="owner")

    def test_wait_for_answer_times_out(self):
        rid = self.queue.submit(session_id="s1", question="q")
        with self.assertRaises(TimeoutError):
            self.queue.wait_for_answer(rid, timeout_s=0.3, poll_interval_s=0.1)

    def test_wait_for_answer_returns_once_answered(self):
        rid = self.queue.submit(session_id="s1", question="q")

        def answer_later():
            time.sleep(0.2)
            self.queue.resolve(rid, answer="continue", resolved_by="owner")

        threading.Thread(target=answer_later).start()
        record = self.queue.wait_for_answer(rid, timeout_s=2, poll_interval_s=0.05)
        self.assertEqual(record["status"], "answered")

    def test_persists_across_instances(self):
        rid = self.queue.submit(session_id="s1", question="q")
        reopened = ConsultQueue(queue_dir=self.tmp)
        self.assertEqual(reopened.get(rid)["status"], "pending")

    def test_concurrent_submit_and_poll_never_sees_a_partial_write(self):
        """Same regression as ApprovalQueue's equivalent test — this queue copies its exact
        atomic-write pattern, so it needs the exact same proof under real concurrency."""
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
            self.queue.submit(session_id="s1", question=f"q{i}")
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [], f"list_pending() raised during concurrent writes: {errors}")


if __name__ == "__main__":
    unittest.main()
