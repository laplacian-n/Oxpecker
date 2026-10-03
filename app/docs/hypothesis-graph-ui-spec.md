# Hypothesis Graph — UI Design Spec

Status: ready to build against a real backend (MVP 0 implemented — `agent/hypothesis_graph/`).
Synthesizes two design-review docs (2026-08-31 and 2026-09-01); where they disagreed, this spec
follows the 2026-09-01 doc, which corrects the earlier one on several load-bearing points (DAG
not tree, confidence/coverage/priority kept separate, no sunk-cost priority formula, `parked` as
the reversible default). This is a **design spec for Claude Design / a frontend implementer** —
not application code.

Menu label: **Hypothesis Tree** (easy to grasp). Internal name: **Hypothesis Graph**.

## 1. What this screen is for

The operator watches an AI security-testing agent (see `agent/prompts/core_identity.md`) form
hypotheses, test them, and decide what to do next — without reading the raw chat transcript. The
screen must answer three questions at a glance: **what is the AI investigating, why, and what has
it found** — and let the operator redirect it without editing history.

This is a real backend already, not a mockup target: `agent/hypothesis_graph/service.py` is the
data source, `agent/hypothesis_graph/schema.py` defines every enum below, and the model already
calls the 9 `graph_*` tools live (see `agent/hypothesis_graph/tools.py`) when a session has
`use_hypothesis_graph=True`.

## 2. Information architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Hypothesis Tree   Engagement: LAB-024   Active: H-12   Tokens: 48.2k         │
│ [Focus active path] [Phase ▾] [Status ▾] [Search] [Fit graph] [Timeline]     │
├───────────────────────────────────────────────────────┬─────────────────────┤
│                                                         │ Selected: H-12      │
│  INTAKE      RECON               ANALYSIS              │ Status / verdict    │
│    ○ 1  ───▶  ● 2 ─────┬──────▶  ◌ 4                   │ Claim               │
│   target      HTTP      │         active                │ Why it exists       │
│                         ├──────▶  ● 5 ───▶ ○ 8           │ Confidence/Coverage │
│    ○ 3 ────────────────┘         API auth                │ Attempts + evidence │
│   user clue            synthesis                         │ Tokens              │
│                                                         │ Chat anchors        │
│                                                         │ Derived hypotheses  │
├───────────────────────────────────────────────────────┴─────────────────────┤
│ Legend: ○ open  ◌ running  ● completed   ✓ supported  × refuted  ? unknown  │
└─────────────────────────────────────────────────────────────────────────────┘
```

- **Header**: engagement name, active hypothesis (`H-<ordinal>`), engagement token total, phase/
  filter/search controls. Keep it to decision-relevant fields — this is not a metrics dashboard.
- **Graph canvas** (left, ~65% width desktop): pan/zoom, click to select, causal generation on the
  X axis (see §4).
- **Detail drawer** (right, ~35% width desktop; bottom sheet on mobile): everything about the
  selected hypothesis. Never a floating box that covers the graph.

## 3. Node visual spec

```
          ANALYSIS             ← phase_created, small label above
             │
          ╭─────╮
          │  12 │ ×3           ← ordinal (large) + attempt count badge (small, if >1)
          ╰─────╯
       Auth bypass clue        ← title, truncated with ellipsis, ≤60 chars
       Med conf · 67% cov      ← confidence_band + coverage %, shown when zoom/space permits
```

- Circle **fill/border** encodes `lifecycle_status` (never fill alone — pair with icon + text so
  color-blind and reduced-contrast users aren't relying on hue):

  | lifecycle_status | Visual |
  |---|---|
  | `draft` | very light/dotted outline |
  | `open` | outline only, solid stroke |
  | `queued` | outline + small queue-tick marker |
  | `running` | dashed ring, `prefers-reduced-motion` respecting pulse (see §8) + "Active" text label near node |
  | `blocked` | outline/fill per prior history + `!` marker |
  | `awaiting_approval` | double-ring outline + amber marker |
  | `completed` | filled, **plus a verdict glyph is mandatory** (filled alone must never read as "succeeded") |
  | `parked` | reduced-opacity fill/outline + pause (⏸) marker — reversible, so keep it legible, not hidden |
  | `abandoned` | struck-through outline + text label — never removed from the canvas |

- **Verdict glyph** (only meaningful once `lifecycle_status=completed`):

  | verdict | glyph |
  |---|---|
  | `confirmed` | `✓✓` |
  | `supported` | `✓` |
  | `refuted` | `×` |
  | `inconclusive` | `?` |
  | `superseded` | `⇒` + link to the replacing node |
  | `unassessed` | (no glyph) |

- **Ordinal**: shown as `12` on the node, referenced in text as `H-12`. Backed by a stable
  `hypothesis_id` (ULID-style) — ordinals are display-only and never reused after abandon/
  supersede (verified: `test_ordinal_not_reused_after_abandon`).
- **Attempt badge**: `×N` where `N = len(experiments)` for that hypothesis, only shown when N>1.
  Clicking it (or the drawer's Attempts section) expands per-attempt rows — do not create a
  separate node per attempt.

## 4. Layout

- **X axis = causal generation/depth**, root at depth 0, each `spawned_by`/`refines` step +1.
  **Not wall-clock time** — investigations have wildly uneven per-node duration (a scan is
  seconds, an approval wait can be an hour), and a time-proportional X axis makes the graph jump
  around and mis-communicates "this took the same effort as that."
- **Y axis** = branch layout; nodes at the same generation stack vertically, ordered by creation.
- A **Timeline view** (toggle in the header, §7) swaps X to `created_at`/attempt time for anyone
  who does want the literal chronology (e.g. explaining the engagement to a stakeholder) — kept
  as a secondary view, not the default.
- **Primary lineage only** draws the main tree-like skeleton (`primary_parent_id`). Non-primary
  edges (`supports`, `contradicts`, `synthesized_from`, `references`, `supersedes`) render as
  lighter cross-links **shown on selection**, not by default — a DAG with every edge always drawn
  is unreadable past ~15 nodes.
- **Active path** (`graph_state.active_path`, set via `graph_set_active_path`) is the one path
  highlighted with a heavier stroke end-to-end, root → active node. It is a **state the agent
  sets with a reason**, shown as one line under the header: *"Pursuing H-12 because — reproduced
  cross-user read on /api/orders."* Do not compute it client-side from scores; read
  `graph_state.active_hypothesis_id` / `.active_path_reason` directly.

## 5. Detail drawer

Sections, top to bottom:

1. **Header**: `H-12` + title, `phase_created`, `lifecycle_status` + verdict, `created_at`/
   `updated_at`, `origin_type` (+ link to the triggering chat message if `origin_ref` is a
   message id).
2. **Claim**: the falsifiable `claim` string verbatim. Not the title — claim is the full
   sentence, e.g. *"Endpoint `/api/orders/{id}` may not enforce object-level authorization
   between user roles."*
3. **Why it exists**: `rationale`, `primary_parent_id` (as a "derived from H-N" link), and any
   `source_hypothesis_ids` via `synthesized_from` edges if this node is a synthesis.
4. **Metrics** (three separate rows — never merge into one score, see §6):
   - Confidence: `confidence_band` (Low/Medium/High) + `confidence_reason` text.
   - Coverage: `coverage` as a progress bar, with the fraction (`completed / planned_tests`)
     spelled out — e.g. "1 of 2 planned tests".
   - Priority: tier (`Now`/`Next`/`Later`/`Parked`, from `PriorityTier`) + an **"Explain"**
     affordance that expands `priority_components` (from `engine.priority_components()`) as a
     short bullet list — impact, information gain, phase relevance, remaining cost. Never show a
     bare decimal as the primary label.
5. **Tokens**: `direct_tokens` (own calls) as the primary number; a secondary "primary-branch
   total" if the frontend chooses to sum through `primary_parent_id` client-side — the backend
   does not currently materialize that sum, so either compute it client-side from the fetched
   subtree or omit it in v1 rather than mislabel `direct_tokens` as a branch total.
6. **Attempts** (`experiments` for this hypothesis, ordered by `attempt_no`): each row shows
   `method_summary`, `observed_result`, status, token cost, and the linked observation's
   `polarity`/`strength`. This section documents **what was already done** — the phase prompts
   (`agent/prompts/phases/validation.md`) deliberately don't ask the model to state "what's
   next" in an attempt record; that belongs to derived hypotheses / the active path, not history.
7. **Evidence**: `evidence_refs` from experiments/observations, each opening the existing evidence
   viewer (evidence-store refs, not raw output duplicated into the graph).
8. **Chat anchors**: buttons — *Go to creation message*, *Go to attempt start*, *Go to result* —
   each jumping the chat pane to a stable `message_id` and highlighting it briefly (§7.3). If the
   message has aged out of the model's working context, still open it from the raw transcript
   store (`SessionStore`) — the chat pane must never show "message not found" for something that
   really happened.
9. **Derived hypotheses**: list of direct children (`primary_parent_id` pointing here) plus any
   `synthesized_from`/`spawned_by` targets, each with the one-line reason from the edge's
   `reason` field, and a "Focus" button.

## 6. Confidence / coverage / priority — must stay visually distinct

This is the single most important rule carried from both review docs into this spec: **do not
merge these into one score**, and do not let child progress silently inflate a parent's
confidence.

- Coverage rising (more attempts done) is progress, not truth — render it as a bar, plain.
- Confidence only moves when the **service** records a change from evidence (`set_confidence`,
  or the advisory suggestion `engine.suggest_confidence_band()` surfaced next to the model's own
  assertion — labelled "AI estimate" if shown before the model has explicitly confirmed it).
- Priority is a ranking heuristic (`priority_score = ...`, see `engine.priority_components()`),
  never presented as a probability. Show the **tier** as the primary UI element; the numeric
  score is available only behind "Explain".

## 7. Interactions

### 7.1 Focus active path
Highlights the active path (root → active node) and running/queued immediate children; fades
everything else to ~30% opacity. Toggle in the header.

### 7.2 Compare
Multi-select 2–3 nodes (shift-click) → a comparison panel: claim, confidence, coverage, direct
tokens, blockers, side by side. Useful before choosing which parked branch to reopen.

### 7.3 Chat synchronization (bidirectional)
- Node → chat: click an anchor button in the drawer → chat scrolls to that `message_id`,
  highlights it for ~2s, shows a small "Back to H-12" pill to jump back to the graph selection.
- Chat → node (nice-to-have, v2 per both docs' roadmap): clicking a chat message that created or
  updated a hypothesis highlights that node in the graph.

### 7.4 Manual steering
The operator can: focus/deprioritize/park a branch, add a comment/constraint, ask the AI (via
chat, not a direct graph edit) to create a hypothesis from new information, or ask for a
confidence re-evaluation. **The UI must never let a user overwrite `claim`, `evidence`, or
`verdict` history directly** — those are append-only through the service. A "Suggest hypothesis"
action sends a chat message; it does not call `graph_hypothesis_add` from the client.

### 7.5 Stale-hypothesis warning
Flag (small clock icon + tooltip) a node that has been `running`/`queued` unusually long, or
whose parent's evidence changed after it was created, or whose dependency was refuted. Advisory
only — does not change `lifecycle_status`.

### 7.6 Negative-path preservation
`refuted`/`parked`/`abandoned` branches collapse by default (to keep the canvas legible) but are
never deleted and are one click away via "expand" or the status filter. This is what makes "we
tested X and it wasn't there" reportable later.

## 8. Accessibility & motion

- Every node is a keyboard-focusable control with an accessible name, e.g.
  `"H-12, Analysis, running, medium confidence"`.
- Selection / drawer updates use `aria-live="polite"`.
- `running`'s dashed-ring animation must respect `prefers-reduced-motion`: when reduced motion is
  requested, replace the animated ring with a static dashed ring + an "Active" text label —
  never rely on motion alone to convey "in progress" even when motion is allowed (pair with the
  text label regardless).
- Never use color as the only differentiator for status or verdict — icon/line-style/text always
  accompany it (already reflected in §3/§5 tables).
- Touch targets ~44px even though node circles are visually smaller — enlarge the hit area, not
  the circle.
- Mobile: graph canvas + a simplified list/focus view (open hypotheses by priority, matching the
  side panel in §9), detail drawer becomes a bottom sheet.

## 9. Filters, views, and the decision-support panel

Minimum filters: phase, `lifecycle_status`/verdict, active-only, `origin_type`, confidence band,
and free-text search (matches ordinal, title, claim, `surface` — backed directly by
`service.graph_search()`).

Views (don't add more than this before real usage data exists):
1. **Causal** (default) — X = generation, as in §4.
2. **Timeline** — X = `created_at`.
3. **Focus** — active path + its immediate neighborhood only.

A compact **"Open hypotheses" panel** (can live in the same right-hand column below the drawer,
or as a collapsible strip) lists actionable hypotheses ranked by `priority_tier`/score — this is
the direct answer to "what should the AI do next," sourced from `engine.rank_open()` /
`service.build_context_block()`'s own top-N, so the UI's ranking always matches what the model
itself was shown that turn.

## 10. Performance / scale

- Smooth up to 50–100 visible nodes; beyond that, auto-collapse `completed`/`refuted`/`abandoned`
  branches (still reachable, never dropped from the data).
- Layout updates incrementally — an existing node must not visually jump position when a sibling
  is added elsewhere in the graph.
- Cross-links (non-primary edges) render only on selection or past a zoom threshold.
- Detail data (attempts, evidence, full claim/rationale text) loads when the drawer opens, not
  bundled into the initial graph payload — the graph payload itself should be closer to
  `service.graph_search()`'s digest shape than to the full hypothesis rows.
- Prefer incremental updates over the graph's `graph_version` (bumped on every mutation,
  `store.graph_version()`) — poll or subscribe, then re-fetch only if the version the client has
  is stale, rather than re-fetching the whole graph on a fixed timer.

## 11. What NOT to build in v1

(Carried directly from both docs' "don't do this" sections — still correct.)

- No 3D graph, no free-form force-directed layout.
- No literal-time-only X axis as the default.
- No "filled = succeeded" — a verdict glyph is mandatory alongside fill.
- No raw percentage confidence without calibration data behind it — bands only.
- No single fused confidence/coverage/priority score.
- No node per tool call/attempt — attempts live inside the hypothesis's Attempts list.
- No deleting failed/refuted/abandoned branches.
- No client-side editing of evidence/verdict history.
- No "Hot Surfaces" cross-hypothesis panel yet — real, but deferred to v1.5 per the roadmap
  (needs `synthesized_from` edges to have real usage data first).

## 12. Build order (matches the backend's own MVP staging)

1. **MVP 1 — read-only causal graph**: nodes + primary edges, status/verdict visuals, detail
   drawer (sections 1–3, 6, 8 from §5), chat anchors for creation/result, `direct_tokens`, active-
   path highlight. This alone is already useful and is a thin read layer over
   `HypothesisGraphService` — no new backend work needed.
2. **MVP 2 — attempts + DAG**: multiple attempts per hypothesis, non-primary edges rendered on
   selection, confidence/coverage separation, focus/collapse/search/filter.
3. **MVP 3 — prioritization UX**: the "Open hypotheses" panel, Explain-priority, compare, stale
   warnings, manual steering actions.
4. **MVP 4 — scale/analytics**: Timeline view, clustering/virtualization for 100+ node graphs,
   token/outcome analytics.

Do not start MVP 1 before the backend's own invariant tests are green (`python3 -m
agent.hypothesis_graph.test_hypothesis_graph` — already true as of this spec) — building visuals
on top of shaky semantics was the exact failure mode both review docs warned against.
