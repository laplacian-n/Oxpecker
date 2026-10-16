"""Which UI an arrival gets, asserted across the files that decide it independently.

The classic single-file UI at `/` is the default again — it is the UI the operator wants, and the
React client became the default by a decision that was reversed. Two places have to agree, neither
of which can import the other:

  * electron/main.js's `uiPath` fallback, which is what the desktop app opens;
  * docs/DEPLOY_UBUNTU.md, which is what a human follows.

They drifted apart once before (the doc claimed one default while the literal said another), and
nothing could catch it because the claim is prose on one side and a string literal on the other.
So the literal is pinned, and the alternative UI's address (/app/, the React client the classic UI
deep-links to) is pinned with it, so the comment beside the literal always tells the next reader
where the other UI is.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
MAIN_JS = APP / "electron" / "main.js"
DEPLOY_DOC = APP.parent / "docs" / "DEPLOY_UBUNTU.md"
DEFAULT_PATH = "/"       # the classic single-file UI the shell opens by default
CLIENT_PATH = "/app/"    # the React operator client, now the opt-in alternative


class ElectronDefaultTest(unittest.TestCase):
    def test_the_shell_opens_the_classic_ui_by_default(self):
        text = MAIN_JS.read_text(encoding="utf-8")
        m = re.search(r"const uiPath = cfg\.uiPath \|\| '([^']*)'", text)
        self.assertIsNotNone(m, "electron/main.js no longer derives uiPath from a fallback "
                                "literal; if it now asks the server, delete this test and say so")
        self.assertEqual(
            m.group(1), DEFAULT_PATH,
            f"the Electron shell defaults to {m.group(1)!r}, not {DEFAULT_PATH!r} (the classic UI). "
            "The deploy doc tells operators it opens the classic UI; a different literal here makes "
            "that sentence false, which is exactly the state this test was written to end.",
        )

    def test_it_names_the_alternative_ui_address(self):
        """The comment beside the literal tells the next reader where the React client is."""
        text = MAIN_JS.read_text(encoding="utf-8")
        self.assertIn(CLIENT_PATH, text,
                      "electron/main.js no longer mentions /app/, so nothing tells an operator "
                      "where the React operator client went")


class DeployDocTest(unittest.TestCase):
    def setUp(self):
        self.text = DEPLOY_DOC.read_text(encoding="utf-8")

    def test_it_names_both_uis(self):
        self.assertIn(CLIENT_PATH, self.text)   # the React client, at /app/
        self.assertIn("classic", self.text.lower())

    def test_it_no_longer_tells_anyone_to_set_uiPath_to_the_old_default(self):
        """The old instruction — set uiPath to /classic to get the classic UI — is now backwards:
        the classic UI is the default, and /app/ is the opt-in."""
        self.assertNotIn(
            '"uiPath": "/classic"', self.text,
            'docs/DEPLOY_UBUNTU.md still tells the reader to set "uiPath": "/classic", which is no '
            'longer how to choose a UI: the classic UI is the default and the alternative is /app/',
        )


if __name__ == "__main__":
    unittest.main()
