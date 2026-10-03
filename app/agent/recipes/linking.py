"""Phase 6 — connects a versioned recipe (recipes/model.py, store.py) to the Finding it
reproduces (agent/findings/model.py). `FindingsStore` is append-only by design (Phase 4, same
tamper-evident-log convention as the evidence/audit stores) with no update method, so this
module does not mutate a persisted Finding — it returns a new Finding object with
`reproduction_recipe_ref` set, for the caller to persist however fits their workflow (the common
case: create the recipe, build the Finding with the ref already set, then call
`FindingsStore.add()` once — never a get-then-mutate-then-overwrite on an append-only log).
"""
from __future__ import annotations

import dataclasses

from ..findings.model import Finding
from .model import Recipe, RecipeStep
from .store import RecipeStore


def attach_recipe(
    finding: Finding, steps: list[RecipeStep], author: str, recipe_store: RecipeStore | None = None
) -> tuple[Finding, Recipe]:
    """Creates a new recipe version for `finding.finding_id` and returns
    (finding_with_ref_set, recipe) — `finding` itself is untouched (dataclasses are not mutated
    in place here), a fresh copy is returned."""
    store = recipe_store if recipe_store is not None else RecipeStore()
    recipe = store.create_version(finding.finding_id, steps, author)
    updated = dataclasses.replace(finding, reproduction_recipe_ref=store.ref_for(recipe))
    return updated, recipe
