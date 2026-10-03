"""Tests for Phase 6 finding <-> recipe linking (agent/recipes/linking.py)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ..findings.model import Finding
from .linking import attach_recipe
from .model import RecipeStep
from .store import RecipeStore


def _finding(**overrides) -> Finding:
    defaults = dict(
        title="Reflected XSS in search",
        severity="medium",
        target="http://127.0.0.1:3000/",
        description="search param reflected unescaped",
        remediation="escape output",
        tool="http_recon",
        session_id="s1",
        engagement_id="test-eng",
    )
    defaults.update(overrides)
    return Finding(**defaults)


class TestAttachRecipe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recipe-linking-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = RecipeStore(recipes_dir=self.tmp)

    def test_returns_finding_with_ref_set(self):
        finding = _finding()
        self.assertIsNone(finding.reproduction_recipe_ref)
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/?q=<script>"})
        updated, recipe = attach_recipe(finding, [step], author="tester", recipe_store=self.store)

        self.assertIsNotNone(updated.reproduction_recipe_ref)
        self.assertEqual(updated.reproduction_recipe_ref, self.store.ref_for(recipe))
        self.assertEqual(recipe.finding_id, finding.finding_id)

    def test_original_finding_object_untouched(self):
        finding = _finding()
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        attach_recipe(finding, [step], author="tester", recipe_store=self.store)
        self.assertIsNone(finding.reproduction_recipe_ref)  # the original is a separate object

    def test_recipe_loadable_via_the_returned_ref(self):
        finding = _finding()
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        updated, _recipe = attach_recipe(finding, [step], author="tester", recipe_store=self.store)
        loaded = self.store.load_by_ref(updated.reproduction_recipe_ref)
        self.assertEqual(loaded.finding_id, finding.finding_id)
        self.assertEqual(loaded.steps[0].tool, "http_recon")

    def test_second_attach_creates_a_new_version_not_overwrite(self):
        finding = _finding()
        step1 = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        updated1, recipe1 = attach_recipe(finding, [step1], author="tester", recipe_store=self.store)

        step2 = RecipeStep(tool="port_discovery", arguments={"host": "127.0.0.1", "ports": [3000]})
        updated2, recipe2 = attach_recipe(updated1, [step2], author="tester2", recipe_store=self.store)

        self.assertEqual(recipe1.version, 1)
        self.assertEqual(recipe2.version, 2)
        self.assertNotEqual(updated1.reproduction_recipe_ref, updated2.reproduction_recipe_ref)


if __name__ == "__main__":
    unittest.main()
