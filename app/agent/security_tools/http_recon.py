"""Passive HTTP metadata collection — Phase 3's first narrow tool, HTTPS completed in M4.6.

"Passive" means non-exploitative recon (headers, status, redirect chain, a capped excerpt of
the body) — not zero-packets-sent, it does make bounded HTTP requests. Every hop of a redirect
chain is independently scope-validated before being followed (never auto-follow an off-scope
redirect), and every connection dials the broker-validated IP directly rather than letting the
HTTP stack re-resolve the hostname (DNS-rebinding prevention, APTS-SE-012) — Host header and
TLS SNI still carry the original hostname so virtual-hosted/TLS targets behave correctly.

M4.6: certificate verification defaults on, `verify_cert=False` is a distinct, harder-gated
action class (`http_recon_insecure` — denied unless the RoE explicitly allows it, on top of the
existing approval gate), per-engagement custom CA bundle support, and every certificate
verification error is recorded on the hop record regardless of outcome (not just on failure) so
the audit trail shows what was actually checked. Proxy environment variables are never
consulted — `_PinnedConnection.connect()` dials the socket directly, bypassing `http.client`'s
normal (proxy-env-aware) connection path entirely, so there's no proxy-env leak to scrub in the
first place; verified by a dedicated test in test_http_recon_https.py.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from urllib.parse import urlparse, urljoin

from .. import config
from ..broker import scope_check
from ..broker.policy import Policy

SCHEMA = {
    "type": "function",
    "function": {
        "name": "http_recon",
        "description": (
            "Passively collect HTTP metadata (status, headers, redirect chain, a capped body "
            "excerpt) from a single in-scope URL. Read-only — issues GET requests only, does "
            "not attempt exploitation. Every redirect hop is independently scope-checked "
            "before being followed. verify_cert defaults to True; setting it False requires "
            "both an RoE action-class allowance and human approval, it is not a normal option."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "e.g. http://127.0.0.1:3000/"},
                "verify_cert": {
                    "type": "boolean",
                    "description": "Default true. False disables TLS certificate verification "
                    "— gated separately, do not set unless explicitly instructed to.",
                },
            },
            "required": ["url"],
        },
    },
}


class _PinnedConnection(http.client.HTTPConnection):
    """Dials `pinned_ip` for the TCP connection but uses `host` (the original hostname) for
    the Host header and TLS SNI — the mechanism DNS-rebinding prevention actually needs.
    Never touches proxy environment variables (HTTP_PROXY/HTTPS_PROXY/NO_PROXY): connect() below
    calls socket.create_connection() directly rather than going through http.client's normal
    (proxy-aware) connection setup, so there is nothing to scrub — there was never a path in."""

    def __init__(
        self,
        pinned_ip: str,
        host: str,
        port: int,
        use_tls: bool,
        timeout: float,
        verify_cert: bool = True,
        ca_bundle_path: str | None = None,
    ):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip
        self._use_tls = use_tls
        self._verify_cert = verify_cert
        self._ca_bundle_path = ca_bundle_path
        self.tls_verification_error: str | None = None
        self.tls_cert_verified: bool | None = None  # None when not TLS at all

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), timeout=self.timeout)
        if not self._use_tls:
            self.sock = sock
            return

        if self._verify_cert:
            ctx = ssl.create_default_context(cafile=self._ca_bundle_path)
        else:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        try:
            self.sock = ctx.wrap_socket(sock, server_hostname=self.host)
            self.tls_cert_verified = self._verify_cert  # reached handshake success under this mode
        except ssl.SSLCertVerificationError as e:
            self.tls_verification_error = str(e)
            self.tls_cert_verified = False
            sock.close()
            raise


def _one_hop(
    url: str,
    policy: Policy,
    timeout: float,
    verify_cert: bool = True,
    ca_bundle_path: str | None = None,
) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"no host in URL: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    result = scope_check.validate_target(host, policy)
    if not result.allowed:
        raise PermissionError(f"{host!r} not in scope for hop {url!r}: {result.reason}")

    conn = _PinnedConnection(
        result.validated_ip, host, port, parsed.scheme == "https", timeout,
        verify_cert=verify_cert, ca_bundle_path=ca_bundle_path,
    )
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host)
        conn.putheader("User-Agent", "localai-phase3-http-recon/1.0")
        conn.putheader("Connection", "close")
        conn.endheaders()
        resp = conn.getresponse()
        # http.client never auto-decompresses; a gzip/deflate-bomb response still only yields
        # the raw (compressed) bytes actually read here, capped exactly like any other body —
        # there is no expansion step in this code path for a bomb to exploit.
        body = resp.read(config.HTTP_RECON_MAX_BODY_BYTES)
        headers = dict(resp.getheaders())
        location = headers.get("Location") or headers.get("location")
        return {
            "url": url,
            "validated_ip": result.validated_ip,
            "policy_rule": result.policy_rule,
            "status": resp.status,
            "reason": resp.reason,
            "headers": headers,
            "body_excerpt": body.decode("utf-8", errors="replace"),
            "body_truncated": len(body) >= config.HTTP_RECON_MAX_BODY_BYTES,
            "redirect_location": location,
            "tls": {
                "used": parsed.scheme == "https",
                "cert_verified": conn.tls_cert_verified,
                "verification_error": conn.tls_verification_error,
                "verify_cert_requested": verify_cert,
            },
        }
    except ssl.SSLCertVerificationError as e:
        # Recorded even on failure — "always log the verification error" per the doc, not just
        # on the success path.
        return {
            "url": url,
            "validated_ip": result.validated_ip,
            "policy_rule": result.policy_rule,
            "status": None,
            "reason": "tls_handshake_failed",
            "headers": {},
            "body_excerpt": "",
            "body_truncated": False,
            "redirect_location": None,
            "tls": {
                "used": True,
                "cert_verified": False,
                "verification_error": str(e),
                "verify_cert_requested": verify_cert,
            },
        }
    except OSError as e:
        # Connection refused/reset/unreachable/timed out, or any other socket-level failure —
        # an expected outcome for a recon tool probing targets that may not be listening, not a
        # crash. (SSLCertVerificationError is an OSError subclass but is caught above first, so
        # this branch is genuinely connection-level, not a masked cert failure.)
        return {
            "url": url,
            "validated_ip": result.validated_ip,
            "policy_rule": result.policy_rule,
            "status": None,
            "reason": f"connection_failed: {type(e).__name__}: {e}",
            "headers": {},
            "body_excerpt": "",
            "body_truncated": False,
            "redirect_location": None,
            "tls": {
                "used": parsed.scheme == "https",
                "cert_verified": None,
                "verification_error": None,
                "verify_cert_requested": verify_cert,
            },
        }
    finally:
        conn.close()


def run(
    url: str,
    policy: Policy,
    verify_cert: bool = True,
    ca_bundle_path: str | None = None,
) -> dict:
    hops = []
    current_url = url
    current_scheme = urlparse(url).scheme
    for _ in range(config.HTTP_RECON_MAX_REDIRECTS + 1):
        hop = _one_hop(current_url, policy, config.HTTP_RECON_TIMEOUT_S, verify_cert, ca_bundle_path)
        hops.append(hop)
        if hop["status"] is None:  # TLS handshake failed or connection failed — stop here
            if hop["tls"]["verification_error"] is not None:
                error_msg = f"TLS verification failed: {hop['tls']['verification_error']}"
            else:
                error_msg = hop["reason"]  # "connection_failed: ..." from the OSError branch
            return {
                "ok": False,
                "error": error_msg,
                "hops": hops,
                "_policy_rule": hop["policy_rule"],
                "_exit_metadata": {
                    "hop_count": len(hops),
                    "tls_failure": hop["tls"]["verification_error"] is not None,
                },
            }
        new_scheme = urlparse(current_url).scheme
        if new_scheme != current_scheme:
            hop["scheme_transition"] = f"{current_scheme}->{new_scheme}"
        current_scheme = new_scheme
        if not (300 <= hop["status"] < 400 and hop["redirect_location"]):
            break
        current_url = urljoin(current_url, hop["redirect_location"])
    else:
        return {"ok": False, "error": "too many redirects", "hops": hops}

    final = hops[-1]
    return {
        "ok": True,
        "final_url": final["url"],
        "final_status": final["status"],
        "final_headers": final["headers"],
        "body_excerpt": final["body_excerpt"],
        "body_truncated": final["body_truncated"],
        "redirect_chain": [h["url"] for h in hops[:-1]],
        "tls": final["tls"],
        "hops": hops,
        "_policy_rule": final["policy_rule"],
        "_exit_metadata": {"hop_count": len(hops)},
    }
