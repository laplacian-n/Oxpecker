"""Human-readable report renderers — Markdown (source) and PDF (rendered from the Markdown),
per §8d. Structure loosely follows PTES/WSTG report shape: executive summary (severity counts),
then one section per finding with evidence/audit references so a reader can trace every claim
back to its source.
"""
from __future__ import annotations

import re
import time

from fpdf import FPDF

from .model import Finding, submission_blocker

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def render_markdown(findings: list[Finding], engagement_id: str) -> str:
    findings = sorted(findings, key=lambda f: _SEVERITY_ORDER[f.severity])
    counts = {sev: 0 for sev in _SEVERITY_ORDER}
    for f in findings:
        counts[f.severity] += 1
    reviewed_count = sum(1 for f in findings if f.reviewed_by)

    lines = [
        f"# Security Findings Report — {engagement_id}",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        "",
        "## Executive Summary",
        "",
        f"{len(findings)} finding(s): "
        + ", ".join(f"{counts[s]} {s}" for s in _SEVERITY_ORDER if counts[s]),
        "",
    ]
    # A finding recorded by the model is not the same thing as a finding a human has reviewed —
    # nothing upstream of this renderer enforces that distinction (see Finding.reviewed_by's own
    # comment), so the report itself has to say so loudly rather than let "in the report" read
    # as "ready to hand to a client."
    if findings and reviewed_count < len(findings):
        lines += [
            f"> ⚠️ **{len(findings) - reviewed_count} of {len(findings)} finding(s) have NOT "
            "been reviewed by a human.** Do not deliver this report until every finding below "
            "is reviewed (mark reviewed via the web UI or `FindingsStore.mark_reviewed()`).",
            "",
        ]
    elif findings:
        lines += ["> ✅ All findings below have been human-reviewed.", ""]
    # §8.6.5 #1 — split on the submission boundary. Findings the boundary blocks go first, under a
    # heading that says why they are not deliverable; the rest go under "Submittable findings".
    # Severity order is preserved within each group.
    blocked: list[tuple[Finding, str]] = []
    submittable: list[Finding] = []
    for f in findings:
        reason = submission_blocker(f)
        if reason is None:
            submittable.append(f)
        else:
            blocked.append((f, reason))

    lines += ["## ⚠ Not ready for submission (§8.6.5 #1)", ""]
    if blocked:
        lines += [
            "These findings may not be submitted as they stand. Each one needs a human review or a "
            "higher assurance level before it can leave the engagement.",
            "",
        ]
        for f, reason in blocked:
            lines += _finding_lines(f)
            lines += [f"> **Submission blocked:** {reason}", ""]
    else:
        lines += ["_None._", ""]

    lines += ["## Submittable findings", ""]
    if submittable:
        for f in submittable:
            lines += _finding_lines(f)
    else:
        lines += ["_None._", ""]
    return "\n".join(lines)


def _finding_lines(f: Finding) -> list[str]:
    cvss_line = f.cvss if f.cvss is not None else "n/a"
    if f.cvss_vector:
        cvss_line = f"{cvss_line} ({f.cvss_version or 'CVSS'}: {f.cvss_vector})"
    review_line = (
        f"✅ Reviewed by {f.reviewed_by} at "
        f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(f.reviewed_at))}"
        if f.reviewed_by else "⚠️ **NOT REVIEWED BY A HUMAN**"
    )
    lines = [
        f"### [{f.severity.upper()}] {f.title}",
        "",
        f"*{review_line}*",
        "",
        f"- **Finding ID:** `{f.finding_id}`",
        f"- **Status:** {f.status}  |  **Confidence:** {f.confidence}",
        f"- **Assurance (confirmed_by):** `{f.confirmed_by}`",
    ]
    if f.rule_disagreed:
        lines.append(f"- **Rule disagreed:** `{f.rule_disagreed}`")
    lines += [
        f"- **Target:** `{f.target}`" + (f"  |  **Component:** {f.affected_component}" if f.affected_component else ""),
        f"- **Tool:** `{f.tool}`",
        f"- **CVSS:** {cvss_line}",
    ]
    if f.cwe_ids:
        lines.append(f"- **CWE:** {', '.join(f.cwe_ids)}")
    if f.cve_ids:
        lines.append(f"- **CVE:** {', '.join(f.cve_ids)}")
    lines += [
        f"- **First seen:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(f.first_seen))}"
        + (f"  |  **Last verified:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(f.last_verified))} by {f.verifier}" if f.last_verified else ""),
        "",
        "**Description**",
        "",
        f.description,
        "",
    ]
    if f.preconditions:
        lines += ["**Preconditions**", "", f.preconditions, ""]
    if f.demonstrated_impact:
        lines += ["**Demonstrated impact**", "", f.demonstrated_impact, ""]
    lines += [
        "**Remediation**",
        "",
        f.remediation,
        "",
    ]
    if f.limitations:
        lines += ["**Limitations**", "", f.limitations, ""]
    if f.references:
        lines += ["**References**", ""]
        lines += [f"- {ref}" for ref in f.references]
        lines += [""]
    if f.reproduction_recipe_ref:
        lines += [f"**Reproduction recipe:** `{f.reproduction_recipe_ref}`", ""]
    if f.observation_refs:
        lines += ["**Observation references:**", ""]
        lines += [f"- `{ref}`" for ref in f.observation_refs]
        lines += [""]
    if f.evidence_refs:
        lines += ["**Evidence references** (encrypted evidence store digests):", ""]
        lines += [f"- `{ref}`" for ref in f.evidence_refs]
        lines += [""]
    if f.audit_refs:
        lines += ["**Audit trail references** (tamper-evident log entry hashes):", ""]
        lines += [f"- `{ref}`" for ref in f.audit_refs]
        lines += [""]
    return lines


# Unicode PDF fonts — already installed system-wide (Ubuntu's fonts-noto-core /
# fonts-noto-cjk-adjacent package family, SIL Open Font License), not downloaded for this
# project. Two families because fpdf2 has no automatic per-glyph font fallback: Noto Sans Thai
# only maps Thai-script codepoints (confirmed empirically — it has no Latin/digit/punctuation
# glyphs at all), so mixed Thai/English text needs explicit per-run font switching, done in
# _script_runs()/render_pdf() below. This replaces the earlier Latin-1-transliteration approach
# entirely — a real Unicode font renders Thai (and any other non-Latin-1 text) correctly instead
# of degrading it to ASCII approximations.
_FONT_LATIN = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
_FONT_LATIN_BOLD = "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"
_FONT_THAI = "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf"
_FONT_THAI_BOLD = "/usr/share/fonts/truetype/noto/NotoSansThai-Bold.ttf"

_THAI_RE = re.compile(r"[฀-๿]+")


def _script_runs(text: str) -> list[tuple[str, str]]:
    """Splits text into consecutive (script, substring) runs, script in {"thai", "latin"}.
    Whitespace/punctuation between scripts is attached to whichever side it's adjacent to via
    the regex split below, which is good enough for readable line wrapping (perfect script
    boundary attribution isn't necessary for rendering)."""
    runs = []
    pos = 0
    for m in _THAI_RE.finditer(text):
        if m.start() > pos:
            runs.append(("latin", text[pos:m.start()]))
        runs.append(("thai", m.group(0)))
        pos = m.end()
    if pos < len(text):
        runs.append(("latin", text[pos:]))
    return runs or [("latin", "")]


def _set_font(pdf: FPDF, bold: bool, size: float, script: str) -> None:
    family = "NotoSansThai" if script == "thai" else "NotoSans"
    pdf.set_font(family, "B" if bold else "", size)


def _write_line(pdf: FPDF, text: str, *, bold: bool, size: float, line_h: float) -> None:
    """Writes one logical line, switching fonts per script run, then advances to the next line
    at the left margin (matching multi_cell's new_x=LMARGIN, new_y=NEXT behavior, which plain
    write() doesn't do on its own)."""
    if not text:
        pdf.ln(line_h)
        return
    for script, run in _script_runs(text):
        if not run:
            continue
        _set_font(pdf, bold, size, script)
        pdf.write(line_h, run)
    pdf.ln(line_h)


def render_pdf(markdown_text: str, out_path: str) -> None:
    """A plain, readable rendering — not a Markdown-feature-complete converter. Headings
    (#, ##, ###), bullet lines, and bold markers are handled; everything else is body text.
    Renders Thai and Latin text correctly via real Unicode fonts (see _FONT_* above) rather
    than transliterating non-ASCII characters away."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("NotoSans", "", _FONT_LATIN)
    pdf.add_font("NotoSans", "B", _FONT_LATIN_BOLD)
    pdf.add_font("NotoSansThai", "", _FONT_THAI)
    pdf.add_font("NotoSansThai", "B", _FONT_THAI_BOLD)
    pdf.add_page()

    for raw_line in markdown_text.split("\n"):
        line = raw_line.strip("\n")
        stripped = line.strip()
        text = stripped.replace("**", "").replace("`", "")
        if stripped.startswith("### "):
            _write_line(pdf, text[4:], bold=True, size=12, line_h=8)
        elif stripped.startswith("## "):
            _write_line(pdf, text[3:], bold=True, size=14, line_h=9)
        elif stripped.startswith("# "):
            _write_line(pdf, text[2:], bold=True, size=18, line_h=10)
        elif stripped.startswith("- "):
            _write_line(pdf, "  - " + text[2:], bold=False, size=10, line_h=6)
        elif stripped == "":
            pdf.ln(3)
        else:
            _write_line(pdf, text, bold=False, size=10, line_h=6)
    pdf.output(out_path)
