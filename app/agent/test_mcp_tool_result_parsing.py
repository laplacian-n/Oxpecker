"""Regression: parsing an MCP CallToolResult must read the real SDK field name.

The SDK's `CallToolResult` names its structured field `structuredContent` (camelCase, per the MCP
wire schema). The client read `result.structured_content`, which does not exist — every tool call
raised AttributeError, so a wave worker died the moment the model used a tool and autonomous recon
produced nothing. These tests pin the extraction against a real `mcp.types.CallToolResult` so a
future SDK rename fails here instead of silently in a live run.
"""
from __future__ import annotations

import unittest

from mcp import types as mcp_types

from .mcp_tool_client import _parse_tool_result


class ParseToolResultTest(unittest.TestCase):
    def test_structured_content_is_returned(self):
        result = mcp_types.CallToolResult(content=[], structuredContent={"ok": True, "n": 3})
        self.assertEqual(_parse_tool_result(result), {"ok": True, "n": 3})

    def test_the_sdk_field_is_camelcase(self):
        # The exact bug: the snake_case attribute does not exist on the SDK model. If a future SDK
        # adds it as an alias this still passes (getattr handles both), but the camelCase field must
        # exist — that is the contract _parse_tool_result relies on.
        result = mcp_types.CallToolResult(content=[])
        self.assertTrue(hasattr(result, "structuredContent"))

    def test_falls_back_to_json_text_block(self):
        result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text='{"ok": false, "error": "x"}')])
        self.assertEqual(_parse_tool_result(result), {"ok": False, "error": "x"})

    def test_non_json_text_is_reported_not_raised(self):
        result = mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text="not json at all")])
        out = _parse_tool_result(result)
        self.assertFalse(out["ok"])
        self.assertIn("non-JSON tool output", out["error"])

    def test_empty_result(self):
        self.assertEqual(
            _parse_tool_result(mcp_types.CallToolResult(content=[])),
            {"ok": False, "error": "empty tool result"})


if __name__ == "__main__":
    unittest.main()
