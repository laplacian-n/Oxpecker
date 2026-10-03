"""High-level entry point the agent (and CLI) use — lazy-loads the index once, degrades cleanly
if the index or the embedding server isn't available (this is reference knowledge, never
load-bearing for the loop itself: a lookup failure returns an empty/error result, it never
raises into the caller's turn).
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .. import config
from .embed_client import EmbedClient, EmbedClientError
from .store import KnowledgeStore

_CANDIDATE_POOL = 60
_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")

# Binary names / tools that should strongly match GTFOBins entries
_KNOWN_BINARIES = {
    "find", "vim", "nano", "python", "python3", "perl", "ruby", "php", "node",
    "nmap", "tar", "zip", "gcc", "make", "git", "docker", "curl", "wget",
    "awk", "sed", "env", "bash", "sh", "zsh", "dash", "less", "more",
    "man", "ftp", "scp", "ssh", "socat", "nc", "ncat", "strace", "ltrace",
    "gdb", "tcpdump", "openssl", "pkexec", "doas", "su", "sudo", "mount",
    "chown", "chmod", "cp", "mv", "dd", "tee", "watch", "xargs", "rlwrap",
    "screen", "tmux", "expect", "script", "crontab", "at", "systemctl",
    "journalctl", "service", "dpkg", "apt", "yum", "rpm", "pip", "gem",
    "install", "chattr", "setcap", "msfconsole", "hydra", "john", "hashcat",
}

# Binaries that are also language names — ambiguous when combined with technique keywords
_LANGUAGE_BINARIES = {"python", "python3", "php", "perl", "ruby", "node", "gcc", "make"}

# Technique keywords that signal HackTricks preference
_TECHNIQUE_KEYWORDS = {
    "injection", "deserialization", "ssrf", "ssti", "xxe", "xss", "csrf",
    "kerberos", "kerberoast", "asreproast", "pass the hash", "golden ticket",
    "silver ticket", "bloodhound", "active directory", "ldap", "smb",
    "privilege escalation", "lateral movement", "persistence", "exfiltration",
    "web shell", "reverse proxy", "tunneling", "pivoting", "port forwarding",
}


def _bm25_score(query_tokens: set[str], doc_text: str, avg_dl: float = 300.0,
                k1: float = 1.2, b: float = 0.75) -> float:
    """Lightweight BM25 score for a single document against query tokens."""
    doc_lower = doc_text.lower()
    doc_tokens = _TOKEN_RE.findall(doc_lower)
    if not doc_tokens or not query_tokens:
        return 0.0
    dl = len(doc_tokens)
    tf_map = Counter(doc_tokens)
    score = 0.0
    for qt in query_tokens:
        tf = tf_map.get(qt, 0)
        if tf == 0:
            continue
        # simplified IDF (assume N=30000, df approximated as 1 for rare terms)
        idf = math.log(1 + (30000 - 1 + 0.5) / (1 + 0.5))
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avg_dl))
        score += idf * tf_norm
    return score


def _entry_binary_name(hit: dict) -> str | None:
    """Extract the binary name from a GTFOBins-style title like 'find — Shell (sudo)'."""
    title = hit.get("title", "")
    if "—" in title:
        return title.split("—")[0].strip().lower()
    return None


def _keyword_bonus(query: str, hit: dict, query_tokens: set[str]) -> float:
    """Hybrid bonus: exact binary-name match + tag overlap + source-aware boost."""
    if not query_tokens:
        return 0.0
    tags = {str(t).lower() for t in (hit.get("tags") or [])}
    title_tokens = set(_TOKEN_RE.findall(hit.get("title", "").lower()))
    source = hit.get("source", "").lower()
    bonus = 0.0

    # Exact binary name match: query mentions a known binary that IS this entry's binary
    entry_bin = _entry_binary_name(hit)
    query_has_technique = any(kw in query.lower() for kw in _TECHNIQUE_KEYWORDS)
    if entry_bin and entry_bin in query_tokens and entry_bin in _KNOWN_BINARIES:
        # Language-name binaries (php, python, etc.) get suppressed bonus when the
        # query also has technique keywords — "PHP deserialization" wants HackTricks,
        # not GTFOBins php reverse shell
        if query_has_technique and entry_bin in _LANGUAGE_BINARIES:
            bonus += 0.05
        else:
            bonus += 0.30
    elif query_tokens & tags & _KNOWN_BINARIES:
        bonus += 0.10
    elif query_tokens & tags:
        bonus += 0.05

    # Title word match
    title_overlap = query_tokens & title_tokens
    if title_overlap:
        bonus += 0.05 + 0.03 * min(len(title_overlap), 3)

    # Source-aware boost: prefer GTFOBins for binary-name queries
    query_has_binary = bool(query_tokens & _KNOWN_BINARIES)
    query_has_technique = any(kw in query.lower() for kw in _TECHNIQUE_KEYWORDS)

    if query_has_binary and not query_has_technique:
        if source == "gtfobins":
            bonus += 0.08
    elif query_has_technique and not query_has_binary:
        if source == "hacktricks":
            bonus += 0.05

    return bonus


class KnowledgeRAGService:
    def __init__(self):
        self._store: KnowledgeStore | None = None
        self._client: EmbedClient | None = None
        self._load_error: str | None = None

    def _ensure_loaded(self) -> bool:
        if self._store is not None:
            return True
        if self._load_error is not None:
            return False
        if not KnowledgeStore.exists():
            self._load_error = (
                f"no index at {config.KNOWLEDGE_RAG_INDEX_PATH} — run "
                "`python3 -m agent.knowledge_rag.build_index` first"
            )
            return False
        try:
            self._store = KnowledgeStore.load()
            self._client = EmbedClient()
        except Exception as e:
            self._load_error = f"failed to load knowledge-RAG index: {e}"
            return False
        return True

    def search(self, query: str, *, top_k: int = 5, source: str | None = None) -> dict:
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "query must not be empty"}
        if not self._ensure_loaded():
            return {"ok": False, "error": self._load_error}
        try:
            qvec = self._client.embed_query(query)
        except EmbedClientError as e:
            return {"ok": False, "error": f"embedding server unavailable: {e}"}

        query_tokens = set(_TOKEN_RE.findall(query.lower()))

        # Phase 1: Dense retrieval (cosine) — wide candidate pool
        candidates = self._store.search(qvec, top_k=_CANDIDATE_POOL, source_filter=source)

        # Phase 1b: Tag recall — if the query mentions a known binary, force-include
        # entries tagged with it (cosine may miss them when the entry text diverges
        # from the query phrasing, e.g. "find — Shell" vs "find SUID privesc")
        query_binaries = query_tokens & _KNOWN_BINARIES
        if query_binaries:
            existing_titles = {(h.get("source", ""), h.get("title", "")) for h in candidates}
            import numpy as np
            q = np.array(qvec, dtype=np.float32)
            qn = np.linalg.norm(q)
            if qn > 0:
                q = q / qn
            for idx, meta in enumerate(self._store.meta):
                if source and meta.get("source") != source:
                    continue
                key = (meta.get("source", ""), meta.get("title", ""))
                if key in existing_titles:
                    continue
                meta_tags = {str(t).lower() for t in (meta.get("tags") or [])}
                if meta_tags & query_binaries:
                    cos = float(self._store.vectors[idx] @ q)
                    candidates.append({**meta, "score": cos})
                    existing_titles.add(key)

        # Phase 2: Hybrid re-scoring (cosine + BM25 + keyword bonus)
        for h in candidates:
            cosine = h["score"]
            bm25 = _bm25_score(query_tokens, h.get("text", "") + " " + h.get("title", ""))
            kw_bonus = _keyword_bonus(query, h, query_tokens)
            # Normalize BM25 to roughly same scale as cosine (0-1)
            bm25_norm = min(bm25 / 15.0, 0.3)
            h["score"] = cosine * 0.65 + bm25_norm + kw_bonus
            h["_debug"] = {"cosine": round(cosine, 3), "bm25": round(bm25_norm, 3),
                           "kw": round(kw_bonus, 3)}

        # Phase 3: Deduplicate — keep best hit per (source, title_prefix)
        seen = set()
        deduped = []
        for h in sorted(candidates, key=lambda h: -h["score"]):
            dedup_key = (h.get("source", ""), h.get("title", "")[:40])
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            deduped.append(h)

        hits = deduped[:top_k]
        return {"ok": True, "results": [
            {"title": h["title"], "text": h["text"], "source": h["source"],
             "url": h.get("url", ""), "score": round(h["score"], 3)}
            for h in hits
        ]}

    def count(self) -> int:
        if not self._ensure_loaded():
            return 0
        return len(self._store)
