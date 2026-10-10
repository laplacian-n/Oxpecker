"""Tests that the shipped runtime's prompt, tool list, dispatcher and broker agree with each other.

This file exists because they had drifted apart in four directions at once, and every one of
them was invisible from inside any single file:

  1. `record_hypothesis`, `update_hypothesis_status`, `record_finding` and `record_note` were
     implemented, dispatched, and backed by API endpoints the UI reads — but they sat in
     SECURITY_TOOL_SCHEMAS behind `use_security_tools`, which defaults to False in Session, in
     CreateSessionRequest and in the UI's own state. So in every session the app creates, the
     model was not given them, while the system prompt instructed it to call all four by name.
     record_finding is the whole output of a hunt.
  2. The prompt's hand-written tool list omitted `port_discovery`, which is always available.
  3. The prompt described run_command as "Execute a shell command on the host" while that tool's
     schema says "There is no shell: pipes, redirects, &&, ; and $( ) are refused".
  4. The UI's toggle described a tool set belonging to loop.py (the CLI runtime) — naming
     osint_record and browser_fetch, which dev_server has never dispatched.

None of that was catchable by a test of any one component, because each component was
self-consistent. These checks are all cross-component on purpose.

The structural fix is that the prompt's tool list is now generated from the schemas actually
being sent, so 2, 3 and most of 1 cannot recur by construction. The checks below pin the rest.

Run directly: `python3 -m agent.web.test_tool_parity`.
"""
from __future__ import annotations

import re

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _dispatchable(src: str) -> set[str]:
    """Tool names `_run_tool` actually has a branch for."""
    start = src.index("def _run_tool(")
    rest = src[start:]
    m = re.search(r"\n(?=def |@app\.)", rest[10:])
    body = rest[: 10 + m.start()] if m else rest
    names = set(re.findall(r'(?:^|\s)(?:el)?if\s+name\s*==\s*"([a-z_0-9]+)"', body, re.M))
    for grp in re.findall(r"name\s+in\s+\(([^)]*)\)", body):
        names |= set(re.findall(r'"([a-z_0-9]+)"', grp))
    # Set-membership dispatch against a whole engine tool family, e.g. `name in _GRAPH_TOOL_NAMES`
    # — resolve the identifier to the family's names so a family routed in one branch still counts
    # as dispatchable for every tool in it.
    from ..hypothesis_graph.tools import GRAPH_TOOL_NAMES as _GTN
    _known_sets = {"_GRAPH_TOOL_NAMES": _GTN}
    for ident in re.findall(r"name\s+in\s+(_[A-Za-z_]+)\b", body):
        names |= set(_known_sets.get(ident, ()))
    return names


def main() -> int:
    import io
    from pathlib import Path

    from . import dev_server as d
    from ..broker import broker as b

    src = io.open(Path(__file__).with_name("dev_server.py"), encoding="utf-8").read()

    base = [t["function"]["name"] for t in d.TOOL_SCHEMAS]
    gated = [t["function"]["name"] for t in d.SECURITY_TOOL_SCHEMAS]
    disp = _dispatchable(src)
    classified = set(getattr(b, "TOOL_ACTION_CLASS", {}))

    print("\n== every tool the model is offered can actually run ==")
    check("no tool is advertised without a dispatch branch",
          set(base + gated) <= disp, f"missing: {sorted(set(base + gated) - disp)}")
    check("no name appears in both schema lists",
          not (set(base) & set(gated)), str(sorted(set(base) & set(gated))))

    print("\n== the four recording tools are unconditional ==")
    # The regression that motivated this file. They are local bookkeeping — no network, no scope
    # decision, no contact with the target — so there is nothing for a security toggle to buy.
    for t in ("record_hypothesis", "update_hypothesis_status", "record_finding", "record_note"):
        check(f"{t} is offered in every session", t in base,
              "in the gated list" if t in gated else "missing entirely")

    print("\n== the prompt cannot name a tool the model was not given ==")
    # The strongest check here: the prompt is prose plus a generated list, and the prose still
    # names tools in its guidance ("call record_finding for every CONFIRMED vulnerability").
    # Every tool name anywhere in the rendered prompt must be in the set actually sent.
    from ..hypothesis_graph import tools as _gt
    from ..notebook import tools as _nt

    # Names belonging to the OTHER runtimes. They are in the vocabulary precisely so that the
    # prompt mentioning one is a failure: the UI toggle had copied this set from loop.py, and a
    # prompt can drift the same way. They are not defined in any schema list reachable from
    # here, so they have to be listed by hand.
    OTHER_RUNTIME_TOOLS = {
        "osint_record", "security_reference_search", "browser_fetch", "knowledge_fetch",
        "graph_hypothesis_add", "note_add", "technique_recall",
    }
    vocabulary = (
        set(base) | set(gated) | classified | OTHER_RUNTIME_TOOLS
        | {t["function"]["name"] for t in _gt.SCHEMAS}
        | {t["function"]["name"] for t in _nt.SCHEMAS}
    )
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, vocabulary), key=len, reverse=True)) + r")\b")

    for label, sent in (("flag off", base), ("flag on", base + gated)):
        schemas = d.TOOL_SCHEMAS if label == "flag off" else d.TOOL_SCHEMAS + d.SECURITY_TOOL_SCHEMAS
        rendered = d.DEFAULT_SYSTEM_PROMPT.replace("@@TOOL_LIST@@", d._tool_list_block(schemas))
        check(f"[{label}] the @@TOOL_LIST@@ marker was substituted",
              "@@TOOL_LIST@@" not in rendered)
        named = set(pattern.findall(rendered))
        check(f"[{label}] the prompt names no tool that was not sent",
              named <= set(sent), f"phantom: {sorted(named - set(sent))}")
        check(f"[{label}] every sent tool appears in the prompt's list",
              all(f"- {n}:" in rendered for n in sent),
              f"absent: {[n for n in sent if f'- {n}:' not in rendered]}")

    print("\n== the generated list agrees with the schemas, not with a copy of them ==")
    block = d._tool_list_block(d.TOOL_SCHEMAS)
    check("one line per tool", len(block.splitlines()) == len(base), block)
    check("run_command's line does not call it a shell, because its schema says there is none",
          "shell" not in dict(
              (l.split(":")[0][2:], l) for l in block.splitlines()
          ).get("run_command", "").lower(),
          [l for l in block.splitlines() if l.startswith("- run_command")])

    print("\n== the broker boundary is where it is meant to be ==")
    check("every advertised tool the broker classifies is routed through the broker",
          {n for n in base + gated if n in classified} <= d._BROKER_MEDIATED,
          f"classified but not mediated: "
          f"{sorted({n for n in base + gated if n in classified} - d._BROKER_MEDIATED)}")
    check("every broker-mediated tool is dispatchable", d._BROKER_MEDIATED <= disp,
          str(sorted(d._BROKER_MEDIATED - disp)))
    check("every broker-mediated tool is classified, so none fails closed as 'unknown tool'",
          d._BROKER_MEDIATED <= classified, str(sorted(d._BROKER_MEDIATED - classified)))

    # Deliberate, and pinned so nobody "completes the table" and silently reroutes them.
    # run_command is gated by the sandbox and its preflight; the file tools by workspace
    # confinement. Adding them to TOOL_ACTION_CLASS would change which component decides.
    for t in ("run_command", "read_file", "write_file"):
        check(f"{t} is deliberately NOT broker-classified", t not in classified)
        check(f"{t} is deliberately NOT broker-mediated", t not in d._BROKER_MEDIATED)

    print("\n== every store the UI reads has something that writes it ==")
    def writes(var: str) -> bool:
        return bool(re.search(rf"{re.escape(var)}\[[^\]]+\]\s*(=|\.append)|{re.escape(var)}\.append", src))

    # Findings is still the in-memory dict (written via its accessors).
    check("_findings (/findings) has a writer", writes("_findings"))

    # The hypothesis graph (step 2b) and the notebook (step 3) moved off their in-memory dicts to
    # the per-engagement HypothesisGraphService / NotebookService, so their writer is the service
    # call inside the accessor, not a dict assignment. The invariant is unchanged (the store the UI
    # reads has a writer); only the shape of the writer did.
    check("_graph_* (/hypothesis-graph) has a writer via the graph service",
          bool(re.search(r"svc\.add_hypothesis\(", src)))
    check("_notebook_* (/notebook) has a writer via the notebook service",
          bool(re.search(r"svc\.store\.add_note\(", src)))

    # Known gaps, asserted as gaps rather than left to look like features: these two endpoints
    # exist and the UI reads them, but nothing in this runtime ever writes either store, so they
    # can only return empty. The real mechanisms live elsewhere — consults in
    # broker/consult_queue.py, the technique KB in the notebook service — both reachable from
    # loop.py and not from here. Wiring them is a separate piece of work; this records the state
    # so the next reader does not mistake an empty panel for an empty engagement.
    for var, endpoint in (("_consults", "/api/consults"), ("_technique_kb", "/api/technique-kb")):
        check(f"KNOWN GAP: {var} ({endpoint}) still has no writer in this runtime",
              not writes(var),
              "it has one now — wire the endpoint and move this out of the known-gap list")

    print("\n== every capture the trace layer offers is actually wired ==")
    # Third instance of one bug class in this codebase: something fully implemented, with a
    # docstring explaining why it matters, that nothing ever calls. First was the four record_*
    # tools; second was DebugTrace.prompt and DebugTrace.model_output — the two that carry the
    # prompt as assembled and the model's own output with its reasoning, i.e. precisely the half
    # of a trajectory that is trainable. Every engagement run before they were wired is a run
    # whose reasoning is gone for good, which is not recoverable later like a missing field is.
    #
    # So this stops being a thing to notice and becomes a rule: a public capture method with no
    # caller is a gap, not a convenience.
    import inspect

    from . import debug_trace as dt

    runtime_src = src  # dev_server.py, read above
    public = [
        n for n, _ in inspect.getmembers(dt.DebugTrace, inspect.isfunction)
        if not n.startswith("_")
    ]
    check("DebugTrace exposes capture methods to check", len(public) >= 4, str(public))
    for name in public:
        if name == "record":
            continue  # the generic primitive the typed helpers are built on
        called = f".{name}(" in runtime_src
        check(f"DebugTrace.{name}() has a caller in the runtime", called,
              "implemented but never called — either wire it or delete it")

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
