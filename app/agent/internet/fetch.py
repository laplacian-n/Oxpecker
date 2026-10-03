"""M5.5 — the actual knowledge_fetch outbound HTTP call, wired live this pass after the owner
chose to enable it (ADR-0007). Reuses `agent.security_tools.http_recon._PinnedConnection` (dial
the SSRF-checked IP directly, Host header/TLS SNI carry the original hostname) rather than
reimplementing pinned-connection logic a second time.

Single hop only — a redirect is reported in the result, never auto-followed: unlike
`http_recon`'s RoE-scoped target (where every hop is independently re-validated against the same
engagement policy), knowledge_fetch targets arbitrary external URLs, so the caller deciding to
follow a redirect makes a new, separately-policy-checked `knowledge_fetch` call rather than this
function silently chaining through hosts on its own.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .. import config
from ..security_tools.http_recon import _PinnedConnection
from .policy import check_knowledge_fetch_allowed


def do_fetch(url: str, validated_ip: str | None = None) -> dict:
    if validated_ip is None:
        validated_ip = check_knowledge_fetch_allowed(url)

    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    conn = _PinnedConnection(
        validated_ip, host, port, parsed.scheme == "https", config.KNOWLEDGE_FETCH_TIMEOUT_S,
    )
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host)
        conn.putheader("User-Agent", "localai-knowledge-fetch/1.0")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        # Same discipline as http_recon: http.client never auto-decompresses, so a
        # gzip/deflate-bomb response only ever yields the raw bytes actually read here, capped
        # exactly like any other body — no expansion step for a bomb to exploit.
        body = resp.read(config.KNOWLEDGE_FETCH_MAX_BYTES)
        headers = dict(resp.getheaders())
        return {
            "ok": True,
            "url": url,
            "validated_ip": validated_ip,
            "status": resp.status,
            "reason": resp.reason,
            "headers": headers,
            "body_excerpt": body.decode("utf-8", errors="replace"),
            "body_truncated": len(body) >= config.KNOWLEDGE_FETCH_MAX_BYTES,
            "redirect_location": headers.get("Location") or headers.get("location"),
        }
    finally:
        conn.close()
