"""The one-line preview on a tool_call_started event (agent/loop.py._tool_call_preview).

It names what a call is about to do — the http target, the command, the search query — so the
chat can narrate "w1 → GET https://…" without the raw arguments. Bounded and derived from a single
known field per tool, so it never becomes a channel for the whole argument blob (that stays in the
audit log).
"""
from __future__ import annotations

import unittest

from .loop import _tool_call_preview


class ToolCallPreviewTest(unittest.TestCase):
    def test_http_request_shows_method_and_url(self):
        self.assertEqual(
            _tool_call_preview("http_request", {"method": "post", "url": "https://x.test/login"}),
            "POST https://x.test/login")

    def test_http_request_defaults_to_get(self):
        self.assertEqual(
            _tool_call_preview("http_request", {"url": "https://x.test/"}), "GET https://x.test/")

    def test_run_command_shows_the_command(self):
        self.assertEqual(
            _tool_call_preview("run_command", {"command": "nmap -sV 10.0.0.1"}), "nmap -sV 10.0.0.1")

    def test_knowledge_search_quotes_the_query(self):
        self.assertEqual(
            _tool_call_preview("knowledge_search", {"query": "SQLi login"}), '"SQLi login"')

    def test_record_hypothesis_shows_the_title(self):
        self.assertEqual(
            _tool_call_preview("record_hypothesis", {"title": "Login SQL injection"}),
            "Login SQL injection")

    def test_an_unknown_tool_has_no_preview(self):
        self.assertEqual(_tool_call_preview("some_future_tool", {"x": 1}), "")

    def test_it_is_bounded_and_never_dumps_the_whole_argument(self):
        long = "A" * 500
        out = _tool_call_preview("run_command", {"command": long})
        self.assertLessEqual(len(out), 80)
        self.assertTrue(out.endswith("…"))

    def test_bad_args_do_not_raise(self):
        self.assertEqual(_tool_call_preview("http_request", None), "GET")


if __name__ == "__main__":
    unittest.main()
