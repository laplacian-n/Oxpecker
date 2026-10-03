"""M5.5 — single entrypoint for all four internet-access channels.

`target_http` is RoE-scoped and reuses the existing broker/`agent.security_tools.http_recon`
path unchanged, so calling it introduces no new network exposure beyond what an engagement's
existing tools already have.

`knowledge_search` and `knowledge_fetch` are wired live this pass (ADR-0007) after the owner was
asked directly and chose to enable real internet egress: `knowledge_search` queries a self-hosted
SearXNG instance (`agent/internet/search.py`), `knowledge_fetch` performs a real, SSRF-checked,
pinned-IP HTTP GET (`agent/internet/fetch.py`, reusing `http_recon`'s `_PinnedConnection`). Both
still run through the same policy/budget/cache/quarantine layers built during the
scaffolding-only pass — nothing about those layers changed, only that a real call now sits behind
them instead of `ChannelNotEnabledError`.

`osint_discovery` never makes a network call at all — see `osint.py`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config, injection_guard
from ..broker.taint import TaintStore
from .budget import BudgetExceededError, ChannelBudgetTracker
from .cache import InternetCache
from .policy import (
    FetchPolicyError,
    SearchPolicyError,
    check_knowledge_fetch_allowed,
    check_knowledge_search_allowed,
)

KNOWLEDGE_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_search",
        "description": (
            "Search the web via a self-hosted metasearch instance for background/reference "
            "information (documentation, CVE writeups, general knowledge) — not for probing "
            "the RoE-scoped target itself, use http_recon/port_discovery for that. Returns "
            "title/url/content per result. Denied unless the current RoE explicitly allows the "
            "knowledge_search action class."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text."},
            },
            "required": ["query"],
        },
    },
}

KNOWLEDGE_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "knowledge_fetch",
        "description": (
            "Fetch a single external URL outside the RoE-scoped target — for reading a "
            "reference or documentation page (e.g. one found via knowledge_search), not for "
            "probing the target. Blocked for loopback/private/link-local/CGNAT/multicast/"
            "cloud-metadata addresses (SSRF protection) regardless of RoE. Denied unless the "
            "current RoE explicitly allows the knowledge_fetch action class."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "e.g. https://example.com/docs/page"},
            },
            "required": ["url"],
        },
    },
}


@dataclass
class ChannelResult:
    channel: str
    ok: bool
    content: str | None
    provenance: dict
    scan_verdict: str | None = None


def _scan_and_taint(session_id: str, channel: str, content: str, source: str) -> str:
    scan_result = injection_guard.scan(content)
    if scan_result.verdict in ("suspicious", "malicious", "unknown"):
        TaintStore(session_id).mark(
            reason=f"{channel} content flagged {scan_result.verdict}",
            verdict=scan_result.verdict,
            source=source,
        )
    return scan_result.verdict


def target_http(session_id: str, url: str, policy, verify_cert: bool = True) -> ChannelResult:
    from ..security_tools import http_recon

    tracker = ChannelBudgetTracker(session_id, "target_http")
    tracker.check_and_consume()
    result = http_recon.run(url, policy, verify_cert=verify_cert)
    content = str(result)
    verdict = _scan_and_taint(session_id, "target_http", content, url)
    return ChannelResult(
        channel="target_http",
        ok=bool(result.get("ok", False)),
        content=content,
        provenance={"url": url, "channel": "target_http"},
        scan_verdict=verdict,
    )


def knowledge_search(
    session_id: str,
    query: str,
    provider: str = "searxng-local",
    allowed_providers: frozenset[str] | None = None,
) -> ChannelResult:
    allowed = (
        allowed_providers if allowed_providers is not None else config.KNOWLEDGE_SEARCH_ALLOWED_PROVIDERS
    )
    check_knowledge_search_allowed(provider, allowed)
    tracker = ChannelBudgetTracker(session_id, "knowledge_search")
    tracker.check_and_consume()

    from .search import do_search

    results = do_search(query)
    content = str(results)
    verdict = _scan_and_taint(session_id, "knowledge_search", content, query)
    InternetCache().put(
        "knowledge_search", query, content, provenance={"query": query, "provider": provider}
    )
    return ChannelResult(
        channel="knowledge_search",
        ok=True,
        content=content,
        provenance={"query": query, "provider": provider, "result_count": len(results)},
        scan_verdict=verdict,
    )


def knowledge_fetch(session_id: str, url: str) -> ChannelResult:
    validated_ip = check_knowledge_fetch_allowed(url)
    tracker = ChannelBudgetTracker(session_id, "knowledge_fetch")
    tracker.check_and_consume()

    from .fetch import do_fetch

    result = do_fetch(url, validated_ip=validated_ip)
    content = result.get("body_excerpt", "")
    verdict = _scan_and_taint(session_id, "knowledge_fetch", content, url)
    InternetCache().put(
        "knowledge_fetch", url, content, provenance={"url": url, "status": result.get("status")}
    )
    return ChannelResult(
        channel="knowledge_fetch",
        ok=bool(result.get("ok", False)),
        content=content,
        provenance={"url": url, "status": result.get("status"), "validated_ip": validated_ip},
        scan_verdict=verdict,
    )
