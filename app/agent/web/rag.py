"""The retrieval side of the chat turn: a TF-IDF store and the memory-mapped vector index.

Lifted out of dev_server.py unchanged. Two classes, and nothing else in that file reaches into
them: they take their corpus and index paths as arguments and touch no module-level state, which
is what made them safe to move. See test_module_split_hazard.py for what is not.

dev_server re-exports both names, so `dev_server.VectorRAG` keeps working.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("oxpecker.web.rag")

class TFIDFStore:
    """Simple TF-IDF-based RAG store. Loads text files from a corpus directory,
    builds a TF-IDF matrix in memory, and does cosine-similarity search."""

    def __init__(self):
        self.documents: list[dict] = []
        self.vectorizer = None
        self.tfidf_matrix = None

    def index_directory(self, corpus_dir: Path) -> int:
        if not corpus_dir.exists():
            return 0
        texts, metas = [], []
        for fp in sorted(corpus_dir.rglob("*")):
            if fp.suffix not in (".txt", ".md", ".json", ".jsonl"):
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if fp.suffix == ".jsonl":
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        text = obj.get("text", "") or obj.get("content", "")
                        if not text:
                            continue
                        meta = {
                            "title": obj.get("title", fp.stem),
                            "source": obj.get("source", fp.parent.name),
                            "url": obj.get("url", ""),
                            "tags": obj.get("tags", []),
                            "text": text[:2000],
                        }
                        texts.append(text[:2000])
                        metas.append(meta)
                    except json.JSONDecodeError:
                        continue
            else:
                chunks = self._chunk_text(content, max_chars=1500)
                for i, chunk in enumerate(chunks):
                    meta = {
                        "title": fp.stem + (f" (part {i+1})" if len(chunks) > 1 else ""),
                        "source": fp.parent.name,
                        "url": "",
                        "tags": [],
                        "text": chunk,
                    }
                    texts.append(chunk)
                    metas.append(meta)

        if not texts:
            return 0

        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=50000,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(texts)
        self.documents = metas
        return len(texts)

    def index_from_existing_meta(self, meta_path: Path) -> int:
        """Load from the project's existing meta.jsonl (the same format knowledge_rag uses)."""
        if not meta_path.exists():
            return 0
        texts, metas = [], []
        with open(meta_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    text = obj.get("text", "")
                    if not text:
                        continue
                    texts.append(text[:2000])
                    metas.append(obj)
                except json.JSONDecodeError:
                    continue
        if not texts:
            return 0
        from sklearn.feature_extraction.text import TfidfVectorizer
        self.vectorizer = TfidfVectorizer(
            max_features=50000,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self.tfidf_matrix = self.vectorizer.fit_transform(texts)
        self.documents = metas
        return len(texts)

    def search(self, query: str, top_k: int = 5, source: str | None = None) -> list[dict]:
        if self.vectorizer is None or self.tfidf_matrix is None:
            return []
        q_vec = self.vectorizer.transform([query])
        from sklearn.metrics.pairwise import cosine_similarity
        scores = cosine_similarity(q_vec, self.tfidf_matrix).flatten()
        order = scores.argsort()[::-1]
        results = []
        for i in order:
            if scores[i] < 0.01:
                break
            doc = self.documents[i]
            if source and doc.get("source") != source:
                continue
            results.append({
                "title": doc.get("title", ""),
                "text": doc.get("text", ""),
                "source": doc.get("source", ""),
                "url": doc.get("url", ""),
                "score": round(float(scores[i]), 3),
                "tags": doc.get("tags", []),
            })
            if len(results) >= top_k:
                break
        return results

    def count(self) -> int:
        return len(self.documents)

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 1500) -> list[str]:
        if len(text) <= max_chars:
            return [text] if text.strip() else []
        chunks = []
        lines = text.split("\n")
        current = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return [c for c in chunks if c.strip()]


class VectorRAG:
    """Dense RAG over a large prebuilt index, kept memory-safe.

    A 547k-chunk index is ~1.7GB of vectors + ~1GB of JSONL metadata; loading both into
    RAM (np.load + [json.loads(l) for l in f]) costs ~5GB and OOMs a 24GB box that is
    already full. Instead: vectors are memory-MAPPED (paged by the OS, ~0 resident), and
    metadata is read on demand via a cached byte-offset index — so steady-state RAM is a
    few MB. Query embedding goes to a small nomic-embed llama-server (/embedding).
    """

    def __init__(self, vectors_path: Path, meta_path: Path, embed_url: str,
                 query_prefix: str = "search_query: "):
        import numpy as np
        self._np = np
        self.vectors = np.load(str(vectors_path), mmap_mode="r")  # (n, dim), L2-normalized rows
        self.meta_path = Path(meta_path)
        self.embed_url = embed_url.rstrip("/")
        self.query_prefix = query_prefix
        self.offsets = self._load_or_build_offsets()
        self._n = int(min(len(self.offsets), self.vectors.shape[0]))
        self._dim = int(self.vectors.shape[1])

    def _load_or_build_offsets(self):
        np = self._np
        off_path = self.meta_path.with_suffix(".offsets.npy")
        try:
            if off_path.exists() and off_path.stat().st_mtime >= self.meta_path.stat().st_mtime:
                return np.load(str(off_path))
        except OSError:
            pass
        offs = []
        with open(self.meta_path, "rb") as f:
            pos = f.tell()
            line = f.readline()
            while line:
                if line.strip():
                    offs.append(pos)
                pos = f.tell()
                line = f.readline()
        arr = np.array(offs, dtype=np.int64)
        try:
            np.save(str(off_path), arr)
        except OSError:
            pass
        return arr

    def embed_query(self, text: str):
        payload = json.dumps({"content": [self.query_prefix + text]}).encode()
        req = urllib.request.Request(
            f"{self.embed_url}/embedding", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        # llama.cpp shapes vary: [{"embedding":[[...]]}] or [{"embedding":[...]}] or {"embedding":[...]}
        row = data[0] if isinstance(data, list) else data
        emb = row.get("embedding", row) if isinstance(row, dict) else row
        if isinstance(emb, list) and emb and isinstance(emb[0], list):
            emb = emb[0]
        return self._np.asarray(emb, dtype=self._np.float32)

    def search(self, query: str, top_k: int = 5, source: str | None = None) -> list[dict]:
        query = (query or "").strip()
        if not query or self._n == 0:
            return []
        np = self._np
        try:
            q = self.embed_query(query)
        except Exception as e:  # degrade silently — RAG is reference, never load-bearing
            log.warning("embed query failed: %s", e)
            return []
        if q.shape[0] != self._dim:
            return []
        qn = np.linalg.norm(q)
        if qn > 0:
            q = q / qn
        scores = np.asarray(self.vectors[:self._n] @ q)  # memmap matmul, streams from page cache
        pool = int(min(max(top_k * 8, 40), self._n))
        cand = np.argpartition(-scores, pool - 1)[:pool]
        cand = cand[np.argsort(-scores[cand])]
        qtok = set(re.findall(r"[a-z0-9]{2,}", query.lower()))
        results, seen = [], set()
        with open(self.meta_path, "rb") as f:
            for i in cand:
                f.seek(int(self.offsets[i]))
                try:
                    m = json.loads(f.readline())
                except json.JSONDecodeError:
                    continue
                if source and m.get("source") != source:
                    continue
                key = (m.get("source", ""), (m.get("title", "") or "")[:40])
                if key in seen:
                    continue
                seen.add(key)
                tags = {str(t).lower() for t in (m.get("tags") or [])}
                ttok = set(re.findall(r"[a-z0-9]{2,}", (m.get("title", "") or "").lower()))
                bonus = 0.08 * len(qtok & tags) + 0.05 * len(qtok & ttok)
                results.append({
                    "title": m.get("title", ""), "text": (m.get("text", "") or "")[:2000],
                    "source": m.get("source", ""), "url": m.get("url", ""),
                    "score": round(float(scores[i]) + bonus, 3), "tags": list(tags),
                })
        results.sort(key=lambda r: -r["score"])
        return results[:top_k]

    def count(self) -> int:
        return self._n

