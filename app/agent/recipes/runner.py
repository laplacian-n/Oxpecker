"""Phase 6 — retest runner. Replays a versioned recipe's steps through the same RoE-scoped tool
functions a live engagement uses (`ALLOWED_TOOLS` below — a small, fixed allowlist, never
arbitrary code execution or a raw shell/HTTP replay). `policy` is always injected fresh at
retest time, never taken from the recipe itself, so a retest re-checks against whatever the RoE
is *right now* — an expired or rescoped engagement fails a retest the same way it fails a live
call, rather than a recorded request just firing again regardless of current scope. A recipe step
that tries to smuggle its own `policy` argument is rejected outright rather than silently
overridden, since a silent override would hide that staleness bug instead of catching it.

This module deliberately does NOT decide "still vulnerable" vs. "fixed" from the raw tool
output — that's exactly the "never claim confirmed from model prose alone" boundary (§8) the
findings schema (M4.3) already enforces structurally. A caller (human or model reasoning over
this structured output, citing what it actually observed) makes that call and records the
conclusion via `EngagementStore.record_retest()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .model import Recipe


@dataclass
class StepOutcome:
    step_index: int
    tool: str
    ok: bool
    result: object | None
    error: str | None


def default_allowed_tools() -> dict[str, Callable]:
    from ..security_tools import http_recon, port_discovery

    return {
        "http_recon": http_recon.run,
        "port_discovery": port_discovery.run,
    }


def run_recipe(
    recipe: Recipe, policy, allowed_tools: dict[str, Callable] | None = None
) -> list[StepOutcome]:
    tools = allowed_tools if allowed_tools is not None else default_allowed_tools()
    outcomes: list[StepOutcome] = []
    for i, step in enumerate(recipe.steps):
        if "policy" in step.arguments:
            outcomes.append(
                StepOutcome(
                    i, step.tool, False, None,
                    "recipe step must not include its own 'policy' argument — policy is always "
                    "injected fresh at retest time",
                )
            )
            continue
        if step.tool not in tools:
            outcomes.append(
                StepOutcome(i, step.tool, False, None, f"tool {step.tool!r} not in the retest allowlist")
            )
            continue
        try:
            result = tools[step.tool](**step.arguments, policy=policy)
            outcomes.append(StepOutcome(i, step.tool, True, result, None))
        except Exception as e:
            outcomes.append(StepOutcome(i, step.tool, False, None, f"{type(e).__name__}: {e}"))
    return outcomes
