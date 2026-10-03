"""ADR-0004 regression: run_command's default isolation tier must be 'bubblewrap', not
'direct' — a security-relevant default this project deliberately flipped after both
independent reviews argued 'direct' shouldn't be the default. Explicit assertion so a future
accidental revert (e.g. someone "simplifying" a default arg) is caught immediately rather than
silently changing runtime behavior.
"""
from __future__ import annotations

import inspect
import unittest

from ..loop import AgentLoop
from .run_command import run


class TestDefaultIsolationTier(unittest.TestCase):
    def test_run_command_run_defaults_to_bubblewrap(self):
        sig = inspect.signature(run)
        self.assertEqual(sig.parameters["isolation_tier"].default, "bubblewrap")

    def test_agent_loop_defaults_to_bubblewrap(self):
        sig = inspect.signature(AgentLoop.__init__)
        self.assertEqual(sig.parameters["isolation_tier"].default, "bubblewrap")


if __name__ == "__main__":
    unittest.main()
