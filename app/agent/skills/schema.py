"""M5.4 — skill schema. A skill is curated guidance text for tackling a class of hypothesis
(e.g. "how to validate a reflected-XSS hypothesis on a search endpoint") — never executable code,
never a place target or model output gets written to. See `library.py` for the read-only,
signature-verified loader and `cli.py` for the offline authoring/signing step.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Skill:
    skill_id: str
    version: str
    title: str
    description: str
    guidance: str
    applicable_phases: list[str] = field(default_factory=list)
    applicable_profiles: list[str] = field(default_factory=list)
    preconditions: str = ""
    references: list[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.skill_id or not self.skill_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"skill_id must be alphanumeric/dash/underscore, got {self.skill_id!r}")
        if not self.version:
            raise ValueError("version is required")
        if not self.guidance.strip():
            raise ValueError("guidance must be non-empty — a skill with no guidance is not a skill")
