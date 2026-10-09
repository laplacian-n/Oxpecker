"""Technique KB scoping per account (AGENT_ARCHITECTURE.md §14.1 B).

Before this change, `technique_kb.py` was "the ONE global store ... not engagement-scoped" at
`config.TECHNIQUE_KB_PATH`, and `NotebookService.build_context_block()` calls `semantic_recall()`
every turn — so a technique saved while working one account's engagement was auto-injected into
a different account's run. §5.3's rule ("the same account may share everything; a different
account nothing") makes that a live cross-account leak. These tests pin the fix: each account
gets its own KB file, the pre-existing default-account file is left exactly where legacy data
already lives, and the account name can't be abused to escape the KB directory.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .. import config
from .service import NotebookService


class TechniqueKbPerAccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="technique-kb-account-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = self.tmp / "state"
        self.engagements_root = self.tmp / "engagements"
        self.engagements_root.mkdir(parents=True)
        self._p = mock.patch.multiple(
            config,
            STATE_DIR=self.state_dir,
            TECHNIQUE_KB_PATH=self.state_dir / "technique_kb.db",
            TECHNIQUE_KB_DIR=self.state_dir / "technique_kb",
            ENGAGEMENTS_ROOT=self.engagements_root,
        )
        self._p.start()
        self.addCleanup(self._p.stop)

    def _engagement(self, eng_id: str, owner: str | None) -> Path:
        d = self.engagements_root / eng_id
        d.mkdir(parents=True)
        roe = {"engagement_id": eng_id}
        if owner is not None:
            roe["owner"] = owner
        (d / "roe.json").write_text(json.dumps(roe))
        return d

    # -- (a) a technique saved under one account is invisible to another ----------------------

    def test_technique_saved_under_alice_is_not_recalled_under_bob(self):
        alice_dir = self._engagement("alice-eng", owner="alice")
        bob_dir = self._engagement("bob-eng", owner="bob")

        alice_svc = NotebookService(alice_dir)
        alice_svc.add_note(
            category="technique", note="JWT alg=none bypass works on the legacy auth endpoint.",
            tags=["jwt"], surface="/auth",
        )

        bob_svc = NotebookService(bob_dir)
        # Without per-account scoping this recall would find alice's technique — the leak this
        # change closes.
        self.assertEqual(bob_svc.recall_techniques("jwt alg none"), [])
        # Sanity: alice can still recall her own technique from her own KB.
        self.assertEqual(len(alice_svc.recall_techniques("jwt alg none")), 1)

    # -- (b) the default account preserves the legacy KB path ----------------------------------

    def test_default_account_engagement_uses_the_legacy_kb_path(self):
        # No `owner` at all — every pre-existing engagement on disk looks like this.
        legacy_dir = self._engagement("legacy-eng", owner=None)
        svc = NotebookService(legacy_dir)
        self.assertEqual(svc.kb.db_path, config.TECHNIQUE_KB_PATH)

        # An engagement explicitly owned by the default account resolves the same way.
        explicit_dir = self._engagement("explicit-default-eng", owner=config.WEB_UI_DEFAULT_ACCOUNT)
        svc2 = NotebookService(explicit_dir)
        self.assertEqual(svc2.kb.db_path, config.TECHNIQUE_KB_PATH)

        # And a technique written through the legacy path really does land in that pre-existing
        # file, not a new one — existing data is not orphaned.
        svc.add_note(category="technique", note="Legacy technique still here.", tags=[])
        self.assertTrue(config.TECHNIQUE_KB_PATH.exists())

    # -- (c) a hostile account name cannot escape the KB directory -----------------------------

    def test_technique_kb_path_for_sanitizes_a_traversal_attempt(self):
        path = config.technique_kb_path_for("../evil")
        self.assertEqual(path.parent, config.TECHNIQUE_KB_DIR)
        self.assertNotIn("..", path.parts)
        # The sanitized name should still be a plain, boring filename inside the KB dir.
        self.assertTrue(path.name.endswith(".db"))

    def test_technique_kb_path_for_sanitizes_other_path_separators_and_dotfiles(self):
        p1 = config.technique_kb_path_for("foo/bar")
        p2 = config.technique_kb_path_for("..\\..\\windows")
        for p in (p1, p2):
            self.assertEqual(p.parent, config.TECHNIQUE_KB_DIR)
            self.assertNotIn("/", p.name.replace(".db", ""))
            self.assertNotIn("\\", p.name)

    def test_technique_kb_path_for_default_account_is_unchanged(self):
        self.assertEqual(
            config.technique_kb_path_for(config.WEB_UI_DEFAULT_ACCOUNT),
            config.TECHNIQUE_KB_PATH,
        )

    def test_technique_kb_path_for_different_accounts_get_different_files(self):
        self.assertNotEqual(
            config.technique_kb_path_for("alice"), config.technique_kb_path_for("bob")
        )


if __name__ == "__main__":
    unittest.main()
