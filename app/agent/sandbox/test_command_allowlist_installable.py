"""Every command the model is allowed to run must be a command the host will actually have.

`config.COMMAND_ALLOWLIST` is what `run_command` will let the model execute. A name in that set
that no documented install step provides is not a sandbox problem and not a security problem --
the model simply gets "command not found", which looks like a broken tool rather than a missing
package. That is how `tree` sat in the allowlist, reachable and uninstalled, with nothing
saying so.

The preceding commit found it by running every allowlisted command for real under the seccomp
filter and reading the output: `tree` printed SKIPPED because the binary was absent. That was
the right way to FIND it and it is not a way to CATCH it. A skip is not a failure, so the check
reports all-pass on a host missing the binary, which is exactly the host where the problem is
live. The next command added to the allowlist and forgotten in the install line would print one
more SKIPPED line into a passing run, and nobody reads the body of a test that passed.

So assert it statically instead, from the two files that already state the answer: the
allowlist itself and the documented apt line. No host involved, so it fails the same way on
every machine, CI included, including machines that do have the binary.

Run directly: `python3 -m agent.sandbox.test_command_allowlist_installable`.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from .. import config

DEPLOY_DOC = Path(__file__).resolve().parents[3] / "docs" / "DEPLOY_UBUNTU.md"

# Commands a stock Ubuntu or Debian base image already provides, so the deploy document does
# not have to name a package for them: coreutils, findutils, grep, diffutils, sed, mawk,
# file, and python3 (installed by the python3-venv line the document does name).
#
# This is a hand-maintained list, which is the shape of defect the three preceding commits were
# all about -- so note what makes it different: it is a list of ASSUMPTIONS, and the assertion
# below requires every allowlisted command to be accounted for by this set OR the apt line.
# Adding a command to COMMAND_ALLOWLIST and classifying it nowhere FAILS. The failure mode of a
# stale hand-maintained list here is a loud error, not a silent pass, which is the only property
# that made the earlier ones dangerous.
BASE_SYSTEM_COMMANDS = frozenset({
    "awk", "cat", "cut", "date", "diff", "echo", "env", "file", "find", "grep",
    "head", "ls", "mkdir", "pwd", "python3", "sed", "sort", "stat", "tail",
    "touch", "uniq", "wc",
})


def _apt_installed_packages() -> set[str]:
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    lines = re.findall(r"apt(?:-get)? install\s+(.+)", text)
    assert lines, "docs/DEPLOY_UBUNTU.md lost its apt install line"
    packages: set[str] = set()
    for line in lines:
        for token in line.split():
            if token.startswith("-"):   # -y and friends
                continue
            packages.add(token)
    return packages


class TestEveryAllowlistedCommandIsInstalled(unittest.TestCase):
    def test_no_allowlisted_command_is_unaccounted_for(self):
        unaccounted = (
            set(config.COMMAND_ALLOWLIST) - _apt_installed_packages() - BASE_SYSTEM_COMMANDS
        )
        self.assertEqual(
            unaccounted, set(),
            f"config.COMMAND_ALLOWLIST lets the model run {sorted(unaccounted)}, but no "
            "documented install step provides them and they are not declared as base-system "
            "commands in BASE_SYSTEM_COMMANDS. The model would get 'command not found' and "
            "read it as a broken tool. Either add the package to the apt line in "
            "docs/DEPLOY_UBUNTU.md, declare it base-system here, or drop it from the allowlist.",
        )

    def test_base_system_declarations_are_still_in_the_allowlist(self):
        """Keeps the assumption list from outliving what it describes.

        A name left here after it leaves the allowlist is a stale assumption that would quietly
        excuse that name if it ever came back -- the same drift, pointed the other way.
        """
        stale = BASE_SYSTEM_COMMANDS - set(config.COMMAND_ALLOWLIST)
        self.assertEqual(
            stale, set(),
            f"BASE_SYSTEM_COMMANDS declares {sorted(stale)} as present on a base system, but "
            "they are no longer in config.COMMAND_ALLOWLIST. Remove them here too.",
        )


if __name__ == "__main__":
    unittest.main()
