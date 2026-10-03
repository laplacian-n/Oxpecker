"""RoE/scope/deny loading — §8c, §12 Phase-3 exit criterion "stale or missing policy fails
closed". Loading never returns a permissive default; any problem raises, and the broker
treats a raised PolicyError as deny-everything, not "skip the check."
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass
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


def _parse_scope_file(path: Path) -> tuple[list[IPNetwork], set[str]]:
    if not path.exists():
        raise PolicyError(f"policy file missing: {path}")
    networks: list[IPNetwork] = []
    hostnames: set[str] = set()
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            networks.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            hostnames.add(line.lower())
    return networks, hostnames


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

    allow_networks, allow_hostnames = _parse_scope_file(scope_path)
    deny_networks, deny_hostnames = _parse_scope_file(deny_path)

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
    )
