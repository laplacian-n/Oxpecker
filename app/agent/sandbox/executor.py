"""Pluggable execution tiers — §3/§12 Phase 4 "lightweight and microVM isolation profiles."

`DirectExecutor` is exactly Phase 1's original run_command behavior (no isolation beyond the
binary blocklist/scrubbed-env — the original "local machine" mode). `BubblewrapExecutor` is
the lightweight tier: unprivileged Linux namespaces via `bwrap` giving *kernel-enforced*
filesystem and network isolation, not just a blocklist — genuinely stronger than anything
Phase 1-3 could offer, because `--unshare-net` removes every network device from the sandbox's
view, including loopback, rather than trying to enumerate forbidden binaries.
`MicroVMExecutor` is an explicit NotImplementedError stub: hardware-virtualized isolation
(Firecracker) needs a guest kernel/rootfs/vsock pipeline this session deliberately did not
build (see research doc's Phase 4 section — the choice was to make the lightweight tier
genuinely solid rather than half-build both), so it must never silently fall back to a weaker
tier and claim the stronger one's name.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config
from . import seccomp_profile
from .availability import (  # re-exported: availability.py owns the tier names, so a
    TIER_BUBBLEWRAP,         # Windows-side caller and this module cannot disagree on spelling
    TIER_DIRECT,
    TIER_MICROVM,
    TIER_WSL2,
    WSL_ULIMIT_FSIZE_MAX_BYTES,
    WSL_ULIMIT_PROLOGUE,
    IsolationUnavailableError,
    probe,
    resolve_tier,
)

# `resource` is Unix-only. It is used in exactly one place — `_set_rlimits()`, the preexec_fn for
# the bubblewrap tier — which cannot be reached on a platform that has no bubblewrap anyway. Left
# unguarded it made this whole module unimportable on Windows, which meant a Windows caller could
# not even ask "is isolation available here?" without an ImportError. Guarding it keeps the
# question answerable; `_set_rlimits()` refuses explicitly rather than failing at attribute
# access, so a path that somehow reaches it still fails loudly instead of running uncapped.
try:
    import resource
except ImportError:  # pragma: no cover — exercised only on non-Unix hosts
    resource = None

log = logging.getLogger("agent.sandbox.executor")


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: float
    isolation_tier: str
    sandbox_profile_digest: str | None = None
    killed_reason: str | None = None  # e.g. "resource_limit", "timeout"
    # Whether a seccomp syscall filter was actually loaded for THIS run. None for tiers where
    # the question does not apply (direct). Surfaced as a field because the digest is an opaque
    # hash: a reader could not tell a filtered run from an unfiltered one without recomputing
    # the profile, so "which protections were applied" was effectively unauditable.
    seccomp_active: bool | None = None


class _CappedCapture:
    """What `subprocess.run(capture_output=True)` should have been for an untrusted child.

    `capture_output` reads each pipe to EOF with no bound: a child writing 2.6 GB put 7.7 GiB
    into the agent's own RSS, inside the normal tool timeout. The sandbox's own limits do not
    help — `RLIMIT_AS` caps the child, and `RLIMIT_FSIZE` does not apply to pipes — so the
    process we are isolating could kill the process doing the isolating.

    Each stream is drained by its own thread, which keeps the first `limit` bytes and discards
    the rest. Draining rather than closing matters: if the parent stopped reading, the child
    would block on a full pipe and look like a hang instead of a noisy command.
    """

    def __init__(self, limit: int):
        self.limit = limit
        self.chunks: list[bytes] = []
        self.total = 0
        self._lock = threading.Lock()

    def drain(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                with self._lock:
                    if self.total < self.limit:
                        self.chunks.append(chunk[: self.limit - self.total])
                    self.total += len(chunk)
        except (OSError, ValueError):  # pragma: no cover — stream closed under us
            return
        finally:
            try:
                stream.close()
            except OSError:  # pragma: no cover
                pass

    def text(self) -> str:
        body = b"".join(self.chunks).decode("utf-8", errors="replace")
        if self.total > self.limit:
            body += (
                f"\n[...capture capped at {self.limit} bytes; the command produced "
                f"{self.total} bytes and the rest was read and discarded]"
            )
        return body


def run_capped(
    argv: list[str], *, timeout: float, limit: int | None = None, **popen_kwargs
) -> tuple[int | None, str, str, bool]:
    """(exit_code, stdout, stderr, timed_out) with the parent's memory bounded.

    Drop-in for `subprocess.run(..., capture_output=True, text=True, timeout=...)` in every
    executor here. On timeout the child is killed, the readers are joined, and whatever was
    captured before the kill is returned — the same contract `TimeoutExpired.stdout` offers,
    without the unbounded read.
    """
    limit = config.EXEC_CAPTURE_MAX_BYTES if limit is None else limit
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **popen_kwargs
    )
    out, err = _CappedCapture(limit), _CappedCapture(limit)
    readers = [
        threading.Thread(target=out.drain, args=(proc.stdout,), daemon=True),
        threading.Thread(target=err.drain, args=(proc.stderr,), daemon=True),
    ]
    for t in readers:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for t in readers:
        # The pipes are closed once the child is gone, so the readers end on their own. The
        # bound is a safety net against a grandchild still holding the write end.
        t.join(timeout=5.0)
    return (None if timed_out else proc.returncode), out.text(), err.text(), timed_out


def _build_env(workspace_root: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workspace_root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
    }


class DirectExecutor:
    """Phase 1's original tier: scrubbed env, no shell, no namespace isolation."""

    tier = TIER_DIRECT

    @classmethod
    def available(cls, deep: bool = False) -> tuple[bool, str]:
        """(can this tier run here, why). Delegates to availability.probe so there is one
        answer, not one per call site."""
        return probe(cls.tier, deep=deep)

    def run(
        self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float
    ) -> ExecResult:
        start = time.monotonic()
        code, stdout, stderr, timed_out = run_capped(
            argv, timeout=timeout, cwd=str(cwd), env=_build_env(workspace_root)
        )
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=(time.monotonic() - start) * 1000,
            isolation_tier=self.tier,
            killed_reason="timeout" if timed_out else None,
        )


# Resource caps applied inside the sandboxed process via RLIMIT — cheap, portable, no
# systemd/cgroup dependency. Tuned for a single short-lived recon/utility command, not a build.
BWRAP_MEM_LIMIT_BYTES = 512 * 1024 * 1024  # 512MB address space
BWRAP_CPU_LIMIT_S = 10  # hard CPU-seconds cap, independent of wall-clock timeout
BWRAP_NOFILE_LIMIT = 64
BWRAP_FSIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50MB max single-file write, caps disk-fill attempts

# RLIMIT_NPROC is deliberately NOT used here: it counts processes against the real UID
# system-wide, not per process-tree/cgroup — setting it low broke bubblewrap itself in testing
# (the invoking user already runs far more than a small cap system-wide, so bwrap's own
# namespace-setup forking immediately failed with EAGAIN). Fork-bomb containment needs the
# cgroup v2 `pids.max` controller, which scopes per-cgroup correctly; that's the real fix,
# tracked as a gap (docs/STATUS.md, ADR-0003's sibling gap) rather than faked with the wrong
# primitive here.


def _set_rlimits() -> None:
    if resource is None:  # pragma: no cover — non-Unix hosts never reach the bwrap tier
        raise RuntimeError(
            "resource limits are unavailable on this platform (no `resource` module); "
            "refusing to run uncapped"
        )
    resource.setrlimit(resource.RLIMIT_AS, (BWRAP_MEM_LIMIT_BYTES, BWRAP_MEM_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (BWRAP_CPU_LIMIT_S, BWRAP_CPU_LIMIT_S))
    resource.setrlimit(resource.RLIMIT_NOFILE, (BWRAP_NOFILE_LIMIT, BWRAP_NOFILE_LIMIT))
    resource.setrlimit(resource.RLIMIT_FSIZE, (BWRAP_FSIZE_LIMIT_BYTES, BWRAP_FSIZE_LIMIT_BYTES))


# Fork-bomb / process-count containment: TWO approaches tried this session, both reverted.
# (1) RLIMIT_NPROC — counts against the real UID system-wide, not per invocation; broke
#     bubblewrap itself immediately (the host user already runs far more processes than any
#     sane per-sandbox cap), so it never got past a sanity check.
# (2) cgroup v2 `pids.max` via a transient `systemd-run --user --scope` wrapper — the correctly
#     *scoped* primitive (per-invocation, not per-UID), and it did cap a real fork bomb when
#     tested standalone. But wrapping every bubblewrap invocation with it introduced a real,
#     reproducible race: `systemd-run --scope` sometimes leaves the sandboxed process tree
#     orphaned on the host when the wrapping process is signaled for cancellation (observed
#     ~50% of runs in this session's own test suite — SIGTERM to the outer process didn't
#     reliably cascade through the scope to the sandboxed child, unlike the previously
#     100%-reliable direct-bwrap case). That's a regression in a property Phase 1 already had
#     working solidly (cancellation kills the whole process tree), traded for fork-bomb
#     protection this project doesn't have a documented active exploit path for yet. A flaky
#     safety mechanism that sometimes leaks orphaned sandboxed processes is worse than the
#     honest, documented absence of one — so this was reverted rather than shipped. See
#     docs/adr/0003 and docs/STATUS.md for the full record; a real fix needs a cgroup v2
#     `pids.max` limit applied directly (writing to the cgroupfs path for a cgroup this process
#     creates and owns) rather than delegated through systemd-run's own process/scope lifecycle.


# The exact set of host paths bound into every bubblewrap sandbox — this list *is* the
# sandbox's attack surface from the inside, so it's declared once, versioned, and hashed into
# every ExecResult (the closest bubblewrap equivalent to a "pinned image digest": there's no
# image to pull, but the profile that defines what's visible inside is itself fixed and
# fingerprinted, so a later change to it is detectable by diffing the digest across runs).
_RO_BINDS = ["/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc/ssl", "/etc/resolv.conf"]


def _profile_digest(
    workspace_root: Path,
    seccomp_active: bool = False,
    seccomp_syscalls: list[str] | None = None,
) -> str:
    profile = {
        "ro_binds": [p for p in _RO_BINDS if Path(p).exists()],
        "rw_bind": str(workspace_root),
        "unshare": ["user", "pid", "net", "uts", "ipc", "cgroup"],
        "mem_limit_bytes": BWRAP_MEM_LIMIT_BYTES,
        "cpu_limit_s": BWRAP_CPU_LIMIT_S,
        "nofile_limit": BWRAP_NOFILE_LIMIT,
        "fsize_limit_bytes": BWRAP_FSIZE_LIMIT_BYTES,
        # ADR-0003: whether the seccomp deny-list was actually loaded for this run — included in
        # the digest (not just a fixed "seccomp: true" assumption) so a run where pyseccomp
        # wasn't available (see seccomp_profile.available()) produces a visibly different
        # profile digest, rather than the audit trail implying uniform protection that wasn't
        # actually applied.
        "seccomp_active": seccomp_active,
        # The syscalls the filter ACTUALLY carries, as reported by build_filter — not the
        # declared deny-list. Rules are skipped per syscall when libseccomp or the kernel does
        # not know the name, and hashing the declaration meant the digest attested rules that
        # were never loaded (on an older libseccomp: ptrace and the whole new-mount-API family),
        # with two materially different filters fingerprinting identically. The fallback to the
        # declared list exists only for a caller that has not been updated; it is the wrong
        # answer and the key says so.
        "seccomp_denied_syscalls": sorted(seccomp_syscalls) if seccomp_syscalls is not None else (
            sorted(seccomp_profile.DENIED_SYSCALLS) if seccomp_active else []
        ),
        "seccomp_syscalls_source": (
            "loaded" if seccomp_syscalls is not None else ("declared" if seccomp_active else "none")
        ),
    }
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


class BubblewrapExecutor:
    """Lightweight tier: unprivileged namespaces via `bwrap`.

    Filesystem: only `_RO_BINDS` (read-only) and the workspace (read-write) exist inside the
    sandbox — everything else, including the rest of the host filesystem, simply isn't mounted,
    so there's nothing to "escape" to via path tricks (no bind exists to traverse into).
    Network: `--unshare-net` with no `--share-net`/veth setup means the sandbox has *zero*
    network devices, not even loopback — egress is impossible at the kernel level, not merely
    discouraged by a binary blocklist.
    Resources: RLIMIT_AS/RLIMIT_CPU/RLIMIT_NOFILE/RLIMIT_FSIZE via preexec_fn, independent of the
    wall-clock timeout Popen itself enforces. Fork-bomb/process-count containment is a known,
    documented gap (see the comment above `_set_rlimits` and ADR-0003/docs/STATUS.md) — two
    approaches were tried and reverted this session rather than shipped half-working.
    """

    tier = TIER_BUBBLEWRAP

    @classmethod
    def available(cls, deep: bool = False) -> tuple[bool, str]:
        """(can this tier run here, why). Delegates to availability.probe so there is one
        answer, not one per call site."""
        return probe(cls.tier, deep=deep)

    @staticmethod
    def build_argv(
        argv: list[str], cwd: Path, workspace_root: Path, seccomp_fd: int | None = None
    ) -> list[str]:
        """The exact argv subprocess.run executes. Exposed (not just inlined in run()) so tests
        exercise the real construction instead of a hand-copied approximation that can silently
        drift from it — that drift already happened once in this file's history (a test rebuilt
        this argv by hand and was missing --clearenv/--setenv after they were added here).

        `seccomp_fd`, when given, is an already-open FD on a compiled BPF program (ADR-0003) —
        the caller (run(), or a test exercising this directly) owns opening/closing it; this
        method only decides where `--seccomp <fd>` goes in the argv."""
        workspace_root = workspace_root.resolve()
        bwrap_argv = ["bwrap"]
        for path in _RO_BINDS:
            if Path(path).exists():
                bwrap_argv += ["--ro-bind", path, path]
        # bwrap clears and rebuilds its own child's environment explicitly (--clearenv +
        # --setenv) rather than relying solely on the env= passed to subprocess.run for this
        # guarantee — a second, independent point of control for the same property.
        sandboxed_env = _build_env(workspace_root)
        bwrap_argv += ["--clearenv"]
        for k, v in sandboxed_env.items():
            bwrap_argv += ["--setenv", k, v]
        bwrap_argv += [
            "--proc", "/proc",
            "--dev", "/dev",
            # Mount order matters: /tmp must be tmpfs'd *before* the workspace bind if the
            # workspace happens to live under /tmp (the Phase-1 default), otherwise this tmpfs
            # would mount on top of and shadow the earlier bind.
            "--tmpfs", "/tmp",
            "--bind", str(workspace_root), str(workspace_root),
            "--chdir", str(cwd.resolve()),
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
        ]
        if seccomp_fd is not None:
            bwrap_argv += ["--seccomp", str(seccomp_fd)]
        bwrap_argv += ["--", *argv]
        return bwrap_argv

    @staticmethod
    def outer_env(workspace_root: Path) -> dict:
        return _build_env(workspace_root)

    def run(
        self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float
    ) -> ExecResult:
        workspace_root = workspace_root.resolve()

        # ADR-0003: load the seccomp deny-list when pyseccomp is available; degrade visibly
        # (log a warning, run without it) rather than crash run_command or silently claim
        # protection that wasn't applied — reflected in the profile digest below either way.
        seccomp_fd = None
        seccomp_active = False
        seccomp_syscalls: list[str] | None = None
        if seccomp_profile.available():
            try:
                seccomp_fd, seccomp_syscalls = seccomp_profile.open_bpf_fd()
                seccomp_active = True
            except Exception:
                log.warning("seccomp filter failed to load, running without it", exc_info=True)
                seccomp_fd = None
        else:
            log.warning(
                "pyseccomp not available (%s) — running without the ADR-0003 seccomp filter",
                seccomp_profile.unavailable_reason(),
            )

        try:
            bwrap_argv = self.build_argv(argv, cwd, workspace_root, seccomp_fd=seccomp_fd)
            outer_env = self.outer_env(workspace_root)

            start = time.monotonic()
            code, stdout, stderr, timed_out = run_capped(
                bwrap_argv,
                timeout=timeout,
                env=outer_env,
                preexec_fn=_set_rlimits,
                pass_fds=(seccomp_fd,) if seccomp_fd is not None else (),
            )
            killed_reason = None
            if timed_out:
                killed_reason = "timeout"
            elif code == 137:
                # bash/coreutils convention: 128+SIGKILL(9)=137 when the OOM/RLIMIT kill lands
                # inside the sandboxed process; bwrap itself exits with the child's real status.
                killed_reason = "resource_limit"
            return ExecResult(
                exit_code=code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=(time.monotonic() - start) * 1000,
                isolation_tier=self.tier,
                sandbox_profile_digest=_profile_digest(
                    workspace_root, seccomp_active, seccomp_syscalls),
                seccomp_active=seccomp_active,
                killed_reason=killed_reason,
            )
        finally:
            if seccomp_fd is not None:
                os.close(seccomp_fd)


# ── WSL2 tier ────────────────────────────────────────────────────────────────────────────────
# Honesty note, stated here rather than buried: this tier is implemented and unit-tested at the
# level of argv construction and path translation, and it has NOT been exercised on a Windows
# host with a real WSL2 guest, because the author has no Windows machine. What makes that
# tolerable rather than a false claim is the direction of its failure: `availability._probe_wsl2`
# always exec-verifies (it runs a sandboxed /bin/true inside the guest), and `resolve_tier`
# refuses rather than degrading, so on a host where anything here is wrong the operator is told
# the tier is unavailable and no command runs. The bad outcome is "it refuses on a host where it
# could have worked", not "it ran unsandboxed while reporting a sandbox".

# `--ro-bind-try` rather than `--ro-bind`: the bubblewrap tier filters this list with
# Path(p).exists() on the host, which it can do because host and sandbox share a filesystem.
# From Windows there is no way to stat a guest path without another wsl.exe round trip per
# path, and bwrap already has the primitive for "bind this if it is there".
_WSL_RO_BINDS = list(_RO_BINDS)

_WSLPATH_TIMEOUT_S = 30


class WSL2PathError(RuntimeError):
    """A Windows path could not be translated to its path inside the WSL2 guest."""


class WSL2Executor:
    """Bubblewrap, executed inside the WSL2 guest, for Windows hosts.

    Why this composition and not `wsl.exe -- <command>` on its own: a bare WSL2 invocation is
    not isolation. The guest mounts the Windows drives at /mnt/c by default and has full network
    access, so a command running there can read the operator's whole filesystem and reach the
    network — the two things the bubblewrap profile exists to prevent. The VM boundary is
    valuable *in addition to* the namespaces, not instead of them.

    Resource caps are applied by the guest shell (`ulimit`, `&&`-chained, then `exec bwrap`)
    rather than by a preexec_fn, which cannot cross the boundary. They are the same four limits
    as the Linux tier and are not best-effort: a shell that rejects any of them fails the run,
    and the probe runs the identical prologue so the capability report says so first.

    What is lost relative to the Linux bubblewrap tier is seccomp. The deny-list is compiled to
    a BPF program and handed to bwrap as an open file descriptor; a descriptor does not survive
    the wsl.exe process boundary. `sandbox_profile_digest` records `seccomp_active: False`, so a
    run on this tier is distinguishable in the audit trail from one that had the syscall filter
    loaded — the digest is the mechanism that already exists for exactly this, rather than a
    second claim someone has to remember to check.
    """

    tier = TIER_WSL2

    @classmethod
    def available(cls, deep: bool = False) -> tuple[bool, str]:
        return probe(cls.tier, deep=deep)

    @staticmethod
    def guest_path(win_path: Path) -> str:
        """Translate a Windows path to its path inside the guest, by asking the guest.

        `wslpath` is the guest's own translator, so it accounts for the operator's actual
        /etc/wsl.conf mount root instead of assuming the /mnt/c default. Hand-rolling
        "C:\\x" -> "/mnt/c/x" would be wrong on any host that moved it.
        """
        raw = str(Path(win_path))
        try:
            proc = subprocess.run(
                ["wsl.exe", "-e", "wslpath", "-a", "-u", raw],
                capture_output=True, timeout=_WSLPATH_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            raise WSL2PathError(f"could not translate {raw!r} via wslpath: {e}") from e
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise WSL2PathError(
                f"wslpath could not translate {raw!r} (exit {proc.returncode}): {err[:200]!r}"
            )
        # One path in, one path out. More than one line means something else wrote to stdout,
        # and guessing which line is the path would silently bind the wrong directory.
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(lines) != 1:
            raise WSL2PathError(
                f"wslpath returned {len(lines)} lines for {raw!r}, expected one: {out[:200]!r}"
            )
        return lines[0]

    @staticmethod
    def build_argv(
        argv: list[str], guest_cwd: str, guest_workspace: str
    ) -> list[str]:
        """The exact argv subprocess.run executes, given paths *already translated* to the
        guest. Separated from the translation so it is testable without a WSL2 guest, and
        exposed for the same reason BubblewrapExecutor.build_argv is: a test that rebuilds this
        by hand drifts from it.

        The inner argv is shell-quoted into a single `/bin/sh -c` string rather than passed as
        separate arguments. That is not a convenience: `subprocess` on Windows joins a list into
        one command line by MSVC rules, wsl.exe re-splits it by its own, and a workspace path
        containing a space (`C:\\Users\\Jane Smith\\...`) does not reliably survive the round
        trip. Quoting every element ourselves and letting the guest's shell do the final split
        makes the boundary deterministic. The quoting is generated from an argv list that the
        caller has already parsed — `shlex.quote` on each element — so no element of it, model-
        supplied or not, can break out into shell syntax.
        """
        inner = ["bwrap"]
        for path in _WSL_RO_BINDS:
            inner += ["--ro-bind-try", path, path]
        inner += ["--clearenv"]
        # PurePosixPath, not Path: on a Windows host `Path` is `WindowsPath`, which flips the
        # guest path's separators to backslashes, so `HOME` became
        # `\\mnt\\c\\Users\\...` — a non-existent single-component name inside the sandbox.
        # Anything expanding `~` or writing to $HOME then wrote a literal backslash-named file
        # or failed. Not caught by the tier's tests because they run on Linux, where `Path` is
        # already `PosixPath`.
        for k, v in _build_env(PurePosixPath(guest_workspace)).items():
            inner += ["--setenv", k, v]
        inner += [
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--bind", guest_workspace, guest_workspace,
            "--chdir", guest_cwd,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--", *argv,
        ]
        # The prologue ends in `exec`, so the limits are set on the shell and then the shell
        # is *replaced* by bwrap — no lingering shell process holding the fds, and the limits are
        # inherited by bwrap and everything under it. `&&`-chained so a shell that rejects one
        # of the flags fails the run instead of running uncapped; availability._probe_wsl2 runs
        # this same prologue, so an operator learns that from the capability report rather than
        # from a failed command.
        script = WSL_ULIMIT_PROLOGUE + " ".join(shlex.quote(a) for a in inner)
        # -e: run the program directly, with no login shell or profile of the guest's own. The
        # /bin/sh here is ours, carrying only the string we quoted.
        return ["wsl.exe", "-e", "/bin/sh", "-c", script]

    @staticmethod
    def profile_digest(guest_workspace: str) -> str:
        """Deliberately not `_profile_digest`: a run on this tier must not produce the same
        fingerprint as a Linux bubblewrap run. It has no seccomp, its binds are `--ro-bind-try`
        rather than a host-filtered `--ro-bind` list, and its workspace path is a guest path."""
        profile = {
            "tier": TIER_WSL2,
            "ro_bind_try": list(_WSL_RO_BINDS),
            "rw_bind": guest_workspace,
            "unshare": ["user", "pid", "net", "uts", "ipc", "cgroup"],
            "mem_limit_bytes": BWRAP_MEM_LIMIT_BYTES,
            "cpu_limit_s": BWRAP_CPU_LIMIT_S,
            "nofile_limit": BWRAP_NOFILE_LIMIT,
            # A MAXIMUM, not an exact figure: `ulimit -f` counts blocks whose size depends on
            # the guest's /bin/sh (512 bytes in dash, 1024 in bash), so the real limit is this
            # or half of it. The key name says which.
            "fsize_limit_bytes_max": WSL_ULIMIT_FSIZE_MAX_BYTES,
            # Not an omission to be read as "unknown": the fd cannot cross wsl.exe. Recorded as
            # false so the trail says which runs had the syscall filter and which did not.
            "seccomp_active": False,
            "seccomp_denied_syscalls": [],
            # RLIMIT_* are applied by a wrapper inside the guest rather than by preexec_fn,
            # which cannot reach across the boundary. Recorded so the digest reflects how.
            # Not decoration: the prologue is part of what this profile *is*, so a change to
            # the caps changes the digest, exactly as the Linux tier's byte limits do.
            "rlimits_applied_via": WSL_ULIMIT_PROLOGUE,
        }
        return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()

    def run(
        self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float
    ) -> ExecResult:
        start = time.monotonic()
        try:
            guest_workspace = self.guest_path(workspace_root)
            guest_cwd = self.guest_path(cwd)
        except WSL2PathError as e:
            # A refusal, not a fallback. Running the command on the Windows host instead would
            # be the silent degradation this whole tier system is built to prevent.
            return ExecResult(
                exit_code=None, stdout="", stderr=str(e), timed_out=False,
                duration_ms=(time.monotonic() - start) * 1000,
                isolation_tier=self.tier, killed_reason="path_translation_failed",
            )

        full_argv = self.build_argv(argv, guest_cwd, guest_workspace)
        digest = self.profile_digest(guest_workspace)
        code, stdout, stderr, timed_out = run_capped(full_argv, timeout=timeout)
        killed_reason = None
        if timed_out:
            killed_reason = "timeout"
        elif code == 137:
            killed_reason = "resource_limit"
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_ms=(time.monotonic() - start) * 1000,
            isolation_tier=self.tier,
            sandbox_profile_digest=digest,
            killed_reason=killed_reason,
            seccomp_active=False,  # the BPF fd cannot cross the wsl.exe boundary; see the class
        )


class MicroVMExecutor:
    """Not implemented. See module docstring — deliberately not built this pass, must never
    silently degrade to a weaker tier while claiming this name."""

    tier = TIER_MICROVM

    @classmethod
    def available(cls, deep: bool = False) -> tuple[bool, str]:
        """(can this tier run here, why). Delegates to availability.probe so there is one
        answer, not one per call site."""
        return probe(cls.tier, deep=deep)

    def run(self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float) -> ExecResult:
        raise NotImplementedError(
            "microVM isolation (Firecracker) is not implemented — needs a guest "
            "kernel/rootfs/vsock pipeline not built this pass. Use tier='bubblewrap' or "
            "tier='direct'."
        )


EXECUTORS = {
    TIER_DIRECT: DirectExecutor(),
    TIER_BUBBLEWRAP: BubblewrapExecutor(),
    TIER_WSL2: WSL2Executor(),
    TIER_MICROVM: MicroVMExecutor(),
}


def get_executor(tier: str):
    if tier not in EXECUTORS:
        raise ValueError(f"unknown isolation tier: {tier!r}, choose from {list(EXECUTORS)}")
    return EXECUTORS[tier]
