"""Turn a claude-red checkout (SnailSploit/claude-red, MIT) into the vendored technique corpus.

claude-red is 79 offensive-security "Skills" — one `SKILL.md` per technique family (SQLi, SSRF,
Active Directory, cloud, …), each a few hundred lines of methodology + payloads. We want that
knowledge in the agent's cross-engagement technique KB (notebook/technique_kb.py) so the strategist
and workers can *choose* a relevant technique from a real catalogue, instead of the pipeline only
ever seeing the mechanical header/cookie hypotheses recon_seed.py extracts.

The technique KB rebuilds a TF-IDF index over its whole corpus on every turn
(NotebookService.build_context_block -> semantic_recall), so it is sized for "dozens to hundreds"
of entries, not thousands. Dumping all ~1000 `##` sections would blow that assumption and drown
the operator's own notes. So this builds ONE concise *card* per skill — the frontmatter
description (what it is / when to use it) plus a compact methodology + payload digest — capped so a
single `technique_recall` result stays within the tool-result budget. The card is a pointer the
agent acts on; the full SKILL.md stays in the checkout for a future embeddings-RAG pass.

Output is `techniques.jsonl` next to this file: one card per line, vendored and reviewable, so the
loader (load_claude_red.py) needs no network and the corpus is pinned to a reviewed commit.

Usage:
    python -m agent.knowledge.claude_red.build /path/to/claude-red
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_PATH = HERE / "techniques.jsonl"

# Body budget per card: generous enough to carry the workflow + a payload sample, small enough that
# one technique_recall result survives MAX_SINGLE_RESULT_TOKENS (1000) truncation mostly intact.
_BODY_CAP = 1800

# claude-red category dir -> the engagement "surface" vocabulary the KB matches against. A card is
# auto-surfaced when the session's notes mention its surface, so this is what ties "I'm looking at a
# web form" to the SQLi/XSS cards. Left-anchored substrings; several surfaces per category is fine.
_CATEGORY_SURFACES = {
    "web": ["web", "http", "url", "form", "endpoint", "api", "parameter", "cookie", "header"],
    "api": ["api", "rest", "graphql", "endpoint", "json", "http"],
    "auth": ["auth", "login", "session", "token", "jwt", "oauth", "saml", "password"],
    "cloud": ["cloud", "aws", "azure", "gcp", "s3", "iam", "metadata"],
    "container": ["container", "docker", "kubernetes", "k8s", "pod"],
    "network": ["network", "port", "service", "tcp", "smb", "dns"],
    "infrastructure": ["infrastructure", "server", "service", "host", "port"],
    "recon": ["recon", "osint", "subdomain", "enumeration", "discovery"],
    "active-directory": ["active-directory", "ad", "kerberos", "ldap", "domain", "windows"],
    "privesc": ["privesc", "privilege", "sudo", "suid", "escalation"],
    "post-exploitation": ["post-exploitation", "persistence", "lateral", "pivot"],
    "crypto": ["crypto", "tls", "cipher", "hash", "certificate"],
    "mobile": ["mobile", "android", "ios", "apk"],
    "wireless": ["wireless", "wifi", "wpa", "bluetooth", "radio"],
    "iot": ["iot", "firmware", "embedded", "device"],
    "exploit-dev": ["exploit", "binary", "memory", "overflow", "rop"],
    "fuzzing": ["fuzzing", "fuzz", "crash", "coverage"],
    "supply-chain": ["supply-chain", "dependency", "package", "ci", "pipeline"],
    "cicd": ["cicd", "ci", "pipeline", "build", "jenkins", "github-actions"],
    "social-engineering": ["social-engineering", "phishing", "pretext"],
    "forensics": ["forensics", "log", "artifact", "memory-image"],
    "ai": ["ai", "llm", "prompt", "model"],
    "utility": ["utility", "tooling"],
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^name:\s*(.+)$", re.MULTILINE)
_DESC_RE = re.compile(r'^description:\s*"?(.*?)"?\s*$', re.MULTILINE | re.DOTALL)
# Sections that are defensive/reference noise for an offensive agent — skipped when digesting a body.
_SKIP_SECTION = re.compile(
    r"^(detection\s*&\s*monitoring|monitoring|blue\s*team|references?|key\s*references?|"
    r"remediation|mitigation|defen[cs]e)", re.I)


def _parse_frontmatter(text: str) -> tuple[str, str, str]:
    """(name, description, body-after-frontmatter). Missing fields come back empty."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", "", text
    fm, body = m.group(1), text[m.end():]
    name = (_NAME_RE.search(fm) or [None, ""])[1].strip() if _NAME_RE.search(fm) else ""
    desc = _DESC_RE.search(fm).group(1).strip() if _DESC_RE.search(fm) else ""
    desc = re.sub(r"\s+", " ", desc).strip().strip('"')
    return name, desc, body


def _digest_body(body: str) -> str:
    """A compact offensive digest: the first substantive `##` sections (workflow + payloads),
    skipping defensive/reference sections, capped at `_BODY_CAP` chars on a line boundary."""
    # Split into (heading, content) on level-2 headings; keep the lead-in before the first heading.
    parts = re.split(r"\n(?=##\s)", body)
    kept: list[str] = []
    for part in parts:
        head = part.lstrip("#").strip().splitlines()[0] if part.strip() else ""
        if _SKIP_SECTION.match(head):
            continue
        kept.append(part.strip())
        if sum(len(p) for p in kept) > _BODY_CAP:
            break
    digest = "\n\n".join(kept).strip()
    if len(digest) > _BODY_CAP:
        cut = digest.rfind("\n", 0, _BODY_CAP)
        digest = digest[: cut if cut > _BODY_CAP // 2 else _BODY_CAP].rstrip() + "\n…"
    return digest


def _tags(name: str, category: str) -> list[str]:
    """Keyword tags: the category, plus the meaningful words of the skill name (minus 'offensive')."""
    words = [w for w in re.split(r"[-_/\s]+", name.lower()) if len(w) > 2 and w != "offensive"]
    return sorted({category, *words})


def build(src_root: Path) -> list[dict]:
    skills_dir = src_root / "Skills"
    if not skills_dir.is_dir():
        raise SystemExit(f"no Skills/ under {src_root} — is this a claude-red checkout?")
    cards: list[dict] = []
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        rel = skill_md.relative_to(skills_dir)
        category = rel.parts[0]
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        name, desc, body = _parse_frontmatter(text)
        name = name or rel.parts[-2]
        digest = _digest_body(body)
        title = name.replace("offensive-", "").replace("-", " ").strip().title()
        # Body the agent reads: what/when (description) first, then the how (digest).
        card_body = (desc + "\n\n" + digest).strip() if desc else digest
        cards.append({
            "name": name,
            "category": category,
            "title": f"{title} — offensive methodology",
            "description": desc,
            "body": card_body,
            "tags": _tags(name, category),
            "surfaces": _CATEGORY_SURFACES.get(category, [category]),
            "source": f"claude-red/{rel.as_posix()}",
        })
    return cards


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    cards = build(Path(argv[1]).resolve())
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        for c in cards:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    total = sum(len(c["body"]) for c in cards)
    print(f"wrote {len(cards)} cards to {OUT_PATH} ({total} body chars, "
          f"avg {total // max(1, len(cards))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
