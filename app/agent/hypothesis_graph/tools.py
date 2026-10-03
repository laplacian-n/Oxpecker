"""Model-callable tool surface for the Hypothesis Graph. These are IN-PROCESS tools dispatched
straight to HypothesisGraphService — deliberately NOT broker-mediated (doc 2 §12: graph mutation
is not a target action; only the experiment's own target-touching calls go through the broker,
and the experiment records that broker_action_ref). So they route like read_file/write_file, not
like http_recon.

Concise schemas on purpose: they're added to the prompt only when use_hypothesis_graph=True, and
in that mode they REPLACE the flat record_hypothesis/update_hypothesis_status tools (the graph
subsumes them), so the compiled-prompt token budget doesn't grow for sessions that don't opt in.
"""
from __future__ import annotations

from .service import HypothesisGraphService
from .store import ConflictError, CycleError, GraphValidationError, NotFoundError

GRAPH_TOOL_NAMES = {
    "graph_hypothesis_add", "graph_attempt_start", "graph_attempt_complete",
    "graph_set_verdict", "graph_park", "graph_abandon", "graph_set_active_path",
    "graph_search", "graph_read_branch",
}


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


SCHEMAS = [
    _fn("graph_hypothesis_add",
        "Record a testable, falsifiable hypothesis in the investigation graph. Every hypothesis "
        "needs a rationale (why it exists) and an honest confidence band — never inflate. Link it "
        "to the hypothesis it derived from via parent_ref.",
        {"title": {"type": "string", "description": "a short descriptive name, e.g. 'Missing CSP header' — "
                    "not an id or ordinal, the system assigns H-<n> automatically"},
         "claim": {"type": "string", "description": "the falsifiable claim, e.g. '/api/orders/{id} lacks object-level authz'"},
         "rationale": {"type": "string", "description": "why this hypothesis exists — the observation behind it"},
         "impact": {"type": "integer", "description": "1-5, how bad if the claim is true"},
         "confidence_band": {"type": "string", "enum": ["low", "medium", "high"]},
         "confidence_reason": {"type": "string"},
         "phase": {"type": "string", "enum": ["INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT"]},
         "parent_ref": {"type": "string", "description": "H-<n> or id of the hypothesis this derived from, if any"},
         "surface": {"type": "string", "description": "attack surface it targets, e.g. /api/orders/{id}"},
         "planned_tests": {"type": "integer", "description": "how many tests you expect to run (default 1)"},
         "origin_type": {"type": "string", "enum": ["ai_inference", "user_message", "tool_observation", "combined_analysis", "retest"]}},
        ["title", "claim", "rationale", "impact", "confidence_band", "confidence_reason", "phase"]),
    _fn("graph_attempt_start",
        "Begin an experiment (attempt) to test a hypothesis. A hypothesis can be tested many "
        "times. If the attempt touches the target, do that through the normal broker-mediated "
        "tools and note nothing extra here — just describe the method.",
        {"hypothesis_ref": {"type": "string"},
         "method_summary": {"type": "string", "description": "what you will do to test it"},
         "expected_observation": {"type": "string"}},
        ["hypothesis_ref", "method_summary"]),
    _fn("graph_attempt_complete",
        "Finish an attempt with its interpreted observation. Records the result and updates "
        "coverage deterministically; returns a suggested confidence band you can compare to your "
        "own assessment.",
        {"experiment_id": {"type": "string"},
         "status": {"type": "string", "enum": ["completed", "failed", "blocked", "cancelled"]},
         "observed_result": {"type": "string"},
         "observation_summary": {"type": "string", "description": "one-line interpretation"},
         "polarity": {"type": "string", "enum": ["supports", "refutes", "neutral"]},
         "strength": {"type": "string", "enum": ["weak", "moderate", "strong"]},
         "evidence_refs": {"type": "array", "items": {"type": "string"}}},
        ["experiment_id", "status", "observed_result", "observation_summary", "polarity", "strength"]),
    _fn("graph_set_verdict",
        "Record the verdict on a hypothesis after testing. 'confirmed' requires reproducible "
        "evidence, not inference; a 'refuted' hypothesis is a real, complete result.",
        {"hypothesis_ref": {"type": "string"},
         "verdict": {"type": "string", "enum": ["supported", "refuted", "inconclusive", "confirmed"]},
         "confidence_band": {"type": "string", "enum": ["low", "medium", "high"]},
         "confidence_reason": {"type": "string"}},
        ["hypothesis_ref", "verdict"]),
    _fn("graph_park",
        "Shelve a hypothesis you're not pursuing right now but might return to. Reversible — the "
        "default for 'not worth it yet', unlike abandon. Requires a reason and (ideally) what "
        "would bring you back.",
        {"hypothesis_ref": {"type": "string"}, "reason": {"type": "string"}},
        ["hypothesis_ref", "reason"]),
    _fn("graph_abandon",
        "Permanently drop a hypothesis — only when it is out of scope, conflicts with the RoE, is "
        "a duplicate, or is genuinely untestable. Not for 'low priority' (use park). Kept in the "
        "graph with the reason, never deleted.",
        {"hypothesis_ref": {"type": "string"}, "reason": {"type": "string"}},
        ["hypothesis_ref", "reason"]),
    _fn("graph_set_active_path",
        "Declare the path you are actively pursuing (root -> current), with a reason. This is how "
        "the operator sees where you're heading. Change it deliberately when evidence or steering "
        "warrants — not on every small score wiggle.",
        {"refs": {"type": "array", "items": {"type": "string"}, "description": "ordered H-<n>/ids root->current"},
         "reason": {"type": "string"}},
        ["refs", "reason"]),
    _fn("graph_search",
        "Find hypotheses by ordinal, title, claim, or surface. Use this instead of guessing an id "
        "or a past result you don't see in the digest.",
        {"query": {"type": "string"}}, ["query"]),
    _fn("graph_read_branch",
        "Read one branch of the graph to a bounded depth — your backtrack tool. Use it before "
        "assuming anything about earlier hypotheses not shown in the digest.",
        {"hypothesis_ref": {"type": "string"}, "depth": {"type": "integer"},
         "mode": {"type": "string", "enum": ["summary", "full"]}},
        ["hypothesis_ref"]),
]


def dispatch(service: HypothesisGraphService, tool_name: str, args: dict) -> dict:
    """Execute one graph tool. Returns a JSON-able dict; validation/not-found/conflict/cycle
    errors are returned as {"ok": False, ...} rather than raised, so a model mistake becomes a
    tool result it can recover from, not a crashed turn."""
    try:
        if tool_name == "graph_hypothesis_add":
            return {"ok": True, **service.add_hypothesis(
                title=args["title"], claim=args["claim"], phase_created=args["phase"],
                rationale=args["rationale"], impact=int(args["impact"]),
                confidence_band=args["confidence_band"], confidence_reason=args["confidence_reason"],
                primary_parent_id=service._resolve(args["parent_ref"]) if args.get("parent_ref") else None,
                surface=args.get("surface"), planned_tests=int(args.get("planned_tests", 1)),
                origin_type=args.get("origin_type", "ai_inference"),
                origin_ref=args.get("origin_ref"),
            )}
        if tool_name == "graph_attempt_start":
            return {"ok": True, **service.start_attempt(
                args["hypothesis_ref"], method_summary=args["method_summary"],
                expected_observation=args.get("expected_observation", ""),
                chat_start_message_id=args.get("chat_start_message_id"),
            )}
        if tool_name == "graph_attempt_complete":
            return {"ok": True, **service.complete_attempt(
                args["experiment_id"], status=args["status"], observed_result=args["observed_result"],
                observation_summary=args["observation_summary"], polarity=args["polarity"],
                strength=args["strength"], evidence_refs=args.get("evidence_refs"),
                chat_result_message_id=args.get("chat_result_message_id"),
            )}
        if tool_name == "graph_set_verdict":
            return {"ok": True, **service.set_verdict(
                args["hypothesis_ref"], args["verdict"],
                confidence_band=args.get("confidence_band"), confidence_reason=args.get("confidence_reason"),
            )}
        if tool_name == "graph_park":
            return {"ok": True, **service.park(args["hypothesis_ref"], args["reason"])}
        if tool_name == "graph_abandon":
            return {"ok": True, **service.abandon(args["hypothesis_ref"], args["reason"])}
        if tool_name == "graph_set_active_path":
            return {"ok": True, **service.set_active_path(args["refs"], args["reason"])}
        if tool_name == "graph_search":
            return {"ok": True, "results": service.graph_search(args["query"])}
        if tool_name == "graph_read_branch":
            return {"ok": True, **service.graph_read_branch(
                args["hypothesis_ref"], depth=int(args.get("depth", 3)), mode=args.get("mode", "summary"),
            )}
        return {"ok": False, "error": f"unknown graph tool {tool_name!r}"}
    except (GraphValidationError, NotFoundError, CycleError, ConflictError, ValueError, KeyError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
