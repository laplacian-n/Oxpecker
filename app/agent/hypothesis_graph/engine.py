"""Hypothesis Graph — deterministic derived quantities. Pure functions over store rows: no LLM
judgment, no hidden state. This is where both review docs' single most important rule lives —
confidence, coverage and priority are computed and kept **separate**, never fused into one
"success %" the model sets and the UI then over-trusts.

  - coverage(h)  : progress — fraction of planned tests that have a completed experiment.
  - confidence   : belief the CLAIM is true — a band the model proposes WITH a reason from
                   evidence; the engine only *suggests* one from observation polarity/strength,
                   the service records what was actually asserted. Never rises just because more
                   attempts happened.
  - priority     : worth-doing-next — a ranking heuristic (tier + components), NEVER a
                   probability. Uses REMAINING cost, never sunk (already-spent) tokens
                   (doc 2 §8.3 — sunk cost is not a reason to keep or drop a branch).

The active path is *proposed* here (advisory) but SET by the agent with a reason in the store —
the engine never silently reroutes the investigation.
"""
from __future__ import annotations

from .schema import ConfidenceBand, LifecycleStatus, Verdict

# Confidence band -> a coarse information-value weight for priority. Medium is highest: a
# medium-confidence claim is the most worth testing (a strong clue with a real alternative
# explanation). Low = weak clue, High = already believed — both yield less *new* information from
# one more test. This is an ordering heuristic, NOT a probability.
_INFO_VALUE = {ConfidenceBand.LOW.value: 0.6, ConfidenceBand.MEDIUM.value: 1.0, ConfidenceBand.HIGH.value: 0.5}

# Lifecycle states that are actionable "next work". completed/abandoned are terminal;
# parked is intentionally-shelved (its own tier); blocked/awaiting are stalled, not choosable now.
_ACTIONABLE = {LifecycleStatus.OPEN.value, LifecycleStatus.QUEUED.value, LifecycleStatus.RUNNING.value}

_STRENGTH_WEIGHT = {"weak": 1, "moderate": 2, "strong": 3}


def coverage(hypothesis: dict, experiments: list[dict]) -> float:
    planned = max(1, hypothesis.get("planned_tests", 1))
    completed = sum(1 for x in experiments if x["status"] == "completed")
    return min(1.0, completed / planned)


def suggest_confidence_band(observations: list[dict]) -> tuple[str, str]:
    """Advisory only — returns (band, reason). The service records the model's asserted band, but
    surfaces this suggestion so a wildly divergent self-assessment is visible. Weighs supporting
    vs refuting observations by strength."""
    support = sum(_STRENGTH_WEIGHT[o["strength"]] for o in observations if o["polarity"] == "supports")
    refute = sum(_STRENGTH_WEIGHT[o["strength"]] for o in observations if o["polarity"] == "refutes")
    if support == 0 and refute == 0:
        return ConfidenceBand.LOW.value, "no direct observations yet"
    net = support - refute
    if net >= 4:
        return ConfidenceBand.HIGH.value, f"supporting evidence dominates ({support} vs {refute})"
    if net <= -2:
        return ConfidenceBand.LOW.value, f"evidence leans against the claim ({support} vs {refute})"
    return ConfidenceBand.MEDIUM.value, f"mixed or moderate evidence ({support} vs {refute})"


def priority_components(hypothesis: dict, experiments: list[dict], *, current_phase: str | None = None) -> dict:
    """Returns the component breakdown AND a normalized score for intra-tier sort. Stored as a
    snapshot whenever it changes (doc 2 §8.3) so a ranking is always explainable, never a magic
    number. Denominator uses REMAINING test cost, never spent tokens."""
    impact_norm = hypothesis["impact"] / 5.0
    info_value = _INFO_VALUE.get(hypothesis["confidence_band"], 0.6)
    cov = coverage(hypothesis, experiments)
    information_gain = 1.0 - cov  # more remaining coverage = more to learn
    phase_relevance = 1.0 if (current_phase is None or hypothesis["phase_created"] == current_phase) else 0.7
    feasibility = 0.8  # MVP constant — a real feasibility signal (auth needed? destructive?) is MVP 3
    remaining_tests = max(1, hypothesis.get("planned_tests", 1) - sum(1 for x in experiments if x["status"] == "completed"))
    remaining_cost = remaining_tests * 0.1
    numerator = phase_relevance * impact_norm * max(0.05, information_gain) * info_value * feasibility
    score = numerator / (1.0 + remaining_cost)
    return {
        "score": round(score, 4),
        "impact_norm": round(impact_norm, 3),
        "information_gain": round(information_gain, 3),
        "info_value": info_value,
        "phase_relevance": phase_relevance,
        "feasibility": feasibility,
        "remaining_cost": round(remaining_cost, 3),
    }


def priority_tier(hypothesis: dict, score: float) -> str:
    """Maps a hypothesis to a Now/Next/Later/Parked tier. Terminal/parked states never rank as
    actionable work; among actionable ones, absolute score bands (not just relative rank) so a
    genuinely weak lead doesn't get promoted to 'Now' merely for being the best of a bad lot."""
    status = hypothesis["lifecycle_status"]
    if status == LifecycleStatus.PARKED.value:
        return "parked"
    if status not in _ACTIONABLE:
        return "later"  # blocked/awaiting/completed/abandoned/draft — not choosable as next work now
    if score >= 0.35:
        return "now"
    if score >= 0.18:
        return "next"
    return "later"


# A running/queued hypothesis with no store write for this long is flagged stale (advisory only,
# UI spec §7.5). Generous because this project's hardware decodes slowly — a real attempt can
# legitimately take many minutes — but a multi-hour silent "running" is worth surfacing.
STALE_RUNNING_SECS = 3 * 3600


def stale_reason(
    hypothesis: dict, *, now: float, parent: dict | None = None,
    parent_observations: list[dict] | tuple = (),
) -> str | None:
    """Advisory staleness flag (UI spec §7.5) — NEVER mutates lifecycle_status. Returns a short
    human reason or None. Three signals, in priority order: a dependency was refuted, the parent's
    evidence moved after this node was formed, or the node has sat running/queued untouched too
    long."""
    if parent is not None:
        if parent.get("verdict") == Verdict.REFUTED.value:
            return f"depends on H-{parent['ordinal']}, which was refuted"
        for o in parent_observations:
            if o["observed_at"] > hypothesis["created_at"]:
                return f"H-{parent['ordinal']}'s evidence changed after this hypothesis was created"
    st = hypothesis["lifecycle_status"]
    if st in (LifecycleStatus.RUNNING.value, LifecycleStatus.QUEUED.value):
        idle = now - hypothesis["updated_at"]
        if idle > STALE_RUNNING_SECS:
            return f"{st} for {int(idle / 3600)}h with no update"
    return None


def rank_open(hypotheses: list[dict], experiments_by_h: dict[str, list[dict]], *, current_phase: str | None = None) -> list[dict]:
    """Returns actionable hypotheses sorted by priority score desc, each annotated with its
    components + tier. This is the decision-support list the side panel renders."""
    out = []
    for h in hypotheses:
        if h["lifecycle_status"] not in _ACTIONABLE:
            continue
        comps = priority_components(h, experiments_by_h.get(h["hypothesis_id"], []), current_phase=current_phase)
        out.append({**h, "priority": comps, "priority_tier": priority_tier(h, comps["score"])})
    out.sort(key=lambda x: x["priority"]["score"], reverse=True)
    return out


def propose_active_path(store, *, current_phase: str | None = None) -> tuple[list[str], str]:
    """ADVISORY (doc 2 §13.7): the greedy highest-priority root->leaf path the agent may adopt —
    but the agent sets the real active path with its own reason, and hysteresis/steering can
    override this. Returns (path_of_hypothesis_ids, reason)."""
    hyps = store.list_hypotheses()
    if not hyps:
        return [], "no hypotheses yet"
    by_id = {h["hypothesis_id"]: h for h in hyps}
    exps = {h["hypothesis_id"]: store.list_experiments(h["hypothesis_id"]) for h in hyps}
    roots = [h for h in hyps if h["primary_parent_id"] is None]
    if not roots:
        return [], "no root hypothesis"

    def best_child(node_id):
        kids = [h for h in hyps if h["primary_parent_id"] == node_id]
        actionable = [k for k in kids if k["lifecycle_status"] in _ACTIONABLE or
                      any(gc["primary_parent_id"] == k["hypothesis_id"] for gc in hyps)]
        if not actionable:
            return None
        return max(actionable, key=lambda k: priority_components(k, exps[k["hypothesis_id"]], current_phase=current_phase)["score"])

    root = max(roots, key=lambda r: priority_components(r, exps[r["hypothesis_id"]], current_phase=current_phase)["score"])
    path = [root["hypothesis_id"]]
    node = root["hypothesis_id"]
    while True:
        nxt = best_child(node)
        if nxt is None or nxt["hypothesis_id"] in path:
            break
        path.append(nxt["hypothesis_id"])
        node = nxt["hypothesis_id"]
    tail = by_id[path[-1]]
    return path, f"highest-priority lineage; leaf H-{tail['ordinal']} ({tail['title']})"


_VERDICT_MARK = {
    Verdict.CONFIRMED.value: "✓✓", Verdict.SUPPORTED.value: "✓", Verdict.REFUTED.value: "×",
    Verdict.INCONCLUSIVE.value: "?", Verdict.SUPERSEDED.value: "⇒", Verdict.UNASSESSED.value: "",
}


def digest_line(hypothesis: dict, experiments: list[dict]) -> str:
    """One compact line for the context digest (doc 1 §7.2 / doc 2 §13.9) — seq + phase + short
    claim + state + verdict + key evidence count. `ย่อ ≠ ตัด`: every hypothesis gets a line, the
    line is just short. The single point of failure the anti-forgetting design rests on, so it is
    built deterministically from stored fields, never written by the model after the fact."""
    ordinal = hypothesis["ordinal"]
    phase = hypothesis["phase_created"]
    status = hypothesis["lifecycle_status"]
    verdict = _VERDICT_MARK.get(hypothesis["verdict"], "")
    title = hypothesis["title"][:44]
    ev_count = sum(len(_json_len(x.get("evidence_refs_json"))) for x in experiments)
    cov = coverage(hypothesis, experiments)
    tail = f" ev:{ev_count}" if ev_count else ""
    parenthetical = ""
    if status == "parked" and hypothesis.get("park_reason"):
        parenthetical = f" [parked: {hypothesis['park_reason'][:30]}]"
    elif status == "abandoned" and hypothesis.get("abandon_reason"):
        parenthetical = f" [abandoned: {hypothesis['abandon_reason'][:30]}]"
    return (
        f"#{ordinal} {phase:10s} \"{title}\" conf:{hypothesis['confidence_band']} "
        f"cov:{cov:.0%} {status}{(' ' + verdict) if verdict else ''}{tail}{parenthetical}"
    )


def _json_len(json_str):
    import json
    try:
        return json.loads(json_str) if json_str else []
    except Exception:
        return []


def build_digest(store, *, current_phase: str | None = None, max_lines: int = 60) -> str:
    """The full-graph digest injected every turn. Root + active path are guaranteed present
    (doc 1 §7.3 — they must never fall out of context); the rest fill up to max_lines."""
    hyps = store.list_hypotheses()
    if not hyps:
        return (
            "[HYPOTHESIS GRAPH — empty]\n"
            "Record each testable claim with graph_hypothesis_add. To test one: graph_attempt_start "
            "→ do the work → graph_attempt_complete, then graph_set_verdict with the evidence."
        )
    exps = {h["hypothesis_id"]: store.list_experiments(h["hypothesis_id"]) for h in hyps}
    gstate = store.get_graph_state()
    guaranteed = set(gstate.get("active_path") or [])
    roots = [h["hypothesis_id"] for h in hyps if h["primary_parent_id"] is None]
    guaranteed.update(roots)

    lines = [f"[HYPOTHESIS GRAPH — digest, graph_version={gstate['graph_version']}]"]
    if gstate.get("active_hypothesis_id"):
        active = next((h for h in hyps if h["hypothesis_id"] == gstate["active_hypothesis_id"]), None)
        if active:
            lines.append(f"ACTIVE: H-{active['ordinal']} \"{active['title']}\" — {gstate['active_path_reason']}")

    ordered = [h for h in hyps if h["hypothesis_id"] in guaranteed]
    ordered += [h for h in hyps if h["hypothesis_id"] not in guaranteed]
    for h in ordered[:max_lines]:
        lines.append(digest_line(h, exps[h["hypothesis_id"]]))
    if len(ordered) > max_lines:
        lines.append(f"... {len(ordered) - max_lines} more (use graph_search / graph_read_branch)")
    return "\n".join(lines)
