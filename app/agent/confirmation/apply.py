"""Applying a confirmation rule to a finding (§8.6.4 / §4.1).

`evaluate` is the pure half: it runs a rule's check over observations and reports the outcome.
`apply_rule_to_finding` is the glue that records that outcome on the finding store. Which rule
applies to which finding class is deliberately NOT decided here — the caller supplies the rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..findings.model import Finding, FindingsStore
from .rules import Rule, rule_ref


@dataclass(frozen=True)
class RuleOutcome:
    rule_ref: str
    matched: bool


def evaluate(rule: Rule, observations: list[dict]) -> RuleOutcome:
    """Run `rule` over `observations` and return the outcome. Pure: no I/O, no mutation."""
    return RuleOutcome(rule_ref(rule), rule.check(observations))


def apply_rule_to_finding(
    store: FindingsStore, finding_id: str, rule: Rule, observations: list[dict],
) -> Finding:
    """Evaluate `rule` against `observations` and record the outcome on `finding_id`.

    A match raises the finding to `rule` assurance; a non-match is recorded as a disagreement
    with the rule and leaves the model's call in place (see FindingsStore.record_rule_outcome)."""
    outcome = evaluate(rule, observations)
    return store.record_rule_outcome(
        finding_id, rule_ref=outcome.rule_ref, matched=outcome.matched,
    )
