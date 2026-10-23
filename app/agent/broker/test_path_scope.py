"""Path-level scope — within an in-scope host, restrict/exclude by URL path (a bug-bounty brief's
"only /api/* is in scope", "do not touch /admin"). scope.txt is host/CIDR only, so this is the
layer that expresses it. Deny wins; an empty allow list means every path is allowed (host-only
scope, the prior behaviour). Enforced per hop in http_recon so a redirect cannot escape it.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from .. import config
from . import policy as policy_mod
from . import scope_check
from .policy import Policy


def _policy(allow_paths=(), deny_paths=()) -> Policy:
    return Policy(
        engagement_id="p", allowed_action_classes={"active_web_request"},
        allow_networks=[], allow_hostnames={"t.test"}, deny_networks=[], deny_hostnames=set(),
        policy_version="test", valid_until=time.time() + 3600,
        allow_paths=tuple(allow_paths), deny_paths=tuple(deny_paths),
    )


class ValidatePathTest(unittest.TestCase):
    def test_empty_allow_permits_every_path(self):
        p = _policy()
        for path in ("/", "/anything", "/deep/nested/path"):
            self.assertTrue(scope_check.validate_path(path, p).allowed)

    def test_deny_wins_even_over_allow(self):
        p = _policy(allow_paths=["/api"], deny_paths=["/api/admin"])
        self.assertTrue(scope_check.validate_path("/api/users", p).allowed)
        self.assertFalse(scope_check.validate_path("/api/admin", p).allowed)
        self.assertFalse(scope_check.validate_path("/api/admin/delete", p).allowed)

    def test_non_empty_allow_restricts_to_matches(self):
        p = _policy(allow_paths=["/api", "/public/*"])
        self.assertTrue(scope_check.validate_path("/api", p).allowed)
        self.assertTrue(scope_check.validate_path("/api/v1/users", p).allowed)
        self.assertTrue(scope_check.validate_path("/public/x", p).allowed)
        self.assertFalse(scope_check.validate_path("/admin", p).allowed)
        self.assertFalse(scope_check.validate_path("/", p).allowed)

    def test_prefix_is_segment_aware_not_substring(self):
        p = _policy(allow_paths=["/api"])
        self.assertTrue(scope_check.validate_path("/api/x", p).allowed)
        self.assertFalse(scope_check.validate_path("/apidocs", p).allowed,
                         "/api must not match /apidocs — prefix is on a segment boundary")

    def test_glob_patterns(self):
        p = _policy(allow_paths=["/files/*.json"])
        self.assertTrue(scope_check.validate_path("/files/data.json", p).allowed)
        self.assertFalse(scope_check.validate_path("/files/data.xml", p).allowed)

    def test_query_and_case_are_ignored_for_matching(self):
        p = _policy(deny_paths=["/admin"])
        self.assertFalse(scope_check.validate_path("/ADMIN?x=1", p).allowed)

    def test_the_reason_names_the_rule(self):
        p = _policy(allow_paths=["/api"], deny_paths=["/admin"])
        self.assertIn("path_scope.deny", scope_check.validate_path("/admin", p).policy_rule)
        self.assertIn("path_scope.allow", scope_check.validate_path("/nope", p).policy_rule)


class LoadPolicyPathScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="path-scope-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "scope.txt").write_text("example.com\n")
        (self.tmp / "deny.txt").write_text("169.254.169.254/32\n")

    def _write_roe(self, path_scope_json: str):
        (self.tmp / "roe.json").write_text(
            '{"engagement_id": "e", "allowed_action_classes": ["active_web_request"], '
            '"valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-01-01T00:00:00Z"'
            + path_scope_json + "}")

    def test_path_scope_is_loaded_from_roe(self):
        self._write_roe(', "path_scope": {"allow": ["/api/*"], "deny": ["/api/internal"]}')
        loaded = policy_mod.load_policy(self.tmp)
        self.assertEqual(loaded.allow_paths, ("/api/*",))
        self.assertEqual(loaded.deny_paths, ("/api/internal",))

    def test_absent_path_scope_is_host_only(self):
        self._write_roe("")
        loaded = policy_mod.load_policy(self.tmp)
        self.assertEqual(loaded.allow_paths, ())
        self.assertEqual(loaded.deny_paths, ())
        # host-only means any path allowed
        self.assertTrue(scope_check.validate_path("/anything", loaded).allowed)

    def test_non_string_entries_are_dropped(self):
        self._write_roe(', "path_scope": {"allow": ["/ok", 123, null, "  "]}')
        loaded = policy_mod.load_policy(self.tmp)
        self.assertEqual(loaded.allow_paths, ("/ok",))


class HttpReconPathGateTest(unittest.TestCase):
    def test_http_recon_refuses_an_out_of_scope_path_before_connecting(self):
        from ..security_tools import http_recon

        p = _policy(deny_paths=["/admin"])
        # Host validation is stubbed to allowed so the test isolates the path gate (no DNS/socket).
        orig = scope_check.validate_target
        scope_check.validate_target = lambda host, policy: scope_check.ValidationResult(
            True, "127.0.0.1", "stubbed in scope", "test")
        try:
            with self.assertRaises(PermissionError) as ctx:
                http_recon._one_hop("https://t.test/admin/panel", p, timeout=1.0)
            self.assertIn("path not in scope", str(ctx.exception))
        finally:
            scope_check.validate_target = orig


if __name__ == "__main__":
    unittest.main()
