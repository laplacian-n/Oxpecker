"""M5.5 — the actual knowledge_search outbound call, wired live this pass to a self-hosted
SearXNG instance (loopback-bound Docker container, same pattern as this project's Juice
Shop/DVWA lab targets — see ADR-0007) chosen so results are provider-agnostic (SearXNG itself
federates multiple upstream engines) rather than sending every query straight to one third
party's own logs.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .. import config


class SearxngError(RuntimeError):
    pass


def do_search(query: str, max_results: int | None = None) -> list[dict]:
    max_results = max_results if max_results is not None else config.KNOWLEDGE_SEARCH_MAX_RESULTS
    url = f"{config.KNOWLEDGE_SEARCH_SEARXNG_URL}/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "localai-knowledge-search/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=config.KNOWLEDGE_FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        raise SearxngError(f"SearXNG request failed: {type(e).__name__}: {e}") from e

    results = []
    for r in data.get("results", [])[:max_results]:
        results.append(
            {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
        )
    return results
