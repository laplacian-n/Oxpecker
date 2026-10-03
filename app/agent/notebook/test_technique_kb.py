"""TechniqueKB.semantic_recall — the hybrid TF-IDF + keyword recall path that closes the
"auto-relevance" gap docs/working-notebook-spec.md §7 used to list as deferred. No live model
needed."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from .technique_kb import TechniqueKB


class SemanticRecallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="technique-kb-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.kb = TechniqueKB(self.tmp / "kb.db")

    def test_finds_a_differently_worded_query_via_cosine_similarity(self):
        self.kb.save(
            title="GraphQL introspection leak",
            body="Sending an introspection query against /graphql revealed a hidden mutation "
                 "deleteAllUsers not exposed in the documented schema.",
            tags=["graphql"], surfaces=["/graphql"], source_engagement="eng-a",
        )
        # different words, same topic — plain substring/keyword recall() would miss this
        hits = self.kb.semantic_recall("hidden mutation on the graphql schema")
        self.assertEqual(len(hits), 1)
        self.assertIn("deleteAllUsers", hits[0]["body"])

    def test_unrelated_query_returns_nothing(self):
        self.kb.save(title="GraphQL introspection leak", body="graphql schema mutation leak",
                     tags=["graphql"], source_engagement="eng-a")
        self.assertEqual(self.kb.semantic_recall("SSRF via image proxy url parameter"), [])

    def test_exclude_engagement_drops_its_own_techniques(self):
        self.kb.save(title="X", body="jwt alg none bypass on the auth endpoint",
                     tags=["jwt"], source_engagement="eng-a")
        same = self.kb.semantic_recall("jwt alg none", exclude_engagement="eng-a")
        other = self.kb.semantic_recall("jwt alg none", exclude_engagement="eng-b")
        self.assertEqual(same, [])
        self.assertEqual(len(other), 1)

    def test_no_query_or_surface_returns_nothing(self):
        self.kb.save(title="X", body="y", source_engagement="eng-a")
        self.assertEqual(self.kb.semantic_recall(), [])

    def test_empty_kb_returns_nothing(self):
        self.assertEqual(self.kb.semantic_recall("anything"), [])

    def test_bump_increments_use_count_like_recall_does(self):
        self.kb.save(title="X", body="jwt alg none bypass", tags=["jwt"], source_engagement="eng-a")
        self.kb.semantic_recall("jwt alg none")
        self.assertEqual(self.kb.all()[0]["use_count"], 1)
        self.kb.semantic_recall("jwt alg none", bump=False)
        self.assertEqual(self.kb.all()[0]["use_count"], 1)

    def test_exact_surface_match_ranks_above_a_looser_topical_one(self):
        self.kb.save(title="A", body="rate limiting missing on password reset flow",
                     surfaces=["/reset-password"], source_engagement="eng-a")
        self.kb.save(title="B", body="rate limiting missing on the login endpoint",
                     surfaces=["/login"], source_engagement="eng-a")
        hits = self.kb.semantic_recall("rate limiting", surface="/login", min_score=0.0)
        self.assertEqual(hits[0]["surfaces"], ["/login"])


if __name__ == "__main__":
    unittest.main()
