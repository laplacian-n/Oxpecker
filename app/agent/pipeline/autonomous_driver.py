"""Autonomous pipeline driver — runs the phase state machine (agent/engagement/store.py's
INTAKE->RECON->ANALYSIS->VALIDATION->REPORT->CLOSEOUT) end to end without a human manually
gluing each step. A real end-to-end run against Juice Shop (2026-09-01) needed exactly that
manual gluing: reading the model's prose hypothesis/verdict and hand-calling
EngagementStore.create_hypothesis()/FindingsStore.add() myself. That only worked because a human
(or Claude) was driving it turn by turn; an unattended run needs the model to persist structured
state itself, via the record_hypothesis/update_hypothesis_status/record_finding MCP tools
(agent/security_mcp_server.py) this same pass added.

Two of this project's three autonomy modes share this one driver:
  - "autonomous": runs until CLOSEOUT or a genuine hard blocker, no check-ins.
  - "consult": identical loop, but pauses at each phase boundary via ConsultQueue to summarize
    what happened and ask whether to continue.
"assistant" mode is simply *not calling this* — the operator drives AgentLoop.run_task() by hand
via chat, today's existing behavior, entirely unchanged by this module.

Never fabricates progress: a judgment-call task (review_observations/validate_hypothesis) is
only marked done when the expected engagement state actually changed, mirroring
executor.py's own "never invent a fake automated answer" rule for these same two task types.
"""
from __future__ import annotations

import calendar
import json
import logging
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import config
from ..broker.broker import Broker
from ..broker.consult_queue import ConsultQueue
from ..broker.kill_switch import KillSwitch
from ..engagement.bootstrap import bootstrap_engagement
from ..engagement.locks import engagement_lock
from ..engagement.store import EngagementStore
from ..loop import AgentLoop
from .executor import TASKS_REQUIRING_MODEL, run_pending_tasks
from .orchestrator import PipelineOrchestrator

log = logging.getLogger("agent.pipeline.autonomous_driver")

MODE_AUTONOMOUS = "autonomous"
MODE_CONSULT = "consult"
VALID_MODES = (MODE_AUTONOMOUS, MODE_CONSULT)

DEFAULT_MAX_WALL_CLOCK_S = 12 * 3600  # "run all day" gets a generous but real ceiling, same
# principle as every other loop in this codebase (AgentLoop.TASK_WALL_CLOCK_S, orchestrator's
# own Budget.max_phase_wall_clock_s) never being truly unbounded.
# Mirrors AgentLoop's own REPEAT_THRESHOLD "don't retry a third near-identical variation"
# principle, applied at the pipeline-task level: a judgment task that produces no state change
# twice in a row is a genuine blocker to surface, not a transient hiccup worth looping on
# forever.
MAX_JUDGMENT_TASK_ATTEMPTS = 2
CONSULT_TIMEOUT_S = 3600  # generous — "run all day," the operator might not be watching live


@dataclass
class AutonomousRunResult:
    status: str  # "closed_out" | "blocked" | "stopped_by_operator" | "wall_clock_exceeded"
    reason: str
    phases_completed: list[str] = field(default_factory=list)
    final_phase: str = ""


def _deny_all(prompt: str) -> bool:
    """confirm_fn for the driver's own Broker instance (deterministic tasks only —
    port_discovery/http_recon/generate_report/closeout). REQUIRES_APPROVAL is empty and RoE's
    approval_policy field isn't broker-enforced today (see agent/engagement/intake.py's own
    docstring), so this should never actually be invoked for those task types — but the default
    confirm_fn blocks on input(), which would hang an unattended run forever if it ever were
    invoked. Deny, don't hang, and let the caller see the denial like any other constrained
    outcome."""
    return False


class AutonomousDriver:
    def __init__(
        self, engagement_id: str, profile_name: str, mode: str, session_id: str,
        device_id: str = "autonomous-driver", on_event: Callable[[dict], None] | None = None,
        max_wall_clock_s: float = DEFAULT_MAX_WALL_CLOCK_S,
        consult_timeout_s: float = CONSULT_TIMEOUT_S,
        stop_event: threading.Event | None = None,
    ):
        # Checked once per loop iteration (same cadence as the wall-clock/hard-blocker checks) —
        # an explicit "stop this run" request from a caller driving it in a background thread
        # (agent/web/server.py's autonomous/stop endpoint), distinct from consult mode's own
        # operator-answered stop, which goes through ConsultQueue instead.
        self.stop_event = stop_event or threading.Event()
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
        self.engagement_id = engagement_id
        self.engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
        self.profile_name = profile_name
        self.mode = mode
        self.session_id = session_id
        self.device_id = device_id
        self.on_event = on_event or (lambda event: None)
        self.max_wall_clock_s = max_wall_clock_s
        self.consult_timeout_s = consult_timeout_s
        self.store = EngagementStore(self.engagement_dir)
        self.orch = PipelineOrchestrator(self.store, profile_name)
        self.broker = Broker(engagement_dir=self.engagement_dir, confirm_fn=_deny_all)
        self._pending_operator_note: str | None = None

    def _emit(self, event_type: str, **fields) -> None:
        self.on_event({
            "type": event_type, "engagement_id": self.engagement_id, "ts": time.time(), **fields,
        })

    def _hard_blocker(self) -> str | None:
        if KillSwitch().is_engaged():
            return "kill switch engaged"
        roe_path = self.engagement_dir / "roe.json"
        if roe_path.exists():
            roe = json.loads(roe_path.read_text())
            valid_until = roe.get("valid_until")
            if valid_until:
                deadline = calendar.timegm(time.strptime(valid_until, "%Y-%m-%dT%H:%M:%SZ"))
                if time.time() > deadline:
                    return f"engagement valid_until ({valid_until}) has passed"
        return None

    def _run_review_observations(self, task: dict) -> bool:
        """"Zero hypotheses" is an explicitly legitimate, complete outcome for this task
        (phases/analysis.md) — so success is "the model completed the turn," not "at least one
        hypothesis exists." Only a non-'ok' TaskResult (budget exhausted, loop break, error)
        counts as no progress."""
        prompt = (
            "ANALYSIS phase. Review the observations recorded for this engagement and call "
            "record_hypothesis for each hypothesis worth testing in VALIDATION. Recording zero "
            "hypotheses because nothing stood out is a legitimate, complete outcome."
        )
        result = self._run_model_task(task, prompt)
        return result.status == "ok"

    def _run_validate_hypothesis(self, task: dict, hypothesis_id: str) -> bool:
        """Unlike review_observations, VALIDATION always has a required outcome once a
        hypothesis is picked up — the hypothesis's status must move off 'open'. Still 'open'
        after the turn means no real progress, whatever the turn's own TaskResult.status says."""
        hyp = self.store.get_hypothesis(hypothesis_id)
        before_status = hyp["status"]
        # Tell the model the concrete in-scope target — the judgment-task loop's system prompt
        # only says "targets in scope will be reachable" in the abstract, and the model was
        # observed defaulting to 127.0.0.1:3000 (the lab Juice Shop it's over-anchored on) and
        # then abandoning the hypothesis when scope-check denied it. RECON's deterministic tasks
        # read the target from the asset store; this one has to be told.
        targets = ", ".join(a["identifier"] for a in self.store.list_assets()) or "(see the RoE)"
        prompt = (
            f"VALIDATION phase. In-scope target(s) for this engagement: {targets} — test against "
            f"those, not any lab default. Test this hypothesis directly using the tools available and "
            f'report what you actually observed: "{hyp["title"]}" — {hyp["description"]} '
            f"Before concluding 'confirmed', compare what you observed against the target's "
            f"baseline/canonical behaviour and rule out the mundane explanation (the same app on "
            f"another port, shared hosting, an edge that answers everywhere). "
            f"Call update_hypothesis_status with the verdict and evidence once tested, and "
            f"record_finding if confirmed. hypothesis_id={hypothesis_id!r}."
        )
        self._run_model_task(task, prompt)
        after_status = self.store.get_hypothesis(hypothesis_id)["status"]
        return after_status != before_status

    def _run_model_task(self, task: dict, prompt: str):
        if self._pending_operator_note:
            prompt = f"{prompt}\n\n[OPERATOR NOTE] {self._pending_operator_note}"
            self._pending_operator_note = None

        workspace = Path(tempfile.mkdtemp(prefix=f"autonomous-{task['task_type']}-"))
        loop = AgentLoop(
            workspace_root=workspace, engagement_id=self.engagement_id, device_id=self.device_id,
            session_id=f"{self.session_id}-{task['task_id']}",
            use_security_tools=True, use_approval_queue=True,
            # use_approval_queue=True routes broker-mediated approval prompts through
            # ApprovalQueue rather than confirm_fn (see agent/loop.py's own comment on that
            # flag) — this confirm_fn only matters for a local run_command confirmation, which
            # this driver's prompts never trigger. Denies rather than hangs on input(), same
            # reasoning as _deny_all above.
            confirm_fn=_deny_all,
            # Stream (no-op sink) so the per-turn read-timeout is per chunk, not the whole
            # generation: a verbose judgment-task answer at this box's ~3 tok/s can exceed the
            # 300s non-streaming cap and crash the phase (found via the Layer-3 eval, same
            # cause). Nothing consumes the chunks here — the driver reports turn-level, not
            # token-level — it's purely for the transport timeout behaviour.
            on_stream=lambda _chunk: None,
            # These tasks must end in a record_hypothesis / update_hypothesis_status call.
            # In ANALYSIS's thinking mode the model burns the whole token budget reasoning and
            # never emits the call (finish_reason:length, empty turn, 0 hypotheses every run) —
            # found live across 6 autonomous runs. /no_think keeps it decisive and tool-calling.
            force_no_think=True,
        )
        try:
            result = loop.run_task(prompt)
        finally:
            loop.close()
        self._emit(
            "judgment_task_result", task_type=task["task_type"], task_id=task["task_id"],
            status=result.status, message=result.message[:500],
        )
        return result

    def _run_judgment_tasks(self, phase: str) -> list[dict]:
        pending = [
            t for t in self.store.list_tasks(phase=phase)
            if t["task_type"] in TASKS_REQUIRING_MODEL and t["status"] == "pending"
        ]
        outcomes = []
        for task in pending:
            self.store.update_task(task["task_id"], expected_version=task["version"], status="running")
            params = json.loads(task["params_json"]) if task["params_json"] else {}
            try:
                if task["task_type"] == "review_observations":
                    changed = self._run_review_observations(task)
                elif task["task_type"] == "validate_hypothesis":
                    changed = self._run_validate_hypothesis(task, params.get("entity_id"))
                else:
                    changed = False
            except Exception as e:
                log.warning("judgment task %s raised: %s", task["task_id"], e, exc_info=True)
                self._emit("judgment_task_error", task_id=task["task_id"], error=str(e))
                changed = False
            outcomes.append({"task": task, "changed": changed})
        return outcomes

    def _consult_checkpoint(self, completed_phase: str) -> bool:
        """consult mode only. Returns True if the operator asked to stop."""
        queue = ConsultQueue()
        current_phase = self.store.get_phase()["current_phase"]
        question = (
            f"Completed phase {completed_phase}. Current phase is now {current_phase}. Continue?"
        )
        request_id = queue.submit(
            session_id=self.session_id, question=question,
            context={"completed_phase": completed_phase, "current_phase": current_phase},
            options=["continue", "stop"],
        )
        self._emit("consult_pending", request_id=request_id, question=question)
        # Polls directly (rather than ConsultQueue.wait_for_answer's own blocking wait) so an
        # explicit stop request (self.stop_event, set by a caller driving this in a background
        # thread) takes effect promptly during a consult wait too, not just at the next loop
        # iteration boundary — a consult window can be up to consult_timeout_s (an hour by
        # default), and "stop" is supposed to be immediate.
        deadline = time.time() + self.consult_timeout_s
        record = None
        while time.time() < deadline:
            if self.stop_event.is_set():
                self._emit("consult_interrupted_by_stop", request_id=request_id)
                return True
            candidate = queue.get(request_id)
            if candidate["status"] != "pending":
                record = candidate
                break
            time.sleep(0.2)
        if record is None:
            # Consult mode was chosen specifically to be asked — an unanswered check-in should
            # stop safely, not silently fall back to running unattended (that would defeat the
            # reason consult mode was picked over autonomous mode in the first place).
            self._emit("consult_timeout", request_id=request_id)
            return True
        answer = (record.get("answer") or "").strip()
        self._emit("consult_answered", request_id=request_id, answer=answer)
        if answer.lower() in ("stop", "no", "halt"):
            return True
        if answer.lower() not in ("continue", "yes", "", "ok"):
            self._pending_operator_note = answer
        return False

    def _bootstrap_engagement(self) -> None:
        """Make an autonomous run start from a usable engagement without the operator hand-wiring
        it first — shared with AgentLoop (agent/engagement/bootstrap.py), which needs the same
        state store + assets for an assistant session against `lab-default`."""
        bootstrap_engagement(
            self.engagement_id, store=self.store,
            on_event=lambda event: self._emit(event["type"], detail=event.get("detail", "")),
        )

    def run(self) -> AutonomousRunResult:
        # Serialize whole runs against the same engagement — same lock AgentLoop.run_task()
        # takes (agent/engagement/locks.py), for the same reason: an assistant session and an
        # autonomous run both mutating one engagement's shared state concurrently is the exact
        # class of bug that lock exists to prevent, not just two autonomous runs racing each
        # other (start_autonomous already refuses a second run on the same *session*, but that
        # says nothing about a *different* session's assistant-mode chat hitting the same
        # engagement while this one is running).
        with engagement_lock(self.engagement_id):
            return self._run_inner()

    def _run_inner(self) -> AutonomousRunResult:
        start = time.monotonic()
        phases_completed: list[str] = []
        attempts: dict[str, int] = {}
        self._bootstrap_engagement()

        while True:
            if self.stop_event.is_set():
                self._emit("stopped_by_operator", reason="explicit stop request")
                return AutonomousRunResult(
                    "stopped_by_operator", "explicit stop request", phases_completed,
                    self.store.get_phase()["current_phase"],
                )

            if time.monotonic() - start > self.max_wall_clock_s:
                reason = f"exceeded max_wall_clock_s ({self.max_wall_clock_s}s)"
                self._emit("blocked", reason=reason)
                return AutonomousRunResult(
                    "wall_clock_exceeded", reason, phases_completed,
                    self.store.get_phase()["current_phase"],
                )

            blocker = self._hard_blocker()
            if blocker:
                self._emit("blocked", reason=blocker)
                return AutonomousRunResult(
                    "blocked", blocker, phases_completed, self.store.get_phase()["current_phase"],
                )

            phase = self.store.get_phase()["current_phase"]
            self._emit("phase_entered", phase=phase)

            self.orch.plan_tasks()
            deterministic_outcomes = run_pending_tasks(self.orch, self.broker, self.session_id)
            for o in deterministic_outcomes:
                self._emit(
                    "deterministic_task_done", task_type=o.task_type, ok=o.ok, detail=o.detail,
                )

            if phase == "CLOSEOUT":
                closeout_task = next(
                    (t for t in self.store.list_tasks(phase="CLOSEOUT") if t["task_type"] == "closeout"),
                    None,
                )
                if closeout_task and closeout_task["status"] == "done":
                    self._emit("run_complete", phase="CLOSEOUT")
                    return AutonomousRunResult(
                        "closed_out", "engagement closed out", phases_completed, "CLOSEOUT",
                    )

            judgment_outcomes = self._run_judgment_tasks(phase)
            blocked_on_task = False
            for outcome in judgment_outcomes:
                task = outcome["task"]
                task_row = next(t for t in self.store.list_tasks(phase=phase) if t["task_id"] == task["task_id"])
                if outcome["changed"]:
                    self.store.update_task(task_row["task_id"], expected_version=task_row["version"], status="done")
                    attempts.pop(task["task_id"], None)
                    continue
                attempts[task["task_id"]] = attempts.get(task["task_id"], 0) + 1
                if attempts[task["task_id"]] >= MAX_JUDGMENT_TASK_ATTEMPTS:
                    self.store.update_task(
                        task_row["task_id"], expected_version=task_row["version"], status="failed",
                        result={"detail": f"no progress after {MAX_JUDGMENT_TASK_ATTEMPTS} attempts"},
                    )
                    reason = (
                        f"{task['task_type']} task {task['task_id']} made no progress after "
                        f"{MAX_JUDGMENT_TASK_ATTEMPTS} attempts"
                    )
                    self._emit("blocked", reason=reason)
                    blocked_on_task = reason
                else:
                    self.store.update_task(task_row["task_id"], expected_version=task_row["version"], status="pending")
            if blocked_on_task:
                return AutonomousRunResult("blocked", blocked_on_task, phases_completed, phase)

            advanced = self.orch.advance_if_ready(f"autonomous-driver ({self.mode}): ")
            if advanced:
                new_phase = self.store.get_phase()["current_phase"]
                phases_completed.append(phase)
                self._emit("phase_advanced", from_phase=phase, to_phase=new_phase)

            if not advanced and not judgment_outcomes and not deterministic_outcomes:
                reason = f"phase {phase} made no progress and is not ready to advance"
                self._emit("blocked", reason=reason)
                return AutonomousRunResult("blocked", reason, phases_completed, phase)

            # INTAKE has no task templates in either profile (profiles.py) — completing it is a
            # near-instant no-op transition with nothing yet to report, so skip the checkpoint
            # there specifically rather than opening a consult run with an empty "completed
            # INTAKE" question. Found via a real test run: the first checkpoint fired after
            # INTAKE, not RECON as expected.
            if self.mode == MODE_CONSULT and advanced and phase != "INTAKE":
                if self._consult_checkpoint(phase):
                    self._emit("stopped_by_operator", after_phase=phase)
                    return AutonomousRunResult(
                        "stopped_by_operator", "operator requested stop", phases_completed,
                        self.store.get_phase()["current_phase"],
                    )
