"""The high-tier engine loop (AGENT_ARCHITECTURE.md §2.5): run waves until there is nothing left
to test. Each wave, the strategist (a model) chooses the experiments and the orchestrator claims,
dispatches and barriers them. This is the thin deterministic loop that ties the two together — it
holds no judgment of its own (that is the strategist's, §2.7); it only decides *when to stop*:
when the strategist dispatches nothing (every open hypothesis is concluded or none remain) or a
wave cap is hit.

Kept separate from both so each stays testable alone: the strategist is a model call, the
orchestrator is the thread/claim/barrier machinery, and this is the loop over them — injected in
tests, so the stop logic is pinned without a model or real workers.
"""
from __future__ import annotations

import logging

log = logging.getLogger("agent.pipeline.wave_driver")


class WaveDriver:
    def __init__(self, strategist, orchestrator, *, max_waves: int = 10):
        self.strategist = strategist
        self.orchestrator = orchestrator
        self.max_waves = max_waves

    def run(self) -> dict:
        """Run waves until the strategist dispatches nothing or the wave cap is hit. Returns a
        summary: status ('complete' | 'wave_cap'), the reason, and how many waves ran."""
        waves_run = 0
        while waves_run < self.max_waves:
            dispatch = self.strategist.decide()
            if not dispatch:
                # The strategist chose nothing — every open hypothesis is concluded, or there were
                # none to begin with. That is the natural end of a high-tier run, not an error.
                return {"status": "complete", "reason": "no experiments to dispatch", "waves": waves_run}
            waves_run += 1
            self.orchestrator.run_wave(waves_run, dispatch)
        return {"status": "wave_cap", "reason": f"reached the {self.max_waves}-wave cap", "waves": waves_run}
