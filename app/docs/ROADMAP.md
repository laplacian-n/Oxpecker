# ROADMAP

Revised phase structure per `doc/fromGPTandHackerAI/04-claude-code-integration-prompt.md`,
synthesizing the three independent reviews (HackerAI, Codex/GPT, and the integration prompt's own
precedence rules). Status per milestone is cross-referenced to `docs/STATUS.md` — this file is the
plan, STATUS.md is the truth about what's actually done.

Precedence when documents disagree (unchanged from the integration prompt, repeated here so this
file is self-contained): owner's current statement → repo/test evidence → security invariants
(broker authority, RoE, fail-closed, assisted mode) → phase1–3 docs → accepted research-doc
decisions → the 6-file proposal set as opinions → phase4.md's past-tense claims as acceptance
targets only.

## Phase 4A — Trust & Evidence Gate

Goal: what Phase 3 built can be re-run, audited, and fails closed — before adding more pentest
tools. Per STATUS.md, most of this phase's *code* already exists; what's incomplete is test
depth and a few schema fields, not the presence of the subsystems.

- **M4.0 — Truthful status baseline.** This ROADMAP.md and STATUS.md *are* M4.0. Exit: done —
  status reflects repo/test evidence, contradiction with the integration prompt's premise is
  recorded, not silently resolved either direction.
- **M4.1 — Bubblewrap profile hardening.** Core implemented; test suite expanded this pass from
  7 to 16 checks (`agent.sandbox.test_isolation`) — symlink escape, inherited-FD, Unix-socket,
  PID-namespace/ptrace-surface, TIOCSTI, nested-userns (informational), signal/cancellation, and
  parent-death are now covered and passing, and fixed 3 vacuous-pass bugs found in the test
  suite itself along the way (see docs/STATUS.md's dedicated note). `RLIMIT_FSIZE` (disk-fill
  containment) added and tested. **Fork-bomb/process-count containment remains a real, open
  gap — two independent approaches tried and reverted this pass, both with concrete evidence
  (ADR-0003):** `RLIMIT_NPROC` (wrong primitive, counts per-UID system-wide, broke bubblewrap
  itself) and cgroup v2 `pids.max` via `systemd-run --user --scope` (correctly scoped and did
  cap a real fork bomb standalone, but introduced a reproducible signal-delivery race that
  leaked orphaned sandboxed processes on the host — reverted rather than shipped flaky). Two
  decisions remain open for the owner, not resolved unilaterally: a seccomp profile (ADR-0003)
  and whether `bubblewrap` should become `run_command`'s default isolation tier instead of
  `direct` (ADR-0004).
- **M4.2 — Evidence/audit lifecycle.** Key versioning + rotation, deletion tombstone, retention
  job, and a restore test are now implemented and tested (13/13,
  `python3 -m agent.evidence.test_lifecycle`) — verified against real production evidence (the
  Juice Shop findings' blobs), which went through a live format migration with no data loss.
  Remaining: external/signed checkpoint (still local-file-only), a scheduler to actually run the
  retention job periodically (the job itself works, nothing calls it on a cadence yet), and the
  plaintext-SHA-256-vs-keyed-content-ID decision (ADR-0005, still open for the owner).
- **M4.3 — Evidence-first finding schema.** Schema expanded (confidence, status,
  demonstrated_impact, CVSS version/vector, CWE/CVE refs, affected_component, preconditions,
  observation_refs, reproduction_recipe_ref, references, first_seen/last_verified/verifier,
  limitations) and tested — backward compatible with the two real findings written under the old
  schema. SARIF and Markdown/PDF renderers updated to surface all of it. **Unicode/Thai PDF
  support is now real**, correcting the original reviewer assumption that it was missing — real
  Noto Sans + Noto Sans Thai TTF fonts (already installed system-wide, no download) with
  per-script font switching, verified with actual mixed Thai/English content extracted back out
  and read correctly. Remaining: the observation→hypothesis→verification→finding *separation*
  (distinct pipeline stages, not just schema fields) still needs the Phase 5 pipeline to mean
  anything — this pass only made the schema ready for it.
- **M4.4 — Eval harness expansion.** 3-task single-seed harness implemented and previously run
  3/3. Remaining: ambiguity/no-call/missing-arg/denial/truncation task cards, multiple
  seeds/temperature profiles, failure taxonomy (model vs. tool vs. policy vs. target vs.
  infrastructure), and re-running the original §1 tool-calling benchmark against the current i1
  quant (flagged as outstanding since the quant swap, still not done).

**Exit for 4A (unchanged from the integration prompt):** regression suites for Phase 1–3 pass,
M4.1–M4.4 acceptance tests pass, nothing in this repo claims a capability the test suite doesn't
evidence.

## Phase 4B — Prerequisites before internet expansion

- **M4.5 — Injection quarantine → broker. Done and tested** (20/20,
  `python3 -m agent.broker.test_injection_quarantine`): a verdict taxonomy
  (clean/suspicious/malicious/unknown, covering encoded/homoglyph/zero-width/multilingual cases)
  plus session-wide cross-turn taint tracking (`agent/broker/taint.py`) that escalates any
  beyond-passive-recon action to human approval for a bounded window after suspicious content is
  seen. Now safe to move on to M5.5 (internet access) — this was the hard gate.
- **M4.6 — HTTPS completion. Done and tested** (8/8,
  `python3 -m agent.security_tools.test_http_recon_https`, with real local TLS test servers —
  self-signed/SAN-mismatch/expired certs all correctly rejected, custom CA bundle trust works,
  `verify_cert=False` gated behind a new `http_recon_insecure` action class that's denied by
  default RoE and still needs approval even when opted in). Found and fixed a real bug along the
  way: `http_recon` crashed on a plain `ConnectionRefusedError` instead of returning a clean
  error — recon tools routinely hit non-listening targets, so this was a real robustness gap,
  not a hypothetical one.
- **M4.7 — Prompt registry skeleton. Done and tested** (17/17,
  `python3 -m agent.prompts.test_compiler`): `agent/prompts/` holds 6 fixed layered files (5
  static + 1 Jinja2-templated), compiled deterministically with a stable digest, strict
  undefined-variable checking, and a real budget check against the live model's
  tokenizer/context window. Wired into `agent/loop.py`'s `_system_message()` (replacing the old
  hardcoded string — same substance, migrated not rewritten) and recorded as `prompt_version` in
  both the audit trail and eval harness metadata. Phase 4B is now fully closed — Phase 5's
  internet-access work (M5.5) can proceed since M4.5 (its hard gate) is done.
  **The `phases/`/`tools/` seam directories M4.7 originally left empty were populated and wired
  in a later pass**: all 6 pipeline phases have real guidance, cards exist for the 5
  model-callable tools, both are optional/additive to `compile_prompt()` (verified
  byte-identical output when omitted), and `AgentLoop` now looks up real M5.2 phase state per
  engagement and passes its actual active tool set — see `docs/STATUS.md`'s M4.7 row for detail
  and for what's still not wired (2 of 4 M5.5 internet channels and the browser service aren't
  model-callable tools yet, so they have no card).

## Phase 5 — Engagement Intelligence + Pipeline + Internet

The consensus highest-value missing piece across all three reviewers: a deterministic pipeline
and authoritative state store around the model, so Qwen3-32B's known weakness (judgment under
ambiguity, ~40% on BFCL's multi-turn subset per the docs) is compensated by structure rather than
by hoping the model chooses well.

- **M5.1 Structured engagement intake. Done and tested** (14/14,
  `python3 -m agent.engagement.test_intake`): `agent/engagement/intake.py`'s
  `EngagementIntake` dataclass + `create_engagement()` writes `roe.json`/`scope.txt`/`deny.txt`
  under `engagements/<id>/`, validated (sign-off required, non-empty scope/action-classes, sane
  time window, valid targets) before anything is written. Interoperates with the existing,
  unmodified Phase-3 `load_policy()`/`Broker` — additive only, `config.ENGAGEMENT_DIR` (the
  legacy single-engagement default) is untouched. CLI at `agent/engagement/cli.py`
  (`--from-json` or interactive). Rate/blast-radius fields are captured as metadata but not yet
  enforced per-engagement (documented gap — broker still uses one global cooldown dict).
- **M5.2 Authoritative engagement-state store. Done and tested** (29/29,
  `python3 -m agent.engagement.test_store`): `agent/engagement/store.py`'s `EngagementStore`,
  one SQLite (WAL) database per engagement at `engagements/<id>/state.db`, same
  connect/optimistic-concurrency pattern as `agent/memory_service/db.py` (reused, not
  reinvented). Covers assets/services/endpoints/relationships, evidence-linked observations, a
  hypothesis board (priority/preconditions/evidence_for/against/coverage_surface/owner) with
  version-checked updates, a coverage ledger (surface × technique → status), a forward-only
  phase state machine with history, version-checked tasks, and a findings lifecycle table with
  retest records and a write-once mandatory closeout that counts outstanding findings rather
  than dropping them. What this milestone deliberately does *not* do: decide *when* to
  transition phases or create tasks (that's M5.3's criteria/budget-driven policy) or wire tool
  output into the store automatically (also M5.3).
- **M5.3 Two pipeline profiles. Done and tested** (16/16,
  `python3 -m agent.pipeline.test_orchestrator`): `agent/pipeline/profiles.py` defines Web/API
  and Network as `TaskTemplate` lists sharing `agent.engagement.store`'s
  `INTAKE→RECON→ANALYSIS→VALIDATION→REPORT→CLOSEOUT` state machine;
  `agent/pipeline/orchestrator.py`'s `PipelineOrchestrator` does idempotent incremental task
  planning from live state (`plan_tasks()`) and criteria/budget-driven transition
  (`check_transition()`/`advance_if_ready()`) — verified a phase can complete with **zero**
  hypotheses created, correcting `phase5-design-detail.md`'s rejected "≥1 confirmed finding"
  exit criterion, and that wall-clock budget exhaustion forces a transition even with tasks
  still pending. What's still open: this is the deterministic planning/gating layer only — wiring
  a planned task to an actual broker-mediated tool call and feeding its result back into the
  state store is separate integration work into `agent/loop.py`, not yet done.
- **M5.4 Hypothesis board + skill library. Done and tested**: the hypothesis board is
  `EngagementStore`'s `hypotheses` table (M5.2 — priority/preconditions/evidence_for/against/
  coverage_surface/owner/version, per HackerAI's A1). The skill library (18/18,
  `python3 -m agent.skills.test_library`) is `agent/skills/library.py`'s `SkillLibrary`: curated,
  versioned YAML artifacts, HMAC-signed (a local-integrity signature, not a PKI trust chain —
  right-sized for a single-operator setup), loaded read-only with signature verification that
  fails closed (a missing/tampered signature excludes the skill and is reported, never silently
  dropped). The only write path is `agent/skills/cli.py`, an offline human-run tool nothing in a
  live engagement can reach — so target/model content structurally cannot write to the library,
  not just by policy. Not yet wired into the pipeline orchestrator's task generation/execution.
- **M5.5 Internet access, four separate channels. Done and tested, including live egress**
  (34/34, `python3 -m agent.internet.{test_budget,test_policy,test_cache,test_osint,
  test_dispatcher}`, two of them real network integration tests): real internet egress is a
  first for this project, so this milestone's scope was checked with the owner twice —
  network-exposure is one of the integration prompt's own ask-first categories. It shipped in
  two stages: scaffolding-only first (`agent/internet/` gets a complete
  budget/policy/cache/provenance/quarantine layer for all 4 channels, `target_http` fully wired),
  then live egress (ADR-0007) after the owner asked to reconsider and chose a self-hosted SearXNG
  container (`docker run ... searxng/searxng`, loopback-bound like the Juice Shop/DVWA lab
  targets) for `knowledge_search` and chose to enable `knowledge_fetch` alongside it —
  `agent/internet/{search,fetch}.py` wire real calls behind the already-tested policy/budget/
  cache/quarantine layers, `knowledge_fetch` reusing `http_recon`'s pinned-IP connection rather
  than reimplementing it. `osint_discovery` needed no such staging — it's record-only by
  construction (`agent/internet/osint.py` has no fetch/request/probe function at all, verified
  by a structural test, not just a docstring).

## Post-Phase-6 wiring pass

The backend primitives below were built and tested in isolation, then flagged as "not yet wired
into anything live" in their own gaps rows. This pass connects them: `knowledge_search`/
`knowledge_fetch` are now real model-callable tools (broker-mediated, own action classes,
default lab RoE updated to allow them); `osint_discovery` is callable as `osint_record`; the
approval queue is wired into `Broker.dispatch()` (sync history-recording always, an opt-in async
out-of-band mode); the steer channel is wired into `agent/loop.py`'s iteration loop; the skill
library is wired into the pipeline orchestrator's task planning; and — the M5.3 gap explicitly
called "separate, larger integration work" — `agent/pipeline/executor.py` connects a planned
task to a real broker-mediated tool call and feeds the result back into engagement state, tested
against the real Juice Shop lab container end to end. Two real bugs were found and fixed doing
this (a wrong output-shape assumption in the port_discovery executor, a batch/rate-limit
interaction), plus the same "default argument bound at import time" class of bug found 3 times
in the prior pass recurred in two more Phase-1 files (`agent/session.py`, `agent/audit_log.py`)
and was fixed at the root. See `docs/STATUS.md`'s "Post-Phase-6 wiring pass" section for detail.
A full eval harness re-run after this pass stayed clean (4/4, 15/15, 2/2, 5/5).

## Phase 6 — Advanced Web + UX

**Every backend primitive done and tested; the web UI itself remains explicitly excluded**
per the owner's own scope for this pass ("finish everything until the UI"):

- **Isolated Playwright browser service** (`agent/browser/service.py`) — non-root, ephemeral
  `browser.new_context()` per session, target-only egress enforced via request interception
  reusing the same `scope_check.validate_target()` every other tool uses (verified against a
  real subresource-blocking case, not just top-level navigation). Playwright/Chromium installed
  with explicit owner approval first (a host-wide-install decision under this project's own
  ask-first rules) — see `requirements-phase6.txt`. Not wrapped in the existing
  `BubblewrapExecutor` profile, which unshares all networking and so cannot host a
  network-needing tool; a network-capable bwrap profile is real, separate infrastructure work
  not taken on this pass — documented precisely, not implied to be the same guarantee.
- **Self-hosted OOB catcher** (`agent/oob/server.py`) — stdlib-only, loopback-bound (matches this
  project's lab-only Docker targets), **correlation-token-based validation, not source-IP
  scope-gating** (the integration prompt's own non-negotiable correction) — verified with a real
  request carrying a spoofed `X-Forwarded-For` header still validated purely by token match.
- **Approval queue** (`agent/broker/approval_queue.py`) and **steer channel**
  (`agent/loop_control/steer.py`) — both tested backend primitives, neither yet wired into the
  live `Broker.dispatch()`/`agent/loop.py` paths (the existing synchronous CLI approval prompt
  is untouched and remains load-bearing); steer wiring was deliberately deferred rather than
  editing `agent/loop.py` while the eval harness had it under active use.
- **Session handoff** (`agent/engagement/handoff.py`) — a thin combinator over the existing
  memory-service session state (Phase 2) and `EngagementStore` phase/task/hypothesis state
  (M5.2), not a new state store.
- **Retest mode from versioned recipes** (`agent/recipes/`) — never raw replay: a recipe is a
  list of typed tool calls from a small fixed allowlist, replayed with policy always injected
  fresh at retest time. Verified the core safety property directly: replaying against a
  rescoped/narrowed policy fails the retest closed exactly like a live call would.
- **Bounded deterministic parallel executor** (`agent/pipeline/parallel.py`) — plain thread pool
  over independent callables, not multi-agent/multi-LLM (Codex's §3.6 and the integration prompt
  agree this is a hard line); verified with a real peak-concurrency measurement, not just correct
  return values.
- **Mandatory closeout** — already done as part of M5.2, not repeated here.
- **Web UI. Done and tested, including live.** The owner's own choice among three offered
  options: a plain FastAPI server + a single static HTML/JS page, no build step, no framework
  (over a modern SPA with a build pipeline, or stopping here). Session lifecycle, turn-level SSE
  streaming (not token-level — `AgentLoop`/`LlamaClient` only do non-streaming completions
  today, stated precisely rather than implied), and approval bridging (both local `run_command`
  confirms and broker-mediated tool approvals, unified through the existing `ApprovalQueue`) all
  verified live against a real running server and the real model, not just via an in-process
  test client. Found and fixed a real concurrency bug in `ApprovalQueue` along the way (file
  writes weren't atomic — a genuine race the web UI's own concurrent poll+submit pattern
  finally exercised), now regression-tested with a real concurrent-stress test. See
  `docs/STATUS.md`'s "Phase 6 web UI" section for full detail and the one real gap (session
  resume after a server restart isn't wired up — transcripts are safe on disk, resuming the
  live `AgentLoop` object isn't).

  **Redesigned once more after the owner shared `ai-web-platform-mockups/`** (a real Claude
  Design handoff bundle for a "RedTeam AI" mockup, plus reference screenshots of the actual
  HackerAI desktop app). Visual language and interaction pattern ported (dark palette, composer
  pills, message-block timeline, slide-out tool-output panel); mockup features with no real
  backend (Projects, Billing, multi-account, cloud environment) deliberately left out rather than
  built as decoration — the owner's own explicit choice when asked. `python3 -m
  agent.web.test_frontend` (6/6) drives a real headless browser against the real server and real
  model, asserting zero console/JS errors across every scenario. See `docs/STATUS.md` for detail.

## ADR resolution pass

The 3 ADRs left open through every prior pass (0003 seccomp, 0004 default isolation tier, 0005
evidence content-ID scheme) were presented to the owner directly, all three accepted as
recommended, and all three implemented and tested the same day — see `docs/STATUS.md`'s "ADR
resolution pass" section and each ADR file for full detail. Headline changes: `run_command`
now sandboxes by default (`bubblewrap`, not `direct`); the bubblewrap tier now loads a real
seccomp deny-list (`ptrace`/mount/namespace/kernel-module primitives), verified with a real
differential test (denied with the filter, succeeds without it); evidence and audit-log content
addressing moved from plaintext SHA-256 to HMAC-SHA256, migrated against this session's own real
accumulated evidence data (not just synthetic test fixtures), with the audit log's historical
hash-chain integrity re-verified intact afterward across all 243 real sessions.

## Phase 7 — Network Red-Team Expansion

Constrained service/version tooling and a network lab, credential/post-exploitation/pivot
capabilities as a high-risk action class disabled by default, microVM/OCI supply-chain hardening
*if and when* arbitrary exploit-grade code justifies the investment (deferred, not abandoned — see
ADR-0002), and no runtime self-modifying tools ever — tool scaffolding stays an offline,
human-reviewed workflow.

## What this roadmap deliberately does not do

Per every source document's shared warning: no giant single-PR implementation of multiple
phases, no "must find at least one vulnerability" success criterion anywhere in the pipeline, no
tool installed before its wrapper+policy+card+tests exist, no multi-agent/model-switching used as
a substitute for the deterministic state/pipeline work above, and no claim in any doc treated as
implementation truth without the command that reproduces it.
