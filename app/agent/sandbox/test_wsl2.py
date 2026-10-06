"""Tests for the wsl2 isolation tier: the probe's refusals, the argv it builds, and the path
translation it depends on.

The honest limitation, repeated here because a test file is where someone checks it: there is no
Windows host and no WSL2 guest in this project's development environment, so what is verified
below is argv construction, path translation and every refusal path — not a real sandboxed
execution. The tier is designed so that gap fails in the safe direction:
`availability._probe_wsl2` always exec-verifies rather than assuming, and `resolve_tier` refuses
instead of degrading, so on a host where any assumption here is wrong the operator is told the
tier is unavailable and nothing runs unsandboxed.

Run directly: `python3 -m agent.sandbox.test_wsl2`.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

from . import availability as av
from .executor import WSL2Executor, WSL2PathError

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


class _Proc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def main() -> int:
    print("\n== the probe refuses, with a reason that says what to do ==")
    ok, why = av.probe(av.TIER_WSL2)
    check("on this Linux host the tier is unavailable", ok is False)
    check("and it points at the bubblewrap tier instead", "bubblewrap" in why, why)

    with patch.object(av.platform, "system", lambda: "Windows"), \
         patch.object(av.shutil, "which", lambda name: None):
        ok, why = av.probe(av.TIER_WSL2)
        check("a Windows host without wsl.exe is unavailable", ok is False)
        check("and is told to install WSL2", "install WSL2" in why, why)

    calls: list[list[str]] = []

    def fake_wsl(results):
        def _run(args, **kw):
            calls.append(list(args))
            return _Proc(*results.pop(0)) if results else _Proc(0)
        return _run

    with patch.object(av.platform, "system", lambda: "Windows"), \
         patch.object(av.shutil, "which", lambda name: "C:\\Windows\\System32\\wsl.exe"):
        # 1. the guest itself does not start
        with patch.object(av.subprocess, "run",
                          fake_wsl([(1, b"", b"no distribution")])):
            ok, why = av.probe(av.TIER_WSL2)
        check("a guest that will not start is unavailable", ok is False)
        check("and the reason quotes the guest's own error", "no distribution" in why, why)

        # 2. guest runs, bwrap missing
        calls.clear()
        with patch.object(av.subprocess, "run",
                          fake_wsl([(0, b"", b""), (127, b"", b"bwrap: not found")])):
            ok, why = av.probe(av.TIER_WSL2)
        check("a guest without bubblewrap is unavailable", ok is False)
        check("and the reason says how to install it in the guest",
              "apt install bubblewrap" in why, why)

        # 3. bwrap present but cannot unshare — the case that cannot be predicted statically
        calls.clear()
        with patch.object(av.subprocess, "run", fake_wsl([
            (0, b"", b""), (0, b"bubblewrap 0.8.0\n", b""),
            (1, b"", b"bwrap: No permissions to creating new namespace"),
        ])):
            ok, why = av.probe(av.TIER_WSL2)
        check("a guest kernel that forbids unsharing is unavailable", ok is False)
        check("and the reason names both possible causes",
              "user namespaces" in why and "ulimit" in why, why)
        check("the definitive check ran the real prologue, not a bare bwrap",
              any(av.WSL_ULIMIT_PROLOGUE in str(a) for a in calls[-1]), str(calls[-1])[:160])

        # 4. everything works
        calls.clear()
        with patch.object(av.subprocess, "run", fake_wsl([
            (0, b"", b""), (0, b"bubblewrap 0.8.0\n", b""), (0, b"", b""),
        ])):
            ok, why = av.probe(av.TIER_WSL2)
        check("a guest that passes every check is available", ok is True, why)
        check("and says it was verified by executing, not assumed",
              "verified by executing" in why, why)
        check("it never claims availability without execing",
              len(calls) == 3, f"{len(calls)} wsl.exe calls")

    print("\n== the probe always exec-verifies, whatever `deep` says ==")
    with patch.object(av.platform, "system", lambda: "Windows"), \
         patch.object(av.shutil, "which", lambda name: "wsl.exe"):
        for deep in (False, True):
            calls.clear()
            with patch.object(av.subprocess, "run", fake_wsl([
                (0, b"", b""), (0, b"bubblewrap 0.8.0\n", b""), (0, b"", b""),
            ])):
                av.probe(av.TIER_WSL2, deep=deep)
            check(f"deep={deep} still execs inside the guest", len(calls) == 3, str(len(calls)))

    print("\n== resolve_tier still refuses rather than downgrading ==")
    try:
        av.resolve_tier(av.TIER_WSL2)
        check("an unavailable wsl2 raises instead of returning direct", False, "it returned")
    except av.IsolationUnavailableError as e:
        check("an unavailable wsl2 raises instead of returning direct", True)
        check("and the message says no command was run",
              "Refusing to execute" in str(e), str(e)[:120])
    tier, why = av.resolve_tier(av.TIER_WSL2, allow_direct_fallback=True)
    check("a fallback still requires the operator to have authorized it",
          tier == av.TIER_DIRECT and "explicit operator authorization" in why, f"{tier}: {why}")

    print("\n== the tier is ranked, described, and reachable through get_executor ==")
    from .executor import EXECUTORS, get_executor
    check("get_executor knows it", isinstance(get_executor(av.TIER_WSL2), WSL2Executor))
    check("it is one of the declared tiers", av.TIER_WSL2 in av.TIERS)
    check("every executor has a tier entry", set(EXECUTORS) == set(av.TIERS))
    check("it is ranked above bare bubblewrap",
          av._TIER_STRENGTH.index(av.TIER_WSL2) > av._TIER_STRENGTH.index(av.TIER_BUBBLEWRAP))
    desc = av.TIER_DESCRIPTION[av.TIER_WSL2]
    check("the description does not claim seccomp it cannot load",
          "NO seccomp" in desc, desc)
    host = av.describe_host()
    check("describe_host reports it", av.TIER_WSL2 in host["tiers"])

    print("\n== the argv is bubblewrap, inside the guest, under real limits ==")
    argv = WSL2Executor.build_argv(["grep", "-rn", "pass word", "notes.txt"],
                                   "/mnt/c/ws", "/mnt/c/ws")
    check("it is launched through wsl.exe with no login shell",
          argv[:4] == ["wsl.exe", "-e", "/bin/sh", "-c"], str(argv[:4]))
    script = argv[4]
    check("the limits are set before the program, and chained so one failing fails the run",
          script.startswith(av.WSL_ULIMIT_PROLOGUE) and "&&" in av.WSL_ULIMIT_PROLOGUE,
          script[:60])
    check("the shell is replaced rather than left holding the process",
          av.WSL_ULIMIT_PROLOGUE.rstrip().endswith("exec"), av.WSL_ULIMIT_PROLOGUE)
    for flag in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
                 "--proc", "--dev", "--tmpfs"):
        check(f"the sandbox keeps {flag}", flag in script)
    check("it binds the workspace read-write",
          "--bind /mnt/c/ws /mnt/c/ws" in script, script[-200:])
    check("and chdirs inside it", "--chdir /mnt/c/ws" in script, script[-200:])
    check("host paths are bound with --ro-bind-try, since they cannot be stat'd from Windows",
          "--ro-bind-try /usr /usr" in script and "--ro-bind " not in script, script[:200])

    print("\n== an argument with a space survives, and cannot break into shell syntax ==")
    tail = shlex.split(script[len(av.WSL_ULIMIT_PROLOGUE):])
    check("the program and its arguments round-trip exactly",
          tail[-4:] == ["--", "grep", "-rn", "pass word"] or
          tail[-2:] == ["pass word", "notes.txt"], str(tail[-5:]))
    hostile = WSL2Executor.build_argv(
        ["cat", "x'; rm -rf /; echo '"], "/mnt/c/ws", "/mnt/c/ws")
    reparsed = shlex.split(hostile[4][len(av.WSL_ULIMIT_PROLOGUE):])
    check("a shell metacharacter in an argument stays one argument",
          reparsed[-1] == "x'; rm -rf /; echo '", repr(reparsed[-1]))
    check("and does not appear as syntax the guest shell would act on",
          reparsed.count("rm") == 0, str(reparsed[-3:]))

    print("\n== the profile digest tells this tier's runs apart from a Linux bubblewrap run ==")
    from .executor import BubblewrapExecutor, _profile_digest
    d_wsl = WSL2Executor.profile_digest("/mnt/c/ws")
    d_bwrap = _profile_digest(Path("/tmp/ws"))
    check("the two digests differ", d_wsl != d_bwrap)
    check("the same workspace gives a stable digest",
          d_wsl == WSL2Executor.profile_digest("/mnt/c/ws"))
    check("a different workspace gives a different one",
          d_wsl != WSL2Executor.profile_digest("/mnt/d/other"))
    # Patched on the executor module, not on availability: executor.py imports the name, so
    # rebinding it on availability would leave the executor reading the original and the check
    # would pass without testing anything.
    from . import executor as ex
    with patch.object(ex, "WSL_ULIMIT_PROLOGUE", "ulimit -v 1 && exec "):
        loosened = WSL2Executor.profile_digest("/mnt/c/ws")
        loosened_argv = WSL2Executor.build_argv(["id"], "/mnt/c/ws", "/mnt/c/ws")
    check("weakening the resource limits changes the fingerprint", loosened != d_wsl,
          f"{loosened[:12]} == {d_wsl[:12]}")
    check("and the weakened limits are what would actually be applied",
          loosened_argv[4].startswith("ulimit -v 1 && exec "), loosened_argv[4][:40])
    check("bubblewrap's own build_argv is untouched by this tier",
          "--ro-bind-try" not in " ".join(
              BubblewrapExecutor.build_argv(["id"], Path("/tmp/ws"), Path("/tmp/ws"))))

    print("\n== path translation asks the guest, and refuses when it cannot ==")
    with patch.object(subprocess, "run",
                      lambda a, **k: _Proc(0, b"/mnt/c/Users/Jane Smith/ws\n", b"")):
        check("wslpath's answer is used verbatim",
              WSL2Executor.guest_path(Path("C:/Users/Jane Smith/ws"))
              == "/mnt/c/Users/Jane Smith/ws")
    for name, proc, frag in [
        ("a non-zero exit", _Proc(1, b"", b"wslpath: bad path"), "could not translate"),
        ("empty output", _Proc(0, b"", b""), "could not translate"),
        ("more than one line", _Proc(0, b"/mnt/c/a\n/mnt/c/b\n", b""), "expected one"),
    ]:
        with patch.object(subprocess, "run", lambda a, _p=proc, **k: _p):
            try:
                WSL2Executor.guest_path(Path("C:/ws"))
                check(f"{name} is refused", False, "it returned a path")
            except WSL2PathError as e:
                check(f"{name} is refused", frag in str(e), str(e)[:120])

    def _boom(a, **k):
        raise FileNotFoundError("wsl.exe")
    with patch.object(subprocess, "run", _boom):
        try:
            WSL2Executor.guest_path(Path("C:/ws"))
            check("a missing wsl.exe is refused", False, "it returned a path")
        except WSL2PathError as e:
            check("a missing wsl.exe is refused", "wslpath" in str(e), str(e)[:120])

    print("\n== a failed translation refuses the run; it does not fall back to the host ==")
    with patch.object(WSL2Executor, "guest_path",
                      staticmethod(lambda p: (_ for _ in ()).throw(WSL2PathError("nope")))), \
         patch.object(subprocess, "run",
                      lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran anyway"))):
        res = WSL2Executor().run(["id"], Path("C:/ws"), Path("C:/ws"), 5)
    check("the result is a refusal", res.exit_code is None and res.timed_out is False)
    check("attributed to the translation, not to the command",
          res.killed_reason == "path_translation_failed", str(res.killed_reason))
    check("and it still reports the tier it was asked for, not a weaker one",
          res.isolation_tier == av.TIER_WSL2, res.isolation_tier)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
