"""Broker action contract — §11.

The response is a typed union over `status`; every field the doc asks for is present so a
decision is reconstructable from the record alone (Phase-3 exit criterion), not from re-running
policy logic against current (possibly since-changed) state.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal[
    "succeeded", "denied", "needs_approval", "timed_out", "budget_exhausted", "cancelled", "failed"
]


@dataclass
class ActionRequest:
    tool: str
    arguments: dict[str, Any]
    session_id: str
    device_id: str
    engagement_id: str = "lab-default"
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: float = field(default_factory=time.time)
    deadline: float | None = None
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))
    approval_ref: str | None = None


@dataclass
class ActionResponse:
    action_id: str
    status: Status
    normalized_arguments: dict[str, Any]
    policy_rule: str
    policy_version: str
    runtime_identity: str
    started_at: float
    finished_at: float
    duration_ms: float
    exit_metadata: dict[str, Any]
    output: dict[str, Any]
    audit_record_digest: str | None = None
    evidence_digest: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "status": self.status,
            "normalized_arguments": self.normalized_arguments,
            "policy_rule": self.policy_rule,
            "policy_version": self.policy_version,
            "runtime_identity": self.runtime_identity,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "exit_metadata": self.exit_metadata,
            "output": self.output,
            "audit_record_digest": self.audit_record_digest,
            "evidence_digest": self.evidence_digest,
            "detail": self.detail,
        }
