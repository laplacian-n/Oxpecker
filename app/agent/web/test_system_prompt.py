"""The custom chat system prompt (Settings → System prompt): the operator can override the
assistant's prompt to test how wording affects refusals on authorised tasks. These pin the two
observable halves — the default-prompt endpoint the editor starts from, and that a session carries
the custom prompt — without a model call. The prompt is only ever the model's framing; the broker,
scope and deny gates run regardless and are not touched here.

Run directly: `python3 -m unittest agent.web.test_system_prompt`.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .. import config
from . import dev_server


class SystemPromptTest(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        self.tmp = Path(tempfile.mkdtemp(prefix="sysprompt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._saved = dict(dev_server._sessions)
        dev_server._sessions.clear()
        self.addCleanup(lambda: (dev_server._sessions.clear(), dev_server._sessions.update(self._saved)))
        self._patchers = [
            patch.object(config, "WEB_UI_API_KEY_FILE", self.tmp / "no-key.txt"),
            patch.object(dev_server, "_persist", lambda: None),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(dev_server.app)
        self.addCleanup(self.client.close)

    def test_default_prompt_endpoint_returns_the_template(self):
        body = self.client.get("/api/default-system-prompt").json()
        self.assertIn("prompt", body)
        # the editor starts from the real template, placeholder intact
        self.assertEqual(body["prompt"], dev_server.DEFAULT_SYSTEM_PROMPT)
        self.assertIn("@@TOOL_LIST@@", body["prompt"])

    def test_a_session_carries_the_custom_prompt(self):
        sid = self.client.post("/api/sessions", json={"system_prompt": "You are a helpful tester."}).json()["session_id"]
        self.assertEqual(dev_server._sessions[sid].system_prompt, "You are a helpful tester.")

    def test_default_is_empty_when_none_given(self):
        sid = self.client.post("/api/sessions", json={}).json()["session_id"]
        self.assertEqual(dev_server._sessions[sid].system_prompt, "")


if __name__ == "__main__":
    unittest.main()
