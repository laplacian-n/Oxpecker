# Security & Responsible Use

Oxpecker is a **research and educational** penetration-testing framework. It is dual-use software:
the same automation that helps a defender or researcher understand and fix vulnerabilities can cause
harm if pointed at systems without permission. Please use it responsibly.

## Authorized use only

By using Oxpecker (the `app/` desktop agent, the training/evaluation code, or any released model
weights) you agree to operate it **only against systems you own or are explicitly authorized, in
writing, to test.** Typical legitimate targets:

- Deliberately-vulnerable training apps you host yourself — OWASP Juice Shop, DVWA, WebGoat, etc.
- CTF / boot2root lab VMs (VulnHub, Hack The Box) running in your own lab.
- Local containers and virtual machines on `localhost` / private (RFC 1918) networks.
- Systems covered by a signed engagement / Rules of Engagement (RoE) or a bug-bounty program's scope.

**Do not** use Oxpecker against production systems, third-party services, or any host you are not
authorized to test. Unauthorized access to computer systems is illegal in most jurisdictions.

## Safety controls built in

> ### Per-runtime status, and what is still missing
>
> An earlier audit found the desktop application — the FastAPI backend at
> `app/agent/web/dev_server.py`, which is what the installer runs — did not use most of the
> controls below, while this file claimed it did. That has been fixed; the table records where
> each control stands now, including the gaps that remain. The finding and the work are in
> [docs/OBSERVABILITY_PLAN.md](docs/OBSERVABILITY_PLAN.md).
>
> | Control | `agent/` research runtime | desktop app |
> |---|---|---|
> | Scope / RoE enforcement | enforced via the broker | **active** — the same matcher, exact host and CIDR comparison, deny evaluated before allow, denying on an empty or expired engagement |
> | Destructive-command denylist | — | **active** |
> | Sandboxed execution | bubblewrap + seccomp + rlimits | **active on Linux**. On Windows, select the `wsl2` tier — the same bubblewrap profile inside the WSL2 guest, without seccomp; implemented and unit-tested, but never exercised on a real Windows host, so it verifies itself by execution at startup and refuses if it cannot. Everywhere else `run_command` refuses rather than running unsandboxed |
> | Audit logging | hash-chained audit log + encrypted evidence store | **active** — every tool call, plus a separate debug trace and a reader (`agent.web.trace_cli`) |
> | Broker mediation (RoE class gate, rate limit, kill switch, evidence) | active | **active** for the outward-facing tools; `run_command` is gated by the sandbox instead, by design |
> | Injection screening of tool output | active | **active** |
> | Human approval of escalated actions | interactive prompt | **active** — the request appears in the UI's Approvals panel and the turn waits for the answer. Anything that is not an explicit approval, a timeout included, is a denial |
> | Session taint after flagged output | marked, escalates to approval | **active** — a flagged tool result taints the session, so the next action beyond passive recon needs an operator. The local knowledge index is exempt: it is text *about* injection, not attacker-controlled |
>
> **This is still dual-use software that runs model-chosen commands.** The isolation is real on
> Linux, available on Windows through WSL2 but not yet exercised on one, and absent elsewhere —
> and no sandbox is a substitute for running it somewhere you can afford to lose. Prefer a
> disposable machine or VM.

Oxpecker enforces safety architecturally, not by relying on a model to refuse:

- **Scope / RoE enforcement** — every request is checked against an allowlisted engagement scope;
  out-of-scope targets are hard-refused. An empty allowlist permits nothing, and cloud
  instance-metadata addresses are denied regardless of what an engagement lists.
- **Destructive-command denylist** — clearly destructive host commands are blocked outright.
- **Sandboxed / isolated execution** — kernel-enforced namespace isolation with a seccomp
  profile and resource limits. *(Linux, or Windows via the `wsl2` tier, which runs the same
  profile inside the WSL2 guest without seccomp. Elsewhere local command execution is refused
  rather than downgraded, and a tier that cannot run is refused rather than silently replaced
  by a weaker one.)*
- **Human approval, and taint after flagged output** — output that trips the injection screen
  marks the session, and the broker then holds any action beyond passive reconnaissance until an
  operator approves it in the UI. A request that goes unanswered expires as a denial.
- **Audit logging** — every action, observation and decision is recorded in a hash-chained log,
  with full output in an encrypted evidence store. Verify a session's chain with
  `python3 -m agent.main --verify-audit <session_id>`, or read it back with
  `python3 -m agent.web.trace_cli <session_id>`.

These controls reduce casual misuse; they are not a substitute for the operator's own legal
authorization and judgment.

## Gated model weights & knowledge base

The specialized fine-tuned model and the retrieval (RAG) knowledge base are **not** distributed in
this repository. They are released **by request only via Hugging Face**, under gated access. The
public code is a framework; the trained capability is gated on purpose.

## Reporting a vulnerability

If you find a security issue **in Oxpecker itself** (e.g. a scope-enforcement bypass, a sandbox
escape, or an injection-guard weakness), please report it privately rather than opening a public
issue:

- Open a **GitHub private security advisory** on this repository (Security → Advisories → Report a
  vulnerability), or
- Contact the maintainer directly.

Please include steps to reproduce and the impact. We aim to acknowledge reports promptly and will
credit reporters who wish to be credited.

## No warranty

This software is provided for research and education **as-is, without warranty of any kind**. The
authors accept no liability for misuse or for any damage arising from its use. You are solely
responsible for ensuring your use is lawful and authorized.
