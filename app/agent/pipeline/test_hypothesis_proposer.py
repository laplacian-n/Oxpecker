"""The hypothesis proposer turns a model's JSON reply into valid hypothesis kwargs, deterministically.

The model chooses; this module parses. So the tests pin the parse: a well-formed reply becomes
create_hypothesis-shaped kwargs (the same contract recon_seed returns on), duplicates and malformed
items are dropped, the cap holds, and junk/empty replies yield [] rather than a guess — the
deterministic seeder has already guaranteed a testable graph, so proposing nothing is valid.
"""
from __future__ import annotations

import json
import unittest

from ..hypothesis_graph.store import HypothesisGraphStore
from .hypothesis_proposer import HypothesisProposer


class _FakeProvider:
    """Returns a fixed reply; records the digest it was asked, so a test can assert the surface and
    the 'do not repeat' titles reached the model."""
    def __init__(self, reply, *more_replies):
        # One reply, or a sequence consumed across calls (so a test can make the first reply junk
        # and the retry valid). The last reply repeats once the sequence is exhausted.
        self.replies = [reply, *more_replies]
        self.calls = 0
        self.user_messages = []

    def chat(self, messages, **kwargs):
        self.user_messages.append(messages[-1]["content"])
        r = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return {"choices": [{"message": {"content": r}}]}

    @property
    def last_user(self):
        return self.user_messages[-1] if self.user_messages else None


class ParseTest(unittest.TestCase):
    def test_a_well_formed_reply_becomes_hypothesis_kwargs(self):
        reply = json.dumps([
            {"title": "IDOR on track-order", "claim": "track-order/{id} returns others' orders",
             "surface": "/rest/track-order/{id}", "impact": 4},
        ])
        out = HypothesisProposer(_FakeProvider(reply)).propose("juice.shop", ["/rest/track-order/{id}"])
        self.assertEqual(len(out), 1)
        kw = out[0]
        self.assertEqual(kw["origin_type"], "ai_inference")
        self.assertEqual(kw["phase_created"], "RECON")
        self.assertEqual(kw["impact"], 4)
        self.assertTrue(kw["rationale"] and kw["confidence_reason"])
        self.assertIn(kw["confidence_band"], ("low", "medium", "high"))

    def test_duplicates_and_blanks_are_dropped(self):
        reply = json.dumps([
            {"title": "Known one", "claim": "x"},           # dup of an existing title
            {"title": "", "claim": "no title"},             # blank title
            {"title": "New", "claim": ""},                  # blank claim
            {"title": "Real", "claim": "a real claim", "impact": 2},
        ])
        out = HypothesisProposer(_FakeProvider(reply)).propose(
            "t", [], existing_titles=["known one"])
        self.assertEqual([h["title"] for h in out], ["Real"])

    def test_the_cap_holds(self):
        reply = json.dumps([{"title": f"H{i}", "claim": f"c{i}"} for i in range(20)])
        out = HypothesisProposer(_FakeProvider(reply), max_hypotheses=3).propose("t", [])
        self.assertEqual(len(out), 3)

    def test_junk_and_empty_yield_nothing(self):
        for reply in ("no json here", "{not an array}", "[]", ""):
            self.assertEqual(HypothesisProposer(_FakeProvider(reply)).propose("t", []), [])

    def test_bad_impact_defaults_to_mid(self):
        reply = json.dumps([{"title": "H", "claim": "c", "impact": "high"}])
        out = HypothesisProposer(_FakeProvider(reply)).propose("t", [])
        self.assertEqual(out[0]["impact"], 3)

    def test_the_digest_carries_surface_and_do_not_repeat(self):
        prov = _FakeProvider("[]")
        HypothesisProposer(prov).propose("juice.shop", ["/login form"],
                                         existing_titles=["Missing CSP header"])
        first = prov.user_messages[0]  # the digest, before any retry reprompt
        self.assertIn("/login form", first)
        self.assertIn("Missing CSP header", first)


class RobustParseTest(unittest.TestCase):
    """A reasoning model (DeepSeek V4 live) does not always return a bare array — it wraps it in an
    object, a code fence, or prose, or spends its whole budget thinking and returns nothing the
    first time. These are the shapes that made the proposer come back empty against Nextcloud."""

    def test_array_inside_a_code_fence(self):
        reply = 'Here are the hypotheses:\n```json\n[{"title":"A","claim":"c"}]\n```'
        out = HypothesisProposer(_FakeProvider(reply)).propose("t", [])
        self.assertEqual([h["title"] for h in out], ["A"])

    def test_object_wrapping_the_array(self):
        reply = '{"hypotheses": [{"title":"SSRF","claim":"url param is fetched server-side"}]}'
        out = HypothesisProposer(_FakeProvider(reply)).propose("t", [])
        self.assertEqual([h["title"] for h in out], ["SSRF"])

    def test_a_single_hypothesis_object(self):
        reply = '{"title":"Lonely","claim":"just one idea","impact":4}'
        out = HypothesisProposer(_FakeProvider(reply)).propose("t", [])
        self.assertEqual([h["title"] for h in out], ["Lonely"])

    def test_empty_first_reply_then_a_good_retry(self):
        # The live failure mode: first pass returns empty content; the terse retry yields the array.
        prov = _FakeProvider("", json.dumps([{"title": "Found on retry", "claim": "c"}]))
        out = HypothesisProposer(prov).propose("t", [])
        self.assertEqual([h["title"] for h in out], ["Found on retry"])
        self.assertEqual(prov.calls, 2)  # it actually retried

    def test_no_retry_when_the_first_reply_is_good(self):
        prov = _FakeProvider(json.dumps([{"title": "A", "claim": "c"}]))
        HypothesisProposer(prov).propose("t", [])
        self.assertEqual(prov.calls, 1)  # a usable first reply is not second-guessed

    def test_still_empty_after_retry_is_fine(self):
        prov = _FakeProvider("no json", "still no json")
        self.assertEqual(HypothesisProposer(prov).propose("t", []), [])
        self.assertEqual(prov.calls, 2)


class ConstructsInTheGraphTest(unittest.TestCase):
    def test_proposed_kwargs_create_a_real_hypothesis(self):
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="proposer-graph-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = HypothesisGraphStore(tmp / "g")
        root = store.create_hypothesis(
            title="root", claim="scope root", phase_created="RECON", rationale="r",
            origin_type="user_message", impact=1, confidence_band="low", confidence_reason="r")
        reply = json.dumps([{"title": "SSRF via url param", "claim": "the url param is fetched server-side",
                             "surface": "?url=", "impact": 5}])
        for kw in HypothesisProposer(_FakeProvider(reply)).propose("t", ["?url="]):
            hid = store.create_hypothesis(primary_parent_id=root, **kw)
            self.assertTrue(hid)
        titles = {h["title"] for h in store.list_hypotheses()}
        self.assertIn("SSRF via url param", titles)


if __name__ == "__main__":
    unittest.main()
