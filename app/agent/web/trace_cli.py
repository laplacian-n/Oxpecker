"""Read one session's history back as a single timeline.

Step 4 and Step 5 put the data somewhere; this is what makes them usable. The records live in
four places — the hash-chained audit log, the debug trace, and the hypothesis graph / findings /
notebook in the persisted state — joined by `(session_id, turn_index)` and by the audit entry's
`entry_id`. Joining that by hand is work nobody does, which is the practical reason detailed
logging so often goes unread.

What is NOT invented here: the hypothesis graph, findings and notebook are persisted as a
snapshot, not as an append-only log, so there is no per-turn history of them to show. They appear
once, as the state at the end, rather than as fabricated per-turn transitions.

Usage:
    python3 -m agent.web.trace_cli --list
    python3 -m agent.web.trace_cli <session_id>
    python3 -m agent.web.trace_cli <session_id> --turn 3 --full
    python3 -m agent.web.trace_cli <session_id> --verify
    python3 -m agent.web.trace_cli <session_id> --json

Content drawn from the debug trace is unredacted (see debug_trace.py). Previews are truncated by
default for that reason and `--full` prints them whole; either way the header says when trace
data is in the output, so it is clear before anything gets pasted elsewhere.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import audit_log as audit_log_mod
from .. import config
from . import debug_trace

# Box-drawing for the timeline. Plain ASCII fallback keeps it readable when piped somewhere that
# mangles UTF-8.
_GLYPH = {"branch": "├", "last": "└", "bar": "│"}
_ASCII = {"branch": "|-", "last": "`-", "bar": "|"}

# A compaction can fold two dozen messages. Listing them all per event buries the rest of the
# timeline, so the default shows the first few and says how many were held back.
_FOLDED_PREVIEW_LIMIT = 6


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"kind": "_unparseable", "line_number": i, "raw": line[:200]})
    return out


def _state_for(session_id: str, state_file: Path) -> dict:
    """Hypothesis graph, findings and notebook for this session, from the persisted snapshot."""
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        "hypotheses": (data.get("graphs") or {}).get(session_id, []),
        "findings": (data.get("findings") or {}).get(session_id, []),
        "notes": (data.get("notebooks") or {}).get(session_id, []),
    }


def list_sessions(audit_dir: Path, trace_dir: Path) -> list[dict]:
    seen: dict[str, dict] = {}
    for d, key in ((audit_dir, "audit"), (trace_dir, "trace")):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.jsonl")):
            if p.name.endswith(".checkpoints.jsonl"):
                continue
            row = seen.setdefault(p.stem, {"session_id": p.stem, "audit": False, "trace": False})
            row[key] = True
            row[f"{key}_bytes"] = p.stat().st_size
            row["mtime"] = max(row.get("mtime", 0), p.stat().st_mtime)
    return sorted(seen.values(), key=lambda r: r.get("mtime", 0), reverse=True)


def build_timeline(session_id: str, *, audit_dir: Path, trace_dir: Path) -> dict:
    """Merge the two logs into ordered turn blocks, plus the events that belong between turns.

    Both files are append-only, so file order is chronological; that is used rather than the
    timestamps, because the audit log records whole seconds and several events can share one.
    """
    audit = _read_jsonl(audit_dir / f"{session_id}.jsonl")
    trace = _read_jsonl(trace_dir / f"{session_id}.jsonl")

    by_entry_id = {e.get("audit_entry_id"): e for e in trace
                   if e.get("kind") == "tool" and e.get("audit_entry_id")}

    turns: dict[int, dict] = {}

    def turn(idx) -> dict:
        return turns.setdefault(idx, {"turn_index": idx, "calls": [], "compactions": [],
                                      "retrievals": [], "prompts": [], "outputs": []})

    for entry in audit:
        idx = entry.get("turn_index")
        turn(idx)["calls"].append({
            "audit": entry,
            # The trace side is optional: the debug sink can be switched off, so a timeline has
            # to render from the audit log alone.
            "trace": by_entry_id.get(entry.get("entry_id")),
        })

    between: list[dict] = []
    for event in trace:
        kind = event.get("kind")
        if kind in ("_header", "tool"):
            continue
        idx = event.get("turn_index")
        bucket = {"compaction": "compactions", "retrieval": "retrievals",
                  "prompt": "prompts", "model_output": "outputs"}.get(kind)
        if bucket is None:
            between.append(event)
        elif idx is None:
            # Session-level compaction happens between turns, before a prompt is built, so it
            # genuinely has no turn index. Shown in sequence rather than forced into a turn.
            between.append(event)
        else:
            turn(idx)[bucket].append(event)

    return {
        "session_id": session_id,
        "turns": [turns[k] for k in sorted(turns, key=lambda k: (k is None, k))],
        "between_turns": between,
        "audit_entry_count": len(audit),
        "trace_event_count": len(trace),
        "trace_present": bool(trace),
    }


def _trunc(value, limit: int, full: bool) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    if full or len(s) <= limit:
        return s
    return s[:limit] + f"…[+{len(s) - limit}]"


def render(timeline: dict, *, full: bool = False, only_turn: int | None = None,
           ascii_only: bool = False, out=sys.stdout) -> None:
    g = _ASCII if ascii_only else _GLYPH
    p = lambda *a: print(*a, file=out)

    p(f"session {timeline['session_id']}")
    p(f"  {timeline['audit_entry_count']} audit entries, "
      f"{timeline['trace_event_count']} debug-trace events")
    if not timeline["trace_present"]:
        p("  no debug trace for this session (sink disabled, or the session predates it) — "
          "showing the audit log alone")
    else:
        p("  NOTE: debug-trace content is unredacted; previews are truncated unless --full")
    p("")

    def render_event(event: dict, prefix: str) -> None:
        kind = event.get("kind")
        if kind == "compaction":
            p(f"{prefix} compaction  scope={event.get('scope')} "
              f"folded={event.get('folded_count')} kept={event.get('kept_verbatim')} "
              f"tokens_at={event.get('prompt_tokens_at_compaction')}")
            p(f"{prefix}             summary: {_trunc(event.get('summary', ''), 160, full)}")
            folded = event.get("folded", [])
            shown = folded if full else folded[:_FOLDED_PREVIEW_LIMIT]
            for f in shown:
                p(f"{prefix}             dropped #{f.get('index')} [{f.get('role')}"
                  f"{'/' + f['tool_name'] if f.get('tool_name') else ''}] "
                  f"{_trunc(f.get('preview', ''), 90, full)}")
            if len(folded) > len(shown):
                p(f"{prefix}             … {len(folded) - len(shown)} more folded messages "
                  f"(--full to list them; this is where a 'why did it forget X' answer lives)")
        elif kind == "retrieval":
            hits = event.get("hits", [])
            scores = ", ".join(f"{h.get('score'):.3f}" if isinstance(h.get("score"), (int, float))
                               else str(h.get("score")) for h in hits)
            p(f"{prefix} retrieval   {event.get('result_count')} hits for "
              f"{_trunc(event.get('query', ''), 60, full)!r}  scores: {scores}")
            for h in hits:
                p(f"{prefix}             {h.get('source')}/{h.get('chunk_id')} "
                  f"{_trunc(h.get('preview', ''), 80, full)}")
        elif kind == "prompt":
            p(f"{prefix} prompt      {event.get('message_count')} msgs, "
              f"{event.get('prompt_tokens')} tokens, "
              f"summary_in_context={event.get('summary_present')}")
        elif kind == "model_output":
            if event.get("reasoning"):
                p(f"{prefix} reasoning   {_trunc(event['reasoning'], 200, full)}")
            if event.get("content"):
                p(f"{prefix} said        {_trunc(event['content'], 200, full)}")
        else:
            p(f"{prefix} {kind}  {_trunc({k: v for k, v in event.items() if k != 'kind'}, 140, full)}")

    for block in timeline["turns"]:
        idx = block["turn_index"]
        if only_turn is not None and idx != only_turn:
            continue
        p(f"turn {idx if idx is not None else '?'}")
        for event in block["prompts"] + block["compactions"] + block["retrievals"] + block["outputs"]:
            render_event(event, f"  {g['bar']}")

        for n, call in enumerate(block["calls"]):
            a, t = call["audit"], call["trace"]
            lead = g["last"] if n == len(block["calls"]) - 1 else g["branch"]
            tier = a.get("isolation_tier")
            args_dict = a.get("arguments") if isinstance(a.get("arguments"), dict) else {}
            # The spelling the model used, when it differs from the tool that ran: "it asked for
            # nmap" and "port_discovery ran" are separate facts, and the first is what explains
            # the model's behaviour.
            asked = args_dict.get("_requested_tool_name")
            asked_note = f"  (model asked for {asked!r})" if asked else ""
            is_last = n == len(block["calls"]) - 1
            cont = "  " if is_last else g["bar"]  # nothing hangs below the last branch
            p(f"  {lead} {a.get('tool_name')}{asked_note}")
            p(f"  {cont}   why     {_trunc(a.get('action_rationale', ''), 140, full)}")
            p(f"  {cont}   args    {_trunc(a.get('arguments', {}), 160, full)}")
            p(f"  {cont}   gate    {a.get('scope_decision')}"
              f"{f'  tier={tier}' if tier else ''}"
              f"{'  INJECTION-FLAGGED' if a.get('injection_flagged') else ''}")
            exit_note = "" if a.get("exit_code") is None else f"exit={a.get('exit_code')} "
            p(f"  {cont}   result  {exit_note}"
              f"{a.get('latency_ms')}ms  evidence={str(a.get('content_digest'))[:12]}…")
            # Prefer the trace's untruncated result; fall back to the audit excerpt.
            if t is not None:
                p(f"  {cont}           {_trunc(t.get('result'), 220, full)}")
            else:
                p(f"  {cont}           {_trunc(a.get('raw_output_excerpt', ''), 220, full)}"
                  f"   [audit excerpt; no trace]")
        p("")

    if timeline["between_turns"] and only_turn is None:
        p("between turns (no turn index — session-level compaction happens before a prompt is built)")
        for event in timeline["between_turns"]:
            render_event(event, f"  {g['bar']}")
        p("")


def render_state(state: dict, *, full: bool = False, out=sys.stdout) -> None:
    if not state or not any(state.values()):
        return
    p = lambda *a: print(*a, file=out)
    p("state at the end (a snapshot, not a per-turn history)")
    for h in state.get("hypotheses", []):
        p(f"  hypothesis {h.get('hypothesis_id', h.get('id', '?'))}  "
          f"status={h.get('status')} verdict={h.get('verdict')}  "
          f"{_trunc(h.get('title', ''), 90, full)}")
    for f in state.get("findings", []):
        p(f"  finding {f.get('finding_id', f.get('id', '?'))}  "
          f"severity={f.get('severity')}  {_trunc(f.get('title', ''), 90, full)}")
    for n in state.get("notes", []):
        p(f"  note [{n.get('category')}] {_trunc(n.get('text', ''), 90, full)}")
    p("")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m agent.web.trace_cli",
        description="Read a session's audit log and debug trace back as one timeline.")
    ap.add_argument("session_id", nargs="?", help="session to read")
    ap.add_argument("--list", action="store_true", help="list sessions that have records")
    ap.add_argument("--turn", type=int, help="show only this turn")
    ap.add_argument("--full", action="store_true",
                    help="do not truncate (prints unredacted trace content in full)")
    ap.add_argument("--json", action="store_true", help="emit the merged timeline as JSON")
    ap.add_argument("--verify", action="store_true",
                    help="verify the audit hash chain and report, without printing the timeline")
    ap.add_argument("--ascii", action="store_true", help="ASCII box drawing instead of UTF-8")
    ap.add_argument("--audit-dir", type=Path, default=None)
    ap.add_argument("--trace-dir", type=Path, default=None)
    ap.add_argument("--state-file", type=Path, default=None)
    args = ap.parse_args(argv)

    audit_dir = args.audit_dir or config.AUDIT_DIR
    trace_dir = args.trace_dir or debug_trace.TRACE_DIR
    state_file = args.state_file or (Path(__file__).resolve().parent / "dev_data" / "state.json")

    if args.list:
        rows = list_sessions(audit_dir, trace_dir)
        if not rows:
            print(f"no sessions found in {audit_dir} or {trace_dir}")
            return 0
        print(f"{'session':40} {'audit':>8} {'trace':>8}")
        for r in rows:
            print(f"{r['session_id']:40} "
                  f"{(str(r.get('audit_bytes', 0)) if r['audit'] else '-'):>8} "
                  f"{(str(r.get('trace_bytes', 0)) if r['trace'] else '-'):>8}")
        return 0

    if not args.session_id:
        ap.error("a session_id is required unless --list is given")

    if args.verify:
        ok, detail = audit_log_mod.verify(args.session_id, audit_dir=audit_dir)
        print(f"audit chain: {'OK' if ok else 'FAILED'} — {detail}")
        return 0 if ok else 1

    timeline = build_timeline(args.session_id, audit_dir=audit_dir, trace_dir=trace_dir)
    if not timeline["audit_entry_count"] and not timeline["trace_event_count"]:
        print(f"no records for session {args.session_id!r} in {audit_dir} or {trace_dir}")
        return 1

    state = _state_for(args.session_id, state_file)
    if args.json:
        print(json.dumps({**timeline, "state": state}, indent=2, default=str))
        return 0

    render(timeline, full=args.full, only_turn=args.turn, ascii_only=args.ascii)
    if args.turn is None:
        render_state(state, full=args.full)
        ok, detail = audit_log_mod.verify(args.session_id, audit_dir=audit_dir)
        print(f"audit chain: {'OK' if ok else 'FAILED'} — {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
