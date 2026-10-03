"""Client for the dedicated embedding llama-server (nomic-embed-text-v1.5, CPU-only).

Why a second server instead of reusing the main one: the main llama-server holds the decision
model (Qwen3-32B) fully loaded on the one GPU this box has (16 GB, ~58 MB free at time of
writing — see doc/handoff.md's load-bearing hardware facts). Embeddings run on a *second*
llama-server process, `-ngl 0` with `CUDA_VISIBLE_DEVICES=""` (nomic.py's build_index CLI docs
the exact launch command) so it never touches the GPU at all and can't contend with the live
agent's decode slot.

nomic-embed-text-v1.5 is instruction-prefixed: documents get "search_document: ", queries get
"search_query: " — mixing these up measurably hurts retrieval quality (it's how the model was
trained), so this module is the one place that prefix is applied; callers never do it themselves.
"""
from __future__ import annotations

import requests

from .. import config

_BATCH_SIZE = 16
_MAX_RETRIES = 3
_TIMEOUT = (10, 30)  # (connect, read) seconds


class EmbedClientError(RuntimeError):
    pass


class EmbedClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or config.KNOWLEDGE_RAG_EMBED_SERVER_URL).rstrip("/")
        self._session = requests.Session()

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import time
        for attempt in range(_MAX_RETRIES):
            try:
                resp = self._session.post(
                    f"{self.base_url}/embedding", json={"content": texts}, timeout=_TIMEOUT,
                )
            except requests.RequestException as e:
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    self._session = requests.Session()
                    continue
                raise EmbedClientError(f"embedding request failed after {_MAX_RETRIES} retries: {e}") from e
            if resp.status_code != 200:
                raise EmbedClientError(f"embedding server -> HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            return [row["embedding"][0] for row in data]
        raise EmbedClientError("unreachable")

    def embed_documents(self, texts: list[str], show_progress: bool = False) -> list[list[float]]:
        """Batches internally — pass as many texts as you have, in document order."""
        import time
        out: list[list[float]] = []
        total = len(texts)
        t0 = time.time()
        for i in range(0, total, _BATCH_SIZE):
            batch = [f"search_document: {t}" for t in texts[i:i + _BATCH_SIZE]]
            try:
                out.extend(self._embed_batch(batch))
            except EmbedClientError:
                for single in batch:
                    try:
                        out.extend(self._embed_batch([single]))
                    except EmbedClientError:
                        out.extend(self._embed_batch([single[:1500]]))
            if show_progress and (len(out) % (_BATCH_SIZE * 50) == 0 or len(out) >= total):
                elapsed = time.time() - t0
                rate = len(out) / elapsed if elapsed > 0 else 0
                eta = (total - len(out)) / rate if rate > 0 else 0
                print(f"  embedded {len(out):,}/{total:,} ({len(out)/total*100:.1f}%) "
                      f"rate={rate:.0f}/s ETA={eta/60:.0f}m", flush=True)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed_batch([f"search_query: {text}"])[0]

    def health(self) -> bool:
        try:
            r = self._session.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except requests.RequestException:
            return False
