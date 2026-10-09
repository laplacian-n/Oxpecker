"""Orchestration tiers (AGENT_ARCHITECTURE.md §2.6), and the one line that does not move with
them (§2.6.1, canonical).

Everything the agent design trades for verifiability is the right trade for an hours-long run
against a real program, and the wrong trade for "let me poke at this for ten minutes". So there
are three tiers — and **the tier changes orchestration and nothing else**:

| tier   | orchestration                                   |
|--------|-------------------------------------------------|
| low    | none — one agent, a person in the strategist's chair |
| medium | sequential waves, one worker at a time          |
| high   | parallel waves, the full four-role design       |

§2.6.1, the line this module exists to protect:

    A tier may remove orchestration. It may never remove the broker, the scope check, the audit
    log or the evidence store. Those four are not overhead, they are the product; a "fast mode"
    that skips them is a back door we built ourselves. The broker path, the audit entry and the
    evidence write are byte-for-byte identical across all three tiers.

This is written **before the tiers exist** on purpose (§12 step 0a). The tier system is not a
switch yet; this module is the seam it will be built on, and `test_tier_parity.py` is the guard
that stands first — so that the day orchestration starts reading the tier, the assertion that it
must not reach the four protected components is already failing if it does. Writing the test
first is what stops the tier system quietly becoming three engines.

**The contract for anyone building orchestration on this:** read the active tier here, in the
orchestration layer, to decide how many workers a wave has and whether there is a verifier. Never
thread it into `agent.broker.*`, `agent.audit_log` or `agent.evidence.store` — those four are
listed in `PROTECTED_FROM_TIER` and the parity test fails if any of them so much as imports this
module.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
from pathlib import Path

from . import config

ORCHESTRATION_TIERS = ("low", "medium", "high")

# Not `low`: the owner declined one-agent-driven-by-a-person as a *default* (it is a mode, not
# the default). `high` is the full design; an engagement opts down to medium/low deliberately.
DEFAULT_TIER = "high"

# The four §2.6.1 names a tier may never remove or alter. The parity test asserts none of these
# modules depends on the orchestration tier — operationally, that none of them imports this one.
PROTECTED_FROM_TIER = (
    "agent/broker/broker.py",
    "agent/broker/scope_check.py",
    "agent/audit_log.py",
    "agent/evidence/store.py",
)


class TierError(ValueError):
    pass


def normalize_tier(tier: str | None) -> str:
    """The canonical tier name, or `DEFAULT_TIER` for None/empty. Raises on an unknown one rather
    than silently falling back, because a typo that became 'high' unnoticed is the quiet failure
    this whole module is about."""
    if tier is None or tier == "":
        return DEFAULT_TIER
    t = str(tier).strip().lower()
    if t not in ORCHESTRATION_TIERS:
        raise TierError(f"unknown orchestration tier {tier!r}; expected one of {ORCHESTRATION_TIERS}")
    return t


# The active tier, for the orchestration layer to read. A contextvar, not a module global, so a
# future parallel/async orchestrator running two engagements at once does not have them stamp on
# each other's tier. The four protected components must never read this — see the module docstring.
_active_tier: contextvars.ContextVar[str] = contextvars.ContextVar("active_tier", default=DEFAULT_TIER)


def active_tier() -> str:
    return _active_tier.get()


@contextlib.contextmanager
def use_tier(tier: str):
    """Run a block with `tier` active. Orchestration enters this once it knows the engagement's
    tier; the parity test enters it to prove the four protected components are unaffected."""
    token = _active_tier.set(normalize_tier(tier))
    try:
        yield
    finally:
        _active_tier.reset(token)


def engagement_tier(engagement_id: str, *, engagements_root: Path | None = None) -> str:
    """The tier an engagement runs at (§2.6.2: tier is set per engagement). Read from roe.json's
    `tier`; an engagement without one runs at `DEFAULT_TIER`, so adding tiers did not change how a
    single existing engagement behaves."""
    root = engagements_root or config.ENGAGEMENTS_ROOT
    roe_path = root / engagement_id / "roe.json"
    if not roe_path.exists():
        return DEFAULT_TIER
    try:
        roe = json.loads(roe_path.read_text())
    except (json.JSONDecodeError, OSError):
        return DEFAULT_TIER
    return normalize_tier(roe.get("tier"))


# The phases a high-tier engagement runs on the hypothesis graph: the recon-wave cold-start entry
# (§14.1 G — a root claim with a recon experiment whose observed_result spawns the real
# hypotheses) through hypothesis testing. INTAKE has no work to do; REPORT and CLOSEOUT are flat
# orchestration that *read* the graph (the reporter walks a confirmed chain back, §2.4 — it reads,
# it does not produce hypotheses, so it needs no wave). Keeping RECON on the graph is what stops a
# two-store bridge forming at the busiest seam: if RECON were flat, its raw evidence would land
# outside the graph and ANALYSIS would have to import it.
GRAPH_PHASES = ("RECON", "ANALYSIS", "VALIDATION")


def uses_graph(phase: str, tier: str | None) -> bool:
    """The single decider of whether a `(phase, tier)` runs on the hypothesis graph (the wave
    engine) or the flat task machine. **Both `plan_tasks` and `check_transition` must call this one
    function** — if two call sites decide the mode independently they will drift, and the day they
    disagree a phase hangs (one plans no tasks, the other waits on tasks that never come) or jumps
    (one defers to a wave, the other sees an empty task list and advances before the wave runs).
    One function, asked from both places, is the whole guard against a third engine fork growing
    inside the driver.

    Only the high tier uses the graph, and only for RECON..VALIDATION. `tier` is explicit on
    purpose — a `None`/unknown tier is treated as flat, never as the `DEFAULT_TIER`, so a caller
    that has not deliberately opted a phase into graph mode keeps the flat behaviour it has today
    rather than silently switching because the default tier happens to be high."""
    if tier is None:
        return False
    return normalize_tier(tier) == "high" and phase in GRAPH_PHASES
