"""Working Notebook CLI — exercise the semantics before any UI.

    python3 -m agent.notebook.cli <engagement_id> list [--category C]
    python3 -m agent.notebook.cli <engagement_id> show <ordinal>
    python3 -m agent.notebook.cli <engagement_id> digest
    python3 -m agent.notebook.cli <engagement_id> search <query>
    python3 -m agent.notebook.cli <engagement_id> add <category> "<note text>"
"""
from __future__ import annotations

import argparse
import sys

from .. import config
from .schema import Category
from .service import NotebookService
from .store import NotFoundError


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Working Notebook read model / CLI")
    p.add_argument("engagement_id")
    p.add_argument("command", choices=["list", "show", "digest", "search", "add"])
    p.add_argument("arg", nargs="?")
    p.add_argument("arg2", nargs="?")
    p.add_argument("--category", choices=[c.value for c in Category])
    args = p.parse_args(argv)

    engagement_dir = config.ENGAGEMENTS_ROOT / args.engagement_id
    if args.command != "add" and not (engagement_dir / "notebook.db").exists():
        print(f"no notebook for engagement {args.engagement_id!r} yet", file=sys.stderr)
        return 1
    svc = NotebookService(engagement_dir)

    if args.command == "digest":
        print(svc.build_context_block())
    elif args.command == "list":
        for n in svc.store.list_notes(category=args.category):
            mark = "✓" if n["status"] == "resolved" else " "
            print(f"[{mark}] {svc._line(n)}")
    elif args.command == "search":
        for r in svc.search(args.arg or "", args.category):
            print(r["line"])
    elif args.command == "show":
        if not args.arg or not args.arg.isdigit():
            print("show requires an ordinal", file=sys.stderr)
            return 1
        try:
            n = svc.store.get_by_ordinal(int(args.arg))
        except NotFoundError:
            print(f"no N-{args.arg}", file=sys.stderr)
            return 1
        for k in ("ordinal", "category", "note", "tags", "surface", "refs", "status",
                  "resolved_reason", "chat_message_id"):
            print(f"  {k}: {n.get(k)}")
    elif args.command == "add":
        if not args.arg or not args.arg2:
            print("add requires: <category> \"<note text>\"", file=sys.stderr)
            return 1
        r = svc.add_note(category=args.arg, note=args.arg2)
        print(f"added N-{r['ordinal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
