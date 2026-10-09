# Agent architecture — the API-mode checkpoint

Status: **draft for review, nothing built.** Every decision below is the owner's answer to a
direct question, recorded here so the reasoning survives the conversation. Where this document
decides something itself it says so, and says what it would take to reverse it.

This supersedes nothing yet. It changes the priority of `docs/API_MODE_DESIGN.md` §5 — see §11.

---

## 0. The checkpoint this serves

A system that works completely on an external API, where:

1. the agent can use **every** tool it is given, at full effectiveness
2. the system extracts maximum performance at minimum cost — **never by lowering quality**
3. a server on Ubuntu works with a client on Windows
4. both server and client have a UI appropriate to their job
5. it produces high-quality trajectory records

Acceptance:

* **A.** Against the same model, find more, faster, more severe, cheaper than hackerAI.co
* **B.** Stay inside scope, precisely

---

## 1. The one architectural decision: one engine, one tool plane

Condition 1 is impossible today, and the reason is structural rather than a missing feature.

Two engines exist and **neither has all the tools**:

| tool | `dev_server.py` (has the UI) | `loop.py` |
|---|---|---|
| `http_request` | yes | no |
| `run_command`, `read_file`, `write_file` | yes | no |
| `record_note` | yes | no |
| `knowledge_fetch` (real page fetch) | **no** | yes |
| `browser_fetch` | **no** | yes |
| `osint_record` | **no** | yes |
| `http_recon`, `port_discovery`, `knowledge_search`, `record_*` | yes | yes |

`agent/internet/` and `agent/browser/` are exposed only through `security_mcp_server.py`, which
only `loop.py` talks to. `dev_server` implements its own tools instead and cannot reach them.

**Decision: `security_mcp_server.py` becomes the single tool plane. `dev_server` becomes the
single engine and a client of that plane. `loop.py` keeps only what a CLI frontend needs, or
goes away.**

Three things follow: one tool registry rather than two that drift, tool execution out of
process (which is where sandboxing belongs anyway), and one place to add a tool.

**This is not a big-bang refactor.** The gate is a test: `test_tool_parity.py` asserts that
every tool in the tool plane is reachable from the engine that has the UI. It fails today. It
goes green one tool at a time. The architectural gap becomes a failing test that drives the
work, which is how the last two days of this project were spent productively.

---

## 2. Agent roles

The owner's answer to "who thinks" was: the agent is the strategist, with the human able to
intervene, advise, and do things the agent cannot. So the strategist is ours to build.

**Four** roles. Each is a **prompt + tool subset + context recipe**, not a separate program.
An earlier draft argued for a fifth — a cheap dispatcher — to keep the strategist from being
called once per worker completion. The wave model (§2.5) makes that unnecessary.

### 2.0 One engagement, end to end

The owner's own walkthrough, which fixes the shape of everything below. Read it before the role
definitions.

| | |
|---|---|
| 0 | a person enters the scope |
| 1 | the strategist dispatches **several** workers to initial recon |
| 2 | **every** worker finishes and reports. Each node's log holds the raw evidence — the commands and their output. Where a worker believes its hypothesis holds it writes a **proposal**; `confirmed` comes from checking that proposal against the raw evidence, not from the worker saying so |
| 3 | the strategist starts chaining and dispatches the next wave — more recon, or deeper recon on what looked interesting |
| 4 | the wave completes and reports, same as step 2 |
| 5 | the strategist dispatches vulnerability testing against what recon found |
| 6 | the worker that found A tests A until it is exhausted; the wave completes and reports |
| 7 | the strategist sees that A and B together reach E, and that C would combine with D if D existed. It creates a worker for E, and sends D back out **three ways at once** |
| 8 | the wave completes and reports |
| 9 | finding 1 and finding 2 exist |
| 10 | a person says that is enough, write it up |
| 11 | the reporter reads the findings along their path and assembles the evidence |

Three properties of this shape matter more than any role definition:

* **It is wave-synchronised.** The strategist thinks between waves, never per worker completion
* **A worker's proposal is not a verdict.** Step 2 separates "the worker believes" from "the
  system confirmed", and the confirmation is a comparison against raw evidence
* **A person closes the engagement**, at step 10. The agent can say it is out of ideas; it
  cannot decide the work is finished

### 2.0.1 The strategist reads nodes itself

Two capabilities the walkthrough calls for, beyond dispatching:

**Going to look.** The strategist can pull a node's contents and walk the ones it finds
suspicious — successful or failed — like a lead going down to inspect the work personally
rather than reading the summary. `graph_read_branch` already exists for this ("your backtrack
tool"); it has no caller yet.

**A full sweep on request.** When a person asks for everything to be checked in detail, the
strategist walks every node looking for anything the automated flow passed over. This must run
in **two stages** or it becomes the thing this architecture exists to avoid — the whole
engagement pushed into one prompt:

1. read the **digest line** of every node first, which is cheap by construction
2. pull the full node only for the ones worth a second look

A sweep is also unusually good training data: it records what a strong model found interesting
that the normal flow missed, which is a judgment signal nothing else in the system captures.

### 2.1 Strategist — decides what happens next, and finds chains

* **Runs:** infrequently — at engagement start, and when workers report back
* **Sees:** the full graph digest, every primitive held, every confirmed finding, the scope
* **Tools:** graph read/write, task assignment, `graph_read_branch` and `graph_search` for
  going to look itself (§2.0.1), `request_human_action`. **No target-touching tools at all** —
  it does not scan, fetch, or send anything
* **Model:** the strong one. Affordable precisely because it runs rarely over a small context
* **Its distinctive job:** writing `synthesized_from` edges. One vulnerability is worth little;
  SSRF plus weak IAM plus a reachable metadata endpoint is a path. Chaining is this role's
  product, and it is the thing a worker structurally cannot do

### 2.2 Worker — runs exactly one experiment

"One hypothesis per worker" was the first draft and it is underspecified, because the tree
branches: a worker testing H-5 may discover three new claims. Does it test them too? If yes
the session grows without bound and we are back to the long-session problem.

**The unit is one experiment (attempt) on one hypothesis** — which is not a new concept, it is
the one the store already implements:

```python
start_experiment(hypothesis_id, *, method_summary, expected_observation="",
                 broker_action_ref=None, chat_start_message_id=None)
```

`attempt_no` is assigned by the store, so retries are first-class and countable without a
hidden counter. `ExperimentStatus` is `PLANNED/RUNNING/COMPLETED/FAILED/BLOCKED/CANCELLED`, and
evidence carries `Polarity` (supports/refutes/neutral) by `Strength` (weak/moderate/strong).

So the worker's contract is the existing schema:

| | field |
|---|---|
| given | `hypothesis_id` |
| declared before acting | `method_summary` (what I will do) + **`expected_observation`** (what I expect to see) |
| returned | `observed_result`, polarity/strength, a **proposed** verdict |
| tied to authorisation | `broker_action_ref` |

`expected_observation` is the most valuable field in this design for §8.4. It is what makes
"the reasoning was right and the target was not vulnerable" distinguishable from "it guessed
and got lucky" — which is the owner's definition of a high-quality trajectory.

**Branching is governed by edge type, and the distinction already exists in the schema:**

| edge | its own definition in `schema.py` | may a worker act on it? |
|---|---|---|
| `REFINES` | *"narrows/sharpens a parent claim"* | **yes** — still answering the same question |
| `SPAWNED_BY` | *"an attempt result produced this new hypothesis"* | **writes it, never follows it** |

**One rule: the tree branches; the worker does not follow the branch.** A worker that uncovers
three new avenues records them as nodes with `spawned_by` edges and ends. Which branch is
pursued next is the strategist's decision, which is exactly where chaining intelligence
belongs. The semantic test is "am I still answering the question I was given" — if yes it is
this worker's work, if it is a new question it is not.

* **Runs:** often, in parallel (§2.5)
* **Tools:** the full target-touching set, broker-mediated as today, plus `graph_search` and
  `graph_read_branch`
* **Knows it is a worker.** The prompt says so. A model that knows where its boundary is does
  not wander outside it, and its trajectory stays attributable
* **Ends when it says it is done** — the owner's rule: exhaustion is the worker's own judgment
  from evidence, not a turn count. There is **no query ceiling and no attempt ceiling**

### 2.2.1 What catches a stuck worker, since nothing caps it

The owner's position is that the agent decides when a line is exhausted, and this document
defers to it. It has to be said plainly that this project has *measured* the failure mode
anyway: a model burned one to six full generations on `sleep` rather than act, and
`dev_server` already carries loop detection that stops a model repeating an identical call
three times because this happened before.

So there is no impatience guard, and there are pathology guards:

* the **spend ceiling** (§7.2 — on budget, stop and report)
* **no progress for N turns**, using the per-turn progress flag from §7.1
* the existing **identical-call loop detector**

**Deciding that a line is exhausted belongs to the agent. Deciding that it is stuck belongs to
the system.** Those are different judgments and neither overrides the other.

### 2.2.2 What a worker is shown, and what it must go and fetch

A worker on H-112 in `1 → 11 → 112` must see how it got there. Concretely: if `11` is
"upload accepts arbitrary extensions (confirmed)", a worker on `112` that cannot see it will
re-test it — paid for twice — or, worse, will not know which primitive it already holds and
will test the wrong thing entirely.

The mechanism exists: `build_digest` already guarantees root and active path are present
("they must never fall out of context"). What needs deciding is *how much*, and the answer is
**claim plus verdict plus `primitive_gained` — not the history of how it was established.**
A worker on `112` does not need `11`'s request log; it needs to know `11` is true and what it
yields. The history is available on demand through `graph_read_branch`, the backtrack tool.

| what | form | always present |
|---|---|---|
| scope + RoE | full | **yes** |
| ancestor chain, root → parent | claim + verdict + `primitive_gained` | **yes** |
| primitives held, whole engagement | list | **yes** |
| confirmed findings, whole engagement | one line each | **yes** |
| dead ends on this branch | one line each | **yes** |
| siblings under the same parent | one line each, plus a `RUNNING` flag | **yes** |
| other branches | — | no — `graph_search` |
| how an ancestor was proven | — | no — `graph_read_branch` |

**Siblings are included** for two reasons: one may already have refuted what this worker is
about to try, and if one is `RUNNING` under parallelism this worker needs to know or two
workers reason about the same thing simultaneously.

**Other branches are not.** They are different questions; if they were relevant the strategist
would have drawn an edge. Including every branch is how a brief becomes a transcript by another
route. The one exception is a confirmed finding anywhere in the tree, which can be the missing
link of a chain — but that arrives through the always-present primitives and findings lists, not
by showing the branch.

Two correctness requirements follow:

1. **The ancestor chain is rendered deterministically and never summarised by a model.**
   `digest_line` already guarantees this. It matters more than it sounds: **a wrong premise is
   more dangerous than a missing one**, because the worker builds on it and the resulting
   trajectory is poison — a model reasoning correctly from a fabricated fact, which is exactly
   what we would be training it to do
2. **A worker's chain is computed from `primary_parent_id`, not read from `active_path`.**
   `graph_set_active_path` is a tool the *model* calls; if it sets the path wrongly, the
   digest's guarantee protects the wrong nodes. `active_path` serves the UI and the strategist.
   A worker's ancestry is derived from stored parentage

Because the brief is recorded in the trajectory, a bad outcome can be attributed: **"the model
reasoned badly" becomes distinguishable from "we briefed it badly."** Without the recorded
brief those are indistinguishable, and we would train against the wrong error.

### 2.3 Verifier — tries to prove the finding wrong

* **Runs:** once per candidate finding
* **Must be a different model instance from the one that produced the finding.** A model asked
  to check its own work agrees with itself nearly always
* **Its only success condition is refutation.** A verifier that cannot refute is the signal
* Cost is small; a false report is the most expensive thing this system can produce, in money
  and in reputation

### 2.4 Reporter — writes the submission

* **Runs:** at the end, or on request
* **Sees:** confirmed findings and their evidence slices only. Not the transcript, not the
  refuted branches
* **Can state nothing that is not backed by an evidence reference.** The finding gate, not the
  model, decides what is reportable

### 2.5 Parallelism runs in waves, and that is what makes it affordable

The owner's walkthrough settles the shape: **workers go out as a batch, every one of them runs
to completion, and only then does the strategist think once and dispatch the next batch.**

That answers the dependency objection — worker B never waits on worker A's in-flight state, it
waits on A's recorded result — and it does two more things that an earlier draft of this
document treated as unsolved problems:

**It removes the strategist bottleneck.** An earlier draft argued the strategist would be
called once per worker completion, making it both the latency bottleneck and the dominant cost,
and proposed a fifth role — a cheap dispatcher — to absorb that. **With waves this is
unnecessary and the fifth role is dropped.** Strategist calls scale with the number of
*waves*, not the number of workers: ten workers in a wave still cost one strategist turn.

**It removes the unstable stop condition.** "Are all avenues exhausted" is evaluated at a wave
boundary, where by construction nothing is `RUNNING`. The premature-shutdown failure an earlier
draft described cannot occur in this shape.

**Duplicated work, and why the claim is on the experiment and not the hypothesis.** An earlier
draft had a worker claim its *hypothesis* by transitioning it to `RUNNING`. The walkthrough's
step 7 breaks that: the strategist tells D to try again *three ways*, so three workers must
work the same hypothesis at once, and a hypothesis-level claim lets only the first in.

**The claim is therefore on `(hypothesis, method)` — the experiment — not on the hypothesis.**
Three attempts on D with three methods proceed without colliding, and "duplicated work" means
what it should mean: two workers running the same method against the same claim.
`attempt_no` and `method_summary` already exist, so this is binding the lock to the right row
rather than building anything new.

**Stragglers are the cost of a barrier.** A wave ends when its slowest worker ends: four
workers finishing in a minute and a fifth taking twenty wastes nineteen minutes of wall clock,
which lands directly on acceptance test A's "faster". So **each worker is dispatched with a
wall-clock limit**; one that exceeds it reports what it has and parks. The strategist then has
an upper bound on how long a wave can take, and the barrier is preserved.

**Rate limits silently multiplied.** This is a real bug waiting: the broker keys its cooldown
on `(session_id, action_class)` while the program's request cap is keyed per engagement. Two
workers are two sessions, so they each get their own cooldown and *together* hit the target at
twice the configured pace. Three workers, three times. Nobody asked for that, and against a
real program it is how an engagement earns an IP ban.

**The cooldown must move to per-engagement before parallelism is turned on.** Design for
parallel from the start, run one worker at a time until that is done.

---

## 2.6 Three tiers, because this machinery is wrong for a small job

Everything above trades flexibility for verifiability. That is the right trade for a run lasting
hours against a real program with money and reputation at stake. It is the wrong trade for "let
me poke at this for ten minutes", where it costs an expensive strategist turn to decide to do
the one obvious thing, a wave barrier with one worker in it, and a hypothesis record before a
single request goes out.

The temptation is a second, lighter engine. **That is the defect this project is already paying
for** — `dev_server` and `loop.py` diverged until neither had all the tools (§1), and building a
second one deliberately would be repeating it on purpose.

**The weight is in the orchestration layer, not in the tools, the broker, the graph or the
record.** So the tier changes orchestration and nothing else.

| | orchestration | memory | confirmation | roles |
|---|---|---|---|---|
| **low** | none — one agent, driven by a person | conversation context only | `confirmed_by: model`, no rule engine | one |
| **medium** | sequential waves, one worker at a time | graph + digest | `model`, raised to `rule` where a rule exists; refutation deferred to submission | strategist (also writes the report) + worker |
| **high** | parallel waves, §2.0 end to end | graph + digest | rules, then a separate verifier per finding | four |

**low** is what `dev_server` does today: a person talks, the agent calls tools. In the language
of this document, **the person is sitting in the strategist's chair** — which is the option the
owner declined as a *default* and which is exactly right as a *mode*. It needs nothing new
built: the strategist's console (digest, pick, dispatch) is the chat and graph UI that already
exists.

**medium** keeps the part that makes the system cheap — the graph, so context is rendered rather
than accumulated — and drops the per-finding and per-role overheads: one worker at a time, no
separate verifier until something is about to be submitted, and the strategist writes the report
itself.

### 2.6.1 The line that does not move with the tier

**A tier may remove orchestration. It may never remove the broker, the scope check, the audit
log or the evidence store.** Those four are not overhead, they are the product; a "fast mode"
that skips them is a back door we built ourselves.

This is testable and therefore is a test: **the broker path, the audit entry and the evidence
write are byte-for-byte identical across all three tiers.** That assertion is also what keeps
the tier system from quietly becoming three engines.

**The tier changes the assurance a finding can reach, never whether it can be recorded.** Per
§8.6.4 a model's own confirmation passes at every tier; low tier simply has no rule engine and
no verifier, so its findings top out at `confirmed_by: model` and say so in the label. The
submission boundary (§8.6.5 #1) is the same at all three tiers — a tier cannot lower it, and
"low tier" is not a way to get a thinly-assured finding out of the door unseen.

### 2.6.2 What low tier costs, stated plainly

* **Weaker training data.** No graph means no hypothesis records, no verdicts, no experiment
  boundaries — so a low-tier run produces a conversation, not the structured trajectory §8
  depends on. It is still recorded, and it is worth less
* **The long-session failures come back.** Memory is the conversation, so context growth,
  elision and drift all return. Acceptable because low tier is for short work, and the tier
  should say so rather than let a person discover it at turn 80

**Tier is set per engagement.** Upgrading mid-engagement is allowed and only adds structure, but
the portion recorded before the upgrade stays thin — and the record says where the upgrade
happened, so nothing downstream mistakes a thin history for a complete one.

## 2.7 The strategist must not be a state machine

A role defined by an action will always take that action. A strategist prompted as "decide what
to dispatch next" will dispatch something — including when the person said nothing more than
hello. Answering a greeting by commissioning a recon wave is not thoroughness, it is a system
that cannot tell the size of what it was asked.

Proportionate behaviour is mostly prompt engineering, and this is the one part of the system
where prompt quality *is* the mechanism. But there is an architectural half, and it is the same
lesson the cooldown taught: **if the only expressible action is to act, the model acts.**

So **"answer, dispatch nothing" is a first-class action with its own tool**, not an absence of
one. Reading a node and replying, or sending one short check, is a legitimate and complete
response. The scale of the reply is the strategist's judgment, exactly as the scale of the work
is the worker's.

**No message classifier in front of it.** A router that sorts "chit-chat" from "instruction" is
a new failure point whose failure mode is dropping a real instruction — far worse than paying
for one cheap strategist turn on a greeting. Let the strategist see everything; give it a way to
do almost nothing.

And because proportionality cannot be guaranteed by prompting, it is **measured**: a turn that
spends and produces no progress is already recorded (§7.1). A strategist that commissions five
workers in response to a greeting shows up as spend with no progress, on a dashboard, the same
day. **Prompting sets the behaviour; measurement catches it drifting.**

## 3. Memory

### 3.1 The finding that shapes this section

`agent/hypothesis_graph/` already contains a complete anti-forgetting design:

* `digest_line()` — its docstring states the principle: *"`ย่อ ≠ ตัด`: every hypothesis gets a
  line, the line is just short. The single point of failure the anti-forgetting design rests
  on, so it is **built deterministically from stored fields, never written by the model after
  the fact**."* That last clause is the anti-hallucination property: the digest cannot drift,
  because no model writes it
* `build_digest()` — *"Root + active path are **guaranteed** present — they must never fall out
  of context"*
* `graph_search` — *"Use this instead of **guessing** an id or a past result you don't see in
  the digest"*
* `graph_read_branch` — *"**your backtrack tool**. Use it before **assuming anything** about
  earlier hypotheses not shown in the digest"*
* `EdgeType.SYNTHESIZED_FROM` — *"this claim combines several source hypotheses"*, inside
  `CAUSAL_EDGE_TYPES`

**The engine with the UI uses none of it.** `dev_server` keeps its own in-memory hypothesis
store with no lifecycle status, no verdict, no experiments, no edges and no digest, and relies
on transcript plus LLM summarisation instead. `loop.py` can reach the real one, behind
`use_hypothesis_graph: bool = False`. And `synthesized_from` has no production writer at all —
only a test.

So the memory architecture is not something to invent. It is something to stop discarding.

### 3.2 The model

* **Engagement = durable state.** Graph, notebook, findings, evidence store. On disk already
* **Session = a worker that advances one hypothesis and dies**
* **A worker's context is rendered from the graph, not inherited from a chat log**

This answers the objection that a short-lived worker never sees the whole picture, because
"the whole picture" has two meanings that have to be separated:

| | what it is | who needs it |
|---|---|---|
| every byte of every response | the raw transcript | nobody, and no model can hold it |
| every established fact | surface, confirmed findings, primitives held, dead ends | chaining needs exactly this, and it is small |

**The long session does not provide the second one either.** `_fit_context` elides and
`_maybe_compact` replaces history with LLM-written summary. Measured on the pre-fix code: a
single turn produced 13 distinct prompt prefixes across 25 rounds — the model's view of its own
history was being rewritten continuously. A rendered digest is strictly better at holding
facts than a transcript that is hoped to still contain them.

### 3.3 Push and pull, and which is actually cheaper

Both. The split is by probability of use, and the reasoning is **not** the one this project
assumed before prompt caching worked:

| | cost |
|---|---|
| data sitting in the **cached prefix** | ~10% of input rate, per turn |
| data fetched by a tool call | **one full generation** — output tokens, full input, latency |

A pull costs more than leaving the same fact in the prefix for dozens of turns. So:

* **used almost every time → push** into the cached prefix (this is what `build_digest` does,
  with root and active path guaranteed)
* **rarely needed, valuable when needed → pull** via `graph_search` / `graph_read_branch`

Pull's justification is not cost. It is that **an agent with no way to look something up will
guess**, and it will do so fluently. Push-only does not remove hallucination, it moves it
somewhere unobservable.

**Pull needs a ceiling.** An unbounded query tool invites the same flailing already measured
in this system, where a model burned one to six turns on `sleep` rather than act. The
trajectory records a **query-to-action ratio**, which is also the metric that tells us whether
our own brief rendering is any good: a high ratio means the push side is wrong.

### 3.4 Prefix layout, which matters more than push-vs-pull

**Every worker's system prompt and tool block must be byte-identical.** Worker-specific content
goes after it.

Prompt caching is prefix-matched. Worker-specific text inside the system prompt gives every
worker a unique prefix and **zero cache reuse between workers**. Placed correctly, worker 2
onward reads worker 1's cached prefix. In an architecture that runs many short workers this is
a larger cost lever than any model-routing decision.

### 3.5 One schema addition: `primitive_gained`

`Verdict` says whether a claim is true. It does not say **what capability we now hold**.

Chaining is set reasoning over capabilities — `arbitrary_file_read`, `ssrf_outbound`,
`low_priv_token`, `sqli_read`, `rce` — not over vulnerability names. A strategist reading five
primitives sees a path faster and more reliably than one reading forty lines of prose, and the
field is what makes chains findable without re-reading everything.

### 3.6 Scale

At 500 hypotheses, `ย่อ ≠ ตัด` cannot hold literally. The owner's rule: collapse into counted
groups rather than dropping. `"refuted: 42 (graph_search to list)"` still tells the model those
exist, which is categorically different from their absence.

---

## 4. The human in the loop — three channels, not one

The owner wants autonomy with intervention: suggestions, extra instructions, and tasks the
agent cannot do itself such as registering a test account.

That is three different things and the system currently has one.

| channel | meaning | who acts | exists today |
|---|---|---|---|
| **directive** | "do this / stop that / try this chain" | agent | partially — operator steer |
| **approval** | "may I do this?" | agent, after a yes | yes — `ApprovalQueue` |
| **`request_human_action`** | "I cannot do this. Please do it and tell me the result." | **the human** | **no** |

Without the third, an agent that needs an account will attempt registration for a dozen turns,
or — the worse failure, and one already observed in this project in another form — report that
it succeeded.

**Both approval and human-action requests must carry a rationale and an expected outcome.**
`ApprovalQueue.submit()` today takes `reason` only. The owner's requirement is explicit: the
approval screen must say why the agent wants to run this and what it expects to get. That is a
new field, and it is also the most useful thing in the trajectory for judging the agent's
planning quality.

### 4.1 Human edits: the future, never the past

The owner's rule: a person may add, through chat, and **may not alter what the AI laid down for
its own work** — it breaks flow, confuses the user, and risks making the model hallucinate.

This resolves a tension with the client UI, which is meant to support cutting a branch,
reordering a chain, retesting, and adding a hypothesis by hand. Those are not edits to the
record, they are **instructions about what to do next**. So:

**Every human action in the UI becomes a recorded, attributed directive — not a database
write.** The graph changes only when the agent acts on a directive, or when a person adds
clearly-attributed content of their own. `OriginType` already carries `USER_MESSAGE` and
`IMPORTED_MANUAL_FINDING` for exactly this.

Two things are preserved at once: the n8n feel in the client, and a trajectory where model
output and human correction are never silently blended. A dataset mixing the two with no
boundary is poison that cannot be detected afterwards.

---

## 5. Server and client

Two programs. The server holds all logic; the client is what the user touches.

### 5.1 Where this is not expressible today

`electron/main.js` builds `appUrl` as `http://127.0.0.1:${cfg.devServer.port}` and `config.json`
has no host field. The shell can only spawn a backend locally and connect to loopback. It is a
launcher, not a client. Condition 3 needs a second mode: connect to a remote server, send an
API key, spawn nothing.

Also: `devArgs` passes only `--port` and the RAG paths. No `--provider`, no `--model`. The
desktop app cannot select API mode at all, even with `startLlama: false`.

### 5.2 The API becomes a product boundary

Today the UI is served *by* the server: `dev_server` reads `static/index.html` and mounts
`/static`. If the client is its own program it must own its assets and speak only a **versioned
API**. Otherwise upgrading the server silently changes the client, and an old client against a
new server fails in ways nobody can diagnose.

### 5.3 Identity

The owner's rule: the same account may see everything; **a different account must never see
anything**.

Today's auth is a single shared key file, which cannot distinguish accounts at all. With two
users this does not need OAuth: **a key per account, an owner on every engagement, and every
read filtered by owner — enforced in code, with a test.**

This is also why `graph_search` must be hard-bound to the current engagement. It is not
tidiness; it is the boundary the owner just defined.

### 5.4 Server UI: health only

Per the owner: how many users, which model is connected, total spend, connection and
completeness status, errors. Nothing else. Every other view belongs to the client.

### 5.5 Client UI

> **Canonical: `CLIENT_UI_DESIGN.md`.** The owner's five-surface specification, the tear-off
> window model and the rendering agreement live there. What remains here is the summary other
> sections depend on.

Three surfaces, and the second and third are different things that an earlier draft of this
document wrongly merged.

* **Chat — the first screen.** The control surface: directives, suggestions and extra
  instructions all arrive here, which is what condition 1's "the human can intervene" requires
* **Hypothesis graph — already exists** in `static/index.html` (`renderGraph`,
  `renderGraphTree`, `renderNodeDrawer`, with a tree view, filters, stats and auto-collapse of
  concluded branches). It gains the ability to drag, retest, cut a branch, add a hypothesis by
  hand and reorder a chain — all as directives per §4.1, never as database writes
* **Flow view — the n8n one, and it is NOT the hypothesis graph.** It shows the *architecture
  in motion*: which role is reading what, who controls whom, who is working on which task,
  which tools are being called. Deliberately shallow — it answers "what is the system doing
  right now", not "what did it find". The hypothesis graph answers the second question and
  already does it well
* **Disconnection must be stated.** When the server is unreachable the client says so and says
  how many events it missed. A UI that keeps showing stale data as though it were live is
  lying to its operator

### 5.6 Export / import

Two shapes: **read-only** (inspect, ask questions, verify) and **resumable** (continue the
engagement). Evidence travels in both, encrypted. The resumable form is also the input to the
lab reproduction loop later.

---

### 5.7 Where the two new UI surfaces live

> **Superseded by `CLIENT_UI_DESIGN.md` §2 and §5.** This section proposed `static/flow.js` and
> no bundler. The owner's agreements 1 and 2 (scale is free; always live and always smooth)
> retire that: the client becomes its own program, built with React + Vite + React Flow, and
> `index.html` is kept as a reference implementation rather than extended. The reasoning below
> is left because the constraint it identified — a canvas cannot use the whole-page re-render —
> is still the reason, and a future change that forgets it will repeat the mistake.

`static/index.html` is 1,853 lines and 141 KB: one `<style>` block, one `<script>` block, a
single `S` state object, and view switching on `S.view`. No build step, no modules, no
framework — `renderX()` functions returning HTML strings.

**The AI browser is not a file-layout question.** A page served over HTTP cannot host a second
browser with its own cookie jar and profile. In Electron that is main-process work
(`BrowserView` with a `partition:` per engagement). And per §6 it is a *tool runtime*, not a
view. It lives in the Electron client from day one because there is no alternative.

**The flow view is a new file on the same page:** `static/flow.js`, loaded with a plain
`<script src>`. No bundler and no ES modules — none exists today, adding one is a new toolchain
and a new failure mode, and `/static` is already mounted so splitting costs nothing. It earns
its own file because it is the first view with a layout engine of its own (node placement, edge
routing, pan/zoom, hit-testing), and because appending several hundred lines to a file that
already holds the styles, the state and every view makes the next change worse.

**The existing code is not refactored into modules as part of this.** That is a separate change
with its own risk and no coverage to catch a regression (§5.8). The `<script src>` split also
makes the eventual move of the UI to the client (§5.2) a folder move rather than surgery.

### 5.8 The UI has 25 real-browser tests and none of them run in API mode

> **Revised by `CLIENT_UI_DESIGN.md` §10.** The conclusion below — that unblocking this suite is
> a prerequisite for steps 10 and 11 — was right that the tests matter and wrong about why. They
> exercise a page that is being retired, so repairing them gates nothing. Their value is as the
> written-down acceptance list of behaviour the new client must reproduce.

`test_frontend.py` drives the real page with a real browser across 25+ tests — notes, graph
filters, drawers, streaming, reasoning traces, tool grouping, panel resizing, session search.
A genuine safety net.

`TestFrontendBase.setUpClass` skips the entire suite when `llama-server` is unreachable. API
mode — the mode this whole checkpoint is about — therefore runs **none** of it.

So building the two largest UI features in this project's history would happen with no
automated coverage at all, in a codebase where six tests were found in two days that passed
while the bug they named was live.

**Making `test_frontend.py` runnable in API mode is a prerequisite for steps 10 and 11, not a
cleanup.** It needs *a* provider, not specifically llama: the gate becomes "is a provider
configured and reachable" rather than "is llama-server up".

This is the same pattern as §1 and §3.1 — a capability that exists in full and is unreachable
in the configuration actually in use.

## 6. The browser, on the client

The owner wants the agent to have its own browser with its own sessions, accounts, cookies and
tokens, running on the client so the human can hand it a registered account.

Correct, and for a stronger reason than convenience: the target may require the human's network
position, and 2FA and cookies live with the human.

**But the client must stay a hand, not a brain.** The browser is a tool the **server dispatches
to the client**: the broker authorises it server-side, scope check runs server-side, the audit
entry is written server-side. A compromised client still cannot act outside scope.

### 6.1 Session boundaries

| | shared? | why |
|---|---|---|
| the human's browser ↔ the agent's browser | **never** | sharing hands the agent every cookie the human holds, including unrelated sites — a scope violation and a credential exposure at once |
| worker A ↔ worker B, same engagement | **yes** | **chaining requires it.** If A authenticates, B must be able to continue from there |
| engagement A ↔ engagement B | **never** | the owner's account rule |

One rule: **one browser profile per engagement.** Handing over an authenticated session from
the human to the agent is a deliberate, per-instance action ("give the agent this account"),
never automatic.

Cookies and tokens the agent observes are evidence: encrypted store, like any other. And they
must never reach an exported training set — see §8.3.

---

## 7. Cost: waste first, tokens second, cheaper models last

The owner's priority, which inverts this document's first instinct and is right:

1. **Eliminate tokens that buy nothing** — repeated identical calls, loops, and work that does
   not need a model at all
2. **Fewer tokens on the same model**
3. **A cheaper model**, only for genuinely easy work

The first two cost no quality. The third always costs some.

**The governing rule: if it can be code, a model must not do it.** This project has already
paid for the lesson — waiting out a two-second cooldown cost one to six full generations
because a decision code could make was handed to a model instead.

**Tiering, where used, is made safe by one principle: a cheap model may propose; only evidence
confirms.** A cheap model can be wrong without costing quality as long as it has no authority
to confirm anything. Remove that principle and tiering is just quality traded for money, which
condition 2 forbids.

### 7.1 Waste has to be measurable before it can be reduced

There is no spend accounting today. `agent/budget.py` is context fitting, not money;
`ProviderCapabilities.cost_per_mtok` and OpenRouter's usage reports exist but nothing
accumulates them.

The trajectory must record, per turn: cost, wall-clock, and **whether the turn produced
progress** — a graph write, a new observation, a verdict. Turns that produced nothing are the
waste, and without that field the first priority above is unmeasurable.

### 7.2 Budget exhaustion stops, it does not degrade

Per the owner: on hitting the budget, stop and report what is outstanding and what each
remaining avenue looked like. "Continue more cheaply" is quality lowered silently, which is
the one thing condition 2 rules out.

---

## 8. Trajectory records

### 8.1 This is a prerequisite for acceptance test A, not only for training

"More, faster, more severe, cheaper" cannot be measured with anything that exists today. Cost,
wall-clock and severity have to be in the record from the first run.

### 8.1.1 One record per experiment, and three things derived from it

* **The unit is the experiment** (§2.2): one attempt, with `method_summary`,
  `expected_observation`, `observed_result`, cost, wall-clock, and the per-turn progress flag
* **A chain is a link, not a copy.** It references the experiments that compose it, and each of
  those carries a reference back to the chain's outcome, written afterwards and append-only.
  This is what retro-credits an experiment whose value was not knowable when it finished
* **A strategist turn is a record too**, and it stores the options it declined alongside the
  one it took. Its label — did the chosen branch pay off, did the deprioritised ones — is
  computed from the graph later, with no human labelling

### 8.2 The metric, stated carefully

Raw vulnerability counts are the easiest number to game — duplicates, low-severity noise,
findings with no evidence. Proposed:

> **unique, evidence-confirmed, severity-weighted findings per dollar and per wall-clock hour,
> on an identical target set, with the model pinned and scope identical.**

Both systems run against our own lab targets. The owner's bar: slightly more expensive and
slightly slower is acceptable; **the results must be better.**

### 8.3 Training eligibility is decided at export, in three classes

The owner's rule: train what can be trained; for what cannot, ask whether the sensitive content
can be censored — if yes it becomes trainable, if no it is eval-only.

So collection does not decide. **Everything is stored raw and tagged** (the owner's answer on
failed trajectories: keep all of it, tag it, filter later). Then one gated export step, pressed
by a person and recorded, classifies each trajectory:

| class | condition |
|---|---|
| **trainable** | lab or self-owned target, no third-party data |
| **trainable after scrubbing** | real target, and the scrubber reports a **complete** pass |
| **eval-only** | real target where scrubbing cannot be shown complete |

This puts a hard requirement on the scrubber: it must **report completeness, not best effort**.
A scrubber that returns "probably fine" cannot be the gate on a decision that is unreversible
once a model is trained — removing a dataset afterwards does not remove it from the weights.

### 8.3.1 Verdicts, and the ones that are not pass or fail

Three values, not two: **confirmed**, **refuted**, and **confirmed but no longer
reproducible** — the last carrying a reason from a closed list that includes "cause unknown"
(§14.2 H). A re-run that fails does not erase the raw evidence that it once worked.

Severity attaches to the **chain**, not to its strongest link (§14.2 I). Two low findings that
compose into a critical one are reported as one critical finding.

### 8.4 What makes a trajectory good

Per the owner: **sound reasoning that failed beats luck that succeeded.** A trajectory that
succeeded for the wrong reason teaches the wrong thing. So the record keeps the plan, the
expectation, and the outcome separately, which makes "the reasoning was right, the target was
not vulnerable" a distinguishable and valuable case rather than a failure to be discarded.

---

## 8.5 False negatives — the asymmetry this design was not reasoned about

Every gate above exists to stop a false claim getting out. **None of them was designed to stop
a true claim being lost.** That is not a flaw in any one gate; it is a property of how the
design was reasoned. The question asked repeatedly was "how could this be wrong", and never
"what if it is right and we drop it".

What makes it worse than a symmetric problem:

> **A false positive is visible** — it is submitted and rejected, and we learn.
> **A false negative is invisible** — nothing ever reports what was missed.

So **false negatives have to be made visible by construction**, because nothing else will do it.

### 8.5.1 What is already safe

* **Recording is not broker-gated.** `loop.py` states it: *"local bookkeeping, not a network
  call, so nothing for the broker to gate"*. A tainted session can still record a finding
* **This failure class has already occurred here, and was fixed today.** The four `record_*`
  tools sat behind the security toggle while the system prompt demanded them, so a model that
  tried to record a finding failed. That is a false negative produced by configuration, in this
  codebase, discovered by accident
* **One detector already exists and is wired.** `engine.suggest_confidence_band()`, called from
  `service.py`, flags a model's self-assessed confidence diverging from the weight of its own
  observations — *"so a wildly divergent self-assessment is visible"*. It catches a model
  underselling what it found

### 8.5.2 Eight ways a true finding can be lost

1. **The confirmation rule was a gate, and a gate with an empty rule library loses everything.**
   An earlier draft let a claim reach `confirmed` only through a rule, so a vulnerability class
   with no rule yet could never reach the reporter and disappeared — **a false negative
   manufactured by our own gate.** *Fix, and it is the owner's §8.6.4 decision: the rule is not
   a gate. A model's own confirmation passes at every tier, labelled `confirmed_by: model`, and
   a rule raises assurance rather than granting permission. "No rule for this claim type" is a
   counted gap in the rule library, not a queue a finding waits in.*
2. **A narrow rule is worse than no rule.** DOM-based XSS never appears in the HTTP response
   body; a body-matching XSS rule returns false, and if that reads as `refuted` the system has
   destroyed a true finding and looked authoritative doing it. *Fix: a rule's negative result is
   **"rule did not match"**, never `refuted`. Only evidence that contradicts the claim refutes
   it.* That distinction did not exist in this design.
3. **The verifier carries the exact flaw §2.7 was written about.** A role whose success
   condition is refutation will refute. The flaw was caught for the strategist and left in the
   verifier. *Fix: "could not refute" is a first-class, equally valid outcome, and verifier
   quality is measured by whether its refutations survive scrutiny — never by how many it
   produces.*
4. **Incidental discoveries.** A worker testing X that notices a stack trace leak has no home
   for it if its only output is a verdict on X. `spawned_by` is the mechanism; what is missing
   is that **recording an observation outside one's own task is mandatory, stated in the
   brief.** Noise is cheaper than a lost finding, and the digest collapses groups anyway.
5. **A worker cut off by its wall-clock limit** (§2.5) may have been seconds from proving
   something, and a partial report may not carry enough to resume. *Fix: a timed-out worker
   reports **what it was about to do next** — its in-flight `expected_observation` — so the next
   attempt continues instead of restarting.*
6. **"Exhausted" is self-declared, and only over-work is detected.** Spend ceilings and loop
   detection catch a *stuck* worker; nothing catches a *lazy* one. `coverage()` already computes
   `completed / planned_tests`, so "declared exhausted at 20% coverage" is computable and must
   be surfaced to the strategist rather than accepted silently — alongside
   `suggest_confidence_band`, which already exists for the same purpose.
7. **Out-of-scope observations.** Not acting is correct; recording nothing is not. *Fix: the
   observation becomes a hypothesis in `ABANDONED` with reason `out_of_scope`.* "There is
   probably something here that we may not touch" is information the owner may want — to ask for
   scope, or to report it anyway.
8. **Items parked awaiting approval can sit forever**, since parking is unlimited by the owner's
   rule. If the person is away and a finding was behind an approval, it is deferred
   indefinitely. *Fix: at step 10 of §2.0 — when a person says to write it up — they are shown
   "N items are parked awaiting your approval." An engagement cannot be wrapped as though
   nothing were pending.*

### 8.5.3 The one mechanism that ties all eight together

**Everything that was not recorded as a finding needs a counter.**

No rule existed. The rule did not match. Out of scope. Parked for approval. Exhausted at low
coverage. Refuted while the raw evidence shows it once worked.

**A "near miss" view is the only way a false negative becomes observable**, and it is nearly
free, because every one of those outcomes is already being recorded. What is missing is the
place that adds them up and puts the number in front of a person.

## 8.6 What a confirmation rule actually is, and why it is not a gate

### 8.6.1 Nothing enforces this today, and the code says so

`agent/findings/model.py` documents the hole it was built around:

> *"a model must never self-promote a finding to confirmed without going through a verification
> rule — Phase 4 doesn't have that verification pipeline yet ... **the schema is ready for it but
> nothing here enforces it automatically yet; callers are responsible** for only marking
> `status="confirmed"` when they actually have verified evidence"*

So a model can set `confirmed` by writing the word. `confidence` and `status` default to
`needs_validation`, which is the right default, and nothing moves them but a caller's say-so.

### 8.6.2 A rule is a Python predicate, not a DSL and not a model

* **Not a DSL or YAML.** Anything past string matching outgrows it, and then we have built a
  poor programming language with no debugger and an interpreter that itself needs tests
* **Not a bare regex.** Too weak to express access control
* **Not an LLM judge.** That destroys the only reason the mechanism exists

Plain predicates are testable with the infrastructure already here, can point at the bytes that
satisfied them — which is the evidence slice (§14.2 L) — and add no new language.

### 8.6.3 The signature, and the mistake it avoids

The obvious shape is `rule(request, response) -> bool`. **That would bake a false negative into
the type system**, because the highest-value classes are confirmed by *comparing observations*,
not inspecting one:

| class | confirmed by |
|---|---|
| IDOR | same request, two identities, different data returned |
| auth bypass | with and without the token, same data returned |
| privilege escalation | a low-privilege account reaching a high-privilege function |
| race | two concurrent requests, inconsistent resulting state |

Access control is the most frequently paid class in most programs. A one-observation signature
makes all of it permanently unconfirmable. So a rule takes a **set** of observations — which the
store already holds per hypothesis, each with polarity and strength.

```python
def rule(claim, observations, context) -> RuleResult
```

```python
@dataclass(frozen=True)
class RuleResult:
    matched: bool | None          # three states, see below
    reason: str                   # recorded either way
    slice: EvidenceSlice | None   # the bytes that satisfied it
    primitive: str | None         # the capability it grants
```

| value | meaning | effect |
|---|---|---|
| `True` | found it | strengthens the confirmation, grants the primitive, cuts the slice |
| `False` | **looked, not there** | weak negative signal — **never `refuted`** (§8.5.2 #2) |
| `None` | **this rule does not apply to this claim** | no information either way |

`False` and `None` are genuinely different: a body-matching XSS rule meeting a DOM-based XSS
claim must answer `None`, not `False`. It did not look in the wrong place; it is the wrong
instrument.

**A rule that needs a comparison and has only one observation answers `None` with a reason that
names what is missing** — which turns confirmation into a work instruction. "Fetch the same
resource as a second identity" is something a worker can act on; "prove it more convincingly"
is not.

### 8.6.4 Owner's decision: a rule strengthens, it does not permit

An earlier draft had `None` route to a human queue. **The owner overrode this, and was right:**
with an empty rule library almost everything lands in `None`, so on day one the system can
confirm nothing. That is not a safety property, it is a system that does not work. It also
breaks the autonomy the whole checkpoint is for — every novel class would stop and wait for a
person who is asleep.

**So a model's own confirmation passes.** And the owner's reasoning for why that is safe is the
strong part: **downstream use is itself a test.** If the strategist chains on a false primitive,
the next worker tries to use that capability and it does not work — an empirical check that is
often stronger than a pattern match, because it tests the capability by exercising it.

A rule therefore does not grant permission; it raises assurance:

```
confirmed_by: model        ← the model asserted it; this passes
            → rule         ← a rule existed and matched
            → differential ← a comparison rule was satisfied
            → downstream   ← something used the primitive and it worked
            → human        ← a person signed it off
```

**Assurance is a property, not a gate.** Which is what "never lower the quality" actually
requires: quality measured and labelled, rather than work blocked.

### 8.6.4.1 When a rule says no and the model says yes

The owner's call: believe the model, and no harm done. The first half is right. The second needs
one correction, because there **is** a harm and it is not the finding.

**A rule returning `False` against evidence the model calls positive is a defect report about
the rule.** An XSS rule looking for the exact payload, against an app that reflected it with
light encoding that still executes: the model watched it run, the rule found no match. The
model is right and the rule is broken.

Believe the model and walk on quietly, and **we have thrown away the only bug report the rule
library will ever receive.** Nobody else is going to tell us that the XSS rule misses encoded
reflections. The disagreement is the sole detector.

Which way it resolves, both outcomes are useful:

| downstream outcome | what it means |
|---|---|
| the model was right — the primitive worked, or a person agreed | **the rule is broken; go fix the rule.** A concrete, high-value work item that is invisible unless disagreements are counted |
| the model was wrong — a later observation contradicted it | **the rule was right.** Trust it more next time, and record a measured model false positive in a case the rule caught |

**So a rule can be retired by its own record.** The ratio of "rule said no, model was right" to
"rule said no, model was wrong", per rule, is that rule's quality score. A rule that is wrong
more often than the model **should be switched off**, because a rule worse than no rule is one
that emits authoritative-looking `False` — precisely the failure §8.5.2 #2 describes. The rule
library cleans itself, with nobody reviewing it by hand.

**The label must carry the disagreement.** `confirmed_by: model` and `confirmed_by: model` with
a rule arguing against it are not evidence of equal weight; the second is weaker. A
`rule_disagreed: rule_id@version` field keeps them distinguishable, which matters most at export
(§8.3): a disagreement must not be laundered into an ordinary confirmation on its way into
training data.

And the owner's "no harm" holds for the outcome, because `confirmed_by: model` still cannot
cross the submission boundary unseen (§8.6.5). A rule-versus-model disagreement cannot reach a
submitted report quietly.

**Believe the model — but count it.** Not counting is the only real harm.

### 8.6.5 The three things that make that decision safe

**1. Loose inside, strict at the submission boundary.** A false positive circulating internally
is cheap and self-correcting. One that reaches a *submitted report* is reputation, and programs
do ban for repeated invalid reports. So `confirmed_by: model` alone **does not cross the
submission boundary without a person seeing it.** Full autonomy inside; the expensive edge
stays guarded.

**2. The label is mandatory, or the training data is poisoned.** A model-confirmed finding that
was wrong, recorded as plain `confirmed` and then trained on, teaches the model that **its own
assertion is truth.** `confirmed_by` costs nothing and lets the export (§8.3) choose which
assurance levels it will learn from.

**3. "It comes back and checks itself" needs a mechanism.** Left implicit it will not happen,
because a worker who cannot use a primitive records *its own failure*, not that the primitive
was false. So: **a primitive carries a reference to the hypothesis that granted it, and failing
to use it files a contradicting observation against that hypothesis.** `EdgeType.CONTRADICTS`
already exists — *"source conflicts with target"* — so this is wiring, not new structure. A
hypothesis contradicted this way is closed with its reason recorded, and that record is a
**negative example of self-confirmation**, which is precisely what training should learn from.

### 8.6.6 Rules need versions and both kinds of test

**Versioned.** The finding records `rule_id@version`. Change a rule later and past
confirmations were made under the old one; without the version, editing a rule silently rewrites
what history meant — losing both the audit trail and the training label.

**A positive and a negative fixture, or it does not get registered.** A rule with only passing
cases is a rule that says yes to everything. Six tests were found in two days here that passed
while the bug they named was live, so this is enforced the same way everything else in this
project now is: a test walks the registry and asserts every rule has at least one matching and
one non-matching fixture.

### 8.6.7 The measurement this decision unlocks

Before: a gate, and **no way to measure what it caught.** Nothing bogus ever got through, so the
model's actual error rate was unobservable.

After: claims pass, some are later contradicted — which yields **a real false-positive rate, per
model and per claim type.**

That number is also the work queue: **write rules for the classes the model gets wrong most
often**, rather than writing them in whatever order they come to mind. The owner's decision
replaced an unmeasurable gate with a number that directs the next piece of work.

## 9. Concrete security changes this architecture requires

1. **`local_state_read` as a new action class, inside `SAFE_WHILE_TAINTED`.** Today
   `SAFE_WHILE_TAINTED = {"passive_recon"}`, so a tainted session needs human approval to read
   its own notes — which send nothing anywhere. With workers depending on `graph_search`, a
   tainted worker becomes unable to remember. This is not loosening `passive_recon`; it is
   recognising that reading our own database was never the same category as touching a target
2. **Cooldown moves from per-session to per-engagement** — required before parallelism (§2.5)
3. **Per-account keys, engagement ownership, owner-filtered reads** (§5.3)
4. **`graph_search` hard-bound to the current engagement**, with a test
5. **Permissiveness bound to the engagement record.** Destructive actions are never available
   generally. An engagement may be created with a named target list and an explicit flag, and
   that is the only way the looser mode can exist. There is no system-wide switch, so there is
   no switch to press by accident, and a normal engagement created later inherits nothing
6. **The browser runs client-side, authorised server-side** (§6)
7. **A primitive is granted by rule, never by model assertion** (§14.1 A) — the same treatment
   `confirmed` already gets
8. **`technique_kb` scoped per account** (§14.1 B) — it is global today and auto-injected every
   turn, which contradicts §5.3 in shipped code
9. **Taint escalates to the engagement when the vector is engagement-shared state** (§14.1 C) —
   a shared browser profile makes per-session taint unsound
10. **Spend is reserved atomically, not checked then spent** (§14.1 D)

---

## 10. What to remove

**Transcript summarisation as a memory mechanism.** Taking an LLM, summarising a transcript,
and using the summary in place of history is lossy and itself a source of hallucination. With
structured state there is nothing to summarise.

---

## 11. What this changes in `API_MODE_DESIGN.md` §5

**Step 3 (append-only history) drops in priority.** That whole step exists to make a long
session survivable. With short workers the context rarely approaches the budget, and when it
does the correct answer is "this worker's scope was too wide, split it", not "summarise it
away".

The prompt-cache work already landed stays fully relevant — a worker still re-sends its prefix
many times, and §3.4 makes the prefix shared across workers.

This is recorded because the alternative was building step 3 first and discovering afterwards
that it had been built for an architecture we had replaced.

---

## 12. Build order

Each step is useful alone, and each has a test that fails before it and passes after.

| # | step | conditions served |
|---|---|---|
| 0 | **the four live conflicts from §14.1 B, C, D and the engagement-level event stream (M)** — these contradict decisions already taken, so they come before new work | 2, 3, B |
| 0a | **the tier parity test** (§2.6.1): broker path, audit entry and evidence write identical across low/medium/high. Written before the tiers exist, so no tier can ever be born with a hole in it | B |
| 1 | tool-parity assertion: every tool reachable from the engine with the UI (red today) | 1 |
| 2 | `dev_server` uses `HypothesisGraphService` instead of its in-memory lookalike | 1, 2, 5 |
| 3 | engine consolidation onto the single tool plane, one tool at a time | 1, 2, 5 |
| 4 | `primitive_gained` **granted by confirmation rule** (§14.1 A); strategist + worker roles; the wave loop with per-worker wall-clock limits; claiming on `(hypothesis, method)`; the recon entry path (§14.1 G) | 2, 5 |
| 5 | cooldown per engagement; `local_state_read`; then enable parallel workers | 2, B |
| 6 | **`confirmed_by` on every finding** (§8.6.4) + the submission-boundary check + the `CONTRADICTS` wiring for downstream failure. This comes before any rule is written, because it is what makes model-confirmation safe | 5, A, B |
| 6c | **confirmation rules** themselves — predicates over a set of observations, versioned, each with a positive and a negative fixture (§8.6). Written in the order the measured error rate dictates, not all at once | 5, A, B |
| 6d | **the disagreement counter** (§8.6.4.1): `rule_disagreed` on the finding, resolution by downstream outcome, and a per-rule quality score that retires a rule worse than no rule | A, B |
| 6b | **the near-miss view** (§8.5.3): one counter per way a true finding can be lost. Cheap, and the only thing that makes a false negative observable | A |
| 6a | trajectory record v1: cost, wall-clock, progress flag, provenance, query:action; chain links; strategist turns with declined options | 5, A |
| 7 | spend accounting and the waste report | 2, A |
| 8 | verifier role (confirms what code cannot); finding gate; reporter; `graph_read_branch` wired for the strategist and the two-stage full sweep (§2.0.1) | A |
| 9 | per-account identity; Electron client mode; versioned API | 3 |
| 10 | `test_frontend.py` runnable in API mode — prerequisite for the two below | 4 |
| 11 | flow view in `static/flow.js`; graph view gains directives | 4 |
| 12 | browser as a client-dispatched tool, one profile per engagement | 1 |
| 13 | scrubber with completeness reporting; three-class export | 5 |

Step 2 is the highest value per line of code in this project: it is deletion plus wiring, and
`hypothesis_graph/` already has its own passing tests.

---

## 13. Decided here, open to veto

These were taken as small-technical under the owner's instruction, and are listed so they can
be overridden:

* ~~Four roles rather than two~~ — **confirmed by the owner.** Reporter stays separate from
  strategist because a model that both decides and writes the report tends to write up its own
  reasoning as though it were evidence
* ~~The query ceiling is per worker~~ — **overridden by the owner: there is no ceiling.**
  Exhaustion is the worker's own judgment (§2.2). The query:action ratio is still recorded,
  because it is the measurement that tells us whether the brief or the model is at fault; it
  just does not gate anything. Pathology guards per §2.2.1 remain
* "All avenues exhausted" is defined as: nothing in `OPEN`/`QUEUED`/`RUNNING`, and everything
  remaining is `AWAITING_APPROVAL` or `PARKED`
* Lifecycle states are used as the existing schema already defines them:
  `AWAITING_APPROVAL`/`BLOCKED` = waiting on a human, `PARKED` = out of ideas but revivable,
  `ABANDONED` = out of scope, duplicate or untestable, with a reason

---

## 14. Second-pass review: holes, conflicts and costs

A design document that does not state its own weaknesses has the same defect as a test that
passes while the bug is live. These are the results of reading this design adversarially,
ranked by whether they can be deferred.

### 14.1 Must be resolved in the design — they are load-bearing

**A. `primitive_gained` is the most load-bearing field here, and the owner's step 2.2 is its
answer — with one detail left.**
Chaining is set reasoning over primitives (§3.5), and the strategist's entire value rests on
them being true. A worker that proves a path traversal on one endpoint and records
`arbitrary_file_read` has handed the strategist a false capability to plan on.

The walkthrough's step 2.2 states the mechanism: a worker writes a **proposal**, and
`confirmed` comes from **checking the proposal against the raw evidence**. The open detail was
*who checks*. Split by claim type:

* **mechanically checkable** — reflected XSS (the payload appears unencoded in the response),
  a version banner, an open port: **code decides** → `confirmed_by: rule`
* **needs judgment** — IDOR, broken access control, business-logic flaws: **the verifier
  decides** where there is one, and per the owner's answer on roles it is a different model
  instance from the one that proposed it → `confirmed_by: rule` (differential) or a verifier pass
* **neither available** — a novel class with no rule, at a tier with no verifier: **the worker's
  own assertion stands**, labelled `confirmed_by: model` per §8.6.4

The invariant is therefore *not* "whoever found it never confirms it" — §8.6.4 retired that,
because with an empty rule library it confirms nothing on day one. The invariant that survives
is narrower and stronger: **a worker can assert, but it cannot raise its own assurance level.**
`rule`, `differential`, `downstream` and `human` are all awarded by something other than the
finder; `model` is the floor, and the floor is honest about being the floor.

The primitive follows the same split. Where a rule exists, the primitive is a property of the
rule rather than of a sentence a worker wrote. Where none does, the primitive is model-asserted
and carries the granting hypothesis' id, so the first downstream worker that cannot use it files
a `CONTRADICTS` observation against it (§8.6.5 #3). **A model-asserted primitive is a claim with
a built-in test, not a fact.**

**B. `technique_kb` already crosses the account boundary the owner just drew.**
`agent/notebook/technique_kb.py` states it plainly: *"the ONE global store ... not
engagement-scoped"*, at `config.TECHNIQUE_KB_PATH`, and `NotebookService.build_context_block()`
calls `semantic_recall()` **every turn**, so past-engagement content is auto-injected without
anyone asking. Under §5.3 ("a different account must never see anything") this is a live
conflict in shipped code, not a design risk.

It should not be deleted — cross-engagement technique learning is how the system improves, and
it is one of the better ideas in this codebase. The requirement is that **a technique is
generalisable knowledge, not target data.** Two options: scope the KB per account (cheap,
correct now), or require a scrubbed form before a note enters the global store (needed anyway
for §8.3). Do the first now, the second when the scrubber exists.

**C. Taint is per session; a shared browser profile is not.**
`TaintStore(session_id)` keys taint on the session. §6.1 shares one browser profile across all
workers in an engagement, because chaining requires it. So worker A can process malicious
content, be marked tainted, and worker B — untainted, same cookie jar, same storage — carries
on with the material that tainted A. The taint model and the session-sharing model disagree.
**Taint must escalate to the engagement when the vector is engagement-shared state** (browser
profile, workspace), and stay per-session only for session-local content.

**D. Parallel spend has the same race the cooldown had.**
§7.2 stops at the budget. With N workers each checking "is there budget left" before spending,
all N can pass and collectively overshoot. This is the identical shape to the cooldown race
already fixed in the broker, and the fix is the same: **reserve atomically, do not check and
then spend.**

**E and F are withdrawn.** An earlier draft listed an unstable stop condition and a strategist
bottleneck as unsolved. **The wave model in §2.5 removes both**, and the fifth "dispatcher"
role proposed to absorb F is dropped. They are recorded here rather than deleted because the
reasoning that retired them — synchronise on the wave, not on the worker — is the load-bearing
idea, and a future change that breaks the barrier brings both problems straight back.

**E′. Claiming was bound to the wrong row.** An earlier draft had workers claim a *hypothesis*;
the walkthrough's step 7 dispatches three workers against one hypothesis with three methods, so
the claim must be on `(hypothesis, method)` — the experiment. Now fixed in §2.5.

**F′. A wave is as slow as its slowest worker.** The cost of a barrier, and it lands on
acceptance test A's "faster". Each worker is dispatched with a wall-clock limit (§2.5).

**G. Cold start: who writes the first hypotheses?**
Recon is enumeration, not a falsifiable claim, so "the strategist creates hypotheses and
workers test them" has nowhere to begin — the strategist would be guessing at a surface nobody
has looked at. `PHASES` already contains `RECON`, so the intended shape exists: a root claim
with a recon experiment under it, whose `observed_result` spawns the real hypotheses. This needs
to be stated as the entry path rather than left implied.

### 14.2 Resolved in review, and what is left

**H. Resolved — raw evidence is the authority, not the re-run.**
A finding that will not reproduce has not been shown false; the raw evidence still records that
it worked. So the verdict space gains a third value, **confirmed but no longer reproducible**,
and the verifier must say *why*, from a closed list — patched, session expired, rate-limited,
WAF now blocking, state consumed — which must include **"cause unknown"** as a first-class
option. Forcing a model to pick a reason it does not know is how fabricated reasons get
recorded as fact.

This is worth more than a technicality. "Exploitable at 14:32, here is the raw evidence, no
longer reproducing because the target has patched it" is a *stronger* report than silence, and
for a bug bounty it is often still payable, since programs do patch between discovery and
triage.

**I. Resolved — the chain is the unit of submission, and severity belongs to the chain.**
Walking the path back gives the reporter every link's evidence; `synthesized_from` is the edge
that makes that possible. The part that needed deciding is severity: if A is low and B is low
but A+B is critical, submitting them separately gets two "informational" ratings and loses both
the payout and the truth. **A chain is reported as one finding whose severity is a property of
the chain, not the maximum of its links**, and the write-up explains what it is composed of.

**J. Resolved — one record per experiment; the chain level is derived, not collected.**
An experiment produces exactly one trajectory record. A chain is **not** stored as a second
copy of those records — it is a link that references the experiments composing it, because
duplicating them doubles the storage and gives two versions to drift apart.

The load-bearing idea: **when an experiment finishes, its value is not yet known.** A worker
that proved SSRF and found nothing to do with it looks like a failure that day; three waves
later it is the middle link of a critical chain. So an experiment record carries a reference to
the chain outcome, **filled in afterwards, append-only** — never by rewriting the original.
That is the mechanism for the delayed-outcome problem `API_MODE_DESIGN` §4.2 describes, and it
is what lets a sound-but-unrewarded experiment be retro-credited instead of discarded.

**K. Resolved — strategist turns are recorded with the options it declined, and labelled
automatically afterwards.**
Every strategist decision is recorded: the digest it was shown, what it chose, and — the part
that carries the value — **what it chose not to do.** "Given eight open hypotheses it picked
the third" is a ranking signal, and ranking is the actual expert skill in this work. Storing
only the choice throws away half the information.

The label needs no human: whether the chosen branch produced a confirmed finding, and what the
deprioritised branches produced, is computable from the graph after the fact, deterministically.
This yields a preference dataset rather than an imitation one, which is the shape RL wants.

**L. Resolved — the evidence slice is cut by the confirmation rule, not by a size limit.**
A worker's request can return 200 KB. All of it goes to the encrypted store, so nothing is
lost. But the reporter has to show "the payload is reflected *here*", and 200 KB cannot go in a
prompt — so something must choose which bytes the model sees. Too few and the finding cannot be
proven; too many and we are back to the prompt sizes this architecture exists to avoid.

**The rule already knows which bytes matter.** The rule for reflected XSS is "the payload
appears unencoded in the response", so the rule can point at exactly the bytes that satisfied
it. The slice is therefore small, provably the relevant part, and **cut by code rather than
chosen by a model.** It always carries the request that produced it — a response without its
request proves nothing — plus a digest pointer to the full artefact so a person can always open
the original.

This also collapses three designs into one: **a single declared rule per claim type confirms
the claim (H and §14.1 A), grants the primitive, and cuts the evidence slice.** Three of the
problems in this review share one mechanism.

**M. The flow view's data source is only half there.** `EVENT_KINDS` covers graph events, not
"worker 3 called `http_request`". `debug_trace` has that but writes per-session files rather
than a stream. The flow view needs one ordered engagement-level event stream, which is also
what §5.5's "you missed N events" requires. Building it once serves the UI, the trajectory
spine and the reconnect counter.

**N. `graph_search` only prevents guessing if the model uses it.** A model that does not know
that it does not know will not search. Cheap mitigation: **the brief states what it omits** —
"3 sibling hypotheses not shown, 42 refuted (use `graph_search`)". That converts an unknown
unknown into a known unknown, which is the only form a model can act on.

### 14.3 Costs this design accepts, stated rather than hidden

* **Consolidating onto one engine removes the fallback path.** `loop.py` today is a second,
  independently working runtime; after §1 there is one. The reason to accept it is that two
  runtimes have already produced exactly the divergence that makes condition 1 unreachable, and
  a fallback nobody tests is not a fallback
* **Worker-per-experiment adds orchestration where a long session had none.** Claiming,
  dispatch, brief rendering and result merging are all new moving parts, and each is a new
  place to have a bug. What is bought: bounded context, attributable trajectories, parallelism,
  and an end to LLM-summarised memory
* **A deterministic digest cannot adapt to what a model actually needs.** That is the point —
  it cannot drift — but it means the brief is sometimes wrong for the task. `graph_search` and
  `graph_read_branch` are the escape hatch, and the query:action ratio is how we find out the
  brief was wrong
* **The verifier adds a model call and latency to every candidate finding.** Accepted: a false
  report costs more than any number of verifier calls
* **Four roles mean four prompts to maintain, and prompt drift between them is a new failure
  class.** Mitigation is the same as everywhere else in this project: the shared, byte-identical
  system block (§3.4) plus a parity test over role definitions

### 14.4 What this review did not cover

**Closed.** The client UI was the weakest part of this document and knowingly so — the one area
with no existing code to read and argue from. It now has its own document,
**`CLIENT_UI_DESIGN.md`**, written against the owner's five-surface specification, and §5.5,
§5.7 and §5.8 above defer to it.

Two things changed in this design as a result, and both are recorded there rather than hidden:
the client became a separate program (the owner's "always live, always smooth" cannot be met by
`index.html`'s whole-page re-render), and the engagement-level event stream (§14.2 M) moved onto
the critical path, because all five surfaces draw from it and none of them can start without it.

## 15. Not in this design

* The RL stage itself. This produces the data it needs and the lab loop it will run in
* Local-model inference. The checkpoint is explicitly external-API
* Automated submission. A human sends every report
* Multi-tenant beyond two accounts with hard separation

## 16. Decision ledger — where each decision lives, and what restates it

Three contradictions were found in this document on its final read, and all three had the same
cause: **a decision was argued out in prose in several places, so overriding it in one place
left the other places standing.** The owner's "a rule strengthens, it does not permit" was
written into §8.6.4 while §2.6's tier table still promised a human sign-off, §8.5.2 #1 still
prescribed a human queue, and §14.1 A still declared the opposite invariant outright.

That is a document defect of exactly the kind §14's preamble is about, so it gets a mechanism
rather than another careful read.

**One canonical section per decision. Everywhere else cites it instead of re-arguing it.**

| decision | canonical | restated in — re-read all of these if it changes |
|---|---|---|
| waste first, tokens second, cheaper models last | §7 | §7.1, §7.2, §2.6.2, §12 |
| parallelism synchronises on the wave, not the worker | §2.5 | §2.0, §14.1 E/F′, §7 |
| `confirmed` comes from the raw evidence, not a model's sentence | §8.6.3 | §14.1 A, §14.2 H, §14.2 L, §3.5 |
| a rule strengthens, it does not permit — no human queue | §8.6.4 | §2.6 table, §2.6.1, §8.5.2 #1, §14.1 A |
| believe the model over a disagreeing rule, and count it | §8.6.4.1 | §8.3, §8.6.5, §8.6.7, §12 row 6d |
| a worker cannot raise its own assurance level | §14.1 A | §8.6.4's ladder, §8.6.5 #3 |
| the submission boundary is absolute and tier-independent | §8.6.5 #1 | §2.6.1, §4, §8.3 |
| server UI is health only; the client holds everything else | §5.4 | §5.5, §5.7, §14.4 |
| one account may share everything; a different account nothing | §5.3 | §6.1, §14.1 B, §8.3 |
| the verifier is a different model instance from the proposer | §2.3 | §14.1 A, §14.3 |
| tier changes orchestration only | §2.6.1 | §2.6 table, §2.6.2 |
| the strategist is not a state machine | §2.7 | §2.1, §2.0.1 |
| the client is its own program; always live, always smooth | `CLIENT_UI_DESIGN.md` §1–2 | §5.1, §5.2, §5.5, §5.7, §5.8, §14.4 |
| editing happens on the hypothesis tree, never on the flow view | `CLIENT_UI_DESIGN.md` §6.2 | §4.1, §5.5 |
| the engagement event stream is the backbone of every surface | §14.2 M | `CLIENT_UI_DESIGN.md` §3–4, §5.5, §12 |

Two rules follow, and they are cheap:

1. **A decision may be restated for readability, but a restatement must cite its canonical
   section.** An uncited restatement is the thing that drifts, because nothing points a later
   editor at it.
2. **When a decision is overridden, the ledger row is the checklist.** Walk the "restated in"
   column before the change is called done. The three fixes above came out of doing exactly
   that by hand; the row is so the next one is not by hand.

This ledger is also the honest answer to "is the architecture finished?" — the design is
decided, and what remains is keeping its many statements of each decision in agreement. That is
maintenance, not design, and it now has a procedure.
