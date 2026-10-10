"""The strategist (AGENT_ARCHITECTURE.md §2.1, §2.7): the model that reads the hypothesis-graph
digest and decides which experiments the next wave runs.

It is a model, not a state machine (§2.7): the digest and the parsed choice are this module's job;
the choosing is the model's. The output is a list of `(hypothesis_id, method)` pairs — experiments
— which is exactly what `WaveOrchestrator.run_wave` dispatches and claims. The deterministic parts
(building the digest, parsing and validating the model's JSON, resolving ordinals) are unit-tested
with a fake provider; a gated test exercises a real model.

§14.2 N is honoured: the digest states what it omits ("N hypotheses not shown — refuted/parked/
done; use graph_search"), so a missing branch is a *known* unknown the model can act on rather
than an invisible one it cannot.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("agent.pipeline.strategist")

# Hypotheses still worth an experiment. Completed/abandoned are done; parked is a human "not now";
# blocked/awaiting_approval are waiting on something other than a worker.
CANDIDATE_STATUSES = ("draft", "open", "queued", "running")

_SYSTEM = (
    "You are the strategist in a security-testing engagement. You are shown the open hypotheses "
    "about the target and must choose which experiments to run next — each experiment is one "
    "hypothesis tested by one concrete method. Several workers run in parallel against different "
    "experiments, so prefer a spread of distinct, high-value experiments over piling methods onto "
    "one hypothesis. Reply with ONLY a JSON array, no prose: "
    '[{"hypothesis": <ordinal:int>, "method": "<short method>"}, ...].'
)


class Strategist:
    def __init__(self, graph_store, provider, *, max_experiments: int = 5):
        self.store = graph_store
        self.provider = provider
        self.max_experiments = max_experiments

    def _candidates(self) -> list[dict]:
        return [h for h in self.store.list_hypotheses() if h.get("lifecycle_status") in CANDIDATE_STATUSES]

    def _digest(self, candidates: list[dict], omitted: int) -> str:
        lines = ["Open hypotheses:"]
        for h in candidates:
            surface = f" [{h['surface']}]" if h.get("surface") else ""
            lines.append(f"  H-{h['ordinal']}: {h.get('title', '')}{surface} — {h.get('claim', '')}")
        if omitted:
            # §14.2 N: name what is not shown so the model knows it does not know, and can ask.
            lines.append(
                f"({omitted} other hypotheses not shown — refuted, parked, abandoned or done; "
                f"use graph_search if you need them.)"
            )
        lines.append(f"\nChoose up to {self.max_experiments} experiments to run next.")
        return "\n".join(lines)

    def decide(self) -> list[tuple[str, str]]:
        """Return the `(hypothesis_id, method)` pairs to dispatch, or [] when there is nothing open
        to test (the model is not called in that case)."""
        candidates = self._candidates()
        if not candidates:
            return []
        omitted = len(self.store.list_hypotheses()) - len(candidates)
        resp = self.provider.chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": self._digest(candidates, omitted)}],
            # Generous because a *reasoning* model (DeepSeek V4, o1, R1, …) spends output tokens on
            # its chain of thought before the answer. At 400 the reasoning alone exhausted the
            # budget — the reply came back with finish_reason=length and an empty content, so the
            # strategist parsed no array and dispatched nothing, stalling ANALYSIS on every wave.
            # The actual answer (a short JSON array) is tiny; this headroom is for the thinking.
            max_tokens=4000,
            temperature=0.0,
        )
        content = resp["choices"][0]["message"].get("content") or ""
        return self._parse(content, candidates)

    def _parse(self, content: str, candidates: list[dict]) -> list[tuple[str, str]]:
        """Pull the JSON array out of the reply and turn it into validated (hypothesis_id, method)
        pairs: the ordinal must be one of the candidates shown, the method non-empty, and the whole
        list capped at max_experiments. A reply that parses to nothing usable yields [] rather than
        a guess — the strategist chose nothing this wave, which is a valid (if rare) outcome."""
        by_ordinal = {int(h["ordinal"]): h["hypothesis_id"] for h in candidates}
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if not match:
            log.warning("strategist reply had no JSON array; dispatching nothing this wave")
            return []
        try:
            items = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.warning("strategist reply was not valid JSON; dispatching nothing this wave")
            return []
        pairs: list[tuple[str, str]] = []
        seen: set[tuple[int, str]] = set()
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                ordinal = int(item.get("hypothesis"))
            except (TypeError, ValueError):
                continue
            method = str(item.get("method") or "").strip()
            if ordinal not in by_ordinal or not method or (ordinal, method) in seen:
                continue
            seen.add((ordinal, method))
            pairs.append((by_ordinal[ordinal], method))
            if len(pairs) >= self.max_experiments:
                break
        return pairs
