"""A static sandbox probe can say yes on a host where the sandbox cannot run.

Found on real hardware, not here: walking docs/DEPLOY_UBUNTU.md under
`systemd-run --user -p RestrictNamespaces=yes` and `-p PrivateUsers=yes`, bwrap failed with
"No permissions to create a new namespace" while `describe_host()` still reported
`"available": true` and `resolve_tier("bubblewrap")` returned the tier without raising. The
restriction lives in the unit's namespace policy, which a platform/PATH/sysctl check never
looks at -- it only bites when bwrap calls unshare(2).

No unit test can create that condition: it requires a hardened service unit. What a unit test
*can* pin down is the part that made the host-level finding fatal rather than merely confusing
-- that nothing in the shipped server ever asked for the definitive answer. These tests fail
against the code as it stood before `verify_once` existed.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from . import availability


class VerifyOnceIsExecVerifiedAndCached(unittest.TestCase):
    def setUp(self):
        availability.reset_verification_cache()
        self.addCleanup(availability.reset_verification_cache)

    def test_a_tier_that_passes_statically_but_fails_on_exec_is_reported_unavailable(self):
        """The systemd case, as a unit: static yes, exec no."""
        def fake_probe(tier, deep=False):
            return (True, "static check passed") if not deep else (
                False,
                "a trivial bwrap invocation failed: bwrap: No permissions to create a new "
                "namespace, likely because the kernel does not allow non-privileged user "
                "namespaces.",
            )

        with patch.object(availability, "probe", side_effect=fake_probe):
            static_ok, _ = availability.probe(availability.TIER_BUBBLEWRAP)
            verified_ok, reason = availability.verify_once(availability.TIER_BUBBLEWRAP)

        self.assertTrue(static_ok, "fixture is wrong: the static probe must pass here")
        self.assertFalse(verified_ok, "the exec-verified probe must reject what the static one allowed")
        self.assertIn("No permissions to create a new namespace", reason)

    def test_the_exec_probe_runs_once_however_many_times_it_is_asked(self):
        """The reason this can be afforded per command. Without caching the honest check costs a
        subprocess on every tool call, which is why the deep path went unused for so long."""
        calls = []

        def counting_probe(tier, deep=False):
            calls.append(deep)
            return True, "ok"

        with patch.object(availability, "probe", side_effect=counting_probe):
            for _ in range(5):
                availability.verify_once(availability.TIER_BUBBLEWRAP)

        self.assertEqual(calls.count(True), 1, f"deep probe ran {calls.count(True)} times, want 1")

    def test_each_tier_is_verified_separately(self):
        seen = []

        def per_tier(tier, deep=False):
            seen.append(tier)
            return True, "ok"

        with patch.object(availability, "probe", side_effect=per_tier):
            availability.verify_once(availability.TIER_BUBBLEWRAP)
            availability.verify_once(availability.TIER_WSL2)
            availability.verify_once(availability.TIER_BUBBLEWRAP)

        self.assertEqual(seen, [availability.TIER_BUBBLEWRAP, availability.TIER_WSL2])


class RunCommandRefusesWhenVerificationFails(unittest.TestCase):
    """The half that matters to an operator: the refusal reaches the model as a tool error that
    names the cause, instead of a raw `bwrap: No permissions...` from deep inside subprocess."""

    def setUp(self):
        availability.reset_verification_cache()
        self.addCleanup(availability.reset_verification_cache)

    def test_the_web_runtime_refuses_rather_than_running_outside_the_sandbox(self):
        from ..web import dev_server

        def fake_probe(tier, deep=False):
            if deep:
                return False, "bwrap: No permissions to create a new namespace"
            return True, "static check passed"

        session = dev_server.Session(
            session_id="exec-verify-test", isolation_tier=availability.TIER_BUBBLEWRAP
        )

        with patch.object(availability, "probe", side_effect=fake_probe):
            result = dev_server._run_tool("run_command", {"command": "echo hi"}, session)

        self.assertFalse(result.get("ok"), f"the command must not run: {result}")
        self.assertIn("No permissions to create a new namespace", result["error"])
        self.assertIn("RestrictNamespaces", result["error"],
                      "the error must point at the likely cause, not just repeat the kernel text")
        self.assertIsNone(session.effective_isolation_tier,
                          "a refused command must not leave a tier recorded as effective")


if __name__ == "__main__":
    unittest.main()
