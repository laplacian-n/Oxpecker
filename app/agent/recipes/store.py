"""Phase 6 — versioned recipe storage. One JSON file per (finding_id, version), never
overwritten once written — `save()` on an existing (finding_id, version) pair raises rather than
silently replacing history, matching this project's evidence-store precedent (tamper/overwrite
detection over silent success).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .. import config
from .model import Recipe, RecipeStep

RECIPES_DIR = config.STATE_DIR / "recipes"


class RecipeVersionExistsError(RuntimeError):
    pass


class RecipeNotFoundError(RuntimeError):
    pass


class RecipeStore:
    def __init__(self, recipes_dir: Path | None = None):
        self.recipes_dir = recipes_dir if recipes_dir is not None else RECIPES_DIR
        self.recipes_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, finding_id: str, version: int) -> Path:
        return self.recipes_dir / f"{finding_id}__v{version}.json"

    def latest_version(self, finding_id: str) -> int:
        versions = [
            int(p.stem.rsplit("__v", 1)[1])
            for p in self.recipes_dir.glob(f"{finding_id}__v*.json")
        ]
        return max(versions) if versions else 0

    def create_version(
        self, finding_id: str, steps: list[RecipeStep], author: str, notes: str = ""
    ) -> Recipe:
        version = self.latest_version(finding_id) + 1
        recipe = Recipe(
            recipe_id=str(uuid.uuid4()),
            finding_id=finding_id,
            version=version,
            steps=steps,
            author=author,
            created_at=time.time(),
            notes=notes,
        )
        self._save(recipe)
        return recipe

    def _save(self, recipe: Recipe) -> None:
        path = self._path(recipe.finding_id, recipe.version)
        if path.exists():
            raise RecipeVersionExistsError(f"{recipe.finding_id} v{recipe.version} already exists")
        data = asdict(recipe)
        path.write_text(json.dumps(data, indent=2))

    def load(self, finding_id: str, version: int | None = None) -> Recipe:
        v = version if version is not None else self.latest_version(finding_id)
        path = self._path(finding_id, v)
        if not path.exists():
            raise RecipeNotFoundError(f"{finding_id} v{v}")
        data = json.loads(path.read_text())
        data["steps"] = [RecipeStep(**s) for s in data["steps"]]
        return Recipe(**data)

    def load_by_ref(self, recipe_ref: str) -> Recipe:
        """recipe_ref is 'finding_id@vN', the format Finding.reproduction_recipe_ref stores."""
        finding_id, _, version_part = recipe_ref.rpartition("@v")
        if not finding_id or not version_part.isdigit():
            raise ValueError(f"malformed recipe_ref {recipe_ref!r}, expected 'finding_id@vN'")
        return self.load(finding_id, int(version_part))

    def ref_for(self, recipe: Recipe) -> str:
        return f"{recipe.finding_id}@v{recipe.version}"

    def list_versions(self, finding_id: str) -> list[int]:
        return sorted(
            int(p.stem.rsplit("__v", 1)[1])
            for p in self.recipes_dir.glob(f"{finding_id}__v*.json")
        )
