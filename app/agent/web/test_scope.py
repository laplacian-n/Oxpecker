"""Tests for web-runtime scope enforcement.

Every hostname check here stubs `scope_check._resolve` with a fixed map. That is not
convenience: without it, a host like `evil.example.com` is refused because DNS fails in the test
environment, and the test passes while proving nothing about the scope logic it claims to
cover. A denial has to be attributable to the matcher, so the assertions below check the rule
that produced it, not merely that something was refused.

Run directly: `python3 -m agent.web.test_scope`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from unittest.mock import patch

from ..broker import scope_check
from . import scope

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


@dataclass
class Eng:
    """Stands in for dev_server's in-memory Engagement — same attribute names."""
    engagement_id: str = "test-eng"
    allow_targets: list = field(default_factory=list)
    allowed_action_classes: list = field(default_factory=list)
    valid_until: str = ""


# Every name below resolves to a distinct, routable-looking address so that an allow decision
# cannot come from an address collision.
_DNS = {
    "target.example.com": ["203.0.113.10"],
    "evil.example.com": ["203.0.113.66"],
    "notevil.example.com": ["203.0.113.11"],
    "example.com": ["203.0.113.12"],
    "sub.target.example.com": ["203.0.113.13"],
    "localhost": ["127.0.0.1"],
}


def _resolve(hostname: str):
    try:
        return _DNS[hostname.lower()]
    except KeyError:
        import socket
        raise socket.gaierror(f"test stub has no record for {hostname!r}")


def main() -> int:
    with patch.object(scope_check, "_resolve", _resolve):
        print("\n== an empty allowlist denies, and says so specifically ==")
        d = scope.check_url("http://target.example.com/", Eng(allow_targets=[]))
        check("empty allowlist denies", not d.allowed)
        check("denial is attributed to the empty allowlist, not to DNS",
              d.rule == "empty_allowlist", f"rule={d.rule!r}")
        check("the reason corrects the misreading that empty means unrestricted",
              "permits nothing" in d.reason, d.reason)

        print("\n== substring matching is gone (the notevil/evil bypass) ==")
        eng = Eng(allow_targets=["notevil.example.com"])
        d = scope.check_url("http://evil.example.com/", eng)
        check("evil.example.com is denied when only notevil.example.com is in scope",
              not d.allowed, f"rule={d.rule!r}")
        check("the denial comes from the matcher, not from a DNS failure",
              d.rule == "not_in_scope", f"rule={d.rule!r} reason={d.reason!r}")
        check("the host that WAS in scope still resolves and is allowed",
              scope.check_url("http://notevil.example.com/", eng).allowed)

        print("\n== a narrower entry does not admit its parent or its siblings ==")
        eng = Eng(allow_targets=["target.example.com"])
        for host in ("example.com", "sub.target.example.com", "evil.example.com"):
            d = scope.check_url(f"http://{host}/", eng)
            check(f"{host} denied by exact matching", not d.allowed and d.rule == "not_in_scope",
                  f"rule={d.rule!r}")
        check("the exact host is allowed", scope.check_url("http://target.example.com/", eng).allowed)

        print("\n== hostname classification is exact, asserted without DNS ==")
        pol = scope.policy_from_engagement(Eng(allow_targets=["notevil.example.com"]))
        check("allow_hostnames holds the exact entry", pol.allow_hostnames == {"notevil.example.com"},
              str(pol.allow_hostnames))
        check("the bypass host is not a member", "evil.example.com" not in pol.allow_hostnames)

        print("\n== the shipped lab-default shape keeps working ==")
        lab = Eng(engagement_id="lab-default", allow_targets=["127.0.0.1", "localhost"])
        for url, want_ip in (("http://127.0.0.1:3000/", "127.0.0.1"),
                             ("http://localhost:3080/", "127.0.0.1")):
            d = scope.check_url(url, lab)
            check(f"{url} allowed", d.allowed, f"reason={d.reason!r}")
            check(f"{url} carries the resolved IP for later pinning", d.pinned_ip == want_ip,
                  f"pinned_ip={d.pinned_ip!r}")

        print("\n== IP literals and CIDR entries ==")
        check("IP literal in a /8 is allowed",
              scope.check_url("http://127.0.0.1/", Eng(allow_targets=["127.0.0.0/8"])).allowed)
        d = scope.check_url("http://203.0.113.66/", Eng(allow_targets=["127.0.0.0/8"]))
        check("IP literal outside the CIDR is denied", not d.allowed and d.rule == "not_in_scope",
              f"rule={d.rule!r}")
        check("a resolved address inside an allowed CIDR is allowed",
              scope.check_url("http://target.example.com/", Eng(allow_targets=["203.0.113.0/24"])).allowed)

        print("\n== a URL pasted into the targets field is accepted as its host ==")
        d = scope.check_url("http://target.example.com/a/b?c=1",
                            Eng(allow_targets=["https://target.example.com/some/path"]))
        check("full-URL target entry matches by host", d.allowed, f"reason={d.reason!r}")

        print("\n== engagement expiry is enforced ==")
        past = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        d = scope.check_url("http://target.example.com/",
                            Eng(allow_targets=["target.example.com"], valid_until=past))
        check("an expired engagement denies", not d.allowed and d.rule == "engagement_expired",
              f"rule={d.rule!r}")
        check("an unexpired engagement allows",
              scope.check_url("http://target.example.com/",
                              Eng(allow_targets=["target.example.com"], valid_until=future)).allowed)
        check("an absent expiry is treated as unset, not as expired",
              scope.check_url("http://target.example.com/",
                              Eng(allow_targets=["target.example.com"], valid_until="")).allowed)
        check("a malformed expiry is not silently treated as expired-and-denied",
              scope.check_url("http://target.example.com/",
                              Eng(allow_targets=["target.example.com"], valid_until="garbage")).allowed)

        print("\n== the remaining deny paths ==")
        check("a missing engagement denies",
              scope.check_url("http://target.example.com/", None).rule == "no_engagement")
        for bad in ("not-a-url", "", "http://", "///x"):
            d = scope.check_url(bad, Eng(allow_targets=["target.example.com"]))
            check(f"unparseable url {bad!r} denies", not d.allowed, f"rule={d.rule!r}")

        print("\n== the refusal text tells the model not to fabricate a result ==")
        err = scope.check_url("http://evil.example.com/",
                              Eng(allow_targets=["target.example.com"])).as_tool_error()
        check("ok is False", err["ok"] is False)
        check("states the request was not sent", "NOT sent" in err["error"], err["error"])
        check("tells the model not to invent a response",
              "do not describe a result" in err["error"].lower(), err["error"])
        check("names the engagement the operator must change",
              "test-eng" in err["error"], err["error"])
        check("carries the rule for the audit entry", bool(err.get("scope_rule")), str(err))

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
