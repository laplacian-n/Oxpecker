"""Tests for the §5.3 account boundary (agent/web/engagement_access.py).

The boundary ships with one side by default (single operator) and must become real the moment a
second account exists. These tests pin both states, and in particular pin that `owns()` returns
*False* — not an accident of a missing file or a default — when a second account asks for an
engagement it does not own. That False is what the endpoint turns into a 403; a test that only
ever exercised the single-operator side would let the boundary rot into "always yes" unnoticed.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import config
from agent.web import engagement_access as ea


class AccountResolutionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.key_file = self.root / "key.txt"
        self.accounts_file = self.root / "accounts.json"
        self._p = mock.patch.multiple(
            config,
            WEB_UI_API_KEY_FILE=self.key_file,
            WEB_UI_ACCOUNTS_FILE=self.accounts_file,
        )
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_no_files_means_auth_off_and_everyone_is_the_default_operator(self):
        self.assertEqual(ea.valid_accounts(), {})
        self.assertEqual(ea.resolve_account(None), config.WEB_UI_DEFAULT_ACCOUNT)
        self.assertEqual(ea.resolve_account("anything"), config.WEB_UI_DEFAULT_ACCOUNT)

    def test_single_key_file_maps_to_the_default_operator(self):
        self.key_file.write_text("sekret\n")
        self.assertEqual(ea.valid_accounts(), {config.WEB_UI_DEFAULT_ACCOUNT: "sekret"})
        self.assertEqual(ea.resolve_account("sekret"), config.WEB_UI_DEFAULT_ACCOUNT)
        self.assertIsNone(ea.resolve_account("wrong"))

    def test_accounts_file_maps_each_key_to_its_account(self):
        self.accounts_file.write_text(json.dumps({"alice": "ka", "bob": "kb"}))
        self.assertEqual(ea.resolve_account("ka"), "alice")
        self.assertEqual(ea.resolve_account("kb"), "bob")
        self.assertIsNone(ea.resolve_account("kc"))

    def test_accounts_file_wins_over_the_single_key_file(self):
        self.key_file.write_text("single")
        self.accounts_file.write_text(json.dumps({"alice": "ka"}))
        self.assertIsNone(ea.resolve_account("single"))  # the single key is no longer valid
        self.assertEqual(ea.resolve_account("ka"), "alice")

    def test_a_malformed_accounts_file_is_an_error_not_a_silent_open_door(self):
        self.accounts_file.write_text(json.dumps({"alice": ""}))  # empty key
        with self.assertRaises(ea.AccountError):
            ea.valid_accounts()


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _engagement(self, eng_id: str, owner: str | None):
        d = self.root / eng_id
        d.mkdir(parents=True)
        roe = {"engagement_id": eng_id}
        if owner is not None:
            roe["owner"] = owner
        (d / "roe.json").write_text(json.dumps(roe))

    def test_unknown_engagement_owner_is_none(self):
        self.assertIsNone(ea.engagement_owner("nope", engagements_root=self.root))

    def test_an_engagement_without_an_owner_belongs_to_the_default_operator(self):
        self._engagement("legacy", owner=None)
        self.assertEqual(
            ea.engagement_owner("legacy", engagements_root=self.root),
            config.WEB_UI_DEFAULT_ACCOUNT,
        )

    def test_owns_is_three_valued(self):
        self._engagement("alices", owner="alice")
        # None = no such engagement, so the endpoint 404s rather than confirming it exists.
        self.assertIsNone(ea.owns("alice", "ghost", engagements_root=self.root))
        # True = the owner.
        self.assertIs(ea.owns("alice", "alices", engagements_root=self.root), True)
        # False = a different account — the refusal the 403 is built on.
        self.assertIs(ea.owns("bob", "alices", engagements_root=self.root), False)
        self.assertIs(ea.owns(None, "alices", engagements_root=self.root), False)


if __name__ == "__main__":
    unittest.main()
