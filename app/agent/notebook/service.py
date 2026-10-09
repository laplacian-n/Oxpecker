"""Working Notebook service — the one entry point the agent (and CLI, and web read model) use.
Mirrors HypothesisGraphService: mutations go straight here (a note is not a target action, so no
broker), and this is where the every-turn context digest is built deterministically from stored
fields — never written by the model after the fact (the anti-forgetting rule the Hypothesis
Graph established).
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config
from ..engagement import emit as event_emit
from .schema import LIFECYCLE_CATEGORIES, Category, NoteStatus
from .store import NotebookStore, NotFoundError, NotebookValidationError
from .technique_kb import TechniqueKB

DIGEST_RECENT = 12
DIGEST_MAX_LINES = 30


def _owner_of(engagement_dir: Path) -> str:
    """Who owns this engagement, for technique-KB scoping (AGENT_ARCHITECTURE.md §14.1 B).

    Mirrors `web/engagement_access.py`'s `engagement_owner()` (same default-on-absence rule) by
    reading `roe.json` directly rather than importing it: the notebook is used from the CLI and
    the MCP tool server too, neither of which should have to pull in the web layer just to find
    out whose KB to use.
    """
    roe_path = Path(engagement_dir) / "roe.json"
    try:
        roe = json.loads(roe_path.read_text())
    except (OSError, json.JSONDecodeError):
        return config.WEB_UI_DEFAULT_ACCOUNT
    owner = roe.get("owner") if isinstance(roe, dict) else None
    return owner if isinstance(owner, str) and owner else config.WEB_UI_DEFAULT_ACCOUNT


class NotebookService:
    def __init__(self, engagement_dir: Path, *, kb: TechniqueKB | None = None):
        self.store = NotebookStore(engagement_dir)
        self.engagement_id = Path(engagement_dir).name
        # §14.1 B: the technique KB is scoped to whichever account owns this engagement, not the
        # single global file — otherwise build_context_block()'s every-turn semantic_recall()
        # call would hand one account's past techniques to another account's run (§5.3). The
        # `kb=` seam above is untouched so tests can still inject a throwaway KB directly.
        self.kb = kb if kb is not None else TechniqueKB(
            db_path=config.technique_kb_path_for(_owner_of(engagement_dir))
        )

    # -- mutations the agent calls -------------------------------------------------------------

    def add_note(self, **kwargs) -> dict:
        r = self.store.add_note(**kwargs)
        # §4.2 note_added — the notebook tab's count and auto-refetch react to this.
        event_emit.emit(self.engagement_id, "note_added",
                        {"note_id": r.get("note_id"), "category": kwargs.get("category")})
        # a 'technique' note is reusable by definition — overflow it into the global KB so a
        # later engagement can recall it (docs/working-notebook-spec.md §7).
        if kwargs.get("category") == Category.TECHNIQUE.value:
            try:
                note = self.store.get(r["note_id"])
                import re as _re
                m = _re.split(r"(?<=[.!?])\s+", note["note"].strip(), maxsplit=1)
                title = m[0] if len(m[0]) <= 100 else note["note"][:97].rstrip() + "…"
                self.kb.save(
                    title=title, body=note["note"],
                    tags=note["tags"], surfaces=[note["surface"]] if note.get("surface") else [],
                    source_engagement=self.engagement_id,
                )
            except Exception:
                pass  # the KB is a nice-to-have overflow, never fail the note on it
        return r

    OPERATOR_TAG = "operator"

    def add_operator_note(self, text: str, *, refs: list[str] | None = None,
                          surface: str | None = None) -> dict:
        """A note the operator typed into the web UI — e.g. on a hypothesis in the graph drawer.
        Stored as a 'misc' note tagged 'operator' so build_context_block pins it in its own
        section every turn: it's the operator's only channel for mid-run steering that isn't a
        chat message, and it must not scroll out of the digest behind newer model notes."""
        r = self.store.add_note(
            category=Category.MISC.value, note=text.strip(),
            tags=[self.OPERATOR_TAG], surface=surface, refs=list(refs or []),
        )
        event_emit.emit(self.engagement_id, "note_added",
                        {"note_id": r.get("note_id"), "category": Category.MISC.value})
        return r

    def recall_techniques(self, query: str = "", surface: str | None = None) -> list[dict]:
        # semantic_recall needs something to vectorize; a bare call (browse everything) has
        # neither, so it falls back to recall()'s "list them all" mode.
        hits = (self.kb.recall(query, surface) if not query and not surface
                else self.kb.semantic_recall(query, surface))
        return [
            {"ordinal": t["ordinal"], "title": t["title"], "body": t["body"], "tags": t["tags"],
             "surfaces": t["surfaces"], "from": t["source_engagement"]}
            for t in hits
        ]

    def resolve_note(self, note_ref: str, reason: str) -> dict:
        note = self._resolve(note_ref)
        reason = (reason or "").strip()
        if reason.lower().startswith("reopen:"):
            self.store.set_status(note["note_id"], NoteStatus.OPEN.value, reason=reason)
            return {"note_id": note["note_id"], "status": "open"}
        if not reason:
            raise NotebookValidationError("resolving a note needs a reason")
        self.store.set_status(note["note_id"], NoteStatus.RESOLVED.value, reason=reason)
        advisory = None
        if note["category"] not in LIFECYCLE_CATEGORIES:
            advisory = (f"note N-{note['ordinal']} is a '{note['category']}' note, not a todo/dead-end — "
                        "marked resolved anyway")
        return {"note_id": note["note_id"], "status": "resolved", "advisory": advisory}

    def promote_note(
        self, note_ref: str, graph, *, title: str, claim: str, phase: str,
        impact: int, confidence_band: str, confidence_reason: str,
        surface: str | None = None, origin_ref: str | None = None,
    ) -> dict:
        """Turn a note (usually a 'todo') into a real hypothesis in the graph: the note text
        becomes the hypothesis rationale (with provenance), and the note is resolved with an
        H-<n> ref back. Needs the hypothesis graph enabled for the session."""
        if graph is None:
            raise NotebookValidationError("the hypothesis graph is not enabled for this session")
        note = self._resolve(note_ref)
        if note["status"] == "resolved":
            raise NotebookValidationError(f"N-{note['ordinal']} is already resolved")
        rationale = f"{note['note']}  [promoted from notebook {note['category']} note N-{note['ordinal']}]"
        added = graph.add_hypothesis(
            title=title, claim=claim, phase_created=phase, rationale=rationale,
            impact=int(impact), confidence_band=confidence_band, confidence_reason=confidence_reason,
            surface=surface or note.get("surface"), origin_type="combined_analysis",
            origin_ref=origin_ref,
        )
        href = f"H-{added['ordinal']}"
        self.store.add_ref(note["note_id"], href)
        self.store.set_status(note["note_id"], NoteStatus.RESOLVED.value, reason=f"promoted to {href}")
        return {"hypothesis_ordinal": added["ordinal"], "hypothesis_id": added["hypothesis_id"],
                "note_resolved": f"N-{note['ordinal']}"}

    # -- retrieval (the anti-guessing tool) --------------------------------------------------

    def search(self, query: str = "", category: str | None = None, *, limit: int = 20) -> list[dict]:
        q = (query or "").strip().lower().lstrip("n").lstrip("-")
        out = []
        for n in self.store.list_notes(category=category):
            hay = f"{n['ordinal']} {n['category']} {n['note']} {' '.join(n['tags'])} {n.get('surface') or ''}".lower()
            if not q or q in hay:
                out.append(self._digest_row(n))
            if len(out) >= limit:
                break
        return out

    # -- context digest (injected every turn) ----------------------------------------------

    def build_context_block(self) -> str:
        notes = self.store.list_notes()  # newest first
        try:
            kb_n = self.kb.count()
        except Exception:
            kb_n = 0
        if not notes:
            extra = (f"\n{kb_n} technique(s) from past engagements are available — call "
                     "technique_recall(surface=…) when relevant.") if kb_n else ""
            return (
                "[NOTEBOOK — empty]\n"
                "Jot down anything cross-cutting you notice with note_add, and record any path "
                "you rule out as a 'dead-end' so you don't retry it — name what you tried and "
                "what ruled it out, not just that it 'didn't work'. This is not for hypotheses "
                "(use graph_hypothesis_add) or confirmed findings (use record_finding)." + extra
            )
        by_id = {n["note_id"]: n for n in notes}
        shown: set[str] = set()
        lines = [f"[NOTEBOOK — {len(notes)} note(s)]"]

        operator = [n for n in notes
                    if self.OPERATOR_TAG in (n["tags"] or []) and n["status"] == "open"][:5]
        if operator:
            lines.append("FROM THE OPERATOR (human steering — weigh this heavily):")
            for n in operator:
                lines.append("  " + self._line(n)); shown.add(n["note_id"])

        dead_ends = [n for n in notes if n["category"] == Category.DEAD_END.value and n["status"] == "open"]
        todos = [n for n in notes if n["category"] == Category.TODO.value and n["status"] == "open"]
        if dead_ends:
            lines.append("RULED OUT (do not retry):")
            for n in dead_ends:
                lines.append("  " + self._line(n)); shown.add(n["note_id"])
        if todos:
            lines.append("OPEN TODO (promote one with note_promote once it's a concrete claim):")
            for n in todos:
                lines.append("  " + self._line(n)); shown.add(n["note_id"])

        recent = [n for n in notes if n["note_id"] not in shown][:DIGEST_RECENT]
        if recent:
            lines.append("RECENT:")
            for n in recent:
                lines.append("  " + self._line(n)); shown.add(n["note_id"])

        if len(lines) > DIGEST_MAX_LINES:
            kept = lines[:DIGEST_MAX_LINES]
            kept.append(f"  ... {len(by_id) - len(shown)} more — call note_search")
            lines = kept
        lines.append("If you need a note not shown here, call note_search — do not guess.")
        if kb_n:
            auto_hits = self._auto_surface_techniques(notes)
            if auto_hits:
                lines.append("RELEVANT FROM PAST ENGAGEMENTS (auto-surfaced — verify before relying on it):")
                for t in auto_hits:
                    body = t["body"] if len(t["body"]) <= 140 else t["body"][:137].rstrip() + "…"
                    tags = f" #{' #'.join(t['tags'])}" if t["tags"] else ""
                    lines.append(f"  T-{t['ordinal']} (from {t['source_engagement'] or '?'}): {body}{tags}")
                lines.append("  Call technique_recall for the full text or more results.")
            lines.append(f"{kb_n} technique(s) saved from past engagements — call technique_recall"
                         "(surface=…) when you hit a surface you've tested before.")
        return "\n".join(lines)

    def _auto_surface_techniques(self, notes: list[dict], *, limit: int = 2) -> list[dict]:
        """Auto-relevance for the technique KB: use the session's own recent notes as an implicit
        query, so a matching past-engagement technique surfaces without the agent having to call
        technique_recall first (docs/working-notebook-spec.md §7's deferred item). Excludes this
        engagement's own techniques — otherwise a technique note echoes right back as "from past
        engagements" the same turn it was written. Never fails the digest: the KB is a nice-to-have."""
        recent = notes[:5]  # newest first
        query_text = " ".join(n["note"] for n in recent)
        surface = next((n.get("surface") for n in recent if n.get("surface")), None)
        if not query_text.strip() and not surface:
            return []
        try:
            return self.kb.semantic_recall(
                query_text, surface, limit=limit, min_score=0.2,
                exclude_engagement=self.engagement_id, bump=False,
            )
        except Exception:
            return []

    # -- web read model -------------------------------------------------------------------

    def overview(self) -> dict:
        notes = self.store.list_notes()
        return {
            "notes": [self._digest_row(n, full=True) for n in notes],
            "version": self.store.version(),
            "counts": self._counts(notes),
        }

    def note_detail(self, note_ref: str) -> dict:
        return {**self._resolve(note_ref)}

    # -- helpers -------------------------------------------------------------------------

    def _resolve(self, ref: str) -> dict:
        ref = str(ref).strip()
        if ref.startswith("n_"):
            return self.store.get(ref)
        num = ref.lower().lstrip("n").lstrip("-")
        if num.isdigit():
            return self.store.get_by_ordinal(int(num))
        raise NotebookValidationError(f"cannot resolve note ref {ref!r} (use an id, ordinal, or N-<n>)")

    @staticmethod
    def _line(n: dict) -> str:
        tags = f" #{' #'.join(n['tags'])}" if n["tags"] else ""
        surface = f" @{n['surface']}" if n.get("surface") else ""
        text = n["note"] if len(n["note"]) <= 160 else n["note"][:157] + "…"
        return f"N-{n['ordinal']} [{n['category']}]{surface} {text}{tags}"

    def _digest_row(self, n: dict, *, full: bool = False) -> dict:
        row = {
            "ordinal": n["ordinal"], "note_id": n["note_id"], "category": n["category"],
            "note": n["note"], "tags": n["tags"], "surface": n.get("surface"),
            "status": n["status"], "line": self._line(n),
        }
        if full:
            row.update({
                "refs": n["refs"], "resolved_reason": n.get("resolved_reason"),
                "chat_message_id": n.get("chat_message_id"),
                "created_at": n["created_at"], "updated_at": n["updated_at"],
            })
        return row

    @staticmethod
    def _counts(notes: list[dict]) -> dict:
        out: dict[str, int] = {}
        for n in notes:
            out[n["category"]] = out.get(n["category"], 0) + 1
        return out
