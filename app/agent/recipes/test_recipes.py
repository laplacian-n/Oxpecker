"""Tests for Phase 6 versioned reproduction recipes: model validation, versioned storage
(immutable, never overwritten), and the retest runner (allowlist enforcement, fresh-policy
injection, a genuinely scope-denied replay actually failing closed against a real Policy)."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ..broker.policy import Policy
from .model import Recipe, RecipeStep
from .runner import run_recipe
from .store import (
    RecipeNotFoundError,
    RecipeStore,
    RecipeVersionExistsError,
)


def _fake_policy() -> Policy:
    import ipaddress

    return Policy(
        engagement_id="test-eng",
        allowed_action_classes={"passive_recon"},
        allow_networks=[ipaddress.ip_network("127.0.0.1/32")],
        allow_hostnames=set(),
        deny_networks=[],
        deny_hostnames=set(),
        policy_version="v1",
        valid_until=time.time() + 3600,
    )


class TestRecipeModel(unittest.TestCase):
    def test_empty_steps_rejected(self):
        with self.assertRaises(ValueError):
            Recipe(recipe_id="r1", finding_id="f1", version=1, steps=[], author="a", created_at=time.time())

    def test_version_below_one_rejected(self):
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1/"})
        with self.assertRaises(ValueError):
            Recipe(recipe_id="r1", finding_id="f1", version=0, steps=[step], author="a", created_at=time.time())


class TestRecipeStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recipe-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = RecipeStore(recipes_dir=self.tmp)

    def _step(self):
        return RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})

    def test_create_version_starts_at_one(self):
        recipe = self.store.create_version("finding-1", [self._step()], author="tester")
        self.assertEqual(recipe.version, 1)

    def test_create_version_increments(self):
        self.store.create_version("finding-1", [self._step()], author="tester")
        recipe2 = self.store.create_version("finding-1", [self._step()], author="tester")
        self.assertEqual(recipe2.version, 2)

    def test_load_latest_by_default(self):
        self.store.create_version("finding-1", [self._step()], author="tester", notes="v1")
        self.store.create_version("finding-1", [self._step()], author="tester", notes="v2")
        loaded = self.store.load("finding-1")
        self.assertEqual(loaded.version, 2)
        self.assertEqual(loaded.notes, "v2")

    def test_load_specific_version(self):
        self.store.create_version("finding-1", [self._step()], author="tester", notes="v1")
        self.store.create_version("finding-1", [self._step()], author="tester", notes="v2")
        loaded = self.store.load("finding-1", version=1)
        self.assertEqual(loaded.notes, "v1")

    def test_load_unknown_raises(self):
        with self.assertRaises(RecipeNotFoundError):
            self.store.load("nonexistent")

    def test_direct_save_of_existing_version_rejected(self):
        recipe = self.store.create_version("finding-1", [self._step()], author="tester")
        with self.assertRaises(RecipeVersionExistsError):
            self.store._save(recipe)

    def test_ref_roundtrip(self):
        recipe = self.store.create_version("finding-1", [self._step()], author="tester")
        ref = self.store.ref_for(recipe)
        self.assertEqual(ref, "finding-1@v1")
        loaded = self.store.load_by_ref(ref)
        self.assertEqual(loaded.recipe_id, recipe.recipe_id)

    def test_malformed_ref_rejected(self):
        with self.assertRaises(ValueError):
            self.store.load_by_ref("not-a-valid-ref")

    def test_list_versions(self):
        self.store.create_version("finding-1", [self._step()], author="tester")
        self.store.create_version("finding-1", [self._step()], author="tester")
        self.assertEqual(self.store.list_versions("finding-1"), [1, 2])


class TestRetestRunner(unittest.TestCase):
    def test_unknown_tool_rejected(self):
        step = RecipeStep(tool="run_command", arguments={"argv": ["ls"]})
        recipe = Recipe(recipe_id="r1", finding_id="f1", version=1, steps=[step], author="a", created_at=time.time())
        outcomes = run_recipe(recipe, policy=_fake_policy())
        self.assertFalse(outcomes[0].ok)
        self.assertIn("not in the retest allowlist", outcomes[0].error)

    def test_smuggled_policy_argument_rejected(self):
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1/", "policy": "fake"})
        recipe = Recipe(recipe_id="r1", finding_id="f1", version=1, steps=[step], author="a", created_at=time.time())
        outcomes = run_recipe(recipe, policy=_fake_policy())
        self.assertFalse(outcomes[0].ok)
        self.assertIn("policy", outcomes[0].error)

    def test_real_tool_called_with_fresh_policy(self):
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        recipe = Recipe(recipe_id="r1", finding_id="f1", version=1, steps=[step], author="a", created_at=time.time())
        fresh_policy = _fake_policy()
        with patch("agent.security_tools.http_recon.run", return_value={"ok": True}) as mock_run:
            outcomes = run_recipe(recipe, policy=fresh_policy)
        mock_run.assert_called_once_with(url="http://127.0.0.1:3000/", policy=fresh_policy)
        self.assertTrue(outcomes[0].ok)

    def test_rescoped_engagement_fails_the_retest_closed(self):
        """The core safety property: replaying against a policy that no longer allows the
        target must fail, exactly like a live call would — never a raw replay bypassing current
        scope checks."""
        step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        recipe = Recipe(recipe_id="r1", finding_id="f1", version=1, steps=[step], author="a", created_at=time.time())

        import ipaddress
        narrowed_policy = Policy(
            engagement_id="test-eng", allowed_action_classes={"passive_recon"},
            allow_networks=[ipaddress.ip_network("10.99.99.99/32")],  # 127.0.0.1 no longer in scope
            allow_hostnames=set(), deny_networks=[], deny_hostnames=set(),
            policy_version="v2", valid_until=time.time() + 3600,
        )
        outcomes = run_recipe(recipe, policy=narrowed_policy)
        self.assertFalse(outcomes[0].ok)  # real http_recon.run() raises PermissionError via scope_check

    def test_multi_step_recipe_partial_failure_isolated(self):
        good_step = RecipeStep(tool="http_recon", arguments={"url": "http://127.0.0.1:3000/"})
        bad_step = RecipeStep(tool="not_a_real_tool", arguments={})
        recipe = Recipe(
            recipe_id="r1", finding_id="f1", version=1, steps=[good_step, bad_step],
            author="a", created_at=time.time(),
        )
        with patch("agent.security_tools.http_recon.run", return_value={"ok": True}):
            outcomes = run_recipe(recipe, policy=_fake_policy())
        self.assertTrue(outcomes[0].ok)
        self.assertFalse(outcomes[1].ok)


if __name__ == "__main__":
    unittest.main()
