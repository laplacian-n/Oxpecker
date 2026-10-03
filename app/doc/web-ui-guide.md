# localAI agent — Web UI guide

The web UI is at **http://127.0.0.1:8765** (loopback only — start it with
`python3 -m agent.run_web_ui`). One page, no login. Everything below is a real backend
capability, not a mock.

---

## 1. The three modes

Pick the mode from the **⚡ pill** in the composer (bottom of the home screen).

| Mode | Who drives | Use it when |
|---|---|---|
| **Assistant** (default) | You, every turn, by chatting | Exploratory work, you want to see each step, ad-hoc questions |
| **Autonomous** | The pipeline runs itself INTAKE→RECON→ANALYSIS→VALIDATION→REPORT→CLOSEOUT until done or a real blocker | "Here's a target, go do a pass and report back" |
| **Consult** | Same as Autonomous, but it pauses at each phase boundary and asks *continue / redirect / stop* | Autonomous, but you want a checkpoint before it moves on |

Switching to Autonomous/Consult hides the *Security tools* and *Isolation* pills — those runs
always use the engagement's own RoE and the sandbox.

---

## 2. Assistant mode — the basic loop

1. On the home screen, type what you want in the composer.
   - Enter sends · Shift+Enter = new line.
2. It creates a session and starts working. You'll see:
   - your message (right, tinted bubble)
   - an **AI** avatar + the model's reply (markdown-rendered)
   - **tool chips** for each tool call — click one to see the raw output in the right-hand panel
   - a **"▸ Phase / context injection"** chip = internal context the driver fed the model; click to expand, ignore otherwise
3. Keep the conversation going in the bottom composer. Use **Steer** (top-right) to inject a
   redirect *before the model's next turn* without stopping it.

The sidebar lists past sessions by their first message, grouped by day, with a search box and a
hover-delete. **Ctrl/Cmd-K** = new session.

### Turning on security tools

Assistant mode starts with only `run_command` / `read_file` / `write_file`. Flip the
**🛡 Security tools** pill to **On** to add: `http_recon`, `port_discovery`,
`knowledge_search` / `knowledge_fetch` (self-hosted SearXNG), `osint_record`, `browser_fetch`.

Every one of those still goes through the **broker** — it checks the target against the
engagement's scope/RoE, rate-limits, and can require your approval. Anything the broker flags
(e.g. an injection-scan hit in a tool result) shows a yellow **warning block** and tightens the
session until it clears.

---

## 3. Engagements & RoE (what "in scope" means)

The **📋 engagement pill** picks which Rules-of-Engagement apply. An engagement is
`allow_targets` + `allowed_action_classes` + a validity window + a sign-off.

- **`lab-default`** — the local lab (loopback targets only: Juice Shop, DVWA). Good enough for
  Assistant mode against `127.0.0.1`. Autonomous mode now seeds its host assets from scope
  automatically.
- **+ New engagement** (in the engagement picker) — for anything real, or when you want
  `http_recon` to hit a specific URL. Fields:
  | Field | Example |
  |---|---|
  | Engagement ID | `juiceshop-2026-09` |
  | In-scope targets | `127.0.0.1` (comma-separated host or CIDR) |
  | Allowed action classes | `passive_recon, active_scan_light` |
  | Authorized by | your name |
  | Valid for (hours) | `24` |

  Creating it writes `roe.json` / `scope.txt` / `deny.txt` and registers one **host asset** per
  single-host target — which is what Autonomous mode needs to plan RECON work.

`deny.txt` always wins over `scope.txt`. Out-of-window or out-of-scope = the broker fails closed.

---

## 4. Autonomous / Consult run

1. Engagement pill → pick or create an engagement with real targets.
2. ⚡ pill → **Autonomous** (or **Consult**).
3. (Optional) type a one-line note for the pipeline; the target comes from the engagement scope,
   not the message.
4. Send.

You'll see phases render as collapsible groups. Deterministic tasks (`port_discovery`,
`http_recon`) run for real; judgment tasks (form hypotheses, validate) call the model.
**Stop** (top-right) ends the run. In **Consult** mode a card appears at each phase boundary —
type an optional redirect note and hit **Continue** or **Stop**.

A run **blocks** (not errors) on a genuine hard stop: kill switch, expired RoE window, a
judgment task making no progress twice, or the run's wall-clock budget.

---

## 5. Hypothesis Tree

Top-right **"Hypothesis Tree"** button (needs an open session). This is where you watch the
agent's reasoning without reading the transcript.

- **Canvas** — one node per hypothesis, laid out left→right by causal depth. Circle
  style = lifecycle (open / running / parked / completed); a glyph = verdict (✓ supported,
  ✓✓ confirmed, × refuted, ? inconclusive). The heavy green line = the **active path** (what the
  agent is pursuing right now, with its stated reason under the header).
- **Views**: Causal (default) · Timeline (X = creation order) · Focus (active path + neighbours only).
- **Filters**: search, phase, status/verdict, confidence, active-only. `refuted`/`parked`/
  `abandoned` branches auto-collapse — click the `▸ N` on a node to expand.
- **Drawer** (click a node) — the falsifiable claim, why it exists, confidence / coverage /
  priority kept separate (with an "AI estimate" and an Explain breakdown), tokens, attempts,
  **chat anchors** (jump the transcript to where this was created / tested), and operator notes.
- **"Next up"** strip (bottom right) — open hypotheses ranked by priority; this is the direct
  answer to "what should it do next" and matches what the model itself was shown.
- **Stats** button — hypotheses by lifecycle/verdict, tokens by phase.

### Steering from the drawer

The drawer's **Steer** section:
| Button | What it does |
|---|---|
| Focus this branch | view-only — fades everything outside this lineage |
| **Park this branch** | marks it parked (asks for a reason) — the model sees it shelved next turn and stops spending effort on it |
| **Reopen** | un-parks it |
| Add a note | attaches an operator comment/constraint (shown in the drawer) |
| Ask AI to re-evaluate | sends a chat message asking the model to re-check this hypothesis |
| Suggest a related hypothesis | sends a chat message |

The UI never edits the claim / evidence / verdict history directly — those are append-only and
the model owns them.

---

## 6. Approvals

**Approvals** in the sidebar (badge shows the count). When a security tool or a `run_command`
needs your sign-off, it queues here — **Approve** / **Decline**. Autonomous runs deny by
default, so an autonomous run that hits an approval-gated action will block rather than wait.

---

## 7. A concrete first run (Juice Shop)

Assistant mode:

1. 🛡 Security tools → **On**
2. 📋 engagement → **lab-default** (loopback is in scope)
3. Type: *"Run http_recon on http://127.0.0.1:3000/ and tell me which security headers are missing. Then use port_discovery on 127.0.0.1."*
4. Watch the tool chips; click one to see the raw response.
5. Follow up: *"Form a hypothesis about the missing headers and one about /api/orders/{id} — record them."* (the graph tools are on by default)
6. Open **Hypothesis Tree** to see the two nodes.

Autonomous mode:

1. 📋 → **+ New engagement**: ID `juiceshop-lab`, targets `127.0.0.1`, classes
   `passive_recon, active_scan_light`, your name, 24h.
2. ⚡ → **Autonomous**
3. Send (message optional). It'll run RECON for real, then ANALYSIS/VALIDATION with the model.
4. Open the Hypothesis Tree while it runs to watch hypotheses appear and get tested.

> Note: this box decodes at ~3–4 tokens/sec, so model turns take real minutes. Deterministic
> RECON tasks are fast; ANALYSIS onward is slow. That's the hardware, not a hang.
