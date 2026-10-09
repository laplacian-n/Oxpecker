"""Graph cold-start (AGENT_ARCHITECTURE.md §14.1 G): the entry path that gives the hypothesis
graph a root to grow from, so a high-tier RECON wave has something to dispatch instead of staring
at an empty graph.

The problem §14.1 G states: recon is enumeration, not a falsifiable claim, so "the strategist
creates hypotheses and workers test them" has nowhere to begin — the graph starts empty and the
strategist would be guessing at a surface nobody has looked at. The entry path is a **root per
authorized scope entry**: a claim like "enumerate the attack surface of <target>", whose recon
experiment's `observed_result` spawns the real child hypotheses (the discovered hosts, services,
endpoints), which are the falsifiable claims the rest of the engagement tests.

Three invariants this module holds, each from the owner's review of the cold-start design:

  1. **A root comes only from a RoE scope entry — never from a host recon discovers.** If a worker
     finds a new host and the system auto-rooted it, we would have rooted the whole engagement's
     work on something that may be out of scope. A discovered host is a *child* under the root of
     the asset that revealed it, and must clear `validate_target` before any experiment. So the
     set of roots equals the set of authorized scope entries, always — `assert_roots_match_scope`
     is the guard, and seeding only ever takes RoE scope entries as input.

  2. **Provenance is a verifiable reference, not a label.** The root is `origin_type=user_message`
     (a human set the scope; the model did not infer it — mislabelling it `ai_inference` would,
     via the §8.3 training export, teach a model that it invents scope, the §8.6.5 #2 "its own
     claim = truth" failure) AND carries `origin_ref` pointing at the scope entry it came from, so
     "a human said so" can be checked against the RoE rather than taken on trust.

  3. **The root is not falsifiable, so it is never counted as outstanding verdict-work.** Recon
     enumerates; the root will never reach `confirmed`/`refuted`. Graph-mode phase advancement
     reads the graph, so a predicate that counted "open hypotheses" would hang RECON forever on a
     root that can never get a verdict. `open_hypotheses_excluding_roots` is the predicate that
     does not, and the root's own close is `mark_root_enumerated` (lifecycle `completed`, verdict
     left `unassessed`) when its asset is enumerated — a lifecycle close, not a verdict.
"""
from __future__ import annotations

import logging

log = logging.getLogger("agent.pipeline.graph_coldstart")

# The origin_ref scheme that marks a hypothesis as a scope root and records which RoE scope entry
# it came from. Parsing the entry back out of the ref is how `assert_roots_match_scope` checks the
# root set against the authorized set without a second store.
SCOPE_REF_PREFIX = "scope:"

# Lifecycle statuses that still count as outstanding testable work. Mirrors the strategist's
# CANDIDATE_STATUSES: a hypothesis in one of these has an experiment still worth running. Roots are
# excluded from the count regardless of their status (invariant 3).
_OPEN_STATUSES = ("draft", "open", "queued", "running")


def _is_wildcard(entry: str) -> bool:
    return "*" in entry


def _claim_for(entry: str) -> tuple[str, str]:
    """(title, claim) for a scope entry. A wildcard entry cannot be enumerated to a single host, so
    its root claims the enumeration of hosts *under* it; a concrete entry claims the enumeration of
    its own attack surface. Either way the claim is an enumeration directive, not a falsifiable
    assertion — that is what makes it a recon root rather than a hypothesis."""
    if _is_wildcard(entry):
        return (f"Enumerate hosts under {entry}", f"enumerate the in-scope hosts under {entry}")
    return (f"Enumerate the attack surface of {entry}", f"enumerate the attack surface of {entry}")


def scope_ref(entry: str) -> str:
    return f"{SCOPE_REF_PREFIX}{entry}"


def is_scope_root(hypothesis: dict) -> bool:
    """A scope root is the parentless hypothesis seeded from a RoE scope entry. Identified by its
    `origin_ref` (the verifiable provenance) and the absence of a parent — a child hypothesis, even
    one seeded with a scope-ish ref, has a `primary_parent_id` and is not a root."""
    ref = hypothesis.get("origin_ref") or ""
    return ref.startswith(SCOPE_REF_PREFIX) and not hypothesis.get("primary_parent_id")


def root_scope_entry(hypothesis: dict) -> str | None:
    """The scope entry a root came from, parsed back out of its origin_ref, or None if not a root."""
    if not is_scope_root(hypothesis):
        return None
    return (hypothesis.get("origin_ref") or "")[len(SCOPE_REF_PREFIX):]


def scope_roots(store) -> list[dict]:
    return [h for h in store.list_hypotheses() if is_scope_root(h)]


def seed_scope_roots(store, scope_entries, *, actor: str = "cold-start") -> list[str]:
    """Seed one root hypothesis per RoE scope entry on an empty-of-roots graph, and return the
    hypothesis_ids of the roots that now exist for those entries (created or already present).

    Idempotent: a scope entry that already has a root is not re-seeded, so a RECON phase re-entered
    (a resumed run, a re-planned wave) does not grow a second root for the same target. Takes only
    RoE scope entries — invariant 1 lives in the caller never passing a recon-discovered host
    here, and `assert_roots_match_scope` is the test that it held."""
    existing = {root_scope_entry(h): h["hypothesis_id"] for h in scope_roots(store)}
    ids: list[str] = []
    for entry in scope_entries:
        entry = str(entry).strip()
        if not entry:
            continue
        if entry in existing:
            ids.append(existing[entry])
            continue
        title, claim = _claim_for(entry)
        hid = store.create_hypothesis(
            title=title, claim=claim, phase_created="RECON",
            rationale="scope entry from the RoE — the recon root its hosts/services descend from",
            origin_type="user_message",          # invariant 2: a human set the scope
            origin_ref=scope_ref(entry),          # invariant 2: a checkable reference, not a label
            impact=3, confidence_band="medium",
            confidence_reason="a scope entry is authorized by the RoE, not inferred",
            surface=entry,
        )
        log.info("seeded recon root %s for scope entry %r", hid, entry)
        existing[entry] = hid
        ids.append(hid)
    return ids


def mark_root_enumerated(store, hypothesis_id: str, *, reason: str = "asset enumerated") -> int:
    """Close a root once its asset is enumerated (or budget is hit). The close is a *lifecycle*
    move to `completed`, not a verdict — a recon root is not falsifiable, so its verdict stays
    `unassessed` forever (invariant 3). Returns the new version."""
    h = store.get_hypothesis(hypothesis_id)
    return store.set_lifecycle_status(hypothesis_id, h["version"], "completed", reason=reason)


def open_hypotheses_excluding_roots(store) -> list[dict]:
    """The hypotheses that still represent outstanding testable work — candidates in an open
    lifecycle status, **with scope roots removed**. This is the predicate graph-mode phase
    advancement must use: a root can never reach a verdict, so counting it as open work would hang
    the phase forever (the same 'done-condition defined by something that never happens' trap the
    check_transition seam closed). Empty means the wave has nothing left to run — RECON/ANALYSIS/
    VALIDATION can conclude — regardless of how many roots sit in the graph un-verdicted."""
    return [
        h for h in store.list_hypotheses()
        if h.get("lifecycle_status") in _OPEN_STATUSES and not is_scope_root(h)
    ]


def assert_roots_match_scope(store, scope_entries) -> None:
    """Invariant 1, as a guard: the set of scope roots in the graph equals the set of authorized
    scope entries — no root for a target the RoE did not authorize, no authorized entry left
    un-rooted. Raises `ColdStartInvariantError` naming the difference, so a drift (an auto-rooted
    discovered host, a scope entry seeding silently skipped) fails loudly rather than rooting work
    on something out of scope."""
    want = {str(e).strip() for e in scope_entries if str(e).strip()}
    have = {root_scope_entry(h) for h in scope_roots(store)}
    if want != have:
        raise ColdStartInvariantError(
            f"scope roots do not match the authorized scope: "
            f"un-rooted entries {sorted(want - have)}, roots with no scope entry {sorted(have - want)}"
        )


class ColdStartInvariantError(AssertionError):
    """The root set drifted from the authorized scope set (invariant 1)."""
