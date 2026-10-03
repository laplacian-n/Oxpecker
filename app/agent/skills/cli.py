"""Offline, human-run skill authoring tool — the only way a skill enters
`agent/skills/library/`. `SkillLibrary` (library.py) never writes; this is that write path,
deliberately outside anything a live engagement can reach.

Usage:
    python3 -m agent.skills.cli new --id my-skill --version 1.0 ...   (writes + signs)
    python3 -m agent.skills.cli sign agent/skills/library/my-skill.yaml (re-sign after a hand-edit)
    python3 -m agent.skills.cli verify-all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .library import LIBRARY_DIR, SkillLibrary
from .schema import Skill
from .signing import sign_file, verify_file


def cmd_sign(args) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1
    Skill(**yaml.safe_load(path.read_text()))  # validates schema before signing garbage
    sig_path = sign_file(path)
    print(f"signed: {sig_path}")
    return 0


def cmd_verify_all(args) -> int:
    lib = SkillLibrary()
    skills, problems = lib.load_all(verify=True)
    print(f"{len(skills)} valid skill(s)")
    for p in problems:
        print(f"PROBLEM: {p['path']}: {p['reason']}", file=sys.stderr)
    return 1 if problems else 0


def cmd_new(args) -> int:
    skill = Skill(
        skill_id=args.id,
        version=args.version,
        title=args.title,
        description=args.description,
        guidance=args.guidance,
        applicable_phases=args.phases.split(",") if args.phases else [],
        applicable_profiles=args.profiles.split(",") if args.profiles else [],
        author=args.author,
    )
    path = LIBRARY_DIR / f"{skill.skill_id}.yaml"
    if path.exists():
        print(f"already exists: {path} — bump --version and edit by hand instead", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(skill.__dict__, sort_keys=False))
    sign_file(path)
    print(f"created and signed: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline skill-library authoring tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_new = sub.add_parser("new")
    p_new.add_argument("--id", required=True)
    p_new.add_argument("--version", required=True)
    p_new.add_argument("--title", required=True)
    p_new.add_argument("--description", required=True)
    p_new.add_argument("--guidance", required=True)
    p_new.add_argument("--phases", default="")
    p_new.add_argument("--profiles", default="")
    p_new.add_argument("--author", default="")
    p_new.set_defaults(func=cmd_new)

    p_sign = sub.add_parser("sign")
    p_sign.add_argument("path")
    p_sign.set_defaults(func=cmd_sign)

    p_verify = sub.add_parser("verify-all")
    p_verify.set_defaults(func=cmd_verify_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
