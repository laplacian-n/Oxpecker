"""The in-process listener hook on the engagement event log (agent/engagement/emit.py).

It exists so a surface in the web process can mirror live wave/worker events into a session chat
during an autonomous run without polling the log — the fix for the autonomous chat going silent
mid-run while the wave engine was busy. Two properties matter and are pinned here: a listener sees
every successful emit with its sequence number, and a listener that raises is swallowed so it can
never break the producer whose telemetry it is observing (the same guarantee the append itself
makes).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ..isolated_state import isolated_state
from . import emit


class EmitListenerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="emit-listener-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self._also = {"ENGAGEMENTS_ROOT": self.tmp / "engagements"}
        # Defensive: never let a listener a test registers survive into the next one.
        self.addCleanup(emit._listeners.clear)

    def test_a_listener_sees_every_emit_with_its_sequence(self):
        seen: list[tuple] = []
        cb = emit.subscribe(lambda eng, kind, payload, seq: seen.append((eng, kind, payload, seq)))
        try:
            with isolated_state(self.tmp, also=self._also):
                seq = emit.emit("eng-a", "worker_spawned", {"worker_id": "w0", "method": "scan"})
        finally:
            emit.unsubscribe(cb)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], ("eng-a", "worker_spawned", {"worker_id": "w0", "method": "scan"}, seq))

    def test_unsubscribe_stops_delivery(self):
        seen: list = []
        cb = emit.subscribe(lambda *a: seen.append(a))
        emit.unsubscribe(cb)
        with isolated_state(self.tmp, also=self._also):
            emit.emit("eng-a", "wave_ended", {"wave": 1})
        self.assertEqual(seen, [])

    def test_a_raising_listener_never_breaks_the_producer(self):
        def boom(*_a):
            raise RuntimeError("listener blew up")

        emit.subscribe(boom)
        with isolated_state(self.tmp, also=self._also):
            seq = emit.emit("eng-a", "wave_started", {"wave": 1, "experiments": []})
        # The append still succeeded and returned its sequence number despite the bad listener.
        self.assertIsNotNone(seq)


if __name__ == "__main__":
    unittest.main()
