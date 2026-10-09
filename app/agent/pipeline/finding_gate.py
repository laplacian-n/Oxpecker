"""The finding gate (AGENT_ARCHITECTURE.md §2.3, §2.4): before a candidate finding can be
submitted, an independent verifier tries to prove it wrong, and the gate records the verdict on the
finding. A false report is the most expensive thing this system can produce, so no candidate
reaches a report un-verified where a verifier is configured.

This is the thin layer between the `Verifier` (which judges one finding, and knows nothing about
the store) and the `FindingsStore` (which persists findings, and knows nothing about models). The
gate:

  * selects the *candidate* findings — those not yet verified and not already shelved
    (false_positive / wont_fix), so re-running the gate is idempotent and never re-pays for a
    finding it already judged;
  * runs the verifier on each, which is a **different model family from the proposer** — the
    `Verifier` enforces that at construction (§2.3/§14.3), and `build_finding_gate` wires the
    proposer's model in so the invariant is real on the live path, not just hoped for;
  * applies the verdict (§14.2 H's three-value space):
      - ``refuted``                 -> status ``false_positive``; the verifier did its job.
      - ``could_not_refute``        -> status unchanged; the finding stands at its floor, and the
                                       human-review gate (`reviewed_by`) still sits downstream.
      - ``confirmed_not_reproducible`` -> status unchanged (it worked when found; the raw evidence
                                       is the authority, not the re-run), but the reason is recorded
                                       so the report can say "exploitable at the time, no longer
                                       reproducing because <reason>".
    Either way the verdict, its reason and the model's rationale are written to the finding's
    `limitations`, so the report carries *why* a finding stands or fell, not just that it did.

The verifier model is an engagement option, read from roe.json (`"verifier": {"provider", "model"}`)
per the owner's decision — a different family, not merely a seed. Without that option there is no
configured verifier; whether that is allowed is the caller's call (a high tier should have one; a
tier with none labels its findings `confirmed_by: model` per §8.6.4).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import config
from ..findings.model import FindingsStore
from .verifier import (
    VERDICT_COULD_NOT_REFUTE,
    VERDICT_REFUTED,
    Verifier,
)

log = logging.getLogger("agent.pipeline.finding_gate")

# Statuses that take a finding out of the candidate set: already shelved, so not worth a verifier
# call. Everything else that has not been verified yet is a candidate.
_SHELVED_STATUSES = ("false_positive", "wont_fix")


class NoVerifierConfiguredError(ValueError):
    """roe.json has no `verifier` option. The gate cannot run; the caller decides whether that is
    allowed for this engagement's tier (a high tier should configure one)."""


def verifier_config(engagement_id: str, *, engagements_root: Path | None = None) -> dict | None:
    """The engagement's verifier model option, or None if it set none. Read from roe.json's
    `verifier` object (`{"provider": ..., "model": ...}`) — the same per-engagement roe the tier is
    read from. A malformed or absent roe yields None rather than raising, so a missing option is a
    'no verifier' decision, not a crash."""
    root = engagements_root or config.ENGAGEMENTS_ROOT
    roe_path = root / engagement_id / "roe.json"
    if not roe_path.exists():
        return None
    try:
        roe = json.loads(roe_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cfg = roe.get("verifier")
    if not isinstance(cfg, dict) or not cfg.get("model"):
        return None
    return cfg


class FindingGate:
    def __init__(self, findings_store: FindingsStore, verifier: Verifier, *, verified_by: str = "verifier"):
        self.store = findings_store
        self.verifier = verifier
        self.verified_by = verified_by

    def _candidates(self) -> list:
        return [
            f for f in self.store.list_all()
            if f.last_verified is None and f.status not in _SHELVED_STATUSES
        ]

    def run(self) -> list[dict]:
        """Verify every candidate finding and apply each verdict. Returns one record per finding
        verified (finding_id, verdict, reason, new status), so the caller can report what the gate
        did. A finding whose verification raises is left untouched and surfaced in the record with
        an ``error`` — one finding's failure never silently drops the others or the gate."""
        results: list[dict] = []
        for finding in self._candidates():
            try:
                verdict = self.verifier.verify(finding.to_dict())
            except Exception as e:  # noqa: BLE001 - one finding's model error must not sink the gate
                log.warning("verifier raised on finding %s: %s", finding.finding_id, e)
                results.append({"finding_id": finding.finding_id, "error": f"{type(e).__name__}: {e}"})
                continue
            new_status = self._status_for(verdict.verdict)
            confirmed_by = self._confirmed_by_for(verdict.verdict)
            self.store.record_verification(
                finding.finding_id, verdict=verdict.verdict, reason=verdict.reason,
                rationale=verdict.rationale, by=self.verified_by, new_status=new_status,
                confirmed_by=confirmed_by,
            )
            results.append({
                "finding_id": finding.finding_id, "verdict": verdict.verdict,
                "reason": verdict.reason, "status": new_status or finding.status,
                "confirmed_by": confirmed_by,
            })
        return results

    @staticmethod
    def _status_for(verdict: str) -> str | None:
        """The only verdict that moves a finding's status is a refutation, to false_positive. A
        'could not refute' leaves it where it was (its floor, with the human gate still downstream),
        and 'confirmed but not reproducible' keeps the finding — the raw evidence is the authority,
        not the re-run (§14.2 H) — and is recorded through `limitations`, not a status change."""
        if verdict == VERDICT_REFUTED:
            return "false_positive"
        return None  # could_not_refute / confirmed_not_reproducible: status unchanged

    @staticmethod
    def _confirmed_by_for(verdict: str) -> str | None:
        """A verifier that is a *different model family* (the gate guarantees it, §2.3) and could
        not refute the finding is a **differential** pass (§8.6.4 ladder / §14.1): an independent
        instance agreed, so the assurance rises from the `model` floor to `differential` — enough to
        cross the submission boundary on its own (§8.6.5 #1). A refutation shelves the finding
        instead, and 'not reproducible' leaves assurance where it was. record_verification only ever
        raises, so this never lowers an already-higher level."""
        if verdict == VERDICT_COULD_NOT_REFUTE:
            return "differential"
        return None


def build_finding_gate(
    engagement_id: str,
    *,
    proposer_model: str,
    provider=None,
    engagements_root: Path | None = None,
    findings_dir: Path | None = None,
) -> FindingGate:
    """Assemble the gate for an engagement: read the verifier option from roe.json, build the
    `Verifier` (which refuses the proposer's own family), and point it at the engagement's findings.

    `proposer_model` is the model the workers ran — the independence invariant is checked against
    it. `provider` is injectable so the gate is testable without a live model and so the caller
    chooses the client (OpenRouter vs. local); omitted, it is built from the registry for the
    verifier option's provider. Raises `NoVerifierConfiguredError` when roe.json names no verifier,
    so a tier that requires one fails loudly rather than skipping verification in silence."""
    cfg = verifier_config(engagement_id, engagements_root=engagements_root)
    if cfg is None:
        raise NoVerifierConfiguredError(
            f"engagement {engagement_id!r} has no 'verifier' option in roe.json; cannot build a gate"
        )
    verifier_model = cfg["model"]
    if provider is None:
        from ..llm import registry

        provider = registry.build(cfg.get("provider", registry.DEFAULT_PROVIDER), model=verifier_model)
    verifier = Verifier(provider, verifier_model=verifier_model, proposer_model=proposer_model)
    store = FindingsStore(engagement_id, findings_dir=findings_dir or config.FINDINGS_DIR)
    return FindingGate(store, verifier)
