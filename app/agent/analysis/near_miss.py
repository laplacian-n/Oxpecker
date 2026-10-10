"""Near-miss view for the hypothesis graph and findings (section 8.5.3 of the design).

Everything that was not recorded as a finding needs a counter: a near-miss view is the only way
a false negative becomes observable. The ways a true finding can be lost are each already
recorded on the hypothesis graph or on the findings themselves; this module adds them up.

This is a pure function over already-listed data. It opens no store and performs no I/O; the
caller passes in ``graph_store.list_hypotheses()`` and ``findings_store.list_all()``.

The categories and the low-coverage threshold are the current best-effort set, not a settled
taxonomy. Revise them as new ways of losing a true finding are identified.
"""
from __future__ import annotations

from typing import Any, Iterable

_PARKED_STATUSES = frozenset(("parked", "awaiting_approval"))
_INCONCLUSIVE_VERDICTS = frozenset(("unassessed", "inconclusive"))


def near_miss_report(
    hypotheses: Iterable[dict],
    findings: Iterable[Any],
    *,
    low_coverage_threshold: float = 0.5,
) -> dict:
    """Count the ways a true finding may have been lost, and the weakly confirmed findings.

    ``hypotheses`` are hypothesis-graph rows (dicts with ``lifecycle_status``, ``verdict`` and
    ``coverage``; missing keys are tolerated). ``findings`` are objects exposing ``confirmed_by``
    and ``rule_disagreed``, read via ``getattr`` so plain stubs also work.

    A hypothesis whose coverage is missing is treated as coverage 0.0 for the low-coverage
    category, so an unrecorded completed-but-inconclusive hypothesis is surfaced rather than hidden.
    """
    out_of_scope = 0
    parked = 0
    blocked = 0
    refuted = 0
    exhausted_low_coverage = 0
    model_only = 0
    rule_disagreed = 0

    for h in hypotheses:
        status = h.get("lifecycle_status")
        verdict = h.get("verdict")
        if status == "abandoned":
            out_of_scope += 1
        elif status in _PARKED_STATUSES:
            parked += 1
        elif status == "blocked":
            blocked += 1
        if verdict == "refuted":
            refuted += 1
        if (
            status == "completed"
            and verdict in _INCONCLUSIVE_VERDICTS
            and (h.get("coverage") or 0.0) < low_coverage_threshold
        ):
            exhausted_low_coverage += 1

    for f in findings:
        if getattr(f, "confirmed_by", None) == "model":
            model_only += 1
        if getattr(f, "rule_disagreed", None) is not None:
            rule_disagreed += 1

    total_lost = out_of_scope + parked + blocked + refuted + exhausted_low_coverage
    return {
        "by_category": {
            "out_of_scope": out_of_scope,
            "parked": parked,
            "blocked": blocked,
            "refuted": refuted,
            "exhausted_low_coverage": exhausted_low_coverage,
            "model_only_findings": model_only,
            "rule_disagreed_findings": rule_disagreed,
        },
        "total_lost_hypotheses": total_lost,
        "weakly_confirmed_findings": model_only + rule_disagreed,
    }
