"""LlamaClient streaming assembly (agent/llama_client.py). Added 2026-09-01 so the web UI's
reply can type itself out instead of landing all at once. No live llama-server needed — a fake
SSE body is fed through _stream_chat_completion and we assert it reassembles into the exact
non-streaming response shape (content / reasoning_content / tool_calls / usage), and that the
on_delta callback saw the content fragments in order.
"""
from __future__ import annotations

import json
import unittest

from agent.llama_client import LlamaClient, LlamaClientError


class _FakeResp:
    def __init__(self, lines, status_code=200):
        self.status_code = status_code
        self.text = ""
        self._lines = lines

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        pass


def _sse(*objs):
    lines = []
    for o in objs:
        lines.append("data: " + (o if isinstance(o, str) else json.dumps(o)))
        lines.append("")  # blank line between events, like a real SSE stream
    lines.append("data: [DONE]")
    return lines


class StreamAssemblyTest(unittest.TestCase):
    def setUp(self):
        self.client = LlamaClient(base_url="http://x", api_key=None)

    def _run(self, lines):
        seen = []
        captured = {}

        def fake_post(url, json=None, timeout=None, stream=False):
            captured["payload"] = json
            captured["stream"] = stream
            return _FakeResp(lines)

        self.client._session.post = fake_post
        resp = self.client.chat_completions([{"role": "user", "content": "hi"}], on_delta=seen.append)
        return resp, seen, captured

    def test_content_deltas_reassemble_and_stream_in_order(self):
        resp, seen, cap = self._run(_sse(
            {"choices": [{"delta": {"content": "Hello"}}]},
            {"choices": [{"delta": {"content": ", "}}]},
            {"choices": [{"delta": {"content": "world"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}},
        ))
        self.assertTrue(cap["stream"])  # on_delta -> stream:true
        self.assertEqual(seen, ["Hello", ", ", "world"])
        self.assertEqual(resp["choices"][0]["message"]["content"], "Hello, world")
        self.assertEqual(resp["choices"][0]["finish_reason"], "stop")
        self.assertEqual(resp["usage"]["completion_tokens"], 3)

    def test_reasoning_content_kept_out_of_content(self):
        resp, seen, _ = self._run(_sse(
            {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
        ))
        msg = resp["choices"][0]["message"]
        self.assertEqual(msg["content"], "answer")
        self.assertEqual(msg["reasoning_content"], "thinking...")
        self.assertEqual(seen, ["answer"])  # no on_reasoning_delta given -> reasoning not streamed

    def test_on_reasoning_delta_streams_reasoning_separately_from_content(self):
        content_seen, reasoning_seen = [], []
        captured = {}

        def fake_post(url, json=None, timeout=None, stream=False):
            captured["stream"] = stream
            return _FakeResp(_sse(
                {"choices": [{"delta": {"reasoning_content": "let me "}}]},
                {"choices": [{"delta": {"reasoning_content": "think"}}]},
                {"choices": [{"delta": {"content": "answer"}}]},
            ))

        self.client._session.post = fake_post
        resp = self.client.chat_completions(
            [{"role": "user", "content": "hi"}],
            on_delta=content_seen.append, on_reasoning_delta=reasoning_seen.append,
        )
        self.assertTrue(captured["stream"])
        self.assertEqual(reasoning_seen, ["let me ", "think"])
        self.assertEqual(content_seen, ["answer"])
        self.assertEqual(resp["choices"][0]["message"]["reasoning_content"], "let me think")
        self.assertEqual(resp["choices"][0]["message"]["content"], "answer")

    def test_on_reasoning_delta_alone_still_turns_on_streaming(self):
        reasoning_seen = []
        captured = {}

        def fake_post(url, json=None, timeout=None, stream=False):
            captured["stream"] = stream
            return _FakeResp(_sse({"choices": [{"delta": {"reasoning_content": "hm"}}]}))

        self.client._session.post = fake_post
        self.client.chat_completions(
            [{"role": "user", "content": "hi"}], on_reasoning_delta=reasoning_seen.append,
        )
        self.assertTrue(captured["stream"])  # no on_delta at all, but reasoning callback alone streams
        self.assertEqual(reasoning_seen, ["hm"])

    def test_tool_calls_assembled_by_index(self):
        resp, seen, _ = self._run(_sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_a", "type": "function", "function": {"name": "http_recon", "arguments": "{\"url\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "\"http://x\"}"}}]}}]},
        ))
        tcs = resp["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(tcs), 1)
        self.assertEqual(tcs[0]["id"], "call_a")
        self.assertEqual(tcs[0]["function"]["name"], "http_recon")
        self.assertEqual(json.loads(tcs[0]["function"]["arguments"]), {"url": "http://x"})
        self.assertEqual(seen, [])  # a pure tool-call turn streams no content

    def test_malformed_chunks_and_keepalives_are_skipped(self):
        resp, seen, _ = self._run([
            "data: ", "", "data: {not json", "", ": keepalive comment",
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}), "",
            "data: [DONE]",
        ])
        self.assertEqual(resp["choices"][0]["message"]["content"], "ok")
        self.assertEqual(seen, ["ok"])

    def test_non_200_raises(self):
        def fake_post(url, json=None, timeout=None, stream=False):
            r = _FakeResp([], status_code=500)
            r.text = "boom"
            return r
        self.client._session.post = fake_post
        with self.assertRaises(LlamaClientError):
            self.client.chat_completions([{"role": "user", "content": "hi"}], on_delta=lambda s: None)

    def test_no_callback_keeps_the_non_streaming_path(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["stream"] = json.get("stream")
            return _FakeResp([])  # not used; _post reads .json()

        # _post calls resp.json(); give it one
        class R(_FakeResp):
            def json(self):
                return {"choices": [{"message": {"role": "assistant", "content": "x"}}]}

        def fp(url, json=None, timeout=None):
            captured["stream"] = json.get("stream")
            return R([])

        self.client._session.post = fp
        self.client.chat_completions([{"role": "user", "content": "hi"}])
        self.assertFalse(captured["stream"])


if __name__ == "__main__":
    unittest.main()
