"""Policy bypass suite — Phase-3 exit criterion (research doc §12/§13): IPv4, IPv6, DNS,
redirect, rebinding cases, plus stale/missing policy fails closed. Run directly, not via
pytest (no test framework in this project yet) — prints PASS/FAIL per case, exits nonzero on
any failure.
"""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from . import policy as policy_mod
from . import scope_check
from .broker import Broker
from .contracts import ActionRequest
from .. import config

PASS = []
FAIL = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def main() -> int:
    policy = policy_mod.load_policy()

    print("\n== IPv4 cases ==")
    r = scope_check.validate_target("127.0.0.1", policy)
    check("ipv4 loopback in scope is allowed", r.allowed, r.reason)
    r = scope_check.validate_target("8.8.8.8", policy)
    check("ipv4 public DNS (out of scope) is denied", not r.allowed, r.reason)
    r = scope_check.validate_target("10.0.0.1", policy)
    check("ipv4 private-but-not-listed is denied (default-deny)", not r.allowed, r.reason)

    print("\n== IPv6 cases ==")
    r = scope_check.validate_target("::1", policy)
    check("ipv6 loopback in scope is allowed", r.allowed, r.reason)
    r = scope_check.validate_target("2001:4860:4860::8888", policy)
    check("ipv6 public DNS (out of scope) is denied", not r.allowed, r.reason)

    print("\n== DNS cases ==")
    r = scope_check.validate_target("localhost", policy)
    check("hostname 'localhost' (explicit scope entry) is allowed", r.allowed, r.reason)
    # A hostname that resolves to a real public IP not on the allowlist must be denied.
    try:
        r = scope_check.validate_target("example.com", policy)
        check("hostname resolving off-scope is denied", not r.allowed, r.reason)
    except Exception as e:
        check("hostname resolving off-scope is denied", False, f"exception instead: {e}")

    print("\n== Deny-first (deny always wins, even over an allowlist match) ==")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copy(config.ROE_PATH, tmp_path / "roe.json")
        # scope.txt allows the metadata-service IP; deny.txt (immutable) must still win.
        (tmp_path / "scope.txt").write_text("127.0.0.1/32\n169.254.169.254/32\n")
        shutil.copy(config.DENY_PATH, tmp_path / "deny.txt")
        conflicting_policy = policy_mod.load_policy(tmp_path)
        r = scope_check.validate_target("169.254.169.254", conflicting_policy)
        check(
            "deny.txt overrides a conflicting scope.txt allow entry",
            not r.allowed and r.policy_rule.startswith("deny.txt"),
            r.reason,
        )

    print("\n== Redirect validation (never auto-follow an off-scope redirect) ==")
    from ..security_tools import http_recon

    # Exercise the real per-hop scope check directly on a redirect target — mocking _one_hop
    # itself would bypass the exact validate_target() call this test needs to prove runs.
    try:
        http_recon._one_hop("http://evil.example.com/steal", policy, config.HTTP_RECON_TIMEOUT_S)
        check("off-scope redirect target raises PermissionError", False, "no exception raised")
    except PermissionError:
        check("off-scope redirect target raises PermissionError", True)
    except Exception as e:
        check("off-scope redirect target raises PermissionError", False, f"wrong exception: {e}")

    # And confirm run()'s loop actually calls validate_target per hop (not just on the first
    # URL) by making hop 1 a real in-scope response that redirects off-scope, and checking
    # run() surfaces the resulting PermissionError rather than silently stopping.
    with patch.object(http_recon, "_one_hop") as mock_hop:
        def side_effect(url, pol, timeout, verify_cert=True, ca_bundle_path=None):
            if url == "http://127.0.0.1:3000/":
                return {
                    "url": url, "validated_ip": "127.0.0.1", "policy_rule": "scope.txt:127.0.0.1/32",
                    "status": 302, "reason": "Found",
                    "headers": {"Location": "http://evil.example.com/steal"},
                    "body_excerpt": "", "body_truncated": False,
                    "redirect_location": "http://evil.example.com/steal",
                }
            # run()'s second iteration should call _one_hop on the *redirect target* — this
            # is what a real (unmocked) _one_hop would raise for an off-scope host.
            raise PermissionError(f"{url!r} not in scope")

        mock_hop.side_effect = side_effect
        try:
            http_recon.run("http://127.0.0.1:3000/", policy)
            check("run() propagates a redirect-hop scope denial", False, "no exception raised")
        except PermissionError:
            check("run() propagates a redirect-hop scope denial", True)

    print("\n== DNS-rebinding prevention (structural: one resolve, fixed IP reused) ==")
    call_count = {"n": 0}
    real_getaddrinfo = socket.getaddrinfo

    def rebinding_getaddrinfo(host, *args, **kwargs):
        call_count["n"] += 1
        if host == "rebind-test.invalid":
            ip = "127.0.0.1" if call_count["n"] == 1 else "10.6.6.6"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    rebind_policy = policy_mod.Policy(
        engagement_id="test",
        allowed_action_classes={"active_scan_light"},
        allow_networks=policy.allow_networks,
        allow_hostnames=policy.allow_hostnames | {"rebind-test.invalid"},
        deny_networks=policy.deny_networks,
        deny_hostnames=policy.deny_hostnames,
        policy_version="test",
        valid_until=time.time() + 3600,
    )
    with patch("socket.getaddrinfo", side_effect=rebinding_getaddrinfo):
        r1 = scope_check.validate_target("rebind-test.invalid", rebind_policy)
        r2 = scope_check.validate_target("rebind-test.invalid", rebind_policy)
    check(
        "each validate_target() call resolves once and returns a fixed IP for that call",
        r1.validated_ip == "127.0.0.1" and call_count["n"] == 2,
        f"r1={r1.validated_ip} calls={call_count['n']}",
    )
    from ..security_tools.port_discovery import _probe  # noqa: F401 - imported to confirm no re-resolve in probe

    check(
        "_probe() connects by IP only, never re-resolves a hostname",
        "socket.getaddrinfo" not in open(
            Path(__file__).parent.parent / "security_tools" / "port_discovery.py"
        ).read().replace("scope_check", ""),
        "port_discovery._probe still references DNS resolution",
    )

    print("\n== Stale/missing policy fails closed ==")
    with tempfile.TemporaryDirectory() as tmp:
        empty_dir = Path(tmp)
        try:
            policy_mod.load_policy(empty_dir)
            check("missing policy files raise PolicyError", False, "no exception")
        except policy_mod.PolicyError:
            check("missing policy files raise PolicyError", True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        expired_roe = json.loads(config.ROE_PATH.read_text())
        expired_roe["valid_until"] = "2020-01-01T00:00:00Z"
        (tmp_path / "roe.json").write_text(json.dumps(expired_roe))
        shutil.copy(config.SCOPE_PATH, tmp_path / "scope.txt")
        shutil.copy(config.DENY_PATH, tmp_path / "deny.txt")
        try:
            policy_mod.load_policy(tmp_path)
            check("expired RoE raises PolicyError", False, "no exception")
        except policy_mod.PolicyError:
            check("expired RoE raises PolicyError", True)

    broker = Broker(engagement_dir=Path(tempfile.mkdtemp()), confirm_fn=lambda p: True)
    req = ActionRequest(
        tool="port_discovery",
        arguments={"host": "127.0.0.1", "ports": [80]},
        session_id="s", device_id="d",
    )
    resp = broker.dispatch(req, executor=lambda pol, args: {"ok": True})
    check(
        "broker.dispatch denies (fails closed) when policy dir is empty",
        resp.status == "denied" and resp.policy_rule == "policy_error",
        f"status={resp.status} rule={resp.policy_rule}",
    )

    print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
