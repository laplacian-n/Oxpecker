# Observability & Runtime Safety Parity Plan

Logging and audit for the agent, and the runtime gap that investigating it uncovered.
Companion to [TOOLING_ROADMAP.md](TOOLING_ROADMAP.md); the tool adapters planned there must
land on the audited path described here, not beside it.

> **Status: all seven steps are implemented.** This document is kept as the record of what was
> wrong and why each fix took the shape it did — the findings in §1 are history, not current
> behaviour. The [README's Implementation Status table](../README.md#implementation-status) is
> the authoritative statement of what exists; §8 lists what is still outstanding.

---

## 1. The finding that reframed this work — FIXED, recorded as history

Everything in this section describes the state **before** the work in §4 landed. It is kept
because the shape of the fixes only makes sense against it, and because a project whose headline
claim is its safety architecture should keep the record of having got that wrong once.


The question was "add detailed debug logging". Inspection showed good logging already exists —
in the runtime that is not the one being shipped.

### Two runtimes, different controls

| Control | `agent/` (CLI) | `app/agent/web/dev_server.py` (shipped as the installer) |
|---------|----------------|----------------------------------------------------------|
| Scope / RoE on network targets | broker `scope_check` | **nominal only** — present inline in `http_request`, but fail-open, self-widening and substring-bypassable; see §1.1 |
| Destructive-command denylist | — | **present** — `_DESTRUCTIVE_PATTERNS`, line 787 |
| Sandboxed execution | `get_executor(tier)` → bubblewrap + seccomp + rlimits | **absent** — `subprocess.run(cmd, shell=True)`, line 883 |
| Audit log + hash chain | `AuditLog`, `prev_hash` | **absent** — `grep -c audit web/dev_server.py` → 0 |
| Evidence store | `EvidenceStore`, encrypted | **absent** |
| Broker (taint, rate limit, approval queue) | `Broker.dispatch` | **absent** — no safety module is imported at all |

To state it fairly, the destructive-command denylist is real and does what it claims. But local
command execution is unsandboxed, nothing is audited, and the scope check — which this document
initially credited as genuinely enforcing — does not hold up on inspection. See §1.1.

### 1.1 The scope check does not hold

`http_request` is the only place any session state influences tool execution (line 947). Its
check, at lines 948–952, has three compounding defects.

**It fails open on an empty allowlist.**

```python
if eng and eng.allow_targets and host:
```

When `allow_targets` is empty the condition short-circuits and the entire check is skipped, so
every host is permitted. The README states that "the default engagement ships empty /
localhost-only" as a safety property. An empty list does not mean "nothing is allowed" here; it
means **no restriction at all** — the opposite of the intended reading.

**It widens itself from chat text.** `_scope_urls_into_engagement` (lines 1702–1712) is called
on every guided turn (1721) and on the message endpoint (1906):

```python
for u in re.findall(r'https?://[^\s<>"\']+', message or ""):
    host = urlparse(u).hostname or ""
    if host and host not in eng.allow_targets:
        eng.allow_targets.append(host)
```

Any URL appearing in an operator message is appended to the engagement scope automatically. The
allowlist is therefore not something an operator deliberately curates; naming a URL in
conversation grants it. A URL pasted as context — from a report, a ticket, a log excerpt — is
indistinguishable from an authorisation.

**It matches by substring, in the permissive direction.**

```python
allowed = any(host == str(t).lower() or host in str(t).lower() for t in eng.allow_targets)
```

`host in t` permits a host whenever its name appears anywhere inside any allow entry. With
`notevil.example.com` in scope, the host `evil.example.com` is allowed, because
`"evil.example.com"` is a substring of `"notevil.example.com"`. Parent domains are likewise
admitted by narrower entries.

Taken together: of the four safety controls the project advertises as its differentiator —
scope/RoE enforcement, sandboxed execution, audit logging, broker mediation — **none holds in
the runtime that ships as the installer.** The destructive-command denylist, which is not among
the four, is the control that does work.

### 1.2 Other inert safety-shaped fields

`isolation_tier` is not the only one. `profile` (line 542) and
`CreateSessionRequest.dangerous_local` (line 1737) are also accepted from the API and never read
for any decision; `AutonomousStartRequest.profile` (1775) is never even stored. Each presents as
a safety or mode control and governs nothing.

### 1.3 Filesystem confinement is prefix-based

`read_file` and `write_file` confine to `DATA_DIR` with
`str(fp).startswith(str(DATA_DIR))` (lines 901, 917) rather than `Path.is_relative_to`. Both
resolve first, so ordinary `../` traversal is caught, but a sibling directory sharing the prefix
(`dev_data_evil`) passes. A narrow issue, and cheap to fix correctly.

### 1.4 The executable surface is wider than the advertised one

`_TOOL_NAME_ALIASES` (lines 838–849) maps 27 aliases onto 4 canonical tools, including `bash`,
`sh`, `shell`, `exec`, `execute`, `command` and `curl`. The aliases are accepted at execution
time but never advertised in `TOOL_SCHEMAS`, so the reachable surface is 36 spellings over 9
tools while the model is shown 5 (or 9 with `use_security_tools`). Any allowlisting work has to
operate on canonical names after alias resolution, not on what the schema advertises.

### 1.5 One thing in our favour

`_run_tool` has exactly **one** call site — line 1504, inside `_run_agent_turn`'s tool loop.
There is no second dispatch path; the guided and autonomous entry points both funnel through it.
A single chokepoint makes the audit and mediation work far smaller than a 2398-line file would
suggest.

### `isolation_tier` is a dead field

`Session.isolation_tier` defaults to `"bubblewrap"` and appears at dev_server.py lines 541, 720,
766, 1738 and 1826. Every one of those is storage, serialization or request parsing. **No line
reads it at execution time.** An operator inspecting session state or the API sees
`"isolation_tier": "bubblewrap"` while `_run_tool` runs `shell=True` on the host.

A field that reports a safety property the system does not have is worse than no field.

### The system prompt tells the model not to refuse targets

dev_server.py lines 103–105:

```
TARGET LOCK: the operator's target and the "In-scope targets" list below are authoritative and
ARE in scope. NEVER switch to localhost / 127.0.0.1, and NEVER claim the given target is out of
scope.
```

The motivation is understandable — a small model that refuses constantly is unusable. But
combined with no broker mediation, this is the opposite of the documented posture that "the
agent hard-enforces an allowlisted scope and refuses out-of-scope targets". Scope here is
instructed, not enforced; `http_request`'s inline check is what actually holds the line.

### Consequence for the roadmap

Every adapter planned in TOOLING_ROADMAP.md (`http_request`, nuclei, ffuf, privesc) would land
in `dev_server.py`. Adding them before closing this gap means each new capability arrives
unsandboxed and unaudited, and the refactor cost grows with every one. Hence the ordering in §4.

---

## 2. Correcting my own earlier error

The Implementation Status table in the README listed sandboxed execution as "Implemented",
pointing at `app/agent/sandbox/`. The module is implemented and has a committed test suite —
but a reader concludes the desktop app sandboxes, which it does not. "A module exists" and "the
runtime uses it" are different claims and the table must distinguish them. Same for audit
logging. Corrected in the same change that adds this document.

---

## 3. What already exists, and should not be rebuilt

`audit_log.py` records per tool call:

```
entry_id, timestamp, session_id, turn_index, client_machine,
tool_name, action_rationale, arguments (redacted via _redact),
content_digest, raw_output_excerpt, sanitized_output,
scope_decision, approval_identity,
prompt_tokens, completion_tokens, latency_ms,
exit_code, injection_flagged, prompt_version, prev_hash
```

Plus: `reasoning_content` is captured (found live that llama-server returns thinking output in
its own response field rather than as `<think>` tags in `content`, so reading only `content` was
silently discarding every reasoning token). Full raw output lives encrypted in the evidence
store, referenced by `content_digest`, so the 2000-character cap in the audit entry is a display
limit, not data loss.

"What it thought" and "what it called" are therefore already covered on the CLI path.

### Audit log and debug log are not the same artifact

Their requirements conflict, and merging them damages both.

| | Audit log | Debug log |
|---|---|---|
| Purpose | accountability | diagnosis |
| Integrity | hash-chained, append-only | not required |
| Secrets | **redacted** (`_redact()`) | **must show raw** |
| Size | capped excerpts | unbounded |
| Retention | per policy | discardable |

Putting full prompts and raw model output into `AuditLog` would bloat the hash chain and place
secrets exactly where redaction was designed to keep them out. Two sinks, correlated by
`(session_id, turn_index, entry_id)`.

---

## 4. Plan

### Step 1 — Sandbox availability probe (`sandbox/`)

`BubblewrapExecutor` builds `bwrap_argv = ["bwrap"]` with no availability check; a missing
binary surfaces as `FileNotFoundError` at execution time. `get_executor(tier)` only validates
that the tier name is known, not that the tier can actually run.

Add to the sandbox layer (both runtimes need it, so it does not belong in dev_server):

- `BubblewrapExecutor.available() -> (bool, reason)` — probes for the `bwrap` binary and a
  supporting platform
- `resolve_tier(requested) -> (effective_tier, reason)` — the single place that decides what
  will actually run

### Step 1b — Fix the scope check (§1.1)

Independent of the sandbox work and smaller: make the empty allowlist deny rather than permit,
require explicit operator action to add a target instead of harvesting URLs from message text,
and replace substring matching with exact host comparison plus explicit subdomain rules. Until
`http_request` moves onto the broker (Step 7), this is the only thing standing between the agent
and an arbitrary host.

### Step 2 — Route `run_command` through the sandbox, fail closed

Correction to an earlier assumption: `run_command` is **not** in `broker.TOOL_ACTION_CLASS`, so
`Broker.dispatch` would deny it as an unknown tool. That is deliberate — the broker gates
*network* actions against the RoE (`http_recon`, `port_discovery`, `knowledge_*`,
`browser_fetch`), while *local execution* is gated by the sandbox via
`tools/run_command.py::run()`, which already does preflight denylist → confirmation check →
`get_executor(tier)`. The CLI's division of responsibility is correct and should be reused, not
replaced.

So dev_server's `run_command` branch calls `tools.run_command.run(...)` with the resolved tier
instead of `subprocess.run`.

**Fail closed.** If the requested tier is unavailable and the operator has not explicitly opted
into `direct`, deny the command and say why. This mirrors the existing CLI posture, where
`--isolation-tier direct` "requires explicit confirmation, same as `--dangerous-local`".

### Step 3 — Make `isolation_tier` honest

Keep the requested tier, add the resolved one. The resolved value is what the API reports, what
the UI displays, and what the audit entry records. The system must never report `bubblewrap`
for an execution that ran direct.

### Step 4 — Audit in dev_server

Write an `AuditLog` entry per tool call, including the effective isolation tier. Reuse the
existing hash-chained format; do not invent a second one.

`Broker.__init__`'s `confirm_fn` defaults to a blocking `input()` call, which would hang a
FastAPI worker. When the broker is introduced here, construct it with `use_approval_queue=True`
— a mode the design already anticipated "for a future UI/multi-operator workflow" — so
approvals resolve out of band through the UI.

### Step 5 — Debug sink

Separate from audit: the prompt as actually assembled, raw model output, RAG chunk IDs with
scores, and compaction deltas.

**Compaction is the worst current blind spot.** `_maybe_compact` (line 1138, folding once 24
messages accumulate past the last summary) logs only `log.info("compacted %d msgs ...")`. It
records the count, not which messages were folded or what the summary said — so "why did the
agent forget what it found at step 5" is unanswerable, and that is the failure mode long agent
runs hit most.

### Step 6 — Trace reader

Data spread across the audit log, evidence store, hypothesis graph and notebook, joined by hand,
does not get used. A reader that prints one correlated timeline per turn is what makes steps 1–5
worth having:

```
turn 7
  ├ prompt    v2.1 + 3 RAG chunks (0.81, 0.77, 0.74) + [compacted: 24 msgs -> summary#2]
  ├ reasoning "ports 3389/3390 suggest RDP; try default creds before brute force"
  ├ decision  action_rationale="..."  ->  http_request(POST /rest/user/login)
  ├ broker    allowed  (scope: target in RoE)  tier=bubblewrap
  ├ result    302, Set-Cookie  [evidence:a3f9...]  412ms  1840/95 tok
  └ state     hypothesis h3 -> supported (+1 observation)
```

### Step 7 — Network tools onto the broker

Fold dev_server's `http_request` into broker mediation as part of the roadmap's `http_request`
and session-store work, rather than as a separate change. Given §1.1, this buys scope
enforcement that actually holds, as well as taint tracking, rate limiting, the approval queue
and evidence.

Note also that dev_server's `http_request` uses `urllib.request.urlopen` (line 955), so it has
none of the IP-pinning or DNS-rebinding protection that `security_tools/http_recon.py` was
deliberately built around. The replacement must keep that property.

---

## 5. Windows

The sandbox is Linux-only: bubblewrap plus a seccomp profile, with no Windows branch anywhere in
`sandbox/executor.py` or `tools/run_command.py`. The application ships as a Windows installer
(`electron-builder --win`, `Oxpecker-Setup-${version}.exe`), and dev_server has a
`_powershell_body()` path, so Windows is a first-class target in practice.

Therefore **bubblewrap cannot be the answer on the platform the installer targets.** Options,
none of them free:

| Option | Assessment |
|--------|------------|
| Fail closed; require explicit `direct` opt-in with a visible warning | What Step 2 does. Honest, cheap, costs Windows users the capability unless they accept the risk |
| Run commands inside WSL2 | Attractive — the pentest tooling (nmap, sqlmap, nuclei) is Linux-native anyway, and bubblewrap works there. Needs a tier and a WSL detection path |
| Container runtime (Docker/Podman) | Cross-platform and well understood; adds a hard dependency |
| Windows Job Objects / AppContainer / restricted tokens | Native but substantial work, and weaker than namespace isolation |

Recommendation: implement fail-closed now, add a WSL2 tier as the real Windows answer, and say
plainly in the documentation which platforms have enforced isolation until that lands.

> Note: `bwrap` is not present in the development container used to write this plan, so the
> bubblewrap path cannot be exercised end to end here. Any claim that sandboxed execution works
> must come from a run on a host that has it.

---

## 6. Why this also serves the training work

The trace format in Step 6 — prompt state, reasoning, action, result — is the trajectory format
that rejection sampling and expert iteration need. Collecting successful trajectories to fine-tune
on requires exactly this record. Logging detailed enough to debug is logging detailed enough to
train on, so Steps 5 and 6 are not overhead against the RL goal; they are a prerequisite for it,
and they run on local hardware.

---

## 8. Outstanding after this work

Both pieces this section originally held open — session taint marking paired with a web approval
endpoint, and a Windows isolation tier — have since landed. What follows records what was done
and what is still not verified, since "implemented" and "verified" are different claims.

**Session taint marking + operator approval — done.** Flagged tool output now marks the session
tainted, so the broker escalates the next action beyond passive recon, and `_web_confirm`
replaces the `_deny_approval` that refused unconditionally: it creates a request, pushes it to
the session's event stream, and blocks that turn's thread until the operator answers through
`POST /api/approvals/{id}/resolve`. Blocking is safe because a turn runs on its own daemon
thread, not the event loop. Two things were found while doing it:

* The approval endpoints and the *entire* UI for them — badge, panel, card, approve/decline, a
  5s poll — already existed. Nothing had ever created a request, so it was a finished UI wired
  to nothing.
* The `injection_flagged` event the server had been pushing since screening was wired in had no
  `case` in the frontend's event switch, so it was dropped. The one signal that says "this
  output tried to redirect the agent" reached nobody. It renders now, and says whether it gated
  the session or was only a notice.

`knowledge_search` is exempt from tainting, mirroring `loop.py`'s
`INJECTION_SCAN_EXEMPT_TOOLS` — that corpus is HackTricks and ExploitDB, text *about* injection
rather than attacker-controlled, and a single search for "prompt injection" would otherwise gate
the next real action. `read_file` is deliberately not exempt: the workspace holds whatever the
agent saved from a target.

Not changed, and worth an explicit decision by the project owner rather than by whoever is
editing this file: `SAFE_WHILE_TAINTED` is `{"passive_recon"}`, so a tainted session needs an
approval even for a local `knowledge_search`, which sends nothing anywhere and cannot act on the
injection. Widening it would be defensible and would reduce clicks; it would also be weakening a
security default for ergonomics, which is not a call to make in passing.

**A Windows isolation tier — implemented, not yet exercised on Windows.** `wsl2` runs the same
bubblewrap profile *inside* the WSL2 guest. The composition is deliberate: a bare
`wsl.exe -- <command>` is not isolation, because the guest mounts the Windows drives at `/mnt/c`
and has full network access — the two things the bubblewrap profile exists to prevent. The VM
boundary is additional to the namespaces, not a replacement.

What it costs relative to the Linux tier is seccomp: the deny-list is compiled to BPF and handed
to `bwrap` as an open file descriptor, and a descriptor does not cross the `wsl.exe` process
boundary. The profile digest records `seccomp_active: false`, so an audit trail distinguishes a
run that had the syscall filter from one that did not. Resource limits are applied by the guest
shell (`ulimit`, `&&`-chained, then `exec bwrap`) and are not best-effort — a shell that rejects
any of them fails the run.

The honest limitation: there is no Windows host or WSL2 guest in this project's development
environment, so what is verified is argv construction, path translation via the guest's own
`wslpath`, and every refusal path — 55 checks in `agent/sandbox/test_wsl2.py` — not a real
sandboxed execution. The design puts that gap on the safe side: `availability._probe_wsl2`
always exec-verifies (it runs a sandboxed `/bin/true` under this tier's own ulimit prologue
inside the guest) rather than inferring availability from `wsl.exe` being on PATH, and
`resolve_tier` refuses instead of degrading. So on a host where any assumption here is wrong,
the operator is told the tier is unavailable and nothing runs. The bad outcome is "it refuses on
a host where it could have worked", never "it ran unsandboxed while reporting a sandbox".

**Also closed since.** `_powershell_body` and its two regexes are deleted — they were kept
pending this Windows tier and the tier does not use them. The web `Engagement` now carries
`deny_targets`, so a per-engagement carve-out ("this /24 except the domain controller at .10")
is expressible instead of only the base cloud-metadata denials; the base entries are prepended
rather than replaced, so an engagement can only add to the deny set.

**Still not verified here.** `agent.web.test_frontend` skips all 14 of its tests without a
reachable llama-server and lab container, so every UI change in this work is unexercised: the
engagement-validation change, the approval panel now being driven by real requests, the
`injection_flagged` rendering, and the `wsl2` entry in the isolation picker. The approval poll
no longer requires `S.running` — an autonomous phase run reports not-running while still
executing tools, which is exactly when an approval can appear — and that too is reasoned, not
observed.
