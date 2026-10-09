"""The documented dependency list must be *sufficient*, not merely self-consistent.

Two tests already keep three copies of the install line in step — `test_electron_install_hint`
(Electron's failure hint) and `test_dev_server_imports` (docs/DEPLOY_UBUNTU.md). Both passed
for the entire life of the list while it was missing `requests`, `jinja2` and `PyYAML`, because
agreement between three copies says nothing about whether the thing they agree on works. A
developer with those packages already in their environment -- which is everyone who has run the
project once -- never sees it. The first machine that followed the documented instructions
exactly was a CI runner, and `agent.main` could not be imported there.

This test asks the question those two do not: **install only what the docs say, and do the
shipped modules import?**

It briefly carried a KNOWN_BROKEN_IMPORTS carve-out for three MCP servers that could not be
imported under the pinned SDK, together with a test asserting that set was exact. The carve-out
is gone because the defect is: porting those three to FastMCP made them importable, and the
exactness test is what demanded the list be emptied. The mechanism is in git history if another
long-lived defect ever needs it; an empty carve-out kept around is the thing it was written to
prevent. It answers it by blocking every third-party module the package imports
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


# Directories that are not this project's source. `knowledge_rag/corpus_src/` is the gitignored
# clone target for the RAG corpora (PayloadsAllTheThings, GTFOBins, LOLBAS); it is third-party
# text, not shipped code, so walking it is wrong on its own terms -- and it bites back. One of
# those repositories ships a *directory* literally named `Configuration Python __init__.py`,
# which `rglob("*.py")` matches happily and `read_text()` then raises IsADirectoryError on. Two
# tests crashed outright on any host that had followed this project's own documented corpus
# clone step, and passed everywhere else, which is why neither CI nor this container saw it.
_NOT_OUR_SOURCE = ("corpus_src",)


def _project_python_files(root):
    """Every .py file of ours under `root`: real files only, vendored corpora excluded."""
    for path in sorted(root.rglob("*.py")):
        if not path.is_file():
            continue  # a directory whose name ends in .py -- see _NOT_OUR_SOURCE
        if any(part in _NOT_OUR_SOURCE for part in path.parts):
            continue
        yield path


def documented_distributions() -> set[str]:
    """The uncommented `pip install` line in dev_server.py's prerequisites block.

    Deliberately the same source `test_dev_server_imports` reads, so there is one canonical list
    and this test cannot drift away from the one the other two enforce.
    """
    for line in _DEV_SERVER.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("pip install"):
            names = stripped[len("pip install"):].split()
            cleaned = set()
            for raw in names:
                # Strip the quoting, the extras and the version specifier: "'mcp<2'" is the
                # distribution mcp, and an earlier version of this parser handled only the
                # extras, so a pinned entry read as a distribution literally named "mcp<2".
                name = raw.strip("'\"")
                name = re.sub(r"\[.*?\]", "", name)
                name = re.split(r"[<>=!~]", name, maxsplit=1)[0]
                if name:
                    cleaned.add(name.strip().lower())
            return cleaned
    raise AssertionError("no uncommented 'pip install' line in dev_server.py's prerequisites")


def documented_modules() -> set[str]:
    return {_DIST_TO_MODULE.get(d, d.replace("-", "_")) for d in documented_distributions()}


def third_party_modules() -> set[str]:
    """Every top-level non-stdlib module imported by the shipped package (tests excluded)."""
    mods: set[str] = set()
    for path in _project_python_files(_AGENT_DIR):
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


_PROBE = r'''
import importlib, sys
from importlib.abc import MetaPathFinder

blocked = set(sys.argv[1].split(",")) - {""}


# `find_spec`, not `find_module`. The first version of this implemented the legacy
# `find_module`/`load_module` finder API, removed in Python 3.12 and never consulted on 3.12+.
# On such an interpreter the blocker blocked nothing, every import succeeded, and the
# sufficiency test passed while verifying absolutely nothing -- it would have passed with the
# dependency list emptied. Found on a 3.14 host; CI and the dev container run 3.11, where the
# legacy API still works, which is exactly why it was invisible to both.
class Blocker(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ImportError("No module named %r" % fullname.split(".")[0])
        return None


sys.meta_path.insert(0, Blocker())

# Prove the blocker is live before trusting anything it does not stop. A finder that is never
# consulted is indistinguishable from one that approves of everything, which is the exact
# failure this probe is recovering from.
if blocked:
    _canary = sorted(blocked)[0]
    try:
        importlib.import_module(_canary)
    except ImportError:
        pass
    else:
        print("BLOCKER-INERT: importing %r succeeded although it was blocked" % _canary)
        raise SystemExit(0)

failures = []
for mod in sys.argv[2:]:
    try:
        importlib.import_module(mod)
    except ImportError as e:
        failures.append("%s: %s" % (mod, e))
    except Exception as e:
        failures.append("%s: %s: %s" % (mod, type(e).__name__, e))
print("\n".join(failures))
'''


def shipped_modules() -> list[str]:
    """Every importable module in the package, tests and __init__ aside."""
    mods = []
    for path in _project_python_files(_AGENT_DIR):
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
    out = proc.stdout.strip()
    if out.startswith("BLOCKER-INERT:"):
        raise AssertionError(
            f"{out}\nThe import blocker is not being consulted on this interpreter, so every "
            "assertion built on it is vacuous. Fix the finder before reading any result from it."
        )
    return out


class DocumentedDependenciesSuffice(unittest.TestCase):
    def test_the_documented_list_is_enough_to_import_every_shipped_module(self):
        undocumented = third_party_modules() - documented_modules()
        failures = _import_with_only(undocumented)
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


if __name__ == "__main__":
    unittest.main()
