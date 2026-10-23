"""The hypothesis proposer: one bounded model call that turns a recon surface into testable
hypotheses the model actually chose (AGENT_ARCHITECTURE.md §2.1, §2.7).

The deterministic seeder (recon_seed.py) guarantees a non-empty graph by extracting the mechanical
facts from an http_recon response — missing headers, insecure cookies, linked endpoints, "a form
is present". That is a safety net, not reasoning: it never proposes "this `/rest/track-order/{id}`
endpoint looks like an IDOR", "this GraphQL endpoint should be tested for introspection + injection
through its filter argument". The model's own recon worker *could*, but it only runs when the
deterministic seeder found nothing (a full model crawl is expensive and was observed to wedge the
run), so on any readable target the creative hypotheses were never formed — the "stop forcing
mechanical hypotheses, let it think and choose" gap.

This closes that gap with the same reliability-over-hope pattern as the Strategist (strategist.py):
*not* a tool-calling crawl the model may or may not follow through on, but a single chat call whose
JSON reply is parsed and validated deterministically, then written to the graph in code. It is
bounded (one call, capped output) so it cannot crawl, and it is handed the attack-technique cards
the engagement's technique KB surfaced for this surface (knowledge/claude_red), so the model chooses
from a real catalogue rather than from whatever it happens to recall.

Pure in the way that matters for tests: the provider and the parse are this module's job; the
choosing is the model's. `propose()` returns hypothesis kwargs (minus the parent id) — exactly what
`EngagementStore.create_hypothesis(primary_parent_id=…, **kw)` takes, the same contract
recon_seed.build_recon_hypotheses returns on.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("agent.pipeline.hypothesis_proposer")

_SYSTEM = (
    "You are a security strategist enumerating a target's attack surface. Given what recon "
    "observed and a set of known attack techniques, propose the most promising hypotheses to test "
    "— each a specific, falsifiable claim that a concrete weakness exists on a named part of the "
    "surface. Choose based on what the surface actually suggests: an endpoint that takes an id is "
    "an access-control/IDOR candidate, a form or query parameter is an injection candidate, an "
    "auth token is a candidate for forgery or weak validation. Do NOT pad the list with generic "
    "checks that do not fit this target, and do not repeat hypotheses already listed. Prefer a few "
    "distinct, high-value ideas. Keep each claim to ONE short sentence — long claims overflow the "
    "reply and are lost. Reply with ONLY a JSON array, no prose:\n"
    '[{"title": "<short>", "claim": "<the falsifiable claim>", "surface": "<what it targets>", '
    '"impact": <1-5>}].\n'
    "Return an empty array [] if the surface warrants nothing beyond what is already listed."
)

# A terse second ask used only when the first reply did not yield a usable array. A reasoning model
# (DeepSeek V4, o1, R1) sometimes spends its whole budget thinking and returns empty or prose
# `content`; a short, format-only reprompt with no room for prose usually produces the array. One
# retry, so a stubborn model costs at most two calls per root, never a loop.
_RETRY = (
    "Reply with ONLY the JSON array, nothing before or after it, no reasoning, no code fence:\n"
    '[{"title": "<short>", "claim": "<falsifiable claim>", "surface": "<what it targets>", '
    '"impact": <1-5>}]\n'
    "If you genuinely have no hypothesis to add, reply exactly []."
)

# impact 1..5 -> the graph's confidence band for a *proposed* (not yet tested) hypothesis. A
# proposal is a lead, not evidence, so even a high-impact idea is recorded at a modest band; the
# worker that tests it sets the real verdict/confidence.
_BAND_FOR_IMPACT = {5: "medium", 4: "medium", 3: "low", 2: "low", 1: "low"}


class HypothesisProposer:
    def __init__(self, provider, *, max_hypotheses: int = 6):
        self.provider = provider
        self.max_hypotheses = max_hypotheses

    def _digest(self, target: str, observations: list[str], techniques: list[dict],
                existing_titles: list[str]) -> str:
        lines = [f"Target: {target}", ""]
        if observations:
            lines.append("Recon observed this surface:")
            lines += [f"  - {o}" for o in observations[:20]]
        else:
            lines.append("Recon observed little; reason from the target and the techniques below.")
        if existing_titles:
            lines.append("")
            lines.append("Already hypothesised (do not repeat these):")
            lines += [f"  - {t}" for t in existing_titles[:20]]
        if techniques:
            lines.append("")
            lines.append("Relevant attack techniques (from the technique KB):")
            for t in techniques[:6]:
                body = (t.get("body") or "").strip().replace("\n", " ")
                lines.append(f"  - {t.get('title', '')}: {body[:240]}")
        lines.append("")
        lines.append(f"Propose up to {self.max_hypotheses} hypotheses as the JSON array.")
        return "\n".join(lines)

    def propose(self, target: str, observations: list[str], techniques: list[dict] | None = None,
                existing_titles: list[str] | None = None) -> list[dict]:
        """Return hypothesis kwargs (title/claim/rationale/impact/confidence_band/…/surface) for the
        model's chosen hypotheses, deduped against `existing_titles` and capped at max_hypotheses.
        [] when the model proposes nothing usable — a valid outcome, not an error (the deterministic
        seeder has already guaranteed a testable graph)."""
        messages = [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": self._digest(target, observations, techniques or [],
                                                     existing_titles or [])},
        ]
        content = self._ask(messages)
        out = self._parse(content, existing_titles or [])
        if out:
            return out
        # The first reply produced nothing usable. Before giving up (proposing nothing IS valid),
        # try once more with a format-only reprompt — a reasoning model often returns empty/prose
        # content on the first pass and the terse retry then yields the array.
        messages += [{"role": "assistant", "content": content or "(no content)"},
                     {"role": "user", "content": _RETRY}]
        return self._parse(self._ask(messages), existing_titles or [])

    def _ask(self, messages: list[dict]) -> str:
        resp = self.provider.chat(
            messages,
            # Generous headroom: a reasoning model spends output tokens thinking before the short
            # JSON answer, and the proposer's task (invent plausible attacks) draws longer chains
            # than the strategist's (pick from a list). Too small and the reply comes back
            # finish_reason=length with empty content — the "proposed nothing" we saw live.
            max_tokens=8000,
            temperature=0.2,
        )
        return resp["choices"][0]["message"].get("content") or ""

    def _extract_items(self, content: str) -> list:
        """Pull the list of hypothesis objects out of a model reply, tolerating the shapes a
        reasoning model actually emits: a bare array, an array inside a ```json fence``` or prose,
        an object that wraps the array under some key ({"hypotheses": [...]}), or a single
        hypothesis object. Returns [] (and logs a truncated raw reply) when nothing usable is found."""
        # 1. The outermost [...] anywhere in the text — covers a bare array and one in a fence/prose.
        match = re.search(r"\[.*\]", content, re.DOTALL)
        if match:
            try:
                items = json.loads(match.group(0))
                if isinstance(items, list):
                    return items
            except json.JSONDecodeError:
                pass
        # 2. The whole reply as JSON: a wrapper object with a list value, or a single hypothesis.
        try:
            obj = json.loads(content.strip().strip("`").strip())
        except (json.JSONDecodeError, AttributeError):
            obj = None
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list) and all(isinstance(x, dict) for x in v) and v:
                    return v
            if obj.get("title") and obj.get("claim"):  # a single hypothesis returned as an object
                return [obj]
        # 3. Salvage from a TRUNCATED array: a verbose reasoning model can overflow its token budget
        # mid-array (no closing ]), so neither 1 nor 2 parses. Recover every complete {...} object it
        # did finish and drop the partial tail — better four proposals than none.
        salvaged = [o for o in self._balanced_objects(content)
                    if isinstance(o, dict) and o.get("title") and o.get("claim")]
        if salvaged:
            return salvaged
        log.info("proposer reply had no usable hypotheses; raw reply (truncated): %r", content[:300])
        return []

    @staticmethod
    def _balanced_objects(text: str) -> list:
        """Every top-level {...} block in `text` that is itself valid JSON, scanning brace depth and
        honouring strings/escapes so a brace inside a string value is not miscounted. Used to rescue
        hypotheses from a reply whose surrounding array never closed."""
        out: list = []
        depth = 0
        start = -1
        in_str = False
        esc = False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        out.append(json.loads(text[start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    start = -1
        return out

    def _parse(self, content: str, existing_titles: list[str]) -> list[dict]:
        items = self._extract_items(content)
        seen = {t.strip().lower() for t in existing_titles}
        out: list[dict] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            claim = str(item.get("claim") or "").strip()
            if not title or not claim or title.lower() in seen:
                continue
            seen.add(title.lower())
            surface = str(item.get("surface") or "").strip()
            try:
                impact = max(1, min(5, int(item.get("impact", 3))))
            except (TypeError, ValueError):
                impact = 3
            out.append(dict(
                title=title, claim=claim,
                rationale="proposed by the strategist from the recon surface and known techniques",
                impact=impact, confidence_band=_BAND_FOR_IMPACT[impact],
                confidence_reason="model proposal from the recon surface — not yet tested",
                phase_created="RECON", origin_type="ai_inference", surface=surface,
            ))
            if len(out) >= self.max_hypotheses:
                break
        return out
