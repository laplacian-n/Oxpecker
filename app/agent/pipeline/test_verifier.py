"""The verifier (§2.3). The independence invariant — a verifier may never run on the proposer's
own model family — is pinned first and hardest, because it is the whole reason the role exists. The
verdict parsing (all three outcomes, the closed reason list, cause_unknown coercion, honest
handling of a reply with no usable verdict) is pinned with a fake provider; a gated test runs a
real second-family model on OpenRouter against an obviously-bogus and a plausible finding.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from agent.pipeline import verifier as V
from agent.pipeline.verifier import SameModelAsProposerError, Verifier, model_family

_HAS_KEY = (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists() \
    or bool(os.environ.get("OPENROUTER_API_KEY"))


class _FakeProvider:
    def __init__(self, content):
        self.content = content
        self.last_messages = None

    def chat(self, messages, *, max_tokens, temperature):
        self.last_messages = messages
        return {"choices": [{"message": {"role": "assistant", "content": self.content}}]}


_OTHER = "anthropic/claude-3.5-sonnet"  # a family that is not openai, for the valid construction


class ModelFamilyTest(unittest.TestCase):
    def test_family_is_the_vendor_before_the_slash(self):
        self.assertEqual(model_family("openai/gpt-4o-mini"), "openai")
        self.assertEqual(model_family("deepseek/deepseek-chat"), "deepseek")

    def test_a_bare_name_is_its_own_family_and_case_is_ignored(self):
        self.assertEqual(model_family("Qwen2.5-Coder"), "qwen2.5-coder")
        self.assertEqual(model_family("OpenAI/GPT-4O"), "openai")

    def test_empty_is_empty(self):
        self.assertEqual(model_family(None), "")
        self.assertEqual(model_family(""), "")


class IndependenceInvariantTest(unittest.TestCase):
    """The §2.3/§14.3 invariant, broken first: the verifier must refuse the proposer's family."""

    def test_the_same_model_id_is_refused(self):
        with self.assertRaises(SameModelAsProposerError):
            Verifier(_FakeProvider("{}"), verifier_model="openai/gpt-4o-mini",
                     proposer_model="openai/gpt-4o-mini")

    def test_the_same_family_but_a_different_model_is_still_refused(self):
        # A different seed or a sibling model in the same family does NOT buy independence — this is
        # exactly the owner's "different family, not just a seed" decision.
        with self.assertRaises(SameModelAsProposerError):
            Verifier(_FakeProvider("{}"), verifier_model="openai/gpt-4o",
                     proposer_model="openai/gpt-4o-mini")

    def test_an_empty_verifier_family_is_refused(self):
        with self.assertRaises(SameModelAsProposerError):
            Verifier(_FakeProvider("{}"), verifier_model="", proposer_model="openai/gpt-4o-mini")

    def test_a_different_family_constructs(self):
        v = Verifier(_FakeProvider("{}"), verifier_model=_OTHER, proposer_model="openai/gpt-4o-mini")
        self.assertEqual(v.verifier_model, _OTHER)


def _verifier(content):
    return Verifier(_FakeProvider(content), verifier_model=_OTHER, proposer_model="openai/gpt-4o-mini")


_FINDING = {"title": "Reflected XSS on /search", "severity": "high", "target": "/search",
            "description": "the q param is reflected unencoded", "demonstrated_impact": "alert(1) fired",
            "evidence": "GET /search?q=<script>alert(1)</script> -> 200, body contains it verbatim"}


class VerdictParsingTest(unittest.TestCase):
    def test_refuted_is_parsed_and_flagged(self):
        v = _verifier('{"verdict": "refuted", "reason": null, "rationale": "the payload was HTML-encoded"}')
        verdict = v.verify(_FINDING)
        self.assertEqual(verdict.verdict, V.VERDICT_REFUTED)
        self.assertTrue(verdict.refuted)
        self.assertIsNone(verdict.reason)

    def test_could_not_refute_is_a_first_class_outcome(self):
        v = _verifier('Sure: {"verdict": "could_not_refute", "reason": null, "rationale": "evidence holds"}')
        verdict = v.verify(_FINDING)
        self.assertEqual(verdict.verdict, V.VERDICT_COULD_NOT_REFUTE)
        self.assertFalse(verdict.refuted)

    def test_not_reproducible_keeps_a_valid_closed_list_reason(self):
        v = _verifier('{"verdict": "confirmed_not_reproducible", "reason": "patched", "rationale": "now 403"}')
        verdict = v.verify(_FINDING)
        self.assertEqual((verdict.verdict, verdict.reason), (V.VERDICT_NOT_REPRODUCIBLE, "patched"))

    def test_an_unknown_not_reproducible_reason_is_coerced_to_cause_unknown(self):
        # §14.2 H: never fabricate a reason; an off-list or missing one becomes cause_unknown.
        v = _verifier('{"verdict": "confirmed_not_reproducible", "reason": "the stars moved", "rationale": "x"}')
        self.assertEqual(v.verify(_FINDING).reason, "cause_unknown")

    def test_a_missing_not_reproducible_reason_becomes_cause_unknown(self):
        v = _verifier('{"verdict": "confirmed_not_reproducible", "rationale": "no longer works"}')
        self.assertEqual(v.verify(_FINDING).reason, "cause_unknown")

    def test_an_unknown_verdict_does_not_refute(self):
        # A verdict we do not understand must never be read as a refutation (which would drop a
        # real finding); it falls to could_not_refute.
        self.assertEqual(_verifier('{"verdict": "looks_bad"}').verify(_FINDING).verdict,
                         V.VERDICT_COULD_NOT_REFUTE)

    def test_a_reply_with_no_json_does_not_refute(self):
        self.assertEqual(_verifier("I think it is fine honestly").verify(_FINDING).verdict,
                         V.VERDICT_COULD_NOT_REFUTE)

    def test_invalid_json_does_not_refute(self):
        self.assertEqual(_verifier('{"verdict": "refuted",,,}').verify(_FINDING).verdict,
                         V.VERDICT_COULD_NOT_REFUTE)


class PromptTest(unittest.TestCase):
    def test_the_prompt_does_not_privilege_refutation(self):
        # The §2.7 flaw guard: a role told only "find fault" will. All three outcomes must be
        # presented as acceptable, and the model told not to invent a flaw.
        v = _verifier('{"verdict": "could_not_refute"}')
        v.verify(_FINDING)
        system = v.provider.last_messages[0]["content"]
        for outcome in V.VALID_VERDICTS:
            self.assertIn(outcome, system)
        self.assertIn("perfectly valid", system)
        self.assertIn("do NOT invent", system)
        for reason in V.NOT_REPRODUCIBLE_REASONS:
            self.assertIn(reason, system)

    def test_the_digest_states_when_there_is_no_evidence(self):
        v = _verifier('{"verdict": "could_not_refute"}')
        v.verify({"title": "t", "severity": "low", "target": "/", "description": "d"})
        user = v.provider.last_messages[1]["content"]
        self.assertIn("none provided", user)


@unittest.skipUnless(_HAS_KEY, "no OpenRouter key — skipping the live cross-family verifier")
class LiveVerifierTest(unittest.TestCase):
    # A second family, distinct from the strategist/worker's openai/gpt-4o-mini, so the independence
    # invariant is real on the live path, not just in construction.
    VERIFIER_MODEL = "deepseek/deepseek-chat"

    def _live_verifier(self):
        from agent.llm.openrouter import OpenRouterProvider

        return Verifier(OpenRouterProvider(model=self.VERIFIER_MODEL),
                        verifier_model=self.VERIFIER_MODEL, proposer_model="openai/gpt-4o-mini")

    def test_a_real_cross_family_model_returns_a_valid_verdict(self):
        verdict = self._live_verifier().verify(_FINDING)
        self.assertIn(verdict.verdict, V.VALID_VERDICTS)
        if verdict.verdict == V.VERDICT_NOT_REPRODUCIBLE:
            self.assertIn(verdict.reason, V.NOT_REPRODUCIBLE_REASONS)

    def test_an_evidence_free_overclaim_tends_to_be_refuted(self):
        # A finding whose "evidence" contradicts the claim should be refutable by an independent
        # model; assert it does not come back as a confident confirmation we would submit.
        bogus = {"title": "Remote code execution on /ping", "severity": "critical", "target": "/ping",
                 "description": "the host is fully compromised via RCE",
                 "demonstrated_impact": "full shell",
                 "evidence": "GET /ping -> 200 OK, body: \"pong\". Nothing else was observed."}
        verdict = self._live_verifier().verify(bogus)
        self.assertIn(verdict.verdict, V.VALID_VERDICTS)
        self.assertEqual(verdict.verdict, V.VERDICT_REFUTED, f"expected refutation, got {verdict!r}")


if __name__ == "__main__":
    unittest.main()
