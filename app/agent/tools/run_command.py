"""argv-execution command tool — no shell, scrubbed env, output/time caps, confirmation gate.

Isolation tier is selectable (agent/sandbox/executor.py): 'direct' is the original Phase-1
behavior — blocking a fixed list of network binaries stops the obvious cases but is not
OS-level network isolation, a command like `python3` can still open a socket in-process.
'bubblewrap' (Phase 4) closes that gap for real: kernel-enforced namespace isolation means
there is no network device to open a socket on at all, and no filesystem path outside the
sandbox's binds exists to escape to — verified with actual escape/egress/resource-limit tests,
not assumed.

ADR-0004: default flipped to 'bubblewrap' — both independent reviews argued 'direct' shouldn't
be the default, and by the time this was decided the bubblewrap tier had 16/16 escape/egress/
resource tests passing (agent/sandbox/test_isolation.py), not a theoretical option. 'direct'
remains available as an explicit, confirmed choice (main.py mirrors the existing
--dangerous-local confirmation UX for it) — this is a default flip, not a removal.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .. import config
from ..sandbox.executor import TIER_WSL2, get_executor
from .workspace import WorkspaceEscapeError, contains_credential_path, resolve_within_workspace

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "Run a command inside the agent's workspace. No shell is used — argv is executed "
            "directly, so shell metacharacters (pipes, redirects, semicolons, globs) are NOT "
            "interpreted and will be passed to the program as literal characters. Network and "
            "privilege-escalation binaries are blocked by default. Commands outside a small "
            "safe allowlist require human confirmation before they run. The isolation tier "
            "(direct execution vs. a network- and filesystem-isolated sandbox) is fixed for "
            "the whole session by the operator, not chosen per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Program and arguments as separate array elements.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to workspace root. Optional.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": f"Max {config.TOOL_CALL_TIMEOUT_S}s, enforced regardless.",
                },
            },
            "required": ["argv"],
        },
    },
}


class BlockedCommandError(RuntimeError):
    pass


def _build_env(workspace_root: Path) -> dict:
    # Deliberately not os.environ.copy() — no inherited secret-bearing env vars
    # (Phase-1 safety boundary).
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace_root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
    }


def preflight(argv: list[str], dangerous_local: bool) -> tuple[bool, str | None]:
    """Returns (blocked, reason). Blocked=True means: refuse outright, no confirmation offered."""
    if not argv:
        return True, "empty argv"

    binary = Path(argv[0]).name
    if binary in config.BLOCKED_BINARIES and not dangerous_local:
        return True, f"binary {binary!r} is in the default block list (network/privesc)"

    for arg in argv:
        if marker := contains_credential_path(arg, config.CREDENTIAL_PATH_MARKERS):
            return True, f"argument references a credential-directory marker ({marker!r})"

    return False, None


def needs_confirmation(argv: list[str]) -> bool:
    binary = Path(argv[0]).name
    return binary not in config.COMMAND_ALLOWLIST


def run(
    argv: list[str],
    workspace_root: Path,
    cwd: str | None = None,
    timeout_s: int | None = None,
    dangerous_local: bool = False,
    isolation_tier: str = "bubblewrap",
) -> dict:
    blocked, reason = preflight(argv, dangerous_local)
    if blocked:
        return {"ok": False, "blocked": True, "error": reason}

    binary = Path(argv[0]).name
    # The wsl2 tier runs the binary inside the WSL2 guest, so it is not on this host's
    # filesystem at all — and this host is Windows, where a which() against a Unix PATH can
    # only ever return None. The guest reports a missing binary itself, as exit 127 with its
    # own message, which is the accurate source for a question about the guest.
    if isolation_tier != TIER_WSL2 and shutil.which(binary, path="/usr/bin:/bin") is None:
        return {"ok": False, "blocked": False, "error": f"binary not found on PATH: {binary}"}

    if cwd:
        try:
            resolved_cwd = resolve_within_workspace(workspace_root, cwd)
        except WorkspaceEscapeError as e:
            return {"ok": False, "blocked": True, "error": str(e)}
    else:
        resolved_cwd = workspace_root
    timeout = min(timeout_s or config.TOOL_CALL_TIMEOUT_S, config.TOOL_CALL_TIMEOUT_S)

    executor = get_executor(isolation_tier)
    result = executor.run(argv, resolved_cwd, workspace_root, timeout)

    def cap(s: str) -> tuple[str, bool]:
        if len(s) > config.RUN_COMMAND_MAX_OUTPUT_BYTES:
            return s[: config.RUN_COMMAND_MAX_OUTPUT_BYTES] + "\n[...output capped...]", True
        return s, False

    stdout, stdout_capped = cap(result.stdout)
    stderr, stderr_capped = cap(result.stderr)

    return {
        "ok": not result.timed_out and result.killed_reason is None,
        "blocked": False,
        "timed_out": result.timed_out,
        "killed_reason": result.killed_reason,
        "exit_code": result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "output_capped": stdout_capped or stderr_capped,
        "duration_ms": round(result.duration_ms, 1),
        "isolation_tier": result.isolation_tier,
        "sandbox_profile_digest": result.sandbox_profile_digest,
    }
