"""electron/main.js keeps its own, independent copy of "what to pip install" in its
startup-failure dialog (the one `python agent/web/dev_server.py did not start` shows when the
backend never comes up). It had drifted since the line was first written: it named
`sse-starlette` and `python-multipart`, neither ever imported anywhere in this repo's history
(`git log -S` on both finds nothing), and it omitted `cryptography`, which dev_server.py cannot
start without. Same bug class `test_dev_server_imports.py` closes for the documentation in
dev_server.py itself, in a second, independent copy of the same advice — a user who hit this
exact dialog and followed it verbatim would still not have a working venv.

Checked against dev_server.py's own Prerequisites block, which is the one place this project
has already committed to keeping accurate (see test_dev_server_imports.py), rather than against
a second hand-maintained list that could drift right alongside this one.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from .test_dev_server_imports import _normalize, _required_install_tokens

MAIN_JS = Path(__file__).resolve().parents[2] / "electron" / "main.js"


def _electron_hint_tokens() -> set[str]:
    text = MAIN_JS.read_text(encoding="utf-8")
    m = re.search(r"pip install (.+)'\);", text)
    assert m, "electron/main.js lost the pip-install hint in its startup-failure dialog"
    tokens = re.findall(r'"([^"]+)"|(\S+)', m.group(1))
    return {_normalize(re.sub(r"\[.*\]$", "", (a or b))) for a, b in tokens}


class TestElectronInstallHintMatchesDevServer(unittest.TestCase):
    def test_hint_matches_dev_servers_prerequisites_exactly(self):
        """Compared BOTH ways, which is the whole point of this test.

        The first version of this check asserted only `hint - documented == set()`, so it
        caught a hint naming a package that does not exist and not a hint missing one that
        does — and then special-cased `cryptography` by name to cover the single instance it
        knew about. That is the same shape as the bug: `pydantic` was absent from the hint when
        the check was written, and the check passed. A test that names the one failure it has
        already seen only ever catches that failure again.

        So assert set equality instead. Extras are stripped on both sides, so
        `uvicorn[standard]` and `uvicorn` compare equal — the extra matters to the user running
        the command and not to the question of which package is named.
        """
        hint, required = _electron_hint_tokens(), _required_install_tokens()
        self.assertEqual(
            hint, required,
            f"electron/main.js's startup-failure hint and dev_server.py's Prerequisites line "
            f"disagree.\n  the hint is missing: {sorted(required - hint) or 'nothing'}\n"
            f"  the hint names packages the Prerequisites line does not: "
            f"{sorted(hint - required) or 'nothing'}\n"
            "Both lists tell a stuck user what to install, so a difference between them means "
            "one of them is wrong. Fix the hint, or fix the Prerequisites line and "
            "docs/DEPLOY_UBUNTU.md with it.",
        )


DEPLOY_DOC = Path(__file__).resolve().parents[3] / "docs" / "DEPLOY_UBUNTU.md"


def _deploy_doc_tokens() -> set[str]:
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    m = re.search(r"pip install \\\n\s*(.+)", text)
    assert m, "docs/DEPLOY_UBUNTU.md lost its pip install block"
    return {_normalize(re.sub(r"\[.*\]$", "", a or b))
            for a, b in re.findall(r"'([^']+)'|(\S+)", m.group(1))}


class TestDeployDocInstallBlockMatchesDevServer(unittest.TestCase):
    def test_the_deploy_doc_names_everything_dev_server_needs(self):
        """The third independent copy of this advice, and the one a real install follows.

        The check above tells whoever it fails to fix this file too, which is exactly the
        arrangement that let the install line drift in the first place: a human instruction to
        keep a copy in sync, with nothing verifying that they did. Three copies of the same
        advice need two tests, both derived from the one source that is already guarded.

        A superset rather than an exact match, because this copy is Linux-specific and may
        legitimately add platform packages (`pyseccomp` for the sandbox) that a
        platform-independent dialog must not tell a Windows user to install.
        """
        missing = _required_install_tokens() - _deploy_doc_tokens()
        self.assertEqual(
            missing, set(),
            f"docs/DEPLOY_UBUNTU.md's pip install block does not name {sorted(missing)}, which "
            "dev_server.py cannot start without. Someone following this document gets a "
            "server that will not bind a port -- which is how `cryptography` was missed.",
        )


if __name__ == "__main__":
    unittest.main()
