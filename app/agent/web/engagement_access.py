"""The account boundary for the web runtime — `AGENT_ARCHITECTURE.md` §5.3, canonical.

The owner's rule: *the same account may see everything; a different account must never see
anything.* §5.3's mechanism is "a key per account, an owner on every engagement, and every read
filtered by owner — enforced in code, with a test." This module is that code; the test lives in
`test_engagement_access.py` and in the event-stream tests, because the event subscription is the
read §5.3 most needs to hold (`CLIENT_UI_DESIGN.md` §4.1.2: it is the largest read in the system
— tool arguments, outputs, model calls, evidence pointers, spend — so an unfiltered one hands
another account the whole engagement in real time, through the most convenient door).

Deliberately minimal, and minimal in a way that still *enforces* rather than stubs:

  * **Identity collapses to one operator by default.** With no `web_ui_accounts.json` the only
    account is `config.WEB_UI_DEFAULT_ACCOUNT`, so the single-key and no-key deployments — and
    every test that predates this — are unchanged. The boundary ships but has one side.
  * **It becomes real the moment a second account exists.** Writing `web_ui_accounts.json`
    ({account_id: key}) makes each listed key valid and maps it to its account; an engagement is
    owned by whoever created it, and a read by anyone else is refused. The refusal is not a flag
    someone remembers to set — `owns()` is the only thing the endpoints call, and removing the
    call fails the two-account test, which is the whole point of writing that test first.

This is the light end of §5.3 on purpose (no rotation, no per-device identity — the same scope
`WEB_UI_API_KEY_FILE`'s own comment draws). The heavier `DeviceStore` pattern is where to go if a
genuinely shared multi-user deployment ever appears; the seam here does not have to be rebuilt to
get there, because every read already asks `owns()` and every engagement already records an owner.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path

from .. import config


class AccountError(RuntimeError):
    """The caller's key is not valid for any account (a 401 at the edge)."""


def valid_accounts() -> dict[str, str]:
    """{account_id: api_key} — the authority on which keys are valid and whose each is.

    `web_ui_accounts.json` wins when present. Otherwise the single `WEB_UI_API_KEY_FILE` is the
    one operator's key, and an empty map means auth is off (loopback is the boundary) — exactly
    the three states `require_api_key` already documents, now with an account attached to each.
    """
    if config.WEB_UI_ACCOUNTS_FILE.exists():
        data = json.loads(config.WEB_UI_ACCOUNTS_FILE.read_text())
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v for k, v in data.items()
        ):
            raise AccountError(
                f"{config.WEB_UI_ACCOUNTS_FILE} must be a JSON object of "
                "{account_id: non-empty api_key}"
            )
        return dict(data)
    if config.WEB_UI_API_KEY_FILE.exists():
        key = config.WEB_UI_API_KEY_FILE.read_text().strip()
        if key:
            return {config.WEB_UI_DEFAULT_ACCOUNT: key}
    return {}


def resolve_account(presented_key: str | None) -> str | None:
    """The account the presented key belongs to, or None if it belongs to none.

    With auth off (no valid accounts configured) every caller is the single default operator —
    the pre-§5.3 world, where loopback is the only boundary and there is exactly one account to
    own anything. With accounts configured, the key must match one (constant-time) or the caller
    is nobody.
    """
    accounts = valid_accounts()
    if not accounts:
        return config.WEB_UI_DEFAULT_ACCOUNT
    supplied = presented_key or ""
    for account_id, key in accounts.items():
        if hmac.compare_digest(supplied, key):
            return account_id
    return None


def engagement_owner(engagement_id: str, *, engagements_root: Path | None = None) -> str | None:
    """The account that owns this engagement, or None if there is no such engagement.

    An engagement whose `roe.json` carries no `owner` is `config.WEB_UI_DEFAULT_ACCOUNT`'s —
    every engagement created before §5.3 existed, and every one created in a single-operator
    deployment, so ownership did not retroactively lock anyone out of their own work.
    """
    root = engagements_root or config.ENGAGEMENTS_ROOT
    roe_path = root / engagement_id / "roe.json"
    if not roe_path.exists():
        return None
    try:
        roe = json.loads(roe_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    owner = roe.get("owner")
    return owner if isinstance(owner, str) and owner else config.WEB_UI_DEFAULT_ACCOUNT


def owns(account_id: str | None, engagement_id: str, *, engagements_root: Path | None = None) -> bool | None:
    """Whether `account_id` may read `engagement_id`. None means *no such engagement* (so the
    caller can 404 rather than 403 — not leaking the existence of someone else's engagement is
    the same boundary from the other side). True/False is the ownership decision itself.

    This is the one function the endpoints call. There is no second place a read is authorised,
    so there is no second place it can be forgotten."""
    owner = engagement_owner(engagement_id, engagements_root=engagements_root)
    if owner is None:
        return None
    return account_id is not None and account_id == owner
