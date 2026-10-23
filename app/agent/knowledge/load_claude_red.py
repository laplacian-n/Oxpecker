"""Load the vendored claude-red technique cards into a technique KB.

The cards (agent/knowledge/claude_red/techniques.jsonl, built by claude_red/build.py) are offensive
methodology summaries — SQLi, SSRF, Active Directory, cloud, … — one per upstream skill. Loading
them into the cross-engagement technique KB (notebook/technique_kb.py) means every turn's
`semantic_recall` can surface a relevant one against what the session is looking at, so the agent
*chooses* a technique from a real catalogue instead of the pipeline only ever seeing the mechanical
header/cookie hypotheses recon_seed.py extracts.

Idempotent: TechniqueKB.save() dedups on normalized title|body, so re-running merges rather than
duplicating. Each card is tagged with `source_engagement="claude-red"` so it is distinguishable in
the KB and never excluded by the per-engagement auto-surface filter (which only drops the *running*
engagement's own notes).

Usage:
    python -m agent.knowledge.load_claude_red                 # into the operator KB
    python -m agent.knowledge.load_claude_red --account <id>  # into one account's KB
    python -m agent.knowledge.load_claude_red --db /path.db   # into an explicit db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import config
from ..notebook.technique_kb import TechniqueKB

CARDS_PATH = Path(__file__).resolve().parent / "claude_red" / "techniques.jsonl"
SOURCE_TAG = "claude-red"


def load_cards(cards_path: Path = CARDS_PATH) -> list[dict]:
    if not cards_path.exists():
        raise SystemExit(f"no card corpus at {cards_path} — run claude_red/build.py first")
    cards = []
    with open(cards_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cards.append(json.loads(line))
    return cards


def load_into(kb: TechniqueKB, cards: list[dict]) -> dict:
    """Save every card into `kb`. Returns {added, deduped, total}."""
    added = deduped = 0
    for c in cards:
        res = kb.save(
            title=c["title"], body=c["body"],
            tags=c.get("tags") or [], surfaces=c.get("surfaces") or [],
            source_engagement=SOURCE_TAG,
        )
        if res.get("deduped"):
            deduped += 1
        else:
            added += 1
    return {"added": added, "deduped": deduped, "total": len(cards)}


def _resolve_db(args) -> Path:
    if args.db:
        return Path(args.db)
    if args.account:
        return config.technique_kb_path_for(args.account)
    return config.TECHNIQUE_KB_PATH


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Load claude-red technique cards into a technique KB.")
    ap.add_argument("--account", help="load into this account's KB (default: the operator KB)")
    ap.add_argument("--db", help="explicit technique-KB sqlite path (overrides --account)")
    args = ap.parse_args(argv)

    db_path = _resolve_db(args)
    cards = load_cards()
    kb = TechniqueKB(db_path=db_path)
    before = kb.count()
    stats = load_into(kb, cards)
    print(f"claude-red: {stats['total']} cards -> {db_path}")
    print(f"  added {stats['added']}, merged {stats['deduped']} (KB {before} -> {kb.count()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
