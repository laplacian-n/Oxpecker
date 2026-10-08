"""The program a session is hunting under, as data the broker can enforce.

Why this exists: in the transcripts this project was designed against, all of this lived as
prose inside a hand-written prompt — 24 targets mapped by stack, the program's focus areas, a
"do not report" list repeated three times, a dedupe strategy derived from prior accepted
submissions. Nothing could validate it, nothing enforced it, and after six context compactions
the model was running on whatever survived the summary.

A `Program` is the same information as fields. `automation_allowed` becomes a gate the broker
applies rather than a rule the operator has to remember; `max_requests_per_min` becomes the
thing that keeps the target reachable tomorrow (an aggressive recon pass in one of those
transcripts earned a Cloudflare IP ban mid-session and lost its most promising host for the
rest of the run); `excluded_vuln_types` becomes a refusal at finding time instead of a line in
a brief.

Deliberately NOT here: credentials, session cookies, or anything a scope check consumes. Scope
lives in the engagement's allow/deny targets and is parsed by `broker.policy.parse_scope_line`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# A program whose terms forbid automated testing forbids it for every action that puts traffic
# on the target, not only for the ones that look like a scanner. A single agent-issued HTTP GET
# is still automated traffic. The classes left out are the ones that touch our own index or a
# public advisory source instead of the engagement's assets.
TARGET_TOUCHING_ACTION_CLASSES = frozenset({
    "passive_recon",
    "http_recon_insecure",
    "active_web_request",
    "active_scan_light",
    "browser_recon",
    "active_write",
})

PLATFORMS = ("bugcrowd", "hackerone", "intigriti", "vdp", "private", "lab")


@dataclass
class Program:
    """What the program says, in a form something can check.

    Every default is the permissive one ONLY where permissiveness is the honest default:
    `automation_allowed=True` because most programs do allow it and an engagement created
    without a program should behave as it did before this type existed, while
    `max_requests_per_min=None` means "no program-specific cap" and leaves the per-action-class
    cooldown as the only limit. A program whose terms you have not read should be created with
    `automation_allowed=False`, not left at the default.
    """

    platform: str = "lab"
    program_url: str = ""
    safe_harbour: str = ""

    automation_allowed: bool = True
    max_requests_per_min: int | None = None

    # Classes the program refuses to receive. Matched case-insensitively against a finding's
    # declared class, as a substring in both directions, because a program writes "XSS" and a
    # finding says "Reflected XSS in search parameter".
    excluded_vuln_types: list[str] = field(default_factory=list)
    focus_areas: list[str] = field(default_factory=list)
    vrt_taxonomy: str = ""

    # host -> classes already accepted on it, from the platform's own activity feed. Read at
    # finding time to warn before a human spends effort on a likely duplicate.
    prior_submissions: list[dict] = field(default_factory=list)
    # Paths or URLs of out-of-scope documents attached to the brief (several programs ship the
    # real exclusion list as a PDF). Recorded so a reader can tell whether it was consulted;
    # this module does not parse them.
    out_of_scope_refs: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Problems with this program record, as messages. Empty means usable.

        Returns rather than raises: the web API collects these alongside the scope errors and
        reports all of them at once, which is the difference between one round trip and five.
        """
        errors: list[str] = []
        if self.platform not in PLATFORMS:
            errors.append(
                f"program.platform {self.platform!r} is not one of {list(PLATFORMS)}"
            )
        if self.platform not in ("lab", "private") and not self.program_url.strip():
            errors.append(
                f"program.program_url is required for platform {self.platform!r} — it is the "
                "record of where the authorization comes from"
            )
        if self.max_requests_per_min is not None and self.max_requests_per_min <= 0:
            errors.append(
                "program.max_requests_per_min must be a positive number of requests, or null "
                "for no program-specific cap; 0 would deny everything while reading as a limit"
            )
        for entry in self.prior_submissions:
            if not isinstance(entry, dict):
                errors.append(f"program.prior_submissions entry {entry!r} is not an object")
        return errors

    def excludes(self, vuln_class: str) -> str | None:
        """The excluded entry that covers `vuln_class`, or None.

        Matched both ways round: a program listing "XSS" must catch a finding called "Reflected
        XSS in the search parameter", and a program listing "Self-XSS requiring user
        interaction" must be caught by a finding called "Self-XSS". One-directional matching
        would let whichever side was phrased more specifically slip through.
        """
        needle = (vuln_class or "").strip().lower()
        if not needle:
            return None
        for raw in self.excluded_vuln_types:
            entry = str(raw).strip().lower()
            if not entry:
                continue
            if entry in needle or needle in entry:
                return str(raw)
        return None

    def duplicate_risk(self, host: str, vuln_class: str) -> list[dict]:
        """Prior accepted submissions on this host that look like this class.

        Advisory, never blocking: a duplicate is the platform's call, and a hunter who found a
        second instance of a class on the same host has something worth submitting. The point is
        that a human sees it before writing the report, not that the tool decides.
        """
        host_l = (host or "").strip().lower()
        needle = (vuln_class or "").strip().lower()
        hits = []
        for entry in self.prior_submissions:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("host", "")).strip().lower() != host_l:
                continue
            classes = entry.get("classes") or []
            if not needle or any(
                needle in str(c).lower() or str(c).lower() in needle for c in classes
            ):
                hits.append(entry)
        return hits

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "program_url": self.program_url,
            "safe_harbour": self.safe_harbour,
            "automation_allowed": self.automation_allowed,
            "max_requests_per_min": self.max_requests_per_min,
            "excluded_vuln_types": list(self.excluded_vuln_types),
            "focus_areas": list(self.focus_areas),
            "vrt_taxonomy": self.vrt_taxonomy,
            "prior_submissions": list(self.prior_submissions),
            "out_of_scope_refs": list(self.out_of_scope_refs),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Program | None":
        """None in, None out — an engagement without a program is a legal state (every local lab
        session), and inventing a default Program here would make the absence invisible."""
        if not data:
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
