# General-knowledge RAG (`agent/knowledge_rag/`)

Built 2026-09-08, at the owner's request after reviewing xOffense (arXiv:2509.13021) — its
"Knowledge Repository" is a vector-DB RAG over general offensive-security reference material,
which this project didn't have (confirmed absent by direct repo search — see
`datasets/qa/STEP0_VERIFICATION_2026-09-07.md` §2, written for an unrelated SFT-dataset review
that happened to need the same answer). This closes that gap.

## What it is, and what it deliberately isn't

- **General, pre-existing reference knowledge** — GTFOBins, LOLBAS, PayloadsAllTheThings —
  embedded offline, semantically searchable. Not engagement-specific.
- **Not** `agent/notebook/technique_kb.py` (the cross-engagement technique KB) — that stores what
  *this agent itself discovered* on past engagements; this stores what security researchers
  published, independent of any engagement ever run here.
- **Not** `knowledge_search`/`knowledge_fetch` (`agent/internet/`) — those hit the live web fresh
  every call, no persistent index. This is a pre-built, offline corpus — deterministic and
  repeatable, no internet needed at query time.

## Architecture

```
corpus_src/ (gitignored, git-cloned)          models/ (gitignored, downloaded)
  gtfobins/  — GitHub: GTFOBins/GTFOBins.github.io    nomic-embed-text-v1.5.f16.gguf (262 MB)
  lolbas/    — GitHub: LOLBAS-Project/LOLBAS
  patt/      — GitHub: swisskyrepo/PayloadsAllTheThings
        |
        v  parsers.py (structured YAML for gtfobins/lolbas; header-boundary markdown for patt)
   3,678 chunks: {id, source, title, text, tags, url}
        |
        v  embed_client.py -> a DEDICATED CPU-only llama-server (port 8091)
   768-dim vector per chunk (nomic's "search_document: " prefix)
        |
        v  store.py: KnowledgeStore.build() -> L2-normalize -> save
   agent/knowledge_rag/index/{vectors.npy, meta.jsonl}  (gitignored, rebuild with build_index.py)
        |
        v  service.py: KnowledgeRAGService.search(query) -- "search_query: " prefix, cosine top-k
        |
        v  tools.py: security_reference_search — model-callable, in-process (no broker: a
           reference lookup touches no target)
```

## Why a second, CPU-only embedding server

The main llama-server holds Qwen3-32B fully loaded on this box's one GPU — 16 GB total, ~58 MB
free at build time. There is no room to also load an embedding model on that GPU, and even
`-ngl 0` isn't enough on its own: llama.cpp's CUDA backend still probes/allocates a CUDA stream
during context setup and crashes with `CUDA error: out of memory` unless the device is hidden
entirely via `CUDA_VISIBLE_DEVICES=""` (found live while bringing this up — see
`deploy/localai-knowledge-rag.service`'s comment). A dedicated process on a different port,
CPU-only, has zero VRAM footprint and can't contend with the live agent's decode slot no matter
how busy either one gets.

nomic-embed-text-v1.5 (137M params, f16 GGUF) was chosen over a TF-IDF approach (which
`agent/notebook/kb_vectors.py` uses for the technique KB) because the owner explicitly asked for
retrieval quality over resource-minimalism for this corpus — dense embeddings capture semantic
similarity TF-IDF's bag-of-words model can't (e.g. a query using none of a document's exact words
still ranks it correctly — verified live, see `test_live.py`). f16 (not a lower quant) for the
same reason: the model is tiny either way, so there's no real cost to keeping full precision.

## Corpus (as of the first build)

| source | chunks | what a chunk is |
|---|---|---|
| gtfobins | 2,125 | one (binary, function, entry, context) — e.g. `find`, `shell`, `suid` |
| lolbas | 486 | one `Commands[]` entry per Windows binary |
| payloads_all_the_things | 1,067 | one `##`/`###`-bounded markdown section, header-breadcrumbed |
| **total** | **3,678** | |

Rebuild: `python3 -m agent.knowledge_rag.build_index` (needs the embedding server up — see
`deploy/README.md`). Re-cloning the corpus repos first picks up upstream updates; the build is
idempotent (overwrites the index, no incremental state to corrupt).

## Tool surface

`security_reference_search(query, source?)` — in-process, gated by `use_security_tools` (not
`use_hypothesis_graph`; a reference lookup isn't part of the structured working-memory surface,
it's available whenever the security tool surface is). Degrades to a clean `{"ok": false, ...}`
result if the index hasn't been built yet or the embedding server is down — never crashes the
loop, matching the Working Notebook's technique-KB error-handling convention (a knowledge lookup
is a nice-to-have, not load-bearing for the turn).

## What's NOT done (explicit deferrals, not oversights)

- **No auto-surfacing into the digest** — unlike the technique KB's auto-relevance step, this is
  explicit-call-only for now. General reference knowledge is far broader than one engagement's
  notebook; auto-injecting it every turn risks the same "size cap" problem doc/handoff.md's
  load-bearing facts warn about, with much less certainty it's relevant to right now. Revisit if
  live use shows the model isn't reaching for the tool when it should.
- **No content-freshness/update mechanism** — the corpus is whatever `git clone` pulled at build
  time; there's no scheduled re-sync. Re-run `build_index.py` by hand when you want newer content.
- **No re-ranking / hybrid keyword+vector scoring** — pure cosine similarity over dense
  embeddings, unlike the technique KB's hybrid TF-IDF+keyword approach. Worth revisiting if a
  query with an exact tool/CVE name in it doesn't rank that exact match first.
- **CVE/NVD data not included** — the owner's original list also named the NVD/CVE database as a
  source; not pulled in this pass (GTFOBins/LOLBAS/PayloadsAllTheThings were judged the highest
  first-value set — structured, small, directly technique/syntax-shaped). Adding a CVE corpus is
  a natural next increment: same pipeline, a new `parsers.py` function, no architecture change.
