"""The agent's brokered browser tools — the page-driving and the schemas, in one place.

Two runtimes drive the persistent per-engagement live browser as an AGENT (not as the operator,
whose door is dev_server's deliberately un-brokered `/browser/input`):

  * the chat/assistant agent, in dev_server's own process (`dev_server._run_tool`), and
  * the autonomous wave workers, whose `AgentLoop` runs in that same process but dispatches most
    tools to the security-tools MCP subprocess (`agent/loop.py`).

Both must go through the broker, both must land on the right page (the operator's for the chat
agent, a per-worker page for a wave worker — `live.LiveBrowser.call(..., worker=...)`), and both
must return the SAME shape. So the tiny page-driving closures and the tool schemas live here, and
neither runtime keeps its own copy to drift from the other.

What is NOT here: the gate. Scope, RoE action class, kill switch, taint and audit are the broker's
and the live browser's egress handler's job — this module only drives a page it is handed and
reports what it landed on.
"""
from __future__ import annotations

from typing import Any

# The two tools this module backs. Each runtime uses this set to recognise a browser tool call and
# route it to the in-process live browser instead of the MCP subprocess (loop.py) or the generic
# dispatch (dev_server). Kept as a named set so the "which tools reach the live browser" fact has
# one definition.
BROWSER_LIVE_TOOLS = {"browser_navigate", "browser_click"}

# How much visible text to hand back to the model. Enough to reason about the page, bounded so one
# navigation cannot blow the context budget.
_MAX_TEXT = 4000
_NAV_TIMEOUT_MS = 20000


def _page_state(page: Any) -> dict:
    return {"url": page.url, "title": page.title(), "text": page.inner_text("body")[:_MAX_TEXT]}


def navigate(page: Any, url: str) -> dict:
    """Drive `page` to `url` and report where it landed. Runs on the browser thread (handed to
    `LiveBrowser.call`); the browser's own egress handler aborts an out-of-scope request here."""
    page.goto(url, timeout=_NAV_TIMEOUT_MS)
    return _page_state(page)


def click(page: Any, x: int, y: int) -> dict:
    """Click at a pixel on `page`, let any resulting navigation settle, and report the new state."""
    page.mouse.click(x, y)
    page.wait_for_timeout(300)  # let any resulting navigation/paint settle
    return _page_state(page)


NAVIGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_navigate",
        "description": ("Drive the PERSISTENT per-engagement browser to an in-scope URL and read the "
                        "page (url, title, visible text). Unlike browser_fetch (a fresh headless "
                        "render), this keeps cookies/sign-ins across calls, so you can log in once "
                        "and keep going. In scope, broker-mediated; the operator watches it live."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "the in-scope URL to navigate to"},
        }, "required": ["url"]},
    },
}

CLICK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_click",
        "description": ("Click at a pixel position on the current page of the persistent browser, "
                        "then read the resulting page (url, title, text). Use the coordinates from "
                        "what you saw in the last browser_navigate/click result."),
        "parameters": {"type": "object", "properties": {
            "x": {"type": "integer", "description": "x pixel on the page"},
            "y": {"type": "integer", "description": "y pixel on the page"},
        }, "required": ["x", "y"]},
    },
}
