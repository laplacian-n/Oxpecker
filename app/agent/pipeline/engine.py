"""Tier -> engine (AGENT_ARCHITECTURE.md §2.6): the seam that turns an engagement's tier into the
thing that actually runs, and — for the high tier — the one place that *assembles* the wave engine
from an engagement_id.

The §2.6 table maps each tier to an orchestration shape, not to a class:

    low    -> none; one agent, a person in the strategist's chair (no autonomous engine)
    medium -> sequential waves, one worker at a time (the existing AutonomousDriver, reused)
    high   -> parallel waves, the full four-role design (the wave engine)

Two jobs live here and nothing else — the tier read belongs in the orchestration layer (§2.6, and
tiers.py's own contract: never thread the tier into the four protected components):

  * `tier_engine_kind(engagement_id)` classifies the engagement into `operator` / `sequential` /
    `wave`, so an entry point knows *which* engine to build without re-deriving the mapping.
  * `build_wave_engine(engagement_id, ...)` is the high-tier constructor. Until now the wave spine
    (strategist -> driver -> orchestrator -> claim -> worker) could be wired by hand in a test but
    nothing built it from an engagement_id: the store had to be located, a provider chosen for the
    strategist, the worker runner built with the right client, the orchestrator and driver tied
    together. That assembly is this function — one call, an engagement_id in, a runnable
    `WaveDriver` out.

Every model seam is injectable (`provider`, `client_factory`, `worker_runner`) so the assembly is
unit-tested without a live model, and so the GPU-vs-OpenRouter choice — a runtime/config concern,
not the engine's — is made by the caller (the wave never names a provider of its own, exactly as
the worker module does not import one). The production defaults build the registry's default
provider and a real agent-loop worker.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .. import config, tiers
from ..engagement import emit as event_emit
from .strategist import Strategist
from .wave import WaveConfig, WaveOrchestrator, WorkerRunner
from .wave_driver import WaveDriver
from .wave_worker import make_agent_loop_worker

log = logging.getLogger("agent.pipeline.engine")

# The §2.6 orchestration shapes, as engine kinds an entry point can switch on. Kept as the mapping
# rather than three string literals scattered at call sites, so the table lives in one place.
ENGINE_OPERATOR = "operator"      # low: no autonomous engine; the operator drives AgentLoop by hand
ENGINE_SEQUENTIAL = "sequential"  # medium: the existing AutonomousDriver, one worker at a time
ENGINE_WAVE = "wave"              # high: the parallel wave engine

_KIND_BY_TIER = {
    "low": ENGINE_OPERATOR,
    "medium": ENGINE_SEQUENTIAL,
    "high": ENGINE_WAVE,
}


def tier_engine_kind(engagement_id: str, *, engagements_root: Path | None = None) -> str:
    """The engine kind an engagement's tier calls for (`operator` / `sequential` / `wave`). Reads
    the tier through `tiers.engagement_tier`, so an engagement with no tier runs the default's
    engine — the tier stays the single source of truth and this never second-guesses it."""
    tier = tiers.engagement_tier(engagement_id, engagements_root=engagements_root)
    return _KIND_BY_TIER[tier]  # tier is already normalized to one of the three by engagement_tier


def build_wave_engine(
    engagement_id: str,
    *,
    provider=None,
    client_factory: Callable[[], object] | None = None,
    worker_runner: WorkerRunner | None = None,
    emit=event_emit.emit,
    engagements_root: Path | None = None,
    max_waves: int = 10,
    max_experiments: int = 5,
    wave_config: WaveConfig | None = None,
) -> WaveDriver:
    """Assemble the high-tier wave engine for `engagement_id` and return a runnable `WaveDriver`.

    The store is located beside the engagement (the one hypothesis_graph.db per engagement, same
    convention as everywhere else). The strategist reads it through `provider`; the workers run
    through `worker_runner`. Both default to production — the registry's default provider for the
    strategist, a real agent-loop worker for the workers — and both are injectable:

      * `provider` / `client_factory` are the two model seams. Pass an `OpenRouterProvider` and
        `client_factory=lambda: OpenRouterLoopClient(...)` to run a wave entirely on OpenRouter so
        it never touches the local GPU; the engine itself names no provider, leaving that choice
        to the caller/config.
      * `worker_runner` overrides the whole worker — a test passes a no-op runner so a live run can
        exercise strategist -> driver -> orchestrator -> claim without a costly agent run.
    """
    root = engagements_root or config.ENGAGEMENTS_ROOT
    from ..hypothesis_graph.store import HypothesisGraphStore  # lazy: keep the deterministic
    # wave layer importable without the graph/sqlite stack when only the loop is exercised.

    store = HypothesisGraphStore(root / engagement_id)

    if provider is None:
        from ..llm import registry

        provider = registry.build()  # the registry's default; a live/test caller injects instead
    if worker_runner is None:
        worker_runner = make_agent_loop_worker(engagement_id, client_factory=client_factory)

    strategist = Strategist(store, provider, max_experiments=max_experiments)
    orchestrator = WaveOrchestrator(engagement_id, store, worker_runner, config=wave_config, emit=emit)
    return WaveDriver(strategist, orchestrator, max_waves=max_waves)
