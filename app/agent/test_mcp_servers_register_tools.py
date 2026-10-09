"""Every MCP server in this package must import AND register its tools.

Three of the four could not be imported at all until they were ported here: they used
`mcp.server.mcpserver.MCPServer`, which no released `mcp` 1.x provides, while
security_mcp_server.py had already been moved to the real `FastMCP` API. Nothing noticed,
because nothing imports these modules -- they are launched as separate processes from
app/.mcp.json, so a broken import surfaces as a server that never starts rather than as a
failing test.

Importing is not the bar, though, and that is the point of the second assertion. A decorator
that silently stopped registering would leave every module here importable and every server
empty, which looks identical to healthy from the outside. The tool count is what distinguishes
"loads" from "works".
"""
from __future__ import annotations

import asyncio
import importlib
import unittest

# module -> the tools it must still expose, as a floor rather than an exact count, so adding a
# tool does not break the test while losing the whole registry does.
MCP_SERVERS = {
    "agent.mcp_tools_server": 3,
    "agent.oxpecker_control_mcp": 9,
    "agent.dev_mcp_server": 25,
    "agent.security_mcp_server": 8,
}


class EveryMCPServerLoadsAndRegisters(unittest.TestCase):
    def test_each_server_imports(self):
        for name in MCP_SERVERS:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_each_server_registers_its_tools(self):
        for name, floor in MCP_SERVERS.items():
            with self.subTest(module=name):
                server = getattr(importlib.import_module(name), "server", None)
                self.assertIsNotNone(server, f"{name} has no module-level `server`")
                tools = asyncio.run(server.list_tools())
                self.assertGreaterEqual(
                    len(tools), floor,
                    f"{name} registered {len(tools)} tools, expected at least {floor} -- an "
                    "importable server with an empty registry starts fine and does nothing",
                )

    def test_no_server_still_uses_the_import_path_that_does_not_exist(self):
        """The specific defect, named, so a copy-paste from an old file is caught at once."""
        import pathlib

        agent_dir = pathlib.Path(__file__).resolve().parent
        offenders = [
            str(p.relative_to(agent_dir))
            for p in agent_dir.rglob("*.py")
            if not p.name.startswith("test_")
            and "from mcp.server.mcpserver import" in p.read_text()
        ]
        self.assertEqual(offenders, [], "these import a path no released mcp 1.x provides")


if __name__ == "__main__":
    unittest.main()
