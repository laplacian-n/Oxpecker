# Wave engine — implementation decisions

The design is `AGENT_ARCHITECTURE.md` §2 (roles), §2.5 (the wave model), §2.6 (tiers) and §14.1
(the load-bearing holes). This file records the owner's decisions on *how* to build it, so the
next session does not re-litigate them. Each cites the section it serves.

## The one theme, stated first because it recurs in every part

**Anywhere there is shared state and concurrency, the write interface is atomic from the start —
before the wave exists to exercise it.** We have paid the check-then-act race three times in one
week (the broker cooldown, `verify_once`, the spend budget). Writing `record-after` now and
"making it atomic later" is inviting the race back. Every shared-state write a worker makes —
the hypothesis graph, the event log, the spend ledger, the experiment claim — is assigned by the
write (a UNIQUE constraint, `MAX()+1` inside one statement, or `BEGIN IMMEDIATE`), and is proven
by a test that fires many threads at cold state and **counts the winners**, never one that only
checks a value.

## Tiers (§2.6)

- **medium = the existing `AutonomousDriver`** (phase/task). Not rebuilt.
- **high = the wave model**, a new orchestration layer on top — parallel waves, four roles.
- **low** stays one agent driven by a person.

## Roles and dispatch (§2, §2.5, §2.7)

- A **worker** is an `AgentLoop` with a narrow brief: one `(hypothesis, method)` experiment.
- Workers run as **threads** in the one web process. This is allowed *only* because every write
  they share is atomic (the theme above); it is not "it handles concurrency" until the counting
  test exists.
- The **strategist** is a model that reads the graph digest and dispatches; the **wave barrier**
  and **claiming** are deterministic, not the model's job (§2.7: the strategist is not a state
  machine).
- Claiming is on the **experiment `(hypothesis, method)`**, not the hypothesis (§14.1 E′): one
  wave dispatches several workers against one hypothesis with different methods. Implemented as
  `HypothesisGraphStore.claim_experiment()`, atomic via a partial unique index — **done**.
- A **wave** dispatches up to **5 workers**, each with a **10-minute wall-clock limit**. The limit
  is a **hard kill**, and a killed worker **records its partial result and the kill reason to its
  trajectory** — it does not vanish, or §8 (the trajectory spine) is lost.

## Verifier (§2.3)

- A different model **instance**, and — because §2.3's job is to *try to prove the finding wrong*,
  which a same-model-different-seed instance answers too similarly — ideally a **different
  provider / model family** for real independence. Configurable in `roe.json`; seed-only is the
  fallback, not the goal.

## Chat / reasoning on the stream (§4.2, §8.4)

- Chat content reaches the engagement-scoped client by **new event kinds on the one engagement
  stream** (keeps §3's "one data path, no window owns state"), not a second per-session channel.
- Token/reasoning **deltas are ephemeral** (UI streaming only, not persisted). But the
  **turn-level message that is persisted carries the turn's full composed reasoning** — §8.4: a
  good trajectory is "good reasoning that failed", so dropping reasoning and keeping only the
  action makes it untrainable.

## Spend (§14.1 D)

- The interface is **`reserve`/`settle` from the start**. In single-worker mode, `reserve` settles
  immediately; the wave only splits the two moments. Writing `record-after` now and retrofitting
  reserve later is the §14.1 D race re-invited. The ledger (`engagement/spend.py`) already offers
  both.
- Cost comes from the provider via a `cost_per_mtok()` seam (local llama = 0/None, OpenRouter =
  real). Default **unmetered**; a cap is set per engagement in `roe.json`.

## CI (process)

- Run the suite on a **3.11 + 3.12 matrix**. 3.12 does not "fix the race class" — a check-then-act
  race on shared state is a logic bug that no Python version removes; the matrix catches both
  legacy-API regressions (3.11) and version-specific breakage (3.12), which is the coverage the
  Ubuntu host has been giving us for free.
- `gh pr merge` / auto-merge may be enabled for a session **only** coupled with a required status
  check (`unittest`) on `main` branch protection. Auto-merge with no required check merges before
  CI runs, which is the back door we do not build.
