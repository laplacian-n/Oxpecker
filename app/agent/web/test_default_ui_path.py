"""Which UI an arrival gets, asserted across the three files that decide it independently.

The operator client at /app/ is the default now. Three places have to agree, none of which can
import another:

  * dev_server's `/` route, which redirects;
  * electron/main.js's `uiPath` fallback, which is what the desktop app opens;
  * docs/DEPLOY_UBUNTU.md, which is what a human follows.

They did not agree before. The deploy doc already stated "the Electron shell defaults to /app/"
while main.js read `cfg.uiPath || '/'`, and nine lines further down the same document said to set
uiPath to /app/ to get that behaviour -- two sentences contradicting each other, one of them
false. Nothing could have caught it: the claim is prose on one side and a string literal on the
other.

So the literal is pinned, and the retired UI's new address is pinned with it, because /app/'s
own "client not built" page links there and a link to a 404 is the same dead end as no link.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
MAIN_JS = APP / "electron" / "main.js"
DEPLOY_DOC = APP.parent / "docs" / "DEPLOY_UBUNTU.md"
CLASSIC_PATH = "/classic"
CLIENT_PATH = "/app/"


class ElectronDefaultTest(unittest.TestCase):
    def test_the_shell_opens_the_operator_client_by_default(self):
        text = MAIN_JS.read_text(encoding="utf-8")
        m = re.search(r"const uiPath = cfg\.uiPath \|\| '([^']*)'", text)
        self.assertIsNotNone(m, "electron/main.js no longer derives uiPath from a fallback "
                                "literal; if it now asks the server, delete this test and say so")
        self.assertEqual(
            m.group(1), CLIENT_PATH,
            f"the Electron shell defaults to {m.group(1)!r}, not {CLIENT_PATH!r}. The deploy doc "
            "tells operators it opens the operator client; a different literal here makes that "
            "sentence false, which is exactly the state this test was written to end.",
        )

    def test_it_names_the_retired_ui_s_real_address(self):
        """The comment beside the literal tells the next reader how to get the old UI back.

        Pinned because `/` was the right answer for that until this change and is now a redirect
        back to the client — an instruction that loops is worse than none.
        """
        text = MAIN_JS.read_text(encoding="utf-8")
        self.assertIn(CLASSIC_PATH, text,
                      "electron/main.js no longer mentions /classic, so nothing tells an operator "
                      "where the UI they used yesterday went")


class DeployDocTest(unittest.TestCase):
    def setUp(self):
        self.text = DEPLOY_DOC.read_text(encoding="utf-8")

    def test_it_sends_readers_to_the_client_and_names_classic(self):
        self.assertIn(CLIENT_PATH, self.text)
        self.assertIn(CLASSIC_PATH, self.text)

    def test_it_no_longer_tells_anyone_to_set_uiPath_to_the_old_default(self):
        """The sentence that was wrong, asserted gone rather than assumed rewritten.

        `"uiPath": "/app/"` as an instruction is now backwards -- it is the default -- and
        `"uiPath": "/"` points at a redirect to the very page it was meant to avoid.
        """
        for stale in ('"uiPath": "/app/"', '"uiPath": "/"'):
            self.assertNotIn(
                stale, self.text,
                f'docs/DEPLOY_UBUNTU.md still tells the reader to set {stale}, which is no longer '
                f'the way to choose a UI: the default is {CLIENT_PATH} and the alternative is '
                f'{CLASSIC_PATH}',
            )


if __name__ == "__main__":
    unittest.main()
