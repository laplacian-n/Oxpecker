# Hand-off brief: bring Oxpecker up on the Ubuntu box and verify every component

Give the section below to the Claude session running on the Ubuntu machine. It is written to be
pasted as-is. The key is deliberately not in this file — hand it over separately.

---

## The task

Bring up Oxpecker on this Ubuntu host and prove every component works together. Do **not** run a
local model: the GPU is busy training, and the whole point of this run is that the system works
driven by an API instead.

Repository: `laplacian-n/Oxpecker`, branch **`claude/peaceful-curie-5mz7a2`**. All the work below
is already on that branch; nothing needs writing unless you find a bug.

```bash
git clone -b claude/peaceful-curie-5mz7a2 <remote> oxpecker && cd oxpecker/app
```

## Context you need

This is a self-hosted AI penetration-testing assistant. The shipped runtime is
`agent/web/dev_server.py` — a FastAPI server with its own agent loop, a broker that gates every
outward action against an engagement's rules of engagement, a hash-chained audit log, an
encrypted evidence store, and an isolation tier (bubblewrap + seccomp) that tool execution runs
inside.

Two facts that will save you time:

- **Run it as a module, never as a script path.** `python agent/web/dev_server.py` fails with
  "attempted relative import with no known parent package" before it binds a port. Use
  `python -m agent.web.dev_server`.
- **`agent/web/server.py` is not the shipped runtime.** It exists, it has 66 tests, and nothing
  launches it. Do not spend time on it.

Read `docs/DEPLOY_UBUNTU.md` first — it covers the topology, the install, the systemd units, and
one trap worth repeating here: **do not add the usual systemd hardening directives**
(`RestrictNamespaces=`, `PrivateUsers=yes`) to the service unit without re-checking the sandbox
afterwards. They are exactly what stops `bwrap` from creating the namespaces the isolation tier
is made of, and the failure is silent — tool runs quietly fall back to a weaker tier.

## Step 1 — install

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip bubblewrap
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi 'uvicorn[standard]' scikit-learn numpy pyseccomp
```

`uvicorn[standard]`, not bare `uvicorn`: the bare package has no WebSocket implementation, so
`/api/sessions/{id}/ws` stops being a WebSocket route and answers as plain HTTP.

Then verify the sandbox is real, before trusting anything that runs inside it:

```bash
python3 -c "from agent.sandbox import availability as a; import json; print(json.dumps(a.describe_host(), indent=2))"
```

Expect `"strongest_available": "bubblewrap"`, `"kernel_isolation_available": true`,
`"seccomp_available": true`. If `bubblewrap` reads unavailable, its `reason` field says why.

On Ubuntu 23.10+ check that first: unprivileged user namespaces are restricted by AppArmor
(`kernel.apparmor_restrict_unprivileged_userns`), the `bubblewrap` package ships a profile for
it, and this is the one failure that silently downgrades every tool run to `direct` — which
provides no kernel isolation at all. Confirm with the command above rather than assuming.

Note `describe_host()` also reports `exercised` per tier. `wsl2` is `False` there on purpose:
implemented and unit-tested, never run on real Windows. It is irrelevant on this host.

## Step 2 — the API key

You will be given an OpenRouter key separately. Put it where the code looks for it, and nowhere
else:

```bash
mkdir -p agent/state
printf '%s' '<THE KEY>' > agent/state/openrouter_api_key.txt
chmod 600 agent/state/openrouter_api_key.txt
```

`agent/state/` is gitignored. **Never pass the key on a command line** — `ps` shows argv to every
user on the host, and it lands in shell history. **Never commit it.** If you need it in the
environment instead, `export OPENROUTER_API_KEY=...`; the code checks the environment first.

The key is for verifying the system, not for a real engagement. It holds about $2.38.

## Step 3 — run the smoke test

This is the main thing being asked for. One command, and it exercises everything:

```bash
python3 -m agent.web.smoke_api_mode
```

It starts a real `dev_server` subprocess, serves its own deliberately-broken HTTP target on
loopback, creates a scoped engagement with a Program, runs one agent turn against that target,
and then checks what landed on disk.

**It defaults to a free, tool-capable model, so a normal run costs $0.00.** The key has a free
tier of 1000 requests/day. Do not pass `--model` with a priced model unless you are deliberately
testing the paid path.

Expected result, verified on another Linux host with real bubblewrap and seccomp:

```
  29 passed   0 failed   0 skipped
```

Free models are rate-limited upstream, so the script tries several in order. A `429` on the
first candidate is normal and is not a failure of this system — the script reports which model
answered.

### What the 29 checks actually prove

Worth knowing, so you can tell a real failure from a model being uncooperative:

| group | what it establishes |
|---|---|
| host and sandbox | a tier is available and declares whether it has been exercised |
| provider probe | capabilities and pricing read from OpenRouter, not assumed; tool support confirmed; reasoning fidelity declared (never `raw` for an API) |
| lab target | the built-in vulnerable target serves |
| server startup | `dev_server` starts with `--provider openrouter` and **no llama-server** |
| authentication | `/api/*` refuses an unauthenticated request and accepts the key; `/api/health` names the provider and model actually serving |
| engagement | a scoped engagement and a session are created |
| the turn | the model answers, calls tools, and the turn finishes |
| the record | `prompt`, `model_output` and `tool` are all in the debug trace; the audit log has entries; an entry cross-references the evidence store; **the hash chain verifies** |
| bookkeeping | the notebook/findings endpoints answer, and the model reached `record_note` / `record_finding` |
| cost | no traceback in the server log; what the run cost |

The last check in the bookkeeping group is the only one that depends on the model obeying an
instruction. If it reports SKIP — "the model did not call them this run" — that is a model
behaviour note, not a defect; the wiring is covered by `python3 -m agent.web.test_tool_parity`.

## Step 4 — run the test suites

```bash
python3 -m unittest discover -s agent -p "test_*.py" -t .
```

Expect `OK` with skips, **zero errors and zero failures**. Anything else is a real finding on
this host and worth reporting with the traceback.

The skips are all missing external services, and on this host most of them should stay skipped
because we are deliberately not running a local model:

| skipped because | count elsewhere | note |
|---|---|---|
| `llama-server not reachable` | ~15 | expected — GPU is training, no local model |
| `embedding server not reachable at :8091` | ~7 | same |
| `Juice Shop lab container not reachable at :3000` | ~24 | needs a docker daemon; optional, see below |
| `corpus_src/{gtfobins,lolbas,patt} not present` | ~11 | needs the corpus repos cloned |
| `local SearXNG container` | 1 | needs a docker daemon |

Then the script-style suites, which are not picked up by unittest discovery and must be run by
name:

```bash
for t in web.test_tool_parity web.test_dev_server_auth web.test_broker_mediation \
         web.test_tools_wiring web.test_scope web.test_approval_taint web.test_audit \
         web.test_debug_trace web.test_workspace_confinement sandbox.test_availability \
         sandbox.test_wsl2 llm.test_provider_seam; do
  echo "--- $t"; python3 -m agent.$t | tail -2
done
```

All should exit 0. Check counts vary with host capability (some checks only run when `bwrap` is
present), so compare the pass/fail verdict, not the totals.

## Step 5 — optional, if you want the remaining skips covered

Only if it is cheap on this host:

- **Juice Shop** (24 tests): needs a working docker daemon. `docker run -d -p 3000:3000
  bkimminich/juice-shop`, then re-run the suite.
- **The knowledge corpus** (11 tests): `agent/knowledge_rag/build_index.py`'s docstring names the
  repos to clone into `agent/knowledge_rag/corpus_src/`. Building the dense index needs the
  embedding server, so TF-IDF-only is fine for now.
- **Browser tests** (4 tests): they skip when Playwright cannot launch its pinned Chromium build.
  If this host has a Chromium that Playwright refuses, point
  `OXPECKER_BROWSER_EXECUTABLE=/path/to/chrome` at it and they run.

## What to report back

1. The smoke test's summary line, and the full output of any failure.
2. The `describe_host()` output — specifically `strongest_available` and `seccomp_available`.
3. The unittest summary line.
4. Any script-style suite that exited non-zero.
5. **One trajectory, dumped.** This is the deliverable that matters most, because the dataset is
   the point. The smoke test prints the state dir it used; dump the trace with:

   ```bash
   python3 - <<'EOF'
   import json, pathlib, sys
   d = pathlib.Path("<THE STATE DIR THE SMOKE TEST PRINTED>")
   tr = next(d.glob("debug_trace/*.jsonl"))
   for line in tr.read_text().splitlines():
       r = json.loads(line); k = r.get("kind")
       if k == "model_output":
           print(f"[{r['turn_index']}] MODEL reasoning={r['reasoning_chars']}ch "
                 f"tools={[t['name'] for t in r.get('tool_calls',[])]}")
       elif k == "tool":
           print(f"[{r['turn_index']}] TOOL  {r['tool_name']} {json.dumps(r['arguments'])[:80]}")
       elif k == "broker":
           print(f"[{r['turn_index']}] BROKER {r['tool_name']} {r.get('broker_status')} {r.get('policy_rule')}")
   EOF
   ```

6. **Whether the per-action-class cooldown cost turns.** On the verification run, the
   `active_web_request` cooldown is 2 seconds (`config.ACTION_CLASS_COOLDOWN_S`) while the
   model's turn latency was ~1.5s, so a second request was **denied** rather than delayed — and
   the model spent two extra turns (one `run_command sleep 2`, one retry) learning to come back
   later. The rate limit is correct; paying a full prompt plus reasoning to discover a
   two-second wait is the part worth measuring. Report how many broker denials in the trace have
   `policy_rule=rate_limit`, so we can decide whether the broker should wait out a short
   cooldown server-side instead of denying.

## Hard constraints

- **Do not run a local model.** No `llama-server`, nothing that loads a GGUF. The GPU is training.
- **Do not bind the server to a non-loopback address.** It will refuse to start without an API
  key configured, which is deliberate: `run_command`, `/api/sessions/.../autonomous/start`, the
  evidence store and `/api/approvals/.../resolve` all sit behind `/api/*`. If remote access is
  wanted, use an SSH tunnel (`ssh -N -L 7777:127.0.0.1:7777 <host>`) and keep the bind on
  loopback. `--insecure-no-auth` exists and should not be used here.
- **Never disable TLS verification** and never unset a proxy to make a request work.
- **Do not commit the API key**, and do not echo it into any file outside `agent/state/`.
- **Do not point this at any target you were not given.** The only target in this exercise is the
  loopback one the smoke test starts itself.
- **If you commit anything**, author it as `laplacian-n <dinucleotide10292910@gmail.com>` (use
  `git -c user.name=... -c user.email=...`) and add **no** `Co-Authored-By` or other attribution
  lines. Push only to `claude/peaceful-curie-5mz7a2`.

## If you find a bug

Likely, and welcome — the recurring defect in this codebase is code that is fully implemented and
never called, so a first real run on a new host is the best bug-finder available. Fix it, add a
test that would have caught it, and say in the commit message what the failure actually was
rather than what the fix does.
