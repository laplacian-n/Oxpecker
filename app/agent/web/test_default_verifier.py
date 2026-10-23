"""An autonomous OpenRouter run gets a finding-gate verifier of a different family by default.

MCP/UI-created engagements set no `verifier` in roe.json, so the finding gate skipped and findings
reached the report merely model-confirmed. dev_server now fills a default verifier at pipeline
start — a different family from the run model, or none at all (never a same-family one the gate
would refuse). This pins the selection and that its result actually satisfies the §2.3 independence
check the Verifier enforces at construction.
"""
from __future__ import annotations

import unittest

from .. import config
from ..pipeline.verifier import SameModelAsProposerError, Verifier, model_family
from . import dev_server


class DefaultVerifierTest(unittest.TestCase):
    def test_deepseek_run_gets_a_different_family_verifier(self):
        v = dev_server._default_verifier_for("deepseek/deepseek-v4-pro")
        self.assertIsNotNone(v)
        self.assertEqual(v["provider"], "openrouter")
        self.assertNotEqual(model_family(v["model"]), "deepseek")

    def test_the_chosen_verifier_passes_the_independence_check(self):
        # The whole point: the selected model must be constructible as a Verifier against the
        # proposer without tripping SameModelAsProposerError.
        model = "deepseek/deepseek-v4-pro"
        v = dev_server._default_verifier_for(model)
        # A fake provider — construction is where the family invariant is enforced, not at call.
        Verifier(object(), verifier_model=v["model"], proposer_model=model)

    def test_a_same_family_pair_would_be_refused(self):
        # Guards the invariant the selector exists to respect: verifying openai with openai raises.
        with self.assertRaises(SameModelAsProposerError):
            Verifier(object(), verifier_model="openai/gpt-4o-mini", proposer_model="openai/gpt-4o")

    def test_picks_an_alternate_when_the_first_candidate_matches_the_proposer(self):
        # If the proposer were the first candidate's family, the selector must skip to another.
        first_family = model_family(config.AUTONOMOUS_VERIFIER_CANDIDATES[0])
        proposer = f"{first_family}/some-model"
        v = dev_server._default_verifier_for(proposer)
        self.assertIsNotNone(v)
        self.assertNotEqual(model_family(v["model"]), first_family)

    def test_candidates_are_three_distinct_families(self):
        families = {model_family(m) for m in config.AUTONOMOUS_VERIFIER_CANDIDATES}
        self.assertEqual(len(families), len(config.AUTONOMOUS_VERIFIER_CANDIDATES),
                         "candidates must be distinct families so one always differs from a proposer")


if __name__ == "__main__":
    unittest.main()
