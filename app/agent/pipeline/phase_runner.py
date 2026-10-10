"""Phase runners (AGENT_ARCHITECTURE.md §2.6): the one thing that differs between a flat-tier and a
graph-tier phase, factored to sit *under* the driver's invariants rather than forking the driver.

`AutonomousDriver` owns everything that must be identical across tiers — the engagement lock, the
bootstrap, the wall-clock, `_hard_blocker`, the kill switch, the consult checkpoint, phase
advancement and event emission (plus the broker/audit/evidence path, §2.6.1). A phase runner owns
only the two things a tier actually changes:

  * **how a phase produces work** — flat: plan tasks and run them; graph: seed the recon roots and
    dispatch the wave; and
  * **when a phase is done** — flat: the orchestrator's task-completion check; graph: the wave
    concluded (no open hypotheses left once scope roots are excluded, §14.1 G).

A runner never takes the lock, never bootstraps, never advances the phase itself — those are the
driver's, so there is exactly one place each invariant lives. This is what makes the §12 step-0a
parity test expressible as "the same engagement through both runners writes a byte-identical audit
entry and evidence record": both runners are driven by one driver, so the protected path is shared
code, not two implementations that merely look alike.

The single mandate (the whole point of the seam): `runner_for(phase, tier)` decides flat vs graph
by calling the **same** `tiers.uses_graph(phase, tier)` that `plan_tasks` and `check_transition`
call. Three deciders of the mode would be the fork this seam exists to prevent, back in a form
that is harder to see.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import tiers
from .executor import run_pending_tasks

log = logging.getLogger("agent.pipeline.phase_runner")


@dataclass
class ProduceResult:
    """What producing a phase's work this iteration did. `made_progress` feeds the driver's
    no-progress blocker; `blocker` is a hard stop the driver surfaces; `run_complete` signals the
    terminal CLOSEOUT finish. The driver decides what to do with all three — the runner only
    reports."""
    made_progress: bool
    blocker: str | None = None
    run_complete: bool = False


class FlatPhaseRunner:
    """The flat tiers (medium/low, and every pre-tier engagement): plan the phase's tasks, run the
    deterministic ones through the broker, run the judgment tasks through the model, and apply the
    per-task retry/blocking policy. This is the driver's original per-phase body, moved verbatim so
    the flat path is byte-for-byte unchanged — the runner is a seam, not a rewrite."""

    def __init__(self, driver):
        self.d = driver  # the driver owns orch/store/broker/session and the judgment-task machinery

    def produce(self, phase: str, attempts: dict) -> ProduceResult:
        d = self.d
        d.orch.plan_tasks()
        deterministic_outcomes = run_pending_tasks(d.orch, d.broker, d.session_id)
        for o in deterministic_outcomes:
            d._emit("deterministic_task_done", task_type=o.task_type, ok=o.ok, detail=o.detail)

        if phase == "CLOSEOUT":
            closeout_task = next(
                (t for t in d.store.list_tasks(phase="CLOSEOUT") if t["task_type"] == "closeout"),
                None,
            )
            if closeout_task and closeout_task["status"] == "done":
                return ProduceResult(made_progress=bool(deterministic_outcomes), run_complete=True)

        judgment_outcomes = d._run_judgment_tasks(phase)
        blocked_on_task: str | None = None
        for outcome in judgment_outcomes:
            task = outcome["task"]
            task_row = next(t for t in d.store.list_tasks(phase=phase) if t["task_id"] == task["task_id"])
            if outcome["changed"]:
                d.store.update_task(task_row["task_id"], expected_version=task_row["version"], status="done")
                attempts.pop(task["task_id"], None)
                continue
            attempts[task["task_id"]] = attempts.get(task["task_id"], 0) + 1
            if attempts[task["task_id"]] >= d.MAX_JUDGMENT_TASK_ATTEMPTS:
                d.store.update_task(
                    task_row["task_id"], expected_version=task_row["version"], status="failed",
                    result={"detail": f"no progress after {d.MAX_JUDGMENT_TASK_ATTEMPTS} attempts"},
                )
                blocked_on_task = (
                    f"{task['task_type']} task {task['task_id']} made no progress after "
                    f"{d.MAX_JUDGMENT_TASK_ATTEMPTS} attempts"
                )
                d._emit("blocked", reason=blocked_on_task)
            else:
                d.store.update_task(task_row["task_id"], expected_version=task_row["version"], status="pending")

        return ProduceResult(
            made_progress=bool(deterministic_outcomes or judgment_outcomes),
            blocker=blocked_on_task,
        )

    def wave_concluded(self, phase: str) -> bool:
        # Flat phases advance through the orchestrator's check_transition (the driver calls
        # advance_if_ready); the runner never advances, so it never claims a wave conclusion.
        return False


class GraphPhaseRunner:
    """The high tier's RECON..VALIDATION: the wave engine against the hypothesis graph. RECON seeds
    a recon root per scope entry (§14.1 G) and dispatches their enumeration; ANALYSIS/VALIDATION run
    the strategist-driven wave over the hypotheses recon spawned. The phase is done when no open
    work remains once the scope roots — which are enumeration directives, not falsifiable claims,
    and so never counted as outstanding work — are excluded.

    The wave engine and the recon dispatch are injected so the deterministic wiring (seeding, root
    closing, the done predicate) is testable without a live model; the defaults build the real
    wave engine and a real recon worker.
    """

    def __init__(self, driver, *, graph_store, scope_entries, model=None,
                 wave_engine_factory=None, recon_runner=None):
        self.d = driver
        self.graph = graph_store
        self.scope_entries = list(scope_entries)
        # The engagement's model (roe.json), translated to the strategist provider and the worker
        # client once, here — so a high-tier engagement on an OpenRouter model runs its wave on
        # OpenRouter (GPU untouched) and a default one runs local, without this class naming a
        # provider. (None, None) means "use the engine's own defaults".
        self.model = model
        self._wave_engine_factory = wave_engine_factory
        self._recon_runner = recon_runner

    # -- RECON --------------------------------------------------------------------------------
    def _seed_and_enumerate(self) -> bool:
        from . import graph_coldstart as cs

        cs.seed_scope_roots(self.graph, self.scope_entries)
        # Invariant 1 (§14.1 G): the roots are exactly the authorized scope entries. A drift here —
        # a root for something the RoE did not authorize — must fail loudly, not enumerate.
        cs.assert_roots_match_scope(self.graph, self.scope_entries)

        open_roots = [h for h in cs.scope_roots(self.graph)
                      if h.get("lifecycle_status") in ("draft", "open", "queued", "running")]
        if not open_roots:
            return False  # already enumerated — nothing to do this iteration
        # Deterministic seed first: fetch each root's surface in code and record a child hypothesis
        # per mechanical finding (missing headers, insecure cookies, disclosed versions, linked
        # endpoints, forms). This guarantees a non-empty, testable graph even when the model's recon
        # pass does not write hypotheses — the failure that left every engagement closing out after
        # one shallow pass.
        seeded = 0
        for r in open_roots:
            seeded += self._seed_root_deterministically(r)

        # The model recon pass is a fallback for a target the deterministic rules could not read
        # (seeded nothing). When seeding already produced a surface, skip it: it was observed to
        # make the model crawl dozens of URLs (one run issued 42 http_recon calls, ran for minutes
        # and left the next phase wedged), and the deeper, model-driven testing belongs to ANALYSIS,
        # which also branches off what it finds. So the model runs here only when there is nothing
        # to test yet — and an injected recon runner (tests) still runs, since it is explicit.
        if seeded == 0 or self._recon_runner is not None:
            dispatch = [(r["hypothesis_id"], "enumerate the in-scope surface") for r in open_roots]
            runner = self._recon_runner or self._default_recon_runner()
            runner(dispatch)

        # Surface what recon found to the operator (the UI renders these over the SSE) — so an
        # autonomous run shows the attack surface it is about to investigate instead of jumping
        # silently from RECON to ANALYSIS.
        self._announce(self._recon_summary(open_roots))
        # Recon is enumeration, not a verdict: once a root's recon ran, close it by lifecycle so the
        # strategist stops dispatching it and the phase can conclude (its children carry the
        # falsifiable claims forward).
        for r in open_roots:
            cs.mark_root_enumerated(self.graph, r["hypothesis_id"])
        return True

    def _announce(self, detail: str) -> None:
        """Forward a human-readable progress line to the driver's event stream (the UI), if the
        driver exposes one. Best-effort: a driver/test without _emit just gets nothing."""
        if not detail:
            return
        emit = getattr(self.d, "_emit", None)
        if callable(emit):
            try:
                emit("recon_enumerated", detail=detail)
            except Exception:  # noqa: BLE001 — telemetry must never break enumeration
                pass

    def _recon_summary(self, roots: list[dict]) -> str:
        """One line naming how many child hypotheses recon produced and a sample of their surfaces —
        what the operator most wants to see when RECON finishes."""
        from . import graph_coldstart as cs

        children = cs.open_hypotheses_excluding_roots(self.graph)
        if not children:
            return "RECON found no testable surface on the in-scope target(s)."
        surfaces = [c.get("surface") or c.get("title") or "" for c in children]
        surfaces = [s for s in surfaces if s]
        sample = ", ".join(surfaces[:8])
        more = f" (+{len(surfaces) - 8} more)" if len(surfaces) > 8 else ""
        return f"RECON found {len(children)} things to test: {sample}{more}"

    def _seed_root_deterministically(self, root: dict) -> int:
        """http_recon the root's scope host in code and seed child hypotheses from the response.

        Best-effort: a scope entry that is a wildcard, an unreachable host, or an out-of-scope URL
        simply seeds nothing (the model pass still runs). Never raises into enumeration."""
        from . import graph_coldstart as cs
        from .recon_seed import seed_recon_hypotheses

        entry = cs.root_scope_entry(root)
        if not entry or "*" in entry:
            return 0
        try:
            from ..broker.policy import load_policy
            from ..security_tools import http_recon

            policy = load_policy(self.d.engagement_dir)
            recon = None
            for url in (f"https://{entry}/", f"http://{entry}/"):
                try:
                    recon = http_recon.run(url, policy)
                except Exception:  # noqa: BLE001 — scope/DNS/TLS failure: try the next scheme
                    recon = None
                if recon and recon.get("ok"):
                    return seed_recon_hypotheses(self.graph, root["hypothesis_id"], recon, url)
        except Exception:  # noqa: BLE001 — deterministic seeding is a best-effort safety net
            return 0
        return 0

    def _default_recon_runner(self):
        # One wave dispatching the per-root recon experiments in parallel (the walkthrough's
        # "send several workers to recon"). Built lazily so importing this module needs no model
        # stack. Workers run on the engagement's model (OpenRouter when configured, so no GPU).
        from .engine import resolve_wave_clients
        from .wave import WaveOrchestrator
        from .wave_worker import make_agent_loop_worker, recon_brief

        _, client_factory = resolve_wave_clients(self.model)
        # The recon-specific brief: enumerate the surface and spawn a child hypothesis per element
        # found (recon_brief), not the default "test one hypothesis, do not branch" brief. Without
        # this the root records a single observation and closes with no children, so ANALYSIS has
        # nothing to dispatch and the engagement ends after one shallow pass.
        worker = make_agent_loop_worker(
            self.d.engagement_id, client_factory=client_factory, brief_fn=recon_brief)
        orch = WaveOrchestrator(self.d.engagement_id, self.graph, worker)
        return lambda dispatch: orch.run_wave(1, dispatch)

    # -- ANALYSIS / VALIDATION ----------------------------------------------------------------
    def _run_strategist_waves(self) -> bool:
        from .engine import build_wave_engine, resolve_wave_clients

        provider, client_factory = resolve_wave_clients(self.model)
        factory = self._wave_engine_factory or (
            lambda: build_wave_engine(self.d.engagement_id, provider=provider,
                                      client_factory=client_factory)
        )
        engine = factory()
        summary = engine.run()
        waves = summary.get("waves", 0)
        if waves:
            tested = summary.get("experiments") or summary.get("dispatched") or ""
            self._announce(
                f"Investigated the hypotheses over {waves} wave(s)"
                + (f", {tested} experiment(s)" if tested else "") + ".")
        return waves > 0

    def produce(self, phase: str, attempts: dict) -> ProduceResult:
        if phase == "RECON":
            progressed = self._seed_and_enumerate()
        else:  # ANALYSIS, VALIDATION
            progressed = self._run_strategist_waves()
        return ProduceResult(made_progress=progressed)

    def wave_concluded(self, phase: str) -> bool:
        from . import graph_coldstart as cs

        if phase == "RECON":
            # RECON is done when every scope root is enumerated (none left open) — children belong
            # to the phases after it, so they do not hold RECON open.
            return not any(
                h.get("lifecycle_status") in ("draft", "open", "queued", "running")
                for h in cs.scope_roots(self.graph)
            )
        # ANALYSIS/VALIDATION: done when no open hypothesis remains once roots are excluded — the
        # strategist has nothing worthwhile left to dispatch (§2.7, a first-class outcome).
        return not cs.open_hypotheses_excluding_roots(self.graph)


def runner_for(driver, phase: str, *, graph_store=None, scope_entries=None, model=None,
               wave_engine_factory=None, recon_runner=None):
    """Select the runner for `(phase, driver.tier)` — the ONE place the mode is decided for work
    production, and it decides it with the same `tiers.uses_graph` that `plan_tasks` and
    `check_transition` use. A graph runner needs the graph store and scope entries; a caller that
    did not provide them for a graph phase is a wiring bug worth failing on, not silently running
    flat."""
    if tiers.uses_graph(phase, driver.tier):
        if graph_store is None or scope_entries is None:
            raise ValueError(
                f"graph-mode phase {phase!r} needs a graph store and scope entries; none supplied"
            )
        return GraphPhaseRunner(
            driver, graph_store=graph_store, scope_entries=scope_entries, model=model,
            wave_engine_factory=wave_engine_factory, recon_runner=recon_runner,
        )
    return FlatPhaseRunner(driver)
