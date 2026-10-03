"""The vector index itself: a plain numpy float32 matrix (one row per chunk, L2-normalized so
cosine similarity is a dot product) plus a parallel JSONL metadata file. No database, no
external vector-DB dependency — a few thousand offensive-security reference chunks is small
enough that a flat in-memory matrix and a linear scan is the right amount of engineering, not a
shortcut (see kb_vectors.py's TF-IDF module for the same reasoning applied to the technique KB).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .. import config


class KnowledgeStore:
    def __init__(self, vectors: np.ndarray, meta: list[dict]):
        if len(vectors) != len(meta):
            raise ValueError(f"{len(vectors)} vectors but {len(meta)} metadata rows")
        self.vectors = vectors  # shape (n, dim), rows L2-normalized
        self.meta = meta

    @classmethod
    def build(cls, chunks: list[dict], embeddings: list[list[float]]) -> "KnowledgeStore":
        arr = np.array(embeddings, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        arr = arr / norms
        return cls(arr, chunks)

    def save(self, vectors_path: Path | None = None, meta_path: Path | None = None) -> None:
        vectors_path = vectors_path or config.KNOWLEDGE_RAG_INDEX_PATH
        meta_path = meta_path or config.KNOWLEDGE_RAG_META_PATH
        vectors_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(vectors_path, self.vectors)
        with open(meta_path, "w", encoding="utf-8") as f:
            for row in self.meta:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, vectors_path: Path | None = None, meta_path: Path | None = None) -> "KnowledgeStore":
        vectors_path = vectors_path or config.KNOWLEDGE_RAG_INDEX_PATH
        meta_path = meta_path or config.KNOWLEDGE_RAG_META_PATH
        vectors = np.load(vectors_path)
        with open(meta_path, encoding="utf-8") as f:
            meta = [json.loads(line) for line in f]
        return cls(vectors, meta)

    @staticmethod
    def exists(vectors_path: Path | None = None, meta_path: Path | None = None) -> bool:
        vectors_path = vectors_path or config.KNOWLEDGE_RAG_INDEX_PATH
        meta_path = meta_path or config.KNOWLEDGE_RAG_META_PATH
        return vectors_path.exists() and meta_path.exists()

    def search(self, query_vec: list[float], top_k: int = 5,
               source_filter: str | None = None) -> list[dict]:
        q = np.array(query_vec, dtype=np.float32)
        qn = np.linalg.norm(q)
        if qn > 0:
            q = q / qn
        scores = self.vectors @ q  # cosine similarity, both sides L2-normalized
        order = np.argsort(-scores)
        out = []
        for i in order:
            row = self.meta[i]
            if source_filter and row.get("source") != source_filter:
                continue
            out.append({**row, "score": float(scores[i])})
            if len(out) >= top_k:
                break
        return out

    def __len__(self) -> int:
        return len(self.meta)
