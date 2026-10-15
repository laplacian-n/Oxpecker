"""Adapter that lets `unittest discover` run this repo's script-style test files.

A number of test files here predate any use of unittest: each is a `main() -> int` that drives a
module-level `check(name, condition, detail)` appending to `PASS`/`FAIL`, prints a tally, and
returns 1 if anything failed. Run by hand (`python3 -m agent.broker.test_policy_bypass`) they
work. Run by CI they did not run at all: discovery imported each file, found no TestCase,
collected nothing, and reported success — see `agent/test_every_test_file_runs.py` for the
ratchet that now makes that visible, and the two stale assertions it uncovered once these
actually executed.

The conversion is an adapter rather than a rewrite on purpose. Between them these files hold
several hundred assertions about the broker, the scope check and the evidence store — the parts
of this system that may never be removed. Transcribing them by hand into TestCase methods would
put every one of those assertions through a retyping step, and a weakened assertion that still
passes is invisible. The adapter runs the existing `main()` byte-for-byte: the assertions cannot
drift, because nothing touches them.

A file opts in with three lines at the bottom:

    def load_tests(loader, tests, pattern):
        from ..script_suite import suite_for
        return suite_for(__name__, min_checks=42)

`load_tests` is the stdlib protocol `loadTestsFromModule` already honours, so no runner change
is needed. `min_checks` is the file's check count at conversion time and is the anti-vacuity
guard: without it, a `main()` edited down to `return 0` — or one that exits early past most of
its checks, which is exactly how `test_approval_taint.py` sat broken for weeks — would satisfy
the adapter while asserting nothing. It is a floor, so adding checks is free and losing them is
a failure.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import unittest

MAX_OUTPUT_ON_FAILURE = 8000


def suite_for(module_name: str, *, min_checks: int) -> unittest.TestSuite:
    """One test that runs `module_name.main()` and fails if any of its checks failed."""
    module = importlib.import_module(module_name)
    main = getattr(module, "main", None)
    if not callable(main):
        raise RuntimeError(f"{module_name} has no callable main(); it cannot be adapted")

    class ScriptChecks(unittest.TestCase):
        maxDiff = None

        def test_every_check_passes(self):
            # main() prints a PASS/FAIL line per check. Captured rather than let through: a
            # thousand-line tally per file would bury the rest of the suite's output, and it is
            # only wanted when something failed.
            # stdout AND stderr: several of these files exercise argparse's error paths, which
            # print multi-line usage text to stderr. Captured rather than let through, and
            # included in the failure message when something does fail.
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = main()
            out = out_buf.getvalue()
            if err_buf.getvalue():
                out += "\n--- stderr ---\n" + err_buf.getvalue()

            passed = list(getattr(module, "PASS", []))
            failed = list(getattr(module, "FAIL", []))
            ran = len(passed) + len(failed)

            if rc != 0 or failed:
                detail = "\n".join(l for l in out.splitlines() if "FAIL" in l) or out
                self.fail(
                    f"{module_name}: main() returned {rc}, {len(failed)} of {ran} checks "
                    f"failed: {failed}\n{detail[-MAX_OUTPUT_ON_FAILURE:]}"
                )

            # Anti-vacuity. A file whose checks stop running must fail here rather than pass
            # quietly, which is the whole failure mode this adapter exists to end.
            self.assertGreaterEqual(
                ran, min_checks,
                f"{module_name} ran only {ran} checks, below its recorded floor of {min_checks}. "
                f"Either checks were removed (lower the floor deliberately, in the same commit "
                f"that removes them) or main() is returning early past them, which is how this "
                f"file's assertions went silent in the first place.",
            )

    # Named after the module so a failure in the runner's output says which file it came from,
    # rather than fifteen identically-named ScriptChecks classes.
    leaf = module_name.rsplit(".", 1)[-1]
    ScriptChecks.__name__ = ScriptChecks.__qualname__ = f"ScriptChecks_{leaf}"
    ScriptChecks.__module__ = module_name
    return unittest.TestLoader().loadTestsFromTestCase(ScriptChecks)
