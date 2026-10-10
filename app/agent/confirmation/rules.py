"""Confirmation rules (§8.6.4 / §8.6.6).

A confirmation rule raises a finding's assurance from the `model` floor to `rule`. It is a
deterministic predicate over a list of observations (plain dicts). Rules are versioned, and
every rule must ship with at least one positive and one negative fixture (enforced by
test_rules.py), because a rule with only passing cases says yes to everything.

check() functions must be pure: no I/O, no mutation of their input.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass
class Fixture:
    name: str
    observations: list[dict]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    description: str
    check: Callable[[list[dict]], bool]
    positive_fixtures: list[Fixture]
    negative_fixtures: list[Fixture]


def rule_ref(rule: Rule) -> str:
    """The string a finding stores in `rule_disagreed` / labels `confirmed_by: rule` with."""
    return f"{rule.rule_id}@{rule.version}"


REGISTRY: dict[str, Rule] = {}


def register(rule: Rule) -> None:
    if rule.rule_id in REGISTRY:
        raise ValueError(f"confirmation rule already registered: {rule.rule_id!r}")
    REGISTRY[rule.rule_id] = rule


def get(rule_id: str) -> Rule | None:
    return REGISTRY.get(rule_id)


def all_rules() -> list[Rule]:
    return list(REGISTRY.values())


# ---------------------------------------------------------------------------
# Example rules
# ---------------------------------------------------------------------------

def _has_open_port(observations: list[dict]) -> bool:
    return any(
        o.get("type") == "port" and o.get("state") == "open"
        for o in observations
    )


OPEN_PORT = Rule(
    rule_id="open_port",
    version=1,
    description="At least one port observation reports state 'open'.",
    check=_has_open_port,
    positive_fixtures=[
        Fixture(
            name="open_http_port",
            observations=[{"type": "port", "port": 80, "state": "open"}],
        ),
    ],
    negative_fixtures=[
        Fixture(
            name="closed_ssh_port_only",
            observations=[{"type": "port", "port": 22, "state": "closed"}],
        ),
    ],
)

_VERSION_TOKEN = re.compile(r"\d\.\d")


def _has_version_banner(observations: list[dict]) -> bool:
    for o in observations:
        if o.get("type") != "banner":
            continue
        value = o.get("value")
        if isinstance(value, str) and _VERSION_TOKEN.search(value):
            return True
    return False


VERSION_BANNER = Rule(
    rule_id="version_banner",
    version=1,
    description="At least one banner observation carries a version-looking token (N.N).",
    check=_has_version_banner,
    positive_fixtures=[
        Fixture(
            name="nginx_banner_with_version",
            observations=[{"type": "banner", "value": "nginx/1.21.0"}],
        ),
    ],
    negative_fixtures=[
        Fixture(
            name="nginx_banner_without_version",
            observations=[{"type": "banner", "value": "nginx"}],
        ),
    ],
)

register(OPEN_PORT)
register(VERSION_BANNER)
