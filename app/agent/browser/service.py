"""Phase 6 — isolated browser service for JS-heavy recon/validation (SPAs, client-rendered
content `http_recon`'s plain HTTP fetch can't see). Built on Playwright/Chromium, installed this
pass with the owner's explicit go-ahead — installing a browser binary is a host-wide-install
decision under this project's own ask-first rules, so this was checked before anything was
written, not assumed.

Isolation model, stated precisely rather than implied:

- **Non-root**: runs as the same unprivileged OS user as everything else in this project; no
  privilege escalation anywhere in this path.
- **Ephemeral context**: every `browser_session()` call gets a fresh `browser.new_context()`
  (isolated cookies/storage/cache) that is always closed at the end of the `with` block, and the
  browser process itself is always closed too (both wrapped in `finally`) — never reused across
  sessions or engagements, and never leaked on an exception.
- **Target-only egress**: enforced at the browser's own request-interception layer
  (`context.route("**/*", ...)`), validating every outgoing request's host against the SAME
  `agent.broker.scope_check.validate_target()` every other tool in this project already uses,
  before the request is allowed to leave the process — not just a documented intention.
  Navigation, scripts, images, XHR/fetch, iframes: request interception sees all of them, and an
  out-of-scope one is aborted before it reaches the network.
- **What this is NOT**: this project's existing `agent.sandbox.executor.BubblewrapExecutor`
  profile unshares ALL namespaces including network (`--unshare-net`, zero network devices at the
  kernel level) — the isolation model every other tool here uses. A browser fundamentally needs
  network reachability to the target, so that exact profile cannot wrap it; building a *new*,
  network-capable bwrap profile (its own netns + veth + nftables egress rules) is real, separate
  infrastructure work this pass deliberately does not take on. The isolation here is
  process-level non-root + ephemeral context + application-layer egress validation, not
  kernel-level network namespacing — documented as a real, current scoping choice, not implied
  to be the same guarantee the sandboxed tool-execution path provides.
"""
from __future__ import annotations

import os

from contextlib import contextmanager
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from ..broker import scope_check
from ..broker.policy import Policy


def _make_route_handler(policy: Policy, denied: list[dict]):
    def _route_handler(route, request):
        host = urlparse(request.url).hostname
        if not host:
            denied.append({"url": request.url, "reason": "no host in URL"})
            route.abort()
            return
        result = scope_check.validate_target(host, policy)
        if not result.allowed:
            denied.append({"url": request.url, "reason": result.reason})
            route.abort()
            return
        route.continue_()

    return _route_handler


@contextmanager
def browser_session(policy: Policy, headless: bool = True):
    """Yields (page, denied_requests) — `page` is a Playwright Page inside a fresh, ephemeral,
    non-root browser context with target-only egress enforcement already wired up;
    `denied_requests` is the same list object, appended to live as requests are blocked, so a
    caller can inspect what was denied during the session without polling anything."""
    denied: list[dict] = []
    with sync_playwright() as p:
        # OXPECKER_BROWSER_EXECUTABLE points at a Chromium binary to use instead of the one
        # Playwright downloaded for itself. Two reasons it is worth having: a server install may
        # prefer the distribution's chromium to a second copy under ~/.cache, and a Playwright
        # Python version only launches the exact browser build it was pinned against — a host
        # carrying a different build has a perfectly good browser that Playwright refuses to use
        # ("Executable doesn't exist at .../chromium_headless_shell-<build>/..."). Unset, this
        # changes nothing: Playwright resolves the browser exactly as before.
        launch_kwargs: dict = {"headless": headless, "args": ["--no-sandbox"]}
        executable = os.environ.get("OXPECKER_BROWSER_EXECUTABLE", "").strip()
        if executable:
            launch_kwargs["executable_path"] = executable
        browser = p.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context()
            try:
                context.route("**/*", _make_route_handler(policy, denied))
                page = context.new_page()
                yield page, denied
            finally:
                context.close()
        finally:
            browser.close()


MAX_CONTENT_EXCERPT_BYTES = 20_000


def browser_fetch(policy: Policy, url: str, timeout_ms: int = 15000) -> dict:
    """One-shot wrapper around browser_session() for a model-callable tool: navigate, capture
    the rendered (post-JS) HTML + title, tear everything down. Every subresource the page tries
    to load is still validated the same as browser_session() does on its own — this is not a
    separate, looser path.
    """
    with browser_session(policy) as (page, denied):
        try:
            page.goto(url, timeout=timeout_ms)
            content = page.content()
            title = page.title()
            return {
                "ok": True,
                "url": url,
                "title": title,
                "content_excerpt": content[:MAX_CONTENT_EXCERPT_BYTES],
                "content_truncated": len(content) > MAX_CONTENT_EXCERPT_BYTES,
                "denied_requests": list(denied),
                "error": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "url": url,
                "title": "",
                "content_excerpt": "",
                "content_truncated": False,
                "denied_requests": list(denied),
                "error": f"{type(e).__name__}: {e}",
            }
