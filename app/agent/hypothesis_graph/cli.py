"""Hypothesis Graph CLI — the MVP 0 read model + ASCII view, so the semantics can be exercised
and eyeballed before any web UI exists (doc 2 §18: CLI/read model first, UI last).

    python3 -m agent.hypothesis_graph.cli <engagement_id> tree
    python3 -m agent.hypothesis_graph.cli <engagement_id> list [--open]
    python3 -m agent.hypothesis_graph.cli <engagement_id> show <ordinal>
    python3 -m agent.hypothesis_graph.cli <engagement_id> digest
    python3 -m agent.hypothesis_graph.cli <engagement_id> search <query>
"""
from __future__ import annotations

import argparse
import sys

from .. import config
from . import engine
from .service import HypothesisGraphService
from .store import NotFoundError

_STATUS_MARK = {
    "open": "○", "queued": "◍", "running": "◌", "blocked": "⊘", "awaiting_approval": "⧖",
    "completed": "●", "parked": "▢", "abandoned": "⊗", "draft": "·",
}
_VERDICT_MARK = {"confirmed": "✓✓", "supported": "✓", "refuted": "×", "inconclusive": "?", "superseded": "⇒"}


def _print_tree(svc: HypothesisGraphService) -> None:
    hyps = svc.store.list_hypotheses()
    if not hyps:
        print("(no hypotheses yet)")
        return
    children: dict = {}
    roots = []
    for h in hyps:
        p = h["primary_parent_id"]
        (children.setdefault(p, []).append(h) if p else roots.append(h))
    gstate = svc.store.get_graph_state()
    active = set(gstate.get("active_path") or [])

    def walk(node, prefix, is_last):
        mark = _STATUS_MARK.get(node["lifecycle_status"], "?")
        v = _VERDICT_MARK.get(node["verdict"], "")
        star = " ◀ active" if node["hypothesis_id"] in active else ""
        connector = "└─ " if is_last else "├─ "
        print(f"{prefix}{connector}{mark} #{node['ordinal']} {node['title']} "
              f"[{node['phase_created']}/{node['lifecycle_status']}{(' ' + v) if v else ''}]{star}")
        kids = children.get(node["hypothesis_id"], [])
        for i, kid in enumerate(kids):
            walk(kid, prefix + ("   " if is_last else "│  "), i == len(kids) - 1)

    for i, root in enumerate(roots):
        walk(root, "", i == len(roots) - 1)
    print(f"\nlegend: {' '.join(f'{m}={s}' for s, m in _STATUS_MARK.items())}")


def _print_show(svc: HypothesisGraphService, ordinal: int) -> None:
    h = svc.store.get_by_ordinal(ordinal)
    exps = svc.store.list_experiments(h["hypothesis_id"])
    comps = engine.priority_components(h, exps)
    print(f"H-{h['ordinal']}  {h['title']}")
    print(f"  phase={h['phase_created']}  status={h['lifecycle_status']}  verdict={h['verdict']}")
    print(f"  claim: {h['claim']}")
    print(f"  rationale: {h['rationale']}")
    if h.get("surface"):
        print(f"  surface: {h['surface']}")
    print(f"  confidence: {h['confidence_band']} ({h['confidence_reason']})")
    print(f"  coverage: {engine.coverage(h, exps):.0%}   impact: {h['impact']}/5   "
          f"priority score: {comps['score']} ({engine.priority_tier(h, comps['score'])})")
    print(f"  direct_tokens: {h['direct_tokens']}  origin: {h['origin_type']}")
    if h.get("park_reason"):
        print(f"  parked: {h['park_reason']}")
    if h.get("abandon_reason"):
        print(f"  abandoned: {h['abandon_reason']}")
    print(f"  attempts ({len(exps)}):")
    for x in exps:
        print(f"    #{x['attempt_no']} [{x['status']}] {x['method_summary']} -> {x['observed_result'] or '(pending)'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hypothesis Graph read model / ASCII view")
    parser.add_argument("engagement_id")
    parser.add_argument("command", choices=["tree", "list", "show", "digest", "search"])
    parser.add_argument("arg", nargs="?", help="ordinal for show; query for search")
    parser.add_argument("--open", action="store_true", help="list: only actionable hypotheses")
    args = parser.parse_args(argv)

    engagement_dir = config.ENGAGEMENTS_ROOT / args.engagement_id
    if not (engagement_dir / "hypothesis_graph.db").exists():
        print(f"no hypothesis graph for engagement {args.engagement_id!r} yet", file=sys.stderr)
        return 1
    svc = HypothesisGraphService(engagement_dir)

    if args.command == "tree":
        _print_tree(svc)
    elif args.command == "digest":
        print(svc.build_context_block())
    elif args.command == "list":
        hyps = svc.store.list_hypotheses()
        exps = {h["hypothesis_id"]: svc.store.list_experiments(h["hypothesis_id"]) for h in hyps}
        rows = engine.rank_open(hyps, exps) if args.open else [
            {**h, "priority_tier": "-", "priority": {"score": 0}} for h in hyps
        ]
        for r in rows:
            score = r["priority"]["score"] if args.open else ""
            print(f"H-{r['ordinal']:<3} [{r.get('priority_tier','-'):5}] {r['lifecycle_status']:16} {r['title']}  {score}")
    elif args.command == "show":
        if not args.arg or not args.arg.isdigit():
            print("show requires an ordinal", file=sys.stderr)
            return 1
        try:
            _print_show(svc, int(args.arg))
        except NotFoundError:
            print(f"no H-{args.arg}", file=sys.stderr)
            return 1
    elif args.command == "search":
        if not args.arg:
            print("search requires a query", file=sys.stderr)
            return 1
        for hit in svc.graph_search(args.arg):
            print(hit["digest"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
