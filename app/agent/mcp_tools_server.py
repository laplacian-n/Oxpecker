"""Stdio MCP server exposing run_command/read_file/write_file — Topology B (§6): tool servers
run on the *client* machine, bound to localhost via stdio, never on the GPU inference box.

This is a thin adapter over agent/tools/*.py — no logic duplication. Launched as a subprocess
by the client-side orchestrator (mcp_tool_client.py), one process per AgentLoop, so
--workspace and --dangerous-local are fixed for its whole lifetime (matches how Phase 1
already scopes a single workspace per agent process).
"""
from __future__ import annotations

import argparse
from pathlib import Path

# `mcp.server.mcpserver.MCPServer` is not a path any released `mcp` 1.x provides, so this
# module could not be imported at all. FastMCP is the real API and takes exactly what this
# file already passes: FastMCP(name), .tool(name=, description=), .run(). Same fix as
# security_mcp_server.py, which carries the longer explanation; these three were left
# behind when it was made, and nothing noticed because nothing imports them.
from mcp.server import FastMCP

from .tools import read_file as read_file_tool
from .tools import run_command as run_command_tool
from .tools import write_file as write_file_tool

server = FastMCP("localai-phase1-tools")

_workspace_root: Path
_dangerous_local: bool = False
_isolation_tier: str = "bubblewrap"  # ADR-0004


@server.tool(description=run_command_tool.SCHEMA["function"]["description"])
def run_command(argv: list[str], cwd: str | None = None, timeout_s: int | None = None) -> dict:
    return run_command_tool.run(
        argv,
        _workspace_root,
        cwd=cwd,
        timeout_s=timeout_s,
        dangerous_local=_dangerous_local,
        isolation_tier=_isolation_tier,
    )


@server.tool(description=read_file_tool.SCHEMA["function"]["description"])
def read_file(path: str) -> dict:
    return read_file_tool.run(path, _workspace_root)


@server.tool(description=write_file_tool.SCHEMA["function"]["description"])
def write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    return write_file_tool.run(path, content, _workspace_root, mode)


def main() -> None:
    global _workspace_root, _dangerous_local, _isolation_tier
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dangerous-local", action="store_true")
    parser.add_argument("--isolation-tier", default="bubblewrap", choices=["direct", "bubblewrap"])  # ADR-0004
    args = parser.parse_args()

    _workspace_root = Path(args.workspace)
    _dangerous_local = args.dangerous_local
    _isolation_tier = args.isolation_tier
    server.run()  # defaults to stdio transport


if __name__ == "__main__":
    main()
