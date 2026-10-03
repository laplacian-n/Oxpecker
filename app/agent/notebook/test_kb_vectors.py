"""TF-IDF + cosine similarity — the pure-stdlib "vector" half of the technique KB's local
knowledge-RAG. No live model needed."""
from __future__ import annotations

import unittest

from .kb_vectors import TfidfIndex, tokenize


class TokenizeTest(unittest.TestCase):
    def test_lowercases_splits_and_drops_short_and_stopwords(self):
        self.assertEqual(tokenize("The GraphQL API is at /graphql!"), ["graphql", "api", "graphql"])

    def test_empty_input(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize(None), [])


class TfidfIndexTest(unittest.TestCase):
    def test_single_document_corpus_is_not_degenerate(self):
        # log(n/df) would be log(1/1)=0 here, zeroing every vector; smoothed idf must not.
        index = TfidfIndex({"a": "prototype pollution gadget bypassed the sanitizer"})
        qvec = index.query_vector("prototype pollution")
        self.assertGreater(TfidfIndex.cosine(qvec, index.vectors["a"]), 0.0)

    def test_matching_document_ranks_above_unrelated_one(self):
        index = TfidfIndex({
            "graphql": "GraphQL introspection query leaked the schema and a hidden mutation",
            "unrelated": "checkout page missing CSRF token on the payment form",
        })
        qvec = index.query_vector("graphql introspection schema")
        sim_graphql = TfidfIndex.cosine(qvec, index.vectors["graphql"])
        sim_unrelated = TfidfIndex.cosine(qvec, index.vectors["unrelated"])
        self.assertGreater(sim_graphql, sim_unrelated)
        self.assertEqual(sim_unrelated, 0.0)  # zero real term overlap

    def test_query_with_no_vocabulary_overlap_is_zero(self):
        index = TfidfIndex({"a": "prototype pollution gadget"})
        qvec = index.query_vector("completely different topic")
        self.assertEqual(TfidfIndex.cosine(qvec, index.vectors["a"]), 0.0)

    def test_empty_query_or_doc_is_zero_not_an_error(self):
        index = TfidfIndex({"a": "prototype pollution gadget"})
        self.assertEqual(TfidfIndex.cosine({}, index.vectors["a"]), 0.0)
        self.assertEqual(TfidfIndex.cosine(index.query_vector(""), index.vectors["a"]), 0.0)

    def test_identical_text_is_maximally_similar(self):
        index = TfidfIndex({"a": "jwt alg none bypass", "b": "unrelated csrf issue"})
        qvec = index.query_vector("jwt alg none bypass")
        self.assertAlmostEqual(TfidfIndex.cosine(qvec, index.vectors["a"]), 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
