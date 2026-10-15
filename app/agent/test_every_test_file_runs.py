"""Every `test_*.py` must contribute at least one test to the runner, or it is decoration.

This repo's CI runs `python -m unittest discover -p 'test_*.py'`, and discovery is happy to
import a file, find no TestCase in it, collect nothing, and report success. 28 of 140 test files
were in exactly that state: written as a `main()` with a hand-rolled `check()` accumulator and a
`if __name__ == "__main__"` guard, so they ran only when someone remembered to run them by hand.
Nobody did. One of them — `agent/web/test_approval_taint.py` — had been dying on a TypeError at
its first assertion ever since §14.1 C changed a constructor, and the suite stayed green the
whole time, because a file that collects zero tests cannot fail. Two more were stale in the same
way and only revealed it once they first ran: `test_integrity.py`'s TaintStore stub, and
`test_broker_mediation.py`'s exact-set assertion.

The executed-count gate in `.github/workflows/tests.yml` cannot see any of this: it floors the
*total*, and these files contribute 0 while the other thousand-odd tests clear the floor on
their own.

There is a second way to be invisible, which this guard also covers: a directory the runner
cannot enter. `agent/prompts/` had no `__init__.py`, so discovery raised "Start directory is not
importable" and skipped every file under it, however the file was written.

So this is the guard at the right level. It is a ratchet, asserted in BOTH directions:

  * a file not on the list that the runner collects nothing from fails — a new script-style
    test, a file whose only TestCase gets deleted, or a directory that stops being importable,
    cannot join the silent set;
  * a file ON the list that now contributes something ALSO fails — converting a file means
    deleting its line here. Without this direction the list would itself rot into a stale
    to-do list, which is the same bug one level up.

The list may only shrink. Each entry is a file whose assertions are not being run by CI; several
of them are named for the parts of the system that may never be removed (the broker, the scope
check, the audit log, the evidence store), so the list is a standing debt register, not a
baseline to settle at.
"""
from __future__ import annotations

import unittest
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent

# Files that currently contribute zero tests to the runner. Shrink only.
COLLECTS_NOTHING = {
    "agent.sandbox.test_availability",
    "agent.sandbox.test_isolation",
    "agent.sandbox.test_seccomp_profile",
    "agent.sandbox.test_wsl2",
    "agent.security_tools.test_http_recon_https",
}

# If the walk below finds far fewer files than the repo has, it is broken, and a ratchet over an
# empty set asserts nothing at all. Floored well under the real count so adding or removing a few
# test files never trips it.
MIN_TEST_FILES = 120


def _modules_discovery_reaches() -> tuple[set[str], list[str]]:
    """The module of every test `unittest discover` collects, measured the way CI measures it.

    Deliberately NOT importlib.import_module per file. That was this guard's first shape, and it
    missed a whole directory: agent/prompts/ had no __init__.py, so discovery raised "Start
    directory is not importable" and skipped every file under it -- while import_module imported
    them fine as a namespace package and reported all well. A guard that measures something
    subtly different from the runner is the very bug it exists to catch, one level up. So this
    runs the real loader, from the same top-level directory and with the same pattern as
    .github/workflows/tests.yml.
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(AGENT_ROOT), pattern="test_*.py",
                            top_level_dir=str(AGENT_ROOT.parent))
    modules: set[str] = set()
    broken: list[str] = []

    def walk(s):
        for item in s:
            if isinstance(item, unittest.TestSuite):
                walk(item)
            elif type(item).__name__ == "_FailedTest":
                broken.append(item.id())
            else:
                modules.add(type(item).__module__)

    walk(suite)
    return modules, broken


def _module_names() -> list[str]:
    names = []
    for path in sorted(AGENT_ROOT.rglob("test_*.py")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        rel = path.relative_to(AGENT_ROOT.parent).with_suffix("")
        names.append(".".join(rel.parts))
    return names


class EveryTestFileRunsTest(unittest.TestCase):
    maxDiff = None

    def test_no_test_file_silently_collects_nothing(self):
        names = _module_names()
        self.assertGreaterEqual(
            len(names), MIN_TEST_FILES,
            f"only found {len(names)} test files under {AGENT_ROOT}; the walk is broken, and a "
            f"ratchet over an empty set asserts nothing",
        )

        reached, broken = _modules_discovery_reaches()
        self.assertEqual(broken, [], f"the runner could not load these: {broken}")

        empty = {n for n in names if n != __name__ and n not in reached}

        newly_silent = sorted(empty - COLLECTS_NOTHING)
        self.assertEqual(
            newly_silent, [],
            "the test runner collects nothing from these files, so their assertions never run. "
            "Either give the file a unittest.TestCase, or -- for a script-style main() -- the "
            "three-line load_tests footer described in agent/script_suite.py. Adding it to "
            f"COLLECTS_NOTHING is not the fix: {newly_silent}",
        )

        now_running = sorted(COLLECTS_NOTHING - empty)
        self.assertEqual(
            now_running, [],
            "these files now contribute tests, so remove them from COLLECTS_NOTHING -- the list "
            f"is a ratchet and a stale entry hides the next regression: {now_running}",
        )


if __name__ == "__main__":
    unittest.main()
