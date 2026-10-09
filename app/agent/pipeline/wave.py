"""The wave orchestrator — the high-tier engine of AGENT_ARCHITECTURE.md §2.5.

A wave dispatches several workers against distinct experiments, runs them in parallel, and
synchronises on the wave finishing rather than on any one worker (§2.5, §14.1 E/F′). This is the
deterministic half: it claims experiments, spawns workers, holds the barrier, enforces the
per-worker wall-clock limit, and emits the §4.2 wave/worker/experiment events that the flow view
and the trajectory spine draw from. The strategist (a model) decides *what* to dispatch; that
decision is the `dispatch` argument, so this layer stays deterministic (§2.7).

Decisions it embodies (docs/WAVE_IMPLEMENTATION.md):

  * **Claiming is atomic.** Each experiment is claimed through `HypothesisGraphStore.claim_experiment`
    (a partial unique index), so two waves — or a retry racing a live worker — never run the same
    `(hypothesis, method)` twice. A pair the claim refuses is simply not dispatched.
  * **The wall-clock limit is a hard stop that records a partial result.** A worker over its
    deadline is signalled to stop and, if it will not, abandoned — but it is recorded as
    `timeout` with a reason, never dropped, or §8's trajectory loses the "ran out of time here"
    that a reader needs. A Python thread cannot be force-killed, so the limit is cooperative (a
    stop event the worker checks) plus a join deadline; what the worker wrote before stopping is
    already on the graph/stream.
  * **Workers are threads, and every shared write they make is atomic** (the claim here, the event
    log's sequence, the spend reservation) — the only reason threads are safe (A2).

The worker itself is injected (`worker_runner`), so the barrier, the timeout handling and the
event sequence are tested without a live model; the real worker is an `AgentLoop` with a narrow
brief, wired in a later step.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..engagement import emit as event_emit


@dataclass
class WorkerResult:
    """What a worker hands back. `outcome` is how it ended; `verdict` is the experiment's result
    where the worker reached one."""
    outcome: str  # "completed" | "timeout" | "error"
    verdict: str | None = None
    detail: str = ""


@dataclass
class WaveConfig:
    max_workers: int = 5          # §2.5 / owner A6
    worker_wall_clock_s: float = 600.0   # 10 minutes, a hard per-worker limit
    join_grace_s: float = 1.0     # how long to wait for a signalled worker to stop before abandoning it


# (hypothesis_id, method, stop_event, deadline_epoch) -> WorkerResult. A cooperative worker checks
# stop_event / deadline between steps; the orchestrator sets stop_event when the wall-clock is hit.
WorkerRunner = Callable[[str, str, threading.Event, float], WorkerResult]


@dataclass
class _LiveWorker:
    worker_id: str
    hypothesis_id: str
    method: str
    claim_id: str
    attempt: int
    stop: threading.Event = field(default_factory=threading.Event)
    result: WorkerResult | None = None
    thread: threading.Thread | None = None


class WaveOrchestrator:
    def __init__(
        self,
        engagement_id: str,
        graph_store,
        worker_runner: WorkerRunner,
        config: WaveConfig | None = None,
        emit=event_emit.emit,
    ):
        self.engagement_id = engagement_id
        self.graph_store = graph_store
        self.worker_runner = worker_runner
        self.config = config or WaveConfig()
        self._emit = emit

    def run_wave(self, wave_no: int, dispatch: list[tuple[str, str]]) -> list[WorkerResult]:
        """Run one wave over the strategist's chosen `(hypothesis_id, method)` pairs. Returns the
        workers' results in dispatch order (only those that claimed and ran)."""
        claimed = self._claim(dispatch)
        self._emit(self.engagement_id, "wave_started", {
            "wave": wave_no,
            "experiments": [{"hypothesis_id": w.hypothesis_id, "method": w.method} for w in claimed],
        })

        deadline = time.monotonic() + self.config.worker_wall_clock_s
        for w in claimed:
            self._emit(self.engagement_id, "worker_spawned", {
                "worker_id": w.worker_id, "hypothesis": w.hypothesis_id, "method": w.method,
            })
            self._emit(self.engagement_id, "experiment_started", {
                "hypothesis_id": w.hypothesis_id, "attempt": w.attempt, "method": w.method,
            })
            w.thread = threading.Thread(target=self._run_one, args=(w,), daemon=True)
            w.thread.start()

        self._await_barrier(claimed, deadline)

        results: list[WorkerResult] = []
        for w in claimed:
            result = w.result or WorkerResult(outcome="timeout",
                                              detail="wall-clock exceeded; worker did not stop in time")
            results.append(result)
            # The claim is completed either way — the experiment ran (to a verdict or to a
            # timeout); a retry is a new claim, decided by the strategist.
            self.graph_store.complete_claim(w.claim_id)
            self._emit(self.engagement_id, "worker_finished", {
                "worker_id": w.worker_id, "outcome": result.outcome, "detail": result.detail,
            })
            self._emit(self.engagement_id, "experiment_ended", {
                "hypothesis_id": w.hypothesis_id, "attempt": w.attempt,
                "verdict": result.verdict or result.outcome,
            })

        self._emit(self.engagement_id, "wave_ended", {"wave": wave_no, "workers": len(claimed)})
        return results

    def _claim(self, dispatch: list[tuple[str, str]]) -> list[_LiveWorker]:
        """Claim up to max_workers experiments. A pair whose claim is refused (already held) is
        skipped, not retried here — the strategist owns re-dispatch."""
        claimed: list[_LiveWorker] = []
        for i, (hid, method) in enumerate(dispatch):
            if len(claimed) >= self.config.max_workers:
                break
            worker_id = f"w{i}"
            cid = self.graph_store.claim_experiment(hid, method, worker=worker_id)
            if cid is None:
                continue
            attempt = self._attempt_no(hid)
            claimed.append(_LiveWorker(worker_id=worker_id, hypothesis_id=hid, method=method,
                                       claim_id=cid, attempt=attempt))
        return claimed

    def _attempt_no(self, hypothesis_id: str) -> int:
        """Best-effort attempt number for the experiment_started/ended events — the count of this
        hypothesis's experiments plus one. Advisory telemetry, so a lookup failure defaults to 1
        rather than breaking the dispatch."""
        try:
            return len(self.graph_store.list_experiments(hypothesis_id)) + 1
        except Exception:  # noqa: BLE001
            return 1

    def _run_one(self, w: _LiveWorker) -> None:
        deadline = time.monotonic() + self.config.worker_wall_clock_s
        try:
            w.result = self.worker_runner(w.hypothesis_id, w.method, w.stop, deadline)
        except Exception as e:  # noqa: BLE001 - a worker crash is a recorded failure, not a wave crash
            w.result = WorkerResult(outcome="error", detail=f"{type(e).__name__}: {e}")

    def _await_barrier(self, claimed: list[_LiveWorker], deadline: float) -> None:
        """Wait for every worker, but no longer than the wall-clock. A worker still running at the
        deadline is signalled to stop; if it does not stop within the grace window it is abandoned
        (its thread is a daemon) and recorded as a timeout by run_wave. The wave does not block on
        a stuck worker past its limit — §2.5's barrier is on the wave, bounded by the limit."""
        for w in claimed:
            if w.thread is None:
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                w.thread.join(timeout=remaining)
            if w.thread.is_alive():
                w.stop.set()  # cooperative hard stop
                w.thread.join(timeout=self.config.join_grace_s)
