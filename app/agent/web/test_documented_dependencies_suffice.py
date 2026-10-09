"""The documented dependency list must be *sufficient*, not merely self-consistent.

Two tests already keep three copies of the install line in step — `test_electron_install_hint`
(Electron's failure hint) and `test_dev_server_imports` (docs/DEPLOY_UBUNTU.md). Both passed
for the entire life of the list while it was missing `requests`, `jinja2` and `PyYAML`, because
agreement between three copies says nothing about whether the thing they agree on works. A
developer with those packages already in their environment -- which is everyone who has run the
project once -- never sees it. The first machine that followed the documented instructions
exactly was a CI runner, and `agent.main` could not be imported there.

This test asks the question those two do not: **install only what the docs say, and do the
entry points import?** It answers it by blocking every third-party module the package imports
that the documented list does not cover, in a subprocess, and then importing the entry points.

One subprocess, not one per module, so this costs about a second rather than doubling the suite.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent
_DEV_SERVER = _AGENT_DIR / "web" / "dev_server.py"

# EVERY shipped module, not a chosen few. An earlier version of this test named two entry
# points, agent.main and agent.web.dev_server, and so declared fpdf2 and mcp "optional" -- while
# agent/pipeline/executor.py imports findings.report (fpdf) at module scope and loop.py's
# security tools reach security_mcp_server (mcp). The second CI run found all three. Picking a
# subset of the surface is how a sufficiency test becomes a sufficiency-for-what test, so the
# subset is gone: a documented install must import everything the package ships.
#
# Modules that guard their own optional import in a try/except are not exempted here, because
# they do not fail -- the guard is what this exercises. seccomp/pyseccomp and
# sentence_transformers are in that group, and stay blocked so the guards keep being tested.

# pip distribution name -> the name you actually `import`. Only the ones that differ.
_DIST_TO_MODULE = {
    "scikit-learn": "sklearn",
    "fpdf2": "fpdf",
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-multipart": "multipart",
    "sse-starlette": "sse_starlette",
    "beautifulsoup4": "bs4",
}


def documented_distributions() -> set[str]:
    """The uncommented `pip install` line in dev_server.py's prerequisites block.

    Deliberately the same source `test_dev_server_imports` reads, so there is one canonical list
    and this test cannot drift away from the one the other two enforce.
    """
    for line in _DEV_SERVER.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("pip install"):
            names = stripped[len("pip install"):].split()
            return {re.sub(r"\[.*?\]", "", n).strip("'\"").lower() for n in names}
    raise AssertionError("no uncommented 'pip install' line in dev_server.py's prerequisites")


def documented_modules() -> set[str]:
    return {_DIST_TO_MODULE.get(d, d.replace("-", "_")) for d in documented_distributions()}


def third_party_modules() -> set[str]:
    """Every top-level non-stdlib module imported by the shipped package (tests excluded)."""
    mods: set[str] = set()
    for path in _AGENT_DIR.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return {m for m in mods if m not in sys.stdlib_module_names and m != "agent"}


# Three modules cannot be imported in ANY environment, documented install or not: they do
# `from mcp.server.mcpserver import MCPServer`, and no such path exists in the mcp SDK (1.28
# has fastmcp, lowlevel, sse, stdio and no mcpserver). They have been this way since the commit
# that first added app/, so these MCP servers have never started. security_mcp_server.py uses
# the real `from mcp.server import FastMCP` and imports fine.
#
# They are listed rather than quietly excluded, and the second test below asserts the list is
# EXACT: fix one and this test tells you to take it off the list, so the carve-out cannot
# outlive the defect or grow to cover a new one.
KNOWN_BROKEN_IMPORTS = {
    "agent.dev_mcp_server",
    "agent.mcp_tools_server",
    "agent.oxpecker_control_mcp",
}

_PROBE = r'''
import importlib, sys
blocked = set(sys.argv[1].split(",")) - {""}
class Blocker:
    def find_module(self, name, path=None):
        top = name.split(".")[0]
        return self if top in blocked else None
    def load_module(self, name):
        raise ImportError(f"No module named {name.split('.')[0]!r}")
sys.meta_path.insert(0, Blocker())
failures = []
for mod in sys.argv[2:]:
    try:
        importlib.import_module(mod)
    except ImportError as e:
        failures.append(f"{mod}: {e}")
    except Exception as e:
        failures.append(f"{mod}: {type(e).__name__}: {e}")
print("\n".join(failures))
'''


def shipped_modules() -> list[str]:
    """Every importable module in the package, tests and __init__ aside."""
    mods = []
    for path in sorted(_AGENT_DIR.rglob("*.py")):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        mods.append(".".join(path.relative_to(_AGENT_DIR.parent).with_suffix("").parts))
    return mods


def _import_with_only(allowed_extra_blocked: set[str], targets=None) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, ",".join(sorted(allowed_extra_blocked)),
         *(targets if targets is not None else shipped_modules())],
        cwd=str(_AGENT_DIR.parent), capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(f"probe itself failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout.strip()


class DocumentedDependenciesSuffice(unittest.TestCase):
    def test_the_documented_list_is_enough_to_import_every_shipped_module(self):
        undocumented = third_party_modules() - documented_modules()
        failures = "\n".join(
            line for line in _import_with_only(undocumented).splitlines()
            if line.split(":")[0] not in KNOWN_BROKEN_IMPORTS
        )
        self.assertEqual(
            failures, "",
            "An install that follows the documented `pip install` line cannot import these "
            f"shipped modules.\n{failures}\n\nThe list is in dev_server.py's prerequisites block, "
            "and Electron's hint plus docs/DEPLOY_UBUNTU.md must be updated with it — two other "
            "tests enforce that those three agree.",
        )

    def test_the_probe_can_fail(self):
        """Anti-vacuity: the test above is worthless if blocking cannot break an import.

        Blocking a package the entry points genuinely need must produce a failure. Without this,
        a probe broken so that it blocks nothing would make the real test pass forever.
        """
        needed = "cryptography"
        self.assertIn(needed, documented_modules(), "fixture assumption changed")
        failures = _import_with_only({needed}, targets=["agent.main", "agent.web.dev_server"])
        self.assertNotEqual(
            failures, "",
            f"blocking {needed!r} did not break any entry point, so the probe is not blocking "
            "anything and the sufficiency test above proves nothing",
        )


class KnownBrokenListIsExact(unittest.TestCase):
    def test_every_listed_module_is_still_broken(self):
        """A carve-out that outlives its defect hides the next one that lands in the same place."""
        failures = _import_with_only(set(), targets=sorted(KNOWN_BROKEN_IMPORTS))
        still_broken = {line.split(":")[0] for line in failures.splitlines() if line.strip()}
        self.assertEqual(
            still_broken, KNOWN_BROKEN_IMPORTS,
            "KNOWN_BROKEN_IMPORTS no longer matches reality. A module that now imports must be "
            "removed from the set so the sufficiency test starts covering it again.",
        )


if __name__ == "__main__":
    unittest.main()
