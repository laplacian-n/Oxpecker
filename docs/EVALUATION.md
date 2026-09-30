# Evaluation Plan

## Benchmarks

| Benchmark | Tasks | Description |
|-----------|-------|-------------|
| [AutoPenBench](https://github.com/lucagioacchini/auto-pen-bench) | 33 | Standard benchmark, 2 difficulty levels (in-vitro + real-world) |
| [AI-Pentest-Benchmark](https://github.com/YouMingYeh/AI-Pentest-Benchmark) | 13 machines · 152 subtasks | Real vulnerable machines |

## Related Work Comparison

| System | Base Model | Method | Data Size | Safety | Benchmark Score |
|--------|-----------|--------|-----------|--------|----------------|
| PentestGPT | GPT-4 | Prompting | — | None | Low |
| PentestAgent | GPT-4 | Multi-agent prompt | — | None | Moderate |
| VulnBot | GPT-4 | Multi-agent collab | — | None | Moderate |
| xOffense | Qwen3-32B | LoRA SFT | Undisclosed | None | 72.72% (AutoPenBench) |
| **Oxpecker** | **Qwen3-32B** | **LoRA SFT + RL** | **123K rows** | **Broker + Sandbox** | **TBD** |

## Ablation Studies

| Experiment | Purpose |
|-----------|---------|
| **SFT only vs SFT + RL** | Measures the contribution of RL to pentest performance |
| With / Without RAG | Measures whether knowledge retrieval improves or hinders task completion |
| With / Without Curriculum | Measures the effect of training data ordering on final performance |
| Multi-turn ratio (0%, 10%, 20%) | Measures whether more agentic data improves multi-step reasoning |

## Baseline Comparisons

- Qwen3-32B base (no fine-tuning) — established at 0% in preliminary results
- xOffense (LoRA fine-tuned Qwen3-32B) — current SOTA at 72.72%
- GPT-4o (proprietary baseline)
- PentestGPT / VulnBot (existing open systems)

## Statistical Rigor

With n = 33 tasks (AutoPenBench), comparisons are sensitive to noise. To address this:

1. Expand the task count to 200+ by including AI-Pentest-Benchmark (152 subtasks) and evaluating availability of Cybench, NYU CTF, and InterCode-CTF.
2. Run core configurations (SFT-only, SFT+RL) with 3 seeds; stretch configurations (ablations) with 1 seed.
3. Report all values as mean ± std with 95% CI where seeds > 1.
4. Test statistical significance using McNemar's test and bootstrap confidence intervals.

| Metric | Method | Seeds | Reported As |
|--------|--------|-------|-------------|
| Task completion rate | AutoPenBench + AI-Pentest | 3 | mean ± std, 95% CI |
| Sub-task completion | Per-phase breakdown | 3 | mean ± std |
| SFT vs SFT+RL | McNemar's test | 3 | p-value, effect size |
| General retention | MMLU, HumanEval, GSM8K | 1 | absolute score |

## Catastrophic Forgetting Evaluation

| Checkpoint | Metric | Purpose |
|-----------|--------|---------|
| Qwen3-32B base | MMLU · HumanEval · GSM8K | Baseline general capability |
| Post SFT | MMLU · HumanEval · GSM8K | Retention after fine-tuning |
| Post SFT + RL | MMLU · HumanEval · GSM8K | Whether RL causes additional forgetting |

A general-domain replay dataset (~10% of training data) is mixed into SFT training to mitigate catastrophic forgetting.

## RAG Evaluation

| Experiment | Metrics | Purpose |
|-----------|---------|---------|
| With / Without RAG | Completion + recall@5, recall@10, nDCG@10 | Whether RAG helps or hinders |
| RAG latency | p50 · p95 · p99 retrieval time | Overhead measurement |
| Distraction test | Completion with injected irrelevant chunks | Robustness to noisy retrieval |

## Safety Red-Team Evaluation

| Test | Method | Success Criteria |
|------|--------|-----------------|
| Scope violation | 100 access attempts outside allow-list | ≥99% blocked (+ 95% Wilson CI) |
| RoE bypass | Tool calls violating RoE but with valid format | ≥99% caught (+ 95% Wilson CI) |
| Injection evasion | 50 adversarial prompts (novel encodings) | Detection rate ≥90%, FPR ≤5% |
| Sandbox escape | ptrace / mount / network escape attempts | ≥99% blocked (+ 95% Wilson CI) |

## Skill-Level Framework

A qualitative reporting lens that maps AI agent performance to human pentester skill levels:

| Level | Description | Target |
|-------|-------------|--------|
| **Novice** | Knows tool names but cannot follow coherent methodology. Skips phases, uses tools incorrectly. | Base Qwen3-32B |
| **Junior** | Completes routine tasks with known procedures. Follows standard methodology for common vulnerability classes. | SFT model |
| **Senior** | Adapts strategy based on findings. Chains exploits across multiple stages. Recovers from failed attempts. | SFT + RL model |

## Comparison with xOffense

| Dimension | xOffense | Oxpecker |
|-----------|---------|----------|
| Training method | LoRA SFT only | LoRA SFT + RL |
| Training data | Undisclosed | 123K rows, fully disclosed |
| Safety mechanism | None | Broker + Sandbox + RoE |
| Knowledge augmentation | None reported | RAG (547K chunks) |
| Decontamination | Not reported | Multi-stage pipeline |
| Reproducibility | Not reproducible | Full pipeline released |
