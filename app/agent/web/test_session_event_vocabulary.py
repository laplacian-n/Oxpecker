"""The chat UI must handle every session event dev_server can push, and no event it cannot.

This closes a bug class the project has already shipped twice. `injection_flagged` was pushed
from the moment injection screening was wired in and nothing rendered it, so the one signal that
says "this tool output tried to redirect the agent" never reached the operator -- for as long as
nobody happened to read both files. The streaming deltas were the same shape of failure from the
other side: the handler existed and the render path did not.

Nothing at build time can catch it. The event type is a bare string in a Python dict on one side
and a `case` label in a TypeScript switch on the other; adding a push is a one-line server change
that compiles, deploys, and goes silently unrendered. So the vocabulary is asserted as set
equality, in both directions:

  * pushed but unhandled -> the operator is not being told something the server is reporting;
  * handled but never pushed -> a dead branch the next reader has to rule out by hand, which is
    exactly what static/index.html's `autonomous_error` case is (only agent/web/server.py, the
    scaffold nothing launches, pushes it).

Deliberately read from dev_server.py alone. server.py is the dead scaffold -- taking its pushes
into account would require the client to carry branches no deployment can ever reach.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2]
DEV_SERVER = APP / "agent" / "web" / "dev_server.py"
CONVERSATION_TS = APP / "client" / "src" / "conversation.ts"


def _pushed_event_types() -> set[str]:
    """Every `type` dev_server.py hands to a Session.push().

    Matched on the push call itself rather than on any `"type":` key, because the file is also
    full of JSON-schema and Pydantic dicts whose `"type"` is "string"/"object"/"array" -- a
    looser regex quietly turns this test into an assertion about JSON Schema.
    """
    text = DEV_SERVER.read_text(encoding="utf-8")
    # Each push call is read from its own opening paren up to the next one (or 600 chars), and the
    # FIRST "type" inside that slice is the event. Bounding the slice on the dict's closing brace
    # instead -- the obvious way -- silently lost three events: `context`, `rag_consulted` and
    # `autonomous_done` are pushed from deeply indented call sites whose literal runs past any
    # reasonable character budget, and `autonomous_done` nests a dict of its own. The floor test
    # above is what surfaced that rather than letting the equality below report a false mismatch.
    starts = [m.start() for m in re.finditer(r"\.push\(", text)]
    types = set()
    for i, start in enumerate(starts):
        end = min(starts[i + 1] if i + 1 < len(starts) else len(text), start + 600)
        m = re.search(r'"type":\s*"([a-z_]+)"', text[start:end])
        if m:
            types.add(m.group(1))
    return types


def _handled_event_types() -> set[str]:
    text = CONVERSATION_TS.read_text(encoding="utf-8")
    m = re.search(r"HANDLED_EVENTS:\s*readonly string\[\]\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert m, "client/src/conversation.ts lost its HANDLED_EVENTS list"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


class TestSessionEventVocabulary(unittest.TestCase):
    def test_the_lists_are_both_non_trivial(self):
        """A set-equality test over two empty sets passes and proves nothing.

        Both regexes depend on source formatting -- a reflowed push call or a renamed export
        makes one side come back empty, and the equality below would then hold vacuously while
        the thing it guards went unchecked. Pin a floor instead of trusting the match.
        """
        pushed, handled = _pushed_event_types(), _handled_event_types()
        self.assertGreaterEqual(
            len(pushed), 15,
            f"only parsed {len(pushed)} pushed event types out of dev_server.py "
            f"({sorted(pushed)}); the regex has probably stopped matching the source",
        )
        self.assertGreaterEqual(
            len(handled), 15,
            f"only parsed {len(handled)} handled event types out of conversation.ts "
            f"({sorted(handled)}); the regex has probably stopped matching the source",
        )

    def test_every_pushed_event_is_handled_and_every_handled_event_is_pushed(self):
        pushed, handled = _pushed_event_types(), _handled_event_types()
        self.assertEqual(
            pushed, handled,
            "dev_server.py and client/src/conversation.ts disagree about the session event "
            "vocabulary.\n"
            f"  pushed by the server, not handled by the chat UI: {sorted(pushed - handled) or 'none'}\n"
            "    -> the operator is not being shown something the server is reporting. Add a "
            "case to reduceConversation and a test for it.\n"
            f"  handled by the chat UI, never pushed: {sorted(handled - pushed) or 'none'}\n"
            "    -> a dead branch. Remove it, or say in a comment which code path emits it "
            "(agent/web/server.py does not count -- nothing launches it).",
        )


if __name__ == "__main__":
    unittest.main()
