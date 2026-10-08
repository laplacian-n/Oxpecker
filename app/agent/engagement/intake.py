"""Structured engagement intake — M5.1. Every reviewer independently ranked this ahead of the
pipeline itself: "engagement metadata ครบตั้งแต่แรก" (hackerai-signature-capabilities.md B3),
"ควรทำก่อน pipeline" (local-security-agent-review-phase4.md §5), because a pipeline built on top
of an unclear or incomplete RoE just automates bad decisions faster.

Produces the exact `roe.json`/`scope.txt`/`deny.txt` shape `agent/broker/policy.py` already
loads — Policy.load() ignores unknown JSON keys, so the richer metadata here (contacts, kill
procedure, credential handles, rate/blast-radius intent, approval policy, retention default)
rides along in roe.json without needing any broker/policy code change. `operator_sign_off` must
be explicitly True — an engagement without an affirmative sign-off cannot be created at all,
matching the doc's "operator confirm ก่อนเริ่ม" requirement structurally, not just as a UI
suggestion.
"""
from __future__ import annotations

import calendar
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config


class IntakeValidationError(RuntimeError):
    pass


@dataclass
class EngagementIntake:
    engagement_id: str
    description: str
    allow_targets: list[str]  # host/CIDR per entry — becomes scope.txt
    valid_from: str  # "YYYY-MM-DDTHH:MM:SSZ"
    valid_until: str
    allowed_action_classes: list[str]
    authorized_by: str
    operator_sign_off: bool

    deny_targets: list[str] = field(default_factory=list)  # additional to the immutable base deny.txt
    credential_handles: list[str] = field(default_factory=list)  # opaque names, never secret values
    rate_limit_notes: str = ""  # free-text intent; actual enforcement is still the global
    # config.ACTION_CLASS_COOLDOWN_S — documented as a real gap below, not silently implied solved
    blast_radius_notes: str = ""
    approval_policy: str = "ask_beyond_passive_recon"  # documents operator intent
    evidence_retention_default: str = "standard"  # must be a agent.evidence.store.RETENTION_DAYS key
    contacts: list[str] = field(default_factory=list)
    kill_procedure: str = "python3 -m agent.main --kill-switch engage"
    created_at: float = field(default_factory=time.time)

    def validate(self) -> None:
        errors = []
        if not self.engagement_id or not self.engagement_id.replace("-", "").replace("_", "").isalnum():
            errors.append("engagement_id must be a non-empty alphanumeric/dash/underscore string")
        if not self.allow_targets:
            errors.append("allow_targets must have at least one entry — an engagement with no "
                           "in-scope target authorizes nothing (default-deny is the point)")
        if not self.allowed_action_classes:
            errors.append("allowed_action_classes must have at least one entry")
        if not self.authorized_by.strip():
            errors.append("authorized_by must name who authorized this engagement")
        if self.operator_sign_off is not True:
            errors.append(
                "operator_sign_off must be explicitly True — an engagement cannot be created "
                "without an affirmative sign-off, this is not a default-yes checkbox"
            )
        try:
            vf = calendar.timegm(time.strptime(self.valid_from, "%Y-%m-%dT%H:%M:%SZ"))
            vu = calendar.timegm(time.strptime(self.valid_until, "%Y-%m-%dT%H:%M:%SZ"))
            if vu <= vf:
                errors.append("valid_until must be after valid_from")
        except ValueError as e:
            errors.append(f"valid_from/valid_until must be 'YYYY-MM-DDTHH:MM:SSZ': {e}")

        from ..evidence.store import RETENTION_DAYS

        if self.evidence_retention_default not in RETENTION_DAYS:
            errors.append(
                f"evidence_retention_default must be one of {list(RETENTION_DAYS)}, "
                f"got {self.evidence_retention_default!r}"
            )

        for target in self.allow_targets:
            _validate_scope_line(target, errors, "allow_targets")
        for target in self.deny_targets:
            _validate_scope_line(target, errors, "deny_targets")

        if errors:
            raise IntakeValidationError("; ".join(errors))


def _validate_scope_line(line: str, errors: list, field_name: str) -> None:
    """Accept exactly what the policy parser will later honour, and reject the rest here.

    This used to carry its own rule (`stripped.replace(".","").replace("-","").isalnum()`),
    which was a third independent opinion about what a scope entry is, and it rejected
    `*.example.com` — the form every bug-bounty scope is written in. Delegating means an entry
    accepted at intake is an entry the broker will enforce, which is the only useful contract
    for this function. Single-label hostnames (`localhost`, a lab box's short name) stay legal:
    the shipped lab engagement uses one.
    """
    from ..broker.policy import ScopeLineError, parse_scope_line

    stripped = (line or "").strip()
    if not stripped:
        errors.append(f"{field_name} contains an empty entry")
        return
    try:
        parsed = parse_scope_line(stripped)
    except ScopeLineError as e:
        errors.append(f"{field_name} entry rejected: {e}")
        return
    if parsed is None:
        # A comment line is legal in a scope FILE but meaningless as an API-supplied target:
        # it would silently contribute nothing to the scope the operator thinks they set.
        errors.append(
            f"{field_name} entry {stripped!r} is a comment, so it authorises nothing"
        )


def create_engagement(
    intake: EngagementIntake, engagements_root: Path = config.ENGAGEMENTS_ROOT
) -> Path:
    """Validates and writes roe.json/scope.txt/deny.txt for a new named engagement. Raises
    IntakeValidationError and writes nothing if validation fails — never a partially-written
    engagement directory."""
    intake.validate()

    engagement_dir = engagements_root / intake.engagement_id
    if engagement_dir.exists():
        raise IntakeValidationError(
            f"engagement {intake.engagement_id!r} already exists at {engagement_dir} — "
            "use a different engagement_id or remove the old one explicitly first"
        )
    engagement_dir.mkdir(parents=True)

    roe = {
        "engagement_id": intake.engagement_id,
        "description": intake.description,
        "authorized_by": intake.authorized_by,
        "authorized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(intake.created_at)),
        "valid_from": intake.valid_from,
        "valid_until": intake.valid_until,
        "allowed_action_classes": intake.allowed_action_classes,
        # Metadata beyond what Policy.load() reads — carried for audit/report/operator
        # reference, ignored (not enforced) by the broker today; see the module docstring's
        # "documented gap" note for what this does and doesn't wire up automatically.
        "credential_handles": intake.credential_handles,
        "rate_limit_notes": intake.rate_limit_notes,
        "blast_radius_notes": intake.blast_radius_notes,
        "approval_policy": intake.approval_policy,
        "evidence_retention_default": intake.evidence_retention_default,
        "contacts": intake.contacts,
        "kill_procedure": intake.kill_procedure,
    }
    (engagement_dir / "roe.json").write_text(json.dumps(roe, indent=2))

    scope_lines = ["# Generated by agent.engagement.intake — allowlist, host/CIDR per line.", ""]
    scope_lines += intake.allow_targets
    (engagement_dir / "scope.txt").write_text("\n".join(scope_lines) + "\n")

    deny_lines = [
        "# Immutable hard-deny additions for this engagement, on top of the base deny list.",
        "# Cloud metadata endpoints are always denied regardless of engagement.",
        "169.254.169.254/32",
        "fd00:ec2::254/128",
        "",
    ]
    deny_lines += intake.deny_targets
    (engagement_dir / "deny.txt").write_text("\n".join(deny_lines) + "\n")

    return engagement_dir


def intake_to_dict(intake: EngagementIntake) -> dict:
    return asdict(intake)
