"""Every `test_*.py` must contribute at least one test to the runner, or it is decoration.

This repo's CI runs `python -m unittest discover -p 'test_*.py'`, and discovery is happy to
import a file, find no TestCase in it, collect nothing, and report success. 28 of 140 test files
were in exactly that state: written as a `main()` with a hand-rolled `check()` accumulator and a
`if __name__ == "__main__"` guard, so they ran only when someone remembered to run them by hand.
Nobody did. One of them — `agent/web/test_approval_taint.py` — had been dying on a TypeError at
its first assertion ever since §14.1 C changed a constructor, and the suite stayed green the
whole time, because a file that collects zero tests cannot fail.

The executed-count gate in `.github/workflows/tests.yml` cannot see this: it floors the *total*,
and these files contribute 0 while the other thousand-odd tests clear the floor on their own.

So this is the guard at the right level. It is a ratchet, asserted in BOTH directions:

  * a file not on the list that contributes nothing fails — a new script-style test, or a file
    whose only TestCase gets deleted, cannot join the silent set;
  * a file ON the list that now contributes something ALSO fails — converting a file means
    deleting its line here. Without this direction the list would itself rot into a stale
    to-do list, which is the same bug one level up.

The list may only shrink. Each entry is a file whose assertions are not being run by CI; several
of them are named for the parts of the system that may never be removed (the broker, the scope
check, the audit log, the evidence store), so the list is a standing debt register, not a
baseline to settle at.
"""
from __future__ import annotations

import importlib
import unittest
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent

# Files that currently contribute zero tests to the runner. Shrink only.
COLLECTS_NOTHING = {
    "agent.broker.test_browser_channel",
    "agent.broker.test_injection_quarantine",
    "agent.broker.test_integrity",
    "agent.broker.test_knowledge_channels",
    "agent.broker.test_policy_bypass",
    "agent.broker.test_wildcard_scope",
    "agent.engagement.test_program",
    "agent.evidence.test_hmac_migration",
    "agent.evidence.test_key_integrity",
    "agent.evidence.test_lifecycle",
    "agent.llm.test_provider_seam",
    "agent.prompts.test_compiler",
    "agent.sandbox.test_availability",
    "agent.sandbox.test_isolation",
    "agent.sandbox.test_seccomp_profile",
    "agent.sandbox.test_wsl2",
    "agent.security_tools.test_http_recon_https",
    "agent.test_detection_and_limits",
    "agent.tools.test_command_screening",
    "agent.web.test_audit",
    "agent.web.test_broker_mediation",
    "agent.web.test_debug_trace",
    "agent.web.test_dev_server_auth",
    "agent.web.test_scope",
    "agent.web.test_tool_parity",
    "agent.web.test_tools_wiring",
    "agent.web.test_trace_cli",
    "agent.web.test_workspace_confinement",
}

# If the walk below finds far fewer files than the repo has, it is broken, and a ratchet over an
# empty set asserts nothing at all. Floored well under the real count so adding or removing a few
# test files never trips it.
MIN_TEST_FILES = 120


def _count(suite) -> int:
    if isinstance(suite, unittest.TestSuite):
        return sum(_count(x) for x in suite)
    return 1


def _module_names() -> list[str]:
    names = []
    for path in sorted(AGENT_ROOT.rglob("test_*.py")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        rel = path.relative_to(AGENT_ROOT.parent).with_suffix("")
        names.append(".".join(rel.parts))
    return names


class EveryTestFileRunsTest(unittest.TestCase):
    def test_no_test_file_silently_collects_nothing(self):
        names = _module_names()
        self.assertGreaterEqual(
            len(names), MIN_TEST_FILES,
            f"only found {len(names)} test files under {AGENT_ROOT}; the walk is broken, and a "
            f"ratchet over an empty set asserts nothing",
        )

        loader = unittest.TestLoader()
        empty = set()
        for name in names:
            if name == __name__:  # this file; counting itself proves only that it exists
                continue
            try:
                module = importlib.import_module(name)
            except Exception as e:  # noqa: BLE001 - an unimportable test file is also broken
                self.fail(f"{name} could not be imported, so CI cannot run it: {e!r}")
            if _count(loader.loadTestsFromModule(module)) == 0:
                empty.add(name)

        newly_silent = sorted(empty - COLLECTS_NOTHING)
        self.assertEqual(
            newly_silent, [],
            "these test files contribute no tests to the runner, so their assertions never run. "
            "Give each a unittest.TestCase (see agent/web/test_approval_taint.py for a converted "
            f"example) rather than adding it to COLLECTS_NOTHING: {newly_silent}",
        )

        now_running = sorted(COLLECTS_NOTHING - empty)
        self.assertEqual(
            now_running, [],
            "these files now contribute tests, so remove them from COLLECTS_NOTHING — the list is "
            f"a ratchet and a stale entry hides the next regression: {now_running}",
        )


if __name__ == "__main__":
    unittest.main()
