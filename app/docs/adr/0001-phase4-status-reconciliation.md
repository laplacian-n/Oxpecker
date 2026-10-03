# ADR-0001: Reconciling Phase 4 status between the integration prompt and repo evidence

**Status:** Accepted (documented, not a design change)
**Date:** 2026-08-31

## Context

`doc/fromGPTandHackerAI/04-claude-code-integration-prompt.md` was written by a session without
visibility into this conversation's history. It states "Phase 1–3 complete, Phase 4 about to
start" as the owner's current status, and instructs treating `phase4.md`'s past-tense claims as
an unbuilt design target. Direct repository inspection and fresh test runs (see `docs/STATUS.md`)
show `agent/broker/`, `agent/sandbox/`, `agent/evidence/`, `agent/findings/`, `agent/eval/` exist
as working code, and re-running `agent.broker.test_policy_bypass` (15/15) and
`agent.sandbox.test_isolation` (7/7) reproduces the pass counts `phase4.md` claims.

## Alternatives considered

1. Follow the integration prompt literally: mark all of Phase 4 as `planned` regardless of what
   the repo actually contains, and rebuild/re-verify from zero.
2. Trust `phase4.md`'s prose at face value and skip independent verification.
3. Verify against the repo directly, report the disagreement explicitly, and let the *evidence*
   (not either document's prose) set the status.

## Decision

Option 3. The integration prompt's own precedence rules (#1 owner's current statement, #2
repository + reproducible artifacts) support this: the real owner in *this* conversation has not
asserted Phase 4 is unbuilt — that assertion exists only inside an attached document written
elsewhere. Repository and fresh test output are hard evidence and outrank both documents' prose.
`docs/STATUS.md` reports `implemented`/`tested` where evidenced this pass, `planned` where not,
and calls out every case where a reviewer's assumption (e.g. "HTTPS is missing") was contradicted
by actually re-testing the code.

## Security consequences

None from this decision itself — it's a reporting/process decision, not a code change. The risk
this ADR guards against is the opposite one: silently accepting a stale "not built yet" premise
would have led to re-implementing already-hardened, already-tested code (wasted effort) or,
worse, skipping the *real* gaps the reviewers correctly identified inside what already exists
(seccomp absence, injection quarantine not wired to the broker, no asset store) because attention
went to re-litigating timeline instead of closing gaps.

## Migration / rollback

N/A — no code changed. If the owner disagrees with this reconciliation, the correction is to
state the actual current status directly; `docs/STATUS.md` would then be re-derived from that
plus fresh tests, same as it was this time.
