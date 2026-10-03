"""M5.5 osint_discovery channel: records something noticed incidentally (a subdomain mentioned
in a page, an email address in response content, a linked third-party service) as
`observed_out_of_scope` in the engagement state store. This channel makes NO outbound network
call of its own — it is a structured place to file things the model happens to see while working
in-scope, not a discovery tool that goes looking. "Never actively probed" (ROADMAP) is enforced
structurally here: there is no fetch/request function in this module at all.
"""
from __future__ import annotations

from ..engagement.store import EngagementStore


def record_out_of_scope(
    store: EngagementStore, observation_type: str, content: str, source: str
) -> str:
    return store.add_observation(
        observation_type=f"observed_out_of_scope:{observation_type}",
        content=content,
        source=source,
    )
