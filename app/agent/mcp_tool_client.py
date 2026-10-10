"""Sync wrapper around the async MCP stdio client.

The MCP SDK is async-native; loop.py (and Phase 1's whole design) is deliberately synchronous
to keep the reliability story simple (see loop.py's module docstring on why tool-executing
turns avoid streaming/async assembly). Rather than rewrite the loop as async, this runs one
persistent event loop in a background thread for the lifetime of an AgentLoop and exposes a
plain blocking call_tool().

The stdio_client/ClientSession context managers use anyio cancel scopes, which must be
entered and exited within the *same* asyncio Task — spreading connect/call/close across
separate `run_coroutine_threadsafe` calls (each of which is its own top-level Task) breaks
that invariant. So the whole session lifetime runs as a single worker task; requests cross the
thread boundary through an asyncio.Queue, and responses come back via
concurrent.futures.Future (thread-safe by design, unlike asyncio.Future).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_STOP = object()


def _parse_tool_result(result) -> dict:
    """Map an MCP `CallToolResult` to the plain dict the loop expects.

    The structured field is `structuredContent` (camelCase, as the MCP wire schema and the SDK's
    pydantic model name it). Reading `result.structured_content` raised AttributeError on *every*
    tool call, so a wave worker died the instant the model first used a tool — which is why
    autonomous recon produced zero hypotheses and every report came back empty. getattr covers both
    spellings, so a future SDK that adds a snake_case alias keeps working. Falls back to the first
    JSON text block, then to an explicit empty-result error."""
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError:
                return {"ok": False, "error": f"non-JSON tool output: {block.text[:300]}"}
    return {"ok": False, "error": "empty tool result"}


class MCPToolClient:
    def __init__(self, server_module: str, server_args: list[str], connect_timeout_s: float = 30):
        args = ["-m", server_module, *server_args]
        # Run the server subprocess from the package root (the dir that contains the `agent`
        # package), so `python -m agent.security_mcp_server` resolves no matter what working
        # directory the parent was launched from. Without this, launching dev_server from anywhere
        # but app/ makes the subprocess fail with "No module named 'agent'" and every
        # broker-mediated tool (port_discovery, http_recon, knowledge_fetch, browser_fetch) silently
        # becomes unavailable — the agent then walks its phases but can never actually probe.
        import pathlib
        _pkg_root = pathlib.Path(__file__).resolve().parent.parent
        self._server_params = StdioServerParameters(
            command=sys.executable, args=args, cwd=str(_pkg_root))

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        self._queue: asyncio.Queue | None = None
        self._ready = threading.Event()
        self._connect_error: BaseException | None = None
        self._worker_future = asyncio.run_coroutine_threadsafe(self._worker(), self._loop)

        if not self._ready.wait(timeout=connect_timeout_s):
            raise TimeoutError("MCP server did not become ready in time")
        if self._connect_error is not None:
            raise self._connect_error

    async def _worker(self) -> None:
        self._queue = asyncio.Queue()
        try:
            async with stdio_client(self._server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._ready.set()
                    while True:
                        request = await self._queue.get()
                        if request is _STOP:
                            break
                        kind, payload, fut = request
                        try:
                            result = await self._handle(session, kind, payload)
                            fut.set_result(result)
                        except Exception as e:  # noqa: BLE001 - surfaced to the caller thread
                            fut.set_exception(e)
        except Exception as e:
            self._connect_error = e
            self._ready.set()

    async def _handle(self, session: ClientSession, kind: str, payload):
        if kind == "list":
            result = await session.list_tools()
            return [{"name": t.name, "description": t.description} for t in result.tools]

        name, arguments = payload
        result = await session.call_tool(name, arguments)
        return _parse_tool_result(result)

    def _submit(self, kind: str, payload=None, timeout: float | None = None):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, payload, fut))
        return fut.result(timeout=timeout)

    def list_tools(self) -> list[dict]:
        return self._submit("list")

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        return self._submit("call", (name, arguments), timeout=timeout)

    def close(self) -> None:
        if self._queue is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, _STOP)
        try:
            concurrent.futures.wait([self._worker_future], timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
