"""The verifier (AGENT_ARCHITECTURE.md §2.3): runs once per candidate finding and tries to prove
it *wrong*. Refutation is its only success condition; a verifier that cannot refute is the signal
that the finding holds.

Two invariants this module exists to hold, both load-bearing:

  1. **A different model instance from the proposer (§2.3, §14.1 A, §14.3).** A model asked to
     check its own work agrees with itself nearly always, so the verifier is constructed with its
     own provider *and* the proposer's model identity, and refuses to run if they are the same
     family. The owner's decision was explicit: a different model *family*, configurable in
     roe.json — not merely a different seed, which does not buy independence. The check is
     structural (at construction), so a wave cannot accidentally verify a finding with the model
     that found it.

  2. **"Could not refute" is a first-class outcome (§2.7, the flaw at §14 "the verifier carries
     the exact flaw §2.7 was written about").** A role whose only success is finding fault will
     find fault. So the verdict space is three values, none of them privileged by the prompt:
       * ``refuted`` — the finding is wrong; the verifier's success condition.
       * ``could_not_refute`` — the verifier tried and could not; the finding stands. Equally
         valid, and what an honest verifier returns most of the time.
       * ``confirmed_not_reproducible`` — it worked when found, the raw evidence says so, but it
         no longer reproduces (§14.2 H). This is a *stronger* report than silence, not a failure,
         so it carries a reason from a closed list — and that list includes ``cause_unknown`` as a
         first-class option, because forcing a model to pick a reason it does not know is how
         fabricated reasons get recorded as fact.

The verifier does not write the finding, raise its assurance, or submit anything — it returns a
verdict. The caller (the wave / finding gate) decides what to do with a refutation. This module's
job is the independence invariant and an honest verdict, nothing downstream of that.
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("agent.pipeline.verifier")

VERDICT_REFUTED = "refuted"
VERDICT_COULD_NOT_REFUTE = "could_not_refute"
VERDICT_NOT_REPRODUCIBLE = "confirmed_not_reproducible"
VALID_VERDICTS = (VERDICT_REFUTED, VERDICT_COULD_NOT_REFUTE, VERDICT_NOT_REPRODUCIBLE)

# §14.2 H: the closed list of reasons a finding that worked no longer reproduces. "cause_unknown"
# is deliberately one of them — an honest "I don't know why" beats a fabricated specific reason.
NOT_REPRODUCIBLE_REASONS = (
    "patched", "session_expired", "rate_limited", "waf_blocking", "state_consumed", "cause_unknown",
)
_DEFAULT_REASON = "cause_unknown"

_SYSTEM = (
    "You are an independent verifier in a security-testing engagement. A worker has proposed a "
    "finding. Your job is to try to prove it WRONG from the evidence given — not to confirm it. "
    "Three outcomes are equally acceptable and you must choose honestly between them:\n"
    f"  - \"{VERDICT_REFUTED}\": the evidence does not actually support the finding (it is a false "
    "positive, the payload did not really execute, the response was misread, the impact is not "
    "demonstrated).\n"
    f"  - \"{VERDICT_COULD_NOT_REFUTE}\": you tried and the evidence holds up; you cannot disprove "
    "it. This is a perfectly valid answer — do NOT invent a flaw to justify refuting.\n"
    f"  - \"{VERDICT_NOT_REPRODUCIBLE}\": the evidence shows it genuinely worked when found, but it "
    "would no longer reproduce now. Give a reason from exactly this list: "
    f"{', '.join(NOT_REPRODUCIBLE_REASONS)} — use cause_unknown if you do not know; do not guess.\n"
    "Reply with ONLY a JSON object, no prose: "
    '{"verdict": "<one of the three>", "reason": "<closed-list reason or null>", '
    '"rationale": "<one or two sentences>"}.'
)


class SameModelAsProposerError(ValueError):
    """Raised when the verifier would run on the same model family that proposed the finding — the
    §2.3/§14.3 independence invariant, enforced at construction rather than hoped for at runtime."""


def model_family(model_id: str | None) -> str:
    """The provider/family of a model id, for the independence check. OpenRouter ids are
    ``vendor/model`` (``openai/gpt-4o-mini``, ``anthropic/claude-3.5``, ``deepseek/deepseek-chat``),
    so the family is the segment before the slash; a bare name (a local llama.cpp model) is its own
    family. Lower-cased so ``OpenAI/…`` and ``openai/…`` are the same family."""
    if not model_id:
        return ""
    s = str(model_id).strip().lower()
    return s.split("/", 1)[0] if "/" in s else s


class Verifier:
    def __init__(self, provider, *, verifier_model: str, proposer_model: str, max_tokens: int = 600):
        """`provider` is the verifier's model client; `verifier_model`/`proposer_model` are the two
        model ids whose families must differ. Constructing a verifier on the proposer's own family
        raises — there is no "verify with the same model" mode to fall into by mistake."""
        vf, pf = model_family(verifier_model), model_family(proposer_model)
        if not vf or vf == pf:
            raise SameModelAsProposerError(
                f"verifier must be a different model family from the proposer (§2.3): "
                f"verifier={verifier_model!r} (family {vf!r}), proposer={proposer_model!r} (family {pf!r})"
            )
        self.provider = provider
        self.verifier_model = verifier_model
        self.proposer_model = proposer_model
        self.max_tokens = max_tokens

    def _finding_digest(self, finding: dict) -> str:
        # What the verifier judges: the claim and the evidence, not the worker's confidence in it.
        def g(k):
            v = finding.get(k)
            return "" if v is None else str(v)
        lines = [
            "Proposed finding:",
            f"  Title: {g('title')}",
            f"  Severity: {g('severity')}",
            f"  Target: {g('target')}",
            f"  Claim/description: {g('description')}",
        ]
        if g("demonstrated_impact"):
            lines.append(f"  Demonstrated impact: {g('demonstrated_impact')}")
        evidence = finding.get("evidence") or finding.get("evidence_text")
        if evidence:
            lines.append(f"\nRaw evidence:\n{evidence}")
        else:
            # No evidence to inspect is itself grounds the verifier should weigh — say so plainly
            # rather than letting the model assume evidence it was never shown.
            lines.append("\nRaw evidence: (none provided)")
        lines.append("\nTry to refute this finding from the evidence. Reply with the JSON object.")
        return "\n".join(lines)

    def verify(self, finding: dict) -> "VerifierVerdict":
        """Return the verifier's verdict on one candidate finding. A provider error propagates (the
        caller decides whether to retry); only an unparseable *reply* is handled here, and it maps
        to ``could_not_refute`` — the verifier produced no refutation, so it cannot claim one, and
        could_not_refute never bypasses the downstream human-review gate the way a false ``refuted``
        would wrongly drop a real finding."""
        resp = self.provider.chat(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": self._finding_digest(finding)}],
            max_tokens=self.max_tokens,
            temperature=0.0,
        )
        content = resp["choices"][0]["message"].get("content") or ""
        return self._parse(content)

    def _parse(self, content: str) -> "VerifierVerdict":
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            log.warning("verifier reply had no JSON object; recording could_not_refute")
            return VerifierVerdict(VERDICT_COULD_NOT_REFUTE, None,
                                   "verifier returned no structured verdict", content)
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            log.warning("verifier reply was not valid JSON; recording could_not_refute")
            return VerifierVerdict(VERDICT_COULD_NOT_REFUTE, None,
                                   "verifier verdict was not valid JSON", content)
        verdict = str(obj.get("verdict") or "").strip()
        if verdict not in VALID_VERDICTS:
            log.warning("verifier returned unknown verdict %r; recording could_not_refute", verdict)
            return VerifierVerdict(VERDICT_COULD_NOT_REFUTE, None,
                                   f"verifier returned an unknown verdict {verdict!r}", content)
        rationale = str(obj.get("rationale") or "").strip()
        reason = None
        if verdict == VERDICT_NOT_REPRODUCIBLE:
            raw_reason = str(obj.get("reason") or "").strip().lower()
            # Coerce an unknown/missing reason to cause_unknown rather than fabricating one or
            # rejecting the verdict — §14.2 H makes "I don't know why" a first-class answer.
            reason = raw_reason if raw_reason in NOT_REPRODUCIBLE_REASONS else _DEFAULT_REASON
        return VerifierVerdict(verdict, reason, rationale, content)


class VerifierVerdict:
    """The verifier's answer on one finding: the verdict, a closed-list reason (only for
    ``confirmed_not_reproducible``), the model's rationale, and the raw reply for the record."""
    __slots__ = ("verdict", "reason", "rationale", "raw")

    def __init__(self, verdict: str, reason: str | None, rationale: str, raw: str):
        self.verdict = verdict
        self.reason = reason
        self.rationale = rationale
        self.raw = raw

    @property
    def refuted(self) -> bool:
        return self.verdict == VERDICT_REFUTED

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reason": self.reason, "rationale": self.rationale}

    def __repr__(self) -> str:
        r = f" reason={self.reason!r}" if self.reason else ""
        return f"VerifierVerdict({self.verdict!r}{r})"
