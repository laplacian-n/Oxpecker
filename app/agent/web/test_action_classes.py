"""The RoE's action-class list was written down three times, and all three disagreed.

broker.py refuses outright on a class an engagement does not grant:

    if action_class not in policy.allowed_action_classes:
        refuse(f"action class {action_class!r} not permitted by current RoE", ...)

So a class missing from a list is not a cosmetic gap, it is a tool that cannot run on every
engagement built from that list -- and the failure surfaces at call time as an RoE refusal, which
reads like a deliberate policy decision rather than like a list someone forgot to update.

What was actually wrong when this test was written:

  * lab-default, the engagement the UI opens by default, granted four classes and omitted
    `browser_recon`. That class gates browser_navigate, browser_click and browser_fetch -- the
    whole live agent browser, four PRs of it, refused on the default engagement.
  * the classic UI's create call granted five and omitted `knowledge_fetch`, so no engagement
    created through any UI has ever permitted that tool.

The second omission turns out to be the right default and is now deliberate and documented rather
than accidental: knowledge_fetch is real egress to the open internet. The first was simply a stale
literal. A test cannot tell those apart by itself, which is why DEFAULT_ACTION_CLASSES exists as
one named decision and everything else is asserted against it.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from ..broker import broker as broker_mod
from . import dev_server

INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"


class KnownSetIsDerivedTest(unittest.TestCase):
    def test_the_known_set_comes_from_the_broker_and_not_from_a_literal(self):
        """The broker decides which class gates which tool; anything else is a copy.

        Asserted as equality so a class added to TOOL_ACTION_CLASS shows up here automatically
        and a class invented in dev_server does not: the previous code rebuilt this set inline at
        the one place that validated it, which was correct and unreachable by anything else.
        """
        self.assertEqual(
            dev_server.KNOWN_ACTION_CLASSES,
            frozenset(broker_mod.TOOL_ACTION_CLASS.values()) | {"http_recon_insecure"},
            "dev_server.KNOWN_ACTION_CLASSES no longer matches what the broker gates. It must be "
            "derived from broker.TOOL_ACTION_CLASS, never typed out.",
        )

    def test_every_default_is_a_class_the_broker_actually_knows(self):
        # A typo here grants nothing and refuses nothing: the RoE check compares names, so
        # "browser-recon" or "active_recon" (which this engagement really did carry once) passes
        # creation and then silently permits no tool at all.
        unknown = sorted(set(dev_server.DEFAULT_ACTION_CLASSES) - dev_server.KNOWN_ACTION_CLASSES)
        self.assertEqual(unknown, [], f"DEFAULT_ACTION_CLASSES names {unknown}, which the broker "
                                      "does not know; such an entry permits nothing")


class DefaultGrantTest(unittest.TestCase):
    def test_the_live_browser_is_granted_by_default(self):
        """The regression this file exists for.

        Nothing else in the suite connects "lab-default's action classes" to "the browser surface
        works". The browser tests all build their own engagement with the class granted, so they
        passed throughout -- the gap was only ever visible to an operator opening the default
        engagement and being told the RoE forbids it.
        """
        self.assertIn(
            "browser_recon", dev_server.DEFAULT_ACTION_CLASSES,
            "browser_recon is not granted by default, so browser_navigate, browser_click and "
            "browser_fetch are refused on every engagement created with the defaults -- the live "
            "agent browser, unusable, reported as an RoE refusal.",
        )
        gated = {t for t, c in broker_mod.TOOL_ACTION_CLASS.items() if c == "browser_recon"}
        self.assertEqual(gated, {"browser_click", "browser_fetch", "browser_navigate"},
                         "the set of tools browser_recon gates has changed; re-read whether the "
                         "default grant is still the right one")

    def test_internet_egress_and_insecure_tls_are_not_granted_by_default(self):
        """Both directions matter, and this is the direction that must never regress quietly.

        knowledge_fetch performs a real HTTP GET against the open internet and brings remote
        content into the agent's context; http_recon_insecure turns off TLS verification. Either
        one appearing in the defaults would widen what every new engagement authorises, without
        anyone asking the operator who signed it off.
        """
        for cls in ("knowledge_fetch", "http_recon_insecure"):
            self.assertNotIn(
                cls, dev_server.DEFAULT_ACTION_CLASSES,
                f"{cls} is granted by default. It must be an explicit, per-engagement choice.",
            )

    def test_the_default_engagement_carries_the_defaults_and_not_its_own_list(self):
        self.assertEqual(
            dev_server._engagements["lab-default"].allowed_action_classes,
            dev_server.DEFAULT_ACTION_CLASSES,
            "lab-default has drifted from DEFAULT_ACTION_CLASSES again. It should hold the shared "
            "list, not a literal of its own -- the literal is how browser_recon went missing.",
        )

    def test_every_class_an_operator_can_grant_says_what_it_does(self):
        """The endpoint renders these as the label beside a checkbox.

        A class with no note is a checkbox that asks the operator to authorise something the UI
        will not name, which for the two non-default ones is the difference between an informed
        decision and a guess.
        """
        missing = sorted(dev_server.KNOWN_ACTION_CLASSES - set(dev_server.ACTION_CLASS_NOTES))
        self.assertEqual(missing, [], f"no operator-facing note for {missing}")


class EndpointTest(unittest.TestCase):
    def test_it_reports_every_class_with_its_tools_and_default_flag(self):
        rows = {r["action_class"]: r for r in dev_server.list_action_classes()}
        self.assertEqual(set(rows), set(dev_server.KNOWN_ACTION_CLASSES))
        for cls, row in rows.items():
            self.assertEqual(row["default"], cls in dev_server.DEFAULT_ACTION_CLASSES, cls)
            expected = sorted(t for t, c in broker_mod.TOOL_ACTION_CLASS.items() if c == cls)
            self.assertEqual(row["tools"], expected, f"{cls} reports the wrong tools")
        # http_recon_insecure is derived from a request's arguments, not from a tool name, so it
        # correctly gates no tool by name. Pinned because an empty list looks like a bug.
        self.assertEqual(rows["http_recon_insecure"]["tools"], [])
        self.assertTrue(rows["knowledge_fetch"]["note"],
                        "the one class whose default an operator most needs explained has no note")


class ClassicUiStaysInStepTest(unittest.TestCase):
    def test_the_classic_create_form_grants_exactly_the_defaults(self):
        """The classic UI builds its create request in JavaScript with a literal array.

        It cannot import DEFAULT_ACTION_CLASSES, so this is the only thing that can keep the two
        in step. Set equality, not a subset: a class the classic UI grants and the defaults do not
        means two UIs creating differently-authorised engagements from the same button, and the
        operator cannot see which one they used.
        """
        html = INDEX_HTML.read_text(encoding="utf-8")
        m = re.search(r"allowed_action_classes:\s*\[([^\]]*)\]", html)
        self.assertIsNotNone(m, "the classic UI's create call no longer passes "
                                "allowed_action_classes; if it now fetches /api/action-classes, "
                                "delete this test and say so in the commit")
        listed = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        self.assertEqual(
            listed, set(dev_server.DEFAULT_ACTION_CLASSES),
            "static/index.html's create call and dev_server.DEFAULT_ACTION_CLASSES disagree.\n"
            f"  the classic UI grants, the defaults do not: {sorted(listed - set(dev_server.DEFAULT_ACTION_CLASSES)) or 'nothing'}\n"
            f"  the defaults grant, the classic UI does not: {sorted(set(dev_server.DEFAULT_ACTION_CLASSES) - listed) or 'nothing'}",
        )


if __name__ == "__main__":
    unittest.main()
