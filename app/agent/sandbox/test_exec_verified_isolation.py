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

import time
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

    def test_concurrent_first_callers_still_produce_exactly_one_probe(self):
        """The sequential version of this test passed while the lock was wrong.

        The first implementation took the lock to check the cache, released it to run the
        probe, and took it again to store the result. Every caller that arrived during that
        window therefore ran its own probe. Measured on real hardware before this fix: 20
        threads on a cold cache, 5 trials, 100 real `bwrap` execs instead of 5.

        Nothing was incorrect -- every caller agreed on the answer -- so a correctness test
        could not see it. Only counting the work can, which is why this asserts the count and
        not the value. It is the same check-then-act shape as the broker cooldown and the spend
        budget; the lesson keeps arriving in a new costume.
        """
        import threading as _t

        probe_calls = []
        entered = _t.Barrier(8)

        def slow_probe(tier, deep=False):
            if deep:
                probe_calls.append(tier)
                time.sleep(0.05)  # widen the window a correct lock must already close
            return True, "ok"

        results = []
        with patch.object(availability, "probe", side_effect=slow_probe):
            def worker():
                entered.wait()  # every thread arrives on a cold cache together
                results.append(availability.verify_once(availability.TIER_BUBBLEWRAP))

            threads = [_t.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(
            len(probe_calls), 1,
            f"{len(probe_calls)} concurrent callers each ran the probe; the cache serialises "
            "the first one and the rest must read its result",
        )
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1, "callers disagreed about the tier")

    def test_a_slow_probe_for_one_tier_does_not_block_another(self):
        """Why the lock is per tier. `_probe_wsl2` makes three round trips into the guest, and
        that is not a wait to impose on a caller asking about bubblewrap."""
        import threading as _t

        wsl_started = _t.Event()
        release_wsl = _t.Event()

        def blocking_probe(tier, deep=False):
            if deep and tier == availability.TIER_WSL2:
                wsl_started.set()
                release_wsl.wait(timeout=5)
            return True, "ok"

        with patch.object(availability, "probe", side_effect=blocking_probe):
            slow = _t.Thread(target=availability.verify_once, args=(availability.TIER_WSL2,))
            slow.start()
            self.assertTrue(wsl_started.wait(timeout=5), "fixture never entered the slow probe")

            done = _t.Event()
            _t.Thread(
                target=lambda: (availability.verify_once(availability.TIER_BUBBLEWRAP),
                                done.set()),
            ).start()
            unblocked = done.wait(timeout=2)
            release_wsl.set()
            slow.join(timeout=5)

        self.assertTrue(unblocked, "bubblewrap waited on the wsl2 probe -- the lock is global")

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


class StartupCheckSharesTheCacheWithTheCommandPath(unittest.TestCase):
    """The startup check and the per-command check must be the same exec, not two.

    dev_server's main() verifies the tier at startup so a hardened unit shows up in the startup
    log instead of in a tool error twenty minutes later. If it called resolve_tier(deep=True)
    directly -- the obvious spelling -- it would not populate the cache, and the first
    run_command would exec a second sandboxed /bin/true for an answer already known.
    """

    def setUp(self):
        availability.reset_verification_cache()
        self.addCleanup(availability.reset_verification_cache)

    def test_a_startup_verification_leaves_nothing_for_the_first_command_to_exec(self):
        deep_calls = []

        def counting_probe(tier, deep=False):
            if deep:
                deep_calls.append(tier)
            return True, "ok"

        with patch.object(availability, "probe", side_effect=counting_probe):
            availability.verify_once(availability.TIER_BUBBLEWRAP)   # startup
            availability.verify_once(availability.TIER_BUBBLEWRAP)   # first command
            availability.verify_once(availability.TIER_BUBBLEWRAP)   # every command after

        self.assertEqual(deep_calls, [availability.TIER_BUBBLEWRAP],
                         f"the sandbox was exec-verified {len(deep_calls)} times, want 1")


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
