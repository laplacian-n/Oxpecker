"""Live tests against the real CPU embedding server and the real built index — skip gracefully
if either isn't up, matching this project's established pattern for tests that need a real
external resource (agent/pipeline/test_executor.py's Juice Shop skip is the model)."""
from __future__ import annotations

import unittest

from .embed_client import EmbedClient
from .service import KnowledgeRAGService
from .store import KnowledgeStore


def _embed_server_up() -> bool:
    try:
        return EmbedClient().health()
    except Exception:
        return False


class EmbedClientLiveTest(unittest.TestCase):
    def setUp(self):
        if not _embed_server_up():
            self.skipTest("embedding server not reachable at 127.0.0.1:8091 — see build_index.py")
        self.client = EmbedClient()

    def test_embed_query_returns_768_dim_vector(self):
        vec = self.client.embed_query("privilege escalation via sudo")
        self.assertEqual(len(vec), 768)

    def test_embed_documents_batches_correctly(self):
        vecs = self.client.embed_documents([f"document number {i}" for i in range(5)])
        self.assertEqual(len(vecs), 5)
        self.assertTrue(all(len(v) == 768 for v in vecs))

    def test_similar_texts_are_more_similar_than_unrelated_ones(self):
        import numpy as np

        a = np.array(self.client.embed_query("escalate privileges using sudo misconfiguration"))
        b = np.array(self.client.embed_query("gain root access through a sudo rule"))
        c = np.array(self.client.embed_query("baking a chocolate cake recipe"))
        cos = lambda x, y: float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))
        self.assertGreater(cos(a, b), cos(a, c))


class ServiceLiveTest(unittest.TestCase):
    def setUp(self):
        if not _embed_server_up():
            self.skipTest("embedding server not reachable at 127.0.0.1:8091")
        if not KnowledgeStore.exists():
            self.skipTest("no built index — run `python3 -m agent.knowledge_rag.build_index`")
        self.service = KnowledgeRAGService()

    def test_search_returns_relevant_gtfobins_hit(self):
        result = self.service.search("find command sudo privilege escalation", top_k=5)
        self.assertTrue(result["ok"])
        self.assertGreater(len(result["results"]), 0)
        self.assertTrue(any(r["source"] == "gtfobins" for r in result["results"]))

    def test_source_filter_is_respected(self):
        result = self.service.search("execute code", top_k=5, source="lolbas")
        self.assertTrue(result["ok"])
        self.assertTrue(all(r["source"] == "lolbas" for r in result["results"]))

    def test_count_matches_a_reasonable_corpus_size(self):
        self.assertGreater(self.service.count(), 1000)

    def test_exact_binary_name_query_ranks_that_binary_first(self):
        # regression test for a real bug found live (2026-09-08): pure cosine similarity ranked
        # install/setcap/chattr above `find` itself for a find-specific query, because their
        # shared boilerplate "privilege escalation" description text dominated the one
        # distinguishing word. Fixed by service.py's keyword-bonus re-ranking.
        result = self.service.search("find command exec sh sudo", top_k=3, source="gtfobins")
        self.assertTrue(result["ok"])
        self.assertTrue(any(r["title"].startswith("find") for r in result["results"]))


if __name__ == "__main__":
    unittest.main()
