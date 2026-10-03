"""Lightweight local vector retrieval for the cross-engagement technique KB — TF-IDF term
vectors + cosine similarity, pure stdlib, no model call.

This plays the role xOffense's "Knowledge Repository" plays (a vector-based store that RAGs
offensive technique/tool knowledge into the agent's context) scaled to what this box can afford:
the GPU is already the bottleneck (~3.1-4.0 tok/s, single decode slot, --parallel 1 — see
doc/handoff.md's load-bearing facts), so a second neural embedding model competing for it is a
real ongoing cost, not a free upgrade. Sparse TF-IDF vectors need no model call at all, so
technique_kb.py's `semantic_recall` can run on every notebook digest without touching the
decode slot the live agent needs.

A technique KB is dozens-to-hundreds of entries, not millions — rebuilding the index from
scratch per call is cheap and sidesteps an entire bug class this codebase has hit repeatedly
(a cached index that silently goes stale because nothing invalidated it — see the "default
argument bound at import time" lesson in doc/handoff.md). There is no cache here to go stale.
"""
from __future__ import annotations

import math
import re
from collections import Counter

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "it", "this", "that",
    "with", "for", "was", "were", "be", "been", "are", "as", "at", "by", "from", "not",
    "no", "any", "you", "your", "its",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 2 and t not in _STOPWORDS]


class TfidfIndex:
    """Build fresh over a corpus of {doc_id: text}; `vectors[doc_id]` and `query_vector()` are
    both sparse {term: weight} dicts comparable with `cosine()`."""

    def __init__(self, docs: dict[str, str]):
        tokenized = {doc_id: tokenize(text) for doc_id, text in docs.items()}
        n = len(tokenized) or 1
        df: Counter[str] = Counter()
        for toks in tokenized.values():
            df.update(set(toks))
        # Smoothed idf (sklearn-style: log((1+n)/(1+df)) + 1) — never zero, even when a term
        # appears in every document (the degenerate single-document case a technique KB starts
        # in), unlike the textbook log(n/df) which collapses every vector to all-zero there.
        self._n = n
        self._idf = {term: math.log((1 + n) / (1 + d)) + 1.0 for term, d in df.items()}
        self._oov_idf = math.log(1 + n) + 1.0  # a query term never seen in the corpus
        self.vectors = {doc_id: self._vectorize(toks) for doc_id, toks in tokenized.items()}

    def _vectorize(self, tokens: list[str]) -> dict[str, float]:
        if not tokens:
            return {}
        tf = Counter(tokens)
        length = len(tokens)
        return {term: (count / length) * self._idf.get(term, self._oov_idf) for term, count in tf.items()}

    def query_vector(self, text: str) -> dict[str, float]:
        return self._vectorize(tokenize(text))

    @staticmethod
    def cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
        if not v1 or not v2:
            return 0.0
        common = v1.keys() & v2.keys()
        if not common:
            return 0.0
        dot = sum(v1[t] * v2[t] for t in common)
        n1 = math.sqrt(sum(w * w for w in v1.values()))
        n2 = math.sqrt(sum(w * w for w in v2.values()))
        if n1 == 0.0 or n2 == 0.0:
            return 0.0
        return dot / (n1 * n2)
