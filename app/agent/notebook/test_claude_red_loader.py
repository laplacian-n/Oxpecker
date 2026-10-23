"""The claude-red corpus loads into a technique KB and is recallable — knowledge-driven hypothesis
selection depends on these cards actually surfacing against a target surface.

Covers the vendored corpus shape (it is present and well-formed), the loader (idempotent, tagged
with the claude-red source), and the thing that matters at runtime: a card surfaces from
semantic_recall against a query that names its surface, so the agent sees it mid-engagement.
"""
from __future__ import annotations

import unittest

from ..knowledge import load_claude_red
from .technique_kb import TechniqueKB


class CorpusShapeTest(unittest.TestCase):
    def test_vendored_cards_are_present_and_well_formed(self):
        cards = load_claude_red.load_cards()
        self.assertGreaterEqual(len(cards), 70, "expected the full claude-red skill set")
        for c in cards:
            self.assertTrue(c["title"].strip())
            self.assertTrue(c["body"].strip())
            self.assertTrue(c["tags"], f"{c['name']} has no tags")
            self.assertTrue(c["surfaces"], f"{c['name']} has no surfaces")
            self.assertTrue(c["source"].startswith("claude-red/"))

    def test_the_web_injection_families_are_covered(self):
        names = {c["name"] for c in load_claude_red.load_cards()}
        for expected in ("offensive-sqli", "offensive-ssrf", "offensive-xss"):
            self.assertIn(expected, names)


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.db = f"/tmp/claude_red_kb_{id(self)}.db"
        self.kb = TechniqueKB(db_path=self.db)

    def tearDown(self):
        import os
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except OSError:
                pass

    def test_load_is_idempotent(self):
        cards = load_claude_red.load_cards()
        first = load_claude_red.load_into(self.kb, cards)
        self.assertEqual(first["added"], len(cards))
        self.assertEqual(first["deduped"], 0)
        self.assertEqual(self.kb.count(), len(cards))
        # Re-loading the same corpus adds nothing new — dedup on normalized title|body.
        second = load_claude_red.load_into(self.kb, cards)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["deduped"], len(cards))
        self.assertEqual(self.kb.count(), len(cards))

    def test_cards_carry_the_claude_red_source(self):
        load_claude_red.load_into(self.kb, load_claude_red.load_cards())
        self.assertTrue(all(t["source_engagement"] == "claude-red" for t in self.kb.all()))

    def test_a_card_surfaces_for_its_surface(self):
        load_claude_red.load_into(self.kb, load_claude_red.load_cards())
        hits = self.kb.semantic_recall("the login form takes a username parameter", surface="web form")
        self.assertTrue(hits, "a web card should surface for a web-form query")
        # SQLi/XSS are the obvious web-form injection families; at least one should rank.
        self.assertTrue(any("sql" in h["title"].lower() or "xss" in h["title"].lower()
                            or "injection" in h["body"].lower() for h in hits))

    def test_claude_red_cards_are_not_excluded_as_a_real_engagement(self):
        # Auto-surface excludes the *running* engagement's own notes; claude-red's sentinel source
        # must not collide with a real engagement id, or the catalogue would vanish mid-run.
        load_claude_red.load_into(self.kb, load_claude_red.load_cards())
        hits = self.kb.semantic_recall("sql injection on an api endpoint", surface="api",
                                       exclude_engagement="eng_some_real_engagement")
        self.assertTrue(hits)


if __name__ == "__main__":
    unittest.main()
