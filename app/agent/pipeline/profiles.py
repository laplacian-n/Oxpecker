"""M5.3 — two pipeline profiles sharing one phase state machine
(`agent.engagement.store.PHASES`). Web/API and Network differ only in what tasks RECON
generates; ANALYSIS/VALIDATION/REPORT/CLOSEOUT are identical because they operate on the shared
state (hypotheses, observations, coverage) rather than on protocol-specific tool output.

Every exit criterion evaluated against these profiles (see `orchestrator.py`) is
budget/completion-driven, never "must find at least one vulnerability" — `phase5-design-
detail.md`'s exit criterion of that shape is explicitly rejected by both review documents and by
the ROADMAP built from them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskTemplate:
    task_type: str
    phase: str
    per: str  # "asset" | "service" | "hypothesis" | "engagement" (singleton, entity_id is None)


WEB_API_PROFILE = {
    "name": "web_api",
    "tasks": [
        TaskTemplate("port_discovery", "RECON", "asset"),
        TaskTemplate("http_recon", "RECON", "asset"),
        TaskTemplate("review_observations", "ANALYSIS", "asset"),
        TaskTemplate("validate_hypothesis", "VALIDATION", "hypothesis"),
        TaskTemplate("generate_report", "REPORT", "engagement"),
        TaskTemplate("closeout", "CLOSEOUT", "engagement"),
    ],
}

NETWORK_PROFILE = {
    "name": "network",
    "tasks": [
        TaskTemplate("port_discovery", "RECON", "asset"),
        TaskTemplate("service_fingerprint", "RECON", "service"),
        TaskTemplate("review_observations", "ANALYSIS", "asset"),
        TaskTemplate("validate_hypothesis", "VALIDATION", "hypothesis"),
        TaskTemplate("generate_report", "REPORT", "engagement"),
        TaskTemplate("closeout", "CLOSEOUT", "engagement"),
    ],
}

PROFILES = {"web_api": WEB_API_PROFILE, "network": NETWORK_PROFILE}
