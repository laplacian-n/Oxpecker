# Oxpecker

**Domain-adapted LLM for automated penetration testing: LoRA SFT plus a broker-mediated agent runtime and a verifiable lab environment**

Oxpecker is a self-hosted penetration testing agent built on [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B). The trained model comes from supervised fine-tuning (LoRA) on 123K+ curated cybersecurity examples. Around it sits an agent runtime whose distinguishing feature is that safety is enforced *architecturally* — an execution broker, an allowlisted engagement scope, and kernel-level sandboxing — rather than by relying on the model to refuse.

A reinforcement-learning stage (GRPO on isolated vulnerable environments) is the **planned** next phase. It is designed but **not implemented** — see [Implementation Status](#implementation-status) below.

> **Status:** Active research, incomplete. The 32B pipeline has one partial SFT checkpoint — about
> half a day of training on an incomplete corpus, stopped early — which is what the benchmark run
> below measures; the full-corpus run is pending compute, and the RL stage has not started. Model
> weights will be released under gated access on HuggingFace upon publication.

---

## Implementation Status

What is built and running, versus what is designed but not yet written. This table is the
authoritative claim list for the repository; anything described elsewhere in this README
inherits its status from here.

| Component | Status | Where |
|-----------|--------|-------|
| Agent runtime (ReAct loop, sessions, budget, audit log) | **Implemented** | `app/agent/loop.py`, `app/agent/main.py` |
| Audit log + evidence store in the **desktop app** | **Implemented** — every tool call recorded at a single chokepoint; broker-mediated calls also store full output in the encrypted evidence store | `app/agent/web/dev_server.py` |
| Debug trace (prompts, model output, RAG scores, compaction deltas) | **Implemented** — separate from the audit log; unredacted, on by default, `OXPECKER_DEBUG_TRACE=0` disables | `app/agent/web/debug_trace.py` |
| Trace reader (one correlated timeline per session) | **Implemented** | `app/agent/web/trace_cli.py` |
| Execution broker (policy, scope check, taint, kill switch, approval queue) | **Implemented**, and the desktop app's outward-facing tools route through it, including taint marking and operator approval through the UI's Approvals panel | `app/agent/broker/` |
| Scope allow + deny per engagement | **Implemented** — exact host and CIDR matching, deny evaluated before allow, cloud instance-metadata denied regardless of what an engagement lists | `app/agent/web/scope.py`, `app/agent/broker/scope_check.py` |
| Sandboxed execution (bubblewrap tiers + seccomp profile) | **Implemented** and wired into both runtimes, failing closed when the requested tier is unavailable rather than downgrading | `app/agent/sandbox/` |
| `wsl2` isolation tier for Windows hosts | **Implemented, not yet exercised on Windows** — the same bubblewrap profile inside the WSL2 guest, without seccomp (a BPF file descriptor cannot cross the `wsl.exe` boundary). Argv construction, path translation and every refusal path are unit-tested; a real sandboxed execution is not. The probe exec-verifies inside the guest at selection time, so an unverified host refuses rather than reporting a sandbox it does not have | `app/agent/sandbox/executor.py` |
| Evidence store (HMAC-chained), findings + SARIF export | **Implemented** | `app/agent/evidence/`, `app/agent/findings/` |
| Hypothesis graph, notebook, engagement/RoE store | **Implemented** | `app/agent/hypothesis_graph/`, `app/agent/notebook/`, `app/agent/engagement/` |
| Injection guard | **Implemented** in both runtimes — tool output is screened and wrapped as data before the model sees it | `app/agent/injection_guard.py` |
| Knowledge RAG retrieval layer | **Implemented** (index not bundled — gated) | `app/agent/knowledge_rag/` |
| Web UI + FastAPI server, Electron shell | **Implemented** | `app/agent/web/`, `app/electron/` |
| Four-layer eval harness (deterministic / model-tool / reasoning / milestone) | **Implemented** | `app/agent/eval/` |
| LoRA SFT training pipeline (DeepSpeed ZeRO-3) | **Implemented** | `training/train_sft.py` |
| AutoPenBench adapter + one committed benchmark run | **Implemented** | `evaluation/` |
| **GRPO reinforcement learning stage** | **Not implemented** — designed only | — |
| **Dense/milestone reward function for RL** | **Not implemented** — milestone counters are logged by the eval adapter but not wired to any reward | — |
| **Training-data decontamination** | **Not implemented** — 5 stages specified in `docs/TRAINING_DATA.md`, no code, no overlap statistics. This qualifies the benchmark result (see [Training Data](#training-data)) | — |
| **Safety red-team evaluation** | **Not run** — thresholds in `docs/EVALUATION.md` are targets, not results | — |
| **Ablations, seed repeats, forgetting + RAG evaluation** | **Not run** — all planned in `docs/EVALUATION.md` | — |

Runtime safety parity between the two runtimes, and the logging that exposed the gap, is
recorded in [docs/OBSERVABILITY_PLAN.md](docs/OBSERVABILITY_PLAN.md) — including the two pieces
deliberately left outstanding (a Windows isolation tier, and session taint marking paired with a
web approval endpoint).

Roughly 28K lines of Python and 60 test modules live under `app/agent/`. That directory is the
maintained agent runtime; see the note in [Repository Structure](#repository-structure) about the
top-level `agent/` snapshot.

Planned work on the agent's security-tool layer — which tools are adopted, why, and the design
rules they must follow — is recorded in [docs/TOOLING_ROADMAP.md](docs/TOOLING_ROADMAP.md).

---

## Desktop Application (`app/`)

The [`app/`](app/) directory contains the **runnable, self-hosted desktop version** of Oxpecker — a packaged Electron application that drives a **local model** (Qwen 4B via llama.cpp, CUDA) end-to-end on a single machine, with a built-in web UI, a live hypothesis graph, a notebook, a findings tracker, and a memory-mapped 547K-chunk RAG. It is the practical, installable counterpart to the research pipeline below: the training work produces the model; `app/` is where the agent is actually operated against authorized lab targets.

- **Backend** — FastAPI agent server (`app/agent/web/dev_server.py`): a destructive-command denylist, structured HTTP tooling, auto-compaction, and **per-session** hypothesis graph / notebook / findings. Its scope check is **nominal only** and it does **not** sandbox command execution or write an audit log — see the warning below.
- **Desktop shell** — Electron + electron-builder with GitHub auto-update (`app/electron/`); one-click installer, no manual dependency setup.
- **MCP servers** — a dev/debug MCP and a control MCP for driving the live agent (`app/.mcp.json`).
- **Install** — download the latest `Oxpecker-Setup-*.exe` from [Releases](../../releases), or run from source per [`app/README.md`](app/README.md).

> ### ⚠ The desktop app does not currently have the safety controls described below
>
> An audit of `dev_server.py` found that the app enforces a destructive-command denylist, but
> **does not** sandbox command execution (it calls `subprocess.run(..., shell=True)` on the
> host), **does not** write an audit log, and **does not** route tool calls through the broker.
> Its scope check is nominal: it fails open when the allowlist is empty, appends any URL found
> in an operator message to the allowlist automatically, and compares hosts by substring. The
> `isolation_tier` field reports `"bubblewrap"` while nothing reads it at execution time.
>
> The controls are real in the research runtime (`agent/`, via `agent/main.py`). Bringing the
> app to parity is the current work; the finding and the plan are in
> [docs/OBSERVABILITY_PLAN.md](docs/OBSERVABILITY_PLAN.md).
>
> **Run the desktop app on a disposable machine or VM until this lands.**

The intended design — which `agent/` implements — enforces safety architecturally: an allowlisted engagement scope the agent hard-refuses to step outside of, a destructive-command block, and sandboxed/isolated execution, so the agent only ever acts against targets the operator has explicitly authorized.

> **Configuration:** `app/electron/config.json` is a **template** with placeholder paths. Copy it to `app/electron/config.local.json` (gitignored, never shipped) and point it at your own `llama-server`, model, and RAG index.

---

## ⚠️ Responsible Use

Oxpecker is **dual-use, research/educational software**. Use it **only against systems you own or are explicitly authorized to test** — self-hosted labs (OWASP Juice Shop, DVWA), CTF / boot2root VMs (VulnHub, HTB), and local containers on `localhost` / private networks. **Never** point it at production systems, third-party services, or any host you are not authorized to test; unauthorized access is illegal in most jurisdictions.

- The agent is **designed to hard-enforce an allowlisted scope** and refuse out-of-scope targets. This holds in `agent/`; in the desktop app it currently does not — see the warning above. Note that an *empty* allowlist in the desktop app means unrestricted, not localhost-only.
- The **fine-tuned model and RAG knowledge base are gated** — released by request only via Hugging Face (institutional/identity verification), not bundled in this repository.
- See **[SECURITY.md](SECURITY.md)** for the full responsible-use policy and how to report a vulnerability in Oxpecker itself.

This software is provided for research and education **as-is, without warranty**. You are solely responsible for ensuring your use is lawful and authorized.

---

## Preliminary Results

Evaluated on [AutoPenBench](https://github.com/lucagioacchini/auto-pen-bench) (33 tasks across web, network, and privilege escalation categories):

| Model | Tasks Solved | Score | Run artifact |
|-------|-------------|-------|--------------|
| Qwen3-32B (base) | 0 / 33 | 0.0% | not committed |
| **Oxpecker (partial SFT, ~0.5 day, incomplete corpus)** | **8 / 33** | **24.2%** | [`run_20260912_230749.json`](evaluation/results/run_20260912_230749.json) |
| xOffense (GPT-4o, SOTA) | 24 / 33 | 72.7% | reported by its authors |

**Read these numbers with the following caveats.**

- **5 of the 33 tasks errored** before producing a verdict (harness/environment failures, not
  model failures). The committed run therefore contains 28 completed attempts, 8 of which
  captured the flag. Scoring them as 24.2% counts the 5 errors as failures, which is the
  conservative reading; on completed tasks alone it is 8/28. The raw per-task records,
  including the errors, are in the run artifact.
- **Only the Oxpecker row has a committed artifact.** The base-model 0/33 figure is from an
  earlier run whose result file is not in this repository, and the xOffense figure is taken
  from its authors' reporting, not reproduced here. The three rows are therefore not a
  controlled head-to-head.
- **Single run, no seeds, no confidence interval.** Treat 24.2% as a proof of concept that
  domain-specific fine-tuning moves the model off a zero baseline — not as a stable benchmark
  claim, and not as a ranking against xOffense.
- **The evaluated checkpoint is a partial run, not a finished model.** It is roughly half a day
  of LoRA SFT on the incomplete v2 corpus, stopped early rather than trained to convergence —
  loss was still decreasing with no plateau when it was halted. The headline reading of this
  table is therefore not "the method reaches 24.2%" but "half a day of SFT on incomplete data
  moved a 0% baseline to 24.2%, with the run never taken to completion."
- **No 32B training is in progress at the time of writing.** The full v4 corpus (123K examples,
  612 MB) has been assembled but has not been trained on; that run is pending compute. Any
  statement about what v4 does to performance would be a prediction, not a result.

---

## Related Work

| System | Base Model | Method | Data | Safety | AutoPenBench |
|--------|-----------|--------|------|--------|-------------|
| PentestGPT | GPT-4 | Prompting | — | None | Low |
| PentestAgent | GPT-4 | Multi-agent prompt | — | None | Moderate |
| VulnBot | GPT-4 | Multi-agent collab | — | None | Moderate |
| xOffense | Qwen3-32B | LoRA SFT | Undisclosed | None | 72.72% |
| **Oxpecker** | **Qwen3-32B** | **LoRA SFT** (RL planned) | **~123K rows** | **Broker + Sandbox** | **24.2%** (partial SFT, single run) |

Scores for the first three systems are as reported by their respective authors and were not
reproduced here. The Safety column is the one axis on which Oxpecker's contribution is
load-bearing: the broker, scope enforcement, and sandbox are implemented and tested, whereas
the comparison systems document no architectural enforcement layer.

See [docs/EVALUATION.md](docs/EVALUATION.md) for the detailed comparison and the evaluation
plan. Note that the safety thresholds in that document are **targets for work not yet run**,
not measured results.

---

## Architecture

```
┌─────────────────────────────────────────────┐
│                  Oxpecker                   │
├─────────────┬───────────────┬───────────────┤
│   Agent     │   Training    │  Evaluation   │
│   Loop      │   Pipeline    │  Framework    │
├─────────────┼───────────────┼───────────────┤
│ • ReAct     │ • LoRA SFT    │ • AutoPenBench│
│ • Broker /  │   (Qwen3-32B) │ • 4-layer     │
│   scope     │ • DeepSpeed   │   harness     │
│ • Sandbox   │   ZeRO-3      │               │
│ • Budget    │               │               │
│ • Injection ├───────────────┼───────────────┤
│   guard     │ ? GRPO RL     │ ? Safety eval │
│             │   (planned)   │   (planned)   │
└─────────────┴───────────────┴───────────────┘
   •  implemented      ?  planned, not written
```

### Agent

The agent uses a ReAct-style loop to plan and execute penetration testing steps:

- **Scope enforcement** — Rules of Engagement (RoE) define allowed targets, ports, and techniques. Every command is validated against the RoE before execution.
- **Budget control** — configurable step limits, time limits, and token budgets prevent runaway sessions.
- **Injection guard** — detects and blocks prompt injection attempts from target system output.
- **Audit logging** — every action, observation, and decision is logged for review.

### Training Pipeline

**Phase 1 — Supervised Fine-Tuning (SFT)**
- Base model: Qwen3-32B
- Method: LoRA (r=128, α=256) with DoRA, rsLoRA, and PiSSA initialization
- Target modules: q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj
- Data: ~123.4K curated examples across reconnaissance, exploitation, privilege escalation, and reporting (gated; not in this repository)
- Hardware: 1× H100 80GB with DeepSpeed ZeRO-3

**Phase 2 — Reinforcement Learning (GRPO) — PLANNED, NOT IMPLEMENTED**

No RL code exists in this repository yet. The design below is a specification for future work,
recorded here so the intended reward structure is reviewable; do not read it as a description of
something that has been trained or measured.

- Algorithm: Group Relative Policy Optimization
- Training environments: VulnHub VMs, OWASP Juice Shop, DVWA, custom Docker scenarios — to be
  kept disjoint from the evaluation benchmarks
- Intended reward: milestone-based (+0.2 host discovery, +0.3 initial access, +0.5 privilege
  escalation). The milestone counters the AutoPenBench adapter already logs
  (`total_cmd_milestones`, `total_stg_milestones`) are the intended starting point for turning
  this into a dense signal, but they are currently recorded for analysis only and are not
  wired to any reward function.
- Open prerequisite: a mechanically-verified milestone checker. Rewarding on the model's own
  account of what it achieved would be reward-hackable, so this stage is blocked on a verifier
  that confirms state changes (actual uid, actual file read, actual service reached) out of
  band rather than trusting the transcript.

### Safety Architecture

Safety is enforced architecturally, not by model-layer refusal:

1. **Scope/RoE enforcement** — hard boundary on allowed targets and operations *(implemented,
   with a committed policy-bypass test suite)*
2. **Sandboxed execution** — bubblewrap isolation tier with a seccomp profile; no network device
   exists inside the sandbox *(implemented, with a committed isolation test suite)*
3. **Injection detection** — monitors target output for prompt injection attempts, with
   quarantine *(implemented)*
4. **Gated weight release** — model weights require institutional affiliation for access
   *(policy, enforced at release time)*
5. **Red-team evaluation** — quantitative safety testing. **Not yet run.** The figures in
   `docs/EVALUATION.md` (≥99% out-of-scope blocking, ≥90% injection detection) are the
   acceptance targets this evaluation is being designed against, **not measured results.**

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full details on the Broker, sandbox, prompt architecture, and pipeline orchestrator.

### RAG Knowledge Base

The agent is augmented with a 547K-chunk retrieval knowledge base covering vulnerability intelligence, exploit techniques, and pentesting guides. The retrieval layer (`app/agent/knowledge_rag/`) is implemented and tested; **the built index itself is not in this repository** — it is gated alongside the model weights, and the builder scripts (`build_index.py`, `build_index_gpu.py`) are provided so the index can be reconstructed from its sources.

| Source | Chunks |
|--------|--------|
| NIST NVD | 247,199 |
| CyberStrike | 120,966 |
| Fenrir | 99,749 |
| ExploitDB | 46,505 |
| HackTricks + GTFOBins + Others | 32,699 |
| **Total** | **547,118** |

### Training Data

~123.4K curated examples across four categories: external security QA (52.7K), custom pentest QA (27K), code SFT (20.9K), and agentic multi-turn conversations (22.8K). Training uses a 4-phase curriculum (simple → complex) in epoch 1, then fully shuffled in epoch 2.

> **Decontamination is specified but not implemented, and this qualifies the benchmark number.**
> A 5-stage decontamination pipeline is designed (see below), but **no decontamination code
> exists in this repository and no overlap statistics have been produced.** Since the corpus
> draws on HackTheBox / TryHackMe / VulnHub writeups and AutoPenBench is built from comparable
> machines, the 8/33 result above is **not controlled for train/test contamination.** Treat it
> accordingly until that pass is run and reported.

See [docs/TRAINING_DATA.md](docs/TRAINING_DATA.md) for the dataset breakdown, curriculum design,
and the decontamination specification — including a 31-row discrepancy between the component
table there and the dataset card that has not been reconciled.

---

## Repository Structure

> **Which agent directory is the real one.** The maintained agent runtime is
> **[`app/agent/`](app/agent/)** (~28K lines, 55 test modules). The top-level `agent/` directory
> is an earlier, partial snapshot of it: it is kept for reference but **does not import on its
> own**, because its modules reference packages (`agent.evidence`, `agent.engagement`,
> `agent.loop_control`, `agent.prompts`, `agent.tools`) that exist only under `app/agent/`.
> Run and read `app/agent/`; the tree below marks the snapshot accordingly.

```
Oxpecker/
├── agent/                  # ⚠ partial snapshot — does NOT import standalone;
│   │                       #   use app/agent/ instead
│   ├── main.py            # Entry point
│   ├── loop.py            # ReAct agent loop
│   ├── config.py          # Configuration
│   ├── llama_client.py    # LLM client (llama.cpp backend)
│   ├── session.py         # Session management
│   ├── budget.py          # Budget and step control
│   ├── audit_log.py       # Action logging
│   └── injection_guard.py # Prompt injection detection
├── training/               # Training pipeline
│   ├── train_sft.py       # SFT training script (LoRA + DeepSpeed)
│   ├── eval_model.py      # Model evaluation
│   └── ds_config.json     # DeepSpeed configuration
├── evaluation/             # Benchmark evaluation
│   ├── localai_agent.py   # AutoPenBench agent adapter
│   └── results/           # Evaluation results
├── datasets/               # Training data
│   ├── README.md          # Dataset card
│   ├── sample_train.jsonl # 1,000 training examples
│   └── sample_eval.jsonl  # 100 evaluation examples
├── safety/                 # Safety components
│   ├── injection_guard.py # Injection detection module
│   └── roe.json           # Rules of Engagement template
├── docs/                   # Documentation
│   ├── ARCHITECTURE.md    # System architecture details
│   ├── TRAINING_DATA.md   # Dataset breakdown and curriculum
│   └── EVALUATION.md      # Benchmarks and evaluation plan
└── app/                    # ★ Maintained runtime + desktop app (Electron + FastAPI + local 4B)
    ├── agent/             #   the real agent runtime (~28K LOC, 55 test modules)
    │   ├── broker/        #   execution broker: policy, scope, taint, kill switch
    │   ├── sandbox/       #   bubblewrap tiers + seccomp profile
    │   ├── evidence/      #   HMAC-chained evidence store
    │   ├── findings/      #   findings model + SARIF export
    │   ├── hypothesis_graph/
    │   ├── knowledge_rag/ #   retrieval layer (index not bundled — gated)
    │   ├── eval/          #   four-layer evaluation harness
    │   └── web/           #   dev_server.py (agent backend) + single-file web UI
    ├── electron/          #   desktop shell + auto-update config
    └── ...                #   MCP servers, build assets, docs
```

---

## Quick Start

### Training

LoRA SFT on Qwen3-32B. `--train_data` and `--eval_data` are required; the base model is
downloaded from Hugging Face unless `--model_path` points at a local copy. Available presets are
`standard`, `aggressive` (default), `max`, and `stage2`.

```bash
# Smoke test against the bundled 1,000-example sample
python training/train_sft.py \
  --preset aggressive \
  --train_data datasets/sample_train.jsonl \
  --eval_data datasets/sample_eval.jsonl \
  --output_dir output/sft \
  --max_seq_len 4096
```

The bundled samples exist so the pipeline can be exercised end to end; they are not the 123K
training set, which is gated alongside the weights.

### Evaluation

Requires a working [auto-pen-bench](https://github.com/lucagioacchini/auto-pen-bench) setup and
a served model.

```bash
# A single task
python evaluation/localai_agent.py --level in-vitro --category access_control --vm 0

# The full in-vitro suite (this is what the committed run artifact came from)
python evaluation/localai_agent.py --level in-vitro --all --max-steps 40
```

### Agent

Run the maintained runtime under `app/`. Targets come from the engagement's Rules of Engagement
(`engagement/roe.json`), not from a command-line flag — this is deliberate, so scope cannot be
widened ad hoc per invocation. Bootstrap an engagement first, then start a session:

```bash
cd app

# Inspect / create the engagement whose RoE defines the allowed scope
python -m agent.engagement.cli --help

# Start a session: security tools on, kernel-isolated execution tier
python -m agent.main \
  --engagement-id lab-default \
  --security-tools \
  --isolation-tier bubblewrap
```

Run `python -m agent.main --help` for the full flag set (profiles, MCP tool mode, shared memory
service, audit verification, kill switch).

---

## Requirements

- Python 3.10+
- PyTorch 2.1+
- transformers, peft, trl, deepspeed
- 80 GB VRAM (H100 PCIe or equivalent) for training
- 24+ GB VRAM for inference (quantized)

---

## Citation

```bibtex
@misc{oxpecker2026,
  title={Oxpecker: A Broker-Mediated Agent Runtime and Domain-Adapted LLM for Automated Penetration Testing},
  author={laplacian-n},
  year={2026},
  howpublished={\url{https://github.com/laplacian-n/Oxpecker}}
}
```

---

## License

This project is licensed under the Apache License 2.0 — see [LICENSE](LICENSE) for details.

Model weights will be released under gated access (institutional affiliation required).

---

## Acknowledgments

- [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B) by Alibaba Cloud
- [AutoPenBench](https://github.com/lucagioacchini/auto-pen-bench) by Gioacchini et al.
- [AI-Pentest-Benchmark](https://github.com/YouMingYeh/AI-Pentest-Benchmark) by Yeh et al.
