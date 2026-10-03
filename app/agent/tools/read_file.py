from __future__ import annotations

from pathlib import Path

from .. import config
from .workspace import WorkspaceEscapeError, contains_credential_path, resolve_within_workspace

SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a text file's contents. Path is relative to the agent's workspace root; "
            "paths outside the workspace are rejected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace root."}
            },
            "required": ["path"],
        },
    },
}


def run(path: str, workspace_root: Path) -> dict:
    if marker := contains_credential_path(path, config.CREDENTIAL_PATH_MARKERS):
        return {
            "ok": False,
            "error": f"blocked: path references a credential-directory marker ({marker!r})",
        }

    try:
        resolved = resolve_within_workspace(workspace_root, path)
    except WorkspaceEscapeError as e:
        return {"ok": False, "error": str(e)}
    if not resolved.exists():
        return {"ok": False, "error": f"no such file: {path}"}
    if not resolved.is_file():
        return {"ok": False, "error": f"not a regular file: {path}"}

    data = resolved.read_bytes()
    truncated = len(data) > config.READ_FILE_MAX_BYTES
    if truncated:
        data = data[: config.READ_FILE_MAX_BYTES]
    text = data.decode("utf-8", errors="replace")
    return {"ok": True, "content": text, "truncated": truncated, "bytes_read": len(data)}
