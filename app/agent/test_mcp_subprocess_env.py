"""Regression: the MCP server subprocess must inherit the live engagements root.

The MCP SDK launches a server with a minimal environment (PATH/HOME/USER/…), dropping
AGENT_ENGAGEMENTS_ROOT. The security MCP server resolves which engagement's RoE/scope to enforce
from config.ENGAGEMENTS_ROOT, so without it the subprocess fell back to the legacy default lab
engagement (localhost scope) regardless of the real target — every probe of an in-scope host was
denied and recon produced nothing. The dev server sets config.ENGAGEMENTS_ROOT at runtime (its
DATA_DIR), not via the environment, so MCPToolClient passes the live value through explicitly.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from . import config
from .mcp_tool_client import _subprocess_env


class SubprocessEnvTest(unittest.TestCase):
    def test_passes_the_live_engagements_root(self):
        original = config.ENGAGEMENTS_ROOT
        try:
            config.ENGAGEMENTS_ROOT = Path("/tmp/some/dev_data")  # a runtime override, not via env
            env = _subprocess_env()
            self.assertEqual(env["AGENT_ENGAGEMENTS_ROOT"], "/tmp/some/dev_data")
        finally:
            config.ENGAGEMENTS_ROOT = original

    def test_inherits_the_parent_environment(self):
        import os

        os.environ["OXPECKER_ENV_PROBE"] = "present"
        try:
            self.assertEqual(_subprocess_env().get("OXPECKER_ENV_PROBE"), "present")
        finally:
            del os.environ["OXPECKER_ENV_PROBE"]


if __name__ == "__main__":
    unittest.main()
