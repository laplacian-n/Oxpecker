# ADR-0004: Whether `bubblewrap` should become the default isolation tier

**Status:** Accepted
**Date:** 2026-08-31 (decided; implemented same day)

## Context

`AgentLoop`'s `isolation_tier` parameter and `run_command`'s corresponding argument both default
to `"direct"` (no sandboxing beyond the Phase-1 binary blocklist and scrubbed env). Both
independent reviews argue this should flip: `01-security-agent-main-direction.md` ("`direct`
ไม่ควรเป็น isolation ค่าเริ่มต้น") and `local-security-agent-review-phase4.md` §1.2 both propose
`bubblewrap` as the default, with `direct` demoted to an explicit, confirmed break-glass mode —
mirroring how `--dangerous-local` already works for the network/privesc binary blocklist.

## Alternatives considered

1. Keep `direct` as default (current state) — matches the original Phase-1 design philosophy of
   "operator chooses per session," and avoids changing default behavior for anyone already
   scripting against the current CLI defaults.
2. Flip the default to `bubblewrap`, require an explicit flag (and, per the reviewers,
   interactive confirmation + an audit flag) to use `direct`.
3. Flip the default only for `run_command` specifically (the tool this applies to), leaving the
   `--dangerous-local` network/privesc override as the separate, existing gate it already is.

## Discussion

The case for flipping (options 2/3) is strong and was not disputed by anything found in this
audit: `bubblewrap` is now genuinely tested (7/7 escape/egress/resource checks passing) and adds
real security margin for the same tool that already has no legitimate reason to touch the
network or the host filesystem outside its workspace in the common case. The main cost is
behavioral: any existing script or workflow relying on `direct`-by-default silently changes
behavior (e.g. a legitimate need to read a host path outside the workspace, or run something
`bubblewrap`'s mount allowlist doesn't cover, now fails until `--dangerous-local`-equivalent
override is added for isolation tier too).

This is exactly the kind of default-behavior change the integration prompt's own "Working Style"
section asks to be conservative about ("Preserve existing user changes and avoid broad
rewrites"), and it changes what runs with elevated trust by default — a security-relevant default
change, which the integration prompt's own escalation rule calls out explicitly ("major
architecture choice").

## Decision

**Option 3, accepted by the owner and implemented.** `run_command`'s isolation-tier default is
now `"bubblewrap"` — at both API surfaces that expose it (`AgentLoop.__init__`'s `isolation_tier`
parameter and `run_command.run()`'s own default; the two are the same conceptual default, just
reachable from two entry points, not a broader "everything" default — `http_recon`/
`port_discovery` still don't take an isolation-tier parameter at all, dispatched through the
broker to network workers as before). `agent/main.py`'s `--isolation-tier` CLI default flipped
to `bubblewrap` too, and choosing `direct` now requires the same typed-confirmation UX
`--dangerous-local` already used — a new prompt, not a reused one, since the two are independent
relaxations (network/privesc blocklist vs. namespace isolation) that shouldn't be confirmable
together by accident. `agent/mcp_tools_server.py`'s own internal default (used when its subprocess
is invoked directly rather than through `AgentLoop`) was flipped too for consistency, and the
one eval-harness layer that had pinned `isolation_tier="direct"` explicitly (`model_tool.py`) now
uses the default instead, so the harness measures the same configuration production runs with.

## Security consequences

Flipping the default raises the floor for every `run_command` invocation that doesn't explicitly
opt out, at the cost of workflows that need `direct`'s broader host access now needing an
explicit flag. Not flipping it means every current and future caller of `run_command` that
forgets to pass `isolation_tier="bubblewrap"` gets the weaker tier silently — this is the
asymmetry the reviewers are pointing at.

## Migration / rollback

If accepted: change `run_command.SCHEMA`'s description, the `isolation_tier` default in
`agent/tools/run_command.py::run()`, `agent/main.py`'s `--isolation-tier` argparse default, and
add the same confirm-and-audit gate `--dangerous-local` uses. Fully reversible by reverting the
default value; no data migration involved.
