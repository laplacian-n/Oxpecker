"""Tests for what run_command will refuse and what it will run without asking a human.

Both gates read `Path(argv[0]).name` and nothing else, which made them laundering paths:

  * `["env", "curl", "-s", "http://x"]` passed the block list AND the confirmation gate, while
    `["curl", ...]` was refused outright. In the `direct` tier that block list is the only
    egress control there is, and the bypass was confirmed with a real HTTP request to a local
    server, with no prompt shown.
  * A file the agent had just written into its own workspace and named `ls` ran with no
    confirmation, while the binary-existence check "validated" /usr/bin/ls.

Run directly: `python3 -m agent.tools.test_command_screening`.
"""
from __future__ import annotations

import http.server
import pathlib
import tempfile
import threading

from .. import config
from . import run_command as rc

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    print("\n== a wrapper cannot carry a blocked binary past the block list ==")
    for argv in (
        ["env", "curl", "--version"],
        ["env", "-i", "curl", "--version"],
        ["timeout", "5", "nc", "-l", "1"],        # positional duration, not a flag
        ["nice", "-n", "5", "wget", "http://x"],  # flag WITH a separate value
        ["flock", "/tmp/l", "curl", "http://x"],  # positional file
        ["xargs", "wget"],
        ["busybox", "wget", "http://x"],
        ["proxychains", "curl", "http://x"],
        ["nohup", "ssh", "host"],
        ["/usr/bin/env", "curl", "http://x"],     # wrapper given by path
    ):
        blocked, why = rc.preflight(argv, dangerous_local=False)
        check(f"{' '.join(argv[:4])} is blocked", blocked is True, str(why))
        if blocked:
            check(f"and the reason names the real binary ({argv})",
                  any(b in why for b in ("curl", "nc", "wget", "ssh")), why)

    check("the unwrapped form is still blocked too",
          rc.preflight(["curl", "--version"], dangerous_local=False)[0] is True)
    check("dangerous_local still overrides, as before",
          rc.preflight(["env", "curl", "--version"], dangerous_local=True)[0] is False)

    print("\n== the over-broad token scan applies only when a wrapper is present ==")
    # Screening every token is the safe direction for a wrapped argv, but it must not turn an
    # ordinary search for the word "curl" into a refusal.
    for argv in (["grep", "-rn", "curl", "notes.txt"],
                 ["cat", "wget-output.txt"],
                 ["echo", "nc"]):
        check(f"{' '.join(argv)} is not blocked",
              rc.preflight(argv, dangerous_local=False)[0] is False,
              str(rc.preflight(argv, dangerous_local=False)[1]))

    print("\n== env assignments are refused: the scrubbed environment is a stated guarantee ==")
    blocked, why = rc.preflight(["env", "SECRET=abc", "python3", "-c", "1"], False)
    check("an assignment is blocked", blocked is True, str(why))
    check("and the reason explains the scrubbed environment",
          "scrubbed" in (why or ""), str(why))
    check("while a plain `env ls` is not blocked",
          rc.preflight(["env", "ls"], False)[0] is False)

    print("\n== the confirmation gate ==")
    check("an allowlisted bare command is auto-approved",
          rc.needs_confirmation(["ls", "-la"]) is False)
    check("python3 stays auto-approved (a documented, accepted choice)",
          rc.needs_confirmation(["python3", "-c", "1"]) is False)
    check("an argv[0] with a path separator is NEVER auto-approved",
          rc.needs_confirmation(["/tmp/anything/ls"]) is True)
    check("including a relative path", rc.needs_confirmation(["./ls"]) is True)
    check("a wrapper is never auto-approved, even when the chain is allowlisted",
          rc.needs_confirmation(["env", "ls"]) is True)
    check("a non-allowlisted command still asks", rc.needs_confirmation(["nmap"]) is True)
    check("an empty argv asks rather than passing", rc.needs_confirmation([]) is True)

    print("\n== effective_binaries sees through the wrapper chain ==")
    for argv, expected_first_two in [
        (["env", "curl"], ["env", "curl"]),
        (["env", "-i", "FOO=1", "curl"], ["env", "curl"]),
        (["ls"], ["ls"]),
        (["nice", "ls"], ["nice", "ls"]),
    ]:
        got = rc.effective_binaries(argv)
        check(f"{argv} -> {expected_first_two}", got[:2] == expected_first_two, str(got))

    print("\n== the workspace-resident impostor does not inherit coreutils' allowlisting ==")
    ws = pathlib.Path(tempfile.mkdtemp(prefix="screen-ws-"))
    impostor = ws / "ls"
    impostor.write_text("#!/bin/sh\necho IMPOSTOR\n")
    impostor.chmod(0o755)
    check("it is not auto-approved", rc.needs_confirmation([str(impostor)]) is True)
    check("but it is not hard-blocked either — a human is asked, which is the right gate",
          rc.preflight([str(impostor)], False)[0] is False)

    print("\n== and the egress bypass no longer reaches the network ==")
    served: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            served.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):  # keep the test output clean
            return

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/probe"
        result = rc.run(["env", "curl", "-s", url], workspace_root=ws, isolation_tier="direct")
        check("the run is refused", result.get("blocked") is True, str(result)[:140])
        check("and nothing reached the server", served == [], str(served))
    finally:
        srv.shutdown()

    print("\n== the parent's memory is bounded by what it captures ==")
    import resource

    from ..sandbox.executor import DirectExecutor

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    res = DirectExecutor().run(
        ["python3", "-c",
         "import sys\nb='A'*65536\nfor _ in range(20000): sys.stdout.write(b)"],
        ws, ws, 30.0,
    )
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    produced = 65536 * 20000
    check("only the capture limit is kept, not the whole stream",
          len(res.stdout) < config.EXEC_CAPTURE_MAX_BYTES + 4096,
          f"{len(res.stdout)} bytes from a {produced}-byte stream")
    check("the parent's RSS does not follow the child's output",
          (after - before) < 200_000, f"grew {after - before} KiB")
    check("the truncation is disclosed rather than silent",
          "capture capped" in res.stdout, res.stdout[-120:])
    check("and the command still reports success", res.exit_code == 0 and res.timed_out is False,
          f"{res.exit_code}/{res.timed_out}")

    small = DirectExecutor().run(["python3", "-c", "print('ok')"], ws, ws, 15.0)
    check("a small command is unaffected", small.stdout.strip() == "ok" and small.exit_code == 0,
          repr(small.stdout))
    slow = DirectExecutor().run(["python3", "-c", "import time;time.sleep(30)"], ws, ws, 2.0)
    check("a timeout is still a timeout", slow.timed_out is True and slow.exit_code is None,
          f"{slow.timed_out}/{slow.exit_code}")
    check("and is attributed as one", slow.killed_reason == "timeout", str(slow.killed_reason))
    failing = DirectExecutor().run(
        ["python3", "-c", "import sys;sys.stderr.write('E');sys.exit(3)"], ws, ws, 15.0)
    check("stderr and a non-zero exit still come back",
          failing.stderr.strip() == "E" and failing.exit_code == 3,
          f"{failing.stderr!r}/{failing.exit_code}")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
