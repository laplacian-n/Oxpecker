# Knowledge RAG

Deep, on-demand offensive-reference retrieval — the `knowledge_search` / `knowledge_fetch` tools.
~0.5M chunks of GTFOBins, LOLBAS, LOLDrivers, HackTricks, atomic-red-team, exploit code, OWASP
cheat sheets and more, embedded with nomic-embed-text-v1.5 and searched by cosine similarity + a
BM25/keyword bonus (`service.py`).

This is the **deep** knowledge layer. The **lean, always-on** layer is the technique KB
(`../notebook/technique_kb.py`), which surfaces a short card every turn with no server; the
claude-red catalogue is loaded into it (`../knowledge/load_claude_red.py`). The two are
complementary: the technique KB nudges, this answers a deliberate query in depth.

## Where the data lives

All runtime data is in this directory and **git-ignored** (`app/.gitignore`):

- `index/vectors.npy` + `index/meta.jsonl` (~2.6 GB) — the built index. Loaded lazily by
  `store.py` on the first `knowledge_search`.
- `models/nomic-embed-text-v1.5.f16.gguf` — the embedding model (query + build).
- `corpus_src/` — the source corpus, kept so the index can be rebuilt (`build_index.py`).

> Historical note: the index/corpus/model were built in the old pre-rename `pentestAI/localAI`
> checkout and lived there, away from this repo. They were moved here on 2026-10-23 so the RAG is
> part of Oxpecker and not stranded in a stale checkout.

## Running it

Querying needs the embedding server up, so the query is embedded with the same model the index was
built with. It is **CPU-only on purpose** (`CUDA_VISIBLE_DEVICES=""`): nomic touches the GPU even
at `-ngl 0` and would OOM against the decision model, so it never competes for VRAM.

```bash
# from app/ — start the embedding server on :8091 (config.KNOWLEDGE_RAG_EMBED_SERVER_URL)
agent/knowledge_rag/run_embed_server.sh &
```

If the server is down, `knowledge_search` degrades gracefully (`{"ok": false, "error":
"embedding server unavailable"}`) — reference knowledge is never a hard dependency of a run.

## Rebuilding the index

See `build_index.py`'s docstring. Needs the embedding server running first, then it embeds
`corpus_src/` into `index/` with checkpointing.
