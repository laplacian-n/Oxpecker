# STATUS

Truthful implementation status, derived from repository code and reproducible test runs on
2026-08-31T05:32Z, not from prose in other documents. Where a doc's past-tense claim ("built",
"exit-tested", "N/N passed") agrees with what was actually re-run here, that's noted as
confirmed; where it doesn't, repo/test evidence wins per the stated precedence order.

## Contradiction flagged before any further work

`doc/fromGPTandHackerAI/04-claude-code-integration-prompt.md` states the owner's current status
as "Phase 1–3 complete, Phase 4 about to start" and instructs treating `phase4.md`'s past-tense
claims as an unbuilt target specification. That does not match this repository: `agent/broker/`,
`agent/sandbox/`, `agent/evidence/`, `agent/findings/`, `agent/eval/` all exist with working code,
and re-running their test suites just now (see below) reproduces the pass counts `phase4.md`
claims. Phase 4's *core* (M4.1 sandbox, M4.2 evidence, M4.3 findings, M4.4 eval) was built and is
evidenced in this same session's history, not merely documented as a future plan.

This is not a case of "trust the docs blindly" — every number below was re-run just now, not
copied from `phase4.md`. The contradiction is between two *owner-attributed* statements: the
integration prompt (written by an external session without visibility into this conversation)
says Phase 4 hasn't started; the actual repository and fresh test output say a meaningful subset
of it has, and has been verified working. Per the integration prompt's own precedence rule #1
("owner's current statement wins") and rule #2 ("repository + reproducible artifacts win"), and
because the real owner in *this* conversation has not asserted Phase 4 is unbuilt, this STATUS.md
reports what the repository actually contains — `implemented` and `tested` where evidenced,
`planned` where not — rather than force everything in Phase 4 back to `planned` to match a
premise this session's own history contradicts. The **gaps the reviewers identified inside what
exists are real and are tracked below as `gap`**, independent of this timeline disagreement.

Environment for all commands below: `/home/nicotine/localAI`, Python 3.14, no venv unless noted.
Not a git repository (no commit hash to pin — noted as a gap in Reproducible toolchain, §Codex
8.4/Working Style "pin versions/digests").

## Legend

`planned` = designed, no code. `implemented` = code exists. `tested` = automated test exists and
was just re-run with the result shown. `accepted` = tested + reviewed against explicit exit
criteria. `deferred` = explicitly out of scope for now, with a stated reason.

## Phase 1 — Terminal agent core

| capability | status | test command | last verified | evidence | gaps | source |
|---|---|---|---|---|---|---|
| llama.cpp tool-calling via `--jinja` | accepted | `python3 -c "from agent.loop import AgentLoop..."` | 2026-08-31 (this session, re-verified: `agent.loop imports OK on system python3`) | inline round-trip in prior session turns | not re-run as a formal regression suite this pass | phase1.md |
| Workspace-constrained file tools | implemented | none formal | prior session | `agent/tools/{read_file,write_file,workspace}.py` | no standalone pytest file | phase1.md |
| Context budget (evict → summarize) | implemented | none formal | prior session | `agent/budget.py` | no standalone pytest file; not re-run this pass | phase1.md |
| Loop guards (max iter/timeout/repeat) | implemented | none formal | prior session | `agent/loop.py` config constants | not re-run this pass | phase1.md |
| Audit log (hash chain) | implemented | none formal this pass | prior session | `agent/audit_log.py` | verified via Phase 3/4 broker test runs below (audit chain intact after mixed writers) | phase1.md |

**Gap (Codex/HackerAI, both):** no dedicated `pytest` regression suite for Phase 1 exists —
correctness was established interactively in a prior session, not via a command this audit can
point to and re-run. Recommend adding one before Phase 5 (M4.0 follow-up, not blocking).

## Phase 2 — Shared memory

| capability | status | test command | last verified | evidence | gaps | source |
|---|---|---|---|---|---|---|
| Memory service (FastAPI+SQLite, TLS, device tokens) | implemented | `python3 -m agent.run_memory_service` (running now) | 2026-08-31, confirmed live: `ps aux` shows pid 63349 | `agent/memory_service/` | no re-run of the resume/conflict/revocation tests this pass | phase2.md |
| Cross-device resume (two `AgentLoop`s, one session) | tested (prior session only) | none re-run this pass | prior session | phase2.md's own account | **cross-device on physically separate machines never tested — phase2.md says so itself** | phase2.md, 04-integration-prompt.md §DOCUMENT MAP item 3 |
| MCP tool subprocess mode | implemented | none re-run this pass | prior session | `agent/mcp_tools_server.py`, `agent/mcp_tool_client.py` | none new found | phase2.md |

## Phase 3 — Broker, RoE, scope enforcement

| capability | status | test command | last verified | evidence | gaps | source |
|---|---|---|---|---|---|---|
| Policy bypass suite (IPv4/IPv6/DNS/redirect/rebinding/fail-closed) | **tested, re-run this pass** | `python3 -m agent.broker.test_policy_bypass` | 2026-08-31T05:32Z | **15/15 passed, output captured this session** | none found this pass | phase3.md |
| Kill switch (dispatch-time + mid-scan) | implemented, tested prior session | not re-run this pass | prior session | `agent/broker/kill_switch.py` | not re-verified this pass | phase3.md |
| `http_recon` (HTTP only, as documented) | implemented, tested | included in policy bypass suite | 2026-08-31 | `agent/security_tools/http_recon.py` | see HTTPS row below — partially superseded | phase3.md |
| `port_discovery` (hand-rolled TCP connect scan) | implemented, tested | included above | 2026-08-31 | `agent/security_tools/port_discovery.py` | no nmap wrapper exists — the review docs' "-oJ doesn't exist" correction is noted but **does not currently apply to any code in this repo**, since nmap is not wrapped anywhere yet | 02-review-of-hackerai-proposals.md §3.5 |

## Phase 4 — Sandbox, evidence, findings, eval

### M4.1 — bubblewrap isolation

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Filesystem escape prevention | **tested, expanded this pass** | `python3 -m agent.sandbox.test_isolation` | 2026-08-31T05:32Z (implemented and passing, this session) | 5/5 filesystem+symlink checks passed (`/etc/shadow`, host home dir, symlink-to-unbound-path denied, symlink-to-bound-path still works, workspace itself reachable) | IPv6-specific egress case and mount-propagation-specific case still not separately tested (subsumed structurally by `--unshare-all`, but no dedicated test asserts it) |
| Inherited file descriptors | **tested, new this pass** | same command | 2026-08-31 | `/proc/self/fd` inside the sandbox shows only stdio + bwrap's own listing fd | none found |
| Unix domain socket reachability | **tested, new this pass** | same command | 2026-08-31 | Docker socket (`/var/run/docker.sock`) unreachable — no `/var/run` bind | none found |
| PID namespace isolation (ptrace surface) | **tested, new this pass** | same command | 2026-08-31 | sandboxed process is PID 1/2 in its own namespace; cannot see or ptrace host processes structurally, not by permission denial | genuine `ptrace()` syscall-level denial (vs. namespace-driven invisibility) not separately tested — namespace isolation already makes the practical attack moot, but the two are different mechanisms |
| TIOCSTI / controlling-TTY injection | **tested, new this pass** | same command | 2026-08-31 | no controlling TTY exists inside sandbox (`/dev/tty` open fails) — closes this escape class structurally | none found |
| Nested unprivileged user namespace | **tested, informational, new this pass** | same command | 2026-08-31 | this host's kernel currently allows nesting; documented as host-kernel-policy-dependent and not itself a privilege escalation (a nested unprivileged namespace can't gain capabilities in the outer one) | not scored pass/fail by design — recorded as a known characteristic |
| Signal delivery / cancellation | **tested, new this pass** | same command | 2026-08-31 | `SIGTERM` to the `bwrap` process kills the sandboxed child too; no orphan left running; confirmed reliable across 5 consecutive full-suite runs | none found (after the vacuous-test fix below) |
| Parent-death (`--die-with-parent`) | **tested, new this pass** | same command | 2026-08-31 | confirmed: sandboxed child does not outlive an exited parent | none found (after the vacuous-test fix below) |
| Network egress prevention | **tested** | same command | 2026-08-31T05:32Z | 2/2 network checks passed | IPv6-specific egress case still not separately tested |
| Resource limits (RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE) | **tested, expanded this pass** | same command | 2026-08-31T05:32Z | 3/3 resource checks passed, including a **new disk-fill test** (100MB write capped at 50MB by a newly-added `RLIMIT_FSIZE`) | **RLIMIT_NOFILE set but not exercised by a test. Fork-bomb/process-count containment attempted with `RLIMIT_NPROC` and reverted — see gap below, this is a real finding from this pass, not a pre-existing known limitation** |
| Fork-bomb / process-count containment | **not implemented — two independent attempts this pass, both reverted with evidence** | attempted #1: `RLIMIT_NPROC`; attempted #2: cgroup v2 `pids.max` via `systemd-run --user --scope` | 2026-08-31 | **Attempt 1:** `RLIMIT_NPROC=32` via `preexec_fn` broke bubblewrap itself (`Resource temporarily unavailable`) — it counts against the real UID system-wide, not per invocation. **Attempt 2:** wrapping every bubblewrap invocation with `systemd-run --user --scope -p TasksMax=48` correctly capped a standalone fork-bomb test, but introduced a reproducible race in the signal-delivery/cancellation path — `SIGTERM` to the wrapped process sometimes (observed in ~50% of `agent.sandbox.test_isolation` runs) failed to cascade down to the sandboxed child, **leaking orphaned sandboxed processes on the host** that then persisted across subsequent test runs until manually killed. This is a regression in a property that was previously 100% reliable (`SIGTERM`/`--die-with-parent` both passing solidly before this attempt). Reverted rather than shipped; both attempts and the exact failure mode are documented in `agent/sandbox/executor.py`'s comments and ADR-0003. | **Real, currently-open gap**, now with two ruled-out approaches on record so a future attempt doesn't re-try either. Next candidate: write to a self-owned cgroup v2 `pids.max` file directly (cgroupfs), not delegated through `systemd-run`'s own process/scope lifecycle — untried this pass. |
| seccomp filtering | **not implemented** | — | — | `grep seccomp agent/sandbox/executor.py` → no matches (verified this pass) | Confirmed gap. Both HackerAI review and Codex flag this explicitly; bubblewrap's own docs say the tool provides no security by itself without arguments that define a model — rlimits alone are not a seccomp filter |
| bubblewrap version pinning | implemented informally | `bwrap --version` | 2026-08-31 | `0.11.1-1ubuntu0.1` (Ubuntu security-updates channel, confirmed via `apt-cache policy` this pass) | Not pinned by digest/lockfile anywhere in the repo — relies on whatever the OS package manager currently has installed |
| `direct` isolation tier as non-default | **not done** | `grep 'isolation_tier: str = "direct"' agent/loop.py` | 2026-08-31 | confirmed: default is still `"direct"`, not `"bubblewrap"` | **01-security-agent-main-direction.md and local-security-agent-review-phase4.md both call this out as something that should change.** This is a real, unresolved disagreement with the reviewers, tracked as ADR-0004. |

### M4.2 — Evidence and audit lifecycle

**Status: gaps closed and tested this pass** (`python3 -m agent.evidence.test_lifecycle`, 13/13
passing). Real production data (the two Juice Shop findings' evidence blobs, referenced from
`agent/state/findings/lab-default.jsonl`) went through a live format migration during this work
— verified decryptable both immediately after migration and after a subsequent key rotation, no
data loss.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Content-addressed, Fernet-encrypted evidence store | tested | `python3 -m agent.evidence.test_lifecycle` | 2026-08-31 | round-trip + tamper detection re-verified this pass | none found |
| Tamper detection on read (decrypt → re-hash → compare) | tested | same command | 2026-08-31 | bit-flip test passes | none found |
| Key versioning + rotation | **implemented and tested this pass** | same command | 2026-08-31 | `rotate_key()`; content encrypted under an old version stays decryptable after rotation, verified with a real cross-version test | key still lives in one JSON file on disk, not HSM/KMS-backed — stated limitation, not hidden |
| Deletion tombstone | **implemented and tested this pass** | same command | 2026-08-31 | `EvidenceStore.delete()`; raises `EvidenceDeletedError` (distinct from "never existed"), tombstone stays queryable, double-delete is idempotent | none found |
| Retention job | **implemented and tested this pass** | same command | 2026-08-31 | `run_retention_job()`; correctly expires a backdated "short"-class blob, correctly leaves an "indefinite"-class blob alone | no scheduler wired up to run it periodically — must be invoked manually or by an external cron for now |
| Restore from backup | **demonstrated and tested this pass** | same command | 2026-08-31 | a copied evidence directory decrypts correctly from a fresh `EvidenceStore` instance pointed at it | file-copy-level, not an automated backup system |
| Content ID = plaintext SHA-256 | unchanged (as designed) | — | — | `agent/evidence/store.py` `hashlib.sha256(data)` | Still ADR-0005-open — HMAC/keyed content ID remains an owner decision, not resolved this pass (changing it now would need the same live-migration care just exercised for the key format) |
| Canonical audit serialization + external/signed checkpoint | unchanged, partially implemented | — | `agent/audit_log.py` writes a separate `.checkpoints.jsonl` file | still a second **local** file, not signed/external | Not addressed this pass — next M4.2 increment if picked up again |

### M4.3 — Findings

**Status: schema expanded and tested this pass**, plus a real correction to the review docs'
assumption about Unicode/Thai PDF support.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Expanded `Finding` schema (confidence, status, demonstrated_impact, cvss_version/vector, cwe_ids, cve_ids, affected_component, preconditions, observation_refs, reproduction_recipe_ref, references, first_seen/last_verified/verifier, limitations) | **implemented and tested this pass** | ad hoc (see docs/STATUS.md history; not yet a committed pytest file) | 2026-08-31 | `agent/findings/model.py`; new fields default to the most conservative values (`needs_validation`), not `confirmed` — a model still can't self-promote a finding | the observation → hypothesis → verification → finding *separation* (distinct tables/pipeline stages) still doesn't exist — this is schema readiness for it, not the pipeline itself, which is Phase 5 work |
| Backward compatibility with pre-expansion records | **verified this pass** | — | 2026-08-31 | the two real Juice Shop findings (written under the old schema) load correctly under the new one, all new fields default sensibly | none found |
| SARIF renderer | updated and tested this pass | — | 2026-08-31 | `agent/findings/sarif.py` now surfaces every new field via SARIF's `properties` bag | none found |
| Markdown/PDF renderer — Unicode/Thai support | **implemented and tested this pass, correcting the reviewers' assumption** | — | 2026-08-31 | `agent/findings/report.py` now uses real Unicode TTF fonts (`Noto Sans` + `Noto Sans Thai`, already installed system-wide via Ubuntu's font packages — no download needed) with per-script-run font switching (fpdf2 has no automatic glyph fallback; Noto Sans Thai has *zero* Latin/digit/punctuation glyphs, confirmed via `fontTools`). Verified with real mixed Thai/English content, extracted back out with `pdftotext` and read correctly. Replaces the earlier ASCII-transliteration approach entirely. | none found — this closes the gap the reviewers flagged, rather than partially addressing it |
| "No finding" as a valid pipeline outcome | N/A — no pipeline exists yet | — | — | — | Cannot be a gap in code that doesn't exist yet (see Phase 5 section); flagged so it isn't forgotten when the pipeline is built |

### M4.4 — Evaluation harness

**Status: rebuilt as a real 4-layer harness this pass**, replacing the single 3-task/single-seed
suite the reviewers correctly flagged as insufficient. `agent/eval/harness.py` now orchestrates
`agent/eval/layers/{deterministic,model_tool,security_reasoning,milestone}.py`:

- **Layer 1 (deterministic):** aggregates the policy/sandbox/evidence/injection-quarantine test
  suites (4 subprocess-run suites).
- **Layer 2 (model/tool under ambiguity, multi-seed):** 5 new task cards — no-call-when-
  underspecified, distractor-tool selection, denial-not-retried, truncated-output awareness,
  unknown-capability-not-fabricated — each run across 3 seeds (42, 123, 7), 15 trials total.
  This is the layer that actually answers "3/3 isn't evidence of judgment under ambiguity,"
  since it's the first eval work in this project testing exactly that.
- **Layer 3 (security reasoning proxies):** 2 tasks — no unverified vulnerability claims,
  evidence-grounded summaries.
- **Layer 4 (milestones):** the original 3 Juice Shop tasks plus 2 new DVWA tasks — **DVWA was
  pulled and stood up this pass** (`docker run ... vulnerables/web-dvwa`, bound to
  `127.0.0.1:3080` only, same loopback-only pattern as Juice Shop) as a second, independent lab
  target, since a milestone pass against only one app risks measuring "memorized this app's
  quirks" rather than general capability.
- A **failure taxonomy** classifies every failed trial (`infrastructure_or_budget`,
  `loop_detection_triggered`, `unexpected_status:*`, `model_judgment_error`) rather than just
  counting pass/fail.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| 4-layer harness (deterministic/model-tool/reasoning/milestone) | **implemented this pass** | `python3 -m agent.eval.harness` | 2026-08-31 (run in progress/completed — see harness run notes) | `agent/eval/harness.py`, `agent/eval/layers/` | layer 2/3 task sets are a starting set (5 + 2 tasks), not exhaustive — more ambiguity/reasoning task cards is always more evidence, this is a floor not a ceiling |
| Reproducibility metadata (model/seeds/policy version/template hash/targets) | implemented and expanded | — | — | now records `seeds_used` (plural) and both lab targets, not just one | none beyond the coverage gap above |
| Real operational finding during this build: `REQUEST_TIMEOUT_S` was too tight | **found and fixed this pass** | — | 2026-08-31 | a real generation exceeded the old 120s client-side timeout (server confirmed healthy immediately after, not a hang) — bumped to 300s in `agent/config.py` | none — this was a genuine latent bug the expanded harness's longer/more-varied prompts actually triggered, that the original 3-task suite never exercised |

**First full clean run of the rebuilt harness** (`agent/state/eval_results/eval-1788160029.json`)
surfaced two more real bugs, both fixed and re-verified this pass — reported here rather than as
a clean first-run success, since the point of expanding this harness was exactly to catch things
like this:

1. **Layer 1 regression: `agent/broker/test_policy_bypass.py` crashed** (`TypeError:
   side_effect() takes 3 positional arguments but 5 were given`). Root cause: M4.6's HTTPS work
   added `verify_cert`/`ca_bundle_path` parameters to `http_recon._one_hop()`/`run()`, and this
   test's mock of `_one_hop` was never updated to accept them — a real drift regression the
   expanded Layer 1 aggregation caught that ad hoc manual testing during M4.6 had missed. Fixed
   by widening the mock's signature; suite now passes 15/15 (was silently reporting a stale
   9-check baseline before the drift).
2. **Layer 3 vacuous-*fail* bug** (the mirror image of the three vacuous-*pass* bugs found
   earlier in `test_isolation.py`): `no_unverified_vulnerability_claim`'s overclaim check did a
   raw substring match for phrases like `"is vulnerable to sql injection"` with no negation
   awareness — so the model's actually-correct, properly hedged answer ("I cannot determine if
   the login page ... is vulnerable to SQL injection **without further investigation**") was
   marked FAIL, because the phrase is a literal substring of that hedged sentence. A per-sentence
   negation check doesn't work either here: the target URL `127.0.0.1:3000` contains periods a
   naive sentence-boundary search treats as sentence ends, which would cut the hedge text out of
   the check window. Fixed with a whole-message hedge-marker check (`agent/eval/layers/
   security_reasoning.py`'s `_has_unhedged_overclaim()`) — coarser, but doesn't have that
   failure mode; added `agent/eval/layers/test_security_reasoning.py` (5/5) as a permanent
   regression test, since this layer previously had none of its own.

**Re-run after both fixes** (`agent/state/eval_results/eval-1788161728.json`): clean —
Layer 1 4/4 suites (64/64 individual checks), Layer 2 15/15 trials, Layer 3 2/2, Layer 4 5/5
milestones, no failures in the taxonomy. This is the first fully clean run of the rebuilt
4-layer harness.

### M4.5 — Injection quarantine connected to broker approval

**Status: implemented and tested this pass** (`python3 -m agent.broker.test_injection_quarantine`,
20/20 passing) — the confirmed cross-cutting gap from the original audit is closed.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Verdict taxonomy (`clean\|suspicious\|malicious\|unknown`, not binary match/no-match) | **implemented and tested this pass** | `python3 -m agent.broker.test_injection_quarantine` | 2026-08-31 | `agent/injection_guard.py`; 10/10 taxonomy cases pass including encoded (base64/entropy), homoglyph (Cyrillic і vs Latin i), zero-width-character fragmentation, and multilingual (Thai/Spanish/French/German) phrasing | pattern lists are a starting set, not exhaustive — same caveat every regex-based detector has, stated explicitly in the module's own docstring |
| Non-match ≠ allow (an "unknown" verdict for ambiguous encoded content, not silently "clean") | **implemented and tested this pass** | same command | 2026-08-31 | a high-entropy base64-looking blob with no other signal correctly returns `unknown`, not `clean` | none found |
| Cross-turn taint tracking connected to broker approval | **implemented and tested this pass** | same command | 2026-08-31 | `agent/broker/taint.py`; a session that scans suspicious/malicious/unknown content is escalated to require human approval for any action beyond `passive_recon` for a 5-minute window — verified end-to-end through the real broker (untainted passive_recon needs no approval; tainted active_scan_light does; the approval prompt names the taint reason) | session-wide time-window taint, not precise per-argument dataflow tracing (not observable without instrumenting the model's own reasoning) — stated as a deliberate scope limit in the module's docstring, not hidden |
| "Same model summarizes dangerous text then considers it safe" bypass | N/A — doesn't apply to this codebase | — | — | Phase 1–4 never implemented a second-pass summarization step for flagged content (only marker-wrapping) — the reviewers' concern is about a design this project doesn't have, not a hole in one it does | flagged so it isn't forgotten if a summarization/quarantine-rewrite step is added later |

### M4.6 — HTTPS completion in `http_recon`

**Status: implemented and tested this pass** (`python3 -m agent.security_tools.test_http_recon_https`,
8/8 passing) — closes the gap identified in this same audit's earlier HTTPS correction (core
mechanism already worked; these are the remaining pieces the reviewers' fuller M4.6 spec asked for).

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Certificate verification (self-signed, SAN mismatch, expired) all correctly rejected | **implemented and tested this pass, with real local TLS test servers** | `python3 -m agent.security_tools.test_http_recon_https` | 2026-08-31 | 3 dedicated test certs generated (self-signed, SAN-mismatched, expired), all correctly rejected by default `verify_cert=True` | none found |
| Custom per-engagement CA bundle | **implemented and tested this pass** | same command | 2026-08-31 | a CA-signed leaf cert fails without the CA bundle, succeeds once `ca_bundle_path` is supplied | none found |
| `verify_cert=False` gated as a distinct, harder-to-reach action class | **implemented and tested this pass** | same command | 2026-08-31 | new `http_recon_insecure` action class: denied by default RoE (before even reaching approval), requires an explicit RoE opt-in *and* human approval, approval prompt names the TLS risk, human decline denies — all 3 paths tested through the real broker | none found |
| Proxy environment variables never consulted | **verified this pass** | same command | 2026-08-31 | bogus `HTTP_PROXY`/`HTTPS_PROXY` set, request succeeds identically — `_PinnedConnection.connect()` dials the socket directly, bypassing `http.client`'s normal proxy-aware path entirely, so there was never a proxy-env leak to begin with | none found |
| IPv6 literal handling | **verified this pass** | same command | 2026-08-31 | an IPv6 literal target is handled without crashing | not tested against a real IPv6-listening service (no such lab target exists yet) — only that it fails cleanly rather than raising |
| **Real bug found and fixed this pass:** unhandled `OSError`/`ConnectionRefusedError` crashed the whole call | **found and fixed this pass** | same command (the IPv6 test case triggered it) | 2026-08-31 | `_one_hop()` only caught `ssl.SSLCertVerificationError`; any other socket-level failure (connection refused, reset, unreachable) propagated uncaught. Fixed with a generic `except OSError` branch returning a clean `{"ok": False, ...}` result, matching every other tool's error-handling convention in this codebase | none — this was a real robustness gap for a *recon* tool, which by definition probes things that may not be listening |
| Decompression-bomb protection | **verified as already safe by design, documented this pass** | — | — | `http.client` never auto-decompresses; the body-read cap (`resp.read(MAX_BODY_BYTES)`) bounds raw bytes read regardless of any `Content-Encoding` claim — there is no expansion step in this code path for a bomb to exploit | not exercised with an actual gzip-bomb response this pass (reasoned from the code path, not empirically fuzzed) |

### M4.7 — Prompt registry skeleton

**Status: implemented, wired in, and tested** (17/17, `python3 -m agent.prompts.test_compiler`;
seam directories populated and wired into the live loop in a later pass, see below) — closes the
confirmed gap ("system prompt is one hardcoded string").

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Layered, versioned prompt files (`agent/prompts/manifest.yaml` + 6 fixed layers) | **implemented** | `python3 -m agent.prompts.test_compiler` | 2026-08-31 | core_identity/authorization/data_provenance/action_protocol/reporting (static) + engagement (Jinja2 `.j2`, dynamic); content migrated from the previous hardcoded `_system_message()` string, not newly invented policy | none found |
| Deterministic compile + digest | **implemented and tested** | same command | 2026-08-31 | compiling the same context twice yields an identical digest and identical per-layer digests; changing one variable changes only the affected layer's digest, not the others | none found |
| Strict template variables (no silent blanks) | **implemented and tested** | same command | 2026-08-31 | `jinja2.StrictUndefined` — an unset variable raises `UndefinedError` rather than rendering empty | none found |
| Budget check against the live model | **implemented and tested** | same command | 2026-08-31 | compiled prompt's real rendered token count checked against the server's actual `n_ctx` — 686 tokens in a 16384-token window in this run | none found |
| **Actually wired into the agent loop** (not just a standalone module) | **implemented and tested** | ad hoc, see build history | 2026-08-31 | `agent/loop.py`'s `_system_message()` calls the compiler instead of building a hardcoded string | none found |
| `prompt_version` recorded in the audit trail and eval metadata | **implemented and tested** | ad hoc / `python3 -m agent.eval.harness` | 2026-08-31 | `agent/audit_log.py`'s `record()` gained a `prompt_version` field; verified a real audit entry's `prompt_version` matches `loop.prompt_version` exactly; also recorded in `agent/eval/harness.py`'s reproducibility block | none found |
| Phase-specific prompts (`agent/prompts/phases/`) — ~~empty seam~~ **populated and wired this pass** | **implemented and tested** | `python3 -m agent.prompts.test_compiler`, `python3 -m agent.test_loop_system_message` | 2026-08-31 | all 6 pipeline phases (`agent.engagement.store.PHASES`) have a real guidance file; `compile_prompt(context, phase=...)` appends it, an unknown phase name raises `UnknownPhaseError` rather than silently omitting guidance; `AgentLoop._current_phase()` looks up the engagement's real M5.2 phase state when one exists (`engagements/<engagement_id>/state.db`) and passes it through — verified both that a real phase's guidance appears in the compiled system message and that a missing/corrupt state store degrades to no phase context (logged, never crashes the loop) | the common case today (`engagement_id="lab-default"` with no M5.1-created engagement directory) has no phase state, so most live sessions still compile with no phase layer — this wiring is real but only activates once a session actually uses an M5.1/M5.2-backed engagement |
| Tool cards (`agent/prompts/tools/`) — ~~empty seam~~ **populated and wired this pass** | **implemented and tested** | same commands | 2026-08-31 | cards written for the 5 tools live in the model-callable surface today (`run_command`, `read_file`, `write_file`, `http_recon`, `port_discovery`); `compile_prompt(context, active_tools=...)` appends only cards that exist, silently skipping unknown/uncarded tool names (this is normal, not an error — coverage is allowed to be partial); `AgentLoop._system_message()` now passes `active_tools` derived from `self.tool_schemas`, so a session only gets cards for tools it can actually call — verified base-tool cards always appear and security-tool cards appear only when `use_security_tools=True` | 2 of 4 M5.5 internet channels (`knowledge_search`, `knowledge_fetch`) and the browser service aren't yet exposed as model-callable tool schemas at all (they're library-level dispatcher functions, not wired into `TOOL_SCHEMAS`), so they have no tool card yet either — writing a card for a tool that doesn't exist in the callable surface would be premature |
| Backward compatibility of the phase/tool-card addition | **implemented and tested** | `python3 -m agent.prompts.test_compiler` | 2026-08-31 | `compile_prompt(context, phase=None, active_tools=None)` produces a byte-identical digest to the original no-argument call — verified directly, not assumed; every pre-existing recorded `prompt_version` from before this change remains a valid, reproducible digest for what it actually was | none found — note this does NOT mean live sessions get identical prompts after this change: `AgentLoop` now always passes `active_tools` (never omits it), so real sessions' compiled prompts and `prompt_version` digests changed starting this pass, which is the intended effect, not a regression |

## Phase 5 — Engagement Intelligence + Pipeline + Internet

### M5.1 — Structured engagement intake

**Status: implemented and tested this pass** (14/14, `python3 -m agent.engagement.test_intake`).

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| `EngagementIntake` dataclass + validation | **implemented and tested** | `python3 -m agent.engagement.test_intake` | 2026-08-31 | rejects missing sign-off, empty allow_targets, empty action classes, bad time window, malformed target, invalid retention class; accepts a valid spec | rate_limit_notes/blast_radius_notes/approval_policy are captured as metadata only — not yet enforced anywhere (see gap row below) |
| `create_engagement()` writes `roe.json`/`scope.txt`/`deny.txt` under `engagements/<id>/` | **implemented and tested** | same command | 2026-08-31 | files created with expected content; invalid intake writes nothing (no partial directory); duplicate engagement_id rejected | none found |
| Interop with the existing single-engagement broker/policy code | **implemented and tested** | same command | 2026-08-31 | `load_policy(engagement_dir=<newly created dir>)` — the existing, unmodified Phase-3 loader — successfully loads a freshly created engagement and reports the correct `engagement_id`/`allowed_action_classes`/`allow_hostnames`; `config.ENGAGEMENT_DIR` (the legacy default) and every existing broker/policy test are untouched | none found |
| Base hard-deny (cloud metadata endpoints) always present | **implemented and tested** | same command | 2026-08-31 | every created `deny.txt` includes `169.254.169.254/32` and the EC2 IPv6 metadata link-local address regardless of operator input | not yet extended to other cloud providers' metadata addresses (GCP/Azure/DO use the same IPv4 address so this is lower-priority than it looks, but not verified) |
| CLI (`python3 -m agent.engagement.cli --from-json <spec>` or interactive) | **implemented, smoke-tested** | ad hoc (`--from-json` against a real temp spec, verified files written, then cleaned up) | 2026-08-31 | not part of the committed automated test suite — interactive path in particular is untested by anything but manual reasoning | no automated test of the interactive prompt path itself |
| Rate/blast-radius **enforcement** per engagement | **not implemented — documented gap, not silently implied solved** | — | — | `rate_limit_notes`/`blast_radius_notes` ride along in `roe.json` as free text; the broker still enforces only the single global `config.ACTION_CLASS_COOLDOWN_S` dict, not a per-engagement value | wiring this into the broker is real work, correctly scoped out of M5.1 (intake) and left for whoever builds per-engagement broker config, likely alongside M5.2/M5.3 |

### M5.2 — Authoritative engagement-state store

**Status: implemented and tested this pass** (29/29, `python3 -m agent.engagement.test_store`) —
closes the gap every reviewer independently called the single biggest ("Asset/target-state
store" and "Hypothesis/coverage/observation store" rows above).

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Asset/service/endpoint/relationship model (SQLite, `engagements/<id>/state.db`) | **implemented and tested** | `python3 -m agent.engagement.test_store` | 2026-08-31 | upsert semantics verified (re-adding the same asset/service/endpoint updates rather than duplicates, unique constraints on asset_type+identifier / asset+port+protocol / asset+method+path); relationship inserts are idempotent | no automated ingestion from tool output yet — recon tools don't write into this store on their own, that wiring is M5.3's job, not M5.2's |
| Observations, evidence-linked | **implemented and tested** | same command | 2026-08-31 | `evidence_ref` carries an evidence-store content ID rather than a content copy, verified round-trip | none found |
| Hypothesis board (priority, preconditions, evidence_for/against, coverage_surface, owner) with optimistic concurrency | **implemented and tested** | same command | 2026-08-31 | version-mismatch update correctly raises `ConflictError` reporting the real current version (same pattern as `agent/memory_service/db.py`'s session version, reused not reinvented per the ROADMAP's own instruction); status enum validated | not yet exposed as its own tool/UI surface — M5.4 |
| Coverage ledger (surface × technique → status) | **implemented and tested** | same command | 2026-08-31 | upsert-by-(surface,technique), summary counts by status verified | none found |
| Phase state machine (`INTAKE→RECON→ANALYSIS→VALIDATION→REPORT→CLOSEOUT`), forward-only, with history | **implemented and tested** | same command | 2026-08-31 | backward transition correctly rejected (`InvalidTransitionError`); stale-version transition correctly conflicts; history table records entered/exited/exit_reason per phase | the criteria/budget-driven *automatic* transition logic (deciding *when* to move phases) is M5.3's job — this milestone only provides the state machine primitive itself, not policy for driving it |
| Task tracking with optimistic concurrency | **implemented and tested** | same command | 2026-08-31 | create/list/update-with-version-check all verified, including a stale-version conflict case | none found — task *scheduling* (what creates tasks, in what order) is M5.3 |
| Findings lifecycle (draft→validated→reported→retested→closed) + retest records + mandatory closeout | **implemented and tested** | same command | 2026-08-31 | `close_engagement()` counts outstanding (non-closed) findings rather than silently dropping them, matches the Phase 6 "mandatory closeout" requirement structurally; write-once (double-closeout rejected); retest correctly transitions status and appends to history | this table indexes/tracks lifecycle only — the actual `Finding` records still live as files via `agent/findings/model.py`; no code yet keeps the two in sync automatically (that's report-generation/pipeline wiring, M5.3+) |
| State survives process restart | **implemented and tested** | same command | 2026-08-31 | re-opening a store pointed at the same directory sees prior assets/phase state correctly (real file-backed SQLite, not in-memory) | none found |

### M5.3 — Two pipeline profiles on the common super-state

**Status: implemented and tested this pass** (16/16, `python3 -m agent.pipeline.test_orchestrator`).

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Web/API and Network profiles sharing one phase state machine | **implemented and tested** | `python3 -m agent.pipeline.test_orchestrator` | 2026-08-31 | `agent/pipeline/profiles.py` — both profiles differ only in RECON task templates (`http_recon` vs `service_fingerprint`), ANALYSIS/VALIDATION/REPORT/CLOSEOUT identical since they operate on shared state, not protocol-specific tool output | only 2 profiles exist; a third pipeline shape would need a new `TaskTemplate` list, not new orchestrator code — not itself a gap, just noting the extension point |
| Idempotent, incremental task planning from state (`plan_tasks()`) | **implemented and tested** | same command | 2026-08-31 | re-planning after no state change creates nothing; a newly discovered asset/service on replan creates exactly the new tasks it warrants (verified for both an asset appearing mid-RECON and a service appearing mid-RECON in the network profile) | none found |
| Criteria/budget-driven phase transition, **never "must find a vulnerability"** | **implemented and tested** | same command | 2026-08-31 | explicit test asserts ANALYSIS is reachable-complete with **zero** hypotheses created; a `failed` task counts as terminal (doesn't block the pipeline forever); wall-clock budget exhaustion forces readiness even with tasks still pending, directly correcting `phase5-design-detail.md`'s rejected "≥1 confirmed finding" exit criterion | budget is wall-clock + max-tasks-per-phase only; no token/dollar-cost budget dimension yet (the ROADMAP's "budget-driven" language doesn't specify which budget, this covers the two easiest/most obviously-needed ones) |
| Hypothesis-driven VALIDATION task generation | **implemented and tested** | same command | 2026-08-31 | only `open`/`testing` hypotheses get a `validate_hypothesis` task; a `confirmed`/`refuted` hypothesis is correctly excluded from re-planning | none found |
| Full profile run reaches CLOSEOUT | **implemented and tested** | same command | 2026-08-31 | one asset, all tasks marked done as the test drives them, profile correctly walks `INTAKE→RECON→ANALYSIS→VALIDATION→REPORT→CLOSEOUT` end to end | this is the *planning/gating* layer only — it does not execute a task against a real tool or feed a real tool result back into the store; that live wiring into `agent/loop.py`'s broker-mediated tool dispatch (task → actual `http_recon`/`port_discovery` call → observation/asset/service update → task status update) is real, not-yet-done integration work, called out explicitly rather than implied solved |

### M5.4 — Hypothesis board + skill library

**Status: implemented and tested this pass.** The hypothesis board itself is `agent.engagement.
store.EngagementStore`'s `hypotheses` table (M5.2, above — priority/preconditions/evidence_for/
evidence_against/coverage_surface/owner, version-checked updates); this milestone adds the skill
library half (18/18, `python3 -m agent.skills.test_library`).

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Hypothesis board (priority, preconditions, evidence_for/against, coverage_surface, owner) | ~~not implemented~~ **RESOLVED — see M5.2** | `python3 -m agent.engagement.test_store` | 2026-08-31 | HackerAI's A1 proposal, reviewed and accepted with the additions listed above | not yet exposed as its own tool the model calls directly — currently only reachable via `EngagementStore`'s Python API |
| Skill schema + read-only, signature-verified loader | **implemented and tested this pass** | `python3 -m agent.skills.test_library` | 2026-08-31 | `agent/skills/library.py`'s `SkillLibrary.load_all()` — a valid signed skill loads; a missing/invalid/tampered signature is reported as a problem (never silently dropped) and the skill excluded; `verify=False` exists only for tooling, not the production load path | signature scheme is a local HMAC key (`agent/skills/signing.py`), i.e. a local-integrity guarantee, not a multi-party PKI trust chain — documented as an intentional scoping choice for a single-operator self-hosted project, not an oversight |
| Offline, human-run authoring/signing CLI (`python3 -m agent.skills.cli`) | **implemented, smoke-tested** | ad hoc (`new` then `verify-all`, cleaned up) | 2026-08-31 | this is the *only* write path into `agent/skills/library/` — nothing reachable from a live engagement (pipeline orchestrator, agent loop, any tool) has a write method into the library, so target/model content structurally cannot end up there, matching the ROADMAP's "target/model content never writes to the library that production uses" | no automated test of the CLI itself (only manual smoke test) |
| Phase/profile-scoped skill retrieval (`for_phase_and_profile()`) | **implemented and tested** | `python3 -m agent.skills.test_library` | 2026-08-31 | scoped skills only match their declared phase+profile; an unscoped skill (empty lists) matches everything, verified both ways | not yet wired into the pipeline orchestrator or agent loop — `PipelineOrchestrator` doesn't call this yet when generating/executing a task |

### M5.5 — Internet access, four separate channels

**Status: all four channels implemented and tested; `knowledge_search`/`knowledge_fetch` live
egress subsequently enabled at the owner's explicit request (ADR-0007)** — 34/34 across
`agent.internet.{test_budget,test_policy,test_cache,test_osint,test_dispatcher}`, including two
real-network integration tests (`TestLiveEgress`) that skip gracefully rather than fail if the
local SearXNG container isn't running on a given machine. This shipped in two stages, both asked
of the owner rather than decided unilaterally, since real internet egress is a first for this
project (everything through M5.4 is strictly loopback/lab-scoped) and squarely matches the
integration prompt's own "network-exposure" ask-the-owner category: first scaffolding-only (build
policy/budget/cache/provenance/quarantine for all 4 channels, wire only `target_http`), then —
after the owner asked to reconsider — live egress with `knowledge_search` pointed at a self-hosted
SearXNG container and `knowledge_fetch` enabled alongside it, both explicit choices among stated
alternatives, not inferred.

**New local infrastructure this pass:** `docker run -d --name localai-searxng -p
127.0.0.1:8888:8080 searxng/searxng`, loopback-bound (same pattern as the Juice Shop/DVWA lab
containers), with `search: formats: [html, json]` added to the container's `/etc/searxng/
settings.yml` (off by default upstream) so `/search?format=json` is reachable.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| `target_http` — RoE-scoped, fully wired | **implemented and tested** | `python3 -m agent.internet.test_dispatcher` | 2026-08-31 | reuses `agent.security_tools.http_recon.run()` and the existing broker/scope-check path unchanged; verified it's actually called (not stubbed), that its result is scanned by `injection_guard`, and that a `malicious`/`suspicious` verdict correctly taints the session via `TaintStore` while a `clean` verdict does not | none found — no new network exposure since this is the same RoE-scoped path other tools already use |
| `knowledge_search` — SearXNG-backed, live | **implemented and tested, real egress enabled** (ADR-0007) | `python3 -m agent.internet.test_dispatcher` | 2026-08-31 | `agent/internet/search.py`'s `do_search()` queries the local SearXNG container's JSON API; a real live test against the running container returned real OWASP-related results (`TestLiveEgress.test_real_searxng_query`); provider allowlist still enforced (`SearchPolicyError` for any non-`searxng-local` provider) and results still flow through budget/cache/injection-quarantine unchanged from the scaffolding pass | single provider (SearXNG); which upstream engines *it* federates across is SearXNG's own `settings.yml` config, not mediated by this project's code |
| `knowledge_fetch` — SSRF-checked, pinned-IP, live | **implemented and tested, real egress enabled** (ADR-0007) | `python3 -m agent.internet.test_dispatcher` | 2026-08-31 | `agent/internet/fetch.py`'s `do_fetch()` reuses `http_recon._PinnedConnection` (dial the validated IP directly, Host/SNI carry the hostname) rather than reimplementing pinned-connection logic; a real live test fetched `https://example.com/` and got a real 200 (`TestLiveEgress.test_real_external_fetch`); every SSRF-range check from the scaffolding pass (loopback/private/link-local/CGNAT/multicast/cloud-metadata, including a DNS-rebinding-style case) still runs unchanged before any real request is made | single-hop only — a redirect is reported, never auto-followed; no HTML/content-type-aware parsing (returns a raw body excerpt, same shape `http_recon` uses) |
| `osint_discovery` — record-only, no network call | **implemented and tested** | `python3 -m agent.internet.test_osint` | 2026-08-31 | `record_out_of_scope()` writes an `observed_out_of_scope:*` observation into `EngagementStore` (M5.2); a structural test confirms the module exposes no fetch/request/probe-named function at all | not yet wired into anything that would actually call it (the pipeline orchestrator/agent loop don't invoke it automatically) |
| Per-channel, per-session budget (`ChannelBudgetTracker`) | **implemented and tested** | `python3 -m agent.internet.test_budget` | 2026-08-31 | query and byte caps enforced independently per channel/session; usage persists across a fresh tracker instance; a real budget-exhaustion test now covers `knowledge_search`'s live path directly (20 successful calls, 21st raises `BudgetExceededError`) | no dollar-cost budget dimension (query/byte only, same scoping note as M5.3's budget) |
| Freshness cache + provenance (`InternetCache`) | **implemented and tested** | `python3 -m agent.internet.test_cache` | 2026-08-31 | put/get round-trips content plus provenance metadata; entries expire by TTL; now verified wired into both live channels (a real `knowledge_search`/`knowledge_fetch` call populates the cache, checked directly) | none found |
| Injection quarantine on channel content | **implemented and tested** | `python3 -m agent.internet.test_dispatcher` | 2026-08-31 | `injection_guard.scan()`/`TaintStore` applied to all three network-touching channels now (`target_http`, `knowledge_search`, `knowledge_fetch`) — this is the first time this path defends against genuinely attacker-influenced content (real internet pages/results) rather than simulated test cases, making M4.5 having shipped before M5.5 a satisfied precondition, not just a sequencing note (ADR-0007) | none found |

**Real bugs found and fixed during this pass:** (1) `ChannelBudgetTracker`'s `state_dir` parameter
had a `Path = BUDGETS_STATE_DIR` default — evaluated once at import time, so
`patch("agent.internet.budget.BUDGETS_STATE_DIR", tmp_dir)` in a test silently had no effect and
an early version of `test_dispatcher.py` leaked 5 real budget-tracking files into
`agent/state/internet/budgets/` before this was caught (found by checking the state directory
after the test run, not assumed clean) and cleaned up. (2) The same bug shape existed in
`InternetCache`'s `cache_dir` default and was fixed pre-emptively (before it could leak) while
wiring the live channels, since both now call `InternetCache()` for real on every request. Both
fixed by resolving the module-level default from the module namespace inside `__init__` instead
of as a bound default argument; re-verified clean with an explicit "does `agent/state/internet/`
exist after the full suite" check.

## Phase 6 — Advanced Web + UX (backend primitives only — the web UI itself is explicitly excluded)

Per the owner's own scoping instruction for this pass ("finish everything until the UI"), this
covers every Phase-6 item except the actual web UI component. Mandatory closeout is not repeated
here — it was already built and tested as part of M5.2 (`EngagementStore.close_engagement()`,
above); everything below is new this pass.

| capability | status | test command | last verified | evidence | gaps |
|---|---|---|---|---|---|
| Isolated Playwright browser service (JS-heavy recon/validation) | **implemented and tested this pass, owner approved the Playwright/Chromium install first** | `python3 -m agent.browser.test_service` (needs the venv active — `playwright` isn't a system package) | 2026-08-31 | `agent/browser/service.py`'s `browser_session()`: ephemeral `browser.new_context()` per session (verified cookies set in one session don't appear in the next); non-root (no privilege escalation anywhere in the path); target-only egress enforced via Playwright request interception validated against the same `scope_check.validate_target()` every other tool uses — verified with a real page fetched from an in-scope local server that itself references an out-of-scope image, confirming the subresource request is actually blocked, not just the top-level navigation | **not wrapped in `BubblewrapExecutor`** (ADR-0006, Accepted) — that profile unshares all namespaces including network (zero network devices), fundamentally incompatible with a tool that needs to reach the target; a network-capable bwrap profile (own netns + veth + nftables egress rules) is real, separate infrastructure work not taken on this pass. Isolation here is process-level non-root + ephemeral context + application-layer egress validation, stated as such rather than implied to match the kernel-level guarantee other tools have. New pinned dependency: `requirements-phase6.txt` (`playwright==1.62.0`), Chromium binary in `~/.cache/ms-playwright/` (not tracked in-repo, matches Playwright's own install model) |
| Self-hosted OOB catcher, correlation-token validation (not source-IP gating) | **implemented and tested this pass** | `python3 -m agent.oob.test_server` | 2026-08-31 | `agent/oob/server.py`'s `OOBServer`, stdlib-only (`http.server`, no new dependency); real HTTP requests (via `urllib`) against a real loopback listener verified: unknown tokens get 404 and are never recorded, known tokens are recorded and validated, and — the actual non-negotiable property — a request with a spoofed `X-Forwarded-For` header is still validated purely by token match, proving source IP is never consulted by `is_valid_interaction()` | bound to `127.0.0.1` only by construction; this project's lab targets (Juice Shop, DVWA) are themselves loopback-bound, so this needed no public exposure — a real external-target engagement would need a publicly reachable OOB host, which is out of scope here (would itself be a "network-exposure" ask-first decision, not assumed) |
| Approval queue (backend primitive) | **implemented and tested this pass** | `python3 -m agent.broker.test_approval_queue` | 2026-08-31 | `agent/broker/approval_queue.py`'s `ApprovalQueue` — submit/list-pending/resolve/wait-for-resolution, file-based, persists across instances; double-resolve correctly rejected; `wait_for_resolution` verified against both a real timeout and a real out-of-band resolution from another thread | **not yet wired into `Broker.dispatch()`** — the existing synchronous `input()` CLI approval prompt (Phase 3, unchanged) remains the only path a live `dispatch()` call actually uses; this queue is a tested, addable primitive a future (out-of-scope-for-this-pass) UI would consume, not yet load-bearing in the live loop |
| Steer channel (backend primitive) | **implemented and tested this pass** | `python3 -m agent.loop_control.test_steer` | 2026-08-31 | `agent/loop_control/steer.py`'s `SteerChannel` — send/peek/take, consumed-once semantics verified, sessions independent | **not yet wired into `agent/loop.py`'s per-iteration read** — deliberately not touched this pass: `agent/loop.py` and its `agent/state/sessions/` files were actively read/written by the concurrently-running eval harness for most of this pass, and this project's own established lesson (an earlier operational mistake this session, documented in the M4.4 build history) is not to touch files under active use by a running background process |
| Session handoff (backend primitive) | **implemented and tested this pass** | `python3 -m agent.engagement.test_handoff` | 2026-08-31 | `agent/engagement/handoff.py` — a thin combinator over the already-authoritative memory-service session state (Phase 2) and `EngagementStore` phase/task/hypothesis state (M5.2), not a new state store; verified it correctly reflects phase transitions and pending-task counts, and that `latest_handoff()`/`list_handoffs()` order correctly | takes `memory_session` as a passed-in dict rather than fetching it directly — deliberate, since the memory service may be a separate remote process (Phase 2's Topology B) this module has no business reaching into itself; a caller (future integration) is responsible for fetching it first |
| Retest mode from versioned recipes (never raw replay) | **implemented and tested this pass** | `python3 -m agent.recipes.test_recipes` | 2026-08-31 | `agent/recipes/{model,store,runner}.py` — a recipe is a list of typed tool invocations from a small fixed allowlist (`http_recon`, `port_discovery`), not raw command/HTTP replay; versions immutable and never overwritten (`RecipeVersionExistsError` on a direct re-save attempt); **the core safety property directly verified**: replaying a recipe against a policy that no longer allows the original target fails closed exactly like a live call would (real `Policy` object, real `scope_check` denial via the real `http_recon.run()`), not a raw replay bypassing current scope; a recipe step smuggling its own `policy` argument is rejected rather than silently overridden | integrates with `Finding.reproduction_recipe_ref` (M4.3) by format (`finding_id@vN`) but nothing yet auto-creates a recipe from a finding — that's a future authoring-tool convenience, not a correctness gap |
| Bounded deterministic parallel executor (low-risk recon only, not multi-agent) | **implemented and tested this pass** | `python3 -m agent.pipeline.test_parallel` | 2026-08-31 | `agent/pipeline/parallel.py`'s `run_bounded()` — results returned in input order regardless of completion order, one task's exception never loses another's result; **real concurrency measurement** (a lock-protected peak-simultaneous-execution counter, not just "it returned the right values") confirms both that it actually runs concurrently (peak > 1) and that `max_workers` is a hard cap (peak never exceeds it) | not yet wired to the pipeline orchestrator (M5.3) to actually run recon tasks in parallel — that wiring depends on the same not-yet-done "task → real tool call" integration M5.3's own gaps row already calls out |
| Mandatory closeout | ~~not implemented~~ **RESOLVED — see M5.2** | `python3 -m agent.engagement.test_store` | 2026-08-31 | `EngagementStore.close_engagement()` — write-once, counts outstanding (non-`closed`) findings rather than dropping them | none beyond what M5.2 already documents |

**Real bug found and fixed while testing M5.4's skill signing during this pass:** the same
default-argument-binding class of bug found once already in `agent/internet/budget.py` recurred
in `agent/skills/signing.py` — `sign_file()`/`verify_file()` had no way to accept an explicit key,
so every skill-signing test silently used and wrote to the *real* production signing key path
(`agent/state/skills/signing_key.bin`), leaking a real key file into project state before this
was caught (found by checking the state directory after a test run, same method as the earlier
`agent/internet/` catch — not assumed clean). Fixed by adding an optional `key` parameter to
`sign_file()`/`verify_file()` and `signing_key` to `SkillLibrary.__init__()`, threading an
explicit test key through every test in `agent/skills/test_library.py`; re-verified clean with an
explicit "does `agent/state/skills/` exist after the full suite" check.

## Post-Phase-6 wiring pass — closing the "built but not connected" gaps

Everything below was explicitly still-pending after the Phase 5/6 backend-primitives pass
(see each milestone's own gaps rows above) — this pass connects those primitives to the live
system rather than leaving them as tested-but-inert libraries. Full eval harness re-run after
this pass (`agent/state/eval_results/eval-1788164965.json`): clean, 4/4 suites, 15/15 trials,
2/2 reasoning tasks, 5/5 milestones — the `loop.py` phase/tool-card change from the prior pass
does not regress the live model flow.

| capability | status | test command | evidence | gaps |
|---|---|---|---|---|
| `knowledge_search`/`knowledge_fetch` exposed as model-callable, broker-mediated tools | **implemented and tested** | `python3 -m agent.broker.test_knowledge_channels` (9/9, includes a real end-to-end call against the live SearXNG container and the actual default engagement) | new `knowledge_search`/`knowledge_fetch` action classes (`agent/broker/broker.py`); default lab `engagement/roe.json` updated to allow both, since live egress for this exact engagement was already authorized (ADR-0007) — completing that decision's wiring, not a new authorization; both correctly denied when an RoE doesn't list them, rate-limited, and taint-escalated like every other broker-mediated tool | `target_http`/`osint_discovery` remain library-only from a model-callable-tool standpoint (`target_http` is redundant with the existing `http_recon` tool; `osint_discovery` is exposed differently, see below) |
| `osint_discovery` exposed as a model-callable tool (`osint_record`) | **implemented and tested** | `python3 -m agent.test_security_mcp_server_osint` (2/2) | not broker-mediated (makes no network call, nothing to gate); degrades to a clear "not recorded, no engagement state store" result rather than crashing or fabricating when `engagement_id` has no M5.1-created directory — the common case today | — |
| Approval queue wired into `Broker.dispatch()` | **implemented and tested** | `python3 -m agent.broker.test_knowledge_channels` (covers both modes) | every approval-required decision now recorded in `ApprovalQueue` regardless of mode (a durable, queryable history even for the default synchronous path); new opt-in `use_approval_queue=True` mode blocks on an out-of-band resolution instead of `confirm_fn()` (verified: an out-of-band approval from another thread is honored; an unresolved request correctly times out and denies) | async mode is off by default — no caller (loop.py, security_mcp_server.py) opts into it yet, since nothing yet provides an out-of-band resolver UI (would need Phase 6's web UI or a CLI companion) |
| Steer channel wired into `agent/loop.py` | **implemented and tested** | `python3 -m agent.test_loop_steer` (4/4) | a pending steer message is injected as a user-role message before the loop's next model call and consumed exactly once; verified for both "queued before the task starts" and the realistic "operator reacts mid-task, message reaches the *next* iteration" case | — |
| Skill library wired into the pipeline orchestrator | **implemented and tested** | `python3 -m agent.pipeline.test_orchestrator` (21/21, was 16/16) | `PipelineOrchestrator(..., skill_library=...)` attaches matching skill_ids to a task's params at creation time (`skills_for_current_phase()` filters by current phase + profile name); a non-matching skill is correctly not attached; omitting `skill_library` reproduces the exact prior behavior (no `skill_ids` key at all) | skill content itself isn't yet fed into a prompt or handed to whatever eventually executes a `review_observations`/`validate_hypothesis` task — the pointer exists, nothing reads it yet |
| Pipeline task execution — the M5.3 gap explicitly called out as "separate, larger integration work" | **implemented and tested against real lab targets** | `python3 -m agent.pipeline.test_executor` (11/11, real Juice Shop container, real default Broker/RoE — not mocked) | `agent/pipeline/executor.py`'s `run_pending_tasks()` connects a planned task to an actual broker-mediated tool call and feeds the result back (services discovered, observations recorded, task marked done/failed with a result). `review_observations`/`validate_hypothesis` are deliberately NOT auto-executed — verified they're left `pending` (not faked) since forming/testing a hypothesis needs the model's judgment, not a deterministic shortcut. `generate_report`/`closeout` are deterministic enough to execute directly (report assembled from `FindingsStore`, `EngagementStore.close_engagement()` called for real) | not yet called from anywhere live (`agent/loop.py` doesn't invoke `run_pending_tasks()` itself) — this is the deterministic execution primitive, not an autonomous scheduler; something still needs to decide *when* to call it |
| **Real bugs found and fixed while building the above** | — | — | (1) `_execute_port_discovery` assumed a `{"ports": [{"port", "status"}]}` output shape that doesn't exist — the real `port_discovery.run()` returns `open_ports`/`closed_ports`/`filtered_ports` as flat lists; caught by the real-target test actually asserting port 3000 showed up, not by a mock that would have echoed the wrong assumption back. (2) Batch-executing multiple same-action-class tasks in one session tripped the broker's own rate limiter (correct behavior for one ad hoc call, wrong for a deliberate batch) — fixed by pacing dispatches in `run_pending_tasks()` rather than loosening the broker's cooldown. (3) The same default-argument-bound-at-import-time bug already found 3 times this session recurred in **`agent/session.py`'s `SessionStore`** and **`agent/audit_log.py`'s `AuditLog`/`verify()`** — both Phase-1 code, never caught before because nothing had previously tried to test-isolate them; found while writing `test_loop_steer.py` (a fixed session-id literal meant the test re-used a real persisted session across repeated runs, so a second run saw 2 steer messages instead of 1) and fixed at the root in both files, not worked around in the test alone. | none — all re-verified: `test_loop_steer.py` now passes twice in a row with an explicit "does agent/state/{sessions,audit}/ show new steer-test-* files after the run" check, reporting clean |
| `browser_fetch` exposed as a model-callable, broker-mediated tool | **implemented and tested against the real Juice Shop container** | `python3 -m agent.broker.test_browser_channel` (4/4) | new `browser_recon` action class (own, not folded into `passive_recon`/`active_scan_light` — full JS execution against the target is a materially different capability), denied by default; verified a real render through the full broker path returns actual post-JS page content (a real title, not a stub) | `BROWSER_FETCH_SCHEMA` lives in `security_mcp_server.py` rather than `agent/browser/service.py` specifically so `loop.py` can list the tool without needing playwright installed — a deliberate structural choice, not an oversight, but worth knowing if `browser_fetch.py`'s schema and its implementation ever need to be found in the same file |
| **All 4 new tools (`knowledge_search`, `knowledge_fetch`, `osint_record`, `browser_fetch`) actually reachable by the model**, not just server-side dead code | **implemented and tested** | `python3 -m agent.test_loop_security_tools_wiring` (9/9) | **found and fixed a real gap while adding tool cards for these 4 tools**: their `@server.tool()` registrations in `security_mcp_server.py` made them dispatchable, but nothing added their schemas to `AgentLoop.tool_schemas` — the model had no way to know they existed at all. Fixed by giving each a `SCHEMA` dict and adding all 4 under `use_security_tools=True` alongside http_recon/port_discovery. Also split `loop.py`'s `BROKER_MEDIATED_TOOLS` into that set (tools that call `Broker.dispatch()` and get their own richer audit entry) and a wider `SECURITY_MCP_TOOLS` (routing to the subprocess) — `osint_record` is in the latter but not the former, since it makes no network call and isn't broker-mediated, so it still needs `loop.py`'s own generic audit entry; verified directly that `osint_record` gets exactly one audit call and `http_recon` gets zero (the broker's own entry is the only one) | — |
| Recipe ↔ Finding linking (`agent/recipes/linking.py`) | **implemented and tested** | `python3 -m agent.recipes.test_linking` (4/4) | `attach_recipe(finding, steps, author)` creates a new recipe version and returns a **new** `Finding` object with `reproduction_recipe_ref` set — `FindingsStore` is append-only by design (Phase 4, same convention as the evidence/audit logs) with no update method, so this deliberately does not mutate a persisted finding; the caller decides how to persist the result. Verified the original `Finding` object is untouched, the ref round-trips through `RecipeStore.load_by_ref()`, and re-attaching creates version 2, not an overwrite | this closes the "no code yet auto-creates a recipe from a finding" gap noted in the earlier Phase 6 pass — but it's still a manual call with explicit `RecipeStep`s (a human/model still decides what reproduces the finding), not automatic derivation from evidence, which would need a way to trace a finding's evidence back to the exact tool call that produced it — not currently possible from the data model alone |

## ADR resolution pass — the 3 previously-open ADRs, all decided and implemented

All three were presented to the owner directly with a recommendation each; all three were
accepted as recommended. Full detail in each ADR file; summarized here.

**Eval harness re-run after implementing all three** (`eval-1788174547.json`, run against the
new `bubblewrap`-by-default + seccomp-active + HMAC-addressed configuration): Layer 1 4/4,
Layer 3 2/2, Layer 4 5/5 — unchanged from before this ADR pass, confirming none of these
security-hardening changes broke the live model/tool flow. Layer 2 showed the same single
failure as the *previous* run before any of this pass's changes existed
(`no_call_when_target_unspecified`, seed=7 specifically — the model fabricates a plausible
target URL instead of asking for one): identical failure, same seed, same task, reproduced
across two separate runs with substantially different code in between. Recorded as a real,
reproducible model-behavior characteristic at that seed for this task — not a regression from
anything in this pass — worth tracking as an eval-layer finding in its own right, not hidden by
re-running until it passes.

| ADR | Decision | Status | test command | evidence |
|---|---|---|---|---|
| **0004** — default isolation tier | `run_command` defaults to `bubblewrap` (was `direct`) at every API surface (`AgentLoop.__init__`, `run_command.run()`, `main.py`'s CLI, `mcp_tools_server.py`'s subprocess default). `direct` remains available but now requires the same typed-confirmation UX `--dangerous-local` already used — a distinct prompt, not a shared one, since the two are independent relaxations. | **Implemented and tested** | `python3 -m agent.tools.test_run_command_default_tier` (2/2) + full `agent.sandbox.test_isolation` (16/16) re-verified against the new default | none found |
| **0003** — seccomp for the bubblewrap tier | A default-allow/explicit-**deny** profile (not a from-scratch per-program allowlist — that still needs real strace-based profiling this pass didn't do), blocking `ptrace`+cross-process-memory syscalls, namespace/mount manipulation, kernel module/BPF loading, and a handful of kernel-info-leak/host-wide-state syscalls — none of which any of `run_command`'s allowlisted commands legitimately need. Loaded via bwrap's `--seccomp FD`, compiled through either `pyseccomp` (this project's venv, `requirements-sandbox-hardening.txt`) or the system package `python3-seccomp` — **the owner installed `python3-seccomp` after this pass** (`sudo apt-get install -y python3-seccomp`), which surfaced a real bug: that package's module is importable as `seccomp`, not `pyseccomp`, and the first version of `seccomp_profile.py` only ever tried the latter — so system-wide Python silently stayed without the filter even after the package was installed. Fixed with a two-name import fallback (`pyseccomp` first, then `seccomp`), re-verified with the full 14-check suite run directly under plain system Python (no venv), not just assumed from the fix. Degrades to a logged warning, not a crash, when neither is importable. | **Implemented and tested, both under the venv and under plain system Python** | `python3 -m agent.sandbox.test_seccomp_profile` (14/14 in both environments) | Real, differential proof, not just "the filter loaded without error": `ptrace(PTRACE_TRACEME)` returns `EPERM` under the filter and **succeeds without it** (control case, same call) — proves the denial is the filter, not some other sandbox property. `ls`/`cat`/`echo`/`python3 -c` all still work under the filter. `sandbox_profile_digest` now includes `seccomp_active` so a run without the filter is visibly distinguishable from one with it, not silently implied equivalent. |
| **0005** — evidence content-ID scheme | Switched from plaintext SHA-256 to `HMAC-SHA256(key, data)` for both `EvidenceStore`'s content address and `AuditLog`'s `content_digest` (same shared `compute_digest()` helper, same key already used for encryption) — closes the equality-leakage gap (a plaintext hash lets anyone who sees digests confirm a guessed/known plaintext matches stored evidence, without breaking the encryption itself). | **Implemented, tested, and migrated against real accumulated data** | `python3 -m agent.evidence.test_hmac_migration` (16/16) + `agent.evidence.test_lifecycle` (13/13) + `agent.broker.test_policy_bypass`/`test_injection_quarantine` re-verified (the cross-reference assertion in `Broker._finalize()`) | `get()` falls back to the legacy scheme when HMAC doesn't match, so un-migrated evidence isn't instantly unreadable — found necessary directly (the first migration-test run hit this for real, not hypothetically). `migrate_to_hmac()` run for real against `agent/state/evidence/` (backed up first to `/tmp/{evidence,audit}_backup_pre_hmac_migration/`): 76+93-cumulative blobs migrated, 2 pre-existing corrupted blobs (confirmed broken *before* migration touched them, via a plain `get()` failing identically) correctly skipped-and-reported rather than aborting the whole run — a real robustness bug found and fixed via this exact real-data run, not assumed safe. All 243 real audit-log sessions accumulated this session re-verified with an intact hash chain afterward — confirms the deliberate choice to leave *historical* audit `content_digest` values untouched (only new entries use HMAC) didn't break tamper-evidence. |

## Phase 6 web UI — the last item before this project's own "until the UI" boundary

**Status: implemented and tested, including live end-to-end against the real model.** A plain
FastAPI server (`agent/web/server.py`) + a single static HTML/JS page
(`agent/web/static/index.html`, no build step, no framework) — the owner's own choice among
three offered options (a modern SPA with a build pipeline, or stopping here, were the
alternatives). Run via `python3 -m agent.run_web_ui`, loopback-only (`127.0.0.1:8765`) and
unauthenticated by default, matching Phase 1's own default-safe posture for a single-operator
local tool — exposing it further would need Phase 2's existing memory-service auth/TLS
machinery, not duplicated here.

**Redesigned after the owner shared a real handoff bundle** (`ai-web-platform-mockups/`, a
Claude Design export of a "RedTeam AI" mockup, plus screenshots of the real HackerAI desktop
app as visual reference). The mockup's dark oklch palette, Inter/JetBrains Mono type, and
interaction patterns (composer with control pills, message-block timeline, a slide-out "Computer"
panel for tool output) were ported; its custom `<sc-if>`/`<sc-for>`/`{{ }}` templating syntax
was not (per the bundle's own README: "recreate pixel-perfectly... don't copy the prototype's
internal structure"), and several mockup features with no real backend (Projects, Billing,
multi-account, "Environment: Cloud") were deliberately left out rather than built as
non-functional decoration — **the owner's own explicit choice** when asked, over building
everything the mockup showed. What ported to real, wired functionality:

- **Message-block rendering from real session data**: tool-call chips (parsed from the real
  `name`/`content` fields `SessionStore` already persists), a slide-out panel showing the real
  raw tool output (the actual `ActionResponse` JSON for broker-mediated calls) on click, and a
  genuine "flagged" warning block driven by real signal — `injection_guard.wrap()`'s
  `VERDICT:SUSPICIOUS/MALICIOUS` marker, already embedded in the persisted message content,
  parsed client-side rather than needing a new backend field.
- **Control pills wired to real `AgentLoop`/broker capability**: Security tools on/off
  (`use_security_tools`), Isolation tier (`bubblewrap`/`direct`, ADR-0004's real default and its
  real opt-out), Engagement ID (which RoE/scope applies) — no fake "Full access"/"Ask for
  approval" scope toggle, since that isn't how this project's approval model actually works
  (RoE action-classes + taint escalation decide it, not a session-wide switch).
  "Environment: Cloud" was dropped outright — there is no cloud mode.
  "Worked for Xm Ys" is computed client-side from real request/response timestamps, no backend
  change needed.
- **Approvals drawer** replaces the old always-visible third column — same real `ApprovalQueue`
  data, opened on demand with a live pending-count badge.

Verified with a real headless Chromium (Playwright, already installed for `browser_fetch`) driving
the actual running server and the real model — not just static review of the HTML/JS.
`python3 -m agent.web.test_frontend` (6/6): a full message round-trip with duration display, pill
menu interaction, a real broker-mediated `http_recon` call rendered as a tool chip whose Computer
panel shows genuine `ActionResponse` JSON (not a stub), the approvals drawer open/close cycle, and
the verdict-parsing/warning-rendering logic (both a synthetic flagged case and a clean case,
since organically triggering a real injection-guard flag through a live target isn't practical to
depend on for a committed test). Zero browser console errors or uncaught JS exceptions across
every scenario, asserted directly, not just eyeballed.

| capability | status | test command | evidence | gaps |
|---|---|---|---|---|
| Session lifecycle (create/list/resume) | **implemented and tested** | `python3 -m agent.web.test_server` (14/14) | a session is a real `AgentLoop` instance held in-process; `GET /api/sessions` merges on-disk sessions (real `SessionStore` files) with in-memory-only sessions that haven't sent a first message yet — found and fixed as a real gap: the first version only checked disk, so a freshly created session was invisible in the list until something was sent | restarting the server process loses "live" sessions (the `AgentLoop` objects) — the transcripts themselves are safe on disk (`agent.SessionStore`'s normal persistence, untouched), but resuming a session after a restart isn't wired up (would need re-constructing an `AgentLoop` from a saved config, not just replaying messages) |
| Turn-level streaming via SSE | **implemented and tested, live** | same command + a live `curl -N` smoke test against a real running server (not just the in-process TestClient) | genuinely turn-level, not token-level — stated precisely, not implied: `AgentLoop`/`LlamaClient` only do non-streaming chat completions today, real token streaming would be a materially larger change to that core path than this pass takes on. `WebSessionStore` (a thin `SessionStore` subclass) pushes an event on every `session.append()` — zero changes needed to `agent/loop.py`'s own iteration logic, reusing the exact extension point (`AgentLoop`'s `memory=` parameter) already built for Phase 2's remote session store | none found for what's implemented |
| Approval bridging (local `run_command` confirms + broker-mediated tool approvals) | **implemented and tested, live** | same command + a live smoke test resolving a real approval over real HTTP | both paths now go through the same `ApprovalQueue` — a new web confirm_fn for `AgentLoop`'s local confirmations, and a new `--use-approval-queue` flag on `security_mcp_server.py`'s subprocess (`Broker(use_approval_queue=True)`) for broker-mediated ones, both reusing the queue/async-mode primitive already built and tested earlier this pass rather than inventing a second mechanism | — |
| Steer channel wired to the UI | **implemented and tested** | same command | `POST /api/sessions/{id}/steer` calls the existing `SteerChannel.send()` unchanged | — |
| **Real bug found and fixed while building this**: `ApprovalQueue`'s file writes weren't atomic | **found, fixed, and regression-tested** | `python3 -m agent.broker.test_approval_queue` (11/11, includes a new concurrent-stress test — 4 polling threads + 200 concurrent submits, asserts zero read errors) | a genuine race — `list_pending()` polling from one thread while `submit()`/`resolve()` wrote from another could observe a truncated/empty JSON file and raise, caught for real via `agent/web/test_server.py`'s own approval test hitting it, not hypothetically. Fixed with the standard write-to-temp-then-`os.replace()`-atomic-rename pattern in both `submit()` and `resolve()`, plus a defensive `except JSONDecodeError: continue` in `list_pending()` as a second layer. This bug had been latent since the approval queue was first built earlier this pass — the exact concurrent submit-while-polling pattern the web UI introduces is what finally exercised it | none found after the fix — re-verified 3 consecutive full test-suite runs with no flakiness |

## Cross-cutting gaps confirmed this pass (apply across phases)

| gap | status | evidence | source |
|---|---|---|---|
| Injection quarantine connected to broker approval | ~~not implemented~~ **RESOLVED this pass — see M4.5 section above** (20/20 tests passing) | — | 01-security-agent-main-direction.md M4.5; local-security-agent-review-phase4.md §1.4 |
| No repository version control | **confirmed** | `git status` → "fatal: not a git repository" | 04-integration-prompt.md "current branch/status"; Working Style "pin versions" |
| No `AGENTS.md`/`CLAUDE.md` | **confirmed** | file check, both absent | 04-integration-prompt.md step 1 |
| Asset/target-state store | **not implemented** | `find agent -iname "*asset*"` → no results | local-security-agent-review-phase4.md Q3 P0; both HackerAI and Codex call this the single biggest gap |
| Hypothesis/coverage/observation store | **not implemented** | `find agent -iname "*hypothesis*" -o -iname "*coverage*" -o -iname "*observation*"` → no results | hackerai-signature-capabilities.md A1; 03-codex-independent-opinion.md §3.2 |
| Prompt registry (versioned, compiled, digested) | ~~not implemented~~ **RESOLVED — see M4.7 section above** (17/17 tests passing, wired into loop.py including phase/tool-card layers) | — | 01-security-agent-main-direction.md M4.7; local-security-agent-review-phase4.md §4.1 |
| Pipeline/task-tree orchestration | **not implemented** | no matching module | every proposal document, unanimously, calls this the highest-value missing piece |
| HTTPS in `http_recon` | **implemented and just verified working**, contradicting the reviewers' assumption it's entirely missing | see below | see note |

### Note on test-suite methodology — three vacuous-pass bugs found and fixed this pass

While expanding `agent/sandbox/test_isolation.py`'s signal/cancellation and parent-death checks,
three separate bugs were found where a check could report `PASS` without actually verifying the
property it claimed to: (1) a hand-copied `bwrap` argv construction inside the test drifted out
of sync with real changes to `executor.py` (missing `--clearenv`/`--setenv`), producing a
malformed command; (2) `pgrep -f` patterns containing unescaped `(`/`)` are parsed as POSIX
extended-regex grouping, not literal characters — `pgrep -f "time.sleep(20)"` never matches a
process with that literal command line, so a check asserting "nothing found" was trivially true
regardless of whether the thing being tested actually worked; (3) a "kill the parent immediately,
then check the child is gone" design couldn't distinguish "started and correctly died" from
"never started at all." All three are fixed (shared `BubblewrapExecutor.build_argv()`/`outer_env()`
helpers instead of duplicated construction; escaped regex patterns; an intermediate parent
process that stays alive long enough to directly confirm the child is running before checking
it's gone). Recorded here because it's a real, general lesson for this project's eval/test
work going forward (directly relevant to 01-security-agent-main-direction.md's M4.4 emphasis on
not trusting a green checkmark without checking what it actually verified) — not just a note
about one test file.

### Note on the HTTPS finding — a real correction to the reviewer documents

All three review documents assume `http_recon` handles HTTP only and treat "add HTTPS" as
undone work (M4.6). That assumption is **not accurate as of this audit**: `_PinnedConnection` in
`agent/security_tools/http_recon.py` already dials the broker-validated IP and performs a TLS
handshake with `ssl.create_default_context()` using the original hostname as SNI — the exact
mechanism M4.6 asks for (connect-to-validated-IP, verify-against-original-hostname).

This was re-verified live this pass, not assumed: a local self-signed HTTPS server was started on
`127.0.0.1` (loopback only, no non-lab network contact) and three cases were run directly against
the real code path —

1. self-signed, untrusted cert → correctly rejected (`SSLCertVerificationError`)
2. cert valid for a different name than requested → correctly rejected (`IP address mismatch`)
3. cert trusted and matching the requested hostname → succeeded, `status 200`

So the **core mechanism is implemented and demonstrated correct**, not missing. What genuinely
*is* still missing, matching the reviewers' fuller M4.6 spec: no `verify_cert`/`tls_policy`
override parameter for an approved insecure connection, no per-engagement custom CA bundle, no
formal automated test suite for these cases (the three checks above were ad hoc, not committed as
a repeatable test), no IPv6-over-HTTPS case, no explicit http↔https redirect-scheme revalidation
test, and no decompression/size-limit test specific to TLS responses.

## Documented-but-unverified claims (carried forward from `security-agent-research-reviewed.md`)

The baseline research doc's own §14 "research hygiene" section already asks for exact-source,
access-date, verified-vs-inferred labeling on external claims (benchmark numbers, tool behavior,
licensing). This audit did not attempt to re-verify BFCL scores, GLM-4.5/Claude/Gemini comparison
numbers, or Qwen3-Coder-30B benchmarks cited in `01-security-agent-main-direction.md` and
`local-security-agent-review-phase4.md` — both documents disagree with each other on how much
weight those numbers deserve (02-review-of-hackerai-proposals.md §3.2 explicitly says the
HackerAI review's BFCL comparison needs revision/leaderboard-date pinning), and resolving that
disagreement requires running this project's own local A/B eval, not reading either document
more carefully. Tracked as an open item for Phase 4B/5, not resolved here.
