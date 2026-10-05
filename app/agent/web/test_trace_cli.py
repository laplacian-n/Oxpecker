"""Tests for the trace reader.

The thing being protected is that the reader stays useful in the states it will actually meet: a
session with both logs, a session with only the audit log because the debug sink was off, a log
with a corrupt line, and a session that does not exist. A reader that only works on the happy
path does not get used when something is wrong, which is the only time anyone opens it.

Run directly: `python3 -m agent.web.test_trace_cli`.
"""
from __future__ import annotations

import io
import json
import pathlib
import tempfile
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


class _FakeLLM:
    def chat(self, *a, **k):
        return {"choices": [{"message": {"content": "SUMMARY: port 3000 open"}}]}


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxpecker-tracecli-test-"))
    audit_dir, trace_dir = tmp / "audit", tmp / "trace"
    with patch("agent.config.AUDIT_DIR", audit_dir), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "key.bin"):
        from . import debug_trace as dt
        from . import dev_server as d
        from . import trace_cli as tc

        dt.TRACE_DIR = trace_dir
        dt._traces.clear()

        sid = "t-both"
        s = d.Session(session_id=sid, engagement_id="lab-default", isolation_tier="direct")
        d._audited_run_tool("nmap", {"host": "127.0.0.1", "ports": [22]}, s,
                            turn_index=1, action_rationale="map the surface")
        d._audited_run_tool("http_request", {"url": "http://8.8.8.8/"}, s,
                            turn_index=1, action_rationale="try the resolver")
        d._audited_run_tool("run_command", {"argv": ["pwd"]}, s,
                            turn_index=2, action_rationale="where am i")
        for i in range(30):
            s.append("user" if i % 2 else "assistant", f"chatter {i} holding finding-{i}")
        with patch.object(d, "_llm", _FakeLLM()):
            d._maybe_compact(s)

        print("\n== the two logs are merged and grouped by turn ==")
        tl = tc.build_timeline(sid, audit_dir=audit_dir, trace_dir=trace_dir)
        check("both sources are seen",
              tl["audit_entry_count"] == 3 and tl["trace_event_count"] >= 4,
              f"audit={tl['audit_entry_count']} trace={tl['trace_event_count']}")
        check("trace_present is true", tl["trace_present"] is True)
        turns = {t["turn_index"]: t for t in tl["turns"]}
        check("turns are grouped, not flattened", sorted(turns) == [1, 2], str(sorted(turns)))
        check("turn 1 holds both of its calls", len(turns[1]["calls"]) == 2,
              str(len(turns[1]["calls"])))

        print("\n== audit and trace records are correlated by entry id, not by timestamp ==")
        # Timestamps in the audit log have whole-second resolution and several calls share one,
        # so correlation has to come from the id.
        check("every call found its trace record",
              all(c["trace"] is not None for t in tl["turns"] for c in t["calls"]),
              str([(c["audit"]["tool_name"], c["trace"] is not None)
                   for t in tl["turns"] for c in t["calls"]]))
        pair = turns[1]["calls"][0]
        check("the paired records are the same action",
              pair["trace"]["audit_entry_id"] == pair["audit"]["entry_id"])
        check("the trace side carries the untruncated result",
              "result" in pair["trace"], str(list(pair["trace"]))[:90])

        print("\n== session compaction is placed between turns, not invented into one ==")
        comps = [e for e in tl["between_turns"] if e["kind"] == "compaction"]
        check("the compaction is in between_turns", len(comps) == 1, str(len(comps)))
        check("it is not also attached to a turn",
              not any(t["compactions"] for t in tl["turns"]),
              str([len(t["compactions"]) for t in tl["turns"]]))

        print("\n== the default render is readable and the facts survive into it ==")
        buf = io.StringIO()
        tc.render(tl, out=buf)
        text = buf.getvalue()
        check("the alias the model used is shown", "model asked for 'nmap'" in text)
        check("the tool that actually ran is shown", "port_discovery" in text)
        # Which layer names the rule is an implementation detail and moved once already: the
        # tool's own check said "not_in_scope", and now the broker classifies the same refusal
        # as "scope_check". The property worth holding is that a refusal reads as denied with a
        # scope reason, not the exact spelling.
        check("a refusal is shown as denied",
              "denied:" in text and ("scope" in text or "not_in_scope" in text),
              [l for l in text.splitlines() if "denied" in l][:2])
        check("the isolation tier is shown for a local command", "tier=direct" in text)
        check("the compaction summary is shown", "port 3000 open" in text)
        check("the operator is warned the trace is unredacted", "unredacted" in text.lower())
        check("the folded list is capped by default",
              "more folded messages" in text, "a long fold would bury the timeline")
        check("nothing hangs below the last branch of a turn",
              "└ http_request\n  │" not in text and "└ run_command\n  │" not in text)

        print("\n== --full expands rather than hiding ==")
        buf_full = io.StringIO()
        tc.render(tl, full=True, out=buf_full)
        full_text = buf_full.getvalue()
        check("--full lists every folded message", "more folded messages" not in full_text)
        check("--full finds a fact that the capped view held back",
              "finding-19" in full_text and "finding-19" not in text)
        check("--full is longer", len(full_text) > len(text),
              f"{len(full_text)} vs {len(text)}")

        print("\n== --turn narrows ==")
        buf_turn = io.StringIO()
        tc.render(tl, only_turn=2, out=buf_turn)
        t2 = buf_turn.getvalue()
        check("only the asked-for turn is rendered",
              "turn 2" in t2 and "turn 1" not in t2, t2[:120])

        print("\n== it still works with the audit log alone (debug sink off) ==")
        sid2 = "t-audit-only"
        s2 = d.Session(session_id=sid2, engagement_id="lab-default", isolation_tier="direct")
        import os
        os.environ["OXPECKER_DEBUG_TRACE"] = "0"
        try:
            dt._traces.clear()
            d._audited_run_tool("run_command", {"argv": ["pwd"]}, s2,
                                turn_index=0, action_rationale="no trace for this one")
        finally:
            os.environ.pop("OXPECKER_DEBUG_TRACE", None)
            dt._traces.clear()
        tl2 = tc.build_timeline(sid2, audit_dir=audit_dir, trace_dir=trace_dir)
        check("the audit entry is still read", tl2["audit_entry_count"] == 1,
              str(tl2["audit_entry_count"]))
        check("trace_present is false", tl2["trace_present"] is False)
        buf2 = io.StringIO()
        tc.render(tl2, out=buf2)
        only = buf2.getvalue()
        check("the reader says the trace is missing rather than looking broken",
              "no debug trace" in only, only[:160])
        check("it falls back to the audit excerpt and labels it as such",
              "[audit excerpt; no trace]" in only, only[-200:])

        print("\n== a corrupt log line does not take the reader down ==")
        path = audit_dir / f"{sid}.jsonl"
        path.write_text(path.read_text() + "{this is not json\n")
        tl3 = tc.build_timeline(sid, audit_dir=audit_dir, trace_dir=trace_dir)
        check("the readable entries are still returned", tl3["audit_entry_count"] == 4,
              str(tl3["audit_entry_count"]))
        bad = [c for t in tl3["turns"] for c in t["calls"]
               if c["audit"].get("kind") == "_unparseable"]
        check("the unreadable line is surfaced, not silently dropped", len(bad) == 1,
              str(len(bad)))
        buf3 = io.StringIO()
        tc.render(tl3, out=buf3)
        check("rendering a corrupt log does not raise", True)

        print("\n== the CLI entry points behave ==")
        rc = tc.main(["--list", "--audit-dir", str(audit_dir), "--trace-dir", str(trace_dir)])
        check("--list exits 0", rc == 0, str(rc))
        rows = tc.list_sessions(audit_dir, trace_dir)
        ids = {r["session_id"] for r in rows}
        check("--list finds both sessions", {sid, sid2} <= ids, str(ids))
        check("checkpoint files are not listed as sessions",
              not any(r["session_id"].endswith(".checkpoints") for r in rows), str(ids))
        check("a session with only an audit log is still listed",
              any(r["session_id"] == sid2 and r["audit"] and not r["trace"] for r in rows),
              str(rows))

        rc = tc.main([sid2, "--verify", "--audit-dir", str(audit_dir)])
        check("--verify exits 0 on an intact chain", rc == 0, str(rc))
        # The corrupt line appended above is itself a tamper case. verify() used to raise
        # JSONDecodeError here rather than return a verdict, which meant corrupting one line
        # turned integrity checking into an unhandled exception.
        rc = tc.main([sid, "--verify", "--audit-dir", str(audit_dir)])
        check("--verify reports a damaged log as a failure instead of raising", rc == 1, str(rc))
        from .. import audit_log as al
        ok_v, detail_v = al.verify(sid, audit_dir=audit_dir)
        check("the failure says which line is unreadable",
              ok_v is False and "line" in detail_v and "damaged" in detail_v, detail_v)

        rc = tc.main(["no-such-session", "--audit-dir", str(audit_dir),
                      "--trace-dir", str(trace_dir)])
        check("an unknown session exits non-zero rather than printing an empty timeline",
              rc == 1, str(rc))

        print("\n== --json emits the merged structure ==")
        buf_json = io.StringIO()
        with patch("sys.stdout", buf_json):
            tc.main([sid2, "--json", "--audit-dir", str(audit_dir),
                     "--trace-dir", str(trace_dir), "--state-file", str(tmp / "none.json")])
        parsed = json.loads(buf_json.getvalue())
        check("the json parses and carries the turns",
              parsed["session_id"] == sid2 and "turns" in parsed, str(list(parsed))[:90])

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
