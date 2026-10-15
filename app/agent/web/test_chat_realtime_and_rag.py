"""Two chat-quality fixes that a model call cannot exercise in CI, pinned by their observable form:

  1. RAG grounding — the default prompt tells the assistant to consult knowledge_search (the bundled
     corpus) before answering a technique/tool/payload question, instead of answering from memory.
     The live symptom was an answer that never touched the 547k-chunk index even when it helped.
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

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"


class RagGroundingPromptTest(unittest.TestCase):
    def test_answering_mode_tells_the_model_to_consult_the_knowledge_base(self):
        prompt = dev_server.DEFAULT_SYSTEM_PROMPT
        self.assertIn("knowledge_search", prompt)
        # It must say to search FIRST / before answering, not merely mention the tool exists.
        self.assertRegex(prompt, r"(?i)FIRST call knowledge_search|search first,? then answer")

    def test_it_frames_the_search_as_scope_free_so_assistant_mode_still_uses_it(self):
        # The whole point of #91 was that answering needs no scope; the RAG nudge must not reintroduce
        # a scope gate by implication, or the model will skip it in a no-target session.
        prompt = dev_server.DEFAULT_SYSTEM_PROMPT.lower()
        self.assertIn("local corpus", prompt)
        self.assertIn("no scope", prompt)


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


if __name__ == "__main__":
    unittest.main()
