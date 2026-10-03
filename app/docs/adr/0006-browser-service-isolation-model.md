# ADR-0006: Browser service isolation model — process-level, not kernel-level

**Status:** Accepted
**Date:** 2026-08-31

## Context

Phase 6 calls for an "isolated Playwright browser service (non-root, sandboxed, ephemeral
context, target-only egress proxy)". Every other tool in this project that runs untrusted-ish
work (`run_command`, via `agent/sandbox/executor.py`'s `BubblewrapExecutor`) is sandboxed with
`bwrap --unshare-all`, which unshares the network namespace entirely — the sandboxed process has
zero network devices, not even loopback. A browser fundamentally needs to reach the RoE-scoped
target over the network, so that exact profile cannot wrap it; a browser inside a
`--unshare-net` namespace simply cannot load a page.

## Alternatives considered

1. **Build a new, network-capable bwrap profile** for the browser specifically: its own network
   namespace, a veth pair back to the host, and nftables/iptables rules restricting egress to
   only the RoE-scoped target's resolved IP(s). This is the isolation model that would actually
   match what the other tools get (kernel-enforced, not application-level).
2. **Application-layer egress control only**: run the browser as a normal non-root process
   (Chromium's own `--no-sandbox`, since nesting Chromium's internal namespace sandbox inside a
   *different* bwrap profile than the zero-network one is untested and this project's own
   `test_isolation.py` already found nested user namespaces unreliable in this environment —
   informational-only, not passing/failing cleanly), and enforce target-only egress by
   intercepting every outgoing request inside the browser (Playwright's `context.route()`) and
   validating each one against the same `scope_check.validate_target()` every other tool uses.
   Ephemeral context (fresh `browser.new_context()` per session, always closed) and non-root
   process ownership still apply; only the *network* boundary is enforced above the kernel
   instead of by it.
3. **Skip the browser service entirely** and wait for option 1's infrastructure to exist.

## Decision

Option 2. Building a new network-capable sandbox profile (real netns + veth + nftables egress
rules, likely needing `CAP_NET_ADMIN` or careful unprivileged netns setup) is a genuinely
separate, nontrivial piece of infrastructure — comparable in scope to the fork-bomb/cgroup work
in ADR-0003, which was tried, found fragile, and reverted rather than shipped half-working. Doing
that *again* for network namespacing, under the same time budget, risked the same outcome: a
fragile "isolated" claim that doesn't actually hold up, which is worse than an honestly-scoped
weaker guarantee. Option 2 is real, tested, and does the one thing that actually matters for this
tool's specific risk (uncontrolled egress to something other than the RoE-scoped target) —
verified directly: a page fetched from an in-scope local server that itself references an
out-of-scope subresource has that subresource request actually aborted before it reaches the
network, not just documented as a policy.

Option 3 was rejected because the browser service has real, immediate value (JS-heavy/SPA content
`http_recon`'s plain HTTP fetch cannot see) and the owner explicitly approved proceeding with the
Playwright/Chromium install (a host-wide-install decision, checked first per this project's own
ask-first rules) rather than deferring the whole milestone.

## Security consequences

What this tier provides: non-root process, no privilege escalation, ephemeral per-session browser
context (verified no cookie/storage leakage across sessions), and every outgoing network request
(navigation, script, image, XHR/fetch, iframe — request interception sees all of them) validated
against the current RoE before it's allowed to leave the process.

What this tier does **not** provide, stated plainly rather than implied: a kernel exploit inside
the Chromium renderer process (running with `--no-sandbox`, i.e. without Chromium's own internal
sandbox either, since that also needs unprivileged user namespaces this environment already found
unreliable) could reach the host directly — there is no namespace boundary between this browser
process and the host the way there is for `run_command`'s bubblewrap-executed processes. This
tier should not be used against untrusted/adversarial targets expected to attempt browser
exploitation; it is scoped for JS-heavy content rendering and DOM/network observation against
lab-authorized RoE-scoped targets, the same trust boundary every other tool in this project
already operates under.

## Migration / rollback

Additive only. If a network-capable bwrap profile (option 1) is built later, `browser_session()`
can be re-pointed to launch Chromium inside it instead of directly — the request-interception
egress check stays as defense-in-depth either way, it doesn't need to be removed once a
kernel-level boundary exists underneath it.
