"""The execution broker — §8's "highest-leverage single component." Every network-touching
tool call goes through Broker.dispatch(); the tool implementation never talks to the network
directly without a validated IP handed to it by the broker.

Policy is reloaded on every dispatch (cheap — three small files) rather than cached, so a live
edit to scope.txt/deny.txt or an expiring RoE takes effect on the very next call, and any
problem loading it (§8c/§12: "stale or missing policy fails closed") denies the action instead
of falling back to a permissive default.

Every dispatch — allowed or denied — produces exactly one audit record (via agent/audit_log.py,
same tamper-evident JSONL used since Phase 1) before returning, and the record's hash becomes
`audit_record_digest` on the response: "every action has a reconstructable decision" means the
decision must be looked up from the audit trail, not re-derived by re-running policy logic
against current (possibly since-changed) state.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Callable

from .. import audit_log as audit_log_mod
from .. import config
from ..evidence.store import EvidenceStore
from . import policy as policy_mod
from .approval_queue import ApprovalQueue
from .contracts import ActionRequest, ActionResponse
from .kill_switch import KillSwitch
from .taint import SAFE_WHILE_TAINTED, TaintStore

RUNTIME_IDENTITY = socket.gethostname()

# tool_name -> action_class. The two Phase-3 tools, plus M5.5's live internet channels
# (knowledge_search/knowledge_fetch reach the real internet, not the RoE-scoped target — kept as
# distinct action classes so an RoE can opt into one without the other, same granularity
# principle as http_recon vs http_recon_insecure below). Neither is in SAFE_WHILE_TAINTED
# (agent/broker/taint.py), so both correctly escalate to approval once a session is tainted.
TOOL_ACTION_CLASS = {
    "http_recon": "passive_recon",
    "port_discovery": "active_scan_light",
    "knowledge_search": "knowledge_search",
    "knowledge_fetch": "knowledge_fetch",
    # Its own action class, not folded into passive_recon/active_scan_light: full JS execution
    # against the target is a materially different capability than a bare HTTP GET or a TCP
    # connect scan, so an RoE must opt in explicitly rather than inheriting this from a broader
    # class it wasn't written with in mind.
    "browser_fetch": "browser_recon",
}

# tool_name -> action_class requiring human sign-off before execution, beyond just being an
# allowed action class. http_recon with verify_cert=False is handled dynamically (see
# _action_class_for) rather than listed here, since it depends on the request's arguments, not
# just its tool name.
REQUIRES_APPROVAL: set[str] = set()


def _action_class_for(request: ActionRequest) -> str | None:
    """M4.6: http_recon with verify_cert=False is a distinct, harder-gated action class —
    denied by default unless the RoE explicitly lists "http_recon_insecure" in
    allowed_action_classes (most engagements' RoE won't, by design: this needs a deliberate,
    documented opt-in, not just an approval click), and still requires approval even then."""
    if request.tool == "http_recon" and request.arguments.get("verify_cert") is False:
        return "http_recon_insecure"
    return TOOL_ACTION_CLASS.get(request.tool)


class Broker:
    def __init__(
        self,
        engagement_dir: Path = config.ENGAGEMENT_DIR,
        confirm_fn: Callable[[str], bool] | None = None,
        idempotency_cache_path: Path = config.IDEMPOTENCY_CACHE_PATH,
        approval_queue: ApprovalQueue | None = None,
        use_approval_queue: bool = False,
    ):
        self.engagement_dir = engagement_dir
        self.confirm_fn = confirm_fn or (lambda prompt: input(prompt).strip().lower() == "y")
        self.kill_switch = KillSwitch()
        self._last_action_at: dict[tuple[str, str], float] = {}  # (session_id, class) -> ts
        self._idempotency_path = idempotency_cache_path
        self._idempotency_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_logs: dict[str, audit_log_mod.AuditLog] = {}
        self.evidence = EvidenceStore()
        # Phase 6: every approval-required decision is now recorded in the durable approval
        # queue regardless of mode — a queryable history even when resolved synchronously via
        # confirm_fn(). use_approval_queue=True switches from blocking on confirm_fn() to
        # blocking on an out-of-band resolution (agent/broker/approval_queue.py), for a future
        # UI/multi-operator workflow — off by default, so the existing synchronous CLI approval
        # prompt remains unchanged behavior for every current caller.
        self.approval_queue = approval_queue if approval_queue is not None else ApprovalQueue()
        self.use_approval_queue = use_approval_queue

    def _audit_for(self, session_id: str) -> audit_log_mod.AuditLog:
        if session_id not in self._audit_logs:
            self._audit_logs[session_id] = audit_log_mod.AuditLog(session_id)
        return self._audit_logs[session_id]

    def _idempotency_cache(self) -> dict:
        if self._idempotency_path.exists():
            return json.loads(self._idempotency_path.read_text() or "{}")
        return {}

    def _save_idempotent(self, key: str, response: ActionResponse) -> None:
        cache = self._idempotency_cache()
        cache[key] = response.to_dict()
        self._idempotency_path.write_text(json.dumps(cache))

    def _finalize(self, request: ActionRequest, response: ActionResponse) -> ActionResponse:
        """Single exit path: store full raw output in the encrypted evidence store, write the
        (capped-excerpt) audit record, stamp both digests onto the response, cache it for
        idempotent replay. Every dispatch outcome (allowed or denied) goes through this — that's
        what makes every decision reconstructable from the audit trail alone, and every finding
        traceable back to its full, unmodified evidence (not just a 2000-char excerpt)."""
        raw_output_str = json.dumps(response.output)
        raw_output_bytes = raw_output_str.encode("utf-8", "replace")

        evidence_digest = self.evidence.put(
            raw_output_bytes,
            action_id=request.action_id,
            session_id=request.session_id,
            source=request.tool,
        )
        response.evidence_digest = evidence_digest

        entry = self._audit_for(request.session_id).record(
            turn_index=0,
            tool_name=request.tool,
            action_rationale=f"broker dispatch: {response.status} ({response.policy_rule})",
            arguments=request.arguments,
            raw_output=raw_output_str,  # audit_log.py itself caps the stored excerpt to 2000
            sanitized_output=response.detail[:2000],
            scope_decision=f"{response.status}:{response.policy_rule}",
            approval_identity=request.approval_ref,
            prompt_tokens=None,
            completion_tokens=None,
            latency_ms=response.duration_ms,
            exit_code=response.exit_metadata.get("exit_code"),
            injection_flagged=False,
        )
        response.audit_record_digest = entry["entry_hash"]
        # By construction, both digests are sha256 of the same raw_output bytes — a genuine
        # cross-reference a reader can verify independently, not just a matching-by-convention.
        assert entry["content_digest"] == evidence_digest, "audit/evidence digest mismatch"
        self._save_idempotent(request.idempotency_key, response)
        return response

    def dispatch(
        self, request: ActionRequest, executor: Callable[[policy_mod.Policy, dict], dict]
    ) -> ActionResponse:
        """executor(policy, arguments) -> raw tool result dict. The broker calls it only after
        every gate passes; scope-touching tools call scope_check.validate_target() themselves
        and raise PermissionError on denial (see security_tools/*.py)."""
        started_at = time.time()

        def deny(reason: str, rule: str, policy_version: str = "") -> ActionResponse:
            response = ActionResponse(
                action_id=request.action_id,
                status="denied",
                normalized_arguments=request.arguments,
                policy_rule=rule,
                policy_version=policy_version,
                runtime_identity=RUNTIME_IDENTITY,
                started_at=started_at,
                finished_at=time.time(),
                duration_ms=(time.time() - started_at) * 1000,
                exit_metadata={},
                output={},
                detail=reason,
            )
            return self._finalize(request, response)

        cache = self._idempotency_cache()
        if request.idempotency_key in cache:
            cached = dict(cache[request.idempotency_key])
            cached["detail"] = "[idempotent replay] " + cached.get("detail", "")
            return ActionResponse(**cached)

        if self.kill_switch.is_engaged():
            status = self.kill_switch.status()
            return deny(f"kill switch engaged: {status.get('reason', '')}", "kill_switch")

        try:
            policy = policy_mod.load_policy(self.engagement_dir)
        except policy_mod.PolicyError as e:
            return deny(f"policy load failed, failing closed: {e}", "policy_error")

        action_class = _action_class_for(request)
        if action_class is None:
            return deny(f"unknown tool: {request.tool}", "unknown_tool", policy.policy_version)
        if action_class not in policy.allowed_action_classes:
            return deny(
                f"action class {action_class!r} not permitted by current RoE",
                "roe.allowed_action_classes",
                policy.policy_version,
            )

        cooldown = config.ACTION_CLASS_COOLDOWN_S.get(action_class, 1.0)
        key = (request.session_id, action_class)
        last = self._last_action_at.get(key, 0.0)
        if time.time() - last < cooldown:
            return deny(
                f"rate limit: {action_class} actions are cooled down to 1 per {cooldown}s",
                "rate_limit",
                policy.policy_version,
            )

        # M4.5: a session that recently processed suspicious/malicious/unknown-scanned content
        # (injection_guard.scan(), triggered from loop.py on every tool result — see that
        # module's docstring) gets escalated to human approval for anything beyond passive
        # recon, for a bounded window — not a precise per-argument taint trace (not
        # observable), a deliberately conservative session-wide precaution instead.
        tainted, taint_info = TaintStore(request.session_id).is_tainted()
        taint_escalation = tainted and action_class not in SAFE_WHILE_TAINTED
        insecure_tls = action_class == "http_recon_insecure"
        if (
            request.tool in REQUIRES_APPROVAL or taint_escalation or insecure_tls
        ) and not request.approval_ref:
            note = f" [session tainted: {taint_info['reason']}]" if taint_escalation else ""
            if insecure_tls:
                note += " [TLS certificate verification disabled]"
            reason_for_queue = f"{request.tool}({request.arguments}){note}"
            request_id = self.approval_queue.submit(
                session_id=request.session_id, tool=request.tool,
                arguments=request.arguments, reason=reason_for_queue,
            )
            if self.use_approval_queue:
                try:
                    record = self.approval_queue.wait_for_resolution(
                        request_id, timeout_s=config.APPROVAL_QUEUE_TIMEOUT_S
                    )
                except TimeoutError:
                    return deny(
                        f"approval request timed out waiting in queue{note}",
                        "approval_timeout", policy.policy_version,
                    )
                approved = record["status"] == "approved"
                approval_ref = f"queue-approved-{request_id}"
            else:
                approved = self.confirm_fn(
                    f"\n[approval required{note}] {request.tool}({request.arguments})  [y/N] "
                )
                self.approval_queue.resolve(request_id, approved=approved, resolved_by="cli-operator")
                approval_ref = f"cli-approved-{time.time()}"
            if not approved:
                reason = "human declined approval" + note
                rule = "approval_required_taint" if taint_escalation else "approval_required"
                return deny(reason, rule, policy.policy_version)
            request.approval_ref = approval_ref

        self._last_action_at[key] = time.time()

        try:
            raw_result = executor(policy, request.arguments)
        except PermissionError as e:
            # scope_check-driven denial raised by the executor (target not in scope)
            return deny(str(e), "scope_check", policy.policy_version)
        except TimeoutError as e:
            response = ActionResponse(
                action_id=request.action_id,
                status="timed_out",
                normalized_arguments=request.arguments,
                policy_rule="n/a",
                policy_version=policy.policy_version,
                runtime_identity=RUNTIME_IDENTITY,
                started_at=started_at,
                finished_at=time.time(),
                duration_ms=(time.time() - started_at) * 1000,
                exit_metadata={},
                output={},
                detail=str(e),
            )
            return self._finalize(request, response)
        except Exception as e:  # noqa: BLE001 - surfaced as a typed failure, not a crash
            response = ActionResponse(
                action_id=request.action_id,
                status="failed",
                normalized_arguments=request.arguments,
                policy_rule="n/a",
                policy_version=policy.policy_version,
                runtime_identity=RUNTIME_IDENTITY,
                started_at=started_at,
                finished_at=time.time(),
                duration_ms=(time.time() - started_at) * 1000,
                exit_metadata={},
                output={},
                detail=f"{type(e).__name__}: {e}",
            )
            return self._finalize(request, response)

        finished_at = time.time()
        response = ActionResponse(
            action_id=request.action_id,
            status="succeeded",
            normalized_arguments=request.arguments,
            policy_rule=raw_result.pop("_policy_rule", "n/a"),
            policy_version=policy.policy_version,
            runtime_identity=RUNTIME_IDENTITY,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=(finished_at - started_at) * 1000,
            exit_metadata=raw_result.pop("_exit_metadata", {}),
            output=raw_result,
        )
        return self._finalize(request, response)
