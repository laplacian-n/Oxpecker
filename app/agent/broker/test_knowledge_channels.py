"""Broker-mediated knowledge_search/knowledge_fetch — wiring M5.5's internet channels into the
same audit/evidence/rate-limit/taint-escalation path every other broker-mediated tool uses.
Run directly: `python3 -m agent.broker.test_knowledge_channels`.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from .. import config
from ..internet.dispatcher import ChannelResult
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
    print("== Denied by default when RoE doesn't allow the class ==")
    tmp = Path(tempfile.mkdtemp(prefix="knowledge-channels-test-"))
    eng_dir = _write_engagement(tmp, ["passive_recon"])
    broker = Broker(
        engagement_dir=eng_dir, confirm_fn=lambda p: True,
        approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
    )
    session_id = f"kc-test-{time.time_ns()}"

    req = ActionRequest(
        tool="knowledge_search", arguments={"query": "test"}, session_id=session_id, device_id="d",
    )
    resp = broker.dispatch(
        req, executor=lambda pol, args: {"channel": "knowledge_search"}
    )
    check(
        "knowledge_search denied when not in RoE allowed_action_classes",
        resp.status == "denied" and resp.policy_rule == "roe.allowed_action_classes",
        f"status={resp.status} rule={resp.policy_rule}",
    )

    print("\n== Allowed and dispatched when RoE explicitly permits it (mocked dispatcher) ==")
    eng_dir2 = _write_engagement(tmp, ["passive_recon", "knowledge_search", "knowledge_fetch"])
    broker2 = Broker(
        engagement_dir=eng_dir2, confirm_fn=lambda p: True,
        approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
    )

    fake_result = ChannelResult(
        channel="knowledge_search", ok=True, content="[]",
        provenance={"query": "test", "result_count": 0}, scan_verdict="clean",
    )
    with patch("agent.internet.dispatcher.knowledge_search", return_value=fake_result) as mock_search:
        req2 = ActionRequest(
            tool="knowledge_search", arguments={"query": "test"}, session_id=session_id, device_id="d",
        )
        from ..internet import dispatcher as internet_dispatcher

        resp2 = broker2.dispatch(
            req2,
            executor=lambda pol, args: {
                "channel": internet_dispatcher.knowledge_search(session_id, args["query"]).channel,
                "ok": True,
            },
        )
    check(
        "knowledge_search allowed and succeeds when RoE permits it",
        resp2.status == "succeeded", f"status={resp2.status} detail={resp2.detail}",
    )
    mock_search.assert_called_once()

    print("\n== Rate-limit cooldown applies to knowledge_search/knowledge_fetch ==")
    with patch("agent.internet.dispatcher.knowledge_search", return_value=fake_result):
        from ..internet import dispatcher as internet_dispatcher

        req3 = ActionRequest(
            tool="knowledge_search", arguments={"query": "second"}, session_id=session_id, device_id="d",
        )
        resp3 = broker2.dispatch(
            req3, executor=lambda pol, args: {"channel": internet_dispatcher.knowledge_search(session_id, args["query"]).channel}
        )
    check(
        "back-to-back knowledge_search call is rate-limited",
        resp3.status == "denied" and resp3.policy_rule == "rate_limit",
        f"status={resp3.status} rule={resp3.policy_rule}",
    )

    print("\n== Tainted session escalates knowledge_fetch to approval ==")
    session_id2 = f"kc-taint-test-{time.time_ns()}"
    TaintStore(session_id2).mark(reason="test", verdict="suspicious", source="http_recon")
    confirm_calls = []
    broker3 = Broker(
        engagement_dir=eng_dir2, confirm_fn=lambda p: (confirm_calls.append(p), True)[1],
        approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue"),
    )
    fake_fetch_result = ChannelResult(
        channel="knowledge_fetch", ok=True, content="hello",
        provenance={"url": "https://example.test/"}, scan_verdict="clean",
    )
    with patch("agent.internet.dispatcher.knowledge_fetch", return_value=fake_fetch_result):
        from ..internet import dispatcher as internet_dispatcher

        req4 = ActionRequest(
            tool="knowledge_fetch", arguments={"url": "https://example.test/"},
            session_id=session_id2, device_id="d",
        )
        resp4 = broker3.dispatch(
            req4, executor=lambda pol, args: {"channel": internet_dispatcher.knowledge_fetch(session_id2, args["url"]).channel}
        )
    check(
        "tainted knowledge_fetch requires approval",
        len(confirm_calls) == 1 and resp4.status == "succeeded",
        f"confirm_calls={len(confirm_calls)} status={resp4.status}",
    )

    print("\n== Approval queue records the decision (Phase 6 wiring) ==")
    recorded = broker3.approval_queue.list_pending(session_id=session_id2)
    check("no longer pending after synchronous resolution", recorded == [])
    all_records = [json.loads(p.read_text()) for p in (tmp / "approval_queue").glob("*.json")]
    matching = [r for r in all_records if r["session_id"] == session_id2 and r["tool"] == "knowledge_fetch"]
    check(
        "a resolved 'approved' record exists for this session/tool",
        len(matching) == 1 and matching[0]["status"] == "approved" and matching[0]["resolved_by"] == "cli-operator",
        f"matching={matching}",
    )

    print("\n== Real end-to-end against the actual default engagement (live SearXNG + real fetch) ==")
    try:
        import urllib.error
        import urllib.request

        urllib.request.urlopen("http://127.0.0.1:8888/search?q=test&format=json", timeout=3)
        from ..internet import dispatcher as internet_dispatcher

        real_broker = Broker(engagement_dir=config.ENGAGEMENT_DIR, confirm_fn=lambda p: True)
        real_session = f"kc-live-test-{time.time_ns()}"
        req5 = ActionRequest(
            tool="knowledge_search", arguments={"query": "OWASP top 10"},
            session_id=real_session, device_id="d",
        )
        resp5 = real_broker.dispatch(
            req5,
            executor=lambda pol, args: {
                "channel": internet_dispatcher.knowledge_search(real_session, args["query"]).channel
            },
        )
        check(
            "real default engagement (updated roe.json) permits and executes knowledge_search",
            resp5.status == "succeeded", f"status={resp5.status} detail={resp5.detail}",
        )
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print(f"  SKIPPED  live end-to-end check (SearXNG not reachable): {e}")

    print("\n== Async approval-queue mode (use_approval_queue=True) ==")
    import threading

    session_id3 = f"kc-async-test-{time.time_ns()}"
    TaintStore(session_id3).mark(reason="test", verdict="suspicious", source="http_recon")
    async_queue = ApprovalQueue(queue_dir=tmp / "approval_queue_async")
    async_broker = Broker(
        engagement_dir=eng_dir2, approval_queue=async_queue, use_approval_queue=True,
    )

    def _resolve_out_of_band():
        deadline = time.time() + 3
        while time.time() < deadline:
            pending = async_queue.list_pending(session_id=session_id3)
            if pending:
                async_queue.resolve(pending[0]["request_id"], approved=True, resolved_by="remote-operator")
                return
            time.sleep(0.05)

    resolver_thread = threading.Thread(target=_resolve_out_of_band)
    resolver_thread.start()
    with patch("agent.internet.dispatcher.knowledge_fetch", return_value=fake_fetch_result):
        from ..internet import dispatcher as internet_dispatcher

        req6 = ActionRequest(
            tool="knowledge_fetch", arguments={"url": "https://example.test/"},
            session_id=session_id3, device_id="d",
        )
        resp6 = async_broker.dispatch(
            req6, executor=lambda pol, args: {"channel": internet_dispatcher.knowledge_fetch(session_id3, args["url"]).channel}
        )
    resolver_thread.join()
    check(
        "async mode waits for and honors an out-of-band approval",
        resp6.status == "succeeded",
        f"status={resp6.status} detail={resp6.detail}",
    )

    print("\n== Async approval-queue mode times out and denies when nobody resolves it ==")
    session_id4 = f"kc-async-timeout-test-{time.time_ns()}"
    TaintStore(session_id4).mark(reason="test", verdict="suspicious", source="http_recon")
    timeout_broker = Broker(
        engagement_dir=eng_dir2, approval_queue=ApprovalQueue(queue_dir=tmp / "approval_queue_timeout"),
        use_approval_queue=True,
    )
    with patch("agent.config.APPROVAL_QUEUE_TIMEOUT_S", 0.3), \
         patch("agent.internet.dispatcher.knowledge_fetch", return_value=fake_fetch_result):
        from ..internet import dispatcher as internet_dispatcher

        req7 = ActionRequest(
            tool="knowledge_fetch", arguments={"url": "https://example.test/"},
            session_id=session_id4, device_id="d",
        )
        resp7 = timeout_broker.dispatch(
            req7, executor=lambda pol, args: {"channel": internet_dispatcher.knowledge_fetch(session_id4, args["url"]).channel}
        )
    check(
        "unresolved async approval times out and denies",
        resp7.status == "denied" and resp7.policy_rule == "approval_timeout",
        f"status={resp7.status} rule={resp7.policy_rule}",
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
