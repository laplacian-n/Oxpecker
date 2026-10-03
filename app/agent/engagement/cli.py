"""CLI for M5.1 engagement intake: `python3 -m agent.engagement.cli --from-json spec.json`
or interactively (prompts for each required field, restricted to what's genuinely required —
optional fields keep their dataclass defaults rather than prompting for everything)."""
from __future__ import annotations

import argparse
import json
import sys

from .. import config
from .intake import EngagementIntake, IntakeValidationError, create_engagement


def _from_json(path: str) -> EngagementIntake:
    data = json.loads(open(path).read())
    return EngagementIntake(**data)


def _interactive() -> EngagementIntake:
    def ask(prompt: str, default: str | None = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        val = input(f"{prompt}{suffix}: ").strip()
        return val or (default or "")

    def ask_list(prompt: str) -> list[str]:
        raw = input(f"{prompt} (comma-separated): ").strip()
        return [x.strip() for x in raw.split(",") if x.strip()]

    engagement_id = ask("engagement_id")
    description = ask("description")
    allow_targets = ask_list("allow_targets (host/CIDR)")
    valid_from = ask("valid_from (YYYY-MM-DDTHH:MM:SSZ)")
    valid_until = ask("valid_until (YYYY-MM-DDTHH:MM:SSZ)")
    allowed_action_classes = ask_list("allowed_action_classes")
    authorized_by = ask("authorized_by")
    sign_off_raw = ask("Do you, the operator, affirmatively sign off on this engagement? (yes/no)", "no")

    return EngagementIntake(
        engagement_id=engagement_id,
        description=description,
        allow_targets=allow_targets,
        valid_from=valid_from,
        valid_until=valid_until,
        allowed_action_classes=allowed_action_classes,
        authorized_by=authorized_by,
        operator_sign_off=sign_off_raw.lower() in ("yes", "y", "true"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a new engagement (M5.1 intake).")
    parser.add_argument("--from-json", metavar="PATH", help="Load intake fields from a JSON file")
    args = parser.parse_args(argv)

    intake = _from_json(args.from_json) if args.from_json else _interactive()

    try:
        eng_dir = create_engagement(intake, engagements_root=config.ENGAGEMENTS_ROOT)
    except IntakeValidationError as e:
        print(f"REJECTED: {e}", file=sys.stderr)
        return 1

    print(f"Engagement created: {eng_dir}")
    print(f"Use with: Broker(engagement_dir={str(eng_dir)!r}) / load_policy(engagement_dir={str(eng_dir)!r})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
