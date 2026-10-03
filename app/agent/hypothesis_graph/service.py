"""Hypothesis Graph State Service — the single entry point the agent (and CLI) use to mutate the
graph. Per doc 2 §12: graph mutations are NOT target actions, so they go through this service
(which validates and writes append-only events) rather than the execution broker. Only the
experiment's own target-touching tool calls go through the broker, and the experiment stores that
`broker_action_ref` to tie reasoning to authorization.

The service's real job over the raw store is orchestration: after an observation lands it
recomputes the (deterministic) coverage, surfaces the engine's suggested confidence band, and
builds the context block the runner injects. It never lets the model do the store's deterministic
work, and never lets the store make the model's judgment calls.
"""
from __future__ import annotations

import time
from pathlib import Path

from . import engine
from .store import GraphValidationError, HypothesisGraphStore, NotFoundError


class HypothesisGraphService:
    def __init__(self, engagement_dir: Path):
        self.store = HypothesisGraphStore(engagement_dir)

    # -- mutations the agent calls (mirrored by the MCP tools) -------------------------------

    def add_hypothesis(self, **kwargs) -> dict:
        hid = self.store.create_hypothesis(**kwargs)
        h = self.store.get_hypothesis(hid)
        return {"hypothesis_id": hid, "ordinal": h["ordinal"]}

    def start_attempt(self, hypothesis_ref: str, **kwargs) -> dict:
        hid = self._resolve(hypothesis_ref)
        xid = self.store.start_experiment(hid, **kwargs)
        return {"experiment_id": xid}

    def complete_attempt(
        self, experiment_id: str, *, status: str, observed_result: str,
        observation_summary: str, polarity: str, strength: str,
        evidence_refs: list[str] | None = None, audit_refs: list[str] | None = None,
        input_tokens: int = 0, output_tokens: int = 0, chat_result_message_id: str | None = None,
    ) -> dict:
        """Complete an attempt AND record its interpreted observation in one call — an attempt
        without an interpretation is not a finished test. Recomputes coverage deterministically
        and returns the engine's suggested confidence band so the caller can compare it to what
        it's about to assert."""
        row = None
        for h in self.store.list_hypotheses():
            for x in self.store.list_experiments(h["hypothesis_id"]):
                if x["experiment_id"] == experiment_id:
                    row = (h["hypothesis_id"], h)
                    break
            if row:
                break
        if row is None:
            raise NotFoundError(experiment_id)
        hid, _ = row
        self.store.complete_experiment(
            experiment_id, status=status, observed_result=observed_result,
            evidence_refs=evidence_refs, audit_refs=audit_refs,
            input_tokens=input_tokens, output_tokens=output_tokens,
            chat_result_message_id=chat_result_message_id,
        )
        self.store.add_observation(
            experiment_id, hid, summary=observation_summary, polarity=polarity,
            strength=strength, evidence_refs=evidence_refs,
        )
        # deterministic coverage recompute + advisory confidence suggestion
        h = self.store.get_hypothesis(hid)
        exps_now = self.store.list_experiments(hid)
        self.store.set_coverage(hid, engine.coverage(h, exps_now))
        band, reason = engine.suggest_confidence_band(self.store.list_observations(hid))
        return {"suggested_confidence_band": band, "suggested_reason": reason, "coverage": engine.coverage(h, exps_now)}

    def set_verdict(self, hypothesis_ref: str, verdict: str, *, confidence_band=None, confidence_reason=None) -> dict:
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        v = self.store.set_verdict(
            hid, h["version"], verdict, confidence_band=confidence_band, confidence_reason=confidence_reason
        )
        return {"hypothesis_id": hid, "version": v}

    def set_confidence(self, hypothesis_ref: str, band: str, reason: str) -> dict:
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        return {"version": self.store.set_confidence(hid, h["version"], band, reason)}

    def park(self, hypothesis_ref: str, reason: str, *, actor: str = "agent") -> dict:
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        # tag an operator's reason so the every-turn graph digest makes clear a human shelved this,
        # not the model — the model then sees it parked and stops spending effort on it.
        if actor == "operator" and reason and not reason.startswith("[operator]"):
            reason = f"[operator] {reason}"
        return {"version": self.store.set_lifecycle_status(
            hid, h["version"], "parked", reason=reason, actor=actor)}

    def abandon(self, hypothesis_ref: str, reason: str, *, actor: str = "agent") -> dict:
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        return {"version": self.store.set_lifecycle_status(
            hid, h["version"], "abandoned", reason=reason, actor=actor)}

    def reopen(self, hypothesis_ref: str, *, actor: str = "agent") -> dict:
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        return {"version": self.store.set_lifecycle_status(hid, h["version"], "open", actor=actor)}

    def add_note(self, hypothesis_ref: str, text: str, *, actor: str = "operator") -> dict:
        hid = self._resolve(hypothesis_ref)
        self.store.add_operator_note(hid, text, actor=actor)
        return {"ok": True}

    def link(self, from_ref: str, to_ref: str, edge_type: str, *, reason: str = "", evidence_refs=None) -> dict:
        return {"edge_id": self.store.add_edge(
            self._resolve(from_ref), self._resolve(to_ref), edge_type, reason=reason, evidence_refs=evidence_refs
        )}

    def set_active_path(self, refs: list[str], reason: str) -> dict:
        path = [self._resolve(r) for r in refs]
        self.store.set_active_path(path, reason)
        return {"path": path, "reason": reason}

    # -- retrieval (the anti-forgetting "fetch instead of guess" tools) ----------------------

    def graph_search(self, query: str, *, limit: int = 10) -> list[dict]:
        """Returns up to `limit` matching nodes with a one-line digest each (doc 1 §7.6 policy).
        Matches ordinal (H-12 / 12), title, claim, surface — case-insensitive substring."""
        q = query.strip().lower().lstrip("h").lstrip("-")
        out = []
        for h in self.store.list_hypotheses():
            hay = f"{h['ordinal']} {h['title']} {h['claim']} {h.get('surface') or ''}".lower()
            if q in hay:
                out.append({
                    "ordinal": h["ordinal"], "hypothesis_id": h["hypothesis_id"],
                    "digest": engine.digest_line(h, self.store.list_experiments(h["hypothesis_id"])),
                })
            if len(out) >= limit:
                break
        return out

    def graph_read_branch(self, hypothesis_ref: str, *, depth: int = 3, mode: str = "summary") -> dict:
        """Read one branch to `depth` — the backtrack tool (doc 1 §7.6). `summary` returns digest
        lines (cheap); `full` returns each node's claim/rationale/latest result. Depth is capped so
        one call can't pull the whole graph into context."""
        if mode not in ("summary", "full"):
            raise GraphValidationError("mode must be 'summary' or 'full'")
        hid = self._resolve(hypothesis_ref)
        depth = max(1, min(depth, 6))
        nodes, frontier, seen = [], [(hid, 0)], set()
        while frontier:
            node_id, d = frontier.pop(0)
            if node_id in seen or d > depth:
                continue
            seen.add(node_id)
            h = self.store.get_hypothesis(node_id)
            exps = self.store.list_experiments(node_id)
            if mode == "summary":
                nodes.append({"ordinal": h["ordinal"], "digest": engine.digest_line(h, exps)})
            else:
                latest = exps[-1]["observed_result"] if exps else None
                nodes.append({
                    "ordinal": h["ordinal"], "title": h["title"], "claim": h["claim"],
                    "rationale": h["rationale"], "status": h["lifecycle_status"],
                    "verdict": h["verdict"], "confidence_band": h["confidence_band"],
                    "latest_result": latest,
                })
            for child in self.store.children_of(node_id):
                frontier.append((child["hypothesis_id"], d + 1))
        return {"root_ordinal": self.store.get_hypothesis(hid)["ordinal"], "depth": depth, "nodes": nodes}

    # -- read model for a UI (docs/hypothesis-graph-ui-spec.md) ------------------------------

    def overview(self, *, current_phase: str | None = None) -> dict:
        """The graph-canvas payload — every node at summary weight (§10 of the UI spec: detail
        loads separately, on drawer open) plus every edge and the active-path state. Cheap
        enough to poll on graph_version change rather than needing a push channel for MVP 1."""
        now = time.time()
        hyps = self.store.list_hypotheses()
        by_id = {h["hypothesis_id"]: h for h in hyps}
        exps_by_h = {h["hypothesis_id"]: self.store.list_experiments(h["hypothesis_id"]) for h in hyps}
        obs_by_h = {h["hypothesis_id"]: self.store.list_observations(h["hypothesis_id"]) for h in hyps}
        nodes = []
        for h in hyps:
            exps = exps_by_h[h["hypothesis_id"]]
            comps = engine.priority_components(h, exps, current_phase=current_phase)
            parent = by_id.get(h["primary_parent_id"])
            nodes.append({
                "hypothesis_id": h["hypothesis_id"], "ordinal": h["ordinal"], "title": h["title"],
                "claim": h["claim"], "surface": h.get("surface"), "phase_created": h["phase_created"],
                "lifecycle_status": h["lifecycle_status"], "verdict": h["verdict"],
                "confidence_band": h["confidence_band"],
                "coverage": engine.coverage(h, exps),
                "primary_parent_id": h["primary_parent_id"], "direct_tokens": h["direct_tokens"],
                "attempt_count": len(exps),
                "priority_tier": engine.priority_tier(h, comps["score"]), "priority_score": comps["score"],
                "stale_reason": engine.stale_reason(
                    h, now=now, parent=parent,
                    parent_observations=obs_by_h.get(h["primary_parent_id"], ()),
                ),
            })
        ranked = engine.rank_open(hyps, exps_by_h, current_phase=current_phase)
        return {
            "nodes": nodes,
            "edges": self.store.list_edges(),
            "graph_state": self.store.get_graph_state(),
            "ranked_open": [
                {
                    "hypothesis_id": r["hypothesis_id"], "ordinal": r["ordinal"], "title": r["title"],
                    "claim": r["claim"], "lifecycle_status": r["lifecycle_status"],
                    "priority_tier": r["priority_tier"], "priority_score": r["priority"]["score"],
                }
                for r in ranked
            ],
        }

    def node_detail(self, hypothesis_ref: str) -> dict:
        """The detail-drawer payload (UI spec §5): full hypothesis fields, every attempt with its
        observations attached, and the direct children/synthesis targets with why they exist."""
        hid = self._resolve(hypothesis_ref)
        h = self.store.get_hypothesis(hid)
        exps = self.store.list_experiments(hid)
        observations = self.store.list_observations(hid)
        comps = engine.priority_components(h, exps)
        # advisory band the engine derives from observation polarity/strength — shown next to the
        # model's own asserted band ("AI estimate", UI spec §6), never replacing it.
        sug_band, sug_reason = engine.suggest_confidence_band(observations)
        derived = []
        for e in self.store.list_edges(hid):
            if e["from_hypothesis_id"] == hid:
                child = self.store.get_hypothesis(e["to_hypothesis_id"])
                derived.append({
                    "ordinal": child["ordinal"], "title": child["title"],
                    "edge_type": e["edge_type"], "reason": e["reason"],
                })
        return {
            **h,
            "coverage": engine.coverage(h, exps),
            "operator_notes": self.store.list_operator_notes(hid),
            "suggested_confidence": {"band": sug_band, "reason": sug_reason},
            "priority": {
                "tier": engine.priority_tier(h, comps["score"]), "score": comps["score"], "components": comps,
            },
            "experiments": [
                {**x, "observations": [o for o in observations if o["experiment_id"] == x["experiment_id"]]}
                for x in exps
            ],
            "derived": derived,
        }

    # -- context injection (what the runner puts in the prompt each turn) --------------------

    def build_context_block(self, *, current_phase: str | None = None, top_open: int = 3) -> str:
        """[CURRENT HYPOTHESES] block: full-graph digest + the top-N actionable hypotheses by
        priority, with active path guaranteed in the digest. This is the working set injected
        every turn (doc 1 §6.3 / doc 2 §13.9) — bounded, and it names the retrieval tools so the
        model fetches rather than guesses anything not shown."""
        digest = engine.build_digest(self.store, current_phase=current_phase)
        hyps = self.store.list_hypotheses()
        exps_by_h = {h["hypothesis_id"]: self.store.list_experiments(h["hypothesis_id"]) for h in hyps}
        ranked = engine.rank_open(hyps, exps_by_h, current_phase=current_phase)[:top_open]
        lines = [digest, "", "TOP OPEN (by priority):"]
        if not ranked:
            lines.append("  (none actionable)")
        for r in ranked:
            lines.append(
                f"  H-{r['ordinal']} [{r['priority_tier']}] \"{r['title']}\" — {r['claim'][:70]}"
            )
        lines.append(
            "\nTest a hypothesis with graph_attempt_start → graph_attempt_complete → "
            "graph_set_verdict; the verdict and its evidence must land in the graph, not only in "
            "your reply."
        )
        lines.append(
            "If you need a node/result not shown above, call graph_search or graph_read_branch — "
            "do not guess an id or a past result."
        )
        return "\n".join(lines)

    # -- helpers -----------------------------------------------------------------------------

    def _resolve(self, ref: str) -> str:
        """Accepts a hypothesis_id ('h_...'), an ordinal int, or an 'H-12'/'12' alias."""
        ref = str(ref).strip()
        if ref.startswith("h_"):
            return ref
        num = ref.lower().lstrip("h").lstrip("-")
        if num.isdigit():
            return self.store.get_by_ordinal(int(num))["hypothesis_id"]
        raise GraphValidationError(f"cannot resolve hypothesis ref {ref!r} (use an id, ordinal, or H-<n>)")
