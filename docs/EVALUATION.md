# Evaluation Plan

> **This document is a plan, not a results report.** With one exception, nothing below has been
> run. The only evaluation actually executed to date is a single AutoPenBench in-vitro run of a
> **partial** SFT checkpoint (roughly half a day of training on an incomplete corpus, stopped
> early) — 8 of 33 tasks solved, 5 tasks errored before producing a verdict —
> committed at [`evaluation/results/run_20260912_230749.json`](../evaluation/results/run_20260912_230749.json).
> Every table of metrics, threshold, seed count, and ablation in this document describes work
> that is **designed but not yet performed**. Numbers in "Success Criteria" and "Target" columns
> are acceptance targets being designed against, never measurements.
>
> See the [Implementation Status table](../README.md#implementation-status) for what exists in code.

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
| **Oxpecker** | **Qwen3-32B** | **LoRA SFT** (RL planned) | **~123K rows** | **Broker + Sandbox** | **24.2%** (early SFT, single run) |

Scores for the first four systems are as reported by their respective authors; none were
reproduced here, so this table is not a controlled comparison. The Safety column is the one axis
on which Oxpecker's implemented code differs from the comparison systems.

## Ablation Studies

None of these have been run.

| Experiment | Purpose | Blocked on |
|-----------|---------|------------|
| **SFT only vs SFT + RL** | Measures the contribution of RL to pentest performance | RL stage does not exist yet |
| With / Without RAG | Measures whether knowledge retrieval improves or hinders task completion | — |
| With / Without Curriculum | Measures the effect of training data ordering on final performance | — |
| Multi-turn ratio (0%, 10%, 20%) | Measures whether more agentic data improves multi-step reasoning | — |

## Baseline Comparisons

Intended baselines. Only the Oxpecker SFT checkpoint has a committed run artifact in this
repository; the figures below are either from earlier runs whose result files were not kept, or
from other authors' papers.

- Qwen3-32B base (no fine-tuning) — measured at 0/33 in an earlier run; **result file not
  committed**, so this figure is currently unverifiable from the repository
- xOffense (LoRA fine-tuned Qwen3-32B) — 72.72% **as reported by its authors**, not reproduced here
- GPT-4o (proprietary baseline) — not run
- PentestGPT / VulnBot (existing open systems) — not run

## Statistical Rigor

**Not yet applied.** The one committed run is a single seed with no confidence interval, which is
exactly the weakness this section is meant to fix. With n = 33 tasks (AutoPenBench), comparisons
are sensitive to noise. The plan to address it:

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

**Not run.** No general-capability benchmark has been measured on any checkpoint, so the extent
of forgetting after SFT is currently unknown.

| Checkpoint | Metric | Purpose |
|-----------|--------|---------|
| Qwen3-32B base | MMLU · HumanEval · GSM8K | Baseline general capability |
| Post SFT | MMLU · HumanEval · GSM8K | Retention after fine-tuning |
| Post SFT + RL *(RL not implemented)* | MMLU · HumanEval · GSM8K | Whether RL causes additional forgetting |

A general-domain replay dataset (~10% of training data) is mixed into SFT training to mitigate
catastrophic forgetting — `train_sft.py` supports this via `--replay_data`, so the mitigation
exists in code even though its effect has not been measured.

## RAG Evaluation

| Experiment | Metrics | Purpose |
|-----------|---------|---------|
| With / Without RAG | Completion + recall@5, recall@10, nDCG@10 | Whether RAG helps or hinders |
| RAG latency | p50 · p95 · p99 retrieval time | Overhead measurement |
| Distraction test | Completion with injected irrelevant chunks | Robustness to noisy retrieval |

## Safety Red-Team Evaluation

**Not yet run.** The percentages below are the acceptance criteria this evaluation is being
designed against — they are **not measured detection or blocking rates**, and must not be quoted
as results. What does exist today is the committed deterministic test suite the broker and
sandbox ship with (`app/agent/broker/test_policy_bypass.py`,
`app/agent/sandbox/test_isolation.py`, `app/agent/broker/test_injection_quarantine.py`), which is
functional regression testing, not an adversarial red-team measurement.

| Test | Method | Acceptance Criteria (target, not result) |
|------|--------|-----------------|
| Scope violation | 100 access attempts outside allow-list | ≥99% blocked (+ 95% Wilson CI) |
| RoE bypass | Tool calls violating RoE but with valid format | ≥99% caught (+ 95% Wilson CI) |
| Injection evasion | 50 adversarial prompts (novel encodings) | Detection rate ≥90%, FPR ≤5% |
| Sandbox escape | ptrace / mount / network escape attempts | ≥99% blocked (+ 95% Wilson CI) |

## Skill-Level Framework

A qualitative reporting lens that maps AI agent performance to human pentester skill levels. The
right-hand column is the **aspiration** for each checkpoint, not an assessed placement — no
checkpoint has been graded against this framework, and the rubric itself is subjective.

| Level | Description | Intended for |
|-------|-------------|--------------|
| **Novice** | Knows tool names but cannot follow coherent methodology. Skips phases, uses tools incorrectly. | Base Qwen3-32B |
| **Junior** | Completes routine tasks with known procedures. Follows standard methodology for common vulnerability classes. | SFT model (aspiration; 8/33 on AutoPenBench does not yet demonstrate this) |
| **Senior** | Adapts strategy based on findings. Chains exploits across multiple stages. Recovers from failed attempts. | SFT + RL model (RL not implemented) |

## Comparison with xOffense

Stated honestly, including where Oxpecker does *not* currently have the advantage. xOffense
scores 72.72% on AutoPenBench against Oxpecker's 24.2%; on the benchmark itself, xOffense is
substantially ahead, and the rows below are differences in approach and openness, not evidence
of better task performance.

| Dimension | xOffense | Oxpecker |
|-----------|---------|----------|
| AutoPenBench score | 72.72% | 24.2% (early checkpoint, single run) |
| Training method | LoRA SFT only | LoRA SFT; RL designed but **not implemented** |
| Training data | Undisclosed | ~123K rows, **composition** disclosed (see TRAINING_DATA.md); the data itself is gated, not yet released |
| Safety mechanism | None | Broker + Sandbox + RoE — **implemented and test-covered** |
| Knowledge augmentation | None reported | RAG retrieval layer implemented; 547K-chunk index gated, not bundled |
| Decontamination | Not reported | 5-stage pipeline **specified** in TRAINING_DATA.md; **no implementation in this repository** |
| Reproducibility | Not reproducible | Agent runtime, training script and eval adapter are released; weights, full dataset and RAG index are gated, so the pipeline is **not end-to-end reproducible by a third party today** |
