"""Broker-mediated browser_fetch — wiring the isolated browser service (agent/browser/service.py)
into the same audit/evidence/rate-limit/taint-escalation path every other broker-mediated tool
uses. Run directly: `python3 -m agent.broker.test_browser_channel`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path

from .. import config
from .approval_queue import ApprovalQueue
from .broker import Broker
from .contracts import ActionRequest
from .taint import TaintStore

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _write_engagement(tmp: Path, allowed_action_classes: list[str]) -> Path:
    eng_dir = tmp / f"eng-{time.time_ns()}"
    eng_dir.mkdir()
    (eng_dir / "roe.json").write_text(json.dumps({
        "engagement_id": "test-eng",
        "allowed_action_classes": allowed_action_classes,
        "valid_from": "2020-01-01T00:00:00Z",
        "valid_until": "2099-01-01T00:00:00Z",
    }))
    (eng_dir / "scope.txt").write_text("127.0.0.1/32\n")
    (eng_dir / "deny.txt").write_text("")
    return eng_dir


def main() -> int:
    from ..security_mcp_server import _browser_fetch

    tmp = Path(tempfile.mkdtemp(prefix="browser-channel-test-"))

    print("== Denied by default when RoE doesn't allow browser_recon ==")
    eng_dir = _write_engagement(tmp, ["passive_recon"])
    broker = Broker(
        engagement_dir=eng_dir, confirm_fn=lambda p: True,
        approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
    )
    session_id = f"browser-test-{time.time_ns()}"
    req = ActionRequest(
        tool="browser_fetch", arguments={"url": "http://127.0.0.1:3000/"},
        session_id=session_id, device_id="d",
    )
    resp = broker.dispatch(req, executor=lambda pol, args: _browser_fetch(pol, args["url"]))
    check(
        "browser_fetch denied when not in RoE allowed_action_classes",
        resp.status == "denied" and resp.policy_rule == "roe.allowed_action_classes",
        f"status={resp.status} rule={resp.policy_rule}",
    )

    print("\n== Allowed and executes a real render against the real Juice Shop container ==")
    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen("http://127.0.0.1:3000/", timeout=3)
        eng_dir2 = _write_engagement(tmp, ["passive_recon", "browser_recon"])
        broker2 = Broker(
            engagement_dir=eng_dir2, confirm_fn=lambda p: True,
            approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
        )
        req2 = ActionRequest(
            tool="browser_fetch", arguments={"url": "http://127.0.0.1:3000/"},
            session_id=f"browser-test2-{time.time_ns()}", device_id="d",
        )
        resp2 = broker2.dispatch(req2, executor=lambda pol, args: _browser_fetch(pol, args["url"]))
        check(
            "browser_fetch allowed and renders the real page when RoE permits it",
            resp2.status == "succeeded" and resp2.output.get("ok") is True,
            f"status={resp2.status} detail={resp2.detail} output_ok={resp2.output.get('ok')}",
        )
        check(
            "rendered content actually came back (post-JS title present)",
            bool(resp2.output.get("title")),
            f"title={resp2.output.get('title')!r}",
        )
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print(f"  SKIPPED  live browser render check (Juice Shop not reachable): {e}")

    print("\n== Tainted session escalates browser_fetch to approval ==")
    eng_dir3 = _write_engagement(tmp, ["passive_recon", "browser_recon"])
    session_id3 = f"browser-taint-test-{time.time_ns()}"
    TaintStore(session_id3).mark(reason="test", verdict="suspicious", source="http_recon")
    confirm_calls = []
    broker3 = Broker(
        engagement_dir=eng_dir3, confirm_fn=lambda p: (confirm_calls.append(p), True)[1],
        approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
    )
    req3 = ActionRequest(
        tool="browser_fetch", arguments={"url": "http://127.0.0.1:3000/"},
        session_id=session_id3, device_id="d",
    )
    resp3 = broker3.dispatch(req3, executor=lambda pol, args: {"ok": True, "url": args["url"], "title": "stub", "content_excerpt": "", "content_truncated": False, "denied_requests": [], "error": None})
    check(
        "tainted browser_fetch requires approval",
        len(confirm_calls) == 1 and resp3.status == "succeeded",
        f"confirm_calls={len(confirm_calls)} status={resp3.status}",
    )

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
