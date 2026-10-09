"""M5.3 — deterministic pipeline planning/gating layer.

`PipelineOrchestrator` incrementally creates tasks from a profile against current engagement
state (`plan_tasks()`) and evaluates whether the current phase's exit criteria are met
(`check_transition()`/`advance_if_ready()`). It deliberately does NOT execute tasks or call any
tool/model itself — connecting a created task to an actual broker-mediated tool call (and
feeding the result back as observations/asset updates) is a distinct, larger integration into
`agent/loop.py` than this milestone covers; see `docs/STATUS.md`'s M5.3 gaps row for exactly what
that leaves undone. This module is the deterministic, model-independent half: given the same
state and profile, it always produces the same plan and the same transition decision — the
structure Phase 5's intro says should compensate for the model's judgment-under-ambiguity
weakness, rather than routing every "are we done with this phase" decision through the model.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .. import tiers
from ..engagement.store import PHASES, EngagementStore
from ..skills.library import SkillLibrary
from ..skills.schema import Skill
from .profiles import PROFILES, TaskTemplate

DEFAULT_MAX_TASKS_PER_PHASE = 50
DEFAULT_MAX_PHASE_WALL_CLOCK_S = 6 * 3600


@dataclass
class Budget:
    max_tasks_per_phase: int = DEFAULT_MAX_TASKS_PER_PHASE
    max_phase_wall_clock_s: float = DEFAULT_MAX_PHASE_WALL_CLOCK_S


class UnknownProfileError(RuntimeError):
    pass


def _entity_key(task: dict) -> str | None:
    params = json.loads(task["params_json"]) if task["params_json"] else {}
    return params.get("entity_id")


class PipelineOrchestrator:
    def __init__(
        self,
        store: EngagementStore,
        profile_name: str,
        budget: Budget | None = None,
        skill_library: SkillLibrary | None = None,
        tier: str | None = None,
    ):
        if profile_name not in PROFILES:
            raise UnknownProfileError(f"unknown profile {profile_name!r}, must be one of {list(PROFILES)}")
        self.store = store
        self.profile = PROFILES[profile_name]
        self.budget = budget or Budget()
        # M5.4 wiring: optional, additive — a caller that omits this gets tasks with no
        # skill_ids in their params, exactly the pre-existing behavior.
        self.skill_library = skill_library
        # The orchestration tier (§2.6), explicit and defaulting to None = flat. In the high tier,
        # RECON..VALIDATION run on the hypothesis graph (the wave engine), so the flat task machine
        # stands aside for those phases — see `_uses_graph`. None keeps the flat behaviour every
        # existing caller has today; nothing switches to graph mode until a caller passes "high".
        self.tier = tier

    def _uses_graph(self, phase: str) -> bool:
        """Whether `phase` runs on the graph for this orchestrator's tier. The ONE decider, so
        plan_tasks and check_transition can never disagree about the mode (tiers.uses_graph's own
        docstring says why that matters)."""
        return tiers.uses_graph(phase, self.tier)

    def skills_for_current_phase(self) -> list[Skill]:
        if self.skill_library is None:
            return []
        phase = self.store.get_phase()["current_phase"]
        return self.skill_library.for_phase_and_profile(phase, self.profile["name"])

    def _templates_for_phase(self, phase: str) -> list[TaskTemplate]:
        return [t for t in self.profile["tasks"] if t.phase == phase]

    def _entities_for(self, per: str) -> list[str | None]:
        if per == "asset":
            return [a["asset_id"] for a in self.store.list_assets()]
        if per == "service":
            return [s["service_id"] for s in self.store.list_services()]
        if per == "hypothesis":
            return [h["hypothesis_id"] for h in self.store.list_hypotheses(status="open")] + [
                h["hypothesis_id"] for h in self.store.list_hypotheses(status="testing")
            ]
        if per == "engagement":
            return [None]
        raise ValueError(f"unknown TaskTemplate.per {per!r}")

    def plan_tasks(self) -> list[str]:
        """Idempotent: safe to call repeatedly as state grows (e.g. after a port_discovery task
        result adds new services, re-calling this creates the service_fingerprint tasks those
        services now warrant). Returns newly created task_ids."""
        phase = self.store.get_phase()["current_phase"]
        if self._uses_graph(phase):
            # Graph-mode phase: the wave engine plans and runs the experiments against the
            # hypothesis graph, not the flat task machine. Creating flat tasks here would be the
            # start of a second source of truth for the same work — so plan nothing and leave the
            # phase to the wave. check_transition honours the same decision (it does not advance a
            # graph phase off an empty task list); the two must agree, which is why both ask
            # `_uses_graph`.
            return []
        templates = self._templates_for_phase(phase)
        if not templates:
            return []

        existing = self.store.list_tasks(phase=phase)
        existing_keys = {(t["task_type"], _entity_key(t)) for t in existing}
        skill_ids = [s.skill_id for s in self.skills_for_current_phase()]

        created: list[str] = []
        for template in templates:
            for entity_id in self._entities_for(template.per):
                if len(existing) + len(created) >= self.budget.max_tasks_per_phase:
                    return created
                key = (template.task_type, entity_id)
                if key in existing_keys:
                    continue
                params = {"entity_type": template.per, "entity_id": entity_id} if entity_id else {}
                if skill_ids:
                    params["skill_ids"] = skill_ids
                task_id = self.store.create_task(phase, template.task_type, params=params)
                created.append(task_id)
                existing_keys.add(key)
        return created

    def check_transition(self) -> tuple[bool, str]:
        """Never requires a finding to exist — only planned-work completion or budget exhaustion
        makes a phase 'ready'. Returns (ready, human-readable reason)."""
        phase_row = self.store.get_phase()
        phase = phase_row["current_phase"]
        idx = PHASES.index(phase)
        if idx == len(PHASES) - 1:
            return False, "already at the terminal phase (CLOSEOUT)"

        elapsed = time.time() - phase_row["entered_at"]
        if elapsed >= self.budget.max_phase_wall_clock_s:
            return True, (
                f"phase wall-clock budget exhausted "
                f"({elapsed:.0f}s >= {self.budget.max_phase_wall_clock_s}s)"
            )

        if self._uses_graph(phase):
            # Graph-mode phase: the flat task list does not govern it, so this method must NOT read
            # readiness off the task list — in particular not the "no templates -> ready" shortcut
            # below, which would jump the phase before the first wave ran (a graph phase has no
            # flat templates, so that shortcut would fire immediately). Readiness here is the
            # wave's conclusion — the strategist dispatching nothing, which is a first-class
            # outcome (§2.7) — and that signal is supplied by the driver, which advances the phase
            # explicitly. The one automatic exit that still applies is the wall-clock ceiling
            # above (the owner's "or budget"). Everything else: not ready by the task machine.
            return False, "graph-mode phase advances on the wave's conclusion, not the task list"

        templates = self._templates_for_phase(phase)
        if not templates:
            next_phase = PHASES[idx + 1]
            next_templates = self._templates_for_phase(next_phase)
            if not next_templates:
                return True, f"no tasks defined for {phase} or {next_phase} in this profile"
            for template in next_templates:
                if self._entities_for(template.per):
                    return True, f"prerequisite entities for {next_phase} exist"
            return False, f"no entities yet for {next_phase}'s task templates"

        tasks = self.store.list_tasks(phase=phase)
        planned_keys = {
            (template.task_type, entity_id)
            for template in templates
            for entity_id in self._entities_for(template.per)
        }
        created_keys = {(t["task_type"], _entity_key(t)) for t in tasks}
        missing = planned_keys - created_keys
        if missing:
            return False, f"{len(missing)} planned task(s) not yet created — call plan_tasks() first"

        non_terminal = [t for t in tasks if t["status"] in ("pending", "running", "blocked")]
        if non_terminal:
            return False, f"{len(non_terminal)} task(s) still not terminal in {phase}"
        return True, f"all {len(tasks)} task(s) in {phase} are terminal"

    def advance_if_ready(self, reason_prefix: str = "") -> bool:
        ready, reason = self.check_transition()
        if not ready:
            return False
        phase_row = self.store.get_phase()
        idx = PHASES.index(phase_row["current_phase"])
        next_phase = PHASES[idx + 1]
        self.store.transition_phase(
            next_phase, f"{reason_prefix}{reason}", expected_version=phase_row["version"]
        )
        return True
