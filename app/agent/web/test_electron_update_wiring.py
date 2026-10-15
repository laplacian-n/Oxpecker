"""The on-demand update path is Electron-side (main.js / preload.js) and cannot be exercised by
the Python test runner, so these pin the wiring that would otherwise regress silently — the exact
properties that were MEASURED to matter (electron-updater 6.3.9's emit-and-reject error path, the
missing HTTP timeout, the unpackaged case) rather than re-deriving them from the source each time.

They also pin the version, in all three places npm keeps it, to the same value — the lock file was
found two releases behind (0.1.5 while package.json was 0.1.7), which would have shipped an
installer whose auto-update feed never matched its own version.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]            # app/
REPO = Path(__file__).resolve().parents[3]           # repo root
MAIN_JS = APP / "electron" / "main.js"
PRELOAD_JS = APP / "electron" / "preload.js"
PACKAGE_JSON = APP / "package.json"
PACKAGE_LOCK = APP / "package-lock.json"


class ElectronUpdateWiringTest(unittest.TestCase):
    def setUp(self):
        self.main = MAIN_JS.read_text(encoding="utf-8")
        self.preload = PRELOAD_JS.read_text(encoding="utf-8")

    def test_check_resolves_a_verdict_and_handles_the_unpackaged_case(self):
        # checkForUpdatesNow returns a verdict object, and reports "unsupported" when not packaged
        # (electron-updater reads app-update.yml, which only the installer carries).
        self.assertIn("function checkForUpdatesNow", self.main)
        self.assertRegex(self.main, r"!app\.isPackaged[\s\S]{0,200}status:\s*'unsupported'")
        for status in ("available", "current", "unsupported", "error"):
            self.assertIn(f"status: '{status}'", self.main, f"no {status!r} verdict branch")

    def test_settle_is_idempotent_and_removes_all_three_listeners(self):
        # The error path emits 'error' AND rejects → settle() is reachable twice and must guard.
        self.assertRegex(self.main, r"if\s*\(settled\)\s*return;", )
        self.assertRegex(self.main, r"settled\s*=\s*true;")
        # All three listeners are removed on settle, not just the one that fired.
        for evt in ("update-available", "update-not-available", "error"):
            self.assertIn(f"auto.removeListener('{evt}'", self.main,
                          f"cleanup does not remove the {evt!r} listener")

    def test_there_is_a_thirty_second_timeout(self):
        # getUpdateInfoAndProvider() is an HTTP fetch with no timeout of its own; without this the
        # button hangs on "Checking…" forever when a server accepts but never answers.
        self.assertRegex(self.main, r"setTimeout\([\s\S]{0,160}timed out[\s\S]{0,40}?,\s*30000\s*\)")

    def test_menu_and_ipc_are_wired_with_the_accelerator(self):
        # Both the client button (via IPC) and the Help menu reach the check; the accelerator
        # matters because autoHideMenuBar hides the bar.
        self.assertIn("ipcMain.handle('oxpecker:check-for-updates'", self.main)
        self.assertIn("ipcMain.handle('oxpecker:app-version'", self.main)
        self.assertIn("installAppMenu", self.main)
        self.assertIn("'CmdOrCtrl+U'", self.main)
        # preload exposes exactly the two the client detects on.
        self.assertIn("checkForUpdates:", self.preload)
        self.assertIn("appVersion:", self.preload)

    def test_the_version_is_the_same_in_all_three_npm_places(self):
        pkg = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        version = pkg["version"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(lock["version"], version,
                         "package-lock.json root version drifted from package.json")
        self.assertEqual(lock["packages"][""]["version"], version,
                         "package-lock.json packages.'' version drifted from package.json")


if __name__ == "__main__":
    unittest.main()
