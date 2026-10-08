"""Tests for wildcard scope entries (`*.example.com`) across all three layers that read them.

Wildcards are how every bug-bounty scope is written, and they are also the easiest place in this
codebase to introduce a scope bypass: `host.endswith(parent)` instead of
`host.endswith("." + parent)` silently puts `evilexample.com` in scope for `*.example.com`. That
is the same permissive-substring defect the web runtime's original inline scope check shipped
with, so it is tested here directly rather than trusted to review.

Run directly: `python3 -m agent.broker.test_wildcard_scope`.
"""
from __future__ import annotations

import ipaddress
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from . import policy as policy_mod
from . import scope_check
from .policy import Policy

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _policy(allow=(), deny=(), nets=(), deny_nets=()) -> Policy:
    return Policy(
        engagement_id="wc",
        allowed_action_classes={"active_web_request"},
        allow_networks=[ipaddress.ip_network(n) for n in nets],
        allow_hostnames=set(),
        deny_networks=[ipaddress.ip_network(n) for n in deny_nets],
        deny_hostnames=set(),
        policy_version="test",
        valid_until=time.time() + 3600,
        allow_suffixes=set(allow),
        deny_suffixes=set(deny),
    )


# Every resolution in this module is stubbed. A test that reached the real resolver would pass or
# fail on this container's DNS rather than on the matcher, and a denial has to be attributable to
# the rule under test — an earlier version of a scope test in this repo passed for the wrong
# reason (rule=dns_error) exactly because it did not do this.
def _resolver(mapping, default="93.184.216.34"):
    def fake(host, *a, **k):
        ip = mapping.get(host, default)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]
    return fake


def main() -> int:
    print("\n== the parser accepts the form scopes are written in, and refuses the dangerous ones ==")
    cases_ok = {
        "*.example.com": ("suffix", "example.com"),
        "*.sub.example.com": ("suffix", "sub.example.com"),
        "*.EXAMPLE.COM.": ("suffix", "example.com"),
        "example.com": ("hostname", "example.com"),
        "localhost": ("hostname", "localhost"),
        "https://a.b.example.com/path?q=1": ("hostname", "a.b.example.com"),
    }
    for line, expected in cases_ok.items():
        got = policy_mod.parse_scope_line(line)
        check(f"{line!r} parses as {expected}", got == expected, str(got))

    for line, why in {
        "*.com": "whole top-level domain",
        "*.co.uk": "public suffix",
        "*.com.au": "public suffix",
        "*": "bare wildcard",
        "*.": "nothing after the wildcard",
        "a.*.example.com": "wildcard in the middle",
        "example.*": "trailing wildcard",
        "*.*.example.com": "two wildcards",
    }.items():
        try:
            policy_mod.parse_scope_line(line)
            check(f"{line!r} is refused ({why})", False, "it was accepted")
        except policy_mod.ScopeLineError:
            check(f"{line!r} is refused ({why})", True)

    print("\n== a wildcard covers strict subdomains, and nothing else ==")
    pol = _policy(allow={"example.com"})
    with patch("socket.getaddrinfo", _resolver({})):
        for host, want in [
            ("sub.example.com", True),
            ("a.b.c.example.com", True),      # arbitrary depth
            ("example.com", False),           # the apex is a separate host
            ("evilexample.com", False),       # the dot is required
            ("notexample.com", False),
            ("example.com.evil.net", False),  # suffix must be at the END
            ("example.compromised.net", False),
        ]:
            r = scope_check.validate_target(host, pol)
            check(f"{host} -> {'allowed' if want else 'denied'}",
                  r.allowed is want, f"{r.allowed} ({r.policy_rule}: {r.reason})")

    with patch("socket.getaddrinfo", _resolver({})):
        r = scope_check.validate_target("example.com", pol)
    check("the apex denial is attributed to nothing matching, not to a DNS failure",
          r.policy_rule == "not_in_scope", r.policy_rule)

    print("\n== a deny wildcard covers the apex too — the directions are not symmetric ==")
    # Excluding the apex is conservative on the allow side and permissive on the deny side.
    # Sharing one rule between them left an operator who wrote `*.customer.com` into a deny list
    # with every subdomain blocked and the apex — usually the most sensitive host — permitted,
    # recorded as a legitimate in-scope action.
    broad = _policy(deny={"evil.com"}, nets=["0.0.0.0/0"])
    with patch("socket.getaddrinfo", _resolver({})):
        for host in ("sub.evil.com", "evil.com", "a.b.evil.com"):
            r = scope_check.validate_target(host, broad)
            check(f"deny '*.evil.com' blocks {host}", r.allowed is False,
                  f"{r.allowed} ({r.policy_rule})")
        check("and a lookalike is not swept up by the deny either",
              scope_check.validate_target("notevil.com", broad).allowed is True)

    print("\n== a hostname is validated against a charset, not by banning two characters ==")
    # `"/" in host or " " in host` let a TAB-separated inline comment through as a hostname, so
    # a deny entry became a string that can never match an IP: the hard deny list read as
    # populated while being empty, and the agent could reach cloud metadata.
    for line in ["169.254.169.254\t#metadata", "1.2.3.4\u00a0#nbsp", "-bad.com", "bad-.com",
                 "ex ample.com", "a" * 64 + ".com", "\u4f8b\u3048.jp", "*.ex ample.com",
                 "*.169.254.169.254\t#x"]:
        try:
            policy_mod.parse_scope_line(line)
            check(f"{line!r} is refused", False, "it was accepted")
        except policy_mod.ScopeLineError:
            check(f"{line!r} is refused", True)
    for line in ["localhost", "a-b.example.com", "host_1.internal", "xn--80ak6aa92e.com"]:
        check(f"{line!r} is still accepted",
              policy_mod.parse_scope_line(line) == ("hostname", line))

    tmp_tab = Path(tempfile.mkdtemp(prefix="wc-tab-"))
    (tmp_tab / "roe.json").write_text(
        '{"engagement_id": "wc", "allowed_action_classes": ["active_web_request"],'
        ' "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-01-01T00:00:00Z"}'
    )
    (tmp_tab / "scope.txt").write_text("0.0.0.0/0\n")
    (tmp_tab / "deny.txt").write_text("169.254.169.254\t#metadata\n")
    try:
        policy_mod.load_policy(tmp_tab)
        check("a deny file whose entry would be dead fails closed", False, "it loaded")
    except policy_mod.PolicyError as e:
        check("a deny file whose entry would be dead fails closed", True)
        check("and the error names the file", "deny.txt" in str(e), str(e)[:120])

    print("\n== the apex is in scope only when it is listed ==")
    pol_apex = _policy(allow={"example.com"})
    pol_apex.allow_hostnames = {"example.com"}
    with patch("socket.getaddrinfo", _resolver({})):
        r = scope_check.validate_target("example.com", pol_apex)
        check("listing the apex alongside the wildcard admits it", r.allowed is True, r.reason)

    print("\n== deny beats a wildcard allow, every way of writing it ==")
    with patch("socket.getaddrinfo", _resolver({"dc.example.com": "10.0.0.10"})):
        # a denied host under an allowed wildcard
        p1 = _policy(allow={"example.com"})
        p1.deny_hostnames = {"dc.example.com"}
        r = scope_check.validate_target("dc.example.com", p1)
        check("an exact deny hostname beats the wildcard", r.allowed is False, r.reason)
        check("and is attributed to the deny list", "deny" in r.policy_rule, r.policy_rule)

        # a denied wildcard under an allowed wildcard
        p2 = _policy(allow={"example.com"}, deny={"internal.example.com"})
        r = scope_check.validate_target("dc.internal.example.com", p2)
        check("a denied sub-wildcard beats the broader allow", r.allowed is False, r.reason)
        check("the rule names the denied domain",
              r.policy_rule == "deny.txt:*.internal.example.com", r.policy_rule)
        r = scope_check.validate_target("www.example.com", p2)
        check("a sibling outside the denied domain is still allowed", r.allowed is True, r.reason)

        # a wildcard allow cannot admit a host that resolves into denied space
        p3 = _policy(allow={"example.com"}, deny_nets=["10.0.0.0/8"])
        r = scope_check.validate_target("dc.example.com", p3)
        check("a wildcard allow cannot admit a host resolving into a denied network",
              r.allowed is False, f"{r.allowed} ({r.policy_rule})")
        check("and that is attributed to the network deny rule",
              r.policy_rule.startswith("deny.txt:10."), r.policy_rule)

    print("\n== cloud metadata stays denied under a wildcard ==")
    with patch("socket.getaddrinfo", _resolver({"meta.example.com": "169.254.169.254"})):
        p4 = _policy(allow={"example.com"}, deny_nets=["169.254.169.254/32"])
        r = scope_check.validate_target("meta.example.com", p4)
        check("a wildcard-covered name pointing at IMDS is denied", r.allowed is False, r.reason)

    print("\n== the longest matching entry is the one reported ==")
    with patch("socket.getaddrinfo", _resolver({})):
        p5 = _policy(allow={"example.com", "sub.example.com"})
        r = scope_check.validate_target("x.sub.example.com", p5)
        check("the specific wildcard is named, not whichever came first in the set",
              r.policy_rule == "scope.txt:*.sub.example.com", r.policy_rule)

    print("\n== a wildcard still pins an IP, so a rebind cannot follow the check ==")
    seq = ["203.0.113.5", "10.1.2.3"]

    def rebinding(host, *a, **k):
        ip = seq.pop(0) if seq else "10.1.2.3"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]

    with patch("socket.getaddrinfo", rebinding):
        p6 = _policy(allow={"example.com"})
        r = scope_check.validate_target("rebind.example.com", p6)
        check("the wildcard path returns a concrete IP to connect to",
              r.allowed is True and r.validated_ip == "203.0.113.5",
              f"{r.allowed} {r.validated_ip}")

    print("\n== a scope FILE with a wildcard loads, and one with a bad entry fails closed ==")
    tmp = Path(tempfile.mkdtemp(prefix="wc-scope-"))
    (tmp / "roe.json").write_text(
        '{"engagement_id": "wc", "allowed_action_classes": ["active_web_request"],'
        ' "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-01-01T00:00:00Z"}'
    )
    (tmp / "scope.txt").write_text("# program scope\n*.example.com\nexample.com\n10.0.0.0/24\n")
    (tmp / "deny.txt").write_text("*.internal.example.com\n169.254.169.254/32\n")
    loaded = policy_mod.load_policy(tmp)
    check("the wildcard reaches the loaded policy",
          loaded.allow_suffixes == {"example.com"}, str(loaded.allow_suffixes))
    check("the apex is loaded as its own hostname entry",
          "example.com" in loaded.allow_hostnames, str(loaded.allow_hostnames))
    check("the deny wildcard reaches the loaded policy",
          loaded.deny_suffixes == {"internal.example.com"}, str(loaded.deny_suffixes))
    check("the comment is not treated as a target",
          not any("#" in h for h in loaded.allow_hostnames), str(loaded.allow_hostnames))

    (tmp / "scope.txt").write_text("*.com\n")
    try:
        policy_mod.load_policy(tmp)
        check("a scope file containing a refused entry does not load", False, "it loaded")
    except policy_mod.PolicyError as e:
        check("a scope file containing a refused entry does not load", True)
        check("and the error names the offending file and entry",
              "scope.txt" in str(e) and "*.com" in str(e), str(e)[:140])

    print("\n== the web runtime reads the same wildcards through the same parser ==")
    from ..web import scope as web_scope

    class _Eng:
        engagement_id = "wc-web"
        allow_targets = ["*.example.com", "10.0.0.0/24"]
        deny_targets = ["*.internal.example.com", "dc.example.com"]
        allowed_action_classes = ["active_web_request"]
        valid_until = ""

    wp = web_scope.policy_from_engagement(_Eng())
    check("the web policy carries the allow wildcard",
          wp.allow_suffixes == {"example.com"}, str(wp.allow_suffixes))
    check("the web policy carries the deny wildcard",
          wp.deny_suffixes == {"internal.example.com"}, str(wp.deny_suffixes))
    check("and still carries the base metadata denies",
          all(ipaddress.ip_network(n) in wp.deny_networks
              for n in web_scope.BASE_DENY_NETWORKS), str(wp.deny_networks))

    with patch("socket.getaddrinfo", _resolver({"dc.example.com": "203.0.113.9"})):
        d = web_scope.check_url("https://shop.example.com/login", _Eng())
        check("a URL under the wildcard is allowed by the web check", d.allowed is True, d.reason)
        d = web_scope.check_url("https://evilexample.com/", _Eng())
        check("the lookalike domain is refused by the web check", d.allowed is False, d.reason)
        d = web_scope.check_url("https://dc.internal.example.com/", _Eng())
        check("the denied sub-wildcard is refused by the web check", d.allowed is False, d.reason)
        d = web_scope.check_url("https://example.com/", _Eng())
        check("the apex is refused when only the wildcard is listed",
              d.allowed is False, d.reason)

    print("\n== a refused entry in a live engagement does not disable the good ones ==")
    class _Mixed(_Eng):
        allow_targets = ["*.com", "*.example.com"]   # first entry is refused
    with patch("socket.getaddrinfo", _resolver({})):
        d = web_scope.check_url("https://shop.example.com/", _Mixed())
        check("the valid wildcard still enforces", d.allowed is True, d.reason)
        d = web_scope.check_url("https://shop.other.com/", _Mixed())
        check("the refused '*.com' authorises nothing", d.allowed is False, d.reason)

    print("\n== intake accepts what the broker will honour, and nothing else ==")
    from ..engagement.intake import _validate_scope_line

    for line, should_pass in [("*.example.com", True), ("example.com", True),
                              ("localhost", True), ("10.0.0.0/8", True),
                              ("*.com", False), ("a.*.b.com", False), ("", False),
                              ("# comment", False)]:
        errs: list = []
        _validate_scope_line(line, errs, "allow_targets")
        check(f"intake {'accepts' if should_pass else 'rejects'} {line!r}",
              (not errs) is should_pass, str(errs)[:110])

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
