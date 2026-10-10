"""Autonomous auto-approval: an approval-required dispatch is granted without a human, but only
after every other gate, and the audit records it as an auto-approval, not a human one.

An unattended wave worker has no operator to answer the approval prompt and its security subprocess
cannot block on stdin — so a tainted or otherwise approval-gated action would hang the whole run.
auto_approve grants it instead, bounded by the gates that run before approval (scope, action class,
deny, kill switch)."""
from __future__ import annotations

import time
import unittest

from .broker import Broker
from .contracts import ActionRequest
from .taint import TaintStore


def _tainted_active_request():
    # A tainted session escalates any non-passive action to approval; port_discovery
    # (active_scan_light) on loopback is in the default lab engagement's allowlist.
    session_id = f"auto-approve-test-{time.time_ns()}"
    TaintStore(session_id).clear()
    TaintStore(session_id).mark(reason="test", verdict="suspicious", source="http_recon")
    req = ActionRequest(
        tool="port_discovery", arguments={"host": "127.0.0.1", "ports": [3000]},
        session_id=session_id, device_id="d",
    )
    return session_id, req


def _run(broker, req):
    from ..security_tools import port_discovery
    return broker.dispatch(
        req, executor=lambda pol, args: port_discovery.run(args["host"], args["ports"], pol))


class AutoApproveTest(unittest.TestCase):
    def test_auto_approve_grants_without_calling_confirm_fn(self):
        session_id, req = _tainted_active_request()
        try:
            confirm_calls = []
            broker = Broker(confirm_fn=lambda p: confirm_calls.append(p) or False,  # would DENY
                            auto_approve=True)
            resp = _run(broker, req)
            self.assertEqual(resp.status, "succeeded")
            self.assertEqual(confirm_calls, [],
                             "auto_approve must not fall through to the human confirm_fn")
        finally:
            TaintStore(session_id).clear()

    def test_without_auto_approve_a_declining_operator_blocks_it(self):
        session_id, req = _tainted_active_request()
        try:
            broker = Broker(confirm_fn=lambda p: False, auto_approve=False)  # operator declines
            resp = _run(broker, req)
            self.assertEqual(resp.status, "denied")
        finally:
            TaintStore(session_id).clear()

    def test_auto_approve_still_respects_scope(self):
        # An out-of-scope host is denied before approval is ever considered — auto_approve does not
        # widen the RoE, it only skips the human sign-off inside it.
        session_id = f"auto-approve-scope-{time.time_ns()}"
        TaintStore(session_id).clear()
        req = ActionRequest(
            tool="port_discovery", arguments={"host": "198.51.100.7", "ports": [80]},
            session_id=session_id, device_id="d",
        )
        broker = Broker(auto_approve=True)
        resp = _run(broker, req)
        self.assertEqual(resp.status, "denied")


if __name__ == "__main__":
    unittest.main()
