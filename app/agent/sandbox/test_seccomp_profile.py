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
import tempfile
from pathlib import Path

from . import seccomp_profile
from .executor import BubblewrapExecutor, _profile_digest

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    print("== pyseccomp availability on this system ==")
    check(
        "pyseccomp is available (venv)",
        seccomp_profile.available(),
        f"unavailable_reason={seccomp_profile.unavailable_reason()}",
    )
    if not seccomp_profile.available():
        print("  SKIPPED: remaining checks need pyseccomp; install it (venv has it) to run them")
        print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
        return 1 if FAIL else 0

    print("\n== Filter construction ==")
    f = seccomp_profile.build_filter()
    check("build_filter() returns a filter object", f is not None)

    fd_a = seccomp_profile.open_bpf_fd()
    fd_b = seccomp_profile.open_bpf_fd()
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
    fd = seccomp_profile.open_bpf_fd()
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
    executor = BubblewrapExecutor()
    allowlisted_cases = [
        ("ls /", ["ls", "/"]),
        # /etc/resolv.conf is one of the paths actually bind-mounted into the sandbox
        # (_RO_BINDS) — /etc/hostname is not, so cat-ing it would fail for an unrelated reason
        # (file doesn't exist in the sandbox) and say nothing about seccomp specifically.
        ("cat /etc/resolv.conf", ["cat", "/etc/resolv.conf"]),
        ("echo hello", ["echo", "hello"]),
        ("python3 -c print", ["python3", "-c", "print('seccomp-ok')"]),
    ]
    for label, argv in allowlisted_cases:
        ws = Path(tempfile.mkdtemp(prefix="seccomp-cmd-test-"))
        try:
            result = executor.run(argv, ws, ws, timeout=15)
            check(
                f"{label} succeeds under seccomp filter",
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
        check(
            "without the filter, ptrace(PTRACE_TRACEME) succeeds (proves the denial above is the filter, not something else)",
            "ret=0 errno=0" in proc.stdout,
            f"stdout={proc.stdout!r} stderr={proc.stderr[:200]!r}",
        )
    finally:
        shutil.rmtree(ws3, ignore_errors=True)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
