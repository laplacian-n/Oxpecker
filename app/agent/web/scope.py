"""Scope enforcement for the web runtime, built on the broker's matcher rather than beside it.

`dev_server.py` grew its own inline scope check, and it did not hold. It had three defects that
failed independently:

  * `if eng and eng.allow_targets and host:` — an empty allowlist short-circuited the whole
    check, so an engagement created without targets permitted every host. `create_engagement`
    defaults `allow_targets` to `[]`, so that was reachable through the public API.
  * `host in str(t).lower()` — substring matching in the permissive direction. With
    `notevil.example.com` in scope, `evil.example.com` was allowed, because it is a substring of
    it. Parent domains were admitted by narrower entries for the same reason.
  * Targets were harvested out of operator message text and appended to the allowlist
    automatically, so naming a URL in conversation authorised it.

The fix is not to patch those three in place. `broker/scope_check.validate_target()` already
does this correctly for the CLI — exact hostname matching against a set, CIDR matching for IP
literals and resolved addresses, deny rules evaluated before allow rules so a deny always wins
(APTS-SE-009), and a terminal `not_in_scope` deny when nothing matches, which makes it fail
closed by construction rather than by a flag someone has to remember to check. Reusing it means
the two runtimes cannot drift, and the web runtime inherits every future fix to the matcher.

What this module does is translate: the web runtime holds scope in an in-memory `Engagement`
dataclass, the broker expects a `Policy`. That translation is the whole job.

Known limitation, deliberately not papered over: `validate_target()` returns the specific IP the
caller must connect to, which is how the CLI's `http_recon` prevents DNS rebinding between the
check and the connection. `dev_server`'s `http_request` calls `urllib.request.urlopen(url)`,
which re-resolves the hostname itself, so the returned IP is currently checked and then
discarded. The check is still worth having — it is what stands between the agent and an
arbitrary host — but the pinning half needs `http_request` rewritten onto a pinned transport,
which is tracked as the `http_request` work in docs/TOOLING_ROADMAP.md. `ScopeDecision.pinned_ip`
is carried through so that rewrite has it ready and so the audit entry can record what was
actually resolved.
"""
from __future__ import annotations

import calendar
import ipaddress
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from ..broker import scope_check
from ..broker.policy import Policy

# Mirrors `policy._parse_scope_file`: an entry that parses as a network is a network, anything
# else is a hostname. Kept as one function so the two cannot classify the same string
# differently.
def _classify_targets(targets: list[str]) -> tuple[list, set[str]]:
    networks, hostnames = [], set()
    for raw in targets or []:
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        # An operator pasting a full URL into the targets field is common enough to handle here
        # rather than reject: take its hostname. A bare host or CIDR passes through untouched.
        if "://" in line:
            line = urlparse(line).hostname or ""
            if not line:
                continue
        try:
            networks.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            hostnames.add(line.lower())
    return networks, hostnames


def _parse_valid_until(value: str) -> float | None:
    """`None` means no expiry was configured, which is distinct from an expired one.

    The shipped `lab-default` engagement carries no `valid_until`, so treating an absent value as
    expired would deny every out-of-box request. An absent window is reported rather than
    enforced; a window that *is* set is enforced, which is the actual defect being fixed —
    `create_engagement` writes a `valid_until` from `valid_hours` and nothing ever read it, so an
    engagement declared valid for 24 hours worked indefinitely.
    """
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return None


# Denied for every engagement, mirroring the base deny.txt that
# `engagement/intake.create_engagement()` writes for CLI-created engagements. The link-local
# metadata addresses are the ones that matter: an agent persuaded to fetch them hands back cloud
# credentials, and SSRF-to-IMDS is a standard finding a pentest agent will stumble into on its
# own. The web runtime had no deny list at all, so these were reachable whenever the operator's
# allowlist happened to admit them.
#
# Deny is evaluated before allow by `scope_check.validate_target`, so these cannot be
# re-permitted by adding them to an engagement's targets. That is the intended behaviour: a
# hard deny is not an operator preference.
BASE_DENY_NETWORKS = (
    "169.254.169.254/32",   # AWS/GCP/Azure/OpenStack instance metadata
    "fd00:ec2::254/128",    # AWS IMDSv6
    "169.254.170.2/32",     # ECS task metadata
)


@dataclass
class ScopeDecision:
    allowed: bool
    reason: str
    rule: str
    host: str = ""
    pinned_ip: str | None = None
    engagement_id: str = ""

    def as_tool_error(self) -> dict:
        """The shape `_run_tool` returns on refusal. Says what to do about it, because the model
        reads this string and the alternative is it retrying the same call or inventing a
        response."""
        return {
            "ok": False,
            "error": (
                f"out of scope: {self.reason}. This request was NOT sent. "
                f"Do not retry it and do not describe a result you did not receive. "
                f"If this target is authorised, the operator must add it to engagement "
                f"{self.engagement_id!r} first."
            ),
            "scope_rule": self.rule,
        }


def policy_from_engagement(eng, *, now: float | None = None) -> Policy:
    """Build the broker's `Policy` from the web runtime's in-memory `Engagement`.

    The deny list is `BASE_DENY_NETWORKS` only. The web `Engagement` has no deny field of its
    own, so per-engagement deny entries are not yet expressible here — worth adding, since
    deny-wins is the more useful half of the matcher. The base entries are not an invented
    policy: they are the same ones `intake.create_engagement()` writes into every CLI
    engagement's deny.txt.
    """
    networks, hostnames = _classify_targets(getattr(eng, "allow_targets", []))
    valid_until = _parse_valid_until(getattr(eng, "valid_until", "") or "")
    deny_networks = [ipaddress.ip_network(n) for n in BASE_DENY_NETWORKS]
    return Policy(
        engagement_id=getattr(eng, "engagement_id", ""),
        allowed_action_classes=set(getattr(eng, "allowed_action_classes", []) or []),
        allow_networks=networks,
        allow_hostnames=hostnames,
        deny_networks=deny_networks,
        deny_hostnames=set(),
        policy_version="web-engagement/in-memory",
        valid_until=valid_until if valid_until is not None else float("inf"),
    )


def check_url(url: str, eng, *, now: float | None = None) -> ScopeDecision:
    """The one place the web runtime decides whether a URL may be requested.

    Denies — never permits — when the engagement is missing, has no targets, has expired, or the
    URL has no host. Each of those was previously a path to an allowed request.
    """
    now = time.time() if now is None else now
    eng_id = getattr(eng, "engagement_id", "") if eng is not None else ""

    if eng is None:
        return ScopeDecision(
            False, "the session references an engagement that does not exist",
            "no_engagement", engagement_id=eng_id,
        )

    targets = getattr(eng, "allow_targets", None) or []
    if not targets:
        return ScopeDecision(
            False,
            f"engagement {eng_id!r} has an empty target list, so nothing is in scope "
            "(an empty allowlist permits nothing, it does not permit everything)",
            "empty_allowlist", engagement_id=eng_id,
        )

    valid_until = _parse_valid_until(getattr(eng, "valid_until", "") or "")
    if valid_until is not None and now > valid_until:
        return ScopeDecision(
            False,
            f"engagement {eng_id!r} expired at {getattr(eng, 'valid_until', '')}",
            "engagement_expired", engagement_id=eng_id,
        )

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ScopeDecision(
            False, f"could not extract a host from {url!r}", "unparseable_url",
            engagement_id=eng_id,
        )

    result = scope_check.validate_target(host, policy_from_engagement(eng, now=now))
    return ScopeDecision(
        allowed=bool(result.allowed),
        reason=result.reason if not result.allowed else f"host {host!r}: {result.reason}",
        rule=result.policy_rule,
        host=host,
        pinned_ip=result.validated_ip,
        engagement_id=eng_id,
    )
