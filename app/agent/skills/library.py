"""M5.4 — read-only, signature-verified skill library loader. This module exposes no write
path: adding or changing a skill is only possible through `cli.py` run by a human operator
offline (the same "tool scaffolding stays an offline, human-reviewed workflow" principle the
ROADMAP's Phase 7 section states for tools, applied here to skills) — nothing reachable from a
live engagement (the pipeline orchestrator, the agent loop, any tool) can write into
`agent/skills/library/`, so target or model content cannot end up "in the library that
production uses" even by accident, not just by policy.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .schema import Skill
from .signing import verify_file

LIBRARY_DIR = Path(__file__).resolve().parent / "library"


class SkillNotFoundError(RuntimeError):
    pass


class SkillIntegrityError(RuntimeError):
    pass


def _load_skill_file(path: Path) -> Skill:
    data = yaml.safe_load(path.read_text())
    return Skill(**data)


class SkillLibrary:
    def __init__(self, library_dir: Path = LIBRARY_DIR, signing_key: bytes | None = None):
        self.library_dir = library_dir
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.signing_key = signing_key

    def load_all(self, verify: bool = True) -> tuple[list[Skill], list[dict]]:
        """Returns (valid_skills, problems). A problem is never silently dropped — each is
        {"path": ..., "reason": ...} so an operator can see exactly what failed to load and why,
        matching this project's existing "no silently-swallowed failure" pattern."""
        skills: list[Skill] = []
        problems: list[dict] = []
        for path in sorted(self.library_dir.glob("*.yaml")):
            if verify and not verify_file(path, key=self.signing_key):
                problems.append({"path": str(path), "reason": "missing or invalid signature"})
                continue
            try:
                skills.append(_load_skill_file(path))
            except Exception as e:  # malformed YAML or schema violation — never a silent skip
                problems.append({"path": str(path), "reason": f"{type(e).__name__}: {e}"})
        return skills, problems

    def get(self, skill_id: str, verify: bool = True) -> Skill:
        skills, problems = self.load_all(verify=verify)
        for skill in skills:
            if skill.skill_id == skill_id:
                return skill
        for problem in problems:
            if Path(problem["path"]).stem == skill_id:
                raise SkillIntegrityError(f"skill {skill_id!r} failed to load: {problem['reason']}")
        raise SkillNotFoundError(skill_id)

    def for_phase_and_profile(self, phase: str, profile: str, verify: bool = True) -> list[Skill]:
        skills, _ = self.load_all(verify=verify)
        return [
            s for s in skills
            if (not s.applicable_phases or phase in s.applicable_phases)
            and (not s.applicable_profiles or profile in s.applicable_profiles)
        ]
