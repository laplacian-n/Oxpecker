# Oxpecker

**Domain-adapted LLM for automated penetration testing via SFT + Reinforcement Learning**

Oxpecker is a self-hosted penetration testing agent built on [Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B), trained through a two-stage pipeline: supervised fine-tuning (LoRA) on 123K+ curated cybersecurity examples, followed by reinforcement learning (GRPO) on isolated vulnerable environments.

> **Status:** Active research — SFT training in progress. Model weights will be released under gated access on HuggingFace upon publication.

---

## Preliminary Results

Evaluated on [AutoPenBench](https://github.com/lucagioacchini/auto-pen-bench) (33 tasks across web, network, and privilege escalation categories):

| Model | Tasks Solved | Score |
|-------|-------------|-------|
| Qwen3-32B (base) | 0 / 33 | 0.0% |
| **Oxpecker (early SFT)** | **8 / 33** | **24.2%** |
| xOffense (GPT-4o, SOTA) | 24 / 33 | 72.7% |

The early fine-tuned model was trained on an older, smaller dataset (v2). Current training uses v4 data (123K examples, 612 MB) with improved coverage and quality. Loss curves show continued decrease with no plateau, indicating significant room for improvement.

Results are from a single run. The 24.2% score should be interpreted as a proof-of-concept demonstrating that domain-specific fine-tuning produces measurable capability gain from a zero baseline, not as a stable benchmark claim.

---

## Related Work

| System | Base Model | Method | Data | Safety | AutoPenBench |
|--------|-----------|--------|------|--------|-------------|
| PentestGPT | GPT-4 | Prompting | — | None | Low |
| PentestAgent | GPT-4 | Multi-agent prompt | — | None | Moderate |
| VulnBot | GPT-4 | Multi-agent collab | — | None | Moderate |
| xOffense | Qwen3-32B | LoRA SFT | Undisclosed | None | 72.72% |
| **Oxpecker** | **Qwen3-32B** | **LoRA SFT + RL** | **123K rows** | **Broker + Sandbox** | **TBD** |

See [docs/EVALUATION.md](docs/EVALUATION.md) for detailed comparison and evaluation plan.

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
│ • Scope     │   (Qwen3-32B) │ • AI-Pentest- │
│   enforce   │ • GRPO RL     │   Benchmark   │
│ • Budget    │ • DeepSpeed   │ • Safety eval │
│   control   │   ZeRO-3     │               │
│ • Injection │               │               │
│   guard     │               │               │
└─────────────┴───────────────┴───────────────┘
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
- Data: 123,416 curated examples across reconnaissance, exploitation, privilege escalation, and reporting
- Hardware: 1× H100 80GB with DeepSpeed ZeRO-3

**Phase 2 — Reinforcement Learning (GRPO)**
- Algorithm: Group Relative Policy Optimization
- Training environments: VulnHub VMs, OWASP Juice Shop, DVWA, custom Docker scenarios
- Reward: milestone-based (+0.2 host discovery, +0.3 initial access, +0.5 privilege escalation)
- Environments are completely disjoint from evaluation benchmarks

### Safety Architecture

Safety is enforced architecturally, not by model-layer refusal:

1. **Scope/RoE enforcement** — hard boundary on allowed targets and operations
2. **Sandboxed execution** — agent runs in isolated environments with no access to production systems
3. **Gated weight release** — model weights require institutional affiliation for access
4. **Injection detection** — monitors target output for prompt injection attempts
5. **Red-team evaluation** — quantitative safety testing (≥99% out-of-scope blocking, ≥90% injection detection)

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full details on the Broker, sandbox, prompt architecture, and pipeline orchestrator.

### RAG Knowledge Base

The agent is augmented with a 547K-chunk retrieval knowledge base covering vulnerability intelligence, exploit techniques, and pentesting guides:

| Source | Chunks |
|--------|--------|
| NIST NVD | 247,199 |
| CyberStrike | 120,966 |
| Fenrir | 99,749 |
| ExploitDB | 46,505 |
| HackTricks + GTFOBins + Others | 32,699 |
| **Total** | **547,118** |

### Training Data

123,447 curated examples across four categories: external security QA (52.7K), custom pentest QA (27K), code SFT (20.9K), and agentic multi-turn conversations (22.8K). Training uses a 4-phase curriculum (simple → complex) in epoch 1, then fully shuffled in epoch 2. A multi-stage decontamination pipeline ensures isolation from evaluation benchmarks.

See [docs/TRAINING_DATA.md](docs/TRAINING_DATA.md) for dataset breakdown, curriculum design, and decontamination details.

---

## Repository Structure

```
Oxpecker/
├── agent/                  # Agent runtime
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
└── docs/                   # Documentation
    ├── ARCHITECTURE.md    # System architecture details
    ├── TRAINING_DATA.md   # Dataset breakdown and curriculum
    └── EVALUATION.md      # Benchmarks and evaluation plan
```

---

## Quick Start

### Training

```bash
# LoRA SFT on Qwen3-32B (requires 1× H100 80GB)
python training/train_sft.py \
  --model_name Qwen/Qwen3-32B \
  --dataset_path datasets/sample_train.jsonl \
  --output_dir output/sft \
  --preset qwen3-32b-lora
```

### Evaluation

```bash
# Run AutoPenBench evaluation (requires auto-pen-bench setup)
# See: https://github.com/lucagioacchini/auto-pen-bench
python evaluation/localai_agent.py
```

### Agent (requires trained model)

```bash
python -m agent.main \
  --target 192.168.1.100 \
  --roe safety/roe.json \
  --max-steps 40
```

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
  title={Oxpecker: Domain-Adapted LLM for Automated Penetration Testing via SFT and Reinforcement Learning},
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
