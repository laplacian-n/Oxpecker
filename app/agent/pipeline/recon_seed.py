"""Deterministic RECON seeding — turn an http_recon result into child hypotheses in code.

RECON's job is to convert a scope root into the testable child hypotheses the later phases work
through. Leaving that to the model alone proved unreliable: a capable model (DeepSeek V4) would
http_recon the target several times and then stop without ever calling the graph-write tool, so
the graph never grew past the root and the engagement closed out after one shallow pass. The
mechanical part of recon — which security headers are missing, which cookies lack flags, which
endpoints and forms the response exposes, what the server advertises about itself — is exactly
extractable from the response, so this module extracts it and records a hypothesis per finding
with no model in the loop. The model's recon pass still runs for anything deterministic rules miss;
this just guarantees a non-empty, testable graph so ANALYSIS always has something to investigate.

Everything here is passive: it reads an http_recon result (headers + a capped body excerpt) that
the broker already fetched and scope-checked. It sends no requests of its own.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

# (header, claim-if-missing, impact) for headers whose absence is a real, testable weakness.
_SECURITY_HEADERS = [
    ("content-security-policy",
     "the response sets no Content-Security-Policy, so an injected script is not mitigated", 3),
    ("strict-transport-security",
     "the response sets no Strict-Transport-Security header, allowing TLS downgrade/stripping", 3),
    ("x-frame-options",
     "the response sets neither X-Frame-Options nor a frame-ancestors CSP, so it may be clickjacked", 2),
    ("x-content-type-options",
     "the response omits X-Content-Type-Options: nosniff, allowing MIME sniffing", 2),
]
# Headers that disclose software/version — worth a hypothesis because a version is a CVE lookup.
_DISCLOSURE_HEADERS = ("server", "x-powered-by", "x-aspnet-version", "x-generator")

# A short list of high-signal paths worth flagging if they appear in the response surface.
_SENSITIVE_HINTS = ("admin", "login", "api", "upload", "debug", "config", "graphql", ".git")


def _norm_headers(headers) -> dict[str, str]:
    """Lower-cased header dict, tolerating a dict or a list of (k, v) pairs."""
    if isinstance(headers, dict):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    out: dict[str, str] = {}
    for item in headers or []:
        try:
            k, v = item
            out[str(k).lower()] = str(v)
        except (ValueError, TypeError):
            continue
    return out


def _links_from_body(body: str, base_url: str) -> list[str]:
    """In-scope-looking paths pulled from href/src/action attributes and obvious JS URL strings.

    Deliberately simple and same-origin: it keeps root-relative paths and absolute URLs on the
    base host, which is what a recon enumeration cares about. It is a seed, not a crawler."""
    if not body:
        return []
    base_host = urlparse(base_url).netloc
    found: list[str] = []
    for m in re.finditer(r"""(?:href|src|action)\s*=\s*["']([^"'#>\s]+)["']""", body, re.I):
        found.append(m.group(1))
    # URL-ish string literals in inline JS: "/api/...", 'https://host/...'
    for m in re.finditer(r"""["'](/[A-Za-z0-9_\-./]{1,120}|https?://[^"'\s]{1,200})["']""", body):
        found.append(m.group(1))
    seen: dict[str, None] = {}
    for raw in found:
        if raw.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        abs_url = urljoin(base_url, raw)
        p = urlparse(abs_url)
        if p.scheme not in ("http", "https"):
            continue
        if p.netloc and p.netloc != base_host:
            continue  # same-origin only — other hosts are out of this root's scope
        path = p.path or "/"
        if path not in seen:
            seen[path] = None
    seen.pop("/", None)  # the root itself is the parent, not a child endpoint
    return list(seen)


def _has_form(body: str) -> bool:
    return bool(body) and re.search(r"<form\b", body, re.I) is not None


def build_recon_hypotheses(recon: dict, base_url: str) -> list[dict]:
    """The deterministic findings from one http_recon result, as hypothesis kwargs (minus parent).

    Pure and side-effect-free so it is unit-testable without a graph: `seed_recon_hypotheses`
    turns each returned dict into a stored child hypothesis."""
    if not recon or not recon.get("ok"):
        return []
    headers = _norm_headers(recon.get("final_headers") or recon.get("headers"))
    body = recon.get("body_excerpt") or recon.get("body") or ""
    host = urlparse(base_url).netloc or base_url
    out: list[dict] = []

    def add(title, claim, rationale, impact, surface, band="medium"):
        out.append(dict(title=title, claim=claim, rationale=rationale, impact=impact,
                        confidence_band=band, confidence_reason="observed in the recon response",
                        phase_created="RECON", origin_type="tool_observation", surface=surface))

    for name, claim, impact in _SECURITY_HEADERS:
        if name not in headers:
            add(f"Missing {name} header", f"{host}: {claim}",
                f"the recon response for {base_url} did not include a {name} header",
                impact, f"{host} response headers")

    for name in _DISCLOSURE_HEADERS:
        if name in headers and headers[name].strip():
            add(f"Version disclosure via {name}",
                f"{host} discloses software/version in the {name} header "
                f"('{headers[name][:80]}'), which maps to known CVEs",
                f"the {name} header was present in the recon response",
                2, f"{host} {name} header", band="low")

    cookie = headers.get("set-cookie", "")
    if cookie:
        low = cookie.lower()
        flags = [f for f in ("secure", "httponly", "samesite") if f not in low]
        if flags:
            add("Cookie set without protective flags",
                f"{host} sets a cookie missing the {', '.join(flags)} flag(s), weakening session "
                f"protection",
                f"Set-Cookie seen in the recon response without {', '.join(flags)}",
                3, f"{host} Set-Cookie")

    if _has_form(body):
        add("Form present — input-handling to test",
            f"{host} serves an HTML form whose inputs may be vulnerable to injection (SQLi/XSS) or "
            f"weak authentication",
            "a <form> element was found in the recon response body",
            3, f"{host} form")

    for path in _links_from_body(body, base_url)[:12]:
        sensitive = any(h in path.lower() for h in _SENSITIVE_HINTS)
        add(f"Endpoint {path}",
            f"the endpoint {path} on {host} is reachable and should be tested for access-control, "
            f"injection and information-disclosure issues",
            f"{path} was linked from the recon response for {base_url}",
            3 if sensitive else 2, f"{host}{path}", band="medium" if sensitive else "low")

    return out


def seed_recon_hypotheses(store, parent_id: str, recon: dict, base_url: str) -> int:
    """Create a child hypothesis under `parent_id` for each deterministic recon finding.

    Returns the number created. Never raises into the recon flow — a malformed field skips that
    one finding rather than aborting enumeration."""
    created = 0
    for kw in build_recon_hypotheses(recon, base_url):
        try:
            store.create_hypothesis(primary_parent_id=parent_id, **kw)
            created += 1
        except Exception:  # noqa: BLE001 — one bad finding must not stop the rest
            continue
    return created
