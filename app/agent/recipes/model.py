"""Phase 6 — reproduction recipes for retest mode, versioned and structured. Deliberately NOT
raw command/HTTP replay: a recipe is a list of typed tool invocations (tool name + structured
arguments) drawn from a small fixed allowlist (see `runner.py`'s `ALLOWED_TOOLS`), so a retest
replays through the exact same RoE-scoped tool functions and policy checks a live engagement
uses — an expired or rescoped engagement fails a retest closed the same way it fails a live call
closed, rather than a recorded raw request just being fired again regardless of current scope.
Immutable and versioned: editing a recipe creates a new version, the old one is never mutated in
place, so a finding's `reproduction_recipe_ref` (agent/findings/model.py) always points at
exactly what was actually run.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RecipeStep:
    tool: str
    arguments: dict
    description: str = ""


@dataclass
class Recipe:
    recipe_id: str
    finding_id: str
    version: int
    steps: list[RecipeStep]
    author: str
    created_at: float
    notes: str = ""

    def __post_init__(self):
        if not self.steps:
            raise ValueError("a recipe with zero steps cannot reproduce anything")
        if self.version < 1:
            raise ValueError(f"version must be >= 1, got {self.version}")
