"""Session event delivery after the SSE rewrite (review items B1/B2/A3).

The old model was a single `queue.Queue` per session, consumed by a busy-poll SSE handler with no
disconnect check:
  * two windows on one session each polled the SAME queue, so every event went to whichever polled
    first — the stream was SPLIT across them, neither complete;
  * a session nobody was watching still had events pushed into that queue forever — an unbounded
    buffer / memory leak;
  * the handler's `while True` never checked `request.is_disconnected()`, so a closed tab left a
    coroutine polling forever (the engagement SSE already checked; the session one did not).

Now push() fans out to one queue PER subscriber, and the handler unsubscribes on disconnect.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from . import dev_server


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class SessionFanoutTest(unittest.TestCase):
    def test_two_subscribers_each_receive_the_whole_stream(self):
        s = dev_server.Session(session_id="s1")
        a, b = s.subscribe(), s.subscribe()
        s.push({"n": 1})
        s.push({"n": 2})
        self.assertEqual([e["n"] for e in _drain(a)], [1, 2])
        self.assertEqual([e["n"] for e in _drain(b)], [1, 2], "the stream was split across subscribers")

    def test_unsubscribe_stops_delivery(self):
        s = dev_server.Session(session_id="s2")
        a = s.subscribe()
        s.unsubscribe(a)
        s.push({"n": 1})
        self.assertTrue(a.empty())
        self.assertEqual(s._subscribers, [])

    def test_a_session_nobody_watches_buffers_nothing(self):
        s = dev_server.Session(session_id="s3")
        for i in range(1000):
            s.push({"n": i})  # no subscriber → dropped, not buffered without bound
        self.assertEqual(s._subscribers, [])

    def test_append_also_fans_out_a_message_event(self):
        s = dev_server.Session(session_id="s4")
        q = s.subscribe()
        s.append("assistant", "hi")
        events = _drain(q)
        self.assertTrue(any(e.get("type") == "message" for e in events))


class SseHandlerShapeTest(unittest.TestCase):
    """Source-level pins for the parts a unit test cannot drive without a live client."""

    def setUp(self):
        self.src = (Path(__file__).resolve().parent / "dev_server.py").read_text(encoding="utf-8")

    def test_handler_subscribes_checks_disconnect_and_unsubscribes(self):
        m = re.search(r"async def stream_events\(.*?\n(.*?)\n# ── Approvals", self.src, re.S)
        self.assertIsNotNone(m, "stream_events handler not found")
        body = m.group(1)
        self.assertIn("s.subscribe()", body)
        self.assertIn("request.is_disconnected()", body)       # B1: stop on disconnect
        self.assertIn("s.unsubscribe(q)", body)                 # B1: and release the subscriber
        self.assertRegex(body, r"finally:")                     # unsubscribe runs even on error

    def test_the_dead_websocket_route_is_gone(self):
        self.assertNotIn("@app.websocket", self.src)
        self.assertNotIn("websocket_events", self.src)


if __name__ == "__main__":
    unittest.main()
