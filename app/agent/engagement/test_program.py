"""Tests for the Program record and the three places it is enforced.

The point of this type is that a program's terms stop being prose in a prompt and become
something that refuses. So these tests are mostly about refusals actually happening, and about
the two ways a refusal can be fake: a restored engagement that lost its program but still
reports one, and a rate-limit window that counts requests which were never sent.

Run directly: `python3 -m agent.engagement.test_program`.
"""
from __future__ import annotations

import ipaddress
import pathlib
import tempfile
import time
from unittest.mock import patch

from .program import TARGET_TOUCHING_ACTION_CLASSES, Program

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    print("\n== validate() reports every problem at once ==")
    p = Program(platform="nope", max_requests_per_min=0)
    errs = p.validate()
    check("an unknown platform is reported", any("platform" in e for e in errs), str(errs))
    check("a zero rate cap is reported, since it would deny everything",
          any("max_requests_per_min" in e for e in errs), str(errs))
    check("both problems come back together, not one per round trip", len(errs) >= 2, str(errs))

    check("a bugcrowd program with no URL is rejected",
          any("program_url" in e for e in Program(platform="bugcrowd").validate()))
    check("a lab program needs no URL", Program(platform="lab").validate() == [])
    check("a null rate cap is legal (no program-specific limit)",
          Program(platform="lab", max_requests_per_min=None).validate() == [])
    check("prior_submissions must be objects",
          any("prior_submissions" in e
              for e in Program(platform="lab", prior_submissions=["oops"]).validate()))

    print("\n== excluded classes match in both directions ==")
    prog = Program(platform="lab", excluded_vuln_types=["XSS", "Self-XSS requiring interaction"])
    check("a program listing 'XSS' catches a specifically-named finding",
          prog.excludes("Reflected XSS in the search parameter") == "XSS")
    check("a specifically-worded exclusion is caught by a short finding class",
          prog.excludes("Self-XSS") is not None)
    check("case does not matter", prog.excludes("reflected xss") == "XSS")
    check("an unrelated class is not excluded", prog.excludes("SQL Injection") is None)
    check("an empty class excludes nothing", prog.excludes("") is None)
    check("an empty exclusion entry matches nothing",
          Program(platform="lab", excluded_vuln_types=["", "  "]).excludes("SQLi") is None)

    print("\n== duplicate risk is advisory and host-scoped ==")
    dup = Program(platform="lab", prior_submissions=[
        {"host": "www.example.com", "classes": ["IDOR"], "severity": "P2"},
        {"host": "api.example.com", "classes": ["SQL Injection"]},
        "not-an-object",
    ])
    check("a matching host and class is flagged",
          len(dup.duplicate_risk("www.example.com", "IDOR on /profile")) == 1)
    check("the same class on a different host is not flagged",
          dup.duplicate_risk("api.example.com", "IDOR") == [])
    check("host matching ignores case",
          len(dup.duplicate_risk("WWW.EXAMPLE.COM", "IDOR")) == 1)
    check("a malformed prior entry is skipped rather than crashing",
          isinstance(dup.duplicate_risk("www.example.com", "anything"), list))

    print("\n== a program survives a round trip, and absence stays absent ==")
    full = Program(platform="bugcrowd", program_url="https://x/y", automation_allowed=False,
                   max_requests_per_min=30, excluded_vuln_types=["XSS"], focus_areas=["db"],
                   vrt_taxonomy="bugcrowd-vrt", prior_submissions=[{"host": "a"}],
                   out_of_scope_refs=["oos.pdf"], safe_harbour="full")
    back = Program.from_dict(full.to_dict())
    check("to_dict/from_dict round-trips every field", back == full, f"{back}")
    check("None in means None out", Program.from_dict(None) is None)
    check("an empty dict means None too", Program.from_dict({}) is None)
    check("an unknown key does not blow up from_dict",
          Program.from_dict({"platform": "lab", "bogus": 1}).platform == "lab")

    print("\n== the broker refuses target-touching work when automation is forbidden ==")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="prog-test-"))
    with patch("agent.config.AUDIT_DIR", tmp / "audit"), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "k.bin"), \
         patch("agent.config.EVIDENCE_DIR", tmp / "ev"), \
         patch("agent.config.IDEMPOTENCY_CACHE_PATH", tmp / "idem.json"), \
         patch("agent.config.STATE_DIR", tmp / "state"):
        from ..broker import broker as broker_mod
        from ..broker.contracts import ActionRequest
        from ..broker.policy import Policy

        def pol(program, classes=None):
            return Policy(
                engagement_id="prog-eng",
                allowed_action_classes=classes or {
                    "passive_recon", "active_web_request", "active_scan_light",
                    "knowledge_search",
                },
                allow_networks=[ipaddress.ip_network("127.0.0.1/32")],
                allow_hostnames={"localhost"},
                deny_networks=[], deny_hostnames=set(),
                policy_version="test", valid_until=time.time() + 3600,
                program=program,
            )

        ran: list[str] = []

        def run(tool, program, session="s1", eng="prog-eng", args=None):
            b = broker_mod.Broker(policy_loader=lambda: pol(program),
                                  confirm_fn=lambda prompt: True)
            req = ActionRequest(tool=tool, arguments=args or {"host": "127.0.0.1", "ports": [9]},
                                session_id=session, device_id="t", engagement_id=eng)
            return b, b.dispatch(req, lambda policy, a: (ran.append(tool), {"ok": True})[1])

        no_auto = Program(platform="bugcrowd", program_url="https://x", automation_allowed=False)
        ran.clear()
        _, r = run("port_discovery", no_auto)
        check("an active scan is denied", r.status == "denied", r.status)
        check("the rule names the program's terms",
              r.policy_rule == "program.automation_forbidden", r.policy_rule)
        check("the reason tells the model nothing was sent", "Nothing was sent" in r.detail,
              r.detail[:90])
        check("and nothing ran", ran == [], str(ran))

        _, r = run("http_recon", no_auto, args={"url": "http://localhost/"})
        check("even passive recon is denied — an agent GET is still automated traffic",
              r.status == "denied" and r.policy_rule == "program.automation_forbidden",
              f"{r.status}/{r.policy_rule}")

        ran.clear()
        _, r = run("knowledge_search", no_auto, args={"query": "x"})
        check("searching our own index is unaffected", r.status == "succeeded",
              f"{r.status}: {r.detail}")
        check("knowledge_search is not in the target-touching set",
              "knowledge_search" not in TARGET_TOUCHING_ACTION_CLASSES)

        print("\n== an engagement with no program behaves exactly as before ==")
        ran.clear()
        _, r = run("port_discovery", None)
        check("no program means no program gate", r.status == "succeeded",
              f"{r.status}: {r.detail}")

        print("\n== the program's rate cap denies past the cap, and only counts what went out ==")
        capped = Program(platform="bugcrowd", program_url="https://x", max_requests_per_min=3)
        b = broker_mod.Broker(policy_loader=lambda: pol(capped), confirm_fn=lambda p: True)

        def dispatch(i, tool="port_discovery", eng="cap-eng"):
            return b.dispatch(
                ActionRequest(tool=tool, arguments={"host": "127.0.0.1", "ports": [9]},
                              session_id=f"s{i}", device_id="t", engagement_id=eng),
                lambda policy, a: {"ok": True},
            )

        # Different session ids so the per-action-class cooldown (which is per session) does not
        # mask the program-wide window being tested here.
        results = [dispatch(i) for i in range(4)]
        statuses = [r.status for r in results]
        check("the first three go through and the fourth is denied",
              statuses[:3] == ["succeeded"] * 3 and statuses[3] == "denied", str(statuses))
        check("the denial is attributed to the program's rate limit",
              results[3].policy_rule == "program.rate_limit", results[3].policy_rule)
        check("the reason says how long to wait", "allowed in" in results[3].detail,
              results[3].detail[:100])
        check("and warns that retrying is the wrong move",
              "IP ban" in results[3].detail, results[3].detail[:140])

        # A refused request must not consume budget, or a run that hit its RoE wall would also
        # burn the program's allowance.
        before = len(b._program_window["cap-eng"])
        b.dispatch(
            ActionRequest(tool="browser_fetch", arguments={"url": "http://localhost/"},
                          session_id="s9", device_id="t", engagement_id="cap-eng"),
            lambda policy, a: {"ok": True},
        )
        check("a request denied by the RoE does not consume the program's budget",
              len(b._program_window["cap-eng"]) == before,
              f"{before} -> {len(b._program_window['cap-eng'])}")

        check("a different engagement has its own budget",
              dispatch(20, eng="other-eng").status == "succeeded")

        # Slide the window by rewriting the recorded timestamps into the past, which is what
        # sixty seconds of wall-clock would do without making the test sleep for a minute.
        w = b._program_window["cap-eng"]
        for _ in range(len(w)):
            w.append(w.popleft() - 61.0)
        check("once the minute has passed the cap allows traffic again",
              dispatch(30).status == "succeeded")

        print("\n== the web runtime carries the program to the broker, and restores it as one ==")
        from ..web import dev_server as d
        from ..web import scope as web_scope

        eng = d.Engagement(engagement_id="web-prog", allow_targets=["*.example.com"],
                           allowed_action_classes=["active_web_request"],
                           authorized_by="op", program=no_auto)
        built = web_scope.policy_from_engagement(eng)
        check("the policy the broker reads carries the program",
              getattr(built, "program", None) is no_auto)
        check("and to_dict exposes it to the UI",
              (eng.to_dict().get("program") or {}).get("automation_allowed") is False,
              str(eng.to_dict().get("program")))

        restored = d._mk(d.Engagement, {"engagement_id": "r", "program": no_auto.to_dict()})
        check("_mk alone leaves program as a plain dict (the bug _load_state must fix)",
              isinstance(restored.program, dict), type(restored.program).__name__)
        restored.program = d._Program.from_dict(no_auto.to_dict())
        check("as a Program it answers the broker's question",
              restored.program.automation_allowed is False)
        check("as a dict it would have silently answered 'allowed'",
              getattr(no_auto.to_dict(), "automation_allowed", True) is True)

        print("\n== create_engagement validates the program ==")
        from fastapi import HTTPException
        try:
            d.create_engagement(d.CreateEngagementRequest(
                engagement_id="bad-prog", allow_targets=["127.0.0.1"], authorized_by="op",
                program={"platform": "bugcrowd"}))
            check("a bugcrowd program with no URL is rejected at the API", False, "accepted")
        except HTTPException as e:
            check("a bugcrowd program with no URL is rejected at the API", e.status_code == 400)
            check("and the error names the missing field", "program_url" in str(e.detail),
                  str(e.detail)[:120])
        d.create_engagement(d.CreateEngagementRequest(
            engagement_id="good-prog", allow_targets=["*.example.com"], authorized_by="op",
            program={"platform": "bugcrowd", "program_url": "https://bugcrowd.com/x",
                     "automation_allowed": False, "max_requests_per_min": 20}))
        stored = d._engagements["good-prog"].program
        check("a valid program is stored as a Program instance", isinstance(stored, d._Program))
        check("with its terms intact",
              stored.automation_allowed is False and stored.max_requests_per_min == 20)
        d._engagements.pop("good-prog", None)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
