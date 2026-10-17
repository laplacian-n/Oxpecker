"""Why dev_server.py was split two blocks at a time and not all at once.

dev_server.py is large, and the obvious remedy -- move each section into its own module and
re-export the names so nothing has to change -- is not behaviour-preserving for this suite. It is
behaviour-preserving for CALLS and silently not for PATCHES, which is the half nobody checks:

    from .owner import SESSIONS          # the "shim"
    patch.object(shim, "SESSIONS", {...}) # looks like it worked
    owner.who_am_i()                      # still reads the original

`from X import name` binds a NEW name in the importing module. patch.object rebinds that new name
and the owning module keeps reading its own. Mutating a shared object through the shim still
reaches the owner (same object); REBINDING does not. Tests that patch see their value and the
server under test does not.

This suite patches nine dev_server attributes:

    _persist  DATA_DIR  STATE_FILE  _run_tool  _brokers
    _audit_for  _dispatch_via_broker  _live_browser_for  _agent_live_browser

DATA_DIR and _persist together are what keep the tests out of the real state directory. Move the
code that reads them into another module behind a re-export and those patches become no-ops: the
suite then writes into agent/state for real, and mostly still passes. The state-leak gate in CI
would catch some of it by the file count and nothing would say why.

So rag.py and prompt.py were chosen for holding no patched state at all (checked, not assumed),
and the Session/routes/app split is left undone deliberately. Whoever does it has to move each
patch target's READERS with it, or change the tests to patch the owning module. This file exists
so that is a known cost rather than a surprise, and it demonstrates the mechanism on a fixture of
its own rather than asserting it in prose.
"""
from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

WEB = Path(__file__).resolve().parent
PATCHED_ATTRS = (
    "_persist", "DATA_DIR", "STATE_FILE", "_run_tool", "_brokers",
    "_audit_for", "_dispatch_via_broker", "_live_browser_for", "_agent_live_browser",
)


def _two_modules():
    """An owner module and a module that re-exports from it, built in memory.

    A real fixture rather than a description: the claim is about how Python binds names, so it is
    demonstrated with modules and patch.object, the same tools the suite uses.
    """
    owner = types.ModuleType("hazard_owner")
    exec(
        "STATE = {'real': True}\n"
        "PATH = '/real'\n"
        "def who_am_i():\n"
        "    return (dict(STATE), PATH)\n",
        owner.__dict__,
    )
    shim = types.ModuleType("hazard_shim")
    shim.STATE = owner.STATE
    shim.PATH = owner.PATH
    shim.who_am_i = owner.who_am_i
    return owner, shim


class ReexportPatchHazardTest(unittest.TestCase):
    def test_patching_a_reexported_name_does_not_reach_the_owner(self):
        """The whole reason the split stopped where it did."""
        owner, shim = _two_modules()
        with patch.object(shim, "PATH", "/tmp/fake"), patch.object(shim, "STATE", {"fake": True}):
            self.assertEqual(shim.PATH, "/tmp/fake")          # the test sees its value
            state, path = shim.who_am_i()                      # the code does not
            self.assertEqual(path, "/real")
            self.assertEqual(state, {"real": True})
        self.assertEqual(owner.PATH, "/real")

    def test_mutating_through_the_shim_does_reach_it_which_is_what_makes_this_subtle(self):
        """Half of it works, which is why a mechanical split looks fine.

        Mutation is shared (one object, two names) and rebinding is not. A split that only ever
        appends to a dict survives; one whose tests redirect a path does not, and both look the
        same in the diff.
        """
        owner, shim = _two_modules()
        shim.STATE["added"] = 1
        self.assertIn("added", owner.who_am_i()[0])


class PatchTargetsStillLiveInDevServerTest(unittest.TestCase):
    """The nine names above must stay where the tests patch them.

    Asserted against the module rather than against a list in a comment: if a later split moves
    one behind a re-export, this fails and names it, instead of the suite quietly starting to
    write into the real state directory.
    """

    def test_every_patched_attribute_is_defined_in_dev_server_itself(self):
        from . import dev_server

        source = (WEB / "dev_server.py").read_text(encoding="utf-8")
        reexported = set()
        for line in source.splitlines():
            m = re.match(r"\s*from \.(\w+) import (.+)", line)
            if m:
                reexported.update(n.strip().strip("(),") for n in m.group(2).split(","))

        moved = sorted(a for a in PATCHED_ATTRS if a in reexported)
        self.assertEqual(
            moved, [],
            f"{moved} are now re-exported into dev_server from another module, but the suite "
            f"patches them with patch.object(dev_server, ...), which no longer reaches the code "
            f"that reads them. Either move their readers too, or change those tests to patch the "
            f"owning module. DATA_DIR and _persist are the dangerous pair: without them the "
            f"suite writes into the real agent/state.",
        )
        # And they are really there, so the check above is not passing because the names vanished.
        for attr in PATCHED_ATTRS:
            self.assertTrue(hasattr(dev_server, attr), f"dev_server lost {attr}")

    def test_what_was_moved_is_still_reachable_by_its_old_name(self):
        """The other half of the contract: the extraction must not break plain reads.

        These four are read as dev_server.<name> in the suite and in dev_server's own turn
        builder, which is exactly the usage a re-export does preserve.
        """
        from . import dev_server

        self.assertEqual(dev_server.VectorRAG.__module__, "agent.web.rag")
        self.assertEqual(dev_server.TFIDFStore.__module__, "agent.web.rag")
        self.assertEqual(dev_server._tool_list_block.__module__, "agent.web.prompt")
        self.assertIn("{platform_facts}", (WEB / "prompt.py").read_text(encoding="utf-8"))
        self.assertNotIn("{platform_facts}", dev_server.DEFAULT_SYSTEM_PROMPT)


class ExtractedModulesAreSelfContainedTest(unittest.TestCase):
    def test_neither_extracted_module_imports_dev_server(self):
        """A module that imports back into dev_server is a cycle waiting for an import order.

        This is also the test that would have stopped the tool schemas and the stage protocol
        being moved with these two: both reference Session, which stays in dev_server, so moving
        them means importing it back.
        """
        for name in ("rag.py", "prompt.py"):
            text = (WEB / name).read_text(encoding="utf-8")
            # An IMPORT, not a mention: both modules' docstrings say which file they came out of,
            # and the first version of this check failed on that prose — a test that fails on its
            # own explanation is testing the wrong string.
            offenders = [
                line for line in text.splitlines()
                if re.match(r"\s*(from\s+\.?\S*dev_server|import\s+\S*dev_server)", line)
            ]
            self.assertEqual(offenders, [], f"{name} imports dev_server: {offenders}")

    def test_they_import_cleanly_on_their_own(self):
        # Imported directly rather than through dev_server, so a dependency this module needs and
        # does not import shows up here instead of being supplied by its old neighbours.
        for mod in ("agent.web.rag", "agent.web.prompt"):
            sys.modules.pop(mod, None)
        import importlib

        self.assertTrue(importlib.import_module("agent.web.rag").VectorRAG)
        self.assertTrue(importlib.import_module("agent.web.prompt").DEFAULT_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
