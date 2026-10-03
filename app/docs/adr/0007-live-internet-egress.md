# ADR-0007: Enable live internet egress for knowledge_search/knowledge_fetch

**Status:** Accepted
**Date:** 2026-08-31

## Context

M5.5 originally shipped as scaffolding-only (see `docs/STATUS.md`'s M5.5 row and the earlier
decision recorded in the ROADMAP): `target_http` fully wired (RoE-scoped, no new exposure),
`knowledge_search`/`knowledge_fetch` policy/budget/cache-complete but their actual outbound HTTP
calls deliberately unimplemented, because real internet egress is a first for this project —
every other tool through Phase 6 is strictly loopback/lab-scoped — and squarely matches the
integration prompt's own "network-exposure" ask-the-owner category. That pass ended with three
options put to the owner directly: scaffolding-only (chosen then), live-egress-now-with-a-named-
provider, or skip to Phase 6. The owner subsequently asked to reconsider and was offered the
live-egress path explicitly, with two follow-up decisions still required before any code changed:
which search provider, and whether to also enable `knowledge_fetch`.

## Alternatives considered (search provider)

1. **Self-hosted SearXNG** — a metasearch engine run as a local Docker container (same
   loopback-bound pattern already used for the Juice Shop/DVWA lab targets:
   `docker run -d --name localai-searxng -p 127.0.0.1:8888:8080 searxng/searxng`), federating
   queries across multiple upstream search engines rather than sending every query to one
   third party directly.
2. **Brave Search API** — a single third-party provider, needs an API key and its own secret-
   handling decision.
3. Leave `knowledge_search` disabled.

Owner chose option 1.

## Alternatives considered (knowledge_fetch)

1. Enable `knowledge_fetch` alongside `knowledge_search` — both reach the real internet, but
   `knowledge_fetch`'s SSRF-relevant policy (`agent/internet/policy.py`,
   `check_knowledge_fetch_allowed`) was already built and tested in the scaffolding pass, so
   enabling it is "wire the already-tested policy to a real call," not new policy work.
2. Enable only `knowledge_search`, leave `knowledge_fetch` disabled.

Owner chose option 1 (enable both).

## Decision

Both channels enabled live this pass:

- `agent/internet/search.py`'s `do_search()` queries the local SearXNG container's `/search?
  format=json` endpoint (JSON output format enabled in the container's `settings.yml`, off by
  default upstream) and returns title/url/content per result.
- `agent/internet/fetch.py`'s `do_fetch()` performs a single-hop GET against the SSRF-checked,
  pinned IP `check_knowledge_fetch_allowed()` already validates — reusing
  `agent.security_tools.http_recon._PinnedConnection` rather than reimplementing pinned-
  connection logic a second time. Redirects are reported, never auto-followed (the caller makes
  a new, separately-policy-checked call to follow one) — `knowledge_fetch` targets arbitrary
  external URLs, unlike `http_recon`'s RoE-scoped target where every hop is independently
  re-validated against the same engagement policy.
- `agent/internet/dispatcher.py`'s `knowledge_search()`/`knowledge_fetch()` now call these
  instead of raising `ChannelNotEnabledError`; every layer built during the scaffolding pass
  (budget, cache, provenance, injection-guard scan + taint escalation) sits in front of the real
  call unchanged.

Verified live, not just unit-tested: a real query against the running SearXNG container returned
real OWASP-related results, and a real fetch of `https://example.com/` returned a real 200
response — both exercised in `agent/internet/test_dispatcher.py`'s `TestLiveEgress` (skips
gracefully, doesn't fail the suite, if the SearXNG container isn't running on a given machine).

## Security consequences

This is the first capability in the project that reaches the real public internet rather than a
loopback-bound lab target. What's still enforced regardless: `knowledge_fetch` still blocks
loopback/private/link-local/CGNAT/multicast/cloud-metadata IP ranges (SSRF protection, unchanged
from the scaffolding pass) and per-session/per-channel budgets still cap query and byte volume.
What's now real that wasn't before: content returned from these channels is genuinely
attacker-influenced (any page on the internet, any SearXNG result), so the M4.5 injection-
quarantine path (scan → taint → broker approval escalation for anything beyond passive recon) is
now defending against real untrusted content for the first time, not just simulated test cases —
this makes M4.5 having already been built and tested before M5.5 shipped (the ROADMAP's own
stated ordering: "M4.5 is the hard gate before M5.5") a real precondition that was actually
satisfied, not just a sequencing note.

`knowledge_search` sends every query to the local SearXNG instance, which in turn sends it to
whichever upstream engines SearXNG is configured to federate across (their default set) — an
operator who wants tighter control over which upstream engines see queries should edit the
SearXNG container's `settings.yml` `engines:` section directly; that's SearXNG's own
configuration surface, not something this project's code mediates.

## Migration / rollback

Fully reversible: reverting to scaffolding-only means restoring the `ChannelNotEnabledError`
branches in `dispatcher.py` (or simply stopping the `localai-searxng` container, which makes
`knowledge_search` fail with a `SearxngError` on the next call rather than silently degrading —
`knowledge_fetch` has no such single point of failure since it dials arbitrary hosts directly).
No schema or state-store changes were needed for this decision; `InternetCache`/
`ChannelBudgetTracker` records from the scaffolding pass remain valid.
