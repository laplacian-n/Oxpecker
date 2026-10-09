"""§2.6.1 / §12 step 0a — the line that does not move with the tier, written before the tiers do.

    A tier may remove orchestration. It may never remove the broker, the scope check, the audit
    log or the evidence store. The broker path, the audit entry and the evidence write are
    byte-for-byte identical across all three tiers.

Two guards, because each catches what the other cannot:

  * **Structural** — none of the four protected components even depends on the orchestration
    tier (operationally: none imports `agent.tiers`). This is what fails the instant someone
    writes `if tier == "low": skip_the_audit()` reachable from those modules. It is paired with
    a check that the scanner is not inert (it *does* find the marker in a module that has it).
  * **Behavioral** — the same action dispatched through the real broker under each tier produces
    a byte-identical evidence write and audit entry. This is what fails if the tier reaches those
    components by some path the import check did not see (a string, a global, a config read).

Written now, against a tier seam (`agent.tiers`) that orchestration does not yet consume, so the
assertion stands guard before there is any tier-conditional code for it to catch.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent import tiers
from agent.broker import broker as broker_mod
from agent.broker.contracts import ActionRequest
from agent.broker.policy import Policy
from agent.evidence.store import EvidenceStore

_AGENT_ROOT = Path(__file__).resolve().parent  # .../app/agent


class StructuralGuardTest(unittest.TestCase):
    # The exact dependencies that would mean a protected component can see the orchestration
    # tier. isolation_tier (the sandbox dimension) is a different concept and deliberately not
    # matched — these tokens are specific to agent.tiers.
    FORBIDDEN_TOKENS = ("agent.tiers", "from ..tiers", "from .tiers", "import tiers",
                        "ORCHESTRATION_TIERS", "active_tier", "use_tier")

    def test_the_tier_seam_exists_so_this_test_is_not_guarding_a_phantom(self):
        self.assertEqual(tiers.ORCHESTRATION_TIERS, ("low", "medium", "high"))
        self.assertEqual(tiers.PROTECTED_FROM_TIER, (
            "agent/broker/broker.py", "agent/broker/scope_check.py",
            "agent/audit_log.py", "agent/evidence/store.py",
        ))

    def test_the_scanner_is_not_inert(self):
        # If the token search could not find the marker where it certainly is, a 'not found' in
        # the protected files below would prove nothing. tiers.py itself is the positive control.
        src = (_AGENT_ROOT / "tiers.py").read_text()
        self.assertIn("ORCHESTRATION_TIERS", src)

    def test_no_protected_component_depends_on_the_orchestration_tier(self):
        for rel in tiers.PROTECTED_FROM_TIER:
            path = _AGENT_ROOT.parent / rel
            src = path.read_text()
            self.assertTrue(src.strip(), f"{rel} read as empty — the guard would be vacuous")
            for token in self.FORBIDDEN_TOKENS:
                self.assertNotIn(
                    token, src,
                    f"{rel} references the orchestration tier ({token!r}) — §2.6.1 says the "
                    "broker/scope/audit/evidence must never change with the tier",
                )


class BehavioralParityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tier-parity-"))
        self._patches = [
            patch("agent.config.AUDIT_DIR", self.tmp / "audit"),
            patch("agent.config.EVIDENCE_DIR", self.tmp / "evidence"),
            patch("agent.config.EVIDENCE_KEY_PATH", self.tmp / "key.bin"),
            patch("agent.config.IDEMPOTENCY_CACHE_PATH", self.tmp / "idem.json"),
            # The broker now emits §4.2 events to ENGAGEMENTS_ROOT/<id>/events.db on dispatch;
            # isolate it so these dispatches do not write into the real engagements dir.
            patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _policy(self) -> Policy:
        return Policy(
            engagement_id="parity",
            allowed_action_classes={"knowledge_search"},
            allow_networks=[], allow_hostnames=set(),
            deny_networks=[], deny_hostnames=set(),
            policy_version="test/parity",
            valid_until=float("inf"),
        )

    def _dispatch_under(self, tier: str) -> tuple[dict, dict]:
        """Dispatch one canonical action with `tier` active; return (response.to_dict(), audit
        entry). A fresh Broker and a tier-specific session id so the cooldown gate (keyed on
        session+class) never makes one tier's dispatch wait on another's."""
        # idempotency_cache_path is an import-time-bound default too, so the config patch does
        # not reach it; passed explicitly, both so the test is isolated and so a stale cache
        # entry from a prior run cannot short-circuit dispatch before it finalizes (which writes
        # the audit and evidence this test reads).
        broker = broker_mod.Broker(
            policy_loader=self._policy, idempotency_cache_path=self.tmp / "idem.json"
        )
        # Broker builds its EvidenceStore from import-time default paths (the same hazard), so the
        # config patches above do not reach it. Point it at the tmp key explicitly — the same file
        # AuditLog reads from the patched config at call time, so the broker's own
        # content_digest == evidence_digest cross-check holds.
        broker.evidence = EvidenceStore(
            evidence_dir=self.tmp / "evidence",
            index_path=self.tmp / "evidence_index.json",
            access_log_path=self.tmp / "evidence_access.jsonl",
            key_path=self.tmp / "key.bin",
        )
        session_id = f"parity-{tier}"
        request = ActionRequest(
            tool="knowledge_search",
            arguments={"query": "canonical parity probe"},
            session_id=session_id,
            device_id="tester",
            engagement_id="parity",
            action_id=f"act-{tier}",          # fixed so normalized_arguments/ids are comparable
            idempotency_key=f"idem-{tier}",
            turn_index=3,
            action_rationale="canonical action for the tier-parity guard",
        )
        with tiers.use_tier(tier):
            self.assertEqual(tiers.active_tier(), tier)  # the tier really is active here
            response = broker.dispatch(request, executor=lambda policy, args: {"results": ["x", "y"]})
        entries = [
            json.loads(l)
            for l in (self.tmp / "audit" / f"{session_id}.jsonl").read_text().splitlines()
            if l.strip()
        ]
        self.assertEqual(len(entries), 1)
        return response.to_dict(), entries[0]

    @staticmethod
    def _tier_independent(entry: dict) -> dict:
        """The audit entry minus the fields that vary per write by construction — the chain
        links, the id, the clock, the session name, and measured latency. What remains is what
        §2.6.1 says must not move with the tier."""
        volatile = {"entry_id", "entry_hash", "prev_hash", "timestamp", "session_id", "latency_ms"}
        return {k: v for k, v in entry.items() if k not in volatile}

    def test_the_audit_entry_is_byte_identical_across_tiers(self):
        entries = {t: self._tier_independent(self._dispatch_under(t)[1]) for t in tiers.ORCHESTRATION_TIERS}
        canonical = json.dumps(entries["low"], sort_keys=True)
        for t in ("medium", "high"):
            self.assertEqual(
                json.dumps(entries[t], sort_keys=True), canonical,
                f"the audit entry under tier {t!r} differs from low — a tier changed what gets recorded",
            )

    def test_the_evidence_write_is_byte_identical_across_tiers(self):
        # evidence_digest is the HMAC of the stored bytes under the shared key; equal digests
        # across tiers is equal evidence content. It must also equal the audit content_digest —
        # the broker's own cross-reference — under every tier.
        digests = set()
        for t in tiers.ORCHESTRATION_TIERS:
            response, entry = self._dispatch_under(t)
            self.assertEqual(response["evidence_digest"], entry["content_digest"])
            digests.add(response["evidence_digest"])
        self.assertEqual(len(digests), 1, "the evidence write differs across tiers")

    def test_the_broker_decision_is_identical_across_tiers(self):
        decisions = []
        for t in tiers.ORCHESTRATION_TIERS:
            r, _ = self._dispatch_under(t)
            decisions.append((r["status"], r["policy_rule"], r["policy_version"],
                              json.dumps(r["normalized_arguments"], sort_keys=True),
                              json.dumps(r["output"], sort_keys=True)))
        self.assertEqual(len(set(decisions)), 1, "the broker reached a different decision under some tier")


if __name__ == "__main__":
    unittest.main()
