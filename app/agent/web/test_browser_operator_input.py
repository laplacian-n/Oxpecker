"""The operator-input endpoint (POST .../browser/input) and the three conditions that keep its
deliberate broker-bypass honest (CLIENT_UI_DESIGN §6.4):

  1. origin is the door — authorization is reaching the authed route, never a body field;
  2. no tool reaches it — the model's tool registry has no path to page.mouse/keyboard here;
  3. not emit.current_worker() — this handler never consults that thread-local as an identity.

Driving a real Chromium is the live-browser test's job; here the browser is a fake whose `call`
runs the handler's function against a recording page, so the endpoint's mapping is checked without
Playwright.

Run:  python3 -m unittest agent.web.test_browser_operator_input
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class _RecordingMouse:
    def __init__(self): self.events = []
    def click(self, x, y, button="left"): self.events.append(("click", x, y, button))
    def move(self, x, y): self.events.append(("move", x, y))
    def wheel(self, dx, dy): self.events.append(("wheel", dx, dy))


class _RecordingKeyboard:
    def __init__(self): self.events = []
    def press(self, key): self.events.append(("press", key))
    def type(self, text): self.events.append(("type", text))


class _FakePage:
    def __init__(self): self.mouse = _RecordingMouse(); self.keyboard = _RecordingKeyboard()


class _FakeBrowser:
    def __init__(self): self.page = _FakePage()
    def call(self, fn, **_kw): return fn(self.page)


class BrowserOperatorInputTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = Path(tempfile.mkdtemp(prefix="browser-input-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-key.txt")  # no key -> open
        p.start(); self.addCleanup(p.stop)
        self.browser = _FakeBrowser()
        bp = patch.object(dev_server, "_live_browser_for", lambda eid: self.browser)
        bp.start(); self.addCleanup(bp.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def _post(self, body):
        return self.client.post("/api/engagements/eng-x/browser/input", json=body)

    def test_click_drives_the_mouse_at_the_given_coords(self):
        r = self._post({"action": "click", "x": 640, "y": 360, "button": "left"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertIn(("click", 640.0, 360.0, "left"), self.browser.page.mouse.events)

    def test_type_and_key_drive_the_keyboard(self):
        self._post({"action": "type", "text": "hello"})
        self._post({"action": "key", "key": "Enter"})
        self.assertIn(("type", "hello"), self.browser.page.keyboard.events)
        self.assertIn(("press", "Enter"), self.browser.page.keyboard.events)

    def test_scroll_drives_the_wheel(self):
        self._post({"action": "scroll", "deltaY": 120})
        self.assertIn(("wheel", 0.0, 120.0), self.browser.page.mouse.events)

    def test_unknown_action_is_rejected(self):
        self.assertEqual(self._post({"action": "evaluate", "x": 1}).status_code, 400)

    def test_bad_button_falls_back_to_left_never_arbitrary(self):
        self._post({"action": "click", "x": 1, "y": 1, "button": "../../etc"})
        self.assertEqual(self.browser.page.mouse.events[-1], ("click", 1.0, 1.0, "left"))

    # ── condition 1: origin is the door, not a body field ──
    def test_an_actor_field_in_the_body_grants_nothing(self):
        # The handler must read only action/geometry/keys — an "operator"/"actor"/"worker" claim in
        # the body is ignored, so a caller cannot promote itself by declaring one.
        r = self._post({"action": "click", "x": 2, "y": 2, "operator": "admin", "worker": "w9",
                        "actor": "root"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.browser.page.mouse.events[-1], ("click", 2.0, 2.0, "left"))

    # ── condition 2: no tool reaches this path (exact-set, like _BROKER_MEDIATED) ──
    def test_no_model_tool_can_reach_operator_input(self):
        names = set()
        for schema in dev_server.TOOL_SCHEMAS + dev_server.SECURITY_TOOL_SCHEMAS:
            fn = schema.get("function") if isinstance(schema, dict) else None
            if fn and fn.get("name"):
                names.add(fn["name"])
        names |= set(dev_server._TOOL_NAME_ALIASES)
        # The OPERATOR-INPUT endpoint handler (browser_input) and raw page-input verbs must not be
        # callable as a model tool. The brokered agent tools browser_navigate/browser_click are a
        # DIFFERENT, gated path (they go through the broker) and are intentionally present — they are
        # not forbidden here.
        forbidden = {"browser_input", "mouse_click", "key_press", "type_text",
                     "page_mouse", "page_keyboard"}
        self.assertEqual(names & forbidden, set(),
                         f"a model tool reaches operator input: {names & forbidden}")
        # And no exposed tool name hints at raw (un-brokered) input control.
        self.assertFalse([n for n in names if "mouse" in n or "keyboard" in n])

    # ── condition 3: the handler never consults emit.current_worker() ──
    def test_handler_does_not_use_current_worker_as_identity(self):
        # Check the names the code actually REFERENCES (co_names of the handler and its nested
        # apply() closure), not the source text — condition 3 is explained in the docstring on
        # purpose; what must be absent is a real reference to the thread-local.
        def referenced(fn):
            code = fn.__code__
            out = set(code.co_names)
            for c in code.co_consts:
                if hasattr(c, "co_names"):  # nested function / comprehension code object
                    out |= set(c.co_names)
            return out
        self.assertNotIn("current_worker", referenced(dev_server.browser_input))


if __name__ == "__main__":
    unittest.main()
