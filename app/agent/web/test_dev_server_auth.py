"""Tests that the dev server's /api surface is actually gated.

Why this file exists: until the change it tests, `dev_server.py` had no authentication of any
kind — a grep for WEB_UI_API_KEY / require_api_key / Depends / api_key / Authorization across
the whole file returned zero hits — while CORS was `allow_origins=["*"]`. That was survivable
only because the bind defaulted to 127.0.0.1 and Electron spawned the process locally, so the
OS was the boundary. The orphaned agent/web/server.py, the runtime nothing ships, is the one
that had `require_api_key`. Moving the server to its own host is what made the gap matter.

Two things here are easy to get wrong and are each pinned below:

  1. No key file must still mean no auth. Every other test in this package sends no credentials;
     if a missing key file started rejecting requests, the suite would go red for the wrong
     reason and the loopback install would break.
  2. An unusable key file (empty, unreadable, not UTF-8) must fail CLOSED. "No key found" and
     "key could not be read" look alike one line apart and only one of them is safe.

(Every route is HTTP — the SSE streams included — so the one http middleware covers them all;
there is no WebSocket route that could slip past it.)

Needs fastapi/pydantic. Run directly: `python3 -m agent.web.test_dev_server_auth`.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}  {detail if not condition else ''}")


def main() -> int:
    from fastapi.testclient import TestClient

    from . import dev_server as d

    client = TestClient(d.app)
    tmp = Path(tempfile.mkdtemp(prefix="oxp-auth-"))

    KEY = "s3cret-key-value"
    key_file = tmp / "web_ui_api_key.txt"
    key_file.write_text(KEY, encoding="utf-8")

    missing = tmp / "no-such-key-file.txt"
    empty = tmp / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    not_utf8 = tmp / "binary.txt"
    not_utf8.write_bytes(b"\xff\xfe\x00rubbish")

    def with_key(path):
        return patch("agent.config.WEB_UI_API_KEY_FILE", path)

    print("\n== no key file: unauthenticated, exactly as before this change ==")
    with with_key(missing):
        r = client.get("/api/health")
        check("/api/health answers with no credentials", r.status_code == 200, str(r.status_code))
        check("_web_ui_key reports (None, not-unreadable)", d._web_ui_key() == (None, False),
              str(d._web_ui_key()))

    print("\n== key file present: /api/* requires it ==")
    with with_key(key_file):
        check("no credentials -> 401", client.get("/api/health").status_code == 401)
        check("wrong key -> 401",
              client.get("/api/health", headers={"X-API-Key": "nope"}).status_code == 401)
        check("X-API-Key (what static/index.html has always sent) -> 200",
              client.get("/api/health", headers={"X-API-Key": KEY}).status_code == 200)
        check("Authorization: Bearer (the convention, and what server.py reads) -> 200",
              client.get("/api/health", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200)
        check("?key= (EventSource cannot set headers) -> 200",
              client.get(f"/api/health?key={KEY}").status_code == 200)
        check("a wrong header does not mask a correct ?key= (every candidate is compared)",
              client.get(f"/api/health?key={KEY}", headers={"X-API-Key": "nope"}).status_code == 200)
        check("a POST route is gated too, not just the GET we probed",
              client.post("/api/sessions", json={}).status_code == 401)

    print("\n== the page shell and the Electron probe stay open ==")
    with with_key(key_file):
        check("/health (Electron's startup probe alias) is not gated",
              client.get("/health").status_code == 200)
        check("/api/health, its gated twin, still is",
              client.get("/api/health").status_code == 401)

    print("\n== an unusable key file fails closed, it does not read as 'auth off' ==")
    for label, path in (("empty", empty), ("not UTF-8", not_utf8)):
        with with_key(path):
            key, unreadable = d._web_ui_key()
            check(f"{label} key file -> unreadable, key None", (key, unreadable) == (None, True),
                  str((key, unreadable)))
            check(f"{label} key file -> 503, never 200",
                  client.get("/api/health").status_code == 503,
                  str(client.get("/api/health").status_code))

    print("\n== the SSE stream is gated like any other /api route ==")
    with with_key(key_file):
        # EventSource authenticates with ?key= (it cannot set a header); the http middleware gates
        # it like every other /api route. A disconnect test needs a real client, so here we only
        # pin the auth boundary: no key is refused, the right key is let through to the handler.
        check("unauthenticated SSE connect -> 401",
              client.get("/api/sessions/whatever/events").status_code == 401)
        check("SSE with ?key= for a nonexistent session gets past auth to 404 (not 401)",
              client.get(f"/api/sessions/definitely-not-a-session/events?key={KEY}").status_code == 404)

    print("\n== CORS is no longer a wildcard ==")
    # allow_origins=["*"] with allow_credentials=True is a combination browsers reject, so the
    # old setting was not even buying the cross-origin access it appeared to grant.
    mw = [m for m in d.app.user_middleware if "CORS" in repr(m)]
    check("no CORS middleware is installed when AGENT_WEB_CORS_ORIGINS is unset",
          mw == [], repr(mw))

    print("\n== _is_loopback: anything unparseable is NOT given the benefit of the doubt ==")
    for host, expected in (
        ("127.0.0.1", True), ("::1", True), ("localhost", True), ("127.0.1.1", True),
        ("0.0.0.0", False), ("192.168.1.10", False), ("", False), ("*", False),
        ("example.com", False),
    ):
        check(f"_is_loopback({host!r}) is {expected}", d._is_loopback(host) is expected,
              str(d._is_loopback(host)))

    print("\n== main() refuses to put an unauthenticated server on the network ==")
    import argparse

    class Args:
        def __init__(self, host, insecure=False):
            self.host, self.port, self.insecure_no_auth = host, 7777, insecure

    def guard(host, insecure=False):
        p = argparse.ArgumentParser()
        try:
            d._check_bind_is_safe(p, Args(host, insecure))
            return "allowed"
        except SystemExit:
            return "refused"

    with with_key(missing):
        check("no key + loopback bind is allowed (the normal install)",
              guard("127.0.0.1") == "allowed")
        check("no key + 0.0.0.0 is REFUSED", guard("0.0.0.0") == "refused")
        check("no key + a LAN address is REFUSED", guard("192.168.1.10") == "refused")
        check("--insecure-no-auth is the typed escape hatch",
              guard("0.0.0.0", insecure=True) == "allowed")
    with with_key(key_file):
        check("a key makes a non-loopback bind fine", guard("0.0.0.0") == "allowed")
    with with_key(empty):
        check("an unusable key file refuses startup rather than running wide open",
              guard("127.0.0.1") == "refused")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0



def load_tests(loader, tests, pattern):  # noqa: ARG001 - stdlib protocol
    """Let `unittest discover` run this file's checks. Without it, discovery imports this module,
    finds no TestCase, collects nothing, and reports success — see agent/script_suite.py."""
    from ..script_suite import suite_for

    return suite_for(__name__, min_checks=30)

if __name__ == "__main__":
    raise SystemExit(main())
