"""Two chat-quality fixes that a model call cannot exercise in CI, pinned by their observable form:

  1. RAG grounding is already automatic (every turn runs _rag.search and injects excerpts), but it
     was invisible, so it read as "it never consults RAG". The fix SURFACES it — the turn emits a
     `rag_consulted` event and the UI shows a knowledge-base chip — and the prompt tells the model
     to USE the excerpts it is handed and cite them. The retrieval depth was bumped 2 -> 4.
  2. Real-time streaming — the classic UI (static/index.html) renders the FIRST reasoning/answer
     delta of a turn with a full render() so the live element exists, then streams into it on every
     later delta. Before the fix, render() at task start ran with the streams empty, so the live
     element did not exist and updateStreaming*() patched nothing — the whole answer appeared at
     once on task_done, which read as "slow / not real time".
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from . import dev_server

WEB = Path(__file__).resolve().parents[1] / "web"
INDEX_HTML = WEB / "static" / "index.html"
DEV_SERVER = WEB / "dev_server.py"


class RagGroundingTest(unittest.TestCase):
    def test_prompt_tells_the_model_to_use_the_auto_provided_excerpts(self):
        prompt = dev_server.DEFAULT_SYSTEM_PROMPT
        self.assertIn("knowledge_search", prompt)
        self.assertRegex(prompt, r"(?is)knowledge-base\s+excerpts")  # wraps across a line in the prompt
        self.assertRegex(prompt, r"(?i)automatically")  # excerpts are handed to it, not opt-in

    def test_it_frames_the_search_as_scope_free_so_assistant_mode_still_uses_it(self):
        # #91: answering needs no scope; the RAG wording must not reintroduce a scope gate.
        prompt = dev_server.DEFAULT_SYSTEM_PROMPT.lower()
        self.assertIn("local corpus", prompt)
        self.assertIn("no scope", prompt)

    def test_the_turn_surfaces_the_retrieval_to_the_ui(self):
        # _run_agent_turn emits a rag_consulted event carrying the sources, so the operator can see
        # the answer is grounded — the whole point (the silent version read as "never consults RAG").
        src = DEV_SERVER.read_text(encoding="utf-8")
        self.assertIn('"type": "rag_consulted"', src)
        self.assertRegex(src, r"_rag\.search\(user_content, top_k=4\)")  # bumped from 2

    def test_the_ui_renders_the_knowledge_base_chip(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn("case 'rag_consulted':", html)
        self.assertIn("Knowledge base", html)


class RealtimeStreamingRenderTest(unittest.TestCase):
    def setUp(self):
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_first_assistant_delta_renders_to_create_the_live_element(self):
        # The first delta (stream empty) must call render(); later deltas use the fast updateStreaming.
        m = re.search(r"case 'assistant_delta':(.+?)break;", self.html)
        self.assertIsNotNone(m, "assistant_delta handler not found")
        branch = m.group(1)
        self.assertIn("if(!S.streamingText)", branch)
        self.assertIn("render()", branch)
        self.assertIn("updateStreamingText()", branch)  # the fast path is still there for later deltas

    def test_first_reasoning_delta_renders_to_create_the_live_element(self):
        m = re.search(r"case 'reasoning_delta':(.+?)break;", self.html)
        self.assertIsNotNone(m, "reasoning_delta handler not found")
        branch = m.group(1)
        self.assertIn("if(!S.streamingReasoning)", branch)
        self.assertIn("render()", branch)
        self.assertIn("updateStreamingReasoning()", branch)

    def test_the_live_area_renders_the_streaming_message_not_a_bare_spinner(self):
        # The real bug: the transcript's live area called renderWorkingIndicator() (a spinner with no
        # #streaming-content/#streaming-reasoning), so the streamed tokens had nowhere to land and
        # only appeared on task_done. It must render renderStreamingMessage(), which carries those
        # elements. renderStreamingMessage was defined but never called.
        self.assertRegex(
            self.html,
            r"role==='user'\)\?renderStreamingMessage\(\)",
            "the live streaming area must render renderStreamingMessage(), which holds the live"
            " #streaming-content/#streaming-reasoning elements",
        )
        self.assertIn("renderStreamingMessage()", self.html)

    def test_the_sse_endpoint_disables_proxy_buffering(self):
        src = DEV_SERVER.read_text(encoding="utf-8")
        self.assertRegex(src, r'"X-Accel-Buffering":\s*"no"')


class NoStoreOnTheUiShellsTest(unittest.TestCase):
    """The UI shells are served no-store so a client never shows a stale page after the server's UI
    is updated — the "I reinstalled and nothing changed" symptom was a cached shell, since these
    changes are server-side (the .exe is a thin client that loads the UI from the server)."""

    def test_the_classic_ui_shell_is_served_no_store(self):
        from fastapi.testclient import TestClient

        with TestClient(dev_server.app) as client:
            # / serves the classic UI (no-store), and /classic is its alias. The React client at
            # /app/ is retired — it redirects to / — so there is no second shell to check.
            self.assertEqual(client.get("/").headers.get("cache-control"), "no-store")
            self.assertEqual(client.get("/classic").headers.get("cache-control"), "no-store")


class ClassicUiHeaderTest(unittest.TestCase):
    """The classic UI is the whole product now, so its header must not link to the retired React
    client (a door into a UI that redirects straight back is a trap), and the update check must
    still be reachable from it."""

    def setUp(self):
        self.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_the_header_no_longer_links_to_the_retired_react_client(self):
        self.assertNotIn("openWorkspace", self.html,
                         "the classic header still has the Workspace link into /app/, which is "
                         "retired — the operator clicks it and lands back where they were")
        self.assertNotIn("/app/", self.html)

    def test_the_update_button_is_method_detected_like_the_react_one(self):
        # Rendered only when the shell actually exposes checkForUpdates (a browser tab / old shell
        # has no such method), mirroring UpdateButton.tsx — never a button that throws on click.
        self.assertIn("window.oxpecker&&window.oxpecker.checkForUpdates", self.html)
        self.assertIn("data-action=\"checkUpdate\"", self.html)
        self.assertIn("case 'checkUpdate'", self.html)
        self.assertIn("async function checkUpdate(", self.html)


if __name__ == "__main__":
    unittest.main()
