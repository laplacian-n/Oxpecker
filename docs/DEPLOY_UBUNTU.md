# Running Oxpecker with the server on Ubuntu

Decided topology: **the server, the GPU and the model all live on one Ubuntu box at home.**
Windows, if it is used at all, is only a window onto it.

```
  Windows laptop                      Ubuntu box (home LAN, GPU)
  ┌──────────────┐                    ┌──────────────────────────────────────┐
  │ browser or   │   SSH tunnel /     │  dev_server   --host 127.0.0.1:7777  │
  │ Electron     │◀──  Tailscale  ───▶│  llama-server (chat)   127.0.0.1:8080│
  │ (UI only)    │                    │  llama-server (embed)  127.0.0.1:8091│
  └──────────────┘                    │  bubblewrap + seccomp + rlimits      │
                                      │  $OXPECKER_DATA_DIR ──▶ backup       │
                                      └──────────────────────────────────────┘
```

Nothing binds a public interface. The tunnel is the authentication; the API key below is the
second layer, for the day someone types `--host 0.0.0.0`.

## Why the server belongs on Linux, not Windows or WSL2

This is not a preference. The isolation tier the agent runs tools under is only real on Linux:

| tier | what it actually provides |
|---|---|
| `direct` | **no kernel isolation** — a scrubbed environment and a command blocklist, nothing more |
| `bubblewrap` | namespace isolation (fs/net/pid) + rlimits + a seccomp syscall filter |
| `wsl2` | bubblewrap inside the WSL2 VM — namespaces and rlimits, but **no seccomp** |
| `microvm` | not implemented |

`wsl2` loses seccomp for a concrete reason, not an oversight: the compiled BPF program is handed
to `bwrap` as a file descriptor, and a file descriptor does not cross the `wsl.exe` process
boundary. On top of that, the `wsl2` tier has never been exercised on real Windows hardware — it
is tested only by unit tests on Linux. Running the server natively on Ubuntu is the only
configuration where the sandbox is both complete and actually tested.

Three further wins, all of which follow from there being exactly one server process:

- **One rate-limit window.** `Program.max_requests_per_min` is enforced by a per-process sliding
  window in the broker. Two machines running the agent means the cap a program's terms set is
  silently doubled — which is how you get IP-banned while believing you are throttled.
- **One audit chain.** The hash-chained audit log and its checkpoint file are per-host on disk.
  Two hosts produce two chains that cannot be reconciled into one account of what was done.
- **The data ends up somewhere backupable.** Trajectories, evidence, the audit chain and the
  debug traces are the training set. On a server they sit on a disk with a backup job instead of
  on a laptop that will be reinstalled.

## Install

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip bubblewrap tree

sudo useradd --system --create-home --home-dir /var/lib/oxpecker oxpecker
sudo mkdir -p /opt/oxpecker /var/lib/oxpecker/{data,state}
sudo chown -R oxpecker:oxpecker /opt/oxpecker /var/lib/oxpecker

sudo -u oxpecker git clone <your-remote> /opt/oxpecker
sudo -u oxpecker python3 -m venv /opt/oxpecker/.venv
sudo -u oxpecker /opt/oxpecker/.venv/bin/pip install \
    fastapi 'uvicorn[standard]' scikit-learn numpy cryptography pydantic requests jinja2 PyYAML fpdf2 mcp playwright pyseccomp
```

`uvicorn[standard]` rather than plain `uvicorn`: the bare package has no WebSocket
implementation, so `/api/sessions/{id}/ws` silently stops being a WebSocket route and answers as
plain HTTP (404). The shipped UI uses SSE and does not need it, but a client that opens the
WebSocket will fail confusingly without it.

`tree` is not a Python dependency, but `config.COMMAND_ALLOWLIST` lets the model call it via
`run_command`, and it is not installed by any other package on a stock Ubuntu image. Found by
running every allowlisted command for real under the seccomp filter instead of a four-command
sample (see `test_seccomp_profile.py`) — 18 of the 22 commands, including this one, had never
actually been invoked by that check.

`cryptography` is not mentioned anywhere else on this page, but it is a hard dependency:
`agent/evidence/store.py` imports `cryptography.fernet` at module scope, `agent/audit_log.py`
imports `evidence.store` at module scope, and `dev_server.py` imports `audit_log` at module
scope. A venv missing it fails before `dev_server` binds a port, with
`ModuleNotFoundError: No module named 'cryptography'` — nothing to do with the sandbox or the
LLM provider.

`pydantic` is listed for the same reason one step earlier: `dev_server.py` imports it directly
for its request bodies, and it is present today only because fastapi happens to depend on it.
A dependency we rely on by accident is one upstream release away from being the next
`cryptography`. `agent/web/test_dev_server_imports.py` keeps this list honest.

`pyseccomp` is what makes the seccomp filter available. Without it `bubblewrap` still gives
namespaces and rlimits, and the tier honestly reports that no filter was loaded — the result
dict's `seccomp_active` and the profile digest's `seccomp_syscalls_source` both say so rather
than implying protection that is not there.

Verify before trusting it:

```bash
cd /opt/oxpecker/app
/opt/oxpecker/.venv/bin/python -c \
  "from agent.sandbox import availability as a; import json; print(json.dumps(a.describe_host(), indent=2))"
```

Expect `"strongest_available": "bubblewrap"`, `"kernel_isolation_available": true` and
`"seccomp_available": true`. If `bubblewrap` reads unavailable, the `reason` field says why.

> On Ubuntu 23.10 and newer, unprivileged user namespaces are restricted by AppArmor
> (`kernel.apparmor_restrict_unprivileged_userns`). The `bubblewrap` package ships a profile for
> this, so it normally works — but this is the first thing to check if the tier reports
> unavailable, because it is the one failure that silently downgrades every tool run to
> `direct`. Confirm with the command above rather than assuming.

## Paths

Both state roots are environment-overridable, and on a server both should be moved off the
source tree — otherwise a `git pull` runs over the directory holding the state store, the audit
trail and the traces, and no backup job would think to look inside a checkout.

| variable | holds |
|---|---|
| `OXPECKER_DATA_DIR` | dev_server state, the agent's workspace root, evidence, debug traces |
| `AGENT_STATE_DIR` | engagement state, the API key file, caches |
| `OXPECKER_BROWSER_EXECUTABLE` | a Chromium binary to use instead of Playwright's own download (optional) |

`OXPECKER_DATA_DIR` is also the confinement root the file tools are checked against, so it is
resolved eagerly at import; point it at a real directory, not a symlink you intend to re-aim.

## systemd

`/etc/systemd/system/oxpecker-llama-chat.service`:

```ini
[Unit]
Description=llama-server (chat model) for Oxpecker
After=network-online.target

[Service]
User=oxpecker
ExecStart=/opt/llama/llama-server -m /var/lib/oxpecker/models/<chat-model>.gguf \
    --host 127.0.0.1 --port 8080 -ngl 999 -c 32768 -fa on \
    --cache-type-k q8_0 --cache-type-v q8_0 -np 1 -lm none
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/oxpecker.service`:

```ini
[Unit]
Description=Oxpecker dev server
After=network-online.target oxpecker-llama-chat.service
Wants=oxpecker-llama-chat.service

[Service]
User=oxpecker
WorkingDirectory=/opt/oxpecker/app
Environment=OXPECKER_DATA_DIR=/var/lib/oxpecker/data
Environment=AGENT_STATE_DIR=/var/lib/oxpecker/state
ExecStart=/opt/oxpecker/.venv/bin/python -m agent.web.dev_server \
    --host 127.0.0.1 --port 7777 \
    --llama-url http://127.0.0.1:8080 \
    --vector-index /var/lib/oxpecker/knowledge_rag/index/vectors.npy \
    --vector-meta  /var/lib/oxpecker/knowledge_rag/index/meta.jsonl \
    --embed-url http://127.0.0.1:8091
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Note `-m agent.web.dev_server`, not the path to `dev_server.py`. The module uses
package-relative imports, so running it as a script file fails at import with "attempted
relative import with no known parent package" before it binds anything.

**Do not add the usual systemd hardening directives to `oxpecker.service` without checking the
sandbox afterwards.** `RestrictNamespaces=`, `PrivateUsers=yes` and friends are exactly the
settings that stop `bwrap` from creating the namespaces the isolation tier is made of. The
failure is quiet: tool runs fall back to a weaker tier or refuse. If you harden this unit,
re-run the `describe_host()` check above *under the unit* and confirm `bubblewrap` is still
available. Hardening that disables the sandbox is a net loss.

## Reaching it from Windows

**Preferred — a tunnel, no code change and no open port:**

```bash
ssh -N -L 7777:127.0.0.1:7777 oxpecker-box
```

Then open `http://127.0.0.1:7777` on the laptop. Tailscale or WireGuard works the same way and
survives reboots better.

**Second layer — an API key.** Worth setting even behind a tunnel, because it is what protects
you on the day the bind address changes:

```bash
sudo -u oxpecker sh -c \
  "python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > /var/lib/oxpecker/state/web_ui_api_key.txt"
sudo chmod 600 /var/lib/oxpecker/state/web_ui_api_key.txt
```

Paste it into the key field in the UI; it is kept in `localStorage` and sent as `X-API-Key`
(the SSE stream passes it as `?key=`, since `EventSource` cannot set headers).

With no key file, `/api/*` is unauthenticated — which is the right default for a loopback
install and is what every existing test relies on. The server will not, however, start on a
non-loopback address with no key configured: behind those routes are `run_command`,
`/api/sessions/.../autonomous/start`, the evidence store and `/api/approvals/.../resolve`, so
unauthenticated it is remote code execution plus the ability to approve your own escalation.
`--insecure-no-auth` overrides the refusal and has to be typed on purpose.

An API key file that exists but is empty, unreadable or not UTF-8 makes every `/api` request
answer 503 and startup refuse — "no key found" and "key unreadable" are one line apart and only
one of them is safe to treat as "auth off".

CORS is off unless `AGENT_WEB_CORS_ORIGINS` is set. Both shipped clients load the page from this
origin and fetch `/api/...` relatively, so they are same-origin and need no CORS header at all.

## Back up

Everything of long-term value is under `$OXPECKER_DATA_DIR` and `$AGENT_STATE_DIR`:

- `state.json` — sessions, engagements, findings
- the evidence store **and its key** — without the key, stored evidence is undecryptable and
  every `content_digest` becomes unverifiable
- the audit log and its checkpoint file — the chain is what makes the record tamper-evident
- debug traces — the raw material for the training set

Back up the key and the evidence together or neither is useful. Keep the API key out of the
same backup as the evidence key; `agent/state/` is gitignored, so neither reaches the repo.

## Home line vs VPS

The box is at home, which is the right call for bug bounty: a residential IP clears Cloudflare's
managed challenges far more often than a datacenter one. In the transcript this project was
designed against, two hosts answered `cf-mitigated: challenge` from a cloud IP and a headless
browser sat on "Just a moment…" — a chunk of the target surface simply was not testable from
there.

The trade is that a ban or an abuse complaint lands on the household connection, and the home IP
is in the target's logs. Mitigations that now exist and are worth actually setting:
`Program.max_requests_per_min` per engagement, and `automation_allowed=False` on any program
whose terms you have not read. A VPS is still worth keeping as a second egress for the cases
where being identifiable is fine.

## The `wsl2` tier: kept, and marked

Deleting it was considered and rejected. The argument for deleting was that it is
safety-relevant code that has never run on real Windows hardware and that cannot carry a seccomp
filter. The first half is true, the conclusion does not follow:

- `_probe_wsl2` makes three real round trips into the guest — `/bin/true`, `bwrap --version`,
  then `bwrap --unshare-all --ro-bind / / -- /bin/true` under this tier's own `ulimit` prologue —
  and returns unavailable with a reason on any failure. It cannot report a tier it has not just
  demonstrated on the host it is running on. The usual reason to delete unverified safety code
  is that it claims protection it cannot deliver; that is engineered out here.
- Its own description states that it carries no seccomp filter, and why (the compiled BPF is
  passed to `bwrap` as a file descriptor, which does not cross the `wsl.exe` process boundary).
  It does not overclaim.
- It is the **only** tier a Windows operator can select. Every other tier needs a Linux kernel
  and a Unix PATH, and running unsandboxed on the Windows host is deliberately not offered as a
  fallback. Deleting the tier would make `run_command` permanently unavailable on Windows.

What was missing is only the knowledge that we have never exercised it, so that is now a field
rather than a fact you had to read the source to learn: `describe_host()["tiers"][t]["exercised"]`
is `False` for `wsl2` and `microvm`, `True` for `direct` and `bubblewrap`. `available` and
`exercised` answer different questions, and a tier can be the first without being the second.

For this deployment it stays unused: the server is Ubuntu and Windows is only a UI.
