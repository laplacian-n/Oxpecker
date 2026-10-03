"""KnowledgeStore — save/load/search semantics. No live model or embedding server needed;
uses small synthetic vectors."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from .store import KnowledgeStore


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="knowledge-rag-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vectors_path = self.tmp / "vectors.npy"
        self.meta_path = self.tmp / "meta.jsonl"

    def _chunk(self, i, title):
        return {"id": f"c{i}", "source": "test", "title": title, "text": title, "url": ""}

    def test_build_normalizes_and_search_ranks_by_cosine(self):
        chunks = [self._chunk(0, "a"), self._chunk(1, "b"), self._chunk(2, "c")]
        embeddings = [[1.0, 0.0], [0.0, 1.0], [0.7071, 0.7071]]
        store = KnowledgeStore.build(chunks, embeddings)
        hits = store.search([1.0, 0.0], top_k=2)
        self.assertEqual(hits[0]["title"], "a")
        self.assertAlmostEqual(hits[0]["score"], 1.0, places=3)
        self.assertEqual(hits[1]["title"], "c")

    def test_save_and_load_round_trip(self):
        chunks = [self._chunk(0, "a"), self._chunk(1, "b")]
        store = KnowledgeStore.build(chunks, [[1.0, 0.0], [0.0, 1.0]])
        store.save(self.vectors_path, self.meta_path)
        self.assertTrue(KnowledgeStore.exists(self.vectors_path, self.meta_path))
        loaded = KnowledgeStore.load(self.vectors_path, self.meta_path)
        self.assertEqual(len(loaded), 2)
        hits = loaded.search([0.0, 1.0], top_k=1)
        self.assertEqual(hits[0]["title"], "b")

    def test_exists_false_when_files_missing(self):
        self.assertFalse(KnowledgeStore.exists(self.vectors_path, self.meta_path))

    def test_source_filter_excludes_other_sources(self):
        chunks = [
            {"id": "c0", "source": "gtfobins", "title": "a", "text": "a", "url": ""},
            {"id": "c1", "source": "lolbas", "title": "b", "text": "b", "url": ""},
        ]
        store = KnowledgeStore.build(chunks, [[1.0, 0.0], [0.9, 0.1]])
        hits = store.search([1.0, 0.0], top_k=5, source_filter="lolbas")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["source"], "lolbas")

    def test_build_rejects_mismatched_lengths(self):
        with self.assertRaises(ValueError):
            KnowledgeStore.build([self._chunk(0, "a")], [[1.0, 0.0], [0.0, 1.0]])

    def test_zero_vector_does_not_crash_normalization(self):
        chunks = [self._chunk(0, "a")]
        store = KnowledgeStore.build(chunks, [[0.0, 0.0]])
        hits = store.search([1.0, 0.0], top_k=1)
        self.assertEqual(len(hits), 1)  # no NaN-induced crash


if __name__ == "__main__":
    unittest.main()
