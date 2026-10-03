# Gap Analysis: localAI vs xOffense & Pentest AI Research

Date: 2026-09-12
Target: Beat xOffense AutoPenBench 72.72% task completion / 79.17% sub-task

## Baseline Comparison

| | xOffense | localAI (v2.6) |
|---|---------|---------------|
| Base model | Qwen3-32B | **Qwen3-32B** (Qwen3.6-35B-A3B ยกเลิก 2026-09-13) |
| Fine-tune | LoRA + DeepSpeed ZeRO-3 | LoRA r=32 alpha=64 bf16 (pending retrain) |
| RL/DPO | None | DPO pipeline ready, awaiting pairable data |
| SFT rows | Not disclosed (1,000+ machine writeups + WhiteRabbitNeo) | 35,422 (v2.6) |
| Tool-use ratio | Unknown | 8.4% tool-calling format |
| Multi-turn ratio | Unknown | 3.5% multi-turn trajectories |
| CoT format | `<think>` tags (empty for WhiteRabbitNeo) | `<think>` tags (50% real, 50% empty) |
| Data sources | THM + HTB + VulnHub + WhiteRabbitNeo + HuggingFace | VulnHub + HTB + THM + WRN + CVE + trajectories |
| Knowledge RAG | Vector DB (HackTricks, HackingArticles, writeups) | nomic-embed v1.5 (16,702: GTFOBins + HackTricks + LOLBAS + PATT) |
| Agent arch | 5-component multi-agent + TCG planner | B-series: planner DAG + state tracking + error recovery + compression |
| Benchmark | AutoPenBench 72.72% / AI-Pentest-Benchmark | AutoPenBench running (C1) / AI-Pentest-Benchmark configured (C2) |

## xOffense AutoPenBench Results

| Category | Task completion | Sub-task (1 exp) | Sub-task (5 exp) |
|----------|----------------|------------------|------------------|
| Overall | 72.72% (24/33) | 79.17% (251/317) | 60.94% (966/1585) |
| Access Control | 100% (5/5) | | |
| Web Security | 71.42% (5/7) | | |
| Network Security | 83.33% (5/6) | | |
| Cryptography | 75% (3/4) | | |
| Real-world CVE | 54.54% (6/11) | | |

Qwen3-32B base (no fine-tune): 30.30% task / 52.36% sub-task — fine-tuning gives 2.4x improvement.

---

## A. Data / Training Gaps

### A1. [CRITICAL] Data source diversity — DONE
- **Gap**: We use VulnHub-heavy (67%) data. xOffense aggregates 1,000+ machines from TryHackMe + HackTheBox + VulnHub.
- **Impact**: Model overfits to VulnHub patterns, fails on diverse benchmark environments.
- **Fix**: Scrape/acquire THM and HTB writeups, convert to SFT format.
- **Status**: DONE. 257 Kyuu-Ji HTB writeups + 191 hackingarticles.in writeups. 3,016 total rows (441 full + 2,575 per-section). 3,009 new after dedup into v2.5.

### A2. [CRITICAL] Tool-output interaction trajectories — DONE + EXPANDING
- **Gap**: Our data is single-turn Q&A. Real pentesting is: run command → read output → decide next.
- **Impact**: Model doesn't learn to parse tool outputs and chain decisions.
- **Fix**: Generate multi-turn trajectories from writeups: extract command→output→next_command sequences.
- **Status**: DONE (original). 194/194 machines processed, 155 passed quality filter. $0.185 total.
  - **A2-relaxed**: IN PROGRESS. Relaxed filter (2+ phases instead of recon+exploit/enum_web). 56/320 machines done, ~$0.07 spent. Running as PID 3215011.
  - **Tool-call conversion**: DONE. 6,936 single-turn rows converted to tool-calling format, 2,594 new after dedup into v2.6 corpus.

### A3. [HIGH] HackTricks + HackingArticles in Knowledge RAG — DONE
- **Status**: RAG rebuilt with 16,702 vectors (2,125 GTFOBins + 13,024 HackTricks + 486 LOLBAS + 1,067 PayloadsAllTheThings). nomic-embed-text-v1.5 CPU.

### A4. [MEDIUM] Phase balance in training data — DONE
- **Status**: DONE. 1,094 augmented recon/enum rows.

### A5. [MEDIUM] Neural embeddings for RAG — DONE
- **Status**: nomic-embed-text-v1.5 running on CPU port 8091.

### A6. [MEDIUM] Real-world CVE exploitation data — DONE
- **Status**: 9,309 rows from 3 sources. All 11 benchmark CVEs covered. Ratio-capped to ~2,762 in merge.

---

## B. Architecture / Agent System Gaps

### B1. [CRITICAL] Phase-aware replanning — DONE
- **Gap**: xOffense uses a DAG-based Task Coordination Graph with Planning Session + Task Session.
- **Fix**: Replan trigger after initial recon discovers services.
- **Status**: DONE. Implemented in `localai_agent.py`. Triggers when 2+ ports discovered and in enum/exploit phase. Feeds discovered state back into prompt for revised attack strategy.

### B2. [HIGH] Multi-agent separation / Planner DAG — DONE
- **Gap**: xOffense has 5 specialized components: Task Orchestrator, Knowledge Repository, Command Synthesizer, Action Executor, Information Aggregator.
- **Fix**: Separate planner from executor with structured task DAG.
- **Status**: DONE. Implemented:
  - `PLANNER_SYSTEM_PROMPT`: Dedicated planning-specific system prompt
  - `TaskDAG` class: Tracks structured phase DAG with dependencies, auto-advancement
  - `_generate_plan()`: Separate LLM call (lower temperature) generates JSON task DAG
  - DAG phases: recon → enum → exploit → privesc → post-exploit → loot
  - Auto-advances phases based on PentestState changes
  - DAG context block injected into system prompt alongside grey-box state
  - Falls back to simple prompt if planner call fails

### B3. [HIGH] Smart output compression — DONE
- **Gap**: xOffense uses MemAgent for outputs exceeding 8,000 chars.
- **Fix**: Tool-type-aware compression (nmap, web scans, msf, searchsploit, brute-force).
- **Status**: DONE. 6 tool-type handlers with keyword-based line filtering. Default head+tail truncation as fallback.

### B4. [HIGH] Structured error recovery — DONE
- **Gap**: xOffense has Algorithms 1-3 for error recovery with LLM regeneration.
- **Fix**: Error classification + specific recovery hints + escalation after 4 consecutive errors.
- **Status**: DONE. 5 error classes (connection_refused, permission_denied, not_found, timeout, auth_failed). Each provides targeted recovery guidance. Escalation prompt after 4 consecutive errors.

### B5. [MEDIUM] Grey-box phase prompting — DONE
- **Gap**: xOffense injects environment cues into agent reasoning context.
- **Fix**: PentestState class tracks ports, OS, web paths, credentials, access level, phase.
- **Status**: DONE. `PentestState` class extracts state from tool outputs, generates context block injected into system prompt each step.

---

## C. Evaluation / Benchmark Gaps

### C1. [CRITICAL] AutoPenBench setup — IN PROGRESS (benchmark running)
- **Gap**: Need baseline measurement.
- **Fix**: Deploy AutoPenBench 33 Docker tasks locally.
- **Status**: Running. Agent adapter + evaluator ready. Benchmark executing with Qwen3.6-35B-A3B (base, no LoRA, B-series agent improvements). Currently on network_security tasks (~25/33 done). PID 3195517.
- **Partial results**: access_control/vm0 FAILED (20 steps). More results pending.
- **Model**: Switched from Qwen3-32B to Qwen3.6-35B-A3B (MoE, 35B total / 3B active, 32 tok/s, 2GB VRAM + CPU MoE offload).

### C2. [MEDIUM] AI-Pentest-Benchmark — CONFIGURED
- **Gap**: Secondary benchmark (13 VulnHub machines, 152 tasks). xOffense uses both.
- **Fix**: Clone, configure, create agent adapter.
- **Status**: DONE (config). Repo cloned to `/mnt/data/AI-Pentest-Benchmark`. Configuration file and agent adapter created.
  - 13 VMs: 6 easy, 4 medium, 3 hard
  - 152 tasks across recon, exploitation, privesc, general techniques
  - Requires VirtualBox/KVM (not Docker) — heavier setup than AutoPenBench
  - Agent adapter: `localai_aipb_agent.py` (SSH-based execution)
  - **Blocker**: Need VirtualBox/KVM setup + VM downloads before running

### C3. [MEDIUM] 5-experiment consistency testing
- **Gap**: xOffense runs each task 5 times to measure consistency (60.94% vs 79.17% single-run).
- **Fix**: Run benchmarks multiple times, track variance.
- **Status**: TODO. Will implement after C1 baseline completes.

---

## D. From Other Research (Not xOffense-specific)

### D1. [MEDIUM] RL/DPO stage after SFT — PIPELINE READY
- **Source**: DeepSeek-R1
- **Gap**: No reinforcement learning after SFT. xOffense also doesn't have this.
- **Impact**: Reasoning quality ceiling without reward signal.
- **Fix**: Use AutoPenBench task completion as reward signal for DPO/GRPO.
- **Status**: DPO preference pair collector created (`datasets/dpo/collect_preference_pairs.py`).
  - Extracts (chosen, rejected) pairs from benchmark runs
  - chosen = trajectories that found the flag
  - rejected = trajectories that failed on the same task
  - Output format: DPO-ready JSONL for trl DPOTrainer
  - **Blocker**: Need multiple benchmark runs with both wins and losses to generate pairs
  - **Note**: Since xOffense also skips this, it's an opportunity to surpass them.

### D2. [LOW] PentestGPT task tree
- **Source**: PentestGPT
- **Gap**: Hierarchical task decomposition.
- **Impact**: Covered by B2 (TaskDAG planner) above.

---

## Current Corpus Stats (v2.6)

| Metric | Value |
|--------|-------|
| Total rows | 35,422 |
| Tool-use format | 8.4% |
| Multi-turn | 3.5% |
| md5 | 318baa37 |
| Sources | VulnHub, HTB, THM, WRN, CVE, trajectories, tool-call converted |

---

## Progress Summary

| Item | Status | Notes |
|------|--------|-------|
| A1 (data diversity) | DONE | +3,009 rows from HTB/THM |
| A2 (trajectories) | DONE + expanding | 155 done, 320 relaxed running |
| A3 (knowledge RAG) | DONE | 16,702 vectors |
| A4 (phase balance) | DONE | +1,094 recon/enum rows |
| A5 (neural embeddings) | DONE | nomic-embed v1.5 |
| A6 (CVE data) | DONE | 9,309 rows, 11 CVEs |
| B1 (replanning) | DONE | Phase-aware replan trigger |
| B2 (planner DAG) | DONE | Separate LLM planner + TaskDAG |
| B3 (compression) | DONE | 6 tool-type handlers |
| B4 (error recovery) | DONE | 5 error classes + escalation |
| B5 (grey-box state) | DONE | PentestState context injection |
| C1 (AutoPenBench) | RUNNING | ~25/33 tasks done |
| C2 (AI-Pentest-Bench) | CONFIGURED | Needs VirtualBox setup |
| C3 (5x consistency) | TODO | After C1 baseline |
| D1 (DPO) | PIPELINE READY | Needs pairable data |
| D2 (PentestGPT tree) | COVERED by B2 | |

**Model**: Qwen3-32B-abliterated (i1-Q4_K_M) — ตัดสินใจ 2026-09-13 ใช้ Qwen3-32B ตัวเดียว
**LoRA**: Pending train on Qwen3-32B with v2.6+ corpus (Qwen3.6 line cancelled).

---

## Priority Next Steps

1. **Wait for C1 benchmark** to complete → evaluate baseline score
2. **Wait for A2-relaxed** expansion (320 machines) → merge into corpus v2.7
3. **Train new LoRA** on Qwen3.6-35B-A3B with v2.6+ corpus
4. **Re-benchmark** with LoRA → compare against baseline
5. **Run C3** (5x consistency) on best config
6. **Collect DPO pairs** from multiple benchmark runs → train D1
7. **Setup C2** (VirtualBox + VM downloads) for cross-validation

---

## References
- xOffense: arXiv 2509.13021
- AutoPenBench: arXiv 2410.03225
- AI-Pentest-Benchmark: arXiv 2410.17141
- DeepSeek-R1: arXiv 2501.12948
- PentestGPT: https://github.com/GreyDGL/PentestGPT
