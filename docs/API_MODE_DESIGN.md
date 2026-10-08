# API Mode — design, end to end

Oxpecker today drives a local llama.cpp server. **API mode** drives a frontier model over an
external API instead, for engagements where capability matters more than running offline — bug
bounty work in particular — and where the trajectories produced are meant to train the local
32B/9B afterwards.

This document is the plan. Nothing in it is implemented yet. It is written to survive a context
loss: every section names the real files and symbols it changes.

---

## 0. The one architectural decision

**Do not fork `dev_server.py`.** Not as a second server, not as a parallel module.

This project already paid for that mistake once. The finding in
[`OBSERVABILITY_PLAN.md`](OBSERVABILITY_PLAN.md) §1 was that the shipped runtime used *none* of
the four safety controls the docs claimed, and the cause was two runtimes (`loop.py` and
`dev_server.py`) drifting apart with nobody diffing them. A third runtime would guarantee a third
drift, and this time the drifting thing would be the audit trail that the whole training-data
plan depends on.

So: **one runtime, one broker, one audit chain, one tool surface.** What changes is the model
provider behind an interface, plus the features a stronger model unlocks. A session records which
provider served it; everything downstream is identical.

```
            ┌──────────────── one runtime ────────────────┐
client ──▶  │ dev_server → prompt → broker → tools        │ ──▶ audit chain
            │                ↑                             │     evidence store
            │        agent/llm/* (provider)                │     debug trace
            └──────┬──────────────────┬───────────────────┘
                   │                  │
            llama.cpp (local)   Claude API (new)
```

---

## 1. Layer 1 — the provider boundary

### 1.1 The package

New: `app/agent/llm/`

| File | Role |
|---|---|
| `base.py` | `ChatProvider` protocol + `ProviderCapabilities` dataclass |
| `llama.py` | wraps the existing `agent/llama_client.py` unchanged |
| `anthropic.py` | the new provider, official `anthropic` SDK |
| `registry.py` | name → provider, and the per-session choice |

`agent/llama_client.py` stays where it is and keeps working. `dev_server` stops importing it
directly and takes a `ChatProvider` instead.

### 1.2 Capabilities are declared, not assumed

A provider must state what it can do, because the pipeline branches on it and because the
**trajectory record has to say which path ran**:

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    native_tool_calls: bool      # structured tool_use blocks vs. scraping text
    strict_tool_schemas: bool    # strict: true + additionalProperties: false
    prompt_caching: bool
    server_compaction: bool
    reasoning: str               # "raw" | "summary" | "omitted" | "none"
    append_only_history: bool    # history edits invalidate prior reasoning blocks
    server_token_count: bool
    cost_per_mtok: tuple[float, float] | None
```

`reasoning` is the field that matters most for the dataset. On current Claude models the raw
chain of thought is **never returned** — `display: "summarized"` gives a readable summary,
`"omitted"` (the default on Opus 5.5) returns empty thinking blocks. So API mode cannot produce
raw-CoT training data, and the log must say so rather than let a later reader assume otherwise.
Every turn records `reasoning_fidelity` from this field. The local provider is the only one that
can emit `"raw"`.

That is not an argument against API mode. It reframes what API mode is *for*: it generates
**trajectories and outcomes** (which tool, with which arguments, against which evidence, judged
by which triage verdict), not reasoning traces. Reasoning-trace data still has to come from the
local model's own runs.

### 1.3 Prompt caching is a structural constraint, not a flag

Caching is the difference between an affordable agentic loop and an unaffordable one, and it is
prefix-matched: any byte change anywhere in the prefix invalidates everything after it. Render
order is `tools` → `system` → `messages`.

This imposes rules on `agent/prompts/compiler.py` and on `dev_server`'s message assembly:

1. **Tool schemas must be byte-stable.** Sorted keys, no dict-iteration order, no per-session
   interpolation into descriptions. Today `TOOL_SCHEMAS` is a literal list — fine — but
   `SECURITY_TOOL_SCHEMAS` is conditionally appended per session, which changes the tool block
   between sessions. That is acceptable (it is stable *within* a session) but the two sets must
   never be reordered.
2. **No clocks or ids in the system prompt.** A timestamp in layer 6 (`engagement.md.j2`) would
   silently cost the whole cache on every turn. The engagement render must be deterministic given
   the engagement record.
3. **Breakpoints:** at most 4. Place them after `tools`, after the compiled system prompt, after
   the engagement block, and after the last stable turn.
4. **Verification is a test, not a hope.** A regression test asserts
   `usage.cache_read_input_tokens > 0` on the second turn of a two-turn fixture. A zero there
   means a silent invalidator shipped.

### 1.4 History must become append-only — this breaks two existing functions

Current Claude models bind a reasoning block to the conversation that produced it ("preserved
thinking"): **editing earlier turns invalidates them**, and for accounts created on or after
2026-08-31 an edited history is a 400, not a degradation.

Oxpecker's two context functions both rewrite history:

* `_fit_context()` (`dev_server.py:1716`) truncates message content **in place** and
  `rest.pop(0)`s the oldest messages.
* `_maybe_compact()` folds older turns into a generated summary that **replaces** them.

Both are correct for llama.cpp and both are incompatible with API mode. Replacement:

* **Budget by tokens, not characters.** `_fit_context` counts `len(str)` against
  `MAX_INPUT_CHARS`. With a provider that bills tokens and offers a counting endpoint, use it;
  characters are neither the billed unit nor the limit.
* **Drop by appending, not by editing.** Use the provider's own context editing
  (`clear_tool_uses_20250919`, which clears old tool results server-side) and/or server-side
  compaction (`compact_20260112`), both of which are append-shaped by construction. When
  compaction is on, `response.content` must be appended back **whole** — extracting the text and
  appending that silently loses the compaction state.
* **Keep our compaction record either way.** `debug_trace.compaction()` must still fire, now
  recording *which mechanism* folded the context and what the folded span was. Server-side
  compaction without our own record would reopen the exact blind spot §1 of the observability
  plan was written to close.

### 1.5 The operator channel

Steer messages, approval outcomes and RoE reminders currently go in as `user` or `system` turns,
which on a cached conversation is expensive and, worse, puts operator instructions in the same
channel as target-derived text.

Current models accept a **mid-conversation system message** — `{"role": "system", ...}` appended
to `messages[]` rather than edited into the top-level `system` field. It preserves the cached
prefix and is the injection-safe channel for exactly this. `SteerRequest`, the approval
resolution, and the "you are now tainted, passive recon only" notice all move onto it.

### 1.6 Tool calls stop being guesswork

`dev_server` currently carries `_extract_tool_calls()` (scrapes JSON blocks out of prose) and a
27-entry `_TOOL_NAME_ALIASES` table, because a 4B model picks near-miss names and malformed JSON.

With native tool calls plus `strict: true` (requires `additionalProperties: false` and `required`
on every schema), both become unnecessary in API mode. They stay for the local provider — the
alias table is real accumulated knowledge about small-model failure modes and deleting it would
regress local sessions. The dispatcher picks the path from `capabilities.native_tool_calls`, and
**the trajectory records which path parsed the call**, because "the model emitted a valid tool_use
block" and "we recovered a tool call from prose" are different facts about the model.

Two further rules:
* **Parse, never string-match.** Tool input JSON escaping varies by model; `json.loads` always.
* **Validate before executing.** With eager input streaming the API stops coercing and validating
  the input, so a truncated or invalid input can arrive. Validate against the schema, and on
  failure return an error tool result — do not execute on a partial argument dict. For a tool that
  sends traffic at a target, a half-parsed argument is a request nobody authorised.

### 1.7 Refusals are an operational fact to design for, not an edge case

Current Claude models can decline a request with HTTP 200 and `stop_reason: "refusal"`, carrying
a `stop_details.category`. **One of those categories is `cyber`.** An offensive-security agent
will hit it.

This must be designed, not discovered in production:

* **Record it as its own event**, distinct from "the model declined in prose" and from an API
  error. `turn.outcome = refusal`, with the category. This is also a *dataset label*: knowing
  which engagements and which phrasings draw a refusal is useful to the project.
* **Check `stop_reason` before reading `content`.** `stop_details` is populated only on refusal
  and is `null` otherwise — guard it.
* **Decide the policy once, in config, and show it in the UI.** Three options, and the operator
  picks per engagement: server-side fallback to another model; fall back to the local 32B (which
  is the whole reason this project trains one); or stop and surface it. Silent retry is not an
  option — it burns money and hides a real signal.

Honest note for the project owner: a frontier API is a third party's judgment sitting inside your
loop. For authorised bug bounty work that is usually fine, and the engagement record (program URL,
safe-harbour statement, `authorized_by`) is what makes the request legible as authorised. But
plan for the local model to be the fallback on principle, not just on cost.

### 1.8 Cost and budget

* A **cost ledger** per turn and per session: input / output / cache-read / cache-write tokens
  and the dollar figure. Written into the trajectory record and shown in the client. Without it
  the economics of the bug-bounty plan are unmeasurable, which is the one thing that plan cannot
  afford.
* **Task budget** (advisory, token-denominated, min 20,000) so the model paces a long hunt and
  finishes gracefully instead of being cut off mid-finding. Distinct from `max_tokens`, which the
  model cannot see.
* **Batch** the offline work at half cost: scrubbing, relabelling, lab-reproduction drafting.
  Never the live loop.
* **Streaming always**, with large `max_tokens` — a long hunt turn otherwise hits the HTTP
  timeout.

---

## 2. Layer 2 — inside the pipeline

Every existing feature stays. What follows is what changes, and what gets added because a stronger
model can use it. The guiding line from the project owner: *better tools mean better work* — so
the tools get sharper, not merely more numerous.

### 2.1 The engagement becomes a typed program record

**This is the highest-value change in the document.**

The best artefact in the HackerAI transcript we examined was not the agent's output — it was the
operator's 113-line hand-written brief: 24 targets mapped by tech stack, a tech→CVE table, the
program's three focus areas, the hard "do not report" list, the dedupe strategy derived from prior
accepted submissions, and a ranked host list. That is domain expertise, and in that system it
lived as **prose inside a prompt**, where nothing could validate it, enforce it, or reuse it.

In Oxpecker it becomes data on the `Engagement`:

```python
@dataclass
class Program:              # alongside Engagement, not inside it
    platform: str           # "bugcrowd" | "hackerone" | "vdp" | "lab"
    program_url: str
    safe_harbour: str
    automation_allowed: bool        # several programs forbid it outright
    max_requests_per_min: int | None
    excluded_vuln_types: list[str]  # the "do not report" list, enforced
    focus_areas: list[str]
    vrt_taxonomy: str | None
    prior_submissions: list[dict]   # host → classes already accepted
    out_of_scope_refs: list[str]    # e.g. the attached OOS PDF
```

What each field buys, concretely:

* `automation_allowed=False` → the broker refuses every active action class, not the operator's
  memory. This is the difference between a policy and a hope.
* `max_requests_per_min` → bound to the broker's existing per-action-class cooldown. In the
  transcript the agent earned a Cloudflare 1015 IP ban and a 429 with `retry-after ~24h` **during
  Phase 1 recon**, and lost access to the single most promising target (an unpatched Drupal
  10.6.10) for the rest of the session. Rate limiting is therefore a *capability* feature: it is
  what keeps the target reachable tomorrow.
* `excluded_vuln_types` → the finding composer refuses to create a finding of an excluded class.
  "XSS is out of scope" was stated three times in that brief and is exactly the kind of rule a
  model drops after six compactions.
* `prior_submissions` → a dedupe check at finding time, surfaced before a human spends effort.

**Scope matching must gain wildcards.** `broker/scope_check.validate_target()` does exact hostname
plus CIDR only — verified, no `fnmatch`, no wildcard path. Bug bounty scopes are written
`*.example.com` with an out-of-scope list nested inside. Without this, API mode cannot express a
single real program. `deny_targets` (shipped) is half the answer; wildcard allow with
deny-evaluated-first is the other half.

### 2.2 The plan step becomes a first-class turn

Today a session begins with a user message. In API mode it begins with a **plan**:

```
raw program brief ──▶ [plan turn] ──▶ plan artifact ──▶ operator edits ──▶ approved plan
                                           │                                     │
                                           └────── diff recorded ────────────────┘
```

Three reasons, in order of value:

1. **The diff is the most valuable training data the project can produce.** Model-drafted plan vs.
   operator-corrected plan, on every hunt, with no dependency on triage latency. It teaches the
   step that is currently missing from every dataset: *brief → strategy*.
2. It makes the expertise reusable instead of re-typed.
3. It gives the operator one place to catch a bad plan before it spends money.

Stored as a `Plan` artifact (phases, ranked targets, hypotheses to seed, excluded classes,
budget), versioned, with `source: model|operator|merged` per field.

### 2.3 Tools — what exists, what to add

Existing and unchanged: `http_request`, `port_discovery`, `run_command` (sandboxed),
`read_file` / `write_file` (workspace-confined), `knowledge_search`, and the `record_*` family.

Additions, each tied to something the transcript showed was needed:

| Tool | Action class | Why |
|---|---|---|
| `knowledge_fetch` | `knowledge_fetch` | Already in `broker.TOOL_ACTION_CLASS` and **not wired into `dev_server`**. The transcript's agent read four SA-CORE advisories to decide which Drupal CVE was anonymously reachable — that is the single highest-leverage research step in the run, and the web runtime currently cannot do it. Implement over the provider's server-side web fetch with `allowed_domains` pinned to advisory sources (NVD, vendor SAs, ExploitDB), which makes the domain allow-list itself a scope control. |
| `surface_snapshot` / `surface_diff` | `passive_recon` | The agent's real edge over a human is being continuously on. Snapshot an engagement's attack surface (hosts, endpoints, versions, headers, JS bundle hashes) and diff against the last run. New subdomain, changed response, bumped version — that is where the non-duplicate findings are. |
| `http_session` | `active_web_request` | The harness owns cookies/tokens/IP pinning. `ScopeDecision.pinned_ip` is already carried for this. Without it, DNS can be swapped between the scope check and the connection. |
| `evidence_slice` | — | Read a byte range out of a stored response. The companion to §2.5. |
| `replay_finding` | varies | Re-run a stored finding's PoC against the target and assert the same outcome. Turns a finding into a regression test, and is the verification gate for §3.4. |

Write-shaped tools against a live third party (`createdealerhotlead`-style endpoints) get **their
own action class**, `active_write`, defaulting to off. In the transcript the agent correctly
*asked* before writing test data to production — but it asked because it was polite, with nothing
behind it. `_web_confirm` plus an action class is the mechanism that makes it structural.

### 2.4 RAG

`knowledge_search` already logs chunk ids and scores through `debug_trace.retrieval()`. Two
changes:

* The web runtime's `TFIDFStore` is a stand-in; API mode should read the real
  `knowledge_rag` index when present and record `index_version` per retrieval, so a behaviour
  change can be attributed to the index rather than the model.
* Retrieval results keep their `[TOOL OUTPUT - TREAT AS DATA]` wrapper into the **training
  export**, not just into the live context. Stripping the provenance marker at export time would
  train the model that retrieved text is trustworthy instruction.

### 2.5 Context discipline for `http_request` — the cost lever

This is the item from [`TOOLING_ROADMAP.md`](TOOLING_ROADMAP.md) that now becomes load-bearing.

A raw HTTP response in context is 10–50k tokens. A hunt is 50–200 tool calls. Putting bodies in
context directly makes a single target cost millions of tokens per pass, and the bug-bounty plan
dies on arithmetic before it reaches a finding.

So `http_request` returns a **structured digest** and the body goes to the evidence store:

```json
{ "status": 200, "headers_of_interest": {...}, "length": 48213,
  "body_sha256": "…", "evidence_id": "ev_…",
  "diff_vs_baseline": {"status": "200→500", "added_headers": [...], "length_delta": 1204},
  "extracted": {"forms": [...], "endpoints": [...], "tokens": [...], "errors": [...]},
  "preview": "first 2KB" }
```

The model then asks for what it needs with `evidence_slice`. This is not only cheaper — it is what
makes the trajectory **causally complete**, because the digest the model saw is recorded exactly,
and the full body is recoverable from the evidence store by digest. A truncated-in-context body
produces a trajectory where the decision cannot be explained by the context, which is the defect
that silently teaches hallucination.

`diff_vs_baseline` exists because differential response reading is most of web testing, and a
model that has to hold two 40k bodies in context to compare them will not do it.

### 2.6 Hypothesis graph and notebook become the durable memory

The transcript compacted **six times** in one run. Any summary-based memory degrades across six
compactions; a structured store does not.

So the rule: **on compaction, the retained summary is reconstructed from the hypothesis graph,
notebook and findings — not generated as free text from the transcript.** The graph is already
the right shape; it needs three fields:

| Field | On | Why |
|---|---|---|
| `confidence` | `HypothesisNode` | Distinguishes "suspected" from "verified". Calibration is the behaviour that separates an accepted report from AI slop. |
| `evidence_refs` | node, note, finding | A claim without an evidence id is not a claim. |
| `ruled_out_reason` | `HypothesisNode` | The transcript's best data was its *negative* table — SSRF ruled out by four probes with identical errors, ViewState MAC proven enforced, SQLi ruled out by HTML-encoded output. Each is a labelled negative with discriminating evidence. Today that only exists as prose in a final chat message; it should be a first-class, exportable record. |

Notes gain `category` values for `ruled_out` and `blocked` (rate-limited, needs-browser,
needs-account) so "what is left and why" is queryable rather than narrated.

### 2.7 Findings are evidence-gated

`create_finding` refuses unless the finding carries at least one `evidence_ref` whose digest
resolves in the evidence store, and `replay_finding` has succeeded at least once. Severity needs
a VRT/CVSS mapping from `Program.vrt_taxonomy`.

This is the anti-slop gate and it protects the thing that is hardest to repair: a reputation on
the platform. Reports that read well and reproduce never are the complaint triage teams make about
AI submissions; a finding that cannot be replayed should not be creatable.

**No auto-submit, ever.** The pipeline produces a submission draft; a human sends it.

### 2.8 Broker changes, collected

| Change | Reason |
|---|---|
| wildcard scope + nested out-of-scope | no real program is expressible without it (§2.1) |
| `automation_allowed` gate | several programs forbid automated testing |
| per-program rate limit bound to the cooldown | keeps the target reachable (§2.1) |
| `active_write` action class, default off | §2.3 |
| `knowledge_fetch` wired into the web runtime | §2.3 |
| response-body scrubber before anything leaves the host | §4.3 — `audit_log._redact` only inspects argument *key names*; nothing scrubs response bodies, and `debug_trace` is deliberately unredacted |

---

## 3. Layer 3 — the client

The existing Electron/web client keeps everything it has. Additions, in build order:

1. **Plan editor** — the model's drafted plan, editable field by field, with Approve. The diff is
   captured on save (§2.2). This is the first screen of a hunt.
2. **Cost meter** — per turn and session cumulative, with the cache hit rate. Visible, because an
   invisible burn rate is how this plan fails quietly.
3. **Evidence viewer** — request/response pairs by digest, with the baseline/exploit diff
   highlighted. The operator must be able to see what the model saw.
4. **Finding composer** — pulls evidence refs, runs the dedupe check against
   `Program.prior_submissions`, blocks excluded classes, renders the platform's submission shape,
   and ends in **Copy / Export, never Send**.
5. **Surface diff view** — what changed on this engagement since the last run (§2.3).
6. **Blocked board** — the `blocked` notes from §2.6: rate-limited, needs a browser, needs an
   account. In the transcript this information was buried in a closing paragraph; it is the
   operator's actual to-do list.
7. **Trace viewer** — `trace_cli` output in the UI, including which provider and which
   `reasoning_fidelity` served each turn.

The approvals panel already exists and is already wired (`_web_confirm`); API mode needs no
change there, which is the point of not forking.

---

## 4. Layer 4 — the training log

### 4.1 The trajectory record

One append-only JSONL per session, hash-chained through the existing `audit_log`, with the
full-fidelity sink in `debug_trace`. Per turn:

```json
{
  "turn": 14,
  "provider": {"name": "anthropic", "model": "claude-opus-5-5",
               "effort": "high", "thinking": "adaptive", "capabilities_digest": "…"},
  "prompt": {"blocks": ["tools@sha", "system@sha", "engagement@sha", "history@sha"],
             "cache": {"read": 182400, "write": 0, "miss": 1200},
             "tokens_in": 183600},
  "reasoning": {"fidelity": "summary", "text": "…"},
  "output": {"stop_reason": "tool_use", "stop_details": null,
             "parse_path": "native_tool_use"},
  "tool_calls": [{"name": "http_request", "arguments": {...},
                  "broker": {"status": "succeeded", "rule": "allow:active_web_request",
                             "action_class": "active_web_request"},
                  "result_digest": "ev_…", "result_inline": "{…capped…}",
                  "provenance": "target_controlled"}],
  "compaction": null,
  "cost": {"usd": 0.74, "tokens_out": 1810},
  "latency_ms": 18400
}
```

Every field exists because its absence breaks a specific use:
`prompt.blocks` → the trajectory is reconstructable; `cache` → caching regressions are visible;
`reasoning.fidelity` → nobody mistakes a summary for a chain of thought; `parse_path` → "the model
emitted a valid call" ≠ "we recovered one from prose"; `broker.rule` → a scope denial is a
*labelled* negative, not the model's self-report; `provenance` → the injection boundary survives
into the dataset; `cost` → cost-aware behaviour is trainable and the plan is measurable.

### 4.2 Outcome labels arrive days later

A finding's verdict comes from triage, typically ~9 days out on the program examined. So labels
cannot be part of the session record; they are a join:

```
submissions/<program>/<submission_id>.json
  → finding_id, session_id, submitted_at, platform_url,
    state: submitted|triaged|accepted|duplicate|informational|na,
    severity_assigned, payout_usd, label_source: platform|operator, labelled_at
```

A reconciliation pass (operator-run, or scraped where the platform's own API allows it) updates
state. Everything downstream — reward shaping, eval scoring — reads this store, never a model's
opinion of its own finding.

### 4.3 Export: two profiles, one gate

The transcript we examined is the cautionary case. A single public share link contained: an
unpatched Drupal core version on a named production host, a working anonymous-token path, a
reachable internal Keycloak provisioning API with 29 endpoints including password reset and OAuth
client creation — plus the operator's own lab IP and SSH key path. Platform terms almost
universally forbid disclosing vulnerability detail without permission, and a model trained on that
text can regurgitate a specific customer's vulnerable endpoint.

So export is gated, with two profiles:

| Profile | Contents | Leaves the host? |
|---|---|---|
| `eval-private` | real-target trajectories, unscrubbed | **never** |
| `train` | lab-reproduced trajectories, scrubbed | yes, under a licence decision |

* **Scrubber**, run before anything is written to a `train` export: hostnames → stable
  placeholders, IPs, tokens, cookies, auth headers, emails, anything matching
  `config.SECRET_ENV_MARKERS`, and the operator's own infrastructure. Scrubbing is
  consistent per engagement so a trajectory stays coherent.
* **Disclosure gate**: an export refuses outright if any session in it references a `Program`
  whose `platform` is not `lab` and whose disclosure status is not explicitly cleared. Fail closed,
  with the program named — the same shape as the broker's scope refusal, for the same reason.
* **PII rule**: a response body that tripped a PII detector is stored encrypted in the evidence
  store and is never eligible for `train`, scrubbed or not.

### 4.4 The lab reproduction loop

The step that converts contractually-poisoned data into clean, replayable data — and the only path
to RL, because reinforcement learning needs an environment that resets and a real target does not:

```
accepted finding ──▶ spec (class, stack, preconditions, oracle)
        ──▶ lab app reproducing it on the same stack (docker compose)
        ──▶ agent re-solves it in the lab
        ──▶ trajectory  → train profile
            environment → RL task
            oracle      → verifiable reward
```

The oracle is the point: the lab app knows whether the bug was actually exploited, so the reward
is computed, not judged. This is the mechanism `EVALUATION.md` lists as blocked on "RL stage does
not exist yet" — what is actually missing is the reward, and this produces it.

---

## 5. Build order

Each step is useful on its own and nothing later is required for anything earlier to ship.

| # | Step | Why here |
|---|---|---|
| 1 | `agent/llm/` + provider protocol + `llama.py` adapter, no behaviour change | proves the seam without risking the working runtime |
| 2 | `anthropic.py`: streaming, native strict tools, refusal handling, cost ledger | the minimum that runs a real turn and reports what it cost |
| 3 | Append-only history: replace `_fit_context` / `_maybe_compact` for API mode; token-based budget; compaction record kept | without this, API mode is a 400 on long sessions (§1.4) |
| 4 | Prompt-cache stability + the `cache_read > 0` regression test | the loop is unaffordable until this holds |
| 5 | **Wildcard scope + `Program` record + automation/rate-limit gates** | no real engagement is expressible before this |
| 6 | `http_request` digest + evidence store + `evidence_slice` | the cost lever and the causal-completeness fix |
| 7 | Trajectory record v1 + `provenance` + `reasoning_fidelity` | start collecting correctly before volume, not after |
| 8 | Plan turn + plan diff capture | begins producing the highest-value data immediately |
| 9 | `knowledge_fetch`, `surface_diff`, `active_write` class | sharper tools once the frame is safe |
| 10 | Finding gate + composer + submission store | protects the reputation asset before the first submission |
| 11 | Scrubber + disclosure gate + export profiles | required before any data leaves the host |
| 12 | Lab reproduction loop + oracle | unlocks RL |

Steps 1–4 are provider plumbing. **Step 5 is the gate on doing any real bug-bounty work at all.**
Steps 6–8 are what make the data worth collecting. 9–12 are the flywheel.

---

## 6. What this design does not do, and what the owner has to decide

Not addressed here, deliberately:

* **No auto-submission, at any phase.** A human sends every report.
* **No wildcard-driven subdomain discovery beyond the program's listed scope.** Wildcard matching
  is for expressing `*.example.com` where the program grants it, not for expanding scope.
* **No training on real-target trajectories.** They are eval-only, by §4.3.

Open decisions, which are the owner's and not the implementer's:

1. **Refusal policy** (§1.7): server-side fallback, local-model fallback, or stop. Local fallback
   is the one that compounds with the rest of the project.
2. **`SAFE_WHILE_TAINTED`** is `{"passive_recon"}`, so a tainted session needs approval even for a
   local `knowledge_search` that sends nothing anywhere. Widening it is defensible; it is also
   weakening a security default for ergonomics.
3. **Dataset licence.** The repository declares Apache 2.0 over corpora derived from third-party
   sources. Unresolved, and a legal question rather than a technical one.
4. **Which programs are eligible at all.** VDPs and self-hosted labs first is the lower-risk ramp;
   paid programs with narrow, recently-added scope are where the non-duplicate findings are.
