"""Corpus parsers — run against the real cloned repos under corpus_src/ if present (skip
gracefully otherwise, matching this project's established preference for real fixtures over
mocks — see doc/handoff.md's testing conventions). No network or embedding server needed."""
from __future__ import annotations

import unittest
from pathlib import Path

from .. import config
from .parsers import parse_gtfobins, parse_lolbas, parse_payloads_all_the_things

_CORPUS_SRC = config.KNOWLEDGE_RAG_DIR / "corpus_src"


def _skip_reason(name: str) -> str | None:
    path = _CORPUS_SRC / name
    if not path.exists():
        return f"{path} not present — clone the corpus repos first (see build_index.py docstring)"
    return None


def _parsed(parser, name: str, case: unittest.TestCase) -> list[dict]:
    """Skip when the corpus is absent; FAIL the whole class when it is present but yields nothing.

    The second half is the point. Most checks in this file are loops over `self.chunks`, and a
    loop over an empty list asserts nothing — so when `parse_lolbas` started returning zero
    chunks (LOLBAS.github.io moved its data from `yml/**/*.yml` to Jekyll frontmatter in
    `_lolbas/**/*.md`), three of that class's four tests went on passing. Only
    `test_produces_many_chunks` noticed, and it is the one check not every class has in the same
    form.

    Asserting non-empty here rather than in each test means a parser that silently stops working
    takes its entire class down, which is the honest signal: nothing about those chunks was
    verified. It belongs in setUp specifically because a corpus that is present but unparseable
    is a broken fixture, not a passing test.
    """
    reason = _skip_reason(name)
    if reason:
        case.skipTest(reason)
    chunks = parser(_CORPUS_SRC / name)
    case.assertGreater(
        len(chunks), 0,
        f"{parser.__name__} returned no chunks from {_CORPUS_SRC / name}, which exists. Every "
        "other check in this class loops over those chunks and would pass vacuously. Either the "
        "clone is empty, or the upstream repo changed its layout and the parser needs to learn "
        "the new one.",
    )
    return chunks


class GtfobinsParserTest(unittest.TestCase):
    def setUp(self):
        self.chunks = _parsed(parse_gtfobins, "gtfobins", self)

    def test_produces_many_chunks(self):
        self.assertGreater(len(self.chunks), 500)

    def test_every_chunk_has_required_fields_and_nonempty_text(self):
        for c in self.chunks[:200]:
            for field in ("id", "source", "title", "text", "tags", "url"):
                self.assertIn(field, c)
            self.assertEqual(c["source"], "gtfobins")
            self.assertTrue(c["text"].strip())

    def test_ids_are_unique(self):
        ids = [c["id"] for c in self.chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_known_binary_present(self):
        self.assertTrue(any("find" in c["tags"] for c in self.chunks))


class LolbasParserTest(unittest.TestCase):
    def setUp(self):
        self.chunks = _parsed(parse_lolbas, "lolbas", self)

    def test_produces_many_chunks(self):
        self.assertGreater(len(self.chunks), 100)

    def test_every_chunk_has_required_fields(self):
        for c in self.chunks[:100]:
            for field in ("id", "source", "title", "text", "tags", "url"):
                self.assertIn(field, c)
            self.assertEqual(c["source"], "lolbas")

    def test_ids_are_unique(self):
        ids = [c["id"] for c in self.chunks]
        self.assertEqual(len(ids), len(set(ids)))


class PayloadsParserTest(unittest.TestCase):
    def setUp(self):
        self.chunks = _parsed(parse_payloads_all_the_things, "patt", self)

    def test_produces_many_chunks(self):
        self.assertGreater(len(self.chunks), 300)

    def test_skips_summary_toc_sections(self):
        for c in self.chunks:
            self.assertNotIn(c["title"].split(" — ")[-1].strip().lower(), {"summary", "table of contents"})

    def test_ids_are_unique(self):
        ids = [c["id"] for c in self.chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_chunk_meets_minimum_length(self):
        for c in self.chunks[:200]:
            self.assertGreaterEqual(len(c["text"]), 40)


if __name__ == "__main__":
    unittest.main()
