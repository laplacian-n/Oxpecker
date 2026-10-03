# ADR-0005: Evidence content-ID scheme (plaintext SHA-256 vs. keyed/HMAC)

**Status:** Accepted
**Date:** 2026-08-31 (decided; implemented and migrated same day)

## Context

`agent/evidence/store.py` addresses stored evidence by `hashlib.sha256(plaintext_data)`, computed
*before* encryption. `01-security-agent-main-direction.md` M4.2 flags this: "หลีกเลี่ยงใช้
plaintext SHA-256 เป็น identifier ภายนอกหาก equality leakage สำคัญ; ใช้ HMAC/content ID แบบ keyed
ได้" (avoid a plaintext-SHA-256 external identifier if equality leakage matters; use a keyed
HMAC/content ID instead).

## What "equality leakage" means here

Because the digest is a deterministic hash of the plaintext, two pieces of evidence with
identical content always produce the same digest, and an attacker who can see digests (e.g. via
the audit log's `content_digest` field, which is not itself secret) but not the encrypted blobs
can potentially confirm whether two evidence entries are byte-identical, or — if they can guess
or narrow down candidate plaintexts (e.g. a specific HTTP response for a well-known page) —
confirm a guess by hashing it themselves and comparing. This is a real, if narrow, information
leak: it's a dictionary/rainbow-table-style attack on the *fact of equality*, not a break of the
encryption itself.

## Alternatives considered

1. Keep plaintext SHA-256 as the content ID (current state) — simplest, and content-addressing's
   whole benefit (dedup identical content, one copy) *requires* identical inputs to map to
   identical addresses, which plaintext hashing gives for free.
2. Switch to `HMAC-SHA256(key, plaintext)` as the content ID — still deterministic (dedup still
   works), but unguessable without the key, closing the equality-leakage/dictionary-attack gap.
3. Use a random UUID as the address and keep the plaintext hash only as an internal integrity
   check (not exposed as the addressable ID) — loses content-addressed dedup entirely.

## Discussion

Option 2 preserves everything the current design already does (content-addressed dedup, integrity
check on read) while closing the specific leak the reviewer identified, at the cost of needing
the same key used for encryption to also be available wherever a digest needs to be computed or
verified — which is already true today (the evidence store already holds and uses that key for
encryption), so this isn't a new key-management burden, just a second use of the existing one.
Option 3 is a bigger, less clean change (loses dedup, and the audit log's `content_digest` field
would then not correspond to anything content-addressed) for a gap option 2 already closes.

## Decision

**Option 2, accepted by the owner and implemented.** `EvidenceStore.put()`/`get()` and
`AuditLog.record()` both now compute `HMAC-SHA256(key, data)` via a single shared helper
(`agent.evidence.store.compute_digest()`) under the same key already used for encryption —
`AuditLog` reads that key file directly (a second, independent use of it, not a new key to
manage). `Broker._finalize()`'s `assert entry["content_digest"] == evidence_digest` needed no
code change at all: both sides moved to the identical scheme, so the cross-reference keeps
holding for every dispatch (verified: `test_policy_bypass.py` and `test_injection_quarantine.py`,
which exercise that exact assertion through the real broker, both still pass).

`get()` was given one adjustment beyond the original recommendation: it verifies against HMAC
*first*, and falls back to the legacy plaintext-SHA-256 check only if that doesn't match — found
necessary directly (not assumed) when the first version of the migration test tried to read an
unmigrated legacy blob and got a hard `EvidenceIntegrityError`, because `get()` had no way to
recognize content stored before this pass shipped. Without that fallback, every existing blob
would have become unreadable the instant this code deployed, not just before its own migration
ran — new digests from `put()` are still always HMAC, this only widens what `get()` accepts when
reading.

### Migration, actually run against real data

`migrate_to_hmac()` re-addresses every live legacy blob (decrypt under its recorded key version,
recompute the HMAC digest under that same version, copy the ciphertext to the new filename,
tombstone the old digest with `deletion_reason="migrated_to_hmac:<new_digest>"` rather than
deleting its history outright). It returns `(migrated, problems)` — a single bad blob is skipped
and reported, never aborts migrating the rest of a batch (the first version did abort on the
first failure; fixed after hitting a real one, see below).

This ADR's own original migration note assumed "no production evidence exists yet." That was no
longer true by the time this decision was implemented — this session's own testing had
accumulated 108 real evidence blobs in `agent/state/evidence/` (all `agent.internet`/
`agent.pipeline.executor`/`agent.broker` test-verification runs against the real Juice
Shop/DVWA lab targets, not operator-authored findings, but real stored-and-encrypted data none
the less). Run for real, after backing up `agent/state/evidence/` and `agent/state/audit/` to
`/tmp/{evidence,audit}_backup_pre_hmac_migration/` first:

- **76 blobs migrated successfully** (a handful more from an earlier, since-superseded run before
  the skip-on-failure fix — 93 `migrated_from`-tagged records exist in total; a re-run
  afterward correctly migrates 0, confirming idempotency on real data).
- **2 blobs reported as unmigratable** (`decryption failed`) — confirmed to be a **pre-existing**
  issue, not something this migration caused: both already failed the exact same way via a plain
  `store.get()` call *before* any migration code touched them. Left as-is (not deleted, not
  silently dropped) — their index history is still queryable via `index_for()`.
- **All 243 real audit-log session files accumulated this session verify cleanly**
  (`audit_log.verify()`) after the migration — confirming the deliberate choice to leave
  *historical* audit entries' `content_digest` untouched (see below) didn't break their
  tamper-evident hash chains, and that new entries written after this pass correctly use the new
  scheme.

## Security consequences

Staying at plaintext SHA-256 (current state) leaves the narrow equality-leakage gap open — it
does not expose evidence content itself (still Fernet-encrypted, still requires the key to
decrypt), only whether two entries or a guessed plaintext are byte-identical to stored evidence.
This is a real but low-severity gap for this project's current single-operator threat model
(§9 "Intended operator and environments" in the baseline research doc); it becomes more relevant
once evidence digests are ever shared outside the operator's own trust boundary (e.g. in a
report, or with a co-operator on a shared engagement).

## Migration / rollback

Implemented as (b), not (a): `get()`'s HMAC-then-legacy-fallback verification means old and new
addressing schemes coexist for reading, so `migrate_to_hmac()` re-addressing every live blob
(what actually ran, see above) is an active cleanup, not a hard requirement for the system to
keep working — an operator could in principle defer running it and everything would still read
correctly, just addressed under the old scheme until migrated.

A **deliberate, important exception** discovered while implementing this: the evidence store's
index (plain append-only JSONL) was safely rewritten/appended to as part of migration, but
`agent/audit_log.py`'s *historical* entries were **not** retroactively changed to use the new
digest scheme, and must never be — the audit log's tamper-evidence comes entirely from its
hash-chain (`entry_hash` chained via `_prev_hash`), and silently editing an old entry's
`content_digest` field to match a new addressing scheme would be indistinguishable, from the
chain's own perspective, from the exact tampering it exists to catch. A pre-migration audit
entry's `content_digest`/`evidence_digest` value is a legitimate historical record of what was
computed *at that time* (plaintext SHA-256) — resolvable to the evidence blob's current (HMAC)
address via that blob's `index_for(old_digest)` tombstone, not by rewriting the audit entry
itself. Rollback: reverting `compute_digest()` to plain SHA-256 and dropping the legacy-fallback
branch in `get()` is the code-level rollback; blobs already migrated stay migrated (their
ciphertext never changed, only which digest maps to that content) — a rollback would re-break
the equality-leakage gap this ADR closed, not re-break data access.
