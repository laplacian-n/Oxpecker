"""Every package the shipped runtime needs at import time must be in its install instructions.

The gap this closes: `agent/evidence/store.py` has imported `cryptography.fernet` at module
scope since the app/ tree was added, `agent/audit_log.py` imports `evidence.store` at module
scope, and `dev_server.py` imports `audit_log` at module scope — so dev_server has required
`cryptography` for as long as it has existed, and no install instruction anywhere named it. A
venv built exactly per the documented pip line dies before binding a port:

    File ".../agent/web/dev_server.py", line 69, in <module>
        from .. import audit_log as _audit_log
    File ".../agent/audit_log.py", line 25, in <module>
        from .evidence.store import _load_or_create_keys, compute_digest
    File ".../agent/evidence/store.py", line 45, in <module>
        from cryptography.fernet import Fernet, InvalidToken
    ModuleNotFoundError: No module named 'cryptography'

It survived because every environment that ever ran this code had `cryptography` already — as a
transitive dependency of something else, or preinstalled in the image. The documented install
line was never the thing anyone actually tested against.

HOW THIS CHECKS IT, and why not the two more obvious ways:

  - Not by importing dev_server and diffing `sys.modules`. That measures what the whole library
    graph dragged in, so it flags packages we never asked for: an earlier version of this test
    failed here on `sniffio`, which `anyio` imports at runtime but does not declare in its
    installed metadata (it arrives via `trio`, which was not installed). The message told you to
    add `sniffio` to our install docs, which is wrong — that is anyio's business. Worse, the
    verdict changed with which optional extras happened to be present, so it passed on one host
    and failed on another. A test whose answer depends on the machine cannot be trusted on the
    machine you care about.
  - Not by walking distribution metadata for a transitive closure, for the same reason: the walk
    can only follow packages that are installed, so a missing intermediate silently truncates it.

Instead: start at dev_server.py, follow this project's OWN relative imports to every file it
pulls in, and collect the third-party modules THOSE files import at module scope. That set is
exactly "what we asked for, in the code that has to import for the server to start" — it is
deterministic, independent of what else is installed, and independent of test ordering (no
`sys.modules` delta, which changes depending on which test module ran first).
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import re
import sys
import unittest
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]      # .../app
PKG_ROOT = APP_ROOT / "agent"
ENTRY = PKG_ROOT / "web" / "dev_server.py"

#: Import name -> the pip token that provides it, where the two differ.
MODULE_TO_PIP = {
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "fpdf": "fpdf2",
    "cv2": "opencv-python",
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _module_path(dotted: str) -> Path | None:
    """`agent.web.dev_server` -> its file, if it is one of ours."""
    rel = Path(*dotted.split("."))
    for candidate in (APP_ROOT / rel.with_suffix(".py"), APP_ROOT / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _relative_targets(dotted_pkg: str, node: ast.ImportFrom) -> list[str]:
    """Every module `from ..x import a, b` could mean, inside package `dotted_pkg`.

    Both halves matter, and missing the second is what made the first draft of this test walk
    nothing: in `from .. import audit_log`, the module being imported is in `node.names`, not
    `node.module`, so resolving only `node.module` lands on `agent/__init__.py` and the walk
    stops before it ever reaches `evidence/store.py` — the file that imports `cryptography`.
    Each name is also tried as a submodule; the ones that are plain attributes simply do not
    resolve to a file and are ignored.
    """
    parts = dotted_pkg.split(".")
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    base = ".".join(parts)
    base = f"{base}.{node.module}" if node.module else base
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _walk_our_imports(entry: Path) -> tuple[set[str], dict[str, str], set[Path]]:
    """(third-party top-level modules at module scope, module -> first file seen in, files walked).

    Only module scope: an import inside a function cannot stop the server from starting, which
    is the failure this exists to prevent.
    """
    stdlib = set(sys.stdlib_module_names)
    third_party: set[str] = set()
    seen_in: dict[str, str] = {}
    visited: set[Path] = set()
    queue = [(entry, "agent.web.dev_server")]

    while queue:
        path, dotted = queue.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a syntax error fails louder elsewhere
            continue
        pkg = dotted.rsplit(".", 1)[0] if path.name != "__init__.py" else dotted

        for node in tree.body:  # module scope only, deliberately not ast.walk
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                targets = (_relative_targets(pkg, node) if node.level
                           else [node.module or ""])
            for target in targets:
                if not target:
                    continue
                own = _module_path(target)
                if own is not None:
                    queue.append((own, target))
                    continue
                top = target.split(".")[0]
                # `agent` itself is never third-party. It reaches here when a relative import
                # resolves to the package root (`from .. import x` yields both `agent` and
                # `agent.x`, and only the second is a file).
                if top in stdlib or top.startswith("_") or top == PKG_ROOT.name:
                    continue
                third_party.add(top)
                seen_in.setdefault(top, str(path.relative_to(APP_ROOT)))
    return third_party, seen_in, visited


def _documented_tokens() -> set[str]:
    text = ENTRY.read_text(encoding="utf-8")
    block = re.search(r"Prerequisites:\n(.*?)\n\nUsage:", text, re.DOTALL)
    assert block, "dev_server.py lost its Prerequisites docstring block"
    lines = re.findall(r"pip install ([^\n#]+)", block.group(1))
    assert lines, "dev_server.py's Prerequisites block lost every `pip install` line"
    tokens: set[str] = set()
    for line in lines:
        tokens |= {_normalize(a or b) for a, b in re.findall(r"'([^']+)'|(\S+)", line)}
    # `uvicorn[standard]` provides `uvicorn`.
    tokens |= {_normalize(re.sub(r"\[.*\]$", "", t)) for t in tokens}
    return tokens


class TestDevServerImportsAreDocumented(unittest.TestCase):
    def test_every_module_scope_dependency_is_in_the_install_line(self):
        third_party, seen_in, _ = _walk_our_imports(ENTRY)
        documented = _documented_tokens()
        module_to_dists = md.packages_distributions()

        missing = []
        for mod in sorted(third_party):
            candidates = {_normalize(mod), _normalize(MODULE_TO_PIP.get(mod, mod))}
            candidates |= {_normalize(d) for d in module_to_dists.get(mod, [])}
            if not (candidates & documented):
                missing.append(f"{mod} (imported by {seen_in[mod]})")

        self.assertEqual(
            missing, [],
            "these packages are imported at module scope by code dev_server pulls in, so the "
            "server cannot start without them, but dev_server.py's Prerequisites `pip install` "
            f"line does not name them: {missing}. Add them there AND to the install block in "
            "docs/DEPLOY_UBUNTU.md. This is exactly how `cryptography` went missing.",
        )

    def test_the_walk_actually_traverses(self):
        """Anti-vacuity. A walk that silently returned nothing would make the check above pass
        for the wrong reason — the same failure mode that let `parse_lolbas` return zero chunks
        against the real repo while three of its four tests went on passing on an empty list.

        The file count is the stronger signal: the first draft of this test resolved
        `from .. import audit_log` to the package root instead of the module, so it walked three
        files, found two third-party imports, and would have reported a clean bill of health
        while never reaching the file that imports `cryptography`."""
        third_party, _, visited = _walk_our_imports(ENTRY)
        self.assertGreater(
            len(visited), 15,
            f"the walk only reached {len(visited)} of this project's files — it is not "
            "following relative imports, so its verdict means nothing",
        )
        # The two that bracket the problem: one obvious, one that needs the walk to work.
        self.assertIn("fastapi", third_party)
        self.assertIn("cryptography", third_party, sorted(third_party))


if __name__ == "__main__":
    unittest.main()
