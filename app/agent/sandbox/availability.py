"""Can this host actually run a given isolation tier?

Split out of `executor.py` for one concrete reason: `executor.py` imports `resource`, which is
Unix-only, so it cannot be imported at all on Windows — and the desktop app ships as a Windows
installer. A caller that needs to ask "is isolation available here?" must be able to ask without
importing the module that only loads on the platforms where the answer is already yes. This
module therefore sticks to `os`, `platform`, `shutil` and `subprocess`, and is safe to import
anywhere.

It also owns the tier-name constants, so `executor.py` and any Windows-side caller agree on the
spelling without one importing the other.

The governing rule is `MicroVMExecutor`'s, stated when that tier was left unimplemented: never
silently degrade to a weaker tier while claiming the stronger one's name. `resolve_tier()`
enforces that — it raises rather than quietly handing back `direct`, and a caller that genuinely
wants the weaker tier has to say so with `allow_direct_fallback=True`, which is the operator's
decision to record, not a default to inherit.
"""
from __future__ import annotations

import platform
import shutil
import subprocess

TIER_DIRECT = "direct"
TIER_BUBBLEWRAP = "bubblewrap"
TIER_MICROVM = "microvm"

TIERS = (TIER_DIRECT, TIER_BUBBLEWRAP, TIER_MICROVM)

# How much isolation each tier actually provides, for honest reporting. `direct` is in the list
# because it is a legitimate, deliberately-chosen tier (Phase 1 behaviour: scrubbed env, no
# shell, blocklist only) — not because it is a form of isolation.
TIER_DESCRIPTION = {
    TIER_DIRECT: "no kernel isolation — scrubbed env and a command blocklist only",
    TIER_BUBBLEWRAP: "kernel-enforced namespace isolation (fs/net/pid) + seccomp + rlimits",
    TIER_MICROVM: "hardware-virtualised guest (not implemented)",
}

_USERNS_MAX_PATH = "/proc/sys/user/max_user_namespaces"


class IsolationUnavailableError(RuntimeError):
    """The requested isolation tier cannot run on this host, and no weaker tier was authorized."""


def _probe_direct() -> tuple[bool, str]:
    return True, "always available (provides no kernel isolation)"


def _probe_bubblewrap(deep: bool = False) -> tuple[bool, str]:
    """Three things have to hold, and they fail independently in the wild.

    1. Linux. Namespaces are a Linux kernel feature; there is no bubblewrap on Windows or macOS.
    2. The `bwrap` binary on PATH. `BubblewrapExecutor.build_argv()` starts its argv with a bare
       `"bwrap"`, so a missing binary surfaces as a `FileNotFoundError` from `subprocess` at the
       moment a command runs — i.e. after the agent has already decided to run it, and reported
       as a tool error rather than as "this host has no sandbox".
    3. Unprivileged user namespaces permitted. Several distributions ship them restricted
       (`user.max_user_namespaces=0`, or a hardened kernel), which leaves `bwrap` installed but
       unable to unshare. Checking the sysctl catches the common case cheaply.

    `deep=True` additionally execs a trivial sandboxed `/bin/true`, which is the only definitive
    answer — a static probe can still be wrong about a seccomp or LSM policy that only bites at
    exec time. It costs one short-lived process, so it suits a startup check rather than a
    per-command one.
    """
    if platform.system() != "Linux":
        return False, f"bubblewrap requires Linux; this host is {platform.system() or 'unknown'}"

    if shutil.which("bwrap") is None:
        return False, "the 'bwrap' binary is not on PATH (install bubblewrap)"

    try:
        with open(_USERNS_MAX_PATH) as f:
            if int(f.read().strip()) <= 0:
                return False, (
                    f"unprivileged user namespaces are disabled ({_USERNS_MAX_PATH} is 0); "
                    "bwrap is installed but cannot unshare"
                )
    except FileNotFoundError:
        pass  # Kernel without the knob — not evidence of restriction either way.
    except (OSError, ValueError):
        pass  # Unreadable or unparseable; let the deep probe or the real run decide.

    if deep:
        try:
            proc = subprocess.run(
                ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--", "/bin/true"],
                capture_output=True, text=True, timeout=10,
            )
        except FileNotFoundError:
            return False, "the 'bwrap' binary disappeared between the PATH check and exec"
        except subprocess.TimeoutExpired:
            return False, "a trivial bwrap invocation did not complete within 10s"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            return False, (
                "a trivial bwrap invocation failed: "
                + (detail[-1] if detail else f"exit {proc.returncode}")
            )
        return True, "verified by executing a sandboxed /bin/true"

    return True, "bwrap is present and user namespaces are permitted (not exec-verified)"


def _probe_microvm(deep: bool = False) -> tuple[bool, str]:
    return False, "microVM isolation is not implemented"


def probe(tier: str, deep: bool = False) -> tuple[bool, str]:
    """(available, reason). `reason` is populated whether or not the tier is available, so it can
    be shown to an operator and recorded in an audit entry in both cases."""
    if tier == TIER_DIRECT:
        return _probe_direct()
    if tier == TIER_BUBBLEWRAP:
        return _probe_bubblewrap(deep=deep)
    if tier == TIER_MICROVM:
        return _probe_microvm(deep=deep)
    raise ValueError(f"unknown isolation tier: {tier!r}, choose from {list(TIERS)}")


def resolve_tier(
    requested: str, *, allow_direct_fallback: bool = False, deep: bool = False
) -> tuple[str, str]:
    """Decide what will actually run, and say why. The single place that answer is produced, so
    no caller has to re-derive it and no two callers can disagree.

    Returns `(effective_tier, reason)`. The caller must report and record `effective_tier`, never
    the requested one — reporting `bubblewrap` for an execution that ran unsandboxed is the
    specific failure this function exists to prevent.

    Raises `IsolationUnavailableError` when the requested tier cannot run and
    `allow_direct_fallback` is False. Failing closed is the point: a sandbox that silently
    becomes no sandbox is worse than no sandbox, because the operator stops watching.
    """
    if requested not in TIERS:
        raise ValueError(f"unknown isolation tier: {requested!r}, choose from {list(TIERS)}")

    ok, reason = probe(requested, deep=deep)
    if ok:
        return requested, reason

    if not allow_direct_fallback:
        raise IsolationUnavailableError(
            f"isolation tier {requested!r} is unavailable: {reason}. Refusing to execute: "
            f"falling back to {TIER_DIRECT!r} would run the command with no kernel isolation "
            f"while the session still advertised {requested!r}. To accept that risk, the "
            f"operator must select the {TIER_DIRECT!r} tier explicitly."
        )

    direct_ok, direct_reason = probe(TIER_DIRECT)
    if not direct_ok:  # pragma: no cover — _probe_direct never fails
        raise IsolationUnavailableError(f"no tier is available: {reason}; {direct_reason}")
    return TIER_DIRECT, (
        f"fell back to {TIER_DIRECT!r} with explicit operator authorization, because "
        f"{requested!r} is unavailable: {reason}"
    )


def describe_host(deep: bool = False) -> dict:
    """A snapshot for the UI, the API and the audit trail: what this host can actually do. Keeps
    'what we claim' and 'what is true' in one place so a UI cannot drift from reality."""
    tiers = {}
    for tier in TIERS:
        ok, reason = probe(tier, deep=deep)
        tiers[tier] = {
            "available": ok,
            "reason": reason,
            "provides": TIER_DESCRIPTION[tier],
        }
    strongest = next(
        (t for t in (TIER_MICROVM, TIER_BUBBLEWRAP) if tiers[t]["available"]), TIER_DIRECT
    )
    return {
        "platform": platform.system(),
        "tiers": tiers,
        "strongest_available": strongest,
        "kernel_isolation_available": strongest != TIER_DIRECT,
    }
