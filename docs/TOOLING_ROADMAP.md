# Tooling Roadmap

Plan for the agent's security-tool layer. Written to capture decisions made in design
discussion so they are not re-litigated. Status of everything described here: **planned**,
unless the Implementation Status table in the [README](../README.md#implementation-status) says
otherwise.

---

## 1. Where the tool layer stands today

`app/agent/security_tools/` contains two tools:

| Tool | Lines | Capability |
|------|-------|------------|
| `http_recon.py` | 257 | Passive HTTP metadata. **GET only** (`conn.putrequest("GET", ...)`, line 132) |
| `port_discovery.py` | 100 | Socket-based port check; no service/version detection |

Verified gaps, by inspection:

- **No request method other than GET.** No POST, PUT, PATCH, DELETE; no request body, so no
  `Content-Type` or `Content-Length` handling exists.
- **No session state of any kind.** A grep across `security_tools/`, `broker/` and `tools/` for
  cookie, CSRF, Set-Cookie or Authorization handling returns nothing.
- Everything beyond unauthenticated GET therefore goes through `execute_bash`, with the model
  hand-writing the whole request (typically `curl`).

### Why this matters

With unauthenticated GET as the only structured HTTP capability, the agent cannot test login
flows, IDOR, POST-based SQL injection, authentication bypass, CSRF, or privilege boundaries.
That is most of the content of the two AutoPenBench categories the committed run does worst on.

For reference, from `evaluation/results/run_20260912_230749.json` (8/33 solved, 5 tasks errored
before a verdict):

| Category | Solved |
|----------|--------|
| cryptography | 3 |
| network_security | 2 |
| web_security | 2 |
| access_control | 1 |

`access_control/vm0` is a sudoers misconfiguration and consumed all 40 steps without solving.

---

## 2. Design rules

These are constraints on every adapter, not style preferences.

### R1 — Invoke by subprocess, never import

sqlmap is GPLv2; this project is Apache 2.0. Importing it into our process creates a licence
conflict, while running it as a separate process and reading its output files does not.
Additionally, do not vendor nmap into the repository — NPSL carries redistribution restrictions
that are commonly mistaken for GPL-like terms.

> Licences named in this document are from general knowledge and **must be confirmed at
> integration time.** The project already has one outstanding licensing question (the Apache 2.0
> declaration on a dataset derived from third-party corpora); do not add a second.

### R2 — The model selects an ID, never a filesystem path

Applies to wordlists, nuclei template directories, and any other path-shaped argument. If the
model can name a path, `--wordlist /etc/shadow` becomes an arbitrary file read whose contents
leave through tool output. Mirror the existing pattern in `recipes/runner.py`, which restricts
recipe steps to an `ALLOWED_TOOLS` allowlist.

This is not covered by existing controls: `scope_check` governs network targets, and the
injection guard inspects incoming output. Neither sees the agent reading a local file it was
never meant to touch.

### R3 — The model supplies intent; the harness supplies mechanics

The model states what it wants to do. The harness computes everything mechanical. See §3.

### R4 — Deliberately malformed requests are a separate, harder-gated action class

Request smuggling and CRLF/header injection legitimately require malformed requests, so a raw
mode must exist — but raw mode bypasses every protection a structured builder provides. Gate it
the way `http_recon` already gates certificate skipping: `verify_cert=False` is the distinct
action class `http_recon_insecure`, denied unless the RoE explicitly allows it, on top of the
existing approval gate. Reuse that pattern rather than inventing a new one.

### R5 — Prefer the recognised tool over the obscure one

Part of sqlmap's value is that a sqlmap finding needs no defending. Adopting a little-known tool
means defending both the finding and the tool. Deviate only for a concrete reason: licence, no
machine-readable output, or unmaintained.

### R6 — Selection criterion for adopting a tool

In priority order:

1. Machine-readable output (JSON/JSONL/XML/SARIF) or a stable API — this decides adapter cost
2. Deterministic enough to serve as an oracle
3. Maintained (commits within ~12 months)
4. Recognised in the field
5. Licence permits subprocess invocation

Criterion 1 is the discriminator. "Is it an alternative to a famous tool" is **not** a useful
criterion, because the famous tools are already open source — see §5.

---

## 3. `http_request` + session store

The foundational piece. Add `http_request` as an **active** action class, distinct from the
passive `http_recon`, plus a session store owned by the harness.

Division of responsibility:

| Model supplies | Harness computes |
|----------------|------------------|
| method, path | `Host`, `Content-Length` |
| params / body as a **structured dict** | `Content-Type`, inferred from body shape |
| `session: "<id>"` | the entire cookie jar |
| `csrf: {from: last_response, field: "..."}` | scrapes the token and injects it |
| | `Accept-Encoding`, and decompresses the response |

### Why the model cannot do this itself

Two distinct failure layers. The second does not improve with a better model.

**Formatting** — the model manages this unreliably: it cannot count bytes accurately for
`Content-Length`; it mismatches `Content-Type` against the actual body (form-encoded vs JSON vs
multipart); it gets multipart boundaries wrong; it omits `Accept-Encoding` and then cannot read
the gzipped response. The last of these is already visible in the committed eval log
(`Error: gzip is not supported by this browser`).

**State** — the model *cannot* do this correctly at any size. Cookies change with every
response. A CSRF token must be scraped from the preceding response, meaning **the correct value
does not exist at the moment the model writes the request.** Authorization tokens expire,
anti-automation nonces are single-use. A one-shot hand-written request is wrong by construction.

Every failure costs a step against a 40-step budget.

### Session as an opaque ID

The session is a harness-side object referenced by ID; the model never sees a raw cookie or
token. This also removes an exfiltration path — the model cannot place a session token into a
URL for another host, because it never holds the token.

### Implementation note

Do not write an HTTP client. `requests` already handles `Content-Length`, encoding, cookie jars
and redirects.

However `requests` cannot be dropped in as-is: `http_recon.py` deliberately uses raw
`http.client` so it can **pin the connection to the broker-validated IP** (preventing DNS
rebinding) while `Host` and TLS SNI still carry the original hostname. That decision is correct
and must be kept. The path forward is to move the existing `_PinnedConnection` behind a custom
`requests` `HTTPAdapter`, which keeps IP pinning while gaining the cookie jar and body encoders.

### What this unlocks beyond capability

`recipes/model.py` is designed to replay a finding through typed tool invocations, but **no
POST-based or authenticated finding can be reproduced today**, because no tool can issue one.
With `http_request` plus sessions, recipes for IDOR, SQLi and auth bypass become replayable, and
the designed retest mode works as intended.

---

## 4. Tool adoption plan

| Tool | Role | Output consumed | Helps category | Value to RL environment |
|------|------|-----------------|----------------|-------------------------|
| **(ours)** `http_request` + session | Active HTTP with state | — | web_security, access_control | Prerequisite for every other web adapter; makes recipes replayable |
| **httpx** | Fingerprinting — what is this host running | `-json`: status, title, tech, server | web (recon) | Produces the fingerprint key the wordlist selector needs |
| **nuclei** | Primary detection engine | `-jsonl`: template-id, severity, matched-at | web_security | **Highest.** One template = one named, typed check → maps 1:1 onto the hypothesis graph (template-id → hypothesis, match → observation with `polarity: supports`). Deterministic, so usable as an oracle |
| **ffuf** | Content discovery; HTTP login brute | `-of json` | web_security, access_control | Discovered paths are re-checkable facts; login success is a binary oracle |
| **(ours)** privesc enumerator | `sudo -l`, SUID, capabilities, cron, writable PATH | ours, typed by construction | **access_control** | **Strongest oracle** — uid/gid and file existence are mechanically checkable, no interpretation |
| **sqlmap** | Deep SQLi after a probe hits | output-dir log + results file | web_security | Injectable parameter, technique and DBMS as a typed finding |
| **nmap** | Upgrade `port_discovery.py` to real service/version detection | `-oX` XML | network_security | Trustworthy service fingerprints |
| **ZAP** | The Burp replacement — daemon-mode web scanning | REST API JSON | web_security | Overlaps nuclei in part; later |
| **Metasploit** | Actual exploitation | msfrpcd RPC | exploit stage, all categories | **Last.** Popping a shell must route through `broker/approval_queue.py` |

### Privilege escalation is the one case for writing our own

linPEAS and LinEnum are good tools, but their output is colour-coded text with no reliable
machine-readable mode. Adapting them means parsing coloured text, which is the problem the whole
adapter layer exists to escape. Writing a focused enumerator is cheaper here — and it happens to
target the category the committed run does worst on.

### Order

```
Phase 1   http_request + session store     <- first; everything else depends on it
          httpx        (fingerprint)
          nuclei       (detection)
          ffuf         (discovery + login brute)

Phase 2   privesc enumerator (ours)

Phase 3   sqlmap  (subprocess, per R1)
          nmap    (-oX, replacing port_discovery)

Phase 4   ZAP           (daemon lifecycle)
          Metasploit    (approval-gated; highest risk)
```

The session layer must come first. ffuf cannot brute-force a login without POST; authenticated
nuclei templates need a cookie; privesc is post-exploitation and presupposes access. Building it
later means revisiting every adapter.

Cost estimate: following the quality bar set by `http_recon.py`, roughly **250 lines per
adapter** plus tests. Phase 1 is therefore on the order of 750–1000 lines.

### Relevance to the 9B

A smaller model hand-writes requests markedly worse than a 32B. If the 9B is to serve as a
demonstration that a locally-runnable model does useful work, as much mechanism as possible must
move out of the model and into tools. This roadmap is that move.

---

## 5. Decisions recorded, with the reasoning

### Do not reimplement the well-known tools

sqlmap is roughly two decades of work, five injection techniques, ~100 tamper scripts and
support for dozens of DBMS backends. Metasploit carries on the order of 2,000 exploit modules.
A solo reimplementation produces something worse while consuming the time that should go to the
agent and its safety architecture, which is where this project's contribution actually lies.

### Most of the famous tools are already open source

| Tool | Licence (confirm before shipping) | Need a substitute? |
|------|-----------------------------------|--------------------|
| sqlmap | GPLv2 | No — already OSS |
| Metasploit Framework | BSD-style (Rapid7) | No — already OSS |
| nmap | NPSL | No |
| ffuf, nuclei, httpx | MIT / Apache-style | No |
| **Burp Suite** | **commercial** | **Yes — the only one** |

For the Burp slot, ZAP is not a weaker substitute but a better fit: it offers daemon mode and a
REST API built for headless automation, whereas Burp's scanning API requires the Pro licence.

### Wordlists: build an index, not a collection

Two things were initially conflated.

**SQL injection payloads are not wordlist-shaped.** They depend on DBMS, injection point and WAF
behaviour, and sqlmap's payload engine with tamper scripts handles this far better than any
static list could. A `sqli-payloads.txt` would underperform what already exists.

*Exception:* a very small probe set for cheap triage — `'`, `' OR '1'='1'--`, one time-delay
probe — is worth having, to decide whether escalating to sqlmap is justified. That saves steps.
Build the probe set; do not build a payload library.

**Credential lists genuinely are list-shaped,** and categorising them helps. The important axis
is not only product but **size tier**, because the agent runs under a step and time budget — a
14-million-line list is useless within 40 steps.

```
fingerprint   ->   candidate lists      ->   size tier
(Tomcat 9)         tomcat-defaults           quick     (~100)
                   common-web-admin          standard  (~10k)
                   rockyou                   deep      (1M+)
```

Ordered by hit rate against cost: product defaults hit most often and are the smallest lists
(Tomcat, Jenkins, Grafana, router admin panels), then top-N common, then deep.

Do not vendor the lists. The manifest points at SecLists with checksums, plus a bootstrap script
the operator runs. This keeps the repository small and the licensing clean. Note provenance in
the manifest — rockyou is a breach dump; it is an industry standard and ships with Kali, but a
project under funding review should state that rather than vendor it silently.

Subject to **R2**: the model picks a list by ID. It never supplies a path.

**The selector is itself a verifiable task.** Given a fingerprint, which list did the agent
choose, and did it find the credential? That is mechanically checkable, making it another source
of dense reward that requires no interpretation.

---

## 6. Why this work is on the critical path

The GRPO stage is blocked on the absence of a mechanically-verified oracle, not on the absence
of GPUs. Structured tool output is that oracle:

- A tool returning `{"sqli": true, "param": "id", "technique": "boolean-blind", "dbms": "mysql"}`
  gives a reward signal that can be checked.
- A tool returning several hundred lines of text forces the agent to summarise, and rewarding the
  summary is reward hacking.

The hypothesis graph already models observations with `polarity: supports | refutes | neutral`
and a strength, and hypotheses with verdicts. The structure anticipated this; nothing currently
feeds it.

So the adapter layer serves three ends at once: the agent stops burning context on unparsed
output, the hypothesis graph gets real input, and the RL stage gets a verifiable reward. All of
it is work that can be done on local hardware now, before compute is available.

---

## 7. Related outstanding work, not covered here

- **Training-data decontamination** — specified in `docs/TRAINING_DATA.md`, no implementation.
  Until it runs, the AutoPenBench result is uncontrolled for train/test overlap. CPU-only work,
  so it is also doable before compute arrives, and it is what makes the existing number
  defensible.
- **Dataset licence review** — the Apache 2.0 declaration covers a corpus derived from sources
  with their own terms. Worth settling before any public dataset release.
- **9B evaluation** — once its CPT and SFT finish, run it on the same AutoPenBench suite so the
  comparison is like-for-like. A locally-runnable model with a real score is a stronger artefact
  than a 32B number alone, because other people can actually run it.
- **The 31-row discrepancy** between `docs/TRAINING_DATA.md` and `datasets/README.md`, untraced.
