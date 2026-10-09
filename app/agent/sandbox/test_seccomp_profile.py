"""ADR-0003 seccomp profile tests. Two things this MUST demonstrate, matching the ADR's own
stated test criteria: (a) run_command's allowlisted commands still work under the filter, and
(b) a denied syscall (ptrace, exercised directly) is actually stopped — not just "the filter
object was constructed without error." Both exercised through the real BubblewrapExecutor.run()
path, not a hand-simulated approximation. Run directly:
`python3 -m agent.sandbox.test_seccomp_profile` (needs the venv active — pyseccomp isn't a
system-wide package on this host yet, see seccomp_profile.py's own docstring).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import config
from . import seccomp_profile
from .executor import BubblewrapExecutor, _profile_digest

PASS, FAIL = [], []
#: Checks this host cannot establish either way — reported, never silently counted as a pass.
UNPROVEN: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    # A missing optional dependency is a SKIP, not a failing check. Asserting
    # `seccomp_profile.available()` made this module report "0/1 checks passed" on any host
    # without pyseccomp, which reads as a broken suite rather than an absent package — and it
    # buried the one thing worth saying, which is that nothing below ran.
    if not seccomp_profile.available():
        print(f"SKIP: pyseccomp is not importable here "
              f"({seccomp_profile.unavailable_reason()}); install it to run these checks")
        print("0/0 checks passed (skipped: no pyseccomp on this host)")
        return 0

    print("== Filter construction ==")
    f, accepted = seccomp_profile.build_filter()
    check("build_filter() returns a filter object", f is not None)
    check("and the list of syscalls it actually accepted", isinstance(accepted, list) and accepted)
    check("every accepted name is one we declared",
          set(accepted) <= set(seccomp_profile.DENIED_SYSCALLS),
          str(sorted(set(accepted) - set(seccomp_profile.DENIED_SYSCALLS))))

    print("\n== the profile digest describes the filter that exists, not the one declared ==")
    ws_d = Path(tempfile.mkdtemp(prefix="seccomp-digest-"))
    loaded_digest = _profile_digest(ws_d, True, accepted)
    declared_digest = _profile_digest(ws_d, True, list(seccomp_profile.DENIED_SYSCALLS))
    partial_digest = _profile_digest(ws_d, True, accepted[:-1] if len(accepted) > 1 else [])
    check("a filter missing a rule fingerprints differently",
          partial_digest != loaded_digest)
    check("and the declared list only matches when everything loaded",
          (declared_digest == loaded_digest) == (set(accepted) == set(seccomp_profile.DENIED_SYSCALLS)),
          f"{len(accepted)} of {len(seccomp_profile.DENIED_SYSCALLS)} loaded")

    fd_a, _ = seccomp_profile.open_bpf_fd()
    fd_b, _ = seccomp_profile.open_bpf_fd()
    try:
        size_a = os.fstat(fd_a).st_size
        check("open_bpf_fd() produces a real, non-empty compiled program", size_a > 0)
        check("open_bpf_fd() returns independent fds per call", fd_a != fd_b)
    finally:
        os.close(fd_a)
        os.close(fd_b)
    leftover = list(Path(tempfile.gettempdir()).glob("localai-seccomp-*.bpf"))
    check("no localai-seccomp-*.bpf files left behind in the temp dir", leftover == [], f"leftover={leftover}")

    print("\n== build_argv() inserts --seccomp only when given an fd ==")
    tmp = Path(tempfile.mkdtemp(prefix="seccomp-argv-test-"))
    argv_no_seccomp = BubblewrapExecutor.build_argv(["true"], tmp, tmp)
    check("no --seccomp flag when seccomp_fd is None", "--seccomp" not in argv_no_seccomp)
    fd, _ = seccomp_profile.open_bpf_fd()
    try:
        argv_with_seccomp = BubblewrapExecutor.build_argv(["true"], tmp, tmp, seccomp_fd=fd)
        check("--seccomp <fd> present when seccomp_fd is given", "--seccomp" in argv_with_seccomp and str(fd) in argv_with_seccomp)
    finally:
        os.close(fd)
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n== Profile digest reflects seccomp_active ==")
    tmp2 = Path(tempfile.mkdtemp(prefix="seccomp-digest-test-"))
    digest_off = _profile_digest(tmp2, seccomp_active=False)
    digest_on = _profile_digest(tmp2, seccomp_active=True)
    check("digest differs when seccomp_active flips", digest_off != digest_on)
    shutil.rmtree(tmp2, ignore_errors=True)

    print("\n== (a) Allowlisted commands still work under the real seccomp+bubblewrap tier ==")
    # Every member of config.COMMAND_ALLOWLIST, not a hand-picked sample of four. The sample
    # covered ls/cat/echo/python3 and reported "PASS" on a host where 18 of the 22 allowlisted
    # commands — including `tree` — were never actually invoked under the filter in this check.
    # That is the same shape as the bug `test_dev_server_imports.py` and
    # `test_electron_install_hint.py` close for the install line: a check whose name claims an
    # invariant over a real registry, verified against a separate, hand-maintained copy that can
    # silently stop covering new (or, here, most existing) members. Running the real list here
    # is what actually found that `tree` is allowlisted but not installed by any documented
    # step — see docs/DEPLOY_UBUNTU.md's apt line and TOOL_PARITY.md if that changes.
    executor = BubblewrapExecutor()

    def _argv_for(cmd: str, fixture: Path) -> list[str]:
        # /etc/resolv.conf and the workspace are the only readable/writable paths inside the
        # sandbox (_RO_BINDS plus the bound workspace) — anything else fails for an unrelated
        # reason (file doesn't exist in the sandbox) and says nothing about seccomp specifically.
        return {
            "ls": ["ls", "/"],
            "cat": ["cat", "/etc/resolv.conf"],
            "pwd": ["pwd"],
            "echo": ["echo", "hello"],
            "head": ["head", str(fixture)],
            "tail": ["tail", str(fixture)],
            "wc": ["wc", "-l", str(fixture)],
            "grep": ["grep", "-c", "beta", str(fixture)],
            "find": ["find", str(fixture)],
            "sort": ["sort", str(fixture)],
            "uniq": ["uniq", str(fixture)],
            "diff": ["diff", str(fixture), str(fixture)],
            "mkdir": ["mkdir", "-p", str(fixture.parent / "sub")],
            "touch": ["touch", str(fixture.parent / "touched")],
            "file": ["file", str(fixture)],
            "stat": ["stat", str(fixture)],
            "tree": ["tree", str(fixture.parent)],
            "cut": ["cut", "-d", "\n", "-f1", str(fixture)],
            "sed": ["sed", "s/alpha/ALPHA/", str(fixture)],
            "awk": ["awk", "{print}", str(fixture)],
            "python3": ["python3", "-c", "print('seccomp-ok')"],
            "date": ["date"],
            "env": ["env"],
        }[cmd]

    for cmd in sorted(config.COMMAND_ALLOWLIST):
        if shutil.which(cmd) is None:
            print(f"  SKIPPED  {cmd}: not installed on this host (not a seccomp question)")
            continue
        ws = Path(tempfile.mkdtemp(prefix="seccomp-cmd-test-"))
        try:
            fixture = ws / "probe.txt"
            fixture.write_text("alpha\nbeta\ngamma\n")
            argv = _argv_for(cmd, fixture)
            result = executor.run(argv, ws, ws, timeout=15)
            check(
                f"{' '.join(argv)} succeeds under seccomp filter",
                result.exit_code == 0 and not result.timed_out,
                f"exit_code={result.exit_code} stderr={result.stderr[:200]!r}",
            )
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    print("\n== (b) A denied syscall (ptrace) is actually stopped, not just documented ==")
    ws2 = Path(tempfile.mkdtemp(prefix="seccomp-ptrace-test-"))
    try:
        ptrace_probe = (
            "import ctypes\n"
            "libc = ctypes.CDLL(None, use_errno=True)\n"
            "ret = libc.ptrace(0, 0, 0, 0)\n"  # PTRACE_TRACEME
            "err = ctypes.get_errno()\n"
            "print(f'ret={ret} errno={err}')\n"
        )
        result = executor.run(["python3", "-c", ptrace_probe], ws2, ws2, timeout=15)
        check(
            "ptrace(PTRACE_TRACEME) denied with EPERM under the filter",
            "ret=-1 errno=1" in result.stdout,
            f"stdout={result.stdout!r} stderr={result.stderr[:200]!r}",
        )
    finally:
        shutil.rmtree(ws2, ignore_errors=True)

    print("\n== Control: without the filter, ptrace(PTRACE_TRACEME) is NOT denied ==")
    ws3 = Path(tempfile.mkdtemp(prefix="seccomp-control-test-"))
    try:
        argv = BubblewrapExecutor.build_argv(
            ["python3", "-c",
             "import ctypes\nlibc=ctypes.CDLL(None,use_errno=True)\nret=libc.ptrace(0,0,0,0)\nerr=ctypes.get_errno()\nprint(f'ret={ret} errno={err}')\n"],
            ws3, ws3, seccomp_fd=None,
        )
        import subprocess

        proc = subprocess.run(argv, env=BubblewrapExecutor.outer_env(ws3), capture_output=True, text=True, timeout=15)

        # Is the control even possible on this bubblewrap? Measured, not assumed: on
        # bubblewrap 0.9.0 the bare host allows ptrace(PTRACE_TRACEME) while ANY bwrap
        # invocation denies it — with no seccomp filter of ours, and with or without
        # --unshare-all. bwrap denies it itself.
        #
        # That makes ptrace unable to demonstrate this filter: the "denied under the filter"
        # check above passes whether or not the filter is loaded, which is the definition of a
        # vacuous test. Reported as unproven rather than failed, because nothing here is broken
        # — ptrace is simply the wrong syscall to prove it with. The unshare pair below is the
        # one that actually proves it, and it is a hard assertion.
        if "ret=0 errno=0" in proc.stdout:
            check(
                "without the filter, ptrace(PTRACE_TRACEME) succeeds (proves the denial above is the filter, not something else)",
                True,
            )
        else:
            UNPROVEN.append(
                "ptrace cannot demonstrate this filter: bubblewrap denies ptrace itself "
                f"(no-filter probe inside bwrap: {proc.stdout.strip()!r}), so the "
                "'ptrace denied under the filter' check above is vacuous on this bwrap version"
            )
            print("  NOTE  bubblewrap denies ptrace with no filter of ours loaded "
                  f"({proc.stdout.strip()!r}), though the bare host allows it. ptrace therefore "
                  "cannot attribute anything to this filter here — see the unshare pair below, "
                  "which can.")
    finally:
        shutil.rmtree(ws3, ignore_errors=True)

    print("\n== The filter is proved by a syscall bubblewrap itself permits: unshare ==")
    # This is the pair that makes the suite non-vacuous. unshare(CLONE_NEWUSER) SUCCEEDS inside
    # bwrap with no filter of ours (bubblewrap permits nested user namespaces) and is EPERM with
    # the filter loaded. Both halves are asserted, so neither "the filter does nothing" nor
    # "something else was denying it" can pass unnoticed.
    #
    # It is also the right syscall to care about: creating a nested user namespace is the
    # primitive for building a fresh sandbox inside this one, i.e. for stepping outside the
    # confinement the tier claims.
    probe = ("import ctypes\n"
             "libc = ctypes.CDLL(None, use_errno=True)\n"
             "ret = libc.unshare(0x10000000)\n"   # CLONE_NEWUSER
             "print(f'ret={ret} errno={ctypes.get_errno()}')\n")
    ws4 = Path(tempfile.mkdtemp(prefix="seccomp-unshare-test-"))
    try:
        with_filter = BubblewrapExecutor().run(["python3", "-c", probe], ws4, ws4, timeout=15)
        check("unshare(CLONE_NEWUSER) is denied with EPERM under the filter",
              "ret=-1 errno=1" in with_filter.stdout,
              f"stdout={with_filter.stdout!r} stderr={with_filter.stderr[:160]!r}")

        argv_off = BubblewrapExecutor.build_argv(
            ["python3", "-c", probe], ws4, ws4, seccomp_fd=None)
        without = subprocess.run(argv_off, env=BubblewrapExecutor.outer_env(ws4),
                                 capture_output=True, text=True, timeout=15)
        check("and SUCCEEDS without it, so the denial is this filter and nothing else",
              "ret=0 errno=0" in without.stdout,
              f"stdout={without.stdout!r} stderr={without.stderr[:160]!r}")
    finally:
        shutil.rmtree(ws4, ignore_errors=True)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if UNPROVEN:
        print("UNPROVEN on this host (not failures, and not passes):")
        for u in UNPROVEN:
            print(f"  - {u}")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
