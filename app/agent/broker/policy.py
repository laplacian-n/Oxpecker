"""RoE/scope/deny loading — §8c, §12 Phase-3 exit criterion "stale or missing policy fails
closed". Loading never returns a permissive default; any problem raises, and the broker
treats a raised PolicyError as deny-everything, not "skip the check."
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


class PolicyError(RuntimeError):
    """Raised for any missing/malformed/expired policy — the broker must fail closed on this."""


@dataclass
class Policy:
    engagement_id: str
    allowed_action_classes: set[str]
    allow_networks: list[IPNetwork]
    allow_hostnames: set[str]
    deny_networks: list[IPNetwork]
    deny_hostnames: set[str]
    policy_version: str
    valid_until: float
    # Wildcard scope: the registrable parent of a `*.example.com` entry, stored WITHOUT the
    # `*.` — so `example.com` here means "any strict subdomain of example.com". Defaulted so
    # every existing keyword construction of Policy keeps working unchanged.
    allow_suffixes: set[str] = field(default_factory=set)
    deny_suffixes: set[str] = field(default_factory=set)
    # The program this engagement runs under, when there is one. None is a legal state — every
    # local lab engagement — and defaulting it to a permissive Program would make the absence
    # invisible, so the broker branches on `is None` rather than on field values.
    program: "object | None" = None


class ScopeLineError(ValueError):
    """A scope entry that cannot be accepted — malformed, or a wildcard too broad to be a scope."""


WILDCARD_PREFIX = "*."

# A wildcard whose parent is a public suffix would put every registrable domain under it in
# scope. One label (`*.com`) is caught structurally; these are the common two-label public
# suffixes that would otherwise pass that check. This is deliberately a short deny-list and NOT
# the Public Suffix List: pulling in a PSL dependency (and keeping it current) is not warranted
# for a check whose input is an operator-typed scope line, and the structural rule below already
# refuses the shapes that do unbounded damage. The limitation is that an unusual public suffix
# not listed here would be accepted if an operator typed it.
_PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.br", "net.br", "org.br", "gov.br",
    "co.in", "net.in", "org.in", "gen.in", "firm.in",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "web.za", "gov.za",
    "com.mx", "com.ar", "com.tr", "com.sg", "com.hk", "com.tw", "com.my",
    "co.kr", "or.kr", "ne.kr", "go.kr",
    "co.th", "in.th", "ac.th", "go.th", "or.th", "net.th",
})


def normalize_host(host: str) -> str:
    """One spelling of a hostname, so two layers cannot disagree about whether they match.

    Lowercased, trailing root dot removed. The root dot matters: `example.com.` and
    `example.com` are the same host to a resolver, so leaving it in would let `example.com.`
    slip past an exact-match deny entry written without it.
    """
    return host.strip().rstrip(".").lower()


def parse_scope_line(line: str):
    """Classify one scope/deny entry. The single place this decision is made.

    Returns `("network", IPNetwork)`, `("hostname", str)`, `("suffix", str)`, or `None` for a
    blank line or a comment. Raises `ScopeLineError` for an entry that must not be accepted.

    `("suffix", "example.com")` comes from `*.example.com` and means *strict subdomains only* —
    the apex is a separate host and bug-bounty programs list it separately when it is in scope,
    so inferring it here would silently widen the scope beyond what the operator wrote.
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None

    # A URL pasted into a targets field is common enough to accept rather than reject.
    if "://" in line:
        from urllib.parse import urlparse

        parsed_host = urlparse(line).hostname or ""
        if not parsed_host:
            raise ScopeLineError(f"no host could be read from {line!r}")
        line = parsed_host

    if line.startswith(WILDCARD_PREFIX):
        parent = normalize_host(line[len(WILDCARD_PREFIX):])
        if not parent:
            raise ScopeLineError(f"{line!r} has no parent domain after the wildcard")
        if "*" in parent:
            raise ScopeLineError(
                f"{line!r} has more than one wildcard; only a leading '*.' is supported"
            )
        labels = parent.split(".")
        if len(labels) < 2 or not all(labels):
            raise ScopeLineError(
                f"{line!r} would put a whole top-level domain in scope; a wildcard needs at "
                f"least a registrable domain (e.g. '*.example.com')"
            )
        if parent in _PUBLIC_SUFFIXES:
            raise ScopeLineError(
                f"{line!r} would put every domain under the public suffix {parent!r} in scope"
            )
        return "suffix", parent

    if "*" in line:
        # Mid-label and suffix wildcards (`a.*.example.com`, `example.*`) are refused rather
        # than approximated: a glob that silently matches more than the operator pictured is
        # the one failure mode this module exists to prevent.
        raise ScopeLineError(
            f"{line!r} is not supported; a wildcard may only appear as a leading '*.'"
        )

    try:
        return "network", ipaddress.ip_network(line, strict=False)
    except ValueError:
        pass

    host = normalize_host(line)
    if not host or "/" in host or " " in host:
        raise ScopeLineError(f"{line!r} is not a valid CIDR, IP or hostname")
    return "hostname", host


def classify_scope_lines(lines) -> tuple[list[IPNetwork], set[str], set[str]]:
    """(networks, hostnames, suffixes) for a sequence of entries. Raises on the first bad one —
    a scope half-loaded is a scope nobody can reason about."""
    networks: list[IPNetwork] = []
    hostnames: set[str] = set()
    suffixes: set[str] = set()
    for line in lines or []:
        parsed = parse_scope_line(str(line))
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "network":
            networks.append(value)
        elif kind == "hostname":
            hostnames.add(value)
        else:
            suffixes.add(value)
    return networks, hostnames, suffixes


def _parse_scope_file(path: Path) -> tuple[list[IPNetwork], set[str], set[str]]:
    if not path.exists():
        raise PolicyError(f"policy file missing: {path}")
    try:
        return classify_scope_lines(path.read_text().splitlines())
    except ScopeLineError as e:
        # Fail closed: a scope file with an entry we will not honour must not load as a policy
        # that silently drops it.
        raise PolicyError(f"{path}: {e}") from e


def load_policy(engagement_dir: Path = config.ENGAGEMENT_DIR) -> Policy:
    roe_path = engagement_dir / "roe.json"
    scope_path = engagement_dir / "scope.txt"
    deny_path = engagement_dir / "deny.txt"

    for p in (roe_path, scope_path, deny_path):
        if not p.exists():
            raise PolicyError(f"policy file missing: {p}")

    roe_raw = roe_path.read_text()
    scope_raw = scope_path.read_text()
    deny_raw = deny_path.read_text()

    try:
        roe = json.loads(roe_raw)
    except json.JSONDecodeError as e:
        raise PolicyError(f"malformed roe.json: {e}") from e

    try:
        valid_from = time.strptime(roe["valid_from"], "%Y-%m-%dT%H:%M:%SZ")
        valid_until = time.strptime(roe["valid_until"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, ValueError) as e:
        raise PolicyError(f"malformed roe.json valid_from/valid_until: {e}") from e

    import calendar

    valid_from_ts = calendar.timegm(valid_from)
    valid_until_ts = calendar.timegm(valid_until)
    now = time.time()
    if not (valid_from_ts <= now <= valid_until_ts):
        raise PolicyError(
            f"policy is not currently valid (window {roe['valid_from']}..{roe['valid_until']})"
        )

    allow_networks, allow_hostnames, allow_suffixes = _parse_scope_file(scope_path)
    deny_networks, deny_hostnames, deny_suffixes = _parse_scope_file(deny_path)

    allowed_action_classes = set(roe.get("allowed_action_classes") or [])
    if not allowed_action_classes:
        raise PolicyError("roe.json declares no allowed_action_classes")

    digest = hashlib.sha256(
        (roe_raw + "\x00" + scope_raw + "\x00" + deny_raw).encode("utf-8")
    ).hexdigest()

    return Policy(
        engagement_id=roe.get("engagement_id", "unknown"),
        allowed_action_classes=allowed_action_classes,
        allow_networks=allow_networks,
        allow_hostnames=allow_hostnames,
        deny_networks=deny_networks,
        deny_hostnames=deny_hostnames,
        policy_version=digest,
        valid_until=valid_until_ts,
        allow_suffixes=allow_suffixes,
        deny_suffixes=deny_suffixes,
    )
