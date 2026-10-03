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

Oxpecker enforces safety architecturally, not by relying on a model to refuse:

- **Scope / RoE enforcement** — every request is checked against an allowlisted engagement scope;
  out-of-scope targets are hard-refused. The default engagement ships empty / localhost-only.
- **Destructive-command denylist** — clearly destructive host commands are blocked outright.
- **Sandboxed / isolated execution** — the agent is intended to run in an isolated environment with
  no path to production systems.
- **Audit logging** — actions, observations, and decisions are logged for review.

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
