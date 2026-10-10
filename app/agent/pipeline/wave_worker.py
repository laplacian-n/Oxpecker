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


def recon_brief(hypothesis_id: str, method: str) -> str:
    """The RECON worker's brief — the opposite of _brief's 'do not branch'.

    RECON's whole job is to turn a scope root into the child hypotheses the rest of the engagement
    tests. A recon worker that only records one observation and stops leaves the strategist
    (ANALYSIS) with nothing open to dispatch, so the engagement closes out after a single shallow
    pass. This brief makes the worker enumerate the surface and spawn one child hypothesis per
    concrete attack-surface element it finds — which is what lets the investigation actually dig."""
    return (
        f"You are the RECON worker. Your ONLY deliverable is testable hypotheses recorded on the "
        f"investigation graph with the graph_hypothesis_add tool. A prose summary is NOT a valid "
        f"result and will be discarded — if you finish without calling graph_hypothesis_add, you "
        f"have failed the task. Do NOT exploit anything yet; this phase only maps the surface.\n"
        f"Root hypothesis to branch from (use as parent_ref): {hypothesis_id}\n"
        f"Goal: {method}\n\n"
        f"Loop, using tools, until you have recorded every hypothesis the surface implies — only "
        f"passive http_recon (GET, no approval needed); do NOT port-scan or send active/exploit "
        f"requests, they are gated and will stall the run:\n"
        f"1. Call http_recon on the target. From the response, list every concrete attack-surface "
        f"element: the server/tech and version, each endpoint/path/route, query/form parameter, "
        f"cookie and its flags, interesting header (or missing security header), redirect, and "
        f"every in-scope URL in the HTML or inline/linked JavaScript. Then call http_recon again on "
        f"the in-scope URLs you discovered to go one level deeper. Make several http_recon calls.\n"
        f"2. For EACH surface element, immediately call graph_hypothesis_add with: parent_ref="
        f"{hypothesis_id}, phase RECON, origin_type tool_observation, a concrete `surface` (e.g. "
        f"'/login', '/api/orders/{{id}}', 'Set-Cookie: session'), an honest confidence_band, a "
        f"short rationale citing what you saw, and a specific falsifiable `claim` a later worker "
        f"can test — for example 'the /login form is vulnerable to SQL injection via the username "
        f"field', '/api/orders/{{id}} lacks object-level authorization', 'the session cookie is "
        f"missing the Secure and HttpOnly flags', 'the server exposes its version in the Server "
        f"header (X)'. One graph_hypothesis_add call per element — aim for at least 3-8.\n"
        f"3. Only after you have recorded a hypothesis for every surface element you found may you "
        f"stop. Keep calling tools until then; do not reply with a summary instead of tool calls."
    )


def make_agent_loop_worker(
    engagement_id: str,
    *,
    loop_factory: LoopFactory = _default_loop_factory,
    client_factory: Callable[[], object] | None = None,
    workspace_root: Path | None = None,
    device_id: str = "wave-worker",
    brief_fn: Callable[[str, str], str] = _brief,
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
            # The orchestrator's caller (AutonomousDriver.run / GraphPhaseRunner) already holds the
            # engagement RLock for the whole run, and each worker runs on its own thread — so taking
            # that same lock here would deadlock the worker against the thread that holds it
            # (locks.py; the lock is re-entrant only on its holder). The workers of one wave are that
            # run, serialized as a unit by the driver's lock, not competing sessions.
            serialize_engagement=False,
            # A wave worker is unattended: there is no operator to answer an approval prompt, and
            # the security subprocess cannot block on stdin. Approval-required dispatches are granted
            # automatically (audited as auto-approvals) — still bounded by scope, the engagement's
            # allowed_action_classes, the deny list and the kill switch, which the operator set.
            auto_approve=True,
        )
        if client_factory is not None:
            kwargs["client"] = client_factory()
        loop = loop_factory(**kwargs)
        try:
            result = loop.run_task(brief_fn(hypothesis_id, method))
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
