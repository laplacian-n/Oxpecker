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


# Programs whose job is to run another program. Screening only argv[0] made every one of these
# a laundering path: `["env", "curl", ...]` passed both the block list and the confirmation gate
# while `["curl", ...]` was refused outright, and in the `direct` tier the block list is the
# ONLY egress control there is. Verified before the fix — `env curl` completed a real HTTP
# request with no prompt.
_COMMAND_WRAPPERS = frozenset({
    "env", "nice", "ionice", "nohup", "setsid", "stdbuf", "unbuffer", "timeout", "time",
    "xargs", "watch", "flock", "taskset", "chrt", "script", "busybox", "command", "exec",
    "strace", "ltrace", "ltrace64", "catchsegv", "proxychains", "proxychains4",
})

# `env NAME=VALUE prog` reintroduces environment this tool deliberately strips: _build_env is
# "no inherited secret-bearing env vars (Phase-1 safety boundary)", and an assignment is how
# that boundary is undone from inside an otherwise-innocuous argv.
_ENV_ASSIGNMENT = __import__("re").compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def effective_binaries(argv: list[str]) -> list[str]:
    """Every program this argv would run, in order, seen through the wrappers.

    `["env", "-i", "FOO=1", "curl", "-s", "http://x"]` -> `["env", "curl"]`. Screening checks
    all of them, so a wrapper cannot carry a blocked program past a check that only looked at
    the first token.
    """
    names: list[str] = []
    i = 0
    while i < len(argv):
        name = Path(argv[i]).name
        names.append(name)
        if name not in _COMMAND_WRAPPERS:
            break
        i += 1
        # Step over the wrapper's own flags and (for env) its assignments to reach the program.
        while i < len(argv) and (argv[i].startswith("-") or _ENV_ASSIGNMENT.match(argv[i])):
            i += 1
    return names


def has_wrapper(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name in _COMMAND_WRAPPERS


def preflight(argv: list[str], dangerous_local: bool) -> tuple[bool, str | None]:
    """Returns (blocked, reason). Blocked=True means: refuse outright, no confirmation offered."""
    if not argv:
        return True, "empty argv"

    chain = effective_binaries(argv)
    # When a wrapper is involved, screen EVERY remaining token rather than trying to parse each
    # wrapper's grammar. Walking the chain positionally is not enough: `timeout 5 nc -l 1` puts
    # a positional duration before the program, so the walk stopped at "5" and `nc` went
    # unscreened — a hole in the first version of this fix, caught by its own test table.
    # Over-broad in one direction (a FILENAME argument that happens to be called `curl` blocks
    # the run) and that is the right direction for a hard block; the message says which token.
    screened = chain if not has_wrapper(argv) else chain + [Path(a).name for a in argv[1:]]
    for binary in screened:
        if binary in config.BLOCKED_BINARIES and not dangerous_local:
            via = f" (reached via {chain[0]})" if has_wrapper(argv) else ""
            return True, (
                f"binary {binary!r} is in the default block list (network/privesc){via}"
            )

    if len(chain) > 1 and chain[0] == "env":
        for arg in argv[1:]:
            if _ENV_ASSIGNMENT.match(arg):
                return True, (
                    f"`env {arg.split('=')[0]}=...` would set an environment variable for the "
                    f"child, and this tool runs commands with a deliberately scrubbed "
                    f"environment. Pass the value as an argument instead."
                )

    for arg in argv:
        if marker := contains_credential_path(arg, config.CREDENTIAL_PATH_MARKERS):
            return True, f"argument references a credential-directory marker ({marker!r})"

    return False, None


def needs_confirmation(argv: list[str]) -> bool:
    """Whether a human must approve this argv.

    Two rules beyond "is the name on the allowlist":

    * EVERY program in the wrapper chain has to be allowlisted, not just the first.
    * An argv[0] carrying a path separator is never allowlisted. The gate matched on
      `Path(argv[0]).name`, so a file the agent had just written into its own workspace and
      named `ls` ran with no confirmation while the existence check "validated" /usr/bin/ls.
      The allowlist names coreutils programs found on the sandbox PATH; it does not name
      whatever happens to share a basename with one.
    """
    if not argv:
        return True
    if "/" in argv[0] or "\\" in argv[0]:
        return True
    if has_wrapper(argv):
        # A wrapper is never auto-approved, even when everything in the chain is allowlisted.
        # The agent has no need for `env ls` over `ls`, and the allowlist's job is to recognise
        # a short list of well-understood invocations — a wrapped one is not among them.
        return True
    return any(b not in config.COMMAND_ALLOWLIST for b in effective_binaries(argv))


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
        # Reported alongside the tier, not folded into the digest: the digest is a hash, so a
        # reader could not tell a seccomp-filtered run from an unfiltered one without
        # recomputing the profile. None means the tier has no syscall-filter dimension.
        "seccomp_active": result.seccomp_active,
    }
