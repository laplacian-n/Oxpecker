"""Tests that the web runtime's tool dispatch routes network actions through the governed
implementations rather than the shell.

The point being protected: `run_command` ran `subprocess.run(..., shell=True)` with no scope
check, no blast-radius cap, no kill switch and no record of what was probed, and a model asked to
scan reached for `nmap` there. Scanning is the job — the objection is to a scan that answers to
nothing. `port_discovery` enforces all four, so dispatch has to land there and the shell route
has to refuse rather than quietly work.

Needs fastapi/pydantic (dev_server imports them). Run directly:
`python3 -m agent.web.test_tools_wiring`.
"""
from __future__ import annotations

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    from . import dev_server as d

    def session(sid="t"):
        return d.Session(session_id=sid, engagement_id="lab-default")

    print("\n== port_discovery is advertised to the model, not just implemented ==")
    names = [t["function"]["name"] for t in d.TOOL_SCHEMAS]
    check("port_discovery appears in TOOL_SCHEMAS", "port_discovery" in names, str(names))

    print("\n== an in-scope scan runs and reports the IP it actually probed ==")
    r = d._run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9, 22]}, session())
    check("scan succeeds", r.get("ok") is True, str(r)[:160])
    check("the probed IP is reported, so a later audit entry can record it",
          r.get("validated_ip") == "127.0.0.1", str(r.get("validated_ip")))
    check("every requested port is accounted for",
          sorted(r.get("open_ports", []) + r.get("closed_ports", []) + r.get("filtered_ports", []))
          == [9, 22], str(r))

    print("\n== an out-of-scope scan is refused, and says it did not run ==")
    r = d._run_tool("port_discovery", {"host": "8.8.8.8", "ports": [53]}, session())
    check("refused", r.get("ok") is False, str(r)[:120])
    check("the refusal states nothing was sent",
          "NOT sent" in r.get("error", "") or "NOT run" in r.get("error", ""), str(r)[:160])

    print("\n== the aliases a model actually reaches for land on the governed tool ==")
    for alias in ("nmap", "scan", "port_scan", "portscan", "scan_ports"):
        r = d._run_tool(alias, {"host": "127.0.0.1", "ports": [9]}, session())
        check(f"{alias!r} dispatches to port_discovery", r.get("ok") is True, str(r)[:110])

    print("\n== the blast-radius cap holds ==")
    r = d._run_tool("port_discovery", {"host": "127.0.0.1", "ports": list(range(1, 5000))},
                    session())
    check("too many ports refused", r.get("ok") is False and "blast-radius" in r.get("error", ""),
          str(r)[:140])

    print("\n== malformed arguments are refused, not guessed ==")
    for args, expect in (({"ports": [80]}, "host is required"),
                         ({"host": "127.0.0.1"}, "at least one port"),
                         ({"host": "127.0.0.1", "ports": ["x"]}, "must be integers")):
        r = d._run_tool("port_discovery", args, session())
        check(f"{args} refused with a usable message",
              r.get("ok") is False and expect in r.get("error", ""), str(r)[:120])

    print("\n== shell scanning is steered to the governed tool, never silently run ==")
    for cmd in ("nmap -sV -p- 127.0.0.1", "nc -zv 127.0.0.1 22", "masscan -p1-65535 10.0.0.0/8",
                "Test-NetConnection -ComputerName x -Port 80"):
        r = d._run_tool("run_command", {"command": cmd}, session())
        check(f"steered: {cmd[:34]!r}",
              r.get("ok") is False and "port_discovery" in r.get("error", ""), str(r)[:120])

    print("\n== http fetching stays steered to http_request (pre-existing behaviour) ==")
    r = d._run_tool("run_command", {"command": "curl http://127.0.0.1:3000/"}, session())
    check("curl is refused", r.get("ok") is False, str(r)[:110])

    print("\n== cloud metadata is unreachable through the scan path too ==")
    eng = d._engagements["lab-default"]
    saved = list(eng.allow_targets)
    try:
        eng.allow_targets.append("169.254.0.0/16")
        r = d._run_tool("port_discovery", {"host": "169.254.169.254", "ports": [80]}, session())
        check("IMDS scan refused even with the range allowlisted",
              r.get("ok") is False, str(r)[:140])
    finally:
        eng.allow_targets[:] = saved

    print("\n== scope refusal reasons are shared with http_request, not reimplemented ==")
    a = d._run_tool("port_discovery", {"host": "8.8.8.8", "ports": [53]}, session())
    b = d._run_tool("http_request", {"url": "http://8.8.8.8/"}, session())
    check("both refuse the same out-of-scope host",
          a.get("ok") is False and b.get("ok") is False)
    check("both name the engagement the operator must change",
          "lab-default" in a.get("error", "") and "lab-default" in b.get("error", ""),
          f"{a.get('error','')[:70]} | {b.get('error','')[:70]}")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
