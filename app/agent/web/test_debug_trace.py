"""Tests for the debug sink, and in particular for the compaction blind spot it exists to close.

Compaction used to log `log.info("compacted %d msgs")` — a count. The folded messages were gone
from context and the LLM-written summary that replaced them was overwritten by the next
compaction, so "why did the agent forget what it found at step 5" had no answer anywhere. The
assertion that matters below is not that a compaction event was written, but that a specific fact
known to have been folded can still be found afterwards.

Everything is written into a temporary trace dir. Run directly:
`python3 -m agent.web.test_debug_trace`.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


class _FakeLLM:
    def __init__(self, summary):
        self.summary = summary

    def chat(self, *a, **k):
        return {"choices": [{"message": {"content": self.summary}}]}


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="oxpecker-trace-test-"))
    with patch("agent.config.AUDIT_DIR", tmp / "audit"), \
         patch("agent.config.EVIDENCE_KEY_PATH", tmp / "evidence_key.bin"):
        from . import debug_trace as dt
        from . import dev_server as d

        trace_dir = tmp / "trace"

        def events(sid):
            path = trace_dir / f"{sid}.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

        def reset():
            dt.TRACE_DIR = trace_dir
            dt._traces.clear()

        reset()

        print("\n== the audit log and the debug sink stay separate ==")
        check("they write to different directories",
              dt.TRACE_DIR != pathlib.Path(str(tmp / "audit")), str(dt.TRACE_DIR))

        print("\n== every file states what it contains before anything else ==")
        s = d.Session(session_id="t-tool", engagement_id="lab-default", isolation_tier="direct")
        d._audited_run_tool("run_command", {"argv": ["pwd"]}, s,
                            turn_index=0, action_rationale="look around")
        ev = events("t-tool")
        check("a header is written first", ev and ev[0]["kind"] == "_header",
              str([e["kind"] for e in ev]))
        check("the header warns the contents are unredacted",
              "UNREDACTED" in ev[0].get("warning", ""), ev[0].get("warning", "")[:70])

        print("\n== tool events keep the whole result and point at the audit entry ==")
        tool_events = [e for e in ev if e["kind"] == "tool"]
        check("the tool call is recorded", len(tool_events) == 1, str(len(tool_events)))
        t = tool_events[0]
        check("the canonical tool name is recorded", t["tool_name"] == "run_command", t["tool_name"])
        check("the audit entry id is carried for correlation", bool(t["audit_entry_id"]),
              str(t["audit_entry_id"]))
        audit_entries = [json.loads(l) for l in
                         (tmp / "audit" / "t-tool.jsonl").read_text().splitlines() if l.strip()]
        check("that id matches a real audit entry",
              any(a["entry_id"] == t["audit_entry_id"] for a in audit_entries),
              f"trace={t['audit_entry_id']} audit={[a['entry_id'] for a in audit_entries]}")
        check("the injection verdict is recorded alongside", t["injection_verdict"] == "clean",
              str(t["injection_verdict"]))

        print("\n== an aliased call records both the request and what ran ==")
        d._audited_run_tool("nmap", {"host": "127.0.0.1", "ports": [9]}, s,
                            turn_index=1, action_rationale="")
        aliased = [e for e in events("t-tool") if e["kind"] == "tool" and e["turn_index"] == 1][0]
        check("requested and canonical names are both kept",
              aliased["requested_name"] == "nmap" and aliased["tool_name"] == "port_discovery",
              f"{aliased['requested_name']} -> {aliased['tool_name']}")

        print("\n== THE BLIND SPOT: session compaction records what it removed ==")
        reset()
        s2 = d.Session(session_id="t-compact", engagement_id="lab-default")
        for i in range(30):
            s2.append("user" if i % 2 else "assistant",
                      f"message number {i} containing finding-{i}")
        with patch.object(d, "_llm", _FakeLLM("SUMMARY: target reachable, one finding")):
            d._maybe_compact(s2)
        comp = [e for e in events("t-compact") if e["kind"] == "compaction"]
        check("a compaction event is written", len(comp) == 1, str(len(comp)))
        c = comp[0]
        check("the scope distinguishes it from the mid-turn path", c["scope"] == "session",
              c["scope"])
        check("the number folded is recorded", c["folded_count"] > 0, str(c["folded_count"]))
        check("the folded list matches the count",
              len(c["folded"]) == c["folded_count"], f"{len(c['folded'])} vs {c['folded_count']}")
        check("the summary that replaced them is kept verbatim",
              c["summary"] == "SUMMARY: target reachable, one finding", c["summary"])
        check("how many were kept verbatim is recorded", c["kept_verbatim"] == d.KEEP_RECENT,
              str(c["kept_verbatim"]))
        # The question this has to answer.
        folded_previews = " ".join(f["preview"] for f in c["folded"])
        check("a fact known to have been folded can still be found afterwards",
              "finding-5" in folded_previews,
              "this is the whole point of the event; if it fails the blind spot is still open")
        check("each folded message carries its position in the transcript",
              all(f["index"] is not None for f in c["folded"]),
              str([f["index"] for f in c["folded"]][:5]))
        check("each carries its role so a tool result is distinguishable from a model turn",
              all(f["role"] for f in c["folded"]))
        check("a message kept verbatim is NOT listed as folded",
              f"finding-{len(s2.messages) - 1}" not in folded_previews)

        print("\n== the mid-turn path records the same shape, under its own scope ==")
        reset()
        s3 = d.Session(session_id="t-inturn", engagement_id="lab-default")
        msgs = [{"role": "system", "content": "sys"}]
        msgs += [{"role": "user", "content": f"working message {i} with marker-{i}"}
                 for i in range(20)]
        with patch.object(d, "_llm", _FakeLLM("IN-TURN SUMMARY: progress so far")):
            out = d._compact_working_messages(msgs, s3)
        comp3 = [e for e in events("t-inturn") if e["kind"] == "compaction"]
        check("the mid-turn compaction is recorded", len(comp3) == 1, str(len(comp3)))
        check("its scope is distinguishable", comp3 and comp3[0]["scope"] == "in_turn",
              str(comp3 and comp3[0]["scope"]))
        check("its folded content is recoverable too",
              comp3 and "marker-2" in " ".join(f["preview"] for f in comp3[0]["folded"]))
        check("the returned message list really is shorter", len(out) < len(msgs),
              f"{len(out)} vs {len(msgs)}")

        print("\n== retrieval records chunk identity and score ==")
        reset()
        s4 = d.Session(session_id="t-rag", engagement_id="lab-default")
        hits = [{"score": 0.81, "source": "nvd", "chunk_id": "c1", "text": "CVE-2021-1 detail"},
                {"score": 0.77, "source": "exploitdb", "chunk_id": "c2", "text": "exploit notes"}]
        class _Rag:
            def search(self, q, top_k=5):
                return hits
        with patch.object(d, "_rag", _Rag()):
            d._audited_run_tool("knowledge_search", {"query": "apache rce"}, s4,
                                turn_index=0, action_rationale="")
        r = [e for e in events("t-rag") if e["kind"] == "retrieval"]
        check("a retrieval event is written", len(r) == 1, str(len(r)))
        check("the query is recorded", r and r[0]["query"] == "apache rce")
        check("scores are recorded per hit",
              r and [h["score"] for h in r[0]["hits"]] == [0.81, 0.77],
              str(r and [h["score"] for h in r[0]["hits"]]))
        check("chunk ids are recorded, so a bad hit can be traced to a chunk",
              r and [h["chunk_id"] for h in r[0]["hits"]] == ["c1", "c2"],
              str(r and [h["chunk_id"] for h in r[0]["hits"]]))

        print("\n== it can be switched off, and failing never breaks the run ==")
        reset()
        os.environ["OXPECKER_DEBUG_TRACE"] = "0"
        try:
            s5 = d.Session(session_id="t-off", engagement_id="lab-default",
                           isolation_tier="direct")
            d._audited_run_tool("run_command", {"argv": ["pwd"]}, s5,
                                turn_index=0, action_rationale="")
            check("OXPECKER_DEBUG_TRACE=0 writes nothing",
                  not (trace_dir / "t-off.jsonl").exists())
        finally:
            os.environ.pop("OXPECKER_DEBUG_TRACE", None)

        dt._traces.clear()
        dt.TRACE_DIR = pathlib.Path("/proc/nonexistent/cannot-write")
        try:
            s6 = d.Session(session_id="t-broken", engagement_id="lab-default",
                           isolation_tier="direct")
            result, _ = d._audited_run_tool("run_command", {"argv": ["pwd"]}, s6,
                                            turn_index=0, action_rationale="")
            check("an unwritable trace dir does not fail the tool call",
                  result.get("ok") is True, str(result)[:110])
        finally:
            reset()

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
