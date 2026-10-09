"""The strategist (§2.1/§2.7). The deterministic parts — digest, parsing, validation, ordinal
resolution, the §14.2 N omission note — are pinned with a fake provider; a gated test runs a real
model and asserts it returns a usable, valid dispatch.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent.hypothesis_graph.store import HypothesisGraphStore
from agent.pipeline.strategist import Strategist

_HAS_KEY = (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists() \
    or bool(os.environ.get("OPENROUTER_API_KEY"))


def _hyp(store, title, claim="c", surface=None):
    return store.create_hypothesis(
        title=title, claim=claim, phase_created="ANALYSIS", rationale="r",
        origin_type="tool_observation", impact=3, confidence_band="medium",
        confidence_reason="x", surface=surface,
    )


class FakeProvider:
    def __init__(self, content):
        self.content = content
        self.last_messages = None

    def chat(self, messages, *, max_tokens, temperature):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": self.content}}]}


class StrategistParseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = HypothesisGraphStore(Path(self._tmp.name) / "eng")
        self.h1 = _hyp(self.store, "SQLi on /login", surface="/login")
        self.h2 = _hyp(self.store, "IDOR on /orders")

    def tearDown(self):
        self._tmp.cleanup()

    def _ordinal(self, hid):
        return self.store.get_hypothesis(hid)["ordinal"]

    def test_valid_json_resolves_to_hypothesis_id_method_pairs(self):
        o1, o2 = self._ordinal(self.h1), self._ordinal(self.h2)
        provider = FakeProvider(
            f'Here you go: [{{"hypothesis": {o1}, "method": "error-based"}}, '
            f'{{"hypothesis": {o2}, "method": "idor-swap"}}]'
        )
        pairs = Strategist(self.store, provider).decide()
        self.assertEqual(pairs, [(self.h1, "error-based"), (self.h2, "idor-swap")])

    def test_unknown_ordinals_and_empty_methods_are_dropped(self):
        o1 = self._ordinal(self.h1)
        provider = FakeProvider(
            f'[{{"hypothesis": {o1}, "method": "ok"}}, {{"hypothesis": 999, "method": "x"}}, '
            f'{{"hypothesis": {o1}, "method": ""}}]'
        )
        self.assertEqual(Strategist(self.store, provider).decide(), [(self.h1, "ok")])

    def test_dispatch_is_capped_at_max_experiments(self):
        o1 = self._ordinal(self.h1)
        provider = FakeProvider(
            "[" + ",".join(f'{{"hypothesis": {o1}, "method": "m{i}"}}' for i in range(10)) + "]"
        )
        self.assertEqual(len(Strategist(self.store, provider, max_experiments=3).decide()), 3)

    def test_a_reply_with_no_json_dispatches_nothing_rather_than_guessing(self):
        self.assertEqual(Strategist(self.store, FakeProvider("I think we should probe login.")).decide(), [])

    def test_no_open_hypotheses_means_no_model_call(self):
        # park both so there are no candidates; the provider must not be called.
        for hid in (self.h1, self.h2):
            h = self.store.get_hypothesis(hid)
            self.store.set_lifecycle_status(hid, h["version"], "parked", reason="done")
        provider = FakeProvider("[]")
        self.assertEqual(Strategist(self.store, provider).decide(), [])
        self.assertIsNone(provider.last_messages)  # never called

    def test_the_digest_states_what_it_omits(self):
        # abandon one so it is not a candidate but counts as omitted (§14.2 N).
        h = self.store.get_hypothesis(self.h2)
        self.store.set_lifecycle_status(self.h2, h["version"], "abandoned", reason="dupe")
        provider = FakeProvider("[]")
        Strategist(self.store, provider).decide()
        digest = provider.last_messages[1]["content"]
        self.assertIn("not shown", digest)
        self.assertIn("H-" + str(self._ordinal(self.h1)), digest)  # the open one is shown


@unittest.skipUnless(_HAS_KEY, "no OpenRouter key — skipping the live strategist call")
class LiveStrategistTest(unittest.TestCase):
    def test_a_real_model_returns_a_usable_dispatch(self):
        from agent.llm.openrouter import OpenRouterProvider

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = HypothesisGraphStore(Path(tmp.name) / "eng")
        h1 = _hyp(store, "SQL injection on the login form", claim="the login form is injectable", surface="/login")
        _hyp(store, "IDOR on the orders API", claim="order ids are not authorised", surface="/api/orders")

        pairs = Strategist(store, OpenRouterProvider(model="openai/gpt-4o-mini"), max_experiments=3).decide()
        self.assertTrue(pairs, "the real strategist returned no dispatch")
        valid_ids = {h["hypothesis_id"] for h in store.list_hypotheses()}
        for hid, method in pairs:
            self.assertIn(hid, valid_ids)   # only real hypotheses
            self.assertTrue(method.strip())  # with a concrete method
        self.assertLessEqual(len(pairs), 3)


if __name__ == "__main__":
    unittest.main()
