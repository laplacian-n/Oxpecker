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

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable

from .. import audit_log as audit_log_mod
from .. import config
from ..engagement import emit as event_emit
from ..evidence.store import EvidenceStore
from . import policy as policy_mod
from .approval_queue import ApprovalQueue
from .contracts import ActionRequest, ActionResponse
from .kill_switch import KillSwitch
from .taint import SAFE_WHILE_TAINTED, TaintStore
from ..engagement.program import TARGET_TOUCHING_ACTION_CLASSES

RUNTIME_IDENTITY = socket.gethostname()

# tool_name -> action_class. The two Phase-3 tools, plus M5.5's live internet channels
# (knowledge_search/knowledge_fetch reach the real internet, not the RoE-scoped target — kept as
# distinct action classes so an RoE can opt into one without the other, same granularity
# principle as http_recon vs http_recon_insecure below). Neither is in SAFE_WHILE_TAINTED
# (agent/broker/taint.py), so both correctly escalate to approval once a session is tainted.
TOOL_ACTION_CLASS = {
    "http_recon": "passive_recon",
    # Its own class rather than folded into passive_recon: http_recon issues GET only and is
    # read-only by construction, while http_request carries arbitrary methods and bodies — it is
    # how a payload reaches a target. An RoE that permits passive reconnaissance has not thereby
    # permitted sending one.
    "http_request": "active_web_request",
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

# The idempotency cache is rewritten whole on every dispatch, so it is bounded rather than
# allowed to grow for the life of an install.
IDEMPOTENCY_CACHE_MAX_ENTRIES = 500


# Strings a tool call can plausibly carry for "off". `http_recon` consumes verify_cert with a
# plain truthiness test, so `0` and `""` already disable verification; these spellings are
# included because a model emitting JSON-ish arguments produces them and a gate that reads them
# as "verification on" would under-classify the action.
_FALSEY_STRINGS = frozenset({"false", "0", "no", "off", "none", "null", ""})


def _verification_disabled(arguments: dict) -> bool:
    """True when this request would run with TLS verification off.

    `arguments.get("verify_cert") is False` only caught the singleton False. Anything else
    falsy — `0`, `""`, `None` — disables verification in `http_recon` (`if self._verify_cert:`)
    while the broker classed the action as ordinary `passive_recon`: no `http_recon_insecure`
    opt-in in the RoE, no approval, and an audit entry that does not say verification was off.
    """
    if "verify_cert" not in arguments:
        return False
    value = arguments["verify_cert"]
    if isinstance(value, str):
        return value.strip().lower() in _FALSEY_STRINGS
    return not value


def _argument_digest(arguments: dict) -> str:
    """A short, stable digest of a tool call's arguments for the event stream (§4.2's
    "argument digest"). A hash, not the arguments themselves — so the flow view can show that two
    calls differ without the stream carrying secrets or payloads (those live in the evidence store,
    reachable by ref)."""
    try:
        canonical = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(arguments)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:12]


def _action_class_for(request: ActionRequest) -> str | None:
    """M4.6: http_recon with verify_cert=False is a distinct, harder-gated action class —
    denied by default unless the RoE explicitly lists "http_recon_insecure" in
    allowed_action_classes (most engagements' RoE won't, by design: this needs a deliberate,
    documented opt-in, not just an approval click), and still requires approval even then."""
    if request.tool == "http_recon" and _verification_disabled(request.arguments):
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
        policy_loader: Callable[[], policy_mod.Policy] | None = None,
    ):
        self.engagement_dir = engagement_dir
        # Where the policy comes from. The default reads roe.json/scope.txt/deny.txt from
        # `engagement_dir`, which is how every CLI caller works. A caller that holds its
        # engagement somewhere other than on disk — the web runtime keeps them in memory —
        # supplies its own loader instead of being forced to materialise files it does not
        # otherwise need. It is only a source: every gate below is applied identically either
        # way, and a loader that raises PolicyError still fails closed.
        self._policy_loader = policy_loader or (lambda: policy_mod.load_policy(self.engagement_dir))
        self.confirm_fn = confirm_fn or (lambda prompt: input(prompt).strip().lower() == "y")
        self.kill_switch = KillSwitch()
        self._last_action_at: dict[tuple[str, str], float] = {}  # (session_id, class) -> ts
        # Guards _last_action_at across the read-decide-claim sequence in the cooldown gate.
        # Held only for that bookkeeping, never across the wait itself, so one session parked
        # in a cooldown does not stall every other session's dispatch.
        self._cooldown_lock = threading.Lock()
        # Timestamps of target-touching dispatches that were allowed through, per engagement.
        # Per ENGAGEMENT and not per session on purpose: a program's request cap applies to its
        # assets, and two sessions on one engagement hitting the same host at half the cap each
        # is the same traffic the program asked us not to send.
        self._program_window: dict[str, deque] = defaultdict(deque)
        self._idempotency_lock = threading.Lock()
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
        with self._idempotency_lock:
            self._save_idempotent_locked(key, response)

    def _save_idempotent_locked(self, key: str, response: ActionResponse) -> None:
        # Held across the read-modify-write: without it two dispatches both read the cache,
        # both add their own entry, and the second write loses the first. The lock is per
        # Broker, which covers the concurrency this process has; two processes sharing the file
        # still interleave, but the atomic replace means the loser loses an entry rather than
        # corrupting the file, and a lost idempotency entry degrades to "replay not available".
        cache = self._idempotency_cache()
        cache[key] = response.to_dict()
        # Bounded, and pruned oldest-first. This file is rewritten in full on every dispatch, so
        # an unpruned cache costs a growing serialization on each call as well as unbounded disk
        # — and because ActionRequest mints a fresh idempotency_key per request unless the caller
        # supplies one, most entries are single-use and will never be read back. Harmless while
        # only the CLI dispatched; the web runtime now dispatches on every tool call.
        if len(cache) > IDEMPOTENCY_CACHE_MAX_ENTRIES:
            # dicts keep insertion order, so the head is the oldest.
            for stale in list(cache)[: len(cache) - IDEMPOTENCY_CACHE_MAX_ENTRIES]:
                cache.pop(stale, None)
        # The temp name carries the pid AND a nonce. A single shared `.tmp` name meant two
        # concurrent dispatches both wrote the same file, the first `replace()` moved it away,
        # and the second raised FileNotFoundError — from inside `_finalize`, i.e. AFTER the
        # action had run and after its audit entry was appended. The caller saw an exception
        # ("it did not happen") for traffic that was sent and is recorded as succeeded. One
        # Broker serves concurrent FastAPI requests per engagement, so this was reachable.
        tmp = self._idempotency_path.with_name(
            f"{self._idempotency_path.name}.tmp{os.getpid()}.{uuid.uuid4().hex[:8]}"
        )
        try:
            tmp.write_text(json.dumps(cache))
            os.replace(tmp, self._idempotency_path)  # atomic: a reader never sees a partial file
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — the replace normally consumed it
                pass

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
            turn_index=request.turn_index,
            tool_name=request.tool,
            # The caller's reason when it has one — the model's own account of why it acted is
            # the part a reader actually needs, and the broker's verdict is already recorded in
            # scope_decision below, so nothing is lost by preferring it.
            action_rationale=(
                request.action_rationale
                or f"broker dispatch: {response.status} ({response.policy_rule})"
            ),
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
        response.audit_entry_id = entry["entry_id"]
        # By construction, both digests are sha256 of the same raw_output bytes — a genuine
        # cross-reference a reader can verify independently, not just a matching-by-convention.
        assert entry["content_digest"] == evidence_digest, "audit/evidence digest mismatch"
        self._save_idempotent(request.idempotency_key, response)

        # §4.2 telemetry, after the evidence and audit are safely written (never before — a
        # stream event must not claim an action the record does not). tool_call_finished closes
        # only the calls that actually ran (started above); a denial/approval-block never started,
        # so it is not finished here. artifact_stored fires only on a real stored output, which is
        # what lets the flow view draw where a worker's output went (§6.3.1).
        if response.status in ("succeeded", "failed", "timed_out", "cancelled"):
            event_emit.emit(
                request.engagement_id,
                "tool_call_finished",
                {"call_id": request.action_id, "worker": request.session_id, "tool": request.tool,
                 "latency": response.duration_ms, "status": response.status},
            )
        if response.status == "succeeded" and response.output:
            event_emit.emit(
                request.engagement_id,
                "artifact_stored",
                {"worker": request.session_id, "artifact": request.tool, "store": "evidence",
                 "ref": evidence_digest},
            )
        return response

    def _wait_out_cooldown(self, wait_s: float) -> bool:
        """Block for `wait_s`, re-checking the kill switch every COOLDOWN_WAIT_POLL_S.

        Returns True if the wait completed, False if the kill switch was engaged part way
        through and the caller should deny instead. A single sleep() would have made the
        switch's termination time depend on the longest cooldown in the table; polling keeps
        it at one slice, which is what kill_switch.py's docstring promises callers."""
        deadline = time.time() + wait_s
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            if self.kill_switch.is_engaged():
                return False
            time.sleep(min(config.COOLDOWN_WAIT_POLL_S, remaining))

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
            # `is_engaged()` is an existence check and `status()` re-reads the file, so an
            # operator disengaging between the two calls made `status` None and
            # `status.get(...)` raise an AttributeError — leaving the caller with no
            # ActionResponse and the trail with no entry, instead of a denial. Deny either way:
            # the switch was engaged when we looked, and that is the safe reading.
            status = self.kill_switch.status() or {}
            return deny(f"kill switch engaged: {status.get('reason', '')}", "kill_switch")

        try:
            policy = self._policy_loader()
        except policy_mod.PolicyError as e:
            return deny(f"policy load failed, failing closed: {e}", "policy_error")
        except Exception as e:  # a custom loader must not be able to bypass the gate by raising
            return deny(f"policy loader failed, failing closed: {e}", "policy_error")

        action_class = _action_class_for(request)
        if action_class is None:
            return deny(f"unknown tool: {request.tool}", "unknown_tool", policy.policy_version)
        if action_class not in policy.allowed_action_classes:
            return deny(
                f"action class {action_class!r} not permitted by current RoE",
                "roe.allowed_action_classes",
                policy.policy_version,
            )

        # The program's own terms, where the engagement carries them. Checked after the RoE
        # gate so a class that was never permitted does not consume the program's budget, and
        # before execution so a refusal means nothing was sent.
        program = getattr(policy, "program", None)
        touches_target = action_class in TARGET_TOUCHING_ACTION_CLASSES
        if program is not None and touches_target:
            if not getattr(program, "automation_allowed", True):
                return deny(
                    f"this program forbids automated testing, and {action_class!r} puts traffic "
                    f"on its assets. Nothing was sent. Testing it needs a human driving, or the "
                    f"program's written permission recorded on the engagement.",
                    "program.automation_forbidden",
                    policy.policy_version,
                )
            cap = getattr(program, "max_requests_per_min", None)
            if cap:
                window = self._program_window[request.engagement_id or policy.engagement_id]
                now = time.time()
                while window and now - window[0] >= 60.0:
                    window.popleft()
                if len(window) >= cap:
                    wait_s = 60.0 - (now - window[0])
                    return deny(
                        f"this program's rate limit is {cap} requests/min and "
                        f"{len(window)} have gone out in the last minute; the next is allowed in "
                        f"{wait_s:.0f}s. Nothing was sent. Pace the hunt rather than retrying: "
                        f"an IP ban costs the whole engagement, not one request.",
                        "program.rate_limit",
                        policy.policy_version,
                    )

        # Our own pacing, not the program's (that is `program.rate_limit` above, which still
        # denies because its waits run to tens of seconds and the model genuinely should go do
        # something else). These are a couple of seconds and we chose them ourselves, so the
        # broker waits them out in place rather than spending a model round trip to say "later".
        # The wait is interruptible: an operator's kill switch is seen within one poll slice.
        cooldown = config.ACTION_CLASS_COOLDOWN_S.get(action_class, 1.0)
        key = (request.session_id, action_class)
        with self._cooldown_lock:
            last = self._last_action_at.get(key, 0.0)
            wait_s = cooldown - (time.time() - last)
            if wait_s > config.ACTION_CLASS_COOLDOWN_MAX_WAIT_S:
                return deny(
                    f"rate limit: {action_class} actions are paced to 1 per {cooldown}s and the "
                    f"next is allowed in {wait_s:.1f}s, which is longer than this broker will "
                    f"block for. Nothing was sent. Do something else and come back to it.",
                    "rate_limit",
                    policy.policy_version,
                )
            if wait_s > 0:
                # Claim the slot before releasing the lock. Without this, two dispatches that
                # arrive together both compute the same deadline, both sleep to it, and both
                # fire at once — which is the thing the cooldown exists to prevent. Claiming
                # makes a concurrent dispatch queue behind this one instead. A request that a
                # later gate denies therefore consumes a slot it never used, which errs toward
                # less traffic and is the safe direction to err in.
                self._last_action_at[key] = last + cooldown
        if wait_s > 0 and not self._wait_out_cooldown(wait_s):
            status = self.kill_switch.status() or {}
            return deny(
                f"kill switch engaged while waiting out the {action_class} cooldown: "
                f"{status.get('reason', '')}",
                "kill_switch",
                policy.policy_version,
            )

        # M4.5: a session that recently processed suspicious/malicious/unknown-scanned content
        # (injection_guard.scan(), triggered from loop.py on every tool result — see that
        # module's docstring) gets escalated to human approval for anything beyond passive
        # recon, for a bounded window — not a precise per-argument taint trace (not
        # observable), a deliberately conservative session-wide precaution instead.
        # engagement_id passed so the check also sees an engagement-scoped taint (§14.1 C): a
        # sibling worker that tainted the shared browser profile escalates this session too, even
        # though this session's own record is clean.
        tainted, taint_info = TaintStore(
            request.session_id, engagement_id=request.engagement_id
        ).is_tainted()
        taint_escalation = tainted and action_class not in SAFE_WHILE_TAINTED
        insecure_tls = action_class == "http_recon_insecure"
        if (
            request.tool in REQUIRES_APPROVAL or taint_escalation or insecure_tls
        ) and not request.approval_ref:
            reason_text = (taint_info or {}).get("reason", "unknown reason")
            note = f" [session tainted: {reason_text}]" if taint_escalation else ""
            if insecure_tls:
                note += " [TLS certificate verification disabled]"
            reason_for_queue = f"{request.tool}({request.arguments}){note}"
            request_id = self.approval_queue.submit(
                session_id=request.session_id, tool=request.tool,
                arguments=request.arguments, reason=reason_for_queue,
            )
            # §4.2 approval_required — surfaces in chat, the approvals drawer and (eventually) the
            # flow node (§7). The raw arguments are NOT put on the stream (they may carry secrets);
            # the tool, a non-sensitive note and the same argument digest the tool_call events use
            # are enough to identify the request. blocked_workers is empty until the wave model
            # knows what a request blocks.
            event_emit.emit(
                request.engagement_id,
                "approval_required",
                {"request_id": request_id, "tool": request.tool, "worker": request.session_id,
                 "argument_digest": _argument_digest(request.arguments), "note": note.strip(),
                 "blocked_workers": []},
            )
            if self.use_approval_queue:
                try:
                    record = self.approval_queue.wait_for_resolution(
                        request_id, timeout_s=config.APPROVAL_QUEUE_TIMEOUT_S
                    )
                except TimeoutError:
                    event_emit.emit(request.engagement_id, "approval_resolved",
                                    {"request_id": request_id, "status": "timeout"})
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
            event_emit.emit(request.engagement_id, "approval_resolved",
                            {"request_id": request_id, "status": "approved" if approved else "denied"})
            if not approved:
                reason = "human declined approval" + note
                rule = "approval_required_taint" if taint_escalation else "approval_required"
                return deny(reason, rule, policy.policy_version)
            request.approval_ref = approval_ref

        self._last_action_at[key] = time.time()
        if program is not None and touches_target and getattr(program, "max_requests_per_min", None):
            # Recorded here, past every gate including approval, so the window counts requests
            # that actually went out rather than ones that were refused.
            self._program_window[request.engagement_id or policy.engagement_id].append(time.time())

        # §4.2 tool_call_started: emitted here, past every gate and right before the tool runs,
        # so the stream marks calls that actually went out (a denied or approval-blocked request
        # never reaches this line, and so never pairs a spurious started/finished). call_id is the
        # action_id, which tool_call_finished in _finalize carries back to close the pair.
        event_emit.emit(
            request.engagement_id,
            "tool_call_started",
            {
                "call_id": request.action_id,
                "worker": request.session_id,
                "tool": request.tool,
                "argument_digest": _argument_digest(request.arguments),
            },
        )
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
