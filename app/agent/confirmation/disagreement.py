"""Rule-disagreement counter (§8.6.4.1 / §12 row 6d).

When a confirmation rule returns False against evidence the model called positive, the finding
records `rule_disagreed = "rule_id@version"`. §8.6.4.1: "The ratio of 'rule said no, model was
right' to 'rule said no, model was wrong', per rule, is that rule's quality score. A rule that is
wrong more often than the model should be switched off — a rule worse than no rule emits
authoritative-looking False."

How a disagreement resolves is derived from the finding's eventual status and assurance, not from
any separate storage:
  - model was WRONG (rule was right): `status == "false_positive"` — later shown to be exactly what
    the rule flagged. This is decisive: it wins even if assurance was also raised.
  - model was RIGHT (rule was broken): it stood up — assurance above the model floor
    (`confirmed_by`), a human review (`reviewed_by`), or `status == "confirmed"`.
  - pending: neither of the above yet.

The thresholds (`min_resolved`, `retire_below`) are the current best-effort defaults, not measured
values; callers can override them per call.

check() is not involved here: this module only reads findings, it never mutates them.
"""
from __future__ import annotations

from typing import Iterable

from ..findings.model import CONFIRMED_BY_FLOOR, assurance_rank


def _resolution(finding) -> str:
    """'rule_right' (model wrong), 'rule_broken' (model right), or 'pending'."""
    if getattr(finding, "status", None) == "false_positive":
        return "rule_right"
    confirmed_by = getattr(finding, "confirmed_by", None) or CONFIRMED_BY_FLOOR
    raised = assurance_rank(confirmed_by) > assurance_rank(CONFIRMED_BY_FLOOR)
    if raised or getattr(finding, "reviewed_by", None) or getattr(finding, "status", None) == "confirmed":
        return "rule_broken"
    return "pending"


def rule_quality(
    findings: Iterable,
    *,
    min_resolved: int = 5,
    retire_below: float = 0.5,
) -> dict:
    """Per-rule disagreement statistics over `findings` (Finding-like objects).

    Returns {rule_ref: {disagreements, rule_right, rule_broken, pending, quality_score, retire}}.
    `quality_score` is rule_right / (rule_right + rule_broken): the fraction of resolved
    disagreements the rule was vindicated on, or None while nothing is resolved. `retire` is True
    only when the rule has at least `min_resolved` resolved disagreements AND its quality_score is
    strictly below `retire_below` — exactly at the threshold does not retire, and a rule with too
    few resolved disagreements is never retired on a small sample.
    """
    stats: dict[str, dict] = {}
    for finding in findings:
        ref = getattr(finding, "rule_disagreed", None)
        if ref is None:
            continue
        s = stats.setdefault(
            ref, {"disagreements": 0, "rule_right": 0, "rule_broken": 0, "pending": 0}
        )
        s["disagreements"] += 1
        s[_resolution(finding)] += 1

    out = {}
    for ref, s in stats.items():
        resolved = s["rule_right"] + s["rule_broken"]
        quality = s["rule_right"] / resolved if resolved > 0 else None
        retire = resolved >= min_resolved and quality is not None and quality < retire_below
        out[ref] = {**s, "quality_score": quality, "retire": retire}
    return out
