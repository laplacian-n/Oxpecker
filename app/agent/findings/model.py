"""Structured findings — §8d: internal JSON model as source of truth, SARIF and Markdown/PDF
are things rendered *from* it, not native formats.

Schema expanded per the 2026-08-31 review set (01-security-agent-main-direction.md M4.3,
local-security-agent-review-phase4.md §B4, 03-codex-independent-opinion.md §8.2's evidence-class
framing) — all three independently converge on separating *confidence* from *severity* from
*demonstrated impact*, and on requiring CWE/CVE/limitations fields a report can't honestly omit.
`confidence`/`status` default to the most conservative values (`needs_validation`) rather than
`confirmed`, matching the reviewers' shared point that a model must never self-promote a finding
to confirmed without going through a verification rule — Phase 4 doesn't have that verification
pipeline yet (§ observation → hypothesis → verification → finding separation is Phase 5 work,
tracked in docs/ROADMAP.md), so the schema is ready for it but nothing here enforces it
automatically yet; callers are responsible for only marking `status="confirmed"` when they
actually have verified evidence, same as before this expansion, just now with a field that says
so explicitly instead of an implicit assumption baked into every finding being "confirmed."
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config


class FindingNotFoundError(LookupError):
    pass


def _write_atomic(path: Path, content: str) -> None:
    """Same write-to-temp-then-os.replace() pattern as agent/broker/approval_queue.py's
    _write_atomic — a concurrent reader (list_all() from another thread/request) must never
    observe a truncated file mid-rewrite, which a plain write_text() doesn't guarantee."""
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp_path.write_text(content)
    os.replace(tmp_path, path)

VALID_SEVERITIES = ("info", "low", "medium", "high", "critical")
VALID_CONFIDENCE = ("confirmed", "hypothesis", "needs_validation")
VALID_STATUS = ("confirmed", "hypothesis", "needs_validation", "false_positive", "wont_fix")

# §8.6.4 — the assurance ladder: who raised confidence in the finding, not whether its claim is
# true. The order is the ladder (low -> high): a model's own assertion is the floor and passes at
# every tier; a rule match, a differential (a different model instance / comparison rule agreeing),
# a downstream use that worked, or a human sign-off each strengthen it. Assurance is a *property*,
# not a gate (§8.6.4): a finding is never blocked for being model-confirmed, only labelled — and
# the submission boundary (§8.6.5 #1) is what keeps a model-only finding from a submitted report
# unseen. A worker cannot raise its own level (§14.1 A): every level above `model` is awarded by
# something other than the finder.
CONFIRMED_BY_LEVELS = ("model", "rule", "differential", "downstream", "human")
CONFIRMED_BY_FLOOR = "model"


def assurance_rank(level: str) -> int:
    """The ladder position of a `confirmed_by` level, for comparison. Raises on an unknown level
    rather than ranking it 0 — an unrecognised assurance label is a bug, not the floor."""
    try:
        return CONFIRMED_BY_LEVELS.index(level)
    except ValueError:
        raise ValueError(f"unknown confirmed_by level {level!r}; expected one of {CONFIRMED_BY_LEVELS}")


@dataclass
class Finding:
    title: str
    severity: str  # one of VALID_SEVERITIES
    target: str
    description: str
    remediation: str
    tool: str
    session_id: str
    engagement_id: str
    finding_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Confidence/status — kept separate from severity per the review set: severity is "how bad
    # if true," confidence/status is "how sure are we this is true." Defaults are deliberately
    # the most conservative option, not "confirmed" — see module docstring.
    confidence: str = "needs_validation"
    status: str = "needs_validation"
    demonstrated_impact: str | None = None  # what was actually shown to work, not theorized

    # §8.6.4 assurance ladder — WHO raised confidence, orthogonal to severity (how bad) and
    # confidence/status (how sure). Defaults to the floor, `model`: a model's own assertion, which
    # passes at every tier but cannot cross the submission boundary unseen (§8.6.5 #1). Levels above
    # `model` are awarded by something other than the finder (a rule, a different-family verifier, a
    # downstream use, a human) — a worker can assert, it cannot promote its own assurance (§14.1 A).
    confirmed_by: str = CONFIRMED_BY_FLOOR
    # §8.6.4.1 — set to `rule_id@version` when a rule returned False against evidence the model
    # called positive. The disagreement is the rule library's only bug report, and the label keeps
    # a model-over-rule confirmation distinguishable from an unchallenged one at export (§8.3).
    rule_disagreed: str | None = None

    # CVSS — score kept as the existing float field (backward compatible with records written
    # before this expansion); version/vector added alongside it, not replacing it.
    cvss: float | None = None
    cvss_version: str | None = None
    cvss_vector: str | None = None

    cwe_ids: list[str] = field(default_factory=list)
    cve_ids: list[str] = field(default_factory=list)
    affected_component: str | None = None
    preconditions: str | None = None

    # Reference chain: observation_refs point at raw tool observations (no observation store
    # exists yet — Phase 5 — so this stays empty in practice for now, but the field exists so
    # findings written today don't need a schema migration once one does).
    observation_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)  # evidence-store digests
    audit_refs: list[str] = field(default_factory=list)  # audit-log entry hashes
    reproduction_recipe_ref: str | None = None
    references: list[str] = field(default_factory=list)  # external URLs/citations

    first_seen: float = field(default_factory=time.time)
    last_verified: float | None = None
    verifier: str = "unspecified"  # device_id/operator identity that last verified this
    limitations: str | None = None

    discovered_at: float = field(default_factory=time.time)  # kept for backward compatibility

    # Human review gate (added — nothing above enforces `status`/`confidence`/`verifier` were
    # ever actually looked at by a person; a model can write "confirmed" to those fields itself).
    # None until a human explicitly reviews it via FindingsStore.mark_reviewed() — never set by
    # the model-facing record_finding tool, only by an operator action (web UI / CLI). Report
    # rendering (agent/findings/report.py) surfaces unreviewed findings prominently rather than
    # silently treating "recorded" as "ready to deliver."
    reviewed_by: str | None = None
    reviewed_at: float | None = None

    def __post_init__(self):
        if self.severity not in VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {self.severity!r}")
        if self.confidence not in VALID_CONFIDENCE:
            raise ValueError(f"confidence must be one of {VALID_CONFIDENCE}, got {self.confidence!r}")
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}, got {self.status!r}")
        if self.confirmed_by not in CONFIRMED_BY_LEVELS:
            raise ValueError(
                f"confirmed_by must be one of {CONFIRMED_BY_LEVELS}, got {self.confirmed_by!r}"
            )

    def to_dict(self) -> dict:
        return asdict(self)


def submission_blocker(finding: "Finding") -> str | None:
    """§8.6.5 #1 — the absolute, tier-independent submission boundary. A false positive circulating
    internally is cheap and self-correcting; one that reaches a *submitted report* is reputation,
    and programs ban for repeated invalid reports. So a finding whose assurance is only `model`
    cannot cross into a submission without a person seeing it (`reviewed_by` set). Assurance above
    the model floor — a rule, a differential verifier pass, a downstream use, a human sign-off —
    crosses on its own: full autonomy inside, the expensive edge guarded. Returns the reason the
    finding is blocked from submission, or None if it may be submitted.

    No tier can lower this (§2.6.1-adjacent): the boundary is a property of the finding, read the
    same way whoever is about to submit it — the reporter, an export, an operator action."""
    if finding.status in ("false_positive", "wont_fix"):
        return f"status {finding.status!r} is not a submittable finding"
    if assurance_rank(finding.confirmed_by) > assurance_rank(CONFIRMED_BY_FLOOR):
        return None  # above the model floor — awarded by something other than the finder, so it crosses
    if finding.reviewed_by:
        return None  # a person saw it (the review gate satisfies "without a person seeing it")
    return (
        "confirmed_by: model has not been seen by a person — it cannot cross the submission "
        "boundary unseen (§8.6.5 #1); have an operator review it or raise its assurance"
    )


def can_submit(finding: "Finding") -> bool:
    return submission_blocker(finding) is None


def unsubmittable(findings: "list[Finding]") -> "list[tuple[Finding, str]]":
    """The findings in `findings` that may NOT be submitted, each with the reason — what a reporter
    or export calls at the boundary to hold back model-only, unreviewed findings (and shelved
    ones) rather than discovering them in a delivered report."""
    blocked = []
    for f in findings:
        reason = submission_blocker(f)
        if reason is not None:
            blocked.append((f, reason))
    return blocked


class FindingsStore:
    def __init__(self, engagement_id: str, findings_dir: Path = config.FINDINGS_DIR):
        findings_dir.mkdir(parents=True, exist_ok=True)
        self.engagement_id = engagement_id
        self.path = findings_dir / f"{engagement_id}.jsonl"

    def add(self, finding: Finding) -> Finding:
        with self.path.open("a") as f:
            f.write(json.dumps(finding.to_dict()) + "\n")
        # §4.2 finding_recorded — the findings tab's count reacts to this. Imported lazily to
        # keep this leaf model module free of an engagement-package import at module load.
        from ..engagement import emit as event_emit
        event_emit.emit(self.engagement_id, "finding_recorded", {"finding_id": finding.finding_id})
        return finding

    def list_all(self) -> list[Finding]:
        if not self.path.exists():
            return []
        return [
            Finding(**json.loads(line))
            for line in self.path.read_text().splitlines()
            if line.strip()
        ]

    def get(self, finding_id: str) -> Finding:
        for f in self.list_all():
            if f.finding_id == finding_id:
                return f
        raise FindingNotFoundError(f"no finding {finding_id!r} in {self.path}")

    def record_verification(
        self, finding_id: str, *, verdict: str, reason: str | None, rationale: str,
        by: str, new_status: str | None = None, confirmed_by: str | None = None,
    ) -> Finding:
        """Record a verifier's verdict on a finding (§2.3). Unlike `mark_reviewed` (a *human* gate),
        this is the independent verifier's result: it stamps `verifier`/`last_verified`, appends the
        verdict to `limitations` so the report carries *why* the finding stands or fell, and — only
        when the verdict calls for it — moves `status` (a refutation to `false_positive`). It never
        touches `reviewed_by`: a verifier pass is not a human review, and must not look like one.

        `confirmed_by`, when given, raises the assurance level — but only ever *raises* it (§14.1 A:
        a worker cannot raise its own level, and assurance never silently drops because a later pass
        was weaker). A different-family verifier that could not refute is a differential pass, so the
        gate passes `differential`; an already-downstream/human finding keeps its higher level.
        Same atomic whole-file rewrite as mark_reviewed (JSONL has no in-place update)."""
        if not by or not by.strip():
            raise ValueError("verifier identity (`by`) must be non-empty, not silently 'someone'")
        if new_status is not None and new_status not in VALID_STATUS:
            raise ValueError(f"new_status must be one of {VALID_STATUS}, got {new_status!r}")
        if confirmed_by is not None and confirmed_by not in CONFIRMED_BY_LEVELS:
            raise ValueError(f"confirmed_by must be one of {CONFIRMED_BY_LEVELS}, got {confirmed_by!r}")
        findings = self.list_all()
        updated = None
        rewritten = []
        note = f"[verifier:{verdict}" + (f"/{reason}" if reason else "") + "]"
        if rationale and rationale.strip():
            note = f"{note} {rationale.strip()}"
        for f in findings:
            if f.finding_id == finding_id:
                f.verifier = by.strip()
                f.last_verified = time.time()
                f.limitations = f"{f.limitations} | {note}" if f.limitations else note
                if new_status is not None:
                    f.status = new_status
                if confirmed_by is not None and assurance_rank(confirmed_by) > assurance_rank(f.confirmed_by):
                    f.confirmed_by = confirmed_by  # raise only — never lower an earned assurance
                updated = f
            rewritten.append(f)
        if updated is None:
            raise FindingNotFoundError(f"no finding {finding_id!r} in {self.path}")
        _write_atomic(self.path, "".join(json.dumps(f.to_dict()) + "\n" for f in rewritten))
        return updated

    def mark_reviewed(self, finding_id: str, reviewed_by: str) -> Finding:
        """The human review gate (see Finding.reviewed_by's own comment) — an operator action,
        never something the model-facing record_finding tool calls itself. JSONL has no
        in-place update, so this rewrites the whole file atomically (_write_atomic): read every
        finding, replace the matching one, write all of them back. Fine at this project's scale
        (a findings-per-engagement file, not a high-write-volume log)."""
        if not reviewed_by or not reviewed_by.strip():
            raise ValueError("reviewed_by must be a non-empty identity, not silently 'someone'")
        findings = self.list_all()
        updated = None
        rewritten = []
        for f in findings:
            if f.finding_id == finding_id:
                f.reviewed_by = reviewed_by.strip()
                f.reviewed_at = time.time()
                updated = f
            rewritten.append(f)
        if updated is None:
            raise FindingNotFoundError(f"no finding {finding_id!r} in {self.path}")
        _write_atomic(self.path, "".join(json.dumps(f.to_dict()) + "\n" for f in rewritten))
        return updated
