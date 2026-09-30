# System Architecture

Oxpecker comprises three tiers: (1) a reasoning core consisting of the fine-tuned Qwen3-32B model working with a prompt registry and pipeline orchestrator; (2) a guarded execution layer where every tool call passes through a Broker for safety checks before reaching the sandboxed tool environment; and (3) a knowledge and isolation layer providing the RAG knowledge base, bubblewrap sandbox, and encrypted evidence store.

## Agent Loop and Tool Calling

The model communicates with tools through a structured tool-call format. Every model response consists of three components:

- A `<think>` block containing chain-of-thought reasoning before any action decision
- An explanation text summarizing the intended action for operator review
- A `<tool_call>` block containing the JSON-validated command to be sent to the sandbox

This format ensures that every action is preceded by explicit reasoning and is parseable for automated safety review.

## Broker and Safety Layer

Every tool call passes through the Broker, which enforces safety policies before permitting execution:

| Component | Function |
|-----------|----------|
| **RoE Enforcement** | Validates that the action falls within the authorized Rules of Engagement |
| **Scope Checking** | Verifies that the target is in the allow-list. Hallucinated targets are automatically blocked |
| **Injection Guard** | Scans tool output for prompt injection attempts using base64, homoglyph, and zero-width character detection with cross-turn taint tracking |
| **Rate Limiting** | Caps the frequency of actions by type |
| **Approval Queue** | Routes high-risk actions to a human operator for approval |

## Sandbox Isolation

Every command executes inside a bubblewrap sandbox providing kernel-level isolation:

- Filesystem isolation (unshare-all, bind-mount workspace only)
- Network namespace isolation and PID namespace isolation
- seccomp deny-list (ptrace, mount, kernel module loading)
- Resource limits (RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE)

## RAG Knowledge Base

The system augments the model with a Retrieval-Augmented Generation knowledge base containing 547,118 chunks from over 15 sources:

| Source | Chunks |
|--------|--------|
| NIST NVD | 247,199 |
| CyberStrike | 120,966 |
| Fenrir | 99,749 |
| ExploitDB | 46,505 |
| HackTricks | 13,044 |
| SFT Knowledge | 12,572 |
| GTFOBins · Atomic · Others | 7,083 |
| **Total** | **547,118** |

The knowledge base uses the nomic-embed-text-v1.5 embedding model (137M parameters, 768 dimensions) with L2-normalized numpy flat vector index and cosine similarity retrieval.

## Prompt Architecture

The system prompt uses a compiled 6-layer architecture, each layer versioned and deterministic:

| Layer | Purpose |
|-------|---------|
| 1 · Core Identity | Role definition, capability boundaries |
| 2 · Authorization | Permission model, human-in-the-loop |
| 3 · Data Provenance | Evidence handling, chain of custody |
| 4 · Action Protocol | Tool usage procedures, safety checks |
| 5 · Reporting | Output format specification |
| 6 · Engagement | Jinja2 template rendering per context |

## Pipeline Orchestrator

The system uses a 6-phase state machine to manage each engagement:

```
INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT
```

Key design decisions: idempotent task planning (tasks are created based on current state, never duplicated), budget-driven transitions (phases complete even with zero findings), and two pipeline profiles (Web/API and Network) sharing the same state machine.
