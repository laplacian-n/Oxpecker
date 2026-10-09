"""The finding gate (§2.3/§2.4). The verdict->status mapping, candidate selection, idempotency and
per-finding error isolation are pinned with a scripted provider; roe.json reading and the builder's
independence invariant are pinned directly. A gated test runs the real cross-family verifier over a
real finding on OpenRouter.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.findings.model import Finding, FindingsStore
from agent.pipeline import finding_gate as FG
from agent.pipeline.finding_gate import (
    FindingGate,
    NoVerifierConfiguredError,
    build_finding_gate,
    verifier_config,
)
from agent.pipeline.verifier import SameModelAsProposerError, Verifier

_HAS_KEY = (Path(__file__).resolve().parents[1] / "state" / "openrouter_api_key.txt").exists() \
    or bool(os.environ.get("OPENROUTER_API_KEY"))

_OTHER = "anthropic/claude-3.5-sonnet"
_PROPOSER = "openai/gpt-4o-mini"


class _ScriptedProvider:
    """Returns queued replies in order; a queued Exception is raised instead (to test the gate's
    per-finding error isolation)."""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def chat(self, messages, *, max_tokens, temperature):
        self.calls += 1
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": {"role": "assistant", "content": reply}}]}


def _verdict_json(verdict, reason=None, rationale="because"):
    return json.dumps({"verdict": verdict, "reason": reason, "rationale": rationale})


def _gate(store, replies, verified_by="verifier"):
    verifier = Verifier(_ScriptedProvider(replies), verifier_model=_OTHER, proposer_model=_PROPOSER)
    return FindingGate(store, verifier, verified_by=verified_by)


def _finding(engagement_id, title="XSS on /search", status="needs_validation", **kw):
    return Finding(
        title=title, severity="high", target="/search", description="reflected unencoded",
        remediation="encode output", tool="probe", session_id="s", engagement_id=engagement_id,
        status=status, **kw,
    )


class FindingGateApplyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.eng = "eng-gate"
        self.store = FindingsStore(self.eng, findings_dir=self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_refutation_shelves_the_finding_as_false_positive(self):
        f = self.store.add(_finding(self.eng))
        results = _gate(self.store, [_verdict_json("refuted", rationale="payload was encoded")]).run()
        self.assertEqual(results[0]["verdict"], "refuted")
        after = self.store.get(f.finding_id)
        self.assertEqual(after.status, "false_positive")
        self.assertIn("verifier:refuted", after.limitations)
        self.assertEqual(after.verifier, "verifier")
        self.assertIsNotNone(after.last_verified)
        self.assertIsNone(after.reviewed_by)  # a verifier pass is NOT a human review

    def test_could_not_refute_leaves_status_but_records_the_verdict(self):
        f = self.store.add(_finding(self.eng, status="confirmed"))
        _gate(self.store, [_verdict_json("could_not_refute")]).run()
        after = self.store.get(f.finding_id)
        self.assertEqual(after.status, "confirmed")  # unchanged — stands at its floor
        self.assertIn("verifier:could_not_refute", after.limitations)
        self.assertIsNotNone(after.last_verified)

    def test_not_reproducible_keeps_the_finding_and_records_the_reason(self):
        f = self.store.add(_finding(self.eng, status="confirmed"))
        _gate(self.store, [_verdict_json("confirmed_not_reproducible", reason="patched")]).run()
        after = self.store.get(f.finding_id)
        self.assertEqual(after.status, "confirmed")  # the raw evidence is the authority (§14.2 H)
        self.assertIn("verifier:confirmed_not_reproducible/patched", after.limitations)

    def test_only_unverified_unshelved_findings_are_candidates(self):
        verified = self.store.add(_finding(self.eng, title="already", last_verified=123.0))
        shelved = self.store.add(_finding(self.eng, title="dup", status="false_positive"))
        fresh = self.store.add(_finding(self.eng, title="fresh"))
        # one reply is enough — only `fresh` should be a candidate, so only one verify call.
        gate = _gate(self.store, [_verdict_json("could_not_refute")])
        results = gate.run()
        self.assertEqual([r["finding_id"] for r in results], [fresh.finding_id])
        self.assertEqual(gate.verifier.provider.calls, 1)
        # the verified and shelved ones are untouched
        self.assertEqual(self.store.get(verified.finding_id).last_verified, 123.0)
        self.assertEqual(self.store.get(shelved.finding_id).status, "false_positive")

    def test_the_gate_is_idempotent(self):
        f = self.store.add(_finding(self.eng))
        _gate(self.store, [_verdict_json("could_not_refute")]).run()
        # second run: the finding now has last_verified, so it is no longer a candidate — a gate
        # with an EMPTY reply queue must do nothing rather than raise (no candidate to verify).
        second = _gate(self.store, []).run()
        self.assertEqual(second, [])

    def test_one_findings_verifier_error_does_not_sink_the_others(self):
        a = self.store.add(_finding(self.eng, title="a"))
        b = self.store.add(_finding(self.eng, title="b"))
        # first finding's call raises; second succeeds. Both must be in the results, and the first
        # left untouched (no false verdict written).
        results = _gate(self.store, [RuntimeError("provider 500"), _verdict_json("refuted")]).run()
        self.assertEqual(len(results), 2)
        self.assertIn("error", results[0])
        self.assertIsNone(self.store.get(a.finding_id).last_verified)  # untouched
        self.assertEqual(self.store.get(b.finding_id).status, "false_positive")


class VerifierConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _roe(self, engagement_id, obj):
        eng = self.root / engagement_id
        eng.mkdir(parents=True, exist_ok=True)
        (eng / "roe.json").write_text(json.dumps(obj))

    def test_reads_the_verifier_object(self):
        self._roe("e", {"verifier": {"provider": "openrouter", "model": "deepseek/deepseek-chat"}})
        self.assertEqual(verifier_config("e", engagements_root=self.root)["model"], "deepseek/deepseek-chat")

    def test_absent_or_modelless_or_missing_roe_is_none(self):
        self._roe("no-key", {"tier": "high"})
        self._roe("no-model", {"verifier": {"provider": "openrouter"}})
        self.assertIsNone(verifier_config("no-key", engagements_root=self.root))
        self.assertIsNone(verifier_config("no-model", engagements_root=self.root))
        self.assertIsNone(verifier_config("absent", engagements_root=self.root))


class BuildFindingGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.findings = self.root / "findings"

    def tearDown(self):
        self._tmp.cleanup()

    def _roe(self, engagement_id, obj):
        eng = self.root / engagement_id
        eng.mkdir(parents=True, exist_ok=True)
        (eng / "roe.json").write_text(json.dumps(obj))

    def test_no_verifier_option_raises_rather_than_skipping_verification(self):
        self._roe("e", {"tier": "high"})
        with self.assertRaises(NoVerifierConfiguredError):
            build_finding_gate("e", proposer_model=_PROPOSER, provider=object(),
                               engagements_root=self.root, findings_dir=self.findings)

    def test_a_verifier_on_the_proposers_family_is_refused(self):
        # roe names an openai verifier; the workers are openai too -> no independence, must raise.
        self._roe("e", {"verifier": {"model": "openai/gpt-4o"}})
        with self.assertRaises(SameModelAsProposerError):
            build_finding_gate("e", proposer_model=_PROPOSER, provider=object(),
                               engagements_root=self.root, findings_dir=self.findings)

    def test_builds_a_gate_for_a_different_family(self):
        self._roe("e", {"verifier": {"model": "deepseek/deepseek-chat"}})
        gate = build_finding_gate("e", proposer_model=_PROPOSER, provider=object(),
                                  engagements_root=self.root, findings_dir=self.findings)
        self.assertIsInstance(gate, FindingGate)
        self.assertEqual(gate.verifier.verifier_model, "deepseek/deepseek-chat")


@unittest.skipUnless(_HAS_KEY, "no OpenRouter key — skipping the live finding gate")
class LiveFindingGateTest(unittest.TestCase):
    def test_a_real_cross_family_verifier_judges_a_real_finding(self):
        from agent.llm.openrouter import OpenRouterProvider

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        store = FindingsStore("live-gate", findings_dir=d)
        bogus = store.add(Finding(
            # the "evidence" in the description contradicts the RCE claim, so an independent model
            # can refute it from what it is shown.
            title="Remote code execution on /ping", severity="critical", target="/ping",
            description="claims full compromise via RCE; evidence: GET /ping -> 200 'pong', "
                        "nothing else observed",
            remediation="patch", tool="probe", session_id="s", engagement_id="live-gate",
            status="confirmed", demonstrated_impact="full shell",
        ))
        gate = FindingGate(
            store,
            Verifier(OpenRouterProvider(model="deepseek/deepseek-chat"),
                     verifier_model="deepseek/deepseek-chat", proposer_model="openai/gpt-4o-mini"),
        )
        results = gate.run()
        self.assertEqual(len(results), 1)
        after = store.get(bogus.finding_id)
        self.assertIsNotNone(after.last_verified)  # the finding was judged
        self.assertIn(results[0]["verdict"], ("refuted", "could_not_refute", "confirmed_not_reproducible"))


if __name__ == "__main__":
    unittest.main()
