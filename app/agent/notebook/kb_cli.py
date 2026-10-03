"""Cross-engagement technique KB CLI (the global store at config.TECHNIQUE_KB_PATH).

    python3 -m agent.notebook.kb_cli list
    python3 -m agent.notebook.kb_cli recall "<query>"  [--surface /api/orders]
    python3 -m agent.notebook.kb_cli forget <ordinal>
"""
from __future__ import annotations

import argparse

from .technique_kb import TechniqueKB


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Technique KB (global, cross-engagement)")
    p.add_argument("command", choices=["list", "recall", "forget"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--surface")
    args = p.parse_args(argv)
    kb = TechniqueKB()

    if args.command == "list":
        rows = kb.all()
        if not rows:
            print("(no techniques saved yet)")
        for t in rows:
            src = f" [{t['source_engagement']}]" if t["source_engagement"] else ""
            used = f" ·used {t['use_count']}x" if t["use_count"] else ""
            print(f"T-{t['ordinal']}{src}{used}  {t['title']}")
            if t["tags"] or t["surfaces"]:
                print(f"       {' '.join('#' + x for x in t['tags'])} {' '.join('@' + x for x in t['surfaces'])}")
    elif args.command == "recall":
        for t in kb.recall(args.arg or "", args.surface, bump=False):
            print(f"T-{t['ordinal']}: {t['body']}")
    elif args.command == "forget":
        if not args.arg:
            print("forget requires an ordinal")
            return 1
        print("forgotten" if kb.delete(args.arg) else "no such technique")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
