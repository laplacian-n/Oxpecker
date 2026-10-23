"""Active HTTP — send one arbitrary request (method, headers, body) to an in-scope target.

`http_recon` is read-only (GET, follows redirects, for mapping a surface). Real exploitation needs
the other verbs and a body: a POST login with `' OR 1=1--`, a PUT/PATCH that changes state, a JSON
body carrying an injection. Without this tool a worker could only ever touch GET endpoints — live
against Juice Shop a worker literally recorded "Blocked: no tool available to issue POST/PUT
requests", so the classic POST `/rest/user/login` SQLi was out of reach and only half the attack
surface was testable.

Same safety spine as http_recon, reused not reinvented: the host is scope-validated and the
connection pinned to the validated IP (`_PinnedConnection`, DNS-rebinding safe), the path is
checked against the engagement's path scope, and the body read is capped. It is gated by the broker
as the `active_web_request` action class (broker.TOOL_ACTION_CLASS), which an engagement must opt
into — listing the schema only makes the model aware the tool exists.

Deliberately does NOT follow redirects: a 3xx is a result the tester wants to see (a login that
302s to a dashboard is the signal that it worked), not a hop to chase. One request, one response.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .. import config
from ..broker import scope_check
from ..broker.policy import Policy
from .http_recon import _PinnedConnection

# The verbs a web test legitimately needs. CONNECT/TRACE are omitted on purpose (proxy/debug
# surfaces, not application testing). HEAD and OPTIONS are cheap and useful for probing.
ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD")

# Headers the tool manages itself; a model-supplied value for these is ignored so it cannot break
# framing (Content-Length), defeat the scope pin (Host), or hold the socket open (Connection).
_RESERVED_HEADERS = {"host", "content-length", "connection"}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "http_request",
        "description": (
            "Send ONE HTTP request (any method, custom headers, optional body) to an in-scope "
            "target and return its status, headers and a capped body excerpt. This is the active "
            "verb for exploitation and auth testing — a POST login with an injection payload, a "
            "JSON body, a PUT/PATCH that changes state. Use http_recon instead for read-only "
            "surface mapping. The host is scope-checked and the connection pinned; redirects are "
            "NOT followed (a 3xx is returned as-is). verify_cert defaults to true."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "e.g. http://127.0.0.1:3000/rest/user/login"},
                "method": {
                    "type": "string",
                    "enum": list(ALLOWED_METHODS),
                    "description": "HTTP method; defaults to GET.",
                },
                "headers": {
                    "type": "object",
                    "description": "Request headers as a flat object, e.g. "
                    '{"Content-Type": "application/json"}. Host/Content-Length/Connection are set '
                    "automatically and cannot be overridden.",
                },
                "body": {
                    "type": "string",
                    "description": "Raw request body (e.g. a JSON string or form-encoded data). "
                    "Omit for methods without a body.",
                },
                "verify_cert": {
                    "type": "boolean",
                    "description": "Default true. False disables TLS verification — gated "
                    "separately, do not set unless explicitly instructed to.",
                },
            },
            "required": ["url"],
        },
    },
}


def run(
    url: str,
    policy: Policy,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
    verify_cert: bool = True,
    ca_bundle_path: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Send one request. Returns ok=True with the response (any status, including 4xx/5xx — a 500 is
    a real result, not a failure) when the request completed, ok=False with an `error` when the host
    is out of scope, the path is excluded, or the connection/TLS failed."""
    method = str(method or "GET").upper()
    if method not in ALLOWED_METHODS:
        return {"ok": False, "error": f"method {method!r} not allowed; choose from {list(ALLOWED_METHODS)}"}

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": f"unsupported scheme: {parsed.scheme!r}"}
    host = parsed.hostname
    if not host:
        return {"ok": False, "error": f"no host in URL: {url!r}"}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    target = scope_check.validate_target(host, policy)
    if not target.allowed:
        return {"ok": False, "error": f"{host!r} not in scope: {target.reason}",
                "_policy_rule": target.policy_rule}
    path_check = scope_check.validate_path(parsed.path or "/", policy)
    if not path_check.allowed:
        return {"ok": False, "error": f"path not in scope: {path_check.reason}",
                "_policy_rule": path_check.policy_rule}

    body_bytes = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    conn = _PinnedConnection(
        target.validated_ip, host, port, parsed.scheme == "https",
        timeout if timeout is not None else config.HTTP_RECON_TIMEOUT_S,
        verify_cert=verify_cert, ca_bundle_path=ca_bundle_path,
    )
    try:
        conn.putrequest(method, path, skip_host=True)
        default_port = 443 if parsed.scheme == "https" else 80
        conn.putheader("Host", host if port == default_port else f"{host}:{port}")
        conn.putheader("User-Agent", "localai-phase3-http-request/1.0")
        for k, v in (headers or {}).items():
            if str(k).strip().lower() in _RESERVED_HEADERS:
                continue
            conn.putheader(str(k), str(v))
        if body_bytes:
            conn.putheader("Content-Length", str(len(body_bytes)))
        conn.putheader("Connection", "close")
        conn.endheaders()
        if body_bytes:
            conn.send(body_bytes)
        resp = conn.getresponse()
        raw = resp.read(config.HTTP_RECON_MAX_BODY_BYTES)
        resp_headers = dict(resp.getheaders())
        return {
            "ok": True,
            "url": url,
            "method": method,
            "validated_ip": target.validated_ip,
            "status": resp.status,
            "reason": resp.reason,
            "headers": resp_headers,
            "redirect_location": resp_headers.get("Location") or resp_headers.get("location"),
            "body_excerpt": raw.decode("utf-8", errors="replace"),
            "body_truncated": len(raw) >= config.HTTP_RECON_MAX_BODY_BYTES,
            "_policy_rule": target.policy_rule,
            "tls": {
                "used": parsed.scheme == "https",
                "cert_verified": conn.tls_cert_verified,
                "verification_error": conn.tls_verification_error,
                "verify_cert_requested": verify_cert,
            },
        }
    except Exception as e:  # noqa: BLE001 — TLS/connection/socket failure is an expected probe outcome
        return {"ok": False, "url": url, "method": method,
                "error": f"request_failed: {type(e).__name__}: {e}",
                "_policy_rule": target.policy_rule}
    finally:
        conn.close()
