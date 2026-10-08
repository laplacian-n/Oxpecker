"""Tests that the prompt the runtime sends keeps a byte-stable prefix as a turn grows.

Prompt caching is prefix-matched: the provider can only reuse work for the longest prefix of
this request that is byte-identical to the previous one. Any change anywhere in that prefix —
one dropped message, one re-truncated body, one incrementing counter — throws away everything
after it. On a metered API the difference is paying a cache-read rate for the bulk of a prompt
versus paying full price for all of it, on every tool round of every turn.

That makes "the prefix did not change" a property worth asserting directly, and it is testable
without a provider and without spending anything: build a growing conversation, run the real
`_fit_context` over it round by round, and compare the prefixes. The paid end-to-end check
(`usage.cache_read > 0` on a second turn) belongs in the smoke test; this is the part that can
fail in CI and name the cause.

What this would have caught before: `_fit_context` dropped the OLDEST messages once the
conversation passed its budget, and truncated message content in place, so the prefix changed
on every round of every long turn — the cache could never hit at all.

Run directly: `python3 -m agent.web.test_context_caching`.
"""
from __future__ import annotations

import sys
import unittest

from . import dev_server as d

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _prefix_bytes(messages: list[dict], n: int) -> str:
    """The first n messages rendered exactly as their bytes would be sent."""
    return repr([(m.get("role"), m.get("content")) for m in messages[:n]])


def _conversation(rounds: int, chunk: int) -> list[dict]:
    """A system message, an operator objective, then `rounds` assistant/tool-result pairs."""
    msgs = [
        {"role": "system", "content": "SYSTEM PROMPT " + "s" * 2000},
        {"role": "user", "content": "Objective: test http://127.0.0.1:3000/ for reflected XSS."},
    ]
    for i in range(rounds):
        msgs.append({"role": "assistant", "content": f"Calling http_request round {i}."})
        msgs.append({"role": "user", "content": f"Tool results round {i}:\n" + "r" * chunk})
    return msgs


def main() -> int:
    print("== the budget comes from the provider's declared window, not a hardcoded number ==")
    # A 32768-token window must reproduce the shipped character budget exactly, or this change
    # moved the llama path's HTTP-400 headroom while claiming not to.
    check("a 32k window reproduces the shipped MAX_INPUT_CHARS exactly",
          int(32768 * d.INPUT_CHARS_PER_CTX_TOKEN) == d.MAX_INPUT_CHARS,
          f"{int(32768 * d.INPUT_CHARS_PER_CTX_TOKEN)} != {d.MAX_INPUT_CHARS}")
    check("a larger window scales the budget up proportionally",
          int(131072 * d.INPUT_CHARS_PER_CTX_TOKEN) == d.MAX_INPUT_CHARS * 4,
          f"{int(131072 * d.INPUT_CHARS_PER_CTX_TOKEN)}")

    print("\n== _fit_context leaves the prefix byte-identical as a turn grows past the budget ==")
    budget = d._input_char_budget()
    # Each round adds ~6000 chars, so ~16 rounds crosses a 90000-char budget and the rest run
    # well past it — the regime where the old implementation dropped from the front every time.
    chunk = 6000
    rounds_needed = (budget // chunk) + 12
    seal = d.SEALED_HEAD_MESSAGES
    prefixes, trimmed_sizes, over_budget_rounds = [], [], 0
    for r in range(2, rounds_needed):
        sent = d._fit_context(_conversation(r, chunk))
        prefixes.append(_prefix_bytes(sent, seal))
        trimmed_sizes.append(sum(len(m.get("content") or "") for m in sent))
        if sum(len(m.get("content") or "")
               for m in _conversation(r, chunk)) > budget:
            over_budget_rounds += 1

    check("the test actually exercised the over-budget path (otherwise it proves nothing)",
          over_budget_rounds >= 5, f"only {over_budget_rounds} of {len(prefixes)} rounds were over budget")
    check("every round sent the identical sealed prefix",
          len(set(prefixes)) == 1,
          f"{len(set(prefixes))} distinct prefixes across {len(prefixes)} rounds")
    check("the system message is still first",
          d._fit_context(_conversation(rounds_needed, chunk))[0]["role"] == "system")
    check("the operator's objective survives, which the old front-dropping trim destroyed",
          "Objective:" in str(d._fit_context(_conversation(rounds_needed, chunk))[1]["content"]))

    print("\n== the trim actually trims ==")
    full = _conversation(rounds_needed, chunk)
    sent = d._fit_context(full)
    check("an over-budget conversation comes back shorter than it went in",
          len(sent) < len(full), f"{len(sent)} vs {len(full)} messages")
    check("the newest tool results are kept, not elided",
          f"round {rounds_needed - 1}" in str(sent[-1]["content"]), str(sent[-1]["content"])[:80])
    check("an elision is marked rather than silently dropped",
          any("elided" in str(m.get("content") or "") for m in sent))

    print("\n== the elision note carries no varying text, which would itself break the cache ==")
    note = d._ELISION_NOTE["content"]
    check("the note holds no digits (a count would change the prefix every round)",
          not any(c.isdigit() for c in note), note)

    print("\n== an under-budget conversation is passed through untouched ==")
    small = _conversation(2, 100)
    out = d._fit_context(small)
    check("no elision note is added when everything fits",
          not any("elided" in str(m.get("content") or "") for m in out))
    check("the message list is unchanged",
          [(m["role"], m["content"]) for m in out] == [(m["role"], m["content"]) for m in small])

    print("\n== nothing is mutated in place: the caller's messages keep their bytes ==")
    # The old implementation rewrote m["content"] on the dicts it was handed, so a message the
    # model had already been sent changed afterwards. Bytes a provider has seen must be final.
    long_msg = _conversation(4, 100)
    long_msg.append({"role": "user", "content": "x" * (d.PER_MSG_CAP * 2)})
    before = [m["content"] for m in long_msg]
    d._fit_context(long_msg)
    check("_fit_context did not edit the caller's message contents",
          [m["content"] for m in long_msg] == before)

    print("\n== _capped bounds a message once, on the way in ==")
    capped = d._capped("y" * (d.PER_MSG_CAP * 3))
    check("an oversized message is capped", len(capped) < d.PER_MSG_CAP + 100, str(len(capped)))
    check("capping is idempotent, so re-capping cannot change settled bytes",
          d._capped(capped) == capped)
    check("a message within the cap is returned unchanged", d._capped("short") == "short")

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


class TestContextCaching(unittest.TestCase):
    """Makes the checks above run under `unittest discover` as well as standalone.

    The project's other print-and-check suites are run by hand, which is fine for the ones a
    person reaches for deliberately. A prompt-cache regression is the opposite: it is silent,
    it only shows up on a bill, and nobody will think to run this file. So it also has to be
    part of the default suite — a check that is not in the default run is a check that stops
    being run.
    """

    def test_prompt_prefix_stays_cacheable(self):
        self.assertEqual(main(), 0, f"failed checks: {FAIL}")


if __name__ == "__main__":
    sys.exit(main())
