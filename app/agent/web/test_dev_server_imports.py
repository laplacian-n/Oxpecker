"""Regression test for the missing `cryptography` dependency.

dev_server.py's own Prerequisites docstring, and docs/DEPLOY_UBUNTU.md's install block, listed
`pip install fastapi 'uvicorn[standard]' scikit-learn numpy [pyseccomp]` as the complete set of
packages needed before `python -m agent.web.dev_server` would run. It was not complete:
agent/evidence/store.py has imported `cryptography.fernet` at module scope since the app/ tree
was added (e995645), and agent/audit_log.py imports evidence.store at module scope, so
dev_server.py has required `cryptography` for just as long -- but nothing installed it and
nothing caught the gap. A venv built exactly per the documented pip install line fails before
binding a port:

    File ".../agent/web/dev_server.py", line 69, in <module>
        from .. import audit_log as _audit_log
    File ".../agent/audit_log.py", line 25, in <module>
        from .evidence.store import _load_or_create_keys, compute_digest
    File ".../agent/evidence/store.py", line 45, in <module>
        from cryptography.fernet import Fernet, InvalidToken
    ModuleNotFoundError: No module named 'cryptography'

This test imports dev_server for real, finds every third-party distribution that pulled in, and
checks each is reachable -- directly or as a dependency of something documented -- from
dev_server.py's own Prerequisites line. A newly introduced hard dependency nobody added there
fails loudly here instead of silently on the next person's freshly provisioned host.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEV_SERVER = HERE / "dev_server.py"

# Pip-install token -> the top-level importable module it actually provides, for the names
# where the two differ. Everything else is assumed to import under its own distribution name.
TOKEN_TO_MODULE = {
    "uvicorn[standard]": "uvicorn",
    "scikit-learn": "sklearn",
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _documented_pip_tokens() -> set[str]:
    text = DEV_SERVER.read_text(encoding="utf-8")
    block = re.search(r"Prerequisites:\n(.*?)\n\n", text, re.DOTALL)
    assert block, "dev_server.py lost its Prerequisites docstring block"
    lines = re.findall(r"pip install ([^\n]+)", block.group(1))
    assert lines, "dev_server.py's Prerequisites block lost every `pip install` line"
    tokens: set[str] = set()
    for line in lines:
        tokens |= {a or b for a, b in re.findall(r"'([^']+)'|(\S+)", line)}
    return tokens


def _transitive_closure(roots: set[str]) -> set[str]:
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        name = _normalize(name)
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = md.requires(name) or []
        except md.PackageNotFoundError:
            continue
        for req in reqs:
            dep = re.match(r"[A-Za-z0-9_.\-]+", req)
            if dep:
                queue.append(dep.group(0))
    return seen


class TestDevServerImportsAreAllDocumented(unittest.TestCase):
    def test_every_third_party_import_is_reachable_from_prerequisites(self):
        before = set(sys.modules)
        importlib.import_module("agent.web.dev_server")
        newly_imported = set(sys.modules) - before

        tokens = _documented_pip_tokens()
        roots = set(tokens)
        roots |= {TOKEN_TO_MODULE[t] for t in tokens if t in TOKEN_TO_MODULE}
        reachable = _transitive_closure(roots)

        stdlib = set(sys.stdlib_module_names)
        module_to_dists = md.packages_distributions()

        undocumented: set[str] = set()
        for mod_name in newly_imported:
            top = mod_name.split(".")[0]
            if top in stdlib or top == "agent" or top.startswith("_"):
                continue
            dists = module_to_dists.get(top)
            if not dists:
                continue  # no distribution metadata to check against -- not our call
            if not any(_normalize(d) in reachable for d in dists):
                undocumented.add(f"{top} (from {', '.join(dists)})")

        self.assertFalse(
            undocumented,
            "agent.web.dev_server imports packages not reachable -- directly or transitively "
            "-- from its own Prerequisites `pip install` line: "
            f"{sorted(undocumented)}. Add them to the Prerequisites docstring in dev_server.py "
            "and to docs/DEPLOY_UBUNTU.md -- this is exactly how `cryptography` went missing.",
        )


if __name__ == "__main__":
    unittest.main()
