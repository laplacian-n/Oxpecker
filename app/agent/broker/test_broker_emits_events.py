"""The broker emits §4.2 events to the engagement log (A1: wiring the real producers into the
pipe step 0 built). A succeeded dispatch produces tool_call_started → tool_call_finished and an
artifact_stored for the stored evidence; a *denied* dispatch produces none of them, because a
denial never reached the executor. Both directions are asserted — an emitter that fired for
denials too would log actions that never ran, the inverse of the §6.3.1 signal the flow view
relies on.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.broker import broker as broker_mod
from agent.broker.contracts import ActionRequest
from agent.broker.policy import Policy
from agent.evidence.store import EvidenceStore
from agent.engagement.event_log import EngagementEventLog


class BrokerEmitsEventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="broker-emits-"))
        self._patches = [
            patch("agent.config.AUDIT_DIR", self.tmp / "audit"),
            patch("agent.config.EVIDENCE_DIR", self.tmp / "evidence"),
            patch("agent.config.EVIDENCE_KEY_PATH", self.tmp / "key.bin"),
            patch("agent.config.IDEMPOTENCY_CACHE_PATH", self.tmp / "idem.json"),
            patch("agent.config.ENGAGEMENTS_ROOT", self.tmp / "engagements"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _broker(self, allowed):
        policy = Policy(
            engagement_id="eng1", allowed_action_classes=set(allowed), allow_networks=[],
            allow_hostnames=set(), deny_networks=[], deny_hostnames=set(),
            policy_version="test", valid_until=float("inf"),
        )
        broker = broker_mod.Broker(policy_loader=lambda: policy, idempotency_cache_path=self.tmp / "idem.json")
        broker.evidence = EvidenceStore(
            evidence_dir=self.tmp / "evidence", index_path=self.tmp / "ei.json",
            access_log_path=self.tmp / "ea.jsonl", key_path=self.tmp / "key.bin",
        )
        return broker

    def _events(self, engagement_id="eng1"):
        log = EngagementEventLog.open_if_exists(self.tmp / "engagements" / engagement_id)
        return log.read_since(0) if log else []

    def _request(self, **kw):
        kw.setdefault("arguments", {"query": "x"})
        return ActionRequest(
            tool="knowledge_search", session_id="s1", device_id="d1",
            engagement_id="eng1", action_id="act1", idempotency_key="idem1", **kw,
        )

    def test_a_succeeded_dispatch_emits_started_finished_and_artifact(self):
        broker = self._broker(allowed={"knowledge_search"})
        resp = broker.dispatch(self._request(), executor=lambda p, a: {"results": ["hit"]})
        self.assertEqual(resp.status, "succeeded")

        events = self._events()
        by_kind = {e["kind"]: e["payload"] for e in events}
        self.assertIn("tool_call_started", by_kind)
        self.assertIn("tool_call_finished", by_kind)
        self.assertIn("artifact_stored", by_kind)
        # started/finished are the same call, and the artifact points at the stored evidence.
        self.assertEqual(by_kind["tool_call_started"]["call_id"], "act1")
        self.assertEqual(by_kind["tool_call_finished"]["call_id"], "act1")
        self.assertTrue(by_kind["tool_call_started"]["argument_digest"])  # a digest, not the args
        self.assertEqual(by_kind["artifact_stored"]["ref"], resp.evidence_digest)
        # Ordered: started before finished before artifact.
        kinds = [e["kind"] for e in events]
        self.assertLess(kinds.index("tool_call_started"), kinds.index("tool_call_finished"))

    def test_a_denied_dispatch_emits_nothing(self):
        # knowledge_search not in the allowed classes -> denied before the executor -> no tool
        # call ever started, so the stream must not claim one.
        broker = self._broker(allowed={"passive_recon"})
        resp = broker.dispatch(self._request(), executor=lambda p, a: {"results": ["hit"]})
        self.assertEqual(resp.status, "denied")
        self.assertEqual(self._events(), [])

    def test_the_digest_does_not_leak_the_arguments(self):
        broker = self._broker(allowed={"knowledge_search"})
        broker.dispatch(
            self._request(arguments={"query": "super-secret-payload"}),
            executor=lambda p, a: {"ok": True},
        )
        started = next(e for e in self._events() if e["kind"] == "tool_call_started")
        self.assertNotIn("super-secret-payload", started["payload"]["argument_digest"])


if __name__ == "__main__":
    unittest.main()
