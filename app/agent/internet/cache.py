"""M5.5 — cache + provenance for externally retrieved content. This is NOT the evidence store
(`agent/evidence/store.py`, encrypted, retention-classed, for engagement findings evidence) — this
is a lightweight, unencrypted freshness cache for external reference content, keyed by
channel+query so identical requests within the freshness window are served from cache instead of
re-fetched (budget-friendly, and gives every returned item a recorded provenance: what channel,
what query/url, when, and — once scanned — what injection-guard verdict it got).
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .. import config

DEFAULT_TTL_S = 3600
CACHE_DIR = config.STATE_DIR / "internet" / "cache"


class InternetCache:
    def __init__(self, cache_dir: Path | None = None):
        # Resolved from the module namespace at call time, not bound as a default-argument value
        # at import time — a `cache_dir: Path = CACHE_DIR` default would silently ignore a test's
        # `patch("agent.internet.cache.CACHE_DIR", tmp_dir)`. This exact bug already leaked real
        # state twice this session (agent/internet/budget.py, agent/skills/signing.py); fixed
        # here before it had the chance to happen a third time.
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, channel: str, query: str) -> str:
        return hashlib.sha256(f"{channel}:{query}".encode()).hexdigest()

    def get(self, channel: str, query: str, ttl_s: float = DEFAULT_TTL_S) -> dict | None:
        path = self.cache_dir / f"{self._key(channel, query)}.json"
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if time.time() - record["fetched_at"] > ttl_s:
            return None
        return record

    def put(self, channel: str, query: str, content: str, provenance: dict) -> dict:
        record = {
            "channel": channel,
            "query": query,
            "content": content,
            "fetched_at": time.time(),
            "provenance": provenance,
        }
        path = self.cache_dir / f"{self._key(channel, query)}.json"
        path.write_text(json.dumps(record))
        return record
