"""Target validation — APTS-SE-006 (pre-action validation), SE-009 (deny-before-allow),
SE-012 (DNS-rebinding prevention).

The contract every network-touching tool must follow: call validate_target() once, get back a
*specific validated IP*, and connect using that IP — never re-resolve the hostname at connect
time. Re-resolving after validation is exactly the DNS-rebinding gap (a malicious/misconfigured
DNS server returns an in-scope IP for the check, then a different, out-of-scope IP moments
later for the real connection since the TTL can be set to 0).
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

from .policy import Policy, normalize_host


@dataclass
class ValidationResult:
    allowed: bool
    validated_ip: str | None
    reason: str
    policy_rule: str


def _matches_any(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, networks) -> str | None:
    for net in networks:
        if ip in net:
            return str(net)
    return None


def _matching_suffix(host: str, suffixes, *, include_apex: bool) -> str | None:
    """The `*.parent` entry that covers `host`, or None.

    Three properties this has to get right, because getting any of them wrong is a scope bypass:

    * The dot is required. `host.endswith(parent)` would match `evilexample.com` against
      `*.example.com` — the same permissive-substring class of bug that the web runtime's
      original inline scope check shipped with.
    * `include_apex` is NOT a convenience flag; the two directions genuinely
      differ, and sharing one rule between them was a real bug. On the ALLOW side the apex is
      excluded: `*.example.com` does not put `example.com` in scope, because a program that
      includes the apex lists it separately and inferring it would authorise a host the
      operator did not write. On the DENY side the apex is included: an operator writing
      `*.customer.com` into a deny list to carve a domain out of a broad allow means the whole
      domain, and excluding the apex there would block every subdomain while leaving the apex —
      usually the most sensitive host — permitted, and recorded as a legitimate in-scope action.
      Excluding is conservative one way round and permissive the other.
    * The longest matching entry is returned, so the rule shown to the operator is the specific
      one rather than whichever happened to be first in an unordered set.
    """
    best = None
    for parent in suffixes:
        if host == parent:
            if not include_apex:
                continue
        elif not host.endswith("." + parent):
            continue
        if best is None or len(parent) > len(best):
            best = parent
    return best


def _resolve(hostname: str) -> list[str]:
    """All A/AAAA-resolved IPs for hostname. Raises socket.gaierror if resolution fails."""
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    seen = []
    for family, _, _, _, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in seen:
            seen.append(ip)
    return seen


def validate_target(host: str, policy: Policy) -> ValidationResult:
    """host: hostname or IP literal (no port, no scheme). Returns a specific IP to connect to.

    Every resolved IP must individually clear deny (checked first, deny always wins) and at
    least one must be in the allowlist — but the IP actually *returned* (and that the caller
    must connect to) is the first allowed one, fixed at validation time, not re-resolved later.
    """
    # Literal IP — no DNS involved.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        candidates = [str(literal)]
    else:
        try:
            candidates = _resolve(host)
        except socket.gaierror as e:
            return ValidationResult(False, None, f"DNS resolution failed: {e}", "dns_error")
        if not candidates:
            return ValidationResult(False, None, "DNS resolution returned no addresses", "dns_error")

    hostname_lower = normalize_host(host)

    # Deny checked first, before allow — deny always wins (APTS-SE-009), even if the same
    # host/IP also appears in the allowlist.
    if hostname_lower in policy.deny_hostnames:
        return ValidationResult(False, None, f"hostname {host!r} is on the deny list", "deny.txt:hostname")
    denied_suffix = _matching_suffix(
        hostname_lower, getattr(policy, "deny_suffixes", ()) or (), include_apex=True)
    if denied_suffix is not None:
        return ValidationResult(
            False, None,
            f"hostname {host!r} is under denied domain {denied_suffix!r}",
            f"deny.txt:*.{denied_suffix}",
        )
    for ip_str in candidates:
        ip = ipaddress.ip_address(ip_str)
        rule = _matches_any(ip, policy.deny_networks)
        if rule is not None:
            return ValidationResult(
                False, None, f"resolved IP {ip_str} matches deny rule {rule}", f"deny.txt:{rule}"
            )

    if hostname_lower in policy.allow_hostnames:
        # Hostname itself is explicitly in scope — still need a concrete IP to connect to and
        # to fix it (rebinding prevention), so use the first resolved candidate.
        return ValidationResult(True, candidates[0], "hostname explicitly in scope", f"scope.txt:{hostname_lower}")

    allowed_suffix = _matching_suffix(
        hostname_lower, getattr(policy, "allow_suffixes", ()) or (), include_apex=False)
    if allowed_suffix is not None:
        # Reached only after every resolved IP cleared the deny networks above, so a wildcard
        # allow cannot admit a host that resolves into denied space (cloud metadata included).
        return ValidationResult(
            True, candidates[0],
            f"hostname under in-scope domain {allowed_suffix!r}",
            f"scope.txt:*.{allowed_suffix}",
        )

    for ip_str in candidates:
        ip = ipaddress.ip_address(ip_str)
        rule = _matches_any(ip, policy.allow_networks)
        if rule is not None:
            return ValidationResult(True, ip_str, f"resolved IP {ip_str} in scope", f"scope.txt:{rule}")

    return ValidationResult(
        False, None, f"no resolved address for {host!r} ({candidates}) is in scope", "not_in_scope"
    )


def validate_redirect_target(url_host: str, policy: Policy) -> ValidationResult:
    """Same check, used before following an HTTP redirect — never auto-follow without this."""
    return validate_target(url_host, policy)
