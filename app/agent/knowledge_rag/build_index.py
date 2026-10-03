"""Build the knowledge-RAG index: parse the corpus repos, embed every chunk, save the index.

Needs the dedicated embedding server running first:

    /home/nicotine/llama.cpp/build/bin/llama-server \
      -m agent/knowledge_rag/models/nomic-embed-text-v1.5.f16.gguf \
      --embedding --pooling mean -ngl 99 -c 2048 -b 2048 -ub 2048 \
      --host 127.0.0.1 --port 8091

Run: python3 -u -m agent.knowledge_rag.build_index
"""
from __future__ import annotations

import json
import sys
import time

import numpy as np
from pathlib import Path

from .. import config
from .embed_client import EmbedClient, EmbedClientError
from .parsers import parse_all, parse_sft_knowledge, parse_external_datasets
from .store import KnowledgeStore

_PROJECT_ROOT = Path(config.KNOWLEDGE_RAG_DIR).parent.parent
SFT_KNOWLEDGE_FILES = [
    _PROJECT_ROOT / "datasets" / "qa" / "wsA_buckets" / "bucket1_cut.jsonl",
    _PROJECT_ROOT / "datasets" / "qa" / "wsA_buckets" / "bucket2_to_rag.jsonl",
]
EXTERNAL_DATASETS_DIR = _PROJECT_ROOT / "datasets" / "external"

CHECKPOINT_DIR = config.KNOWLEDGE_RAG_INDEX_PATH.parent / "checkpoints"
CHECKPOINT_INTERVAL = 5000  # save every N chunks


def _save_checkpoint(embeddings: list[list[float]], offset: int) -> None:
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    arr = np.array(embeddings, dtype=np.float32)
    np.save(CHECKPOINT_DIR / f"emb_{offset}.npy", arr)
    print(f"  checkpoint saved: {offset} vectors", flush=True)


def _load_checkpoints() -> tuple[np.ndarray | None, int]:
    if not CHECKPOINT_DIR.exists():
        return None, 0
    files = sorted(CHECKPOINT_DIR.glob("emb_*.npy"),
                   key=lambda p: int(p.stem.split("_")[1]))
    if not files:
        return None, 0
    arrays = []
    for f in files:
        arrays.append(np.load(f))
    combined = np.concatenate(arrays, axis=0)
    total = combined.shape[0]
    print(f"  resumed from checkpoint: {total:,} vectors already embedded", flush=True)
    return combined, total


def _clear_checkpoints() -> None:
    if CHECKPOINT_DIR.exists():
        for f in CHECKPOINT_DIR.glob("emb_*.npy"):
            f.unlink()


def main() -> int:
    client = EmbedClient()
    if not client.health():
        print(f"embedding server not reachable at {client.base_url} — start it first",
              file=sys.stderr)
        return 1

    print("parsing corpus...", flush=True)
    chunks = parse_all(config.KNOWLEDGE_RAG_DIR / "corpus_src")
    sft_paths = [p for p in SFT_KNOWLEDGE_FILES if p.exists()]
    if sft_paths:
        chunks += parse_sft_knowledge(*sft_paths)
    if EXTERNAL_DATASETS_DIR.exists():
        print("parsing external datasets (CyberStrike, Fenrir, ExploitDB, NIST)...", flush=True)
        ext_chunks = parse_external_datasets(EXTERNAL_DATASETS_DIR)
        print(f"  {len(ext_chunks):,} external chunks parsed", flush=True)
        chunks += ext_chunks

    by_src = {}
    for c in chunks:
        by_src[c["source"]] = by_src.get(c["source"], 0) + 1
    src_str = ", ".join(f"{v} {k}" for k, v in sorted(by_src.items()))
    print(f"{len(chunks):,} chunks parsed ({src_str})", flush=True)

    # Check for existing checkpoints
    prev_emb, start_offset = _load_checkpoints()

    if start_offset >= len(chunks):
        print(f"all {len(chunks):,} chunks already embedded from checkpoints", flush=True)
        embeddings_arr = prev_emb
    else:
        print(f"embedding {len(chunks) - start_offset:,} remaining chunks "
              f"(of {len(chunks):,} total, batch=16)...", flush=True)
        t0 = time.time()
        texts = [c["text"] for c in chunks[start_offset:]]
        new_embeddings: list[list[float]] = []
        batch_size = 16
        total_remaining = len(texts)

        for i in range(0, total_remaining, batch_size):
            batch = [f"search_document: {t}" for t in texts[i:i + batch_size]]
            try:
                result = client._embed_batch(batch)
                new_embeddings.extend(result)
            except EmbedClientError:
                for single in batch:
                    try:
                        new_embeddings.extend(client._embed_batch([single]))
                    except EmbedClientError:
                        new_embeddings.extend(client._embed_batch([single[:1500]]))

            done = len(new_embeddings)
            if done % (batch_size * 50) == 0 or done >= total_remaining:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total_remaining - done) / rate if rate > 0 else 0
                print(f"  embedded {start_offset + done:,}/{len(chunks):,} "
                      f"({(start_offset + done)/len(chunks)*100:.1f}%) "
                      f"rate={rate:.0f}/s ETA={eta/60:.0f}m", flush=True)

            # Checkpoint
            if done % CHECKPOINT_INTERVAL == 0 and done > 0:
                _save_checkpoint(new_embeddings[-CHECKPOINT_INTERVAL:],
                                 start_offset + done)

        # Save final partial checkpoint
        remainder = len(new_embeddings) % CHECKPOINT_INTERVAL
        if remainder > 0:
            _save_checkpoint(new_embeddings[-remainder:],
                             start_offset + len(new_embeddings))

        elapsed = time.time() - t0
        print(f"embedded {len(new_embeddings):,} new chunks in {elapsed:.1f}s", flush=True)

        new_arr = np.array(new_embeddings, dtype=np.float32)
        if prev_emb is not None:
            embeddings_arr = np.concatenate([prev_emb, new_arr], axis=0)
        else:
            embeddings_arr = new_arr

    # Normalize and save
    norms = np.linalg.norm(embeddings_arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    embeddings_arr = embeddings_arr / norms

    store = KnowledgeStore(embeddings_arr, chunks)
    store.save()
    print(f"saved index: {config.KNOWLEDGE_RAG_INDEX_PATH} ({len(store)} vectors, "
          f"dim={store.vectors.shape[1]})", flush=True)

    _clear_checkpoints()
    print("done — checkpoints cleaned up", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
