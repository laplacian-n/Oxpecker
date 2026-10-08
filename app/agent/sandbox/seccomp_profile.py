"""ADR-0003: seccomp filter for the bubblewrap tier.

A default-allow / explicit-deny profile — deliberately NOT a from-scratch per-program
allowlist. Both independent reviews warned against copying a generic allowlist profile and
calling it safe ("ห้าม copy profile ทั่วไปแล้วอ้างว่าปลอดภัย" — local-security-agent-review-
phase4.md §1.2); building a genuine minimal allowlist for every command in
`config.COMMAND_ALLOWLIST` (python3 especially — its interpreter alone uses dozens of distinct
syscalls) needs strace-based profiling this project explicitly declined to fake. What IS built
here, and is real: a deny-list of syscalls with no legitimate use by any of run_command's
allowlisted commands but genuine value as sandbox-escape/privilege-escalation primitives —
process introspection (ptrace and its cross-process-memory relatives), namespace/mount
manipulation, kernel module/BPF loading, and a handful of kernel-info-leak/host-wide-state
syscalls. Verified two ways in `test_seccomp_profile.py`: every allowlisted command still runs
correctly under this filter, and a process that calls a denied syscall (ptrace, exercised
directly) is actually stopped.

Optional dependency, tried under two different import names for the same API: `pyseccomp` (a
pure-ctypes wrapper around `libseccomp.so.2`, installed in this project's venv via
`requirements-sandbox-hardening.txt`) or `seccomp` (the official libseccomp Python binding,
installed system-wide by the Debian package `python3-seccomp` — its module name is `seccomp`,
not `pyseccomp`, confirmed by inspecting `dpkg -L python3-seccomp` directly rather than assumed;
an earlier version of this file only tried `pyseccomp` and would have silently stayed
venv-only even after the system package was installed). Both expose the identical
`SyscallFilter`/`ALLOW`/`ERRNO` API pyseccomp was built to mirror. Import failure degrades
visibly, not silently: `available()` reports False and every caller checks it before relying on
seccomp being active, rather than silently running unfiltered while claiming otherwise.
"""
from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger("agent.sandbox.seccomp_profile")

try:
    import pyseccomp as _seccomp

    _IMPORT_ERROR: str | None = None
except ImportError as e:
    try:
        import seccomp as _seccomp  # the python3-seccomp system package's module name

        _IMPORT_ERROR = None
    except ImportError:
        _seccomp = None
        _IMPORT_ERROR = str(e)

# Syscalls with no legitimate use by any of run_command's allowlisted commands
# (config.COMMAND_ALLOWLIST: ls/cat/pwd/echo/head/tail/wc/grep/find/sort/uniq/diff/mkdir/touch/
# file/stat/tree/cut/sed/awk/python3/date/env) but real value as sandbox-escape/privilege-
# escalation primitives. Grouped by the capability they represent.
DENIED_SYSCALLS = [
    # Process introspection / cross-process memory access — the core "ptrace a sibling process,
    # extract its secrets or hijack its execution" primitive.
    "ptrace", "process_vm_readv", "process_vm_writev",
    # Namespace/mount manipulation — bwrap itself needs these to SET UP the sandbox (run from
    # outside, before this filter is loaded into the sandboxed child), but nothing running
    # *inside* an already-constructed sandbox legitimately needs them again.
    "mount", "umount2", "unshare", "setns", "pivot_root",
    "move_mount", "open_tree", "fsopen", "fsconfig", "fsmount", "fspick",
    # Kernel/module/BPF — host-kernel-level capability, never needed by a CLI text-processing
    # tool.
    "bpf", "init_module", "finit_module", "delete_module",
    "kexec_load", "kexec_file_load",
    # Host-wide state changes with no per-sandbox scoping.
    "reboot", "swapon", "swapoff", "acct",
    "clock_settime", "clock_adjtime", "settimeofday", "adjtimex",
    # Kernel info-leak / exploit-dev surface.
    "syslog", "perf_event_open", "open_by_handle_at",
    # Kernel keyring — a credential-adjacent primitive irrelevant to run_command's own tools.
    "keyctl", "add_key", "request_key",
]

def available() -> bool:
    return _seccomp is not None


def unavailable_reason() -> str | None:
    return _IMPORT_ERROR


def build_filter() -> tuple[object, list[str]]:
    """(compiled filter, the syscall names actually in it).

    The second element is the whole point of the signature change. Rules are skipped per
    syscall when libseccomp/the kernel/the architecture does not know the name — correctly, so
    one unknown name does not cost the whole deny-list — but the caller used to hash
    `DENIED_SYSCALLS` into the sandbox profile digest regardless. The digest then attested a
    deny-list that was partially not loaded: on an older libseccomp, `ptrace` and the entire
    new-mount-API family (`move_mount`, `open_tree`, `fsopen`, `fsconfig`, `fsmount`, `fspick`)
    were skipped while the digest still listed them, and two hosts with materially different
    filters produced the same fingerprint. Returning what was accepted lets the digest describe
    the filter that exists.

    Raises RuntimeError if pyseccomp isn't available — callers must check available() first if
    they want to degrade gracefully instead.
    """
    if _seccomp is None:
        raise RuntimeError(f"pyseccomp not available: {_IMPORT_ERROR}")
    f = _seccomp.SyscallFilter(defaction=_seccomp.ALLOW)
    accepted: list[str] = []
    skipped: list[str] = []
    for name in DENIED_SYSCALLS:
        try:
            # EPERM, not a filter-triggered kill: the caller sees a normal-looking syscall
            # failure it can report/handle, rather than the whole sandboxed process being
            # killed outright — easier to diagnose, same practical denial.
            f.add_rule(_seccomp.ERRNO(1), name)
        except OSError:
            # Syscall name not recognized on this libseccomp/kernel version/arch — skip rather
            # than fail filter construction entirely; the rest of the deny-list still applies.
            skipped.append(name)
            continue
        accepted.append(name)
    if skipped:
        # Raised from debug to warning: "which protections were applied" is the question the
        # profile digest exists to answer, and a silent skip is how that answer went wrong.
        log.warning(
            "seccomp: %d of %d deny rules were not recognised on this system and are NOT in "
            "the filter: %s", len(skipped), len(DENIED_SYSCALLS), ", ".join(skipped),
        )
    return f, accepted


def open_bpf_fd() -> tuple[int, list[str]]:
    """Compiles the filter and returns an open, seek-reset FD holding the compiled BPF program.
    The backing temp file is deleted immediately after the FD is duplicated — Linux doesn't
    require a path to exist for an already-open descriptor to keep working, so nothing
    accumulates on disk across repeated calls (an earlier version of this function cached the
    compiled file under a fixed path with `delete=False` and never cleaned it up; found and
    fixed while verifying this module didn't leak state, the same discipline applied to every
    other module this session).

    Returns `(fd, accepted_syscalls)` — the second element is what the caller must record in
    the sandbox profile digest, not the declared deny-list; see `build_filter`.
    """
    f, accepted = build_filter()
    with tempfile.NamedTemporaryFile(prefix="localai-seccomp-", suffix=".bpf") as tmp:
        f.export_bpf(tmp)
        tmp.flush()
        fd = os.dup(tmp.fileno())
    # tmp's own fd is closed (and the file unlinked, delete=True default) by the `with` block
    # exiting — `fd` above is an independent duplicate that survives that.
    os.lseek(fd, 0, os.SEEK_SET)
    return fd, accepted
