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

from .test_dev_server_imports import _documented_tokens, _normalize

MAIN_JS = Path(__file__).resolve().parents[2] / "electron" / "main.js"


def _electron_hint_tokens() -> set[str]:
    text = MAIN_JS.read_text(encoding="utf-8")
    m = re.search(r"pip install (.+)'\);", text)
    assert m, "electron/main.js lost the pip-install hint in its startup-failure dialog"
    tokens = re.findall(r'"([^"]+)"|(\S+)', m.group(1))
    return {_normalize(re.sub(r"\[.*\]$", "", (a or b))) for a, b in tokens}


class TestElectronInstallHintMatchesDevServer(unittest.TestCase):
    def test_hint_names_no_package_dev_server_does_not_document(self):
        phantom = _electron_hint_tokens() - _documented_tokens()
        self.assertEqual(
            phantom, set(),
            f"electron/main.js's startup-failure hint names packages dev_server.py's own "
            f"Prerequisites block does not: {phantom}. A user following this hint verbatim "
            "installs packages that do nothing and still does not have a working venv.",
        )

    def test_hint_names_cryptography(self):
        self.assertIn(
            "cryptography", _electron_hint_tokens(),
            "electron/main.js's startup-failure hint must name every package dev_server.py "
            "cannot start without -- this is exactly how it went missing the first time.",
        )


if __name__ == "__main__":
    unittest.main()
