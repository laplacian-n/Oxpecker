"""Shared workspace-containment logic used by all three Phase-1 tools.

All file/command operations are constrained to an explicit workspace root; path traversal and
symlink escapes are rejected (Phase-1 in-scope requirement from the research doc).
"""
from __future__ import annotations

from pathlib import Path


class WorkspaceEscapeError(RuntimeError):
    pass


def resolve_within_workspace(workspace_root: Path, relative_path: str) -> Path:
    """Resolve relative_path against workspace_root, rejecting any escape.

    Uses realpath resolution (following symlinks) so a symlink planted inside the workspace
    that points outside it is caught too, not just literal '..' traversal.
    """
    workspace_root = workspace_root.resolve()
    candidate = (workspace_root / relative_path).resolve()
    try:
        candidate.relative_to(workspace_root)
    except ValueError:
        raise WorkspaceEscapeError(
            f"path {relative_path!r} resolves to {candidate}, outside workspace {workspace_root}"
        ) from None
    return candidate


def contains_credential_path(text: str, markers: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for marker in markers:
        if marker.lower() in lowered:
            return marker
    return None
