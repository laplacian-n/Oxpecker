"""Which UI an arrival gets, asserted across the files that decide it independently.

The classic single-file UI at `/` is the whole product now. The separate React client that used to
live at `/app/` is retired — the server redirects `/app*` back to `/` — so there is no second UI to
point anyone at. Two files still have to tell that story straight, neither able to import the other:

  * electron/main.js's `uiPath` fallback, which is what the desktop app opens;
  * docs/DEPLOY_UBUNTU.md, which is what a human follows.

They drifted apart once before (the doc claimed one default while the literal said another), and
nothing could catch it because the claim is prose on one side and a string literal on the other.
So the literal is pinned, and so is the absence of any surviving "open the other UI" instruction —
there is no other UI, and an instruction to open one that redirects away is a loop.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
MAIN_JS = APP / "electron" / "main.js"
DEPLOY_DOC = APP.parent / "docs" / "DEPLOY_UBUNTU.md"
DEFAULT_PATH = "/"       # the classic single-file UI, the only UI the shell opens


class ElectronDefaultTest(unittest.TestCase):
    def test_the_shell_opens_the_classic_ui_by_default(self):
        text = MAIN_JS.read_text(encoding="utf-8")
        m = re.search(r"const uiPath = cfg\.uiPath \|\| '([^']*)'", text)
        self.assertIsNotNone(m, "electron/main.js no longer derives uiPath from a fallback "
                                "literal; if it now asks the server, delete this test and say so")
        self.assertEqual(
            m.group(1), DEFAULT_PATH,
            f"the Electron shell defaults to {m.group(1)!r}, not {DEFAULT_PATH!r} (the classic UI).",
        )

    def test_it_does_not_tell_the_operator_to_open_the_retired_client(self):
        """No surviving instruction to set uiPath to the retired React client: /app/ redirects to
        /, so opening it just loops back, and a comment that says otherwise is worse than none."""
        text = MAIN_JS.read_text(encoding="utf-8")
        self.assertNotIn(
            '"uiPath": "/app/"', text,
            "electron/main.js still tells the operator to set uiPath to /app/, which is retired and "
            "redirects straight back to /",
        )


class DeployDocTest(unittest.TestCase):
    def setUp(self):
        self.text = DEPLOY_DOC.read_text(encoding="utf-8")

    def test_it_describes_the_classic_ui_as_the_product(self):
        self.assertIn("classic", self.text.lower())

    def test_it_no_longer_tells_anyone_to_switch_uiPath_to_another_ui(self):
        """Both old switch instructions are now dead: /classic (the classic UI is the default) and
        /app/ (retired, redirects to /)."""
        for stale in ('"uiPath": "/classic"', '"uiPath": "/app/"'):
            self.assertNotIn(
                stale, self.text,
                f'docs/DEPLOY_UBUNTU.md still tells the reader to set {stale}, but there is one UI '
                f'now — the classic one at / — and nothing to switch to',
            )


if __name__ == "__main__":
    unittest.main()
