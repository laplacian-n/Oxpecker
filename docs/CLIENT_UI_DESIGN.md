# Client UI design

This is the document `AGENT_ARCHITECTURE.md` §14.4 reserved. That section stated plainly that
the client UI was the weakest part of it, because it was the one area with no existing code to
read and argue from. §5.5, §5.7 and §5.8 of that document are superseded by this one and should
cite it rather than re-argue it (§16's rule).

Scope: the program the operator touches. The server, the agent roles, the graph, the broker and
the record are decided in `AGENT_ARCHITECTURE.md` and are not re-opened here.

---

## 1. The three agreements this is built on

The owner set these as the central agreement before any screen was designed. Everything below
is a consequence of one of them, and where a design choice is made, the agreement it follows is
named.

1. **Scale is not a constraint here.** Whatever tooling the job needs may be used.
2. **Always rendering live, and it must stay smooth** — the mouse must not be dropped mid-drag,
   and typing must never be interrupted.
3. **Five surfaces. Each one opens inside the program, and each one can be torn off into its
   own window.**

Agreement 2 is the load-bearing one. It is not a polish requirement; it decides the entire
shape of the client, as §2 shows.

---

## 2. Agreement 2 makes the client a new program, not an addition

`app/agent/web/static/index.html` cannot meet it, and no amount of adding to it can.

The page re-renders everything on every state change — `document.getElementById('app').innerHTML
= renderApp()` (line 496) — and then has to repair the damage afterwards: it saves and restores
the text cursor by hand (`setSelectionRange`), and `render(bg)` (line 483) **refuses to render
at all** while a menu is open or the operator is typing:

```js
if(bg){ const a=document.activeElement;
  if(S.menu||S.ctxOpen||(a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')))return; }
```

So today "typing is not interrupted" is bought by **stopping the screen from updating**. That is
the exact trade agreement 2 forbids: live rendering and uninterrupted input are both required,
and this architecture can only ever have one at a time.

Combined with agreement 1 and with §5.2 of the architecture — the client owns its assets and
speaks a versioned API — the conclusion is not a refactor:

> **The client is a new program. `index.html` is kept as a reference implementation and is no
> longer the product** (owner's decision). It is not maintained in parallel; maintaining two
> full UIs is the `dev_server`-versus-`loop.py` mistake (§1) relocated to the front end.

### 2.1 What "smooth" actually requires, as rules

Stated as rules because "use a framework and it will be fine" is not true, and the failure is
hard to see until a wave is running and the screen is busy.

1. **Updates are component-local.** No code path replaces a subtree that contains focus, a text
   selection, a scroll position or an in-flight pointer gesture.
2. **Anything that fires faster than about ten times a second does not go through application
   state.** Token deltas, reasoning deltas, pan/zoom transforms, drag positions and the elapsed
   clock are written straight to the DOM through a ref. State updates are for things that change
   the shape of the screen, not for things that change a number on it.
3. **Input elements own their own value.** The event stream never writes into a field the
   operator is editing.
4. **Lists are keyed by identity, never by position**, so an event arriving mid-list does not
   recreate the rows below it.
5. **The two existing workarounds are deleted, not ported**: no skipping a render because
   someone is typing, and no manual cursor restoration. If either is needed again, rule 1 has
   been broken somewhere.

These are testable, so §11 makes them tests rather than intentions.

---

## 3. The rule that makes tear-off windows work

In Electron, separate `BrowserWindow`s do not share JavaScript memory. A shared store is
therefore not available, and trying to synchronise two stores through IPC is a bug farm.

> **No window owns state.** Every window is an equal subscriber to the engagement event stream,
> and holds only a projection it can rebuild from that stream.

A window opens by fetching a snapshot, then subscribing from that snapshot's sequence number.
Three things come free from this and would each be separate work otherwise:

* a window opened a minute ago and one opened three hours ago show identical content
* "disconnected, you missed N events" (§5.5 of the architecture) is one mechanism for all five
  surfaces, computed from a sequence gap rather than guessed at
* the time scrubber (§6.3) is the same projection run to an earlier sequence number — not a
  second data path

**Every surface is a route** (`/chat`, `/work`, `/flow`, `/browser`, `/worker/:id`). Tearing a
surface off opens the same route in a new window. There is no separate "popped-out" build of
anything, and no surface has two implementations.

---

## 4. What the client needs from the server, and it does not exist yet

This is `AGENT_ARCHITECTURE.md` §14.2 M, and it is worth restating that it blocks everything
here: today the stream is `/api/sessions/{session_id}/events`, bound to one session, and the
frontend binds every view to `S.activeSession`. **A wave is many sessions.** There is no pipe
that sees a whole engagement, so until there is, none of the five surfaces has anything to draw.

### 4.1 Shape

`GET /api/engagements/{id}/events` — Server-Sent Events, chosen over WebSocket because the
traffic is one-way (commands go over REST), and reconnection with `Last-Event-ID` is built into
`EventSource` rather than hand-rolled. Every event carries a **monotonic per-engagement
sequence number**; that number is what snapshots, resumption, missed-event counts and the time
scrubber are all built on.

`GET /api/engagements/{id}/snapshot?at=<seq>` — the projection as of a sequence number, so a new
window does not replay an eight-hour engagement from zero.

#### 4.1.1 Who assigns the sequence number — this is the cooldown race again

A sequence number is only useful if it is **gapless and strictly increasing**, and a wave has
several workers emitting at once. Each of them reading "what is the last sequence number" and
then writing is **check-then-act**, the identical shape to the cooldown race already fixed in
the broker and the spend race named in §14.1 D of the architecture — and the failure here is
quiet rather than loud: two events share a number, a reconnecting window computes a nonsensical
gap, and the scrubber replays a history that never happened.

> **One serialisation point per engagement. The number is assigned by the append, not read
> before it.** The event log is append-only with the sequence number allocated inside the same
> atomic append, so no caller can observe a number it has not already been given.

This is the third appearance of the same bug shape in this project. It is worth stating as a
pattern rather than a third one-off fix: **anywhere a shared counter is read and then written,
the read and the write are one operation or the design is wrong.**

#### 4.1.2 The stream is an account boundary, not just a pipe

§5.3 of the architecture is absolute — *a different account must never see anything* — and every
read is filtered by owner. **This endpoint is a read**, and it is the largest one in the system:
tool arguments, outputs, model calls, evidence pointers, spend. A stream that is not
owner-filtered hands another account the entire engagement in real time, which would be the
worst leak in the product arriving through the most convenient door.

So: **the subscription is authorised per engagement against the caller's account on connect, and
again on resume** — a client that reconnects with a `Last-Event-ID` for an engagement it no
longer owns is refused, not resumed. This gets the same test as the rest of §5.3, in the same
place.

#### 4.1.3 How far back the scrubber can go

The time scrubber (§6.5) replays the log, so the log's retention **is** the scrubber's range,
and the two must be one decision rather than two defaults that disagree.

The engagement's event log is kept for the life of the engagement and travels in the resumable
export (§5.6 of the architecture) — it is the trajectory spine, so it is not a cache and is not
trimmed to save space. The client holds a bounded window in memory and fetches older ranges
from the server on demand, which is why §3's "no window owns state" has to be true: a window
that had quietly become the only holder of old events would lose them on close.

### 4.2 The events each surface needs

| event | carries | drawn by |
|---|---|---|
| `wave_started` / `wave_ended` | wave no., dispatched experiments | flow, chat |
| `worker_spawned` / `worker_finished` | worker id, hypothesis, method, outcome | flow, tree glow, rail |
| `experiment_started` / `experiment_ended` | hypothesis id, attempt no., verdict | tree glow, rail |
| `tool_call_started` / `tool_call_finished` | worker, tool, argument digest, latency | flow, rail |
| `model_call` | model id, role, prompt/completion tokens, cost | flow model roster |
| `artifact_stored` | what was produced, which store it landed in | **flow wiring** |
| `graph_node_changed` | node, status, verdict | tree |
| `note_added`, `finding_recorded` | ordinal | notebook, findings tabs |
| `approval_required` / `approval_resolved` | request, blocked workers | flow node, drawer, chat |
| `budget_updated` | spent, reserved, remaining | flow, header |

`artifact_stored` is the one that is easy to leave out and should not be: it is what lets the
flow view draw where a worker's output actually went, which §6.4 explains is worth more than it
looks.

### 4.3 Versioning

Per §5.2 of the architecture the API is a product boundary now. The client sends its API version
on every request; a server that does not serve that version says so plainly and the client
refuses to run against it. An old client silently half-working against a new server is the
failure this exists to prevent.

---

## 5. Technology

**React + Vite + `@xyflow/react` (React Flow), bundled, vendored, no CDN at runtime.**

Verified rather than assumed: `@xyflow/react` 12.12.0 is **MIT** (npm registry, checked
2026-10-09). Its paid tier is additional components, not the core.

Why this and not something else:

* **React Flow is the thing the flow view is**: pan, zoom, hit-testing, custom node types, edge
  routing, viewport culling. n8n's own canvas is Vue Flow, the sibling project. Hand-rolling
  this is months of work to arrive at a worse version of a mature MIT library, and agreement 1
  explicitly removes the reason to hand-roll it
* **It serves §2.1 rule 2 properly** — the viewport transform stays out of React state, which is
  exactly the rule that keeps a drag smooth while events stream in
* **The hypothesis tree does not use it.** A tree is not a free-form canvas; plain DOM with CSS
  transitions is less code and animates the glow (§6.2) better
* **No CDN at runtime.** Dependencies are vendored into the build; a client that needs the open
  internet to render is unusable in the environments this tool is for

Rejected: keeping the string-rendering pattern (fails agreement 2, §2); a second UI framework
for the flow view only (two toolchains, one product); hand-rolled SVG canvas (cost without
benefit, given agreement 1).

---

## 6. The five surfaces

### 6.0 Layout

One main window. The centre shows one surface at a time; the **right rail is reserved** for the
live work of the strategist and the workers (owner's answer B). Seeing two surfaces at once is
done by tearing one off into its own window — which is what agreement 3 is for.

### 6.1 Chat — always the primary surface

In **medium and high this is the strategist's chat**, and that must be stated on screen rather
than implied: the same box in low tier is one agent talking to a person, and an operator who
mistakes one for the other will misread everything else.

It is the control surface. Directives, suggestions and extra instruction all arrive here, which
is what condition 1's "the human can intervene" requires. Approval requests also surface here
(§6.5), not only in the drawer.

### 6.2 Work — hypothesis tree, notebook, findings as one unit with three tabs

These three are one surface with three tabs, and tear off together as one window (owner's 3.2).
They are the same question looked at three ways — what we think, what we noticed, what we are
prepared to claim — and splitting them into three windows would mean three places to look for
one answer.

**The tree gains exactly one thing: a glow on the nodes a worker is working on right now**
(owner's 3.2.1). It pulses, it does not move anything. Live motion on a tree someone is reading
is noise; live *marking* is information.

The node drawer, filters, stats and auto-collapse of concluded branches already exist in the
reference implementation and are ported as-is in behaviour.

**Editing lives here and nowhere else** (owner's question E, and the owner's instinct was
right): drag, retest, cut a branch, add a hypothesis by hand, reorder a chain. Three reasons,
the second being the one that matters most:

1. what is being edited is a **hypothesis**, and this is the hypothesis surface
2. **the flow view's diagram cannot be changed by dragging it.** Allowing a drag there would
   advertise an authority that does not exist
3. the flow view is in constant motion, and editing on top of motion is unpleasant

Per §4.1 of the architecture, an edit is a **directive about the future, never a write to the
past**. It appears immediately as a pending overlay on the node and resolves when the server
accepts it.

**Dragging on a live tree has the same motion problem, and it is solved rather than ignored:**
while a pointer is down, layout is frozen. New nodes queue and a quiet marker says "3 new
waiting". They land on release. The glow keeps pulsing throughout, because it moves nothing.

### 6.3 Flow — the architecture in motion

A **wiring diagram, not a timeline** (owner's answer A). An earlier draft proposed a timeline
because workers are born and die every wave and a fixed diagram seemed unable to represent that.
The owner's specification resolves it by splitting the picture in two:

* **the diagram is fixed** — strategist, worker pool, broker, hypothesis graph, evidence store,
  notebook, findings. It is the architecture, and architecture does not move
* **what moves is the traffic on the wires, and the worker instances inside the worker pool**

So the diagram stays still and is legible, while what it shows is live — which is what n8n
feels like to use.

Each live worker node shows: its model, the tool it is calling right now, and the hypothesis it
is working on. Edges show where output went — and `artifact_stored` makes that literal rather
than decorative.

**The model roster** (owner's 3.3.1), as a panel on this surface: every model in use, with its
name, the role it is filling, its price per million tokens, and what it has spent so far. Price
comes from the OpenRouter `/models` probe already in `agent/llm/openrouter.py`; spend is the §7
accounting the architecture already requires. Neither is new data, it is the first place both
are shown to a person.

**Read-only, with two exceptions**, both about the running system rather than about hypotheses:
stop this worker, and approve this request.

#### 6.3.1 An unplanned benefit worth protecting

Drawing where every output goes means **a node whose output goes nowhere is visible at a
glance.** That is §8.5 of the architecture — a true finding lost on the way to the record —
turned from something you would have to dig out of a log into something an operator sees.

This was not the reason for the requirement. It is a reason not to simplify the wiring away
later if the view gets crowded.

### 6.4 Browser — the agent's own browser

As in the reference image the owner provided: tab strip, address bar, page, and a button to tear
it off. Per §5.7 and §6 of the architecture this is an Electron `BrowserView` with a
`partition:` per engagement, not a page inside the web UI — a page served over HTTP cannot host
a second browser with its own cookie jar.

**The line that must stay sharp, since both happen in the same window:**

| | goes through the broker? |
|---|---|
| **a person types a URL / signs in** — this is "hand the agent this account" | **no.** It is a human action in the human's own hands |
| **the agent navigates, clicks, submits** | **yes, always.** Broker authorisation, scope check and audit entry, all server-side, per §6 |

What the agent reads from a page is evidence and goes to the encrypted store like any other
observation; cookies and tokens it observes likewise (§6.1 of the architecture).

### 6.5 Worker detail — the right rail, and the window it becomes

The right rail is reserved for the live work of the strategist and the workers (owner's B and
3.5). Torn off, it becomes a full window with the detailed work of one worker: its brief, its
tool calls with arguments and results, its reasoning, its evidence, its spend.

**The time scrubber lives here** (owner's answer C): drag it to watch what a worker did earlier;
let go and it returns to now. It is the same projection replayed to an earlier sequence number
(§3), not a second data path.

**Being in the past must be unmistakable** — a distinct frame and a persistent label, not a
subtle one. A screen showing old data that looks live is the same lie as stale data after a
disconnection, which §5.5 of the architecture already forbids.

**One component, mounted three times** (the rule the owner set in answer D, applied again): the
worker-activity view is written once and appears in the rail, in its own window, and in summary
on its flow node. Only the level of detail differs. Three implementations of "what is this
worker doing" would drift, and the one that drifts is always the one nobody is looking at.

---

## 7. Approvals appear in three places, from one source

Owner's answer D, plus the addition that it must also appear in chat.

* **on the flow node** — because this is the only place that shows *what else is blocked* by it,
  which is the thing that decides how urgent it is. In a wave of five workers that is the
  difference between "one worker waits" and "the wave is stalled"
* **in the approvals drawer** — the list, as it exists today
* **in chat** — because chat is the surface the operator is actually looking at

One request, one state, three renderings.

---

## 8. Tier behaviour

A surface with nothing in it is worse than a surface that is not offered. In **low** tier there
is no strategist, no wave, no worker and no graph (§2.6 of the architecture), so:

| surface | low | medium / high |
|---|---|---|
| chat | yes — *one agent*, labelled as such | yes — the strategist |
| work: findings, notebook | yes | yes |
| work: hypothesis tree | **hidden** — no graph exists | yes |
| flow | **hidden** — one agent is not a flow | yes |
| worker rail | **hidden** — no workers | yes |
| browser | yes | yes |

Hidden surfaces **say why** rather than vanishing, so the difference reads as a property of the
tier and not as a broken build. Per §2.6.2 the tier is set per engagement and may be upgraded
mid-engagement; surfaces appear at the upgrade point.

---

## 9. What the server UI keeps

Per §5.4: health only — user count, connected model, total spend, connection and completeness
status, errors. `index.html` is reduced to that and to a debug console, and is otherwise kept as
a reference implementation of behaviour that the client must reproduce (§11).

---

## 10. The reference implementation is a specification, not a safety net

`test_frontend.py` drives a real browser across 25+ tests — notes, graph filters, drawers,
streaming, reasoning traces, tool call grouping, panel resizing, session search — and
`TestFrontendBase.setUpClass` skips the entire suite when `llama-server` is unreachable, so in
API mode it runs none of them (§5.8).

An earlier recommendation in this project was to unblock that suite **before** building these
views. **That was right about the tests mattering and wrong about why, and the correction
matters for sequencing:** those tests exercise a page that is being retired, so repairing them
is not a prerequisite for the new client.

Their value is different and larger: **they are the written-down list of behaviour the new
client must still have.** Every one of them names something the current UI does correctly that
would otherwise be rediscovered by noticing it missing. They are read as an acceptance list and
re-expressed against the new client.

---

## 11. Tests, including two that do not exist anywhere yet

1. **Typing under load.** Characters are typed continuously into the chat input while a flood of
   engagement events arrives. Not one character is lost, and the caret never moves on its own.
   This is agreement 2 expressed as an assertion, and it fails against the reference
   implementation by construction (§2)
2. **Dragging under load.** A node is dragged on the flow canvas while events stream. The
   pointer is never dropped, and the node ends where it was released

   **Both of these are vacuous unless the flood is proved**, and that is the failure mode this
   project has already found six times: a test that passes because the condition it names never
   occurred. Typing into a calm page keeps every character effortlessly. So each of these two
   tests **asserts that the events actually arrived and were rendered during the typing or the
   drag** — a count taken before and after, checked against what was sent — and fails if the
   flood did not happen, separately from failing if a character was lost. Write that assertion
   first and watch it fail, before making the test pass.
3. **Tear-off parity.** The same surface, embedded and in its own window, at the same sequence
   number, shows the same content. This is what keeps §3 true as the code grows
4. **Resume correctness.** A client disconnected at sequence N and reconnected at N+k shows the
   same state as one that never disconnected, and reports k missed events
5. **Tier surfacing.** Each tier offers exactly the surfaces in §8's table — the parity partner
   to the broker/audit/evidence tier test in §2.6.1 of the architecture
6. **The acceptance list from §10**, ported view by view

Tests 1 and 2 are the ones to write first, because they are the agreement the owner set, and an
agreement with no assertion behind it is a preference.

---

## 12. Build order

| # | step | why here |
|---|---|---|
| 0 | **engagement event stream + snapshot + sequence numbers** (§4) | nothing else has data to draw. This is §14.2 M, and it is now on the critical path of five surfaces |
| 1 | client skeleton: Vite, routing, one route per surface, SSE subscriber, snapshot/resume, tear-off | §3's rule is cheap now and expensive to retrofit |
| 2 | tests 1 and 2 (§11), against the skeleton | the agreement, asserted before there is a lot of code to make it true in |
| 3 | chat, against the §10 acceptance list | the primary surface; nothing else is usable without it |
| 4 | work surface: tree + notebook + findings, with the glow and frozen-layout dragging | the operator's existing daily surface |
| 5 | worker rail + tear-off + time scrubber | one component, three mounts — written once, here |
| 6 | flow view with React Flow, model roster, wiring from `artifact_stored` | the largest piece, and it depends on 0 and 5 |
| 7 | browser surface (`BrowserView`, partition per engagement, brokered navigation) | independent of the rest; can move earlier if the target needs it sooner |
| 8 | reduce the server UI to health (§9) | last, so nothing is removed until its replacement works |

---

## 13. Open

* **Keyboard and accessibility** are not specified here. A flow canvas and a tear-off window
  model both have real keyboard-navigation design in them, and neither has been thought about
* **What the rail shows when several workers are active at once** — one at a time with a picker,
  or stacked. This needs a running wave to judge honestly, so it is deliberately left until
  step 5
* **Visual design** — layout, palette, typography, the glow's exact behaviour. This document
  specifies structure and behaviour only
