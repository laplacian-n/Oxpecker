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
TIER_WSL2 = "wsl2"
TIER_MICROVM = "microvm"

TIERS = (TIER_DIRECT, TIER_BUBBLEWRAP, TIER_WSL2, TIER_MICROVM)

# How much isolation each tier actually provides, for honest reporting. `direct` is in the list
# because it is a legitimate, deliberately-chosen tier (Phase 1 behaviour: scrubbed env, no
# shell, blocklist only) — not because it is a form of isolation.
TIER_DESCRIPTION = {
    TIER_DIRECT: "no kernel isolation — scrubbed env and a command blocklist only",
    TIER_BUBBLEWRAP: "kernel-enforced namespace isolation (fs/net/pid) + seccomp + rlimits",
    TIER_WSL2: (
        "bubblewrap inside the WSL2 guest VM — namespace isolation (fs/net/pid) + rlimits "
        "behind a hypervisor boundary, but NO seccomp (the compiled BPF program is passed to "
        "bwrap as a file descriptor, and an fd does not cross the wsl.exe process boundary)"
    ),
    TIER_MICROVM: "hardware-virtualised guest (not implemented)",
}

# Ranked weakest to strongest, for `describe_host`. wsl2 sits above bubblewrap because it is
# bubblewrap *plus* a hypervisor boundary; it loses seccomp, which is a narrowing of the syscall
# surface inside an already-unshared namespace rather than a containment boundary of its own.
_TIER_STRENGTH = (TIER_DIRECT, TIER_BUBBLEWRAP, TIER_WSL2, TIER_MICROVM)

_USERNS_MAX_PATH = "/proc/sys/user/max_user_namespaces"

# Resource caps for the wsl2 tier, applied by the guest shell rather than by a preexec_fn: an
# fd-passing preexec_fn cannot reach across the wsl.exe process boundary, and `ulimit` is the
# shell's interface to the same setrlimit(2) calls. Declared here, not in executor.py, because
# `_probe_wsl2` has to verify the guest's /bin/sh accepts the whole prologue — a limit silently
# skipped is a limit not applied, and the executor's own digest would then claim caps that were
# not in force. executor.py imports this so the probe and the real run use one string.
#
# Units differ per flag and are not interchangeable: -v is KiB, -f is 512-byte blocks, -t is
# seconds, -n is a count. Kept in sync with the BWRAP_* constants in executor.py, which are the
# Linux tier's and are expressed in bytes.
WSL_ULIMIT_PROLOGUE = (
    "ulimit -v 524288 && "     # 512 MiB address space
    "ulimit -t 10 && "          # 10 CPU-seconds
    "ulimit -n 64 && "          # 64 file descriptors
    "ulimit -f 102400 && "      # 50 MiB max single-file write
    "exec "
)


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


# How long to wait for the WSL2 guest. A cold VM start is seconds, not milliseconds, and the
# probe pays that cost once — it is not on the per-command path.
_WSL_PROBE_TIMEOUT_S = 60


def _wsl(args: list[str], timeout: float = _WSL_PROBE_TIMEOUT_S) -> tuple[int, str]:
    """Run something in the WSL2 guest and return (exit code, combined output).

    Decoded with errors="replace" rather than text=True: some wsl.exe subcommands emit UTF-16,
    and a probe that raises UnicodeDecodeError would report "unavailable" for the wrong reason.
    """
    try:
        proc = subprocess.run(
            ["wsl.exe", *args], capture_output=True, timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "wsl.exe not found"
    except subprocess.TimeoutExpired:
        return 124, f"wsl.exe did not respond within {timeout:.0f}s"
    except OSError as e:  # pragma: no cover — platform-specific spawn failures
        return 126, f"could not start wsl.exe: {e}"
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


def _probe_wsl2(deep: bool = False) -> tuple[bool, str]:
    """Always exec-verifies, regardless of `deep`.

    Every other probe here can answer statically because the thing it checks is a property of
    the host that a file or a PATH lookup settles. This one cannot: whether `bwrap` can unshare
    *inside* the WSL2 guest depends on that guest distribution's packages and on the WSL kernel's
    user-namespace configuration, neither of which is visible from the Windows side. A static
    "wsl.exe is on PATH, so this tier is available" would be precisely the false claim
    `resolve_tier` exists to prevent — the operator would be told the sandbox is active and get
    a failure at the first command instead. So the cost of one `wsl.exe` round trip is paid to
    make the answer true.

    This tier has not been exercised on a real Windows host by its author; see
    docs/OBSERVABILITY_PLAN.md. That is also why the verification is a real execution rather
    than a set of assumptions about wsl.exe's behaviour: on a host where any assumption here is
    wrong, the probe fails and the tier reports unavailable, which is the safe direction.
    """
    if platform.system() != "Windows":
        return False, (
            f"the wsl2 tier is for Windows hosts; this host is {platform.system() or 'unknown'} "
            f"— use the {TIER_BUBBLEWRAP!r} tier directly"
        )

    if shutil.which("wsl.exe") is None:
        return False, "the 'wsl.exe' launcher is not on PATH (install WSL2 and a distribution)"

    code, out = _wsl(["-e", "/bin/true"])
    if code != 0:
        return False, (
            f"the WSL2 guest did not run a trivial command (exit {code}): {out[:200]!r}. "
            "Check that a distribution is installed and starts (`wsl -l -v`)."
        )

    code, out = _wsl(["-e", "bwrap", "--version"])
    if code != 0:
        return False, (
            "the WSL2 guest runs, but 'bwrap' is not usable inside it "
            f"(exit {code}): {out[:200]!r}. Install bubblewrap in the distribution "
            "(e.g. `sudo apt install bubblewrap`)."
        )

    # The definitive check, and the one that cannot be predicted from the Windows side: several
    # WSL kernel builds permit bwrap to run but not to unshare.
    # The definitive check runs the real shape of a real invocation: the guest's /bin/sh, the
    # whole ulimit prologue, and bwrap unsharing. Anything that only tests one of those can
    # report the tier available while the other two fail at the first command.
    code, out = _wsl([
        "-e", "/bin/sh", "-c",
        WSL_ULIMIT_PROLOGUE
        + "bwrap --unshare-all --ro-bind / / -- /bin/true",
    ])
    if code != 0:
        return False, (
            "the WSL2 guest has bubblewrap but could not run a sandboxed /bin/true under the "
            f"resource limits this tier applies (exit {code}): {out[:200]!r}. Either the guest "
            "kernel restricts unprivileged user namespaces, or its /bin/sh does not accept the "
            "ulimit prologue — in both cases the tier cannot honour what it would claim."
        )
    return True, (
        "verified by executing a sandboxed /bin/true inside the WSL2 guest, under this tier's "
        "resource limits"
    )


def _probe_microvm(deep: bool = False) -> tuple[bool, str]:
    return False, "microVM isolation is not implemented"


def probe(tier: str, deep: bool = False) -> tuple[bool, str]:
    """(available, reason). `reason` is populated whether or not the tier is available, so it can
    be shown to an operator and recorded in an audit entry in both cases."""
    if tier == TIER_DIRECT:
        return _probe_direct()
    if tier == TIER_BUBBLEWRAP:
        return _probe_bubblewrap(deep=deep)
    if tier == TIER_WSL2:
        return _probe_wsl2(deep=deep)
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
        (t for t in reversed(_TIER_STRENGTH) if tiers[t]["available"]), TIER_DIRECT
    )
    return {
        "platform": platform.system(),
        "tiers": tiers,
        "strongest_available": strongest,
        "kernel_isolation_available": strongest != TIER_DIRECT,
    }
