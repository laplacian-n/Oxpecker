from __future__ import annotations

from pathlib import Path

from .. import config
from .workspace import WorkspaceEscapeError, contains_credential_path, resolve_within_workspace

SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write or append text to a file. Path is relative to the agent's workspace root; "
            "paths outside the workspace are rejected. Parent directories are created as "
            "needed, still within the workspace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace root."},
                "content": {"type": "string", "description": "Text content to write."},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "Default overwrite.",
                },
            },
            "required": ["path", "content"],
        },
    },
}


def run(path: str, content: str, workspace_root: Path, mode: str = "overwrite") -> dict:
    if marker := contains_credential_path(path, config.CREDENTIAL_PATH_MARKERS):
        return {
            "ok": False,
            "error": f"blocked: path references a credential-directory marker ({marker!r})",
        }
    if mode not in ("overwrite", "append"):
        return {"ok": False, "error": f"invalid mode: {mode!r}"}
    if len(content.encode("utf-8")) > config.WRITE_FILE_MAX_BYTES:
        return {
            "ok": False,
            "error": f"content exceeds WRITE_FILE_MAX_BYTES={config.WRITE_FILE_MAX_BYTES}",
        }

    try:
        resolved = resolve_within_workspace(workspace_root, path)
    except WorkspaceEscapeError as e:
        return {"ok": False, "error": str(e)}
    resolved.parent.mkdir(parents=True, exist_ok=True)
    file_mode = "a" if mode == "append" else "w"
    with resolved.open(file_mode, encoding="utf-8") as f:
        f.write(content)
    return {"ok": True, "bytes_written": len(content.encode("utf-8")), "path": path}
