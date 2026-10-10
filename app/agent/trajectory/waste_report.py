"""Waste report — the step-7 measurement over the trajectory store (§7.1 / §2.7 / §8.2).

"Turns that produced nothing are the waste." An experiment is waste when it made no graph write,
no new observation and no verdict (`made_progress is False`). A strategist turn is waste when it
spends and dispatches nothing: the §2.7 proportionality smell. This module turns the raw records
into the totals that §8.2's "per dollar" and "per wall-clock hour" metrics divide by, so the
numbers here are the denominators and numerators of those metrics.

This is a pure read. It never writes to the store, never emits events and never mutates a record.
"""
from __future__ import annotations

from .store import TrajectoryStore

UNATTRIBUTED = "unattributed"


def waste_report(store: TrajectoryStore) -> dict:
    experiments = store.experiments()
    strategist_turns = store.strategist_turns()

    total_cost = sum(e.cost for e in experiments) + sum(t.cost for t in strategist_turns)
    total_wall_clock_s = sum(e.wall_clock_s for e in experiments) + sum(
        t.wall_clock_s for t in strategist_turns
    )

    wasted = [e for e in experiments if e.made_progress is False]
    wasted_cost = sum(e.cost for e in wasted)
    wasted_wall_clock_s = sum(e.wall_clock_s for e in wasted)

    by_provenance: dict[str, dict[str, float]] = {}
    for e in experiments:
        key = e.provenance or UNATTRIBUTED
        bucket = by_provenance.setdefault(key, {"cost": 0.0, "wasted_cost": 0.0})
        bucket["cost"] += e.cost
        if e.made_progress is False:
            bucket["wasted_cost"] += e.cost

    idle = [t for t in strategist_turns if not t.chosen and t.cost > 0]

    return {
        "total_cost": total_cost,
        "total_wall_clock_s": total_wall_clock_s,
        "wasted_cost": wasted_cost,
        "wasted_wall_clock_s": wasted_wall_clock_s,
        "waste_fraction": wasted_cost / total_cost if total_cost > 0 else None,
        "by_provenance": by_provenance,
        "strategist_spend_no_dispatch": {
            "count": len(idle),
            "cost": sum(t.cost for t in idle),
        },
        "query_action_ratio": store.query_action_ratio(),
        "experiment_count": len(experiments),
        "wasted_experiment_count": len(wasted),
    }
