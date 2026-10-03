"""M5.5 — per-channel policy for the three channels that reach beyond the engagement's RoE-
scoped target. `target_http` needs nothing new here: it reuses the existing broker/
`agent.broker.scope_check` machinery unchanged (`Broker.dispatch` already gates it).

- **knowledge_search**: provider allowlist. Empty by default — no search provider is configured
  or credentialed anywhere in this codebase yet (docs/STATUS.md's M5.5 gaps row), so every call
  is denied until an operator explicitly configures one. This is intentional fail-closed
  scaffolding, not a placeholder that quietly does nothing.
- **knowledge_fetch**: URL policy — http/https scheme only, plus a structural block on
  private/loopback/link-local/multicast/cloud-metadata IP ranges the resolved host lands on (the
  same SSRF-relevant categories `agent/broker/scope_check.py` already treats as sensitive for
  RoE targets, applied here to arbitrary external URLs).
- **osint_discovery**: no network policy at all — this channel makes no outbound call; see
  `osint.py`.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "100.64.0.0/10", "224.0.0.0/4", "0.0.0.0/8",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]


class FetchPolicyError(RuntimeError):
    pass


class SearchPolicyError(RuntimeError):
    pass


def check_knowledge_search_allowed(provider: str, allowed_providers: frozenset[str] = frozenset()) -> None:
    if provider not in allowed_providers:
        raise SearchPolicyError(
            f"knowledge_search provider {provider!r} not in the configured allowlist "
            f"{sorted(allowed_providers)!r} — no provider is configured by default "
            "(M5.5 scaffolding-only pass, see docs/STATUS.md)"
        )


def check_knowledge_fetch_allowed(url: str) -> str:
    """Raises FetchPolicyError if the URL isn't fetchable; otherwise returns the first resolved
    IP that cleared every blocked-range check, so the actual fetch (agent/internet/fetch.py) can
    dial that exact IP directly rather than re-resolving the hostname a second time (the same
    DNS-rebinding-prevention discipline agent/broker/scope_check.py already applies to RoE
    targets, applied here to arbitrary external URLs)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchPolicyError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchPolicyError(f"no host in URL: {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchPolicyError(f"DNS resolution failed for {host!r}: {e}") from e
    validated_ip: str | None = None
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                raise FetchPolicyError(
                    f"{host!r} resolves to {ip} in blocked range {network} (SSRF protection)"
                )
        if validated_ip is None:
            validated_ip = sockaddr[0]
    return validated_ip
