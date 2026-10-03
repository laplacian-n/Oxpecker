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


class GtfobinsParserTest(unittest.TestCase):
    def setUp(self):
        reason = _skip_reason("gtfobins")
        if reason:
            self.skipTest(reason)
        self.chunks = parse_gtfobins(_CORPUS_SRC / "gtfobins")

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
        reason = _skip_reason("lolbas")
        if reason:
            self.skipTest(reason)
        self.chunks = parse_lolbas(_CORPUS_SRC / "lolbas")

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
        reason = _skip_reason("patt")
        if reason:
            self.skipTest(reason)
        self.chunks = parse_payloads_all_the_things(_CORPUS_SRC / "patt")

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
