"""Model-callable tools for the Working Notebook. IN-PROCESS (dispatched straight to
NotebookService, like the graph_* tools and read_file/write_file — a note is not a target
action, so nothing for the broker to gate). Added to the prompt only when the structured working
memory surface is on (same gate as the Hypothesis Graph).
"""
from __future__ import annotations

from .schema import Category
from .service import NotebookService
from .store import NotFoundError, NotebookValidationError

NOTEBOOK_TOOL_NAMES = {"note_add", "note_search", "note_resolve", "note_promote", "technique_recall"}

_CATEGORIES = [c.value for c in Category]


def _fn(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


SCHEMAS = [
    _fn("note_add",
        "Jot a note in your working notebook — cross-cutting things you notice, a technique that "
        "worked, something to come back to (category 'todo'), or a path you have ruled out "
        "(category 'dead-end', so you don't retry it). Write it so a later turn can act on it "
        "without you: a 'dead-end' names what you tried and the observation that rules it out "
        "(\"tested alg=none and kid traversal, server 401s both\"), not \"auth looks fine\"; a "
        "'todo' names a concrete next action on a named surface (\"re-test /api/proxy?url= for "
        "SSRF once a cookie is captured\"), not \"investigate the proxy\". This is NOT for a "
        "falsifiable hypothesis (use graph_hypothesis_add) or a confirmed vulnerability (use "
        "record_finding); a note may reference those by id in `refs`.",
        {"category": {"type": "string", "enum": _CATEGORIES},
         "note": {"type": "string", "description": "the substance — a sentence or two, tight"},
         "tags": {"type": "array", "items": {"type": "string"}, "description": "your own lowercase facets"},
         "surface": {"type": "string", "description": "the attack surface it's about, if any"},
         "refs": {"type": "array", "items": {"type": "string"},
                  "description": "ids this note relates to, e.g. 'H-3', a finding id"}},
        ["category", "note"]),
    _fn("note_search",
        "Search your notebook by text and/or category. Use this before assuming what you noted "
        "earlier or whether you already ruled something out — do not guess.",
        {"query": {"type": "string"},
         "category": {"type": "string", "enum": _CATEGORIES}},
        []),
    _fn("note_resolve",
        "Close a 'todo' or 'dead-end' note once it's handled or no longer relevant, with a "
        "reason. To reopen one, pass a reason starting with 'reopen:'.",
        {"note_ref": {"type": "string", "description": "N-<n> or id"},
         "reason": {"type": "string"}},
        ["note_ref", "reason"]),
    _fn("note_promote",
        "Turn a notebook note (usually a 'todo') into a real hypothesis you'll test: the note "
        "text becomes the hypothesis rationale and the note is resolved with a link back. Use "
        "this when a note is now concrete enough to be a falsifiable claim.",
        {"note_ref": {"type": "string", "description": "N-<n> or id of the note to promote"},
         "title": {"type": "string", "description": "short name for the hypothesis"},
         "claim": {"type": "string", "description": "the falsifiable claim"},
         "impact": {"type": "integer", "description": "1-5"},
         "confidence_band": {"type": "string", "enum": ["low", "medium", "high"]},
         "confidence_reason": {"type": "string"},
         "phase": {"type": "string", "enum": ["INTAKE", "RECON", "ANALYSIS", "VALIDATION", "REPORT", "CLOSEOUT"]},
         "surface": {"type": "string"}},
        ["note_ref", "title", "claim", "impact", "confidence_band", "confidence_reason", "phase"]),
    _fn("technique_recall",
        "Search techniques you saved on PAST engagements (every note you filed as 'technique' is "
        "kept in a global library). Call this when you reach a surface or bug class you've "
        "worked before — e.g. technique_recall(surface='/api/orders') — instead of re-deriving "
        "an approach from scratch.",
        {"query": {"type": "string"},
         "surface": {"type": "string", "description": "the attack surface you're on"}},
        []),
]


def dispatch(service: NotebookService, tool_name: str, args: dict, *, graph=None) -> dict:
    """Run one notebook tool. Errors come back as {"ok": False, ...} so a model mistake is a
    recoverable tool result, not a crashed turn. `graph` (a HypothesisGraphService) is required
    only for note_promote."""
    try:
        if tool_name == "note_add":
            return {"ok": True, **service.add_note(
                category=args["category"], note=args["note"],
                tags=args.get("tags"), surface=args.get("surface"), refs=args.get("refs"),
                chat_message_id=args.get("chat_message_id"),
            )}
        if tool_name == "note_search":
            return {"ok": True, "results": service.search(
                args.get("query", ""), args.get("category"),
            )}
        if tool_name == "technique_recall":
            return {"ok": True, "results": service.recall_techniques(
                args.get("query", ""), args.get("surface"),
            )}
        if tool_name == "note_resolve":
            return {"ok": True, **service.resolve_note(args["note_ref"], args["reason"])}
        if tool_name == "note_promote":
            return {"ok": True, **service.promote_note(
                args["note_ref"], graph, title=args["title"], claim=args["claim"],
                phase=args["phase"], impact=int(args["impact"]),
                confidence_band=args["confidence_band"], confidence_reason=args["confidence_reason"],
                surface=args.get("surface"), origin_ref=args.get("origin_ref"),
            )}
        return {"ok": False, "error": f"unknown notebook tool {tool_name!r}"}
    except (NotebookValidationError, NotFoundError, ValueError, KeyError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
