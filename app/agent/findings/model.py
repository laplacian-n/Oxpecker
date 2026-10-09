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

    def to_dict(self) -> dict:
        return asdict(self)


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
        by: str, new_status: str | None = None,
    ) -> Finding:
        """Record a verifier's verdict on a finding (§2.3). Unlike `mark_reviewed` (a *human* gate),
        this is the independent verifier's result: it stamps `verifier`/`last_verified`, appends the
        verdict to `limitations` so the report carries *why* the finding stands or fell, and — only
        when the verdict calls for it — moves `status` (a refutation to `false_positive`). It never
        touches `reviewed_by`: a verifier pass is not a human review, and must not look like one.
        Same atomic whole-file rewrite as mark_reviewed, for the same reason (JSONL has no in-place
        update)."""
        if not by or not by.strip():
            raise ValueError("verifier identity (`by`) must be non-empty, not silently 'someone'")
        if new_status is not None and new_status not in VALID_STATUS:
            raise ValueError(f"new_status must be one of {VALID_STATUS}, got {new_status!r}")
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
