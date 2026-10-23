#!/usr/bin/env bash
# The dedicated embedding server for the knowledge RAG (nomic-embed-text-v1.5, CPU-only).
#
# The RAG index (index/vectors.npy + meta.jsonl, ~2.6 GB, ~0.5M chunks of GTFOBins/LOLBAS/
# HackTricks/atomic-red-team/exploit code/cheatsheets) lives in this directory. Querying it needs
# the query embedded with the SAME model the index was built with, served here on :8091, which is
# what agent/config.py's KNOWLEDGE_RAG_EMBED_SERVER_URL points at and embed_client.py talks to.
#
# CPU-ONLY ON PURPOSE: CUDA_VISIBLE_DEVICES="" forces llama.cpp to ignore the GPU entirely. nomic
# touches the CUDA device even with -ngl 0 and OOMs on a GPU that already holds the decision model,
# so hiding the device is load-bearing, not caution. This server never competes for VRAM.
#
#   agent/knowledge_rag/run_embed_server.sh            # foreground
#   agent/knowledge_rag/run_embed_server.sh &          # background
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LLAMA_SERVER="${LLAMA_SERVER:-/home/nicotine/llama.cpp/build/bin/llama-server}"
MODEL="${NOMIC_MODEL:-$HERE/models/nomic-embed-text-v1.5.f16.gguf}"
PORT="${KNOWLEDGE_RAG_EMBED_PORT:-8091}"

[ -x "$LLAMA_SERVER" ] || { echo "llama-server not found at $LLAMA_SERVER" >&2; exit 1; }
[ -f "$MODEL" ]        || { echo "nomic model not found at $MODEL" >&2; exit 1; }

exec env CUDA_VISIBLE_DEVICES="" "$LLAMA_SERVER" \
  -m "$MODEL" \
  --embedding --pooling mean -ngl 0 -c 2048 -b 2048 -ub 2048 \
  --host 127.0.0.1 --port "$PORT"
