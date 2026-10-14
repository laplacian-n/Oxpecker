"""Promote confirmed/supported hypotheses into findings (AGENT_ARCHITECTURE.md §2.3, §2.4).

A graph-tier autonomous run records its conclusions on the hypothesis graph: a worker tests a
hypothesis and writes a *verdict* (confirmed/supported/refuted/...). But the report
(agent/findings/report.py, rendered from `FindingsStore.list_all()`) and the finding gate
(agent/pipeline/finding_gate.py, which verifies `FindingsStore` candidates) both read the
*findings* store, not the graph. Nothing bridged the two, so a run that confirmed real
weaknesses still produced a report with "0 finding(s)" — the graph knew, the report did not.

This is that bridge, and it is deterministic on purpose (the same reliability-over-elegance call
as the recon seeder, recon_seed.py): a confirmed or supported hypothesis becomes a candidate
finding in code, model-independent, so the report is populated by what the engagement actually
established rather than by whether a worker also remembered to call record_finding.

It is *promotion to a candidate*, not self-promotion past the gate (§14.1 A): the finding lands at
the conservative floor — `confirmed_by: model`, `status: needs_validation` — exactly where
record_finding would put it. The finding gate (a different-family verifier) and the human-review
gate still sit downstream; the submission boundary (§8.6.5 #1) is unchanged. All this step does is
make sure the thing the graph proved is in the store the report and the gate read.

Idempotent: a finding carries the id of the hypothesis it came from (`source_hypothesis_id`), so
re-running the VALIDATION->REPORT boundary never double-promotes.
"""
from __future__ import annotations

import logging

from ..findings.model import Finding, FindingsStore

log = logging.getLogger("agent.pipeline.finding_promotion")

# The verdicts worth a finding. 'confirmed' is a demonstrated weakness; 'supported' is one the
# evidence leans toward but did not fully demonstrate — both belong in the report (the finding's
# own confidence field records which), where 'refuted'/'inconclusive'/'unassessed' do not.
PROMOTABLE_VERDICTS = ("confirmed", "supported")

# impact (1..5, the graph's severity-of-the-claim) -> the finding severity vocabulary.
_IMPACT_TO_SEVERITY = {1: "info", 2: "low", 3: "medium", 4: "high", 5: "critical"}


def _severity_for(impact) -> str:
    try:
        return _IMPACT_TO_SEVERITY[max(1, min(5, int(impact)))]
    except (TypeError, ValueError):
        return "medium"  # an unreadable impact is a real finding at an honest middle, not dropped


def _confidence_for(verdict: str) -> str:
    # Never 'confirmed' — that would be the finder promoting its own assurance (§14.1 A). A
    # confirmed-verdict hypothesis is a tested-but-not-independently-verified claim ('hypothesis');
    # a supported one is weaker still ('needs_validation'). The gate/human raise it from here.
    return "hypothesis" if verdict == "confirmed" else "needs_validation"


def _describe(hypothesis: dict, observations: list[dict]) -> str:
    parts = [hypothesis.get("claim", "").strip()]
    rationale = (hypothesis.get("rationale") or "").strip()
    if rationale:
        parts.append(f"Rationale: {rationale}")
    reason = (hypothesis.get("confidence_reason") or "").strip()
    if reason:
        parts.append(f"Confidence: {reason}")
    supporting = [o.get("summary", "").strip() for o in observations
                  if o.get("polarity") == "supports" and o.get("summary")]
    if supporting:
        parts.append("Evidence:\n" + "\n".join(f"- {s}" for s in supporting))
    return "\n\n".join(p for p in parts if p)


def _demonstrated_impact(observations: list[dict]) -> str | None:
    strong = [o.get("summary", "").strip() for o in observations
              if o.get("polarity") == "supports" and o.get("strength") == "strong" and o.get("summary")]
    return "; ".join(strong) or None


def promote_hypotheses_to_findings(
    graph_store, findings_store: FindingsStore, *, engagement_id: str, session_id: str,
    default_target: str = "",
) -> list[dict]:
    """Create a candidate finding for every confirmed/supported hypothesis that does not already
    have one. Returns one record per finding created (finding_id, hypothesis_id, severity), so the
    caller can report what it promoted. Best-effort per hypothesis: one that cannot be turned into
    a valid finding is logged and skipped, never sinking the others or the run."""
    already = {f.source_hypothesis_id for f in findings_store.list_all() if f.source_hypothesis_id}
    created: list[dict] = []
    for h in graph_store.list_hypotheses():
        if h.get("verdict") not in PROMOTABLE_VERDICTS:
            continue
        hid = h["hypothesis_id"]
        if hid in already:
            continue  # idempotent — already promoted on an earlier pass
        try:
            observations = graph_store.list_observations(hid)
        except Exception:  # noqa: BLE001 — a store read failure must not drop the finding
            observations = []
        verdict = h.get("verdict")
        target = (h.get("surface") or "").strip() or default_target or engagement_id
        try:
            finding = Finding(
                title=h.get("title") or h.get("claim") or "untitled hypothesis",
                severity=_severity_for(h.get("impact")),
                target=target,
                description=_describe(h, observations) or (h.get("claim") or "").strip() or "(no detail)",
                remediation="Remediate the weakness described above; confirm with a retest.",
                tool="hypothesis-graph",
                session_id=session_id,
                engagement_id=engagement_id,
                confidence=_confidence_for(verdict),
                status="needs_validation",
                demonstrated_impact=_demonstrated_impact(observations),
                observation_refs=[o["observation_id"] for o in observations if o.get("observation_id")],
                source_hypothesis_id=hid,
            )
        except (ValueError, KeyError) as e:
            log.warning("could not promote hypothesis %s to a finding: %s", hid, e)
            continue
        findings_store.add(finding)
        created.append({
            "finding_id": finding.finding_id, "hypothesis_id": hid, "severity": finding.severity,
            "verdict": verdict,
        })
    return created
