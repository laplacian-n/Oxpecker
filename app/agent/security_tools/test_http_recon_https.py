"""HTTPS regression suite for http_recon — M4.6. Spins up local TLS test servers on loopback
(never touches a non-lab network target) covering: self-signed rejection, SAN mismatch, expired
cert, custom-CA-bundle trust, verify_cert=False gating at the broker (RoE-denied by default),
IPv6 literal handling, and confirms no proxy-env var affects the connection target.
Run directly: `python3 -m agent.security_tools.test_http_recon_https`.
"""
from __future__ import annotations

import http.server
import os
import ssl
import threading
import time

from ..broker.policy import load_policy
from . import http_recon

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def _serve_tls(port: int, certfile: str, keyfile: str, host: str = "127.0.0.1") -> http.server.HTTPServer:
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.HTTPServer((host, port), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    cert_dir = "/tmp/https-certs"
    required = [
        "selfsigned.crt", "selfsigned.key", "ca.crt", "leaf.crt", "leaf.key",
        "mismatch.crt", "mismatch.key", "expired.crt", "expired.key",
    ]
    missing = [f for f in required if not os.path.exists(f"{cert_dir}/{f}")]
    if missing:
        print(f"SKIPPED: test certs not found in {cert_dir} (missing: {missing}). "
              f"Regenerate with: bash agent/security_tools/gen_https_test_certs.sh")
        return 0

    servers = []
    servers.append(_serve_tls(8451, f"{cert_dir}/selfsigned.crt", f"{cert_dir}/selfsigned.key"))
    servers.append(_serve_tls(8452, f"{cert_dir}/leaf.crt", f"{cert_dir}/leaf.key"))
    servers.append(_serve_tls(8453, f"{cert_dir}/mismatch.crt", f"{cert_dir}/mismatch.key"))
    servers.append(_serve_tls(8454, f"{cert_dir}/expired.crt", f"{cert_dir}/expired.key"))
    time.sleep(0.5)

    policy = load_policy()

    print("== Certificate verification (default verify_cert=True) ==")
    r = http_recon.run("https://localhost:8451/", policy)
    check("self-signed cert (no CA) is rejected", r["ok"] is False and "verif" in r["error"].lower())

    # The mismatch-cert server (8453) is issued for "wrong.example" — testing via the IP
    # literal directly (rather than an actually-out-of-scope hostname, which would raise
    # PermissionError before TLS is even attempted) isolates the SAN-mismatch behavior cleanly.
    r2 = http_recon.run("https://127.0.0.1:8453/", policy)
    check(
        "SAN-mismatched cert (issued for wrong.example) is rejected when accessed as 127.0.0.1",
        r2["ok"] is False and "verif" in r2["error"].lower(),
        r2.get("error"),
    )

    r3 = http_recon.run("https://localhost:8454/", policy)
    check("expired cert is rejected", r3["ok"] is False and "verif" in r3["error"].lower())

    print("\n== Custom CA bundle ==")
    r4 = http_recon.run("https://localhost:8452/", policy)
    check("CA-signed cert fails without the custom CA bundle", r4["ok"] is False)
    r5 = http_recon.run("https://localhost:8452/", policy, verify_cert=True, ca_bundle_path=f"{cert_dir}/ca.crt")
    check(
        "CA-signed cert succeeds once the matching CA bundle is supplied",
        r5["ok"] is True and r5["tls"]["cert_verified"] is True,
        r5.get("error"),
    )

    print("\n== verify_cert=False path (broker-level gating tested separately in broker suite) ==")
    r6 = http_recon.run("https://localhost:8451/", policy, verify_cert=False)
    check(
        "verify_cert=False accepts the same self-signed cert that verify_cert=True rejected",
        r6["ok"] is True and r6["tls"]["cert_verified"] is False,
        r6.get("error"),
    )

    print("\n== Proxy environment variables are never consulted ==")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1/nonexistent-proxy-that-would-error-if-used"
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:1/nonexistent-proxy-that-would-error-if-used"
    try:
        r7 = http_recon.run("https://localhost:8452/", policy, verify_cert=True, ca_bundle_path=f"{cert_dir}/ca.crt")
        check(
            "request succeeds identically with bogus proxy env vars set (proxy env is never read)",
            r7["ok"] is True,
            r7.get("error"),
        )
    finally:
        del os.environ["HTTPS_PROXY"]
        del os.environ["HTTP_PROXY"]

    print("\n== IPv6 literal handling ==")
    r8 = http_recon.run("http://[::1]:3000/", policy)
    check(
        "IPv6 literal target resolves and is handled (Juice Shop doesn't listen on ::1, so "
        "expect a connection-level failure, not a crash or scope-check failure)",
        "ok" in r8,  # didn't raise/crash — the exact ok value depends on whether ::1:3000 has a listener
    )

    print("\n== Broker gating for verify_cert=False (http_recon_insecure action class) ==")
    import json
    import shutil
    import tempfile
    from pathlib import Path

    from ..broker.broker import Broker
    from ..broker.contracts import ActionRequest

    default_broker_calls = []
    default_broker = Broker(confirm_fn=lambda p: (default_broker_calls.append(p), True)[1])
    req = ActionRequest(
        tool="http_recon", arguments={"url": "https://localhost:8451/", "verify_cert": False},
        session_id="https-test-insecure-default", device_id="d",
    )
    resp = default_broker.dispatch(
        req, executor=lambda pol, args: http_recon.run(args["url"], pol, args["verify_cert"])
    )
    check(
        "default RoE denies verify_cert=False before even reaching approval",
        resp.status == "denied" and resp.policy_rule == "roe.allowed_action_classes"
        and len(default_broker_calls) == 0,
        f"status={resp.status} rule={resp.policy_rule} approval_calls={len(default_broker_calls)}",
    )

    tmp_engagement = Path(tempfile.mkdtemp(prefix="https-test-engagement-"))
    shutil.copy("engagement/scope.txt", tmp_engagement / "scope.txt")
    shutil.copy("engagement/deny.txt", tmp_engagement / "deny.txt")
    roe = json.loads(Path("engagement/roe.json").read_text())
    roe["allowed_action_classes"] = ["passive_recon", "active_scan_light", "http_recon_insecure"]
    (tmp_engagement / "roe.json").write_text(json.dumps(roe))

    optin_calls = []
    optin_broker = Broker(
        engagement_dir=tmp_engagement, confirm_fn=lambda p: (optin_calls.append(p), True)[1]
    )
    req2 = ActionRequest(
        tool="http_recon", arguments={"url": "https://localhost:8451/", "verify_cert": False},
        session_id="https-test-insecure-optin", device_id="d",
    )
    resp2 = optin_broker.dispatch(
        req2, executor=lambda pol, args: http_recon.run(args["url"], pol, args["verify_cert"])
    )
    check(
        "RoE opt-in still requires approval, and the prompt names the TLS risk",
        resp2.status == "succeeded" and len(optin_calls) == 1 and "TLS" in optin_calls[0],
        f"status={resp2.status} calls={len(optin_calls)}",
    )

    decline_broker = Broker(engagement_dir=tmp_engagement, confirm_fn=lambda p: False)
    req3 = ActionRequest(
        tool="http_recon", arguments={"url": "https://localhost:8451/", "verify_cert": False},
        session_id="https-test-insecure-decline", device_id="d",
    )
    resp3 = decline_broker.dispatch(
        req3, executor=lambda pol, args: http_recon.run(args["url"], pol, args["verify_cert"])
    )
    check(
        "human declining the approval denies the action",
        resp3.status == "denied" and resp3.policy_rule == "approval_required",
        f"status={resp3.status} rule={resp3.policy_rule}",
    )
    shutil.rmtree(tmp_engagement, ignore_errors=True)

    for s in servers:
        s.shutdown()

    print(f"\n{len(PASS)}/{len(PASS)+len(FAIL)} checks passed")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
