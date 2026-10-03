"""Tests for the Phase 6 steer channel."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .steer import SteerChannel


class TestSteerChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="steer-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_no_message_returns_none(self):
        ch = SteerChannel("s1", steer_dir=self.tmp)
        self.assertIsNone(ch.take_pending())

    def test_send_then_take(self):
        ch = SteerChannel("s1", steer_dir=self.tmp)
        ch.send("check the /admin endpoint too", sender="owner")
        msg = ch.take_pending()
        self.assertEqual(msg, "check the /admin endpoint too")

    def test_message_consumed_only_once(self):
        ch = SteerChannel("s1", steer_dir=self.tmp)
        ch.send("hello")
        self.assertEqual(ch.take_pending(), "hello")
        self.assertIsNone(ch.take_pending())

    def test_peek_does_not_consume(self):
        ch = SteerChannel("s1", steer_dir=self.tmp)
        ch.send("hello")
        record = ch.peek_pending()
        self.assertEqual(record["message"], "hello")
        self.assertEqual(ch.take_pending(), "hello")  # still there after peek

    def test_sessions_are_independent(self):
        ch1 = SteerChannel("s1", steer_dir=self.tmp)
        ch2 = SteerChannel("s2", steer_dir=self.tmp)
        ch1.send("for session 1")
        self.assertIsNone(ch2.take_pending())
        self.assertEqual(ch1.take_pending(), "for session 1")

    def test_new_message_overwrites_unconsumed_one(self):
        ch = SteerChannel("s1", steer_dir=self.tmp)
        ch.send("first")
        ch.send("second")
        self.assertEqual(ch.take_pending(), "second")


if __name__ == "__main__":
    unittest.main()
