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
import resource
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import seccomp_profile

log = logging.getLogger("agent.sandbox.executor")

TIER_DIRECT = "direct"
TIER_BUBBLEWRAP = "bubblewrap"
TIER_MICROVM = "microvm"


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

    def run(
        self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float
    ) -> ExecResult:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=_build_env(workspace_root),
                timeout=timeout,
                capture_output=True,
                shell=False,
                text=True,
            )
            return ExecResult(
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
                duration_ms=(time.monotonic() - start) * 1000,
                isolation_tier=self.tier,
            )
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                exit_code=None,
                stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
                timed_out=True,
                duration_ms=(time.monotonic() - start) * 1000,
                isolation_tier=self.tier,
                killed_reason="timeout",
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


def _profile_digest(workspace_root: Path, seccomp_active: bool = False) -> str:
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
        "seccomp_denied_syscalls": sorted(seccomp_profile.DENIED_SYSCALLS) if seccomp_active else [],
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
        if seccomp_profile.available():
            try:
                seccomp_fd = seccomp_profile.open_bpf_fd()
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
            try:
                proc = subprocess.run(
                    bwrap_argv,
                    env=outer_env,
                    timeout=timeout,
                    capture_output=True,
                    shell=False,
                    text=True,
                    preexec_fn=_set_rlimits,
                    pass_fds=(seccomp_fd,) if seccomp_fd is not None else (),
                )
                duration_ms = (time.monotonic() - start) * 1000
                killed_reason = None
                # bash/coreutils convention: 128+SIGKILL(9)=137 when the OOM/RLIMIT kill lands
                # inside the sandboxed process; bwrap itself exits with the child's real status.
                if proc.returncode == 137:
                    killed_reason = "resource_limit"
                return ExecResult(
                    exit_code=proc.returncode,
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                    timed_out=False,
                    duration_ms=duration_ms,
                    isolation_tier=self.tier,
                    sandbox_profile_digest=_profile_digest(workspace_root, seccomp_active),
                    killed_reason=killed_reason,
                )
            except subprocess.TimeoutExpired as e:
                return ExecResult(
                    exit_code=None,
                    stdout=(e.stdout or "") if isinstance(e.stdout, str) else "",
                    stderr=(e.stderr or "") if isinstance(e.stderr, str) else "",
                    timed_out=True,
                    duration_ms=(time.monotonic() - start) * 1000,
                    isolation_tier=self.tier,
                    sandbox_profile_digest=_profile_digest(workspace_root, seccomp_active),
                    killed_reason="timeout",
                )
        finally:
            if seccomp_fd is not None:
                os.close(seccomp_fd)


class MicroVMExecutor:
    """Not implemented. See module docstring — deliberately not built this pass, must never
    silently degrade to a weaker tier while claiming this name."""

    tier = TIER_MICROVM

    def run(self, argv: list[str], cwd: Path, workspace_root: Path, timeout: float) -> ExecResult:
        raise NotImplementedError(
            "microVM isolation (Firecracker) is not implemented — needs a guest "
            "kernel/rootfs/vsock pipeline not built this pass. Use tier='bubblewrap' or "
            "tier='direct'."
        )


EXECUTORS = {
    TIER_DIRECT: DirectExecutor(),
    TIER_BUBBLEWRAP: BubblewrapExecutor(),
    TIER_MICROVM: MicroVMExecutor(),
}


def get_executor(tier: str):
    if tier not in EXECUTORS:
        raise ValueError(f"unknown isolation tier: {tier!r}, choose from {list(EXECUTORS)}")
    return EXECUTORS[tier]
