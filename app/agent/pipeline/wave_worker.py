"""The real wave worker (§2.5): an AgentLoop given a narrow brief — one (hypothesis, method)
experiment — adapted to the orchestrator's `WorkerRunner` signature.

A worker is not a new kind of agent; it is the same AgentLoop with a scoped task and the wave's
stop event, so there is one engine (the §1 "one engine, one tool plane" rule) rather than a
second, lighter one that drifts. The orchestrator owns claiming, the barrier and the wall-clock;
this maps a claimed experiment to a run and the run's outcome back to a `WorkerResult`.

The AgentLoop factory is injectable so the adapter's own logic — the brief, the stop wiring, the
outcome mapping — is unit-tested without a live model; the default builds a real security-tools +
hypothesis-graph loop.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from .. import config
from .wave import WorkerResult

# A factory the adapter calls to build the worker's loop. Injectable for tests.
LoopFactory = Callable[..., object]


def _default_loop_factory(**kwargs):
    # Imported lazily so this module (and the orchestrator) can be imported without pulling the
    # whole loop/model stack when only the deterministic wave layer is exercised.
    from ..loop import AgentLoop

    return AgentLoop(**kwargs)


def _brief(hypothesis_id: str, method: str) -> str:
    return (
        f"Test exactly one hypothesis with exactly one method, then stop.\n"
        f"Hypothesis: {hypothesis_id}\n"
        f"Method: {method}\n"
        f"Run the experiment, record the observation and the verdict on the hypothesis graph for "
        f"this hypothesis, and do not branch into other hypotheses or methods — a sibling worker "
        f"is covering those."
    )


def make_agent_loop_worker(
    engagement_id: str,
    *,
    loop_factory: LoopFactory = _default_loop_factory,
    client_factory: Callable[[], object] | None = None,
    workspace_root: Path | None = None,
    device_id: str = "wave-worker",
):
    """Return a `WorkerRunner` the WaveOrchestrator can dispatch. Each call builds a fresh loop for
    the one experiment, passes it the wave's stop event (so the wall-clock hard-stop interrupts it
    at a turn boundary), runs it, and maps the result:

      * ``cancelled`` / ``budget_exhausted`` -> ``timeout`` (ran out of its allowance; partial work
        already recorded on the graph by the loop)
      * ``error`` -> ``error``
      * anything else -> ``completed``

    The experiment's verdict lives on the hypothesis graph, written by the loop's own graph tools
    during the run; the orchestrator reads it from there, so the WorkerResult carries the outcome
    and leaves `verdict` for the graph to own.

    `client_factory`, when given, builds the loop's model client per worker — this is how workers
    run on OpenRouter (so the wave never touches the local GPU) without this module importing the
    provider: the caller passes `lambda: OpenRouterLoopClient("openai/gpt-4o-mini")`. Omitted, the
    loop uses its own default client.
    """
    def run(hypothesis_id: str, method: str, stop: threading.Event, deadline: float) -> WorkerResult:
        kwargs = dict(
            workspace_root=workspace_root or config.default_workspace_root(),
            engagement_id=engagement_id,
            use_security_tools=True,
            use_hypothesis_graph=True,
            device_id=device_id,
            stop_event=stop,
        )
        if client_factory is not None:
            kwargs["client"] = client_factory()
        loop = loop_factory(**kwargs)
        try:
            result = loop.run_task(_brief(hypothesis_id, method))
        finally:
            close = getattr(loop, "close", None)
            if callable(close):
                close()
        status = getattr(result, "status", "error")
        if status in ("cancelled", "budget_exhausted"):
            return WorkerResult(outcome="timeout", detail=getattr(result, "message", ""))
        if status == "error":
            return WorkerResult(outcome="error", detail=getattr(result, "message", ""))
        return WorkerResult(outcome="completed", detail=getattr(result, "message", ""))

    return run
