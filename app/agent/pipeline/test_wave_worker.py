"""The wave worker adapter maps an AgentLoop run to a WorkerResult. Tested with a fake loop
factory so the adapter's own logic — the brief, the stop wiring, the outcome mapping — is pinned
without a live model. The real loop is the default factory.
"""
from __future__ import annotations

import threading
import unittest

from agent.pipeline.wave_worker import make_agent_loop_worker, _brief
from agent.loop import TaskResult


class FakeLoop:
    def __init__(self, result, record):
        self._result = result
        self._record = record

    def run_task(self, brief):
        self._record["brief"] = brief
        return self._result

    def close(self):
        self._record["closed"] = True


def _factory_for(result, record):
    def factory(**kwargs):
        record["kwargs"] = kwargs
        return FakeLoop(result, record)
    return factory


class WaveWorkerAdapterTest(unittest.TestCase):
    def _run(self, result):
        record: dict = {}
        worker = make_agent_loop_worker("eng1", loop_factory=_factory_for(result, record))
        wr = worker("H-3", "error-based", threading.Event(), deadline=0.0)
        return wr, record

    def test_a_completed_run_maps_to_completed_and_closes_the_loop(self):
        wr, record = self._run(TaskResult("ok", "done"))
        self.assertEqual(wr.outcome, "completed")
        self.assertTrue(record["closed"])
        # the loop was built with the wave's engagement and the worker toolset.
        self.assertEqual(record["kwargs"]["engagement_id"], "eng1")
        self.assertTrue(record["kwargs"]["use_security_tools"])
        self.assertTrue(record["kwargs"]["use_hypothesis_graph"])
        # and given the stop event, so the wall-clock can interrupt it.
        self.assertIsInstance(record["kwargs"]["stop_event"], threading.Event)

    def test_a_cancelled_run_maps_to_timeout(self):
        wr, _ = self._run(TaskResult("cancelled", "stop requested"))
        self.assertEqual(wr.outcome, "timeout")

    def test_a_budget_exhausted_run_maps_to_timeout(self):
        wr, _ = self._run(TaskResult("budget_exhausted", "max iterations"))
        self.assertEqual(wr.outcome, "timeout")

    def test_an_error_run_maps_to_error(self):
        wr, _ = self._run(TaskResult("error", "boom"))
        self.assertEqual(wr.outcome, "error")

    def test_the_brief_scopes_to_one_hypothesis_and_method(self):
        b = _brief("H-3", "error-based")
        self.assertIn("H-3", b)
        self.assertIn("error-based", b)
        self.assertIn("do not branch", b)  # a worker tests one thing; siblings cover the rest

    def test_a_client_factory_is_used_to_build_the_loops_client(self):
        # This is how a worker runs on OpenRouter (no GPU): the caller supplies a client factory.
        record: dict = {}
        sentinel = object()
        worker = make_agent_loop_worker(
            "eng1",
            loop_factory=_factory_for(TaskResult("ok"), record),
            client_factory=lambda: sentinel,
        )
        worker("H-1", "m", threading.Event(), 0.0)
        self.assertIs(record["kwargs"]["client"], sentinel)

    def test_the_loop_is_closed_even_when_the_run_raises(self):
        record: dict = {}

        def factory(**kwargs):
            record["kwargs"] = kwargs
            loop = FakeLoop(None, record)
            loop.run_task = lambda brief: (_ for _ in ()).throw(RuntimeError("mid-run"))
            return loop

        worker = make_agent_loop_worker("eng1", loop_factory=factory)
        with self.assertRaises(RuntimeError):
            worker("H-1", "m", threading.Event(), 0.0)
        self.assertTrue(record["closed"])  # closed in the finally, not leaked


if __name__ == "__main__":
    unittest.main()
