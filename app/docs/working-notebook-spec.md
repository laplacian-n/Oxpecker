# Working Notebook — design spec

Status: design, ready to build MVP 0. Sibling of the Hypothesis Graph (`agent/hypothesis_graph/`,
`docs/hypothesis-graph-ui-spec.md`) — same architecture (schema → store → service → tools →
digest → CLI → web panel), a different job.

## 1. What it is (and what it is NOT)

A per-engagement lab notebook the agent keeps as it works: cross-cutting observations, reusable
techniques, things to come back to, and dead-ends. It is **raw material for thinking**, injected
into the model's context every turn (bounded) so the agent stops re-deriving what it already
noticed and stops re-exploring paths it already ruled out.

**It is the place for:**
- an insight that spans surfaces — *"the app reflects `X-Forwarded-Host` into password-reset
  links"* — that isn't yet a falsifiable hypothesis
- a **technique** that worked — *"the `constructor.prototype` gadget bypassed the sanitizer
  here"* — worth reusing
- a **todo** — *"revisit the `/api/proxy` param once auth is captured"*
- a **dead-end** — *"tried JWT `alg=none`, `kid` traversal, and weak HS256 secrets — none present.
  Do not retry."*

**It is NOT for** (the boundary the tool descriptions enforce):
| Content | Goes to |
|---|---|
| a falsifiable, testable claim | Hypothesis Graph (`graph_hypothesis_add`) |
| a confirmed vulnerability / report artifact | `record_finding` |
| a raw fact about one endpoint from a scan | RECON's own observation records (`EngagementStore`) |
| immutable proof (a response body, a PoC) | the evidence store |

A note may **reference** any of those by id/ordinal in its `refs` field; it never duplicates them.

## 2. Entry shape

```
note_id           ULID-style, stable
ordinal           N-1, N-2, … — display id, never reused
category          one of §3
note              the text (the substance; keep it tight, a sentence or two)
tags              free-text array, lowercase — the operator's / agent's own facets
surface           optional — the attack surface it's about, e.g. /api/orders/{id}
refs              optional array of free-text ids — "H-3", "finding_ab12", "evidence:sha…"
status            open | resolved   (only load-bearing for todo / dead-end; open for the rest)
resolved_reason   optional — why a todo/dead-end was closed
chat_message_id   the assistant turn that recorded it (jump-to-chat, like graph anchors)
created_at, updated_at
```

Append-only event log backs the materialized table (`note.added`, `note.resolved`,
`note.reopened`), same as the Hypothesis Graph — the notebook is rebuildable by replay and
`check_invariants()` verifies no duplicate ordinals / every note has an `added` event.

## 3. Categories

A small fixed enum, aligned to how a tester actually thinks:

`recon` · `auth` · `access-control` · `injection` · `crypto` · `config` · `business-logic` ·
`infra` · `technique` · `todo` · `dead-end` · `misc`

- `technique` — reusable know-how. (Cross-engagement technique KB is deferred — see §7.)
- `todo` — actionable, has an `open`/`resolved` lifecycle.
- `dead-end` — "ruled out, don't retry". Cheap to always show in the digest; quietly one of the
  highest-value categories because it stops wasted re-exploration.

## 4. Model tools (MVP 0)

Three, in-process (dispatched straight to `NotebookService`, not broker-mediated — a note is not
a target action), added to the prompt only when the structured-memory surface is on (same gate
as the Hypothesis Graph):

- **`note_add(category, note, tags?, surface?, refs?)`** — record a note. Description spells out
  the §1 boundary ("not a hypothesis, not a finding").
- **`note_search(query?, category?)`** — find notes by text / category. The anti-guessing
  retrieval tool — "use this before assuming what you noted earlier".
- **`note_resolve(note_ref, reason)`** — close a `todo` or `dead-end` (or reopen with
  `reason` starting `reopen:`). No-op-with-explanation for other categories.

## 5. Digest — injected every turn

`[NOTEBOOK]` block, bounded, built deterministically from stored fields (never written by the
model after the fact — same anti-forgetting rule as the graph digest). Contents, in order:

1. **Open operator notes** (up to 5) — a `misc` note tagged `operator`, written from the web UI
   (see §6): mid-run human steering, pinned above everything so it never scrolls out behind
   newer model notes. Retired when the operator resolves it in the panel.
2. **All open `dead-end` notes** (compressed one-liners) — always shown, they're the cheapest
   high-value thing.
3. **All open `todo` notes**.
4. The **most recent N** notes of any category (default 12), newest first, minus anything
   already shown.
5. A trailing line: *"If you need a note not shown here, call `note_search` — do not guess."*

Hard cap on total lines (default 30). Over the cap → "… M more, use note_search".

Injected in `_run_safe_default`'s context blocks alongside `_graph_context_block` — every phase,
not just ANALYSIS: a dead-end matters in VALIDATION as much as ANALYSIS.

## 6. Web panel

A **"Notes"** view in the existing Hypothesis Tree overlay (a second tab, or a toggle in its
header) — reuses the read-model + `GET /api/engagements/{id}/notebook` + overlay pattern:

- category filter chips + free-text search (`service.search()`)
- each note: category tag, ordinal (`N-3`), text, tags, `surface`, a "go to chat" button
  (`chat_message_id`, like the graph's anchors), and for todo/dead-end a resolve/reopen control
- open todos and dead-ends pinned to the top (mirrors the digest)
- read-only for claim-like content; the resolve/reopen control is the only write (goes through
  a `POST …/notebook/notes/{ordinal}/resolve`, same shape as the graph's operator-action
  endpoint)

**Operator notes.** The hypothesis-graph drawer's "add a note" (`POST …/hypothesis-graph/nodes/
{ordinal}/operator-action` with `action: "note"`) also mirrors the text into the notebook via
`NotebookService.add_operator_note` — `misc`, tagged `operator`, `refs: ["H-<ordinal>"]`. The
graph's own digest shows hypothesis lines, not the notes attached to them, so without the mirror
an operator's mid-run steering never reaches the model; with it, the note is pinned in the
notebook digest (§5.1) every turn.

## 7. Build order

1. **MVP 0** — schema/store/service/3 tools/digest/CLI, wired into `AgentLoop` behind the
   existing structured-memory gate. **Done** (`a1208c0`). Live-verified: a real turn ran
   `http_recon` then `note_add` ×2.
2. **MVP 1** — the web Notes panel (list + filter + search + jump-to-chat + resolve).
   **Done** (`70afaf5`).
3. **MVP 2** — phase-prompt nudges (`analysis.md` / `validation.md` get a tool-agnostic line
   about `dead-end` / `todo` notes) and `todo` → hypothesis promotion: a 4th tool
   `note_promote(note_ref, …hypothesis fields…)` folds the note text into the hypothesis
   rationale, inherits its `surface`, resolves the note, and adds an `H-<n>` ref back. The
   promotion is agent-only (crafting the falsifiable claim is a judgment call) — the web panel
   stays read-only-plus-resolve. **Done.**
4. **Cross-engagement technique KB** — **Done**. Every `technique` note also overflows into one
   global store (`config.TECHNIQUE_KB_PATH`), dedup on normalized `title|body`. A 5th tool
   `technique_recall(query?, surface?)` now ranks by a hybrid of TF-IDF cosine similarity
   (`kb_vectors.py`) and keyword/tag/surface overlap, so a differently-worded query can still
   surface a relevant technique; the digest names it; a web "★ Techniques (global)" toggle +
   `GET /api/technique-kb` show the library; CLI is `agent.notebook.kb_cli`. The **auto-relevance
   step** ("surface these techniques *because* they match the current target's stack") is
   **done** too, 2026-09-07: `NotebookService.build_context_block()` calls
   `TechniqueKB.semantic_recall()` every turn using the session's own recent notes as an implicit
   query (excluding this engagement's own techniques, so a note doesn't echo itself back), and
   pins up to 2 relevant past-engagement techniques above the library-count line, labelled
   auto-surfaced so the model knows to verify relevance rather than trust it blindly. Deliberately
   local/pure-stdlib (no embedding model call) — this box's one GPU is already the decode
   bottleneck (doc/handoff.md's load-bearing facts), so a second neural embedding model would
   compete for it on every turn; TF-IDF needs no model call at all.

## 8. Risks

The Hypothesis Graph works partly because the pipeline **forces** it — ANALYSIS forms
hypotheses, VALIDATION tests them. The notebook has **no forcing function**; it relies on the
model's discipline, and this box's abliterated Qwen only reaches for a structured tool when the
schema + digest make it obvious. Mitigations: the digest shows *"you have 0 notes — jot down
anything cross-cutting you notice, and any path you rule out"* when empty; phase prompts get a
one-line nudge (MVP 2). Expect to iterate on wording. It is still worth it — a notebook is how
real testers keep from going in circles.
