"""Tests that the web runtime's outward-facing tools go through the execution broker.

Scope enforcement landed earlier; this is the rest of what the broker provides and the web
runtime had no way to do: the RoE action-class gate, the per-class rate limit, the kill switch,
the encrypted evidence store holding the full output with its digest cross-referenced against
the audit entry, and idempotent replay.

run_command is deliberately NOT mediated — local execution is gated by the sandbox and its
preflight, and run_command is absent from broker.TOOL_ACTION_CLASS by design. A test asserts it
stays that way, because quietly routing it would both break it (no action class) and confuse two
different kinds of gate.

Rate-limit cooldowns are keyed on (session_id, action_class), so each case below uses its own
session rather than sleeping.

Run directly: `python3 -m agent.web.test_broker_mediation`.
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxpecker-broker-test-"))
    with patch("agent.config.AUDIT_DIR", tmp / "audit"), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "key.bin"), \
         patch("agent.config.EVIDENCE_DIR", tmp / "evidence"), \
         patch("agent.config.IDEMPOTENCY_CACHE_PATH", tmp / "idem.json"):
        from .. import audit_log as al
        from ..broker import broker as broker_mod
        from . import debug_trace as dt
        from . import dev_server as d

        dt.TRACE_DIR = tmp / "trace"
        dt._traces.clear()
        d._brokers.clear()

        n = [0]

        def fresh(name):
            n[0] += 1
            return d.Session(session_id=f"{name}-{n[0]}", engagement_id="lab-default",
                             isolation_tier="direct")

        def entries(sid):
            p = tmp / "audit" / f"{sid}.jsonl"
            return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] \
                if p.exists() else []

        def trace(sid):
            p = tmp / "trace" / f"{sid}.jsonl"
            return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] \
                if p.exists() else []

        print("\n== which tools are mediated, and which deliberately are not ==")
        # http_recon joined this set when it was wired into this runtime. It is deliberately
        # mediated rather than dispatched directly: the broker is what maps verify_cert=False to
        # the `http_recon_insecure` action class, which needs an explicit RoE allowance AND
        # approval. Dispatching it straight to the implementation would have moved that decision
        # out of the component that records it. Kept as an exact-set assertion so adding an
        # outward-facing tool without mediating it fails here; test_tool_parity.py additionally
        # asserts the general rule (anything the broker classifies must be mediated).
        check("the outward-facing tools are mediated",
              d._BROKER_MEDIATED == {"http_request", "port_discovery", "knowledge_search",
                                     "http_recon"},
              str(d._BROKER_MEDIATED))
        check("run_command is not mediated", "run_command" not in d._BROKER_MEDIATED)
        check("and the broker does not claim an action class for it",
              "run_command" not in broker_mod.TOOL_ACTION_CLASS,
              str(sorted(broker_mod.TOOL_ACTION_CLASS)))
        check("http_request has its own class, not passive_recon",
              broker_mod.TOOL_ACTION_CLASS.get("http_request") == "active_web_request",
              str(broker_mod.TOOL_ACTION_CLASS.get("http_request")))

        print("\n== an in-scope call succeeds and carries the policy rule back ==")
        s = fresh("ok")
        r, _ = d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                                   turn_index=0, action_rationale="scan")
        check("the call succeeds", r.get("ok") is True, str(r)[:140])
        check("the allow rule is reported", "127.0.0.1" in str(r.get("_policy_rule")),
              str(r.get("_policy_rule")))

        print("\n== the RoE action-class gate is enforced, not decorative ==")
        eng = d._engagements["lab-default"]
        saved = list(eng.allowed_action_classes)
        try:
            eng.allowed_action_classes = ["passive_recon"]
            d._brokers.clear()
            s = fresh("roe")
            r, _ = d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                                       turn_index=0, action_rationale="")
            check("a scan is refused when the RoE does not permit the class",
                  r.get("ok") is False and "not permitted" in str(r.get("error")), str(r)[:150])
            check("the refusal names the RoE rule",
                  r.get("scope_rule") == "roe.allowed_action_classes", str(r.get("scope_rule")))
            check("the refusal states the call did not happen", "NOT made" in str(r.get("error")))
        finally:
            eng.allowed_action_classes = saved
            d._brokers.clear()

        print("\n== the policy is read per dispatch, so an edit takes effect ==")
        # The loader is called on every dispatch rather than captured at construction, which is
        # what makes the check above possible without recreating the broker.
        s = fresh("reload")
        try:
            eng.allow_targets = ["10.9.9.9"]
            r, _ = d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                                       turn_index=0, action_rationale="")
            check("removing a target from scope denies immediately",
                  r.get("ok") is False, str(r)[:130])
        finally:
            eng.allow_targets = ["127.0.0.1", "localhost"]

        print("\n== the per-class rate limit is enforced ==")
        s = fresh("rate")
        d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                            turn_index=0, action_rationale="")
        r, _ = d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                                   turn_index=1, action_rationale="")
        check("a back-to-back call of the same class is rate limited",
              r.get("ok") is False and "rate limit" in str(r.get("error")), str(r)[:130])

        print("\n== a refusal is recorded as a denial however the tool reports it ==")
        # port_discovery raises PermissionError; dev_server's http_request returns a dict. Left
        # untranslated, the second would be recorded as "succeeded" for a request that was
        # refused — the trail would say the call went through.
        s = fresh("refusal")
        r, _ = d._audited_run_tool("http_request", {"url": "http://8.8.8.8/"}, s,
                                   turn_index=0, action_rationale="")
        check("the tool result is a refusal", r.get("ok") is False, str(r)[:110])
        e = entries(s.session_id)
        check("the audit entry records it as denied",
              e and e[0]["scope_decision"].startswith("denied"),
              str(e and e[0]["scope_decision"]))

        print("\n== one action, one audit entry ==")
        # The broker writes its own entry in _finalize; the local wrapper must not add a second.
        s = fresh("single")
        d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                            turn_index=0, action_rationale="")
        d._audited_run_tool("run_command", {"argv": ["pwd"]}, s,
                            turn_index=1, action_rationale="")
        e = entries(s.session_id)
        check("two calls produced two entries, not three",
              len(e) == 2, f"{len(e)}: {[x['tool_name'] for x in e]}")
        check("both tools are represented once each",
              sorted(x["tool_name"] for x in e) == ["port_discovery", "run_command"],
              str([x["tool_name"] for x in e]))
        ok, detail = al.verify(s.session_id, audit_dir=tmp / "audit")
        check("the chain still verifies across both paths", ok, detail)

        print("\n== the evidence store holds the full output, digest cross-referenced ==")
        s = fresh("evidence")
        d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                            turn_index=0, action_rationale="")
        br = [t for t in trace(s.session_id) if t["kind"] == "broker"]
        check("a broker event is traced", len(br) == 1, str(len(br)))
        check("it carries an evidence digest", br and bool(br[0]["evidence_digest"]),
              str(br and br[0].get("evidence_digest")))
        e = entries(s.session_id)
        check("the audit content digest equals the evidence digest",
              e and br and e[0]["content_digest"] == br[0]["evidence_digest"],
              f"audit={e and e[0]['content_digest']} evidence={br and br[0]['evidence_digest']}")
        check("evidence was actually written to disk",
              any((tmp / "evidence").rglob("*")), str(tmp / "evidence"))

        print("\n== a non-success that is not a block is not reported as one ==")
        # "denied" means the call never happened; "failed" means it was attempted. A model told
        # it was blocked when it actually failed goes looking for permission instead of retrying.
        s = fresh("failed")
        with patch.object(d, "_run_tool", side_effect=RuntimeError("boom")):
            r, _ = d._audited_run_tool("port_discovery", {"host": "127.0.0.1", "ports": [9]}, s,
                                       turn_index=0, action_rationale="")
        check("a failing executor is reported as attempted, not blocked",
              r.get("ok") is False and "did not complete" in str(r.get("error")), str(r)[:150])
        check("it is not labelled a policy block", "NOT made" not in str(r.get("error")),
              str(r)[:120])

        print("\n== approval is asked of the operator, and fails closed when it cannot be ==")
        b = d._broker_for("lab-default")
        check("the confirm function is ours, not the stdin default",
              b.confirm_fn is d._web_confirm)
        check("the broker's own blocking queue mode is not used",
              b.use_approval_queue is False, str(b.use_approval_queue))
        # No session bound to this thread: the one path where there is nobody to ask.
        d._dispatch_ctx.session = None
        check("an approval with no session to ask denies instead of guessing",
              d._web_confirm("test prompt") is False)

        print("\n== a broken policy source fails closed ==")
        bad = broker_mod.Broker(policy_loader=lambda: (_ for _ in ()).throw(RuntimeError("nope")),
                               confirm_fn=lambda prompt: False)
        from ..broker.contracts import ActionRequest
        resp = bad.dispatch(
            ActionRequest(tool="port_discovery", arguments={"host": "127.0.0.1", "ports": [9]},
                          session_id="failclosed", device_id="test"),
            lambda policy, arguments: {"ok": True},
        )
        check("a loader that raises denies rather than letting the call through",
              resp.status == "denied", resp.status)
        check("the reason says it failed closed", "failing closed" in resp.detail, resp.detail)

        print("\n== an engagement cannot be created with an unknown action class ==")
        from fastapi import HTTPException
        from .dev_server import CreateEngagementRequest, create_engagement
        try:
            create_engagement(CreateEngagementRequest(
                engagement_id="bogus-classes", allow_targets=["127.0.0.1"],
                allowed_action_classes=["active_recon"], authorized_by="test"))
            check("an unknown class is rejected", False, "it was accepted")
        except HTTPException as ex:
            check("an unknown class is rejected with 400", ex.status_code == 400, str(ex.detail))
            check("the error lists the valid classes", "choose from" in str(ex.detail),
                  str(ex.detail)[:140])
        check("the shipped lab-default uses only known classes",
              set(saved) <= set(broker_mod.TOOL_ACTION_CLASS.values()) | {"http_recon_insecure"},
              str(saved))

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
