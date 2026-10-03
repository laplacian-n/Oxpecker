#!/usr/bin/env python3
"""Build the knowledge-RAG index using GPU-accelerated sentence-transformers.

Much faster than the CPU llama-server approach for large corpus (500K+ chunks).
Uses nomic-embed-text-v1.5 via HuggingFace, GPU-accelerated.

Usage: python3 -m agent.knowledge_rag.build_index_gpu
"""
from __future__ import annotations

import sys
import time

from .. import config
from pathlib import Path

from .parsers import parse_all, parse_sft_knowledge, parse_external_datasets
from .store import KnowledgeStore

_PROJECT_ROOT = Path(config.KNOWLEDGE_RAG_DIR).parent.parent
SFT_KNOWLEDGE_FILES = [
    _PROJECT_ROOT / "datasets" / "qa" / "wsA_buckets" / "bucket1_cut.jsonl",
    _PROJECT_ROOT / "datasets" / "qa" / "wsA_buckets" / "bucket2_to_rag.jsonl",
]
EXTERNAL_DATASETS_DIR = _PROJECT_ROOT / "datasets" / "external"


def main() -> int:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("sentence-transformers not installed. Run: pip install sentence-transformers", file=sys.stderr)
        return 1

    print("loading nomic-embed-text-v1.5 on GPU...")
    t0 = time.time()
    model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True)
    model = model.to("cuda")
    print(f"model loaded in {time.time() - t0:.1f}s")

    # Parse all sources
    print("parsing corpus_src...")
    chunks = parse_all(config.KNOWLEDGE_RAG_DIR / "corpus_src")

    sft_paths = [p for p in SFT_KNOWLEDGE_FILES if p.exists()]
    if sft_paths:
        print("parsing SFT knowledge files...")
        chunks += parse_sft_knowledge(*sft_paths)

    if EXTERNAL_DATASETS_DIR.exists():
        print("parsing external datasets (CyberStrike, Fenrir, ExploitDB, NIST)...")
        ext_chunks = parse_external_datasets(EXTERNAL_DATASETS_DIR)
        print(f"  {len(ext_chunks):,} external chunks")
        chunks += ext_chunks

    by_src = {}
    for c in chunks:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
    for src, count in sorted(by_src.items(), key=lambda x: -x[1]):
        print(f"  {src}: {count:,}")
    print(f"total: {len(chunks):,} chunks")

    # Embed with GPU
    print(f"\nembedding {len(chunks):,} chunks on GPU (batch_size=256)...")
    t0 = time.time()
    texts = [c["text"] for c in chunks]

    # nomic-embed-text requires "search_document: " prefix for documents
    prefixed = [f"search_document: {t}" for t in texts]

    embeddings = model.encode(
        prefixed,
        batch_size=256,
        show_progress_bar=True,
        normalize_embeddings=True,
    )
    elapsed = time.time() - t0
    print(f"embedded {len(embeddings):,} chunks in {elapsed:.1f}s ({len(embeddings)/elapsed:.0f} chunks/s)")

    # Build store (skip re-normalization since sentence-transformers already normalized)
    import numpy as np
    arr = np.array(embeddings, dtype=np.float32)
    store = KnowledgeStore(arr, chunks)
    store.save()
    print(f"\nsaved index: {config.KNOWLEDGE_RAG_INDEX_PATH}")
    print(f"  {len(store)} vectors, dim={store.vectors.shape[1]}")
    size_mb = config.KNOWLEDGE_RAG_INDEX_PATH.stat().st_size / 1024 / 1024
    meta_mb = config.KNOWLEDGE_RAG_META_PATH.stat().st_size / 1024 / 1024
    print(f"  vectors: {size_mb:.1f} MB, meta: {meta_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
