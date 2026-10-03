"""Make an engagement usable from a bare id — create its state store, import the lab-default
RoE from the legacy path, and seed host assets from scope — without the operator hand-wiring it
first.

Two callers, one job:
  - the autonomous driver needs at least one host asset or RECON can never plan a task and the
    run is stuck at INTAKE forever;
  - an assistant session (AgentLoop) against `lab-default` otherwise hits security_mcp_server's
    `_engagement_store_or_error()` wall — "no M5.2 engagement state store" — the first time the
    model calls record_finding / osint_record / record_hypothesis, because that gate checks for
    `engagements/<id>/state.db` and nothing had created it.

Idempotent: an existing state.db is left alone, present RoE files are not overwritten, and
assets are only seeded when there are none.
"""
from __future__ import annotations

import logging
import shutil
from typing import Callable

from .. import config
from ..broker import policy as policy_mod
from .store import EngagementStore

log = logging.getLogger("agent.engagement.bootstrap")

_ROE_FILES = ("roe.json", "scope.txt", "deny.txt")


def bootstrap_engagement(
    engagement_id: str,
    *,
    store: EngagementStore | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> EngagementStore:
    """Returns the (existing or freshly created) EngagementStore for `engagement_id`."""
    emit = on_event or (lambda event: None)
    engagement_dir = config.ENGAGEMENTS_ROOT / engagement_id
    engagement_dir.mkdir(parents=True, exist_ok=True)

    # 1. `lab-default` historically keeps its RoE in the legacy `engagement/` dir, while its
    #    pipeline state lives under `engagements/lab-default/` — the broker and policy loader
    #    look in the engagement dir, so copy the RoE files across once. Scoped to lab-default:
    #    any other engagement is expected to have been created with its own RoE.
    if engagement_id == "lab-default" and not all((engagement_dir / f).exists() for f in _ROE_FILES):
        legacy = config.ENGAGEMENT_DIR
        if all((legacy / f).exists() for f in _ROE_FILES):
            for f in _ROE_FILES:
                shutil.copy2(legacy / f, engagement_dir / f)
            emit({"type": "engagement_bootstrapped",
                  "detail": "imported the lab-default RoE from the legacy engagement/ path"})

    # 2. constructing the store creates state.db (+ schema) if absent — this is exactly what
    #    security_mcp_server's _engagement_store_or_error() checks for.
    store = store or EngagementStore(engagement_dir)

    # 3. an engagement with a scope but no assets can never leave INTAKE (RECON plans work per
    #    host asset). Seed host assets from the in-scope entries — the web "New engagement" form
    #    already does this; this covers lab-default and any CLI/direct-created engagement.
    if not store.list_assets():
        try:
            policy = policy_mod.load_policy(engagement_dir)
        except policy_mod.PolicyError:
            return store  # no usable policy — the broker will fail closed with a clear reason
        seeded: list[str] = []
        for hostname in sorted(policy.allow_hostnames):
            store.upsert_asset("host", hostname)
            seeded.append(hostname)
        if not seeded:
            for net in policy.allow_networks:
                if net.num_addresses == 1:  # a single host, not a range we shouldn't blanket-scan
                    ip = str(net.network_address)
                    store.upsert_asset("host", ip)
                    seeded.append(ip)
        if seeded:
            emit({"type": "engagement_bootstrapped",
                  "detail": f"seeded {len(seeded)} host asset(s) from scope: {', '.join(seeded)}"})
    return store
