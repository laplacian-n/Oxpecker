# โครงการวิจัย: ตัวแทนทดสอบความมั่นคงปลอดภัยไซเบอร์ที่ขับเคลื่อนด้วย AI แบบ Self-Hosted
# Research Project: Self-Hosted AI-Driven Cybersecurity Testing Agent

**เอกสารฉบับนี้:** เขียนเพื่อให้ Claude session ถัดไป (หรือผู้วิจัยคนใดก็ตาม) สามารถเข้าใจ
ทั้งหมดตั้งแต่เป้าหมาย สถาปัตยกรรม สิ่งที่ทำแล้ว สิ่งที่ยังค้าง และวิธีเริ่มทำงานต่อ
โดยไม่ต้อง re-derive จากศูนย์

**วันที่:** 2026-09-13
**ที่ตั้ง:** `/home/nicotine/pentestAI/localAI`
**Git branch:** `main`

---

## สารบัญ

1. [วัตถุประสงค์และขอบเขตงานวิจัย](#1-วัตถุประสงค์และขอบเขตงานวิจัย)
2. [ภูมิทัศน์งานวิจัยที่เกี่ยวข้อง](#2-ภูมิทัศน์งานวิจัยที่เกี่ยวข้อง)
3. [สถาปัตยกรรมระบบ](#3-สถาปัตยกรรมระบบ)
4. [โมเดลและโครงสร้าง Inference](#4-โมเดลและโครงสร้าง-inference)
5. [ข้อมูลฝึกสอน (SFT Corpus)](#5-ข้อมูลฝึกสอน-sft-corpus)
6. [ระบบ Agent — สถาปัตยกรรมแบบ Phased Pipeline](#6-ระบบ-agent--สถาปัตยกรรมแบบ-phased-pipeline)
7. [ระบบ Knowledge RAG](#7-ระบบ-knowledge-rag)
8. [กลไกความมั่นคงปลอดภัย (Security Controls)](#8-กลไกความมั่นคงปลอดภัย-security-controls)
9. [Web UI และ Operator Interface](#9-web-ui-และ-operator-interface)
10. [Benchmarking และ Evaluation](#10-benchmarking-และ-evaluation)
11. [สถานะปัจจุบัน — สิ่งที่เสร็จแล้ว](#11-สถานะปัจจุบัน--สิ่งที่เสร็จแล้ว)
12. [งานที่กำลังดำเนินการและยังค้าง](#12-งานที่กำลังดำเนินการและยังค้าง)
13. [วิธีเริ่มทำงานต่อ (Quick Start for Next Session)](#13-วิธีเริ่มทำงานต่อ)
14. [ข้อจำกัดของฮาร์ดแวร์ — Load-Bearing Facts](#14-ข้อจำกัดของฮาร์ดแวร์--load-bearing-facts)
15. [แผนผังไฟล์หลัก](#15-แผนผังไฟล์หลัก)
16. [เอกสารอ้างอิงภายในโปรเจกต์](#16-เอกสารอ้างอิงภายในโปรเจกต์)

---

## 1. วัตถุประสงค์และขอบเขตงานวิจัย

### 1.1 เป้าหมายหลัก

สร้าง **ตัวแทนทดสอบเจาะระบบแบบอัตโนมัติ (Autonomous Penetration Testing Agent)** ที่:

- **ทำงานบนเครื่องของผู้วิจัยเอง** (self-hosted) ไม่พึ่งพา cloud API — ทุกอย่างรันบน GPU
  ตัวเดียว, ข้อมูลไม่ออกเครื่อง, ควบคุมได้ทั้งหมด
- **มีเป้าหมายด้านความมั่นคงแห่งชาติ** — เครื่องมือนี้ออกแบบเพื่อใช้ในงานทดสอบเชิง
  ป้องกัน (defensive security testing), CTF, และงานประเมินความเสี่ยงภายในองค์กร
- **เอาชนะ xOffense** (arXiv 2509.13021) ซึ่งเป็น state-of-the-art ปัจจุบันบน
  AutoPenBench (72.72% task completion) โดยใช้ Qwen3-32B เป็น base model ตัวเดียวกัน
- **ใช้งานจริงได้** (production-ready) — ไม่ใช่แค่ proof-of-concept, แต่มี audit trail,
  scope enforcement, findings lifecycle, operator controls

### 1.2 ปรัชญาการออกแบบ

1. **โครงสร้างชดเชยข้อจำกัดของโมเดล** — Qwen3-32B ทำ ~40% บน BFCL multi-turn subset;
   แทนที่จะหวังว่าโมเดลจะเลือกถูก ระบบใช้ deterministic pipeline (phase state machine +
   scope enforcement + kill switch) ครอบโมเดลไว้
2. **Fail-closed ทุกจุด** — ไม่มี tool call ที่ผ่านได้โดยไม่ผ่าน broker → scope check →
   injection guard; ค่า default คือ deny, ไม่ใช่ allow
3. **Real verification over assumption** — ทุก feature ถูก verified ด้วย live model + real
   target (Juice Shop/DVWA containers), ไม่ใช่แค่ mock tests
4. **Evidence-first** — findings ต้องมี evidence chain; model ไม่สามารถ self-promote
   finding จาก `needs_validation` เป็น `confirmed` ได้ด้วยตัวเอง

### 1.3 บริบทด้านความมั่นคง

- เครื่องมือนี้ออกแบบสำหรับ **authorized penetration testing** เท่านั้น
- ทุก engagement ต้องมี **Rules of Engagement (RoE)** ที่ signed-off ก่อนเริ่ม
- scope enforcement ป้องกัน out-of-scope access ทั้ง IPv4/IPv6/DNS/redirect/rebinding
- audit log เป็น hash-chained + HMAC-addressed — tamper-evident

---

## 2. ภูมิทัศน์งานวิจัยที่เกี่ยวข้อง

### 2.1 คู่แข่งหลัก: xOffense (arXiv 2509.13021)

| Feature | xOffense | localAI (โปรเจกต์นี้) |
|---------|----------|----------------------|
| Base model | Qwen3-32B | **Qwen3-32B** (ตัวเดียว; Qwen3.6-35B-A3B ยกเลิกแล้ว) |
| Fine-tune | LoRA + DeepSpeed ZeRO-3 | LoRA r=32 alpha=64 (pending retrain) |
| RL/DPO | ไม่มี | Pipeline ready, รอ pairable data |
| SFT rows | ≥1,000 machines (ไม่เปิดเผยจำนวนแถว) | **35,422 rows** (v2.6) |
| Knowledge RAG | Vector DB (HackTricks, writeups) | nomic-embed v1.5 (**16,702 chunks**: GTFOBins + HackTricks + LOLBAS + PATT) |
| Agent arch | 5-component multi-agent + TCG planner | Phased pipeline + planner DAG + state tracking |
| Benchmark | AutoPenBench **72.72%** / 79.17% sub-task | **กำลังรัน** (base: ~30% expected pre-finetune) |

### 2.2 งานวิจัยอื่นที่ศึกษา

- **PentestGPT** — task tree decomposition (ถูก subsume โดย B2 TaskDAG)
- **AutoPenBench** (arXiv 2410.03225) — 33 Docker-based tasks, primary benchmark
- **AI-Pentest-Benchmark** (arXiv 2410.17141) — 13 VulnHub VMs, 152 tasks, secondary
- **DeepSeek-R1** (arXiv 2501.12948) — DPO/GRPO methodology สำหรับ D1
- **hackingBuddyGPT, RedAmon** — อ่านเป็น reference architecture

---

## 3. สถาปัตยกรรมระบบ

### 3.1 ภาพรวม (High-Level Architecture)

```
┌──────────────────────────────────────────────────────────────────────┐
│                        OPERATOR (Web UI / CLI)                       │
│  localhost:8765 — FastAPI + static HTML/JS, SSE streaming            │
└──────────────────┬───────────────────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────────────────┐
│                         AGENT LOOP (loop.py)                         │
│  Tool-calling loop ← system prompt (compiled, versioned, digested)  │
│  Dispatch: BROKER_MEDIATED → SECURITY_MCP → LOCAL_BOOKKEEPING       │
│  Context: budget.py (evict → summarize), steer channel, thinking    │
└──────┬──────────┬──────────┬──────────┬──────────┬──────────────────┘
       │          │          │          │          │
┌──────▼───┐ ┌───▼────┐ ┌───▼─────┐ ┌──▼───┐ ┌───▼──────────┐
│  BROKER  │ │SANDBOX │ │PIPELINE │ │ RAG  │ │  HYPOTHESIS  │
│ RoE/scope│ │bwrap + │ │ orkstr. │ │dense │ │   GRAPH      │
│ kill sw  │ │seccomp │ │executor │ │embed │ │  + notebook   │
│ taint    │ │direct  │ │profiles │ │16.7k │ │  + technique  │
│ cooldown │ │        │ │auto drv │ │chunks│ │  KB           │
└──────────┘ └────────┘ └─────────┘ └──────┘ └──────────────┘
       │                     │
┌──────▼─────────────────────▼───────────────────────────────────────┐
│                    SECURITY TOOLS (MCP server)                      │
│  http_recon | port_discovery | browser_fetch | knowledge_search     │
│  knowledge_fetch | osint_record | security_reference_search         │
└────────────────────────────────────────────────────────────────────┘
       │
┌──────▼─────────────────────────────────────────────────────────────┐
│                     INFERENCE (llama-server)                        │
│  GPU: Qwen3-32B-abliterated (-ngl 44, 16384 ctx, --jinja)         │
│  CPU: nomic-embed-text-v1.5 (port 8091, CUDA_VISIBLE_DEVICES="")  │
└────────────────────────────────────────────────────────────────────┘
       │
┌──────▼──────────────────────────────────────┐
│             LAB TARGETS (Docker)             │
│  Juice Shop (127.0.0.1:3000)                │
│  DVWA (127.0.0.1:3080)                      │
│  SearXNG (127.0.0.1:8888)                   │
└─────────────────────────────────────────────┘
```

### 3.2 Topology

- **ปัจจุบัน**: ทุกอย่างรันบนเครื่องเดียว (GPU machine) — inference, agent loop, tools, targets
- **ออกแบบรองรับ**: Topology B (Phase 2) — brain + memory บน GPU machine, body (tools) บน
  client machine ต่างเครื่อง; ยังไม่ถูก tested ข้ามเครื่องจริง

### 3.3 Data Flow — หนึ่ง Turn ของ Agent

1. User/operator ส่ง message → `AgentLoop._run_turn()`
2. `_system_message()` compile prompt (versioned, phase-aware, tool-card-aware)
3. `[CURRENT HYPOTHESES]` + `[NOTEBOOK]` digest injected เข้า context
4. ส่งไป `LlamaClient.chat_completions()` → llama-server
5. Model ตอบกลับด้วย tool call (e.g. `http_recon`)
6. `_dispatch()` routes:
   - **BROKER_MEDIATED_TOOLS** → `Broker.dispatch()` → scope check → rate limit → tool execution → injection scan → taint tracking → audit
   - **SECURITY_MCP_TOOLS** → security MCP server subprocess
   - **LOCAL_BOOKKEEPING_TOOLS** → direct dispatch (graph_*, note_*, record_finding)
7. Tool result truncated (`MAX_SINGLE_RESULT_TOKENS=1000`) → appended to messages
8. วนลูปจนกว่า model ตอบ text (ไม่ใช่ tool call) หรือ hit `MAX_ITERATIONS=25`

---

## 4. โมเดลและโครงสร้าง Inference

### 4.1 โมเดลหลัก

| Model | Use | Config | Performance |
|-------|-----|--------|-------------|
| **Qwen3-32B-abliterated** (i1-Q4_K_M) | Decision model (หลัก) | `-ngl 44 -fa on --ctx-size 16384 --parallel 1 --jinja --api-key-file` | ~3.1–4.0 tok/s |
| ~~Qwen3.6-35B-A3B~~ (MoE, Q3_K_M) | ~~Decision model (ทดลอง)~~ **ยกเลิก** | ~~MoE 35B total / 3B active~~ | ยกเลิกแล้ว — LoRA ไม่ compatible, focus Qwen3-32B ตัวเดียว |
| **nomic-embed-text-v1.5** (f16 GGUF) | Embedding สำหรับ Knowledge RAG | CPU-only port 8091, `CUDA_VISIBLE_DEVICES=""` | ~0.5s per query |

### 4.2 API Key

- llama-server auth: `agent/state/llama_api_key.txt`
- **สำคัญ**: ค่านี้ derive จาก `STATE_DIR` — ถ้า redirect `STATE_DIR` ต้อง copy key file ด้วย
  (เคยเจอ bug จริง: 401 silent failure)

### 4.3 Thinking Mode

- เปิดเฉพาะ phase `ANALYSIS` — ตัดสินใจจาก A/B experiment จริง (900s)
- ที่ ~3.1 tok/s, thinking mode บน **ทุก** turn จะ 3-8x latency → ไม่มีทาง finish task
- Autonomous judgment tasks **force `no_think`** — ถ้าไม่ทำ model จะเสีย budget ทั้งหมดไปกับ
  `reasoning_content` แล้ว hit `finish_reason: length` ก่อนจะ emit tool call (0 hypotheses ทุกรอบ)

---

## 5. ข้อมูลฝึกสอน (SFT Corpus)

### 5.1 สถานะ Corpus ล่าสุด (v2.9)

| Metric | v2.6 (เดิม) | **v2.9 (ล่าสุด)** |
|--------|-------------|-------------------|
| Total rows | 35,422 | **46,013** |
| Multi-turn | 3.5% | **30.9%** (14,211 rows) |
| Has `<think>` | ~50% | **100.0%** (46,012/46,013) |
| Tool-use format | 8.4% | 4.2% (1,939 rows) |
| Size | ~100 MB | **225.5 MB** |
| md5 | 318baa37 | **287a4fd3** |
| File | `train_qwen3_think_v25.jsonl` | `train_qwen3_think_v29.jsonl` |

**Path**: `datasets/stage3_v4/train_qwen3_think_v29.jsonl`

### 5.1.1 Version Chain

```
v2.5 (35,422) ─── merge_v27.py ──→ v2.7 (+A7 offensive coding, +A2 relaxed trajectories)
                                      │
                                merge_v28.py ──→ v2.8 (+A8-A13: access control, stuck detection,
                                      │           web exploit chains, tool-output decisions,
                                      │           post-exploit pipeline, working exploit scripts)
                                      │
                              fix3_think_backfill ──→ v28_think (backfill <think> tags)
                                      │
                                merge_v29.py ──→ v2.9 (+A14-A28 gap data, +A18 external datasets)
```

### 5.1.2 v2.9 Composition by Origin

| Origin | Rows | % | Description |
|--------|------|---|-------------|
| wrn (WhiteRabbitNeo) | 17,075 | 37.1% | Base offensive security knowledge |
| wrn_ch2 | 5,218 | 11.3% | WRN Chapter 2 |
| trendyol_cybersec | 3,689 | 8.0% | **NEW in v29** — external cybersec dataset |
| main (original SFT) | 3,459 | 7.5% | Original CoT corpus |
| wrn_code_cyber | 3,121 | 6.8% | WRN coding + cyber |
| wrn_ch1 | 2,846 | 6.2% | WRN Chapter 1 |
| toolcall_converted | 2,084 | 4.5% | Single-turn → tool-calling format |
| A1_writeup_section | 1,935 | 4.2% | HTB/THM writeup sections |
| A6_nuclei | 1,310 | 2.8% | CVE/Nuclei exploitation data |
| A4_oversample | 1,010 | 2.2% | Recon/enum phase rebalance |
| p23_aug | 840 | 1.8% | Augmentation pass |
| A6_exploitdb | 586 | 1.3% | ExploitDB data |
| A2_trajectory | 523 | 1.1% | Multi-turn tool-output trajectories |
| A1_writeup | 441 | 1.0% | Full writeups |
| refuse | 415 | 0.9% | Refusal/empty-command rows |
| knowledge | 387 | 0.8% | Knowledge-based rows |
| p17_aug | 318 | 0.7% | Earlier augmentation |
| A7_offensive_coding_* | 94 | 0.2% | Exploit dev, payload craft, reverse eng, etc. |
| cybersec_32k | 64 | 0.1% | **NEW in v29** — external |
| A14-A28 (gap fills) | 454 | 1.0% | **NEW in v29** — 15 specialized gap buckets |

### 5.1.3 New in v2.9 (vs v2.8): +15,379 rows

**A14-A28 specialized gap data** (454 rows total):

| Bucket | Rows | Topic |
|--------|------|-------|
| A14_windows_privesc | 50 | Windows privilege escalation |
| A15_credential_access | 25 | Credential harvesting/cracking |
| A16_lateral_movement | 25 | Lateral movement techniques |
| A17_ad_enumeration | 20 | Active Directory enumeration |
| A19_web_exploitation | 35 | Web application exploitation |
| A20_linux_privesc | 34 | Linux privilege escalation |
| A21_pivoting_tunneling | 25 | Pivoting and tunneling |
| A22_post_exploitation | 30 | Post-exploitation activities |
| A23_initial_access | 25 | Initial access vectors |
| A24_cloud_pentesting | 25 | Cloud environment pentesting |
| A25_container_k8s | 20 | Container/Kubernetes security |
| A26_binary_exploit | 25 | Binary exploitation |
| A27_wireless_physical | 20 | Wireless/physical security |
| A28_osint_recon | 20 | OSINT and reconnaissance |

**A18 external filtered** (~14,900 rows): Large external dataset batch (trendyol_cybersec + cybersec_32k), quality-filtered from 426 MB raw → 106 MB filtered → deduped into corpus

### 5.2 Data Pipeline

```
Write-ups (930 machines) ──parse_writeups.py──→ decision_points.jsonl (11,883 pts)
                                                       │
                              ┌─────────────────────────┤
                              ▼                         ▼
                     CoT rewrite              Multi-turn trajectory gen
                   (local Qwen3-32B)            (OpenRouter/OrcaRouter)
                              │                         │
                              ▼                         ▼
                      stage3_clean/              A2 trajectories
                    4,148 main + 2,430 refuse    155 + expanding
                              │                         │
                              └──────────┬──────────────┘
                                         ▼
                              merge scripts (merge_v25-v29.py)
                                         │
                                         ▼
                              Final corpus v2.6 (35,422 rows)
```

### 5.3 ข้อมูลใหม่ที่เพิ่มเข้ามา (A-series gaps from xOffense comparison)

| Gap | Status | Rows Added |
|-----|--------|------------|
| A1: HTB/THM diversity | DONE | +3,009 |
| A2: Tool-output trajectories | DONE + expanding (480 remaining) | +155 full + 2,594 tool-call converted |
| A3: HackTricks in RAG | DONE | 16,702 vectors |
| A4: Phase balance (recon/enum) | DONE | +1,094 |
| A6: CVE exploitation data | DONE | +9,309 (ratio-capped ~2,762) |
| A7: Offensive coding SFT | DONE | 94 rows in v2.9 corpus |
| A8-A13: Specialized skill gaps | DONE | 169 rows (A8 access control, A9 stuck detection, A10-A13) |
| A14-A28: Extended gap fills | DONE | 454 rows across 15 specialized buckets |
| A18: External datasets | DONE | ~14,900 rows (trendyol_cybersec + cybersec_32k) |

### 5.4 Training Config (proposed, ยัง pending train)

- **Method:** LoRA r=32, alpha=64, bf16
- **Target:** 1x A100 80GB (rent) หรือ 2x RTX 4090 + QLoRA
- **Framework:** axolotl or Unsloth
- **Epochs:** 2-3, cosine scheduler, assistant-only loss
- **Eval temp:** 0.5 (ตาม xOffense paper)
- **ตัดสินใจแล้ว (2026-09-13):** ใช้ Qwen3-32B เท่านั้น — Qwen3.6-35B-A3B ยกเลิก (LoRA ไม่ compatible, quality ต่ำกว่า)

---

## 6. ระบบ Agent — สถาปัตยกรรมแบบ Phased Pipeline

### 6.1 Phase State Machine

```
INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT
```

- Forward-only, version-checked transitions
- Wall-clock budget per phase (max 6h)
- `ANALYSIS` เป็น phase เดียวที่เปิด thinking mode
- "No finding" is a valid outcome — pipeline ไม่ require ≥1 vulnerability

### 6.2 สาม Autonomy Modes

| Mode | Behavior | ใช้เมื่อ |
|------|----------|---------|
| **assistant** (default) | Operator drives ทุก turn | Interactive testing |
| **autonomous** | `AutonomousDriver.run()` loops ทุก phase | Unattended full scan |
| **consult** | เหมือน autonomous แต่ pause ที่ phase boundary | Semi-supervised |

### 6.3 Pipeline Orchestrator + Executor

- `pipeline/orchestrator.py` — deterministic task planning, idempotent, incremental
- `pipeline/executor.py` — connects planned tasks → real broker-mediated tool calls
- `pipeline/autonomous_driver.py` — drives the whole pipeline unattended
- `pipeline/profiles.py` — Web/API and Network profiles (different RECON tasks)
- `pipeline/parallel.py` — bounded thread pool for independent recon tasks

### 6.4 Hypothesis Graph

ระบบ typed-DAG สำหรับ track hypotheses ตลอด engagement:

- **9 model-callable tools**: `graph_hypothesis_add`, `graph_set_active_path`,
  `update_hypothesis_status`, `graph_link`, etc.
- **Store** → Engine → Service → CLI → MCP tools (full stack)
- **Web UI panel**: Causal/Timeline/Focus views, drawer, compare, stats, operator write-path
- **Operator actions**: park/reopen/note (reversible, non-history mutations)

### 6.5 Working Notebook

Per-engagement lab notebook injected into model context every turn:

- `note_add` / `note_search` / `note_resolve` / `note_promote`
- `technique` notes overflow to global KB → recallable across engagements
- Auto-relevance via TF-IDF cosine similarity (ไม่ใช้ GPU)
- Operator notes pinned with high priority in digest

---

## 7. ระบบ Knowledge RAG

### 7.1 Two RAG Systems

| System | Scope | Embedding | Corpus Size |
|--------|-------|-----------|-------------|
| **Technique KB** (`notebook/kb_vectors.py`) | Per-engagement + cross-engagement | TF-IDF (no GPU) | Dozens-hundreds |
| **General Knowledge RAG** (`knowledge_rag/`) | Global reference | nomic-embed-text-v1.5 (dense, CPU) | **16,702 chunks** |

### 7.2 General Knowledge RAG Corpus

| Source | Chunks |
|--------|--------|
| GTFOBins | 2,125 |
| HackTricks | 13,024 |
| LOLBAS | 486 |
| PayloadsAllTheThings | 1,067 |

- Tool: `security_reference_search(query, source?)` — model-callable, gated by `use_security_tools`
- Store: flat L2-normalized numpy matrix + JSONL metadata, cosine similarity linear scan
- Embedding server: CPU-only llama-server port 8091 (`CUDA_VISIBLE_DEVICES=""` required —
  ถ้าไม่ set จะ OOM แม้จะใส่ `-ngl 0`)

---

## 8. กลไกความมั่นคงปลอดภัย (Security Controls)

### 8.1 Broker (`agent/broker/`)

**ทุก** target-touching tool call ผ่าน `Broker.dispatch()`:

1. **Scope check** — IPv4/IPv6/DNS/redirect/rebinding validation (15/15 bypass tests)
2. **Kill switch** — dispatch-time + mid-scan check
3. **Rate limiting** — per-session, per-action-class cooldown
4. **Injection quarantine** — verdict taxonomy (clean/suspicious/malicious/unknown),
   cross-turn taint tracking ที่ escalate เป็น human approval
5. **Audit** — every action logged with hash-chain integrity

### 8.2 Sandbox (`agent/sandbox/`)

- **Default: `bubblewrap`** (เปลี่ยนจาก `direct` แล้ว — ADR-0004)
- **Seccomp deny-list**: ptrace, mount, namespace manipulation, kernel modules
- **rlimits**: AS, CPU, FSIZE (fork-bomb containment ยังเป็น open gap — 2 approaches reverted)
- Filesystem/network/PID namespace isolation, 16/16 tests

### 8.3 Evidence Store (`agent/evidence/`)

- Fernet-encrypted, HMAC-SHA256-addressed content store
- Key versioning + rotation, deletion tombstones
- Content encrypted under old key version remains decryptable after rotation

### 8.4 Findings (`agent/findings/`)

- Expanded schema: confidence, CVSS, CWE/CVE, demonstrated_impact, observation_refs
- `reviewed_by`/`reviewed_at` — model **ไม่สามารถ** mark reviewed
- SARIF + Markdown/PDF output (รองรับ Unicode/Thai)
- Human review gate ก่อน delivery

---

## 9. Web UI และ Operator Interface

### 9.1 Stack

- **Backend**: FastAPI (`agent/web/server.py`, 743 lines)
- **Frontend**: Single static HTML/JS (`agent/web/static/index.html`, 3,185 lines)
- **No build step, no framework** — ทำงานเลยจาก `python3 -m agent.run_web_ui`
- **Port**: `127.0.0.1:8765` (loopback-only by default)
- **Auth**: Optional API key (`agent/state/web_ui_api_key.txt`)

### 9.2 Features

- Session lifecycle (create/list, SSE streaming, derived titles)
- 3 autonomy mode selector (assistant/autonomous/consult)
- Control pills: Security tools, Isolation tier, Engagement, Mode
- Tool-call chips with slide-out Computer panel (real ActionResponse JSON)
- Hypothesis Tree panel (MVP 0-4 complete, operator write-path)
- Working Notebook tab + Findings tab
- Live plan/todo panel (autonomous mode)
- Live thinking trace (reasoning_content streaming)
- Approvals drawer
- Injection-guard warning blocks

### 9.3 Testing

- `agent/web/test_server.py` — 14/14 (in-process TestClient)
- `agent/web/test_frontend.py` — 6/6 (real headless Chromium via Playwright)

---

## 10. Benchmarking และ Evaluation

### 10.1 Internal Eval Harness (4-Layer)

| Layer | What | Tests |
|-------|------|-------|
| L1: Deterministic | Policy/sandbox/evidence/injection suites | 4 suites, 64 checks |
| L2: Model+Tool (multi-seed) | Ambiguity handling, 5 tasks × 3 seeds | 15 trials |
| L3: Security Reasoning | Hallucination probes + LLM-judge | 5 tasks |
| L4: Milestones | Juice Shop + DVWA real tasks | 5 milestones |

### 10.2 External Benchmarks

| Benchmark | Status | Tasks |
|-----------|--------|-------|
| **AutoPenBench** | **กำลังรัน** (base model, no LoRA) | 33 Docker tasks |
| AI-Pentest-Benchmark | Configured (needs VirtualBox) | 13 VMs, 152 tasks |

### 10.3 B-Series Agent Improvements (implemented in benchmark adapter)

| Feature | Description |
|---------|-------------|
| B1: Phase-aware replanning | Replan after recon discovers services |
| B2: Planner DAG | Separate LLM planner call, structured task DAG |
| B3: Smart compression | 6 tool-type handlers (nmap, web, msf, etc.) |
| B4: Error recovery | 5 error classes + escalation after 4 consecutive |
| B5: Grey-box state | PentestState context injection every step |

---

## 11. สถานะปัจจุบัน — สิ่งที่เสร็จแล้ว

### 11.1 ระบบ Agent (agent/)

| Component | Files | Tests | Status |
|-----------|-------|-------|--------|
| Agent Loop | `loop.py` (827 lines) | 55 test files total | Production-ready |
| Broker + Policy | `broker/` (10 files) | 6 test files, all green | Production-ready |
| Sandbox | `sandbox/` (3 files) | 2 test files, 16/16 + 14/14 | Production-ready |
| Evidence | `evidence/` (2 files) | 2 test files, 29 tests | Production-ready |
| Engagement | `engagement/` (6 files) | 5 test files | Production-ready |
| Pipeline | `pipeline/` (5 files) | 4 test files | Production-ready |
| Hypothesis Graph | `hypothesis_graph/` (7 files) | 1 test file | Spec-complete (MVP 0-4) |
| Notebook | `notebook/` (7 files) | 3 test files | Spec-complete |
| Knowledge RAG | `knowledge_rag/` (7 files) | 4 test files | Production-ready |
| Internet | `internet/` (7 files) | 5 test files, 34/34 | Production-ready |
| Security Tools | `security_tools/` (3 files) | 1 test file, 8/8 | Production-ready |
| Browser | `browser/` (2 files) | 1 test file | Working (no bwrap isolation) |
| Web UI | `web/` (2+1 files) | 2 test files, 20/20 | Production-ready |
| Findings | `findings/` (4 files) | 1 test file, 14/14 | Production-ready |
| Skills | `skills/` (4 files) | 1 test file, 18/18 | Working |
| Recipes | `recipes/` (4 files) | 2 test files | Working |
| Eval Harness | `eval/` (5 files) | 1 test file | Working |
| Prompts | `prompts/` (compiler + 11 prompt files) | 1 test file, 17/17 | Production-ready |

### 11.2 ข้อมูลฝึกสอน

- **Corpus v2.9: 46,013 rows** ✓ (was v2.6: 35,422)
- Tool-call conversion: **2,084 rows** in corpus ✓
- CVE data: **1,896 rows** (nuclei+exploitdb) in corpus ✓
- Phase rebalance: **+1,010 rows** ✓
- A14-A28 gap fills: **454 rows** ✓
- A18 external datasets: **~14,900 rows** (trendyol+cybersec_32k) ✓
- Think tag coverage: **100.0%** ✓
- Knowledge RAG index: **16,702 chunks** ✓
- DPO collector: framework ready ✓

### 11.3 Infrastructure

- llama-server (GPU, port 8080) ✓
- nomic-embed (CPU, port 8091) ✓
- SearXNG (Docker, port 8888) ✓
- Juice Shop (Docker, port 3000) ✓
- DVWA (Docker, port 3080) ✓
- systemd units (web server, backup, knowledge-rag) ✓
- Pre-commit hook ✓

---

## 12. งานที่กำลังดำเนินการและยังค้าง

### 12.1 กำลังดำเนินการ (In Progress)

| Task | Detail | Priority |
|------|--------|----------|
| **C1: AutoPenBench benchmark** | Running base model (no LoRA), ~25/33 tasks done | CRITICAL |
| **A2-relaxed: Trajectory expansion** | 56/480 machines done, ~$0.07 spent | HIGH |
| **A7: Offensive coding SFT** | 121 prompts, generating | MEDIUM |

### 12.2 ยังค้าง — ลำดับความสำคัญ

| # | Task | Detail | Blocks |
|---|------|--------|--------|
| 1 | **LoRA training** | ต้อง retrain บน Qwen3.6 (เก่าไม่ compatible) หรือ Qwen3-32B ด้วย v2.6+ corpus | Benchmark comparison |
| 2 | **Evaluate C1 results** | วิเคราะห์ AutoPenBench baseline score | Training priority |
| 3 | **C3: 5x consistency** | Run each task 5 times, measure variance | Paper comparison |
| 4 | **D1: DPO training** | Need wins+losses pairs from benchmark runs | LoRA + benchmark |
| 5 | **C2: VirtualBox setup** | AI-Pentest-Benchmark ต้อง VMs | Cross-validation |
| 6 | **Fork-bomb containment** | 2 approaches reverted, 3rd candidate: direct cgroup v2 pids.max | Security hardening |
| 7 | **Session resume after restart** | Transcripts safe on disk, AgentLoop objects lost | UX |
| 8 | **Spec deferrals** (Hypothesis Graph) | Hot Surfaces panel (v1.5), Chat→node (v2), viewport virtualization | Low priority |

### 12.3 งานที่ตั้งใจเลื่อนออกไป (Intentionally Deferred)

- **Phase 7: Network Red-Team Expansion** — credential/post-exploit/pivot capabilities
- **Microvm isolation** (ADR-0002) — deferred pending exploit-grade code justification
- **Multi-agent/model-switching** — explicitly rejected in favor of deterministic pipeline
- **Auto-surfacing RAG into per-turn digest** — general knowledge RAG เป็น explicit-call-only
- **CVE/NVD corpus** in RAG — not yet built

---

## 13. วิธีเริ่มทำงานต่อ (Quick Start for Next Session)

### 13.1 Verify Machine State

```bash
# 1. Check services
ps aux | grep -E "llama-server|run_web_ui|memory_service" | grep -v grep
curl -s http://127.0.0.1:8080/health
docker ps --format "{{.Names}}: {{.Status}}"
nvidia-smi --query-gpu=memory.used,memory.total --format=csv

# 2. Run full test suite (ไม่ต้องใช้ live model)
cd /home/nicotine/pentestAI/localAI && source .venv/bin/activate
export PYTHONPATH=/home/nicotine/pentestAI/localAI
for f in $(find agent -name "test_*.py" ! -path "*/web/*"); do
  python3 -m "$(echo "$f" | sed 's#/#.#g; s#\.py$##')" > /tmp/t.out 2>&1 || { echo "FAIL: $f"; cat /tmp/t.out; }
done
echo done

# 3. Live-model tests (ต้องมี llama-server running, ไม่มี concurrent workload)
python3 -m agent.web.test_server
python3 -m agent.web.test_frontend
```

### 13.2 Key Files to Read First

1. **`doc/handoff.md`** — detailed session handoff (สถานะ ณ 2026-09-08)
2. **`doc/gap-analysis-vs-xoffense.md`** — เปรียบเทียบกับ xOffense, task tracking
3. **`docs/ROADMAP.md`** — แผน phase-by-phase
4. **`docs/STATUS.md`** — truthful status per milestone (ยาวมาก แต่ละเอียดที่สุด)
5. **`agent/config.py`** — ค่า constants ทั้งหมด, comments อธิบาย rationale
6. **`git log --oneline -30`** — commit messages เป็น incident reports ที่อ่านได้

### 13.3 Conventions

- **ภาษา**: User สื่อสารภาษาไทย; agent system prompt ตอบตามภาษา operator
- **Commit style**: ทุก commit เป็น "incident report" (พบอะไร, ทำไม, แก้ยังไง)
- **Test-then-commit**: ทุกงานต้อง test ก่อน commit, ไม่ batch รวม
- **Never fabricate progress**: `executor.py` / `autonomous_driver.py` ไม่ mark task "done"
  จนกว่าสถานะจะเปลี่ยนจริง
- **Live verify**: ทุก non-trivial change ต้อง test กับ real model + real target

### 13.4 สิ่งที่ต้องระวัง (Pitfalls)

1. **Tool schema ≠ dispatchable** — เพิ่ม schema ใน `tool_schemas` ไม่พอ, ต้องเพิ่มใน
   `_dispatch()` ด้วย (เจอ bug จริง **3 ครั้ง**)
2. **Default argument bound at import time** — `Path = config.X` as default parameter
   captures ค่าครั้งเดียว; patch config ใน test ไม่มีผล (เจอ **5+ ครั้ง**)
3. **Single-slot contention** — `--parallel 1` = 1 request at a time; 2 concurrent workloads
   ทำให้ timeout misleading
4. **Thinking-mode starvation** — autonomous tasks ที่ต้อง emit tool call **ต้อง**
   `force_no_think=True` ไม่งั้น model จะ burn budget ไปกับ reasoning แล้วไม่เหลือ room
5. **`reasoning_content` ≠ `content`** — llama-server แยก thinking ออกเป็น field ต่างหาก
6. **`CUDA_VISIBLE_DEVICES=""`** — ต้อง set จริงสำหรับ CPU-only server; `-ngl 0` ไม่พอ

---

## 14. ข้อจำกัดของฮาร์ดแวร์ — Load-Bearing Facts

| Constraint | Value | Impact |
|------------|-------|--------|
| Decode speed | ~3.1–4.0 tok/s (Qwen3-32B) | 1 turn อาจใช้ 10-15 นาที |
| Context window | 16,384 tokens | n_ctx สูงกว่านี้ OOM บน 32B dense |
| GPU VRAM free | ~58 MB (Qwen3-32B loaded) | ไม่มี room สำหรับ second model |
| Fixed overhead | ~7.8-8.4k tokens (system prompt + tools + digests) | เหลือ ~4.3-4.9k per turn |
| `MAX_SINGLE_RESULT_TOKENS` | 1,000 | ลดจาก 4,000 → 2,800 → 1,300 → 1,000 (ทุกค่าก่อนหน้า OOM จริง) |
| `REQUEST_TIMEOUT_S` | 300s (normal) / 560s (ANALYSIS thinking) | Tuned จาก real timeouts |

---

## 15. แผนผังไฟล์หลัก

```
localAI/
├── agent/                          # <<< CORE — ระบบ agent ทั้งหมด
│   ├── loop.py                     # Agent loop (827 lines) — หัวใจของระบบ
│   ├── main.py                     # CLI entry point
│   ├── config.py                   # All constants (env-overridable)
│   ├── llama_client.py             # llama-server API client
│   ├── session.py                  # Session persistence
│   ├── audit_log.py                # Hash-chained audit
│   ├── budget.py                   # Context budget management
│   ├── injection_guard.py          # Prompt injection detection
│   ├── security_mcp_server.py      # MCP server for security tools
│   ├── broker/                     # RoE, scope, kill switch, approval, taint
│   ├── sandbox/                    # bubblewrap + seccomp isolation
│   ├── evidence/                   # Encrypted evidence store
│   ├── engagement/                 # Engagement lifecycle + state store
│   ├── pipeline/                   # Pipeline orchestrator + executor + driver
│   ├── hypothesis_graph/           # Typed-DAG hypothesis tracking
│   ├── notebook/                   # Working notebook + technique KB
│   ├── knowledge_rag/              # General knowledge RAG (GTFOBins etc.)
│   ├── internet/                   # SearXNG search, fetch, OSINT
│   ├── security_tools/             # http_recon, port_discovery
│   ├── browser/                    # Playwright browser service
│   ├── findings/                   # Finding model, SARIF, Markdown/PDF
│   ├── skills/                     # Versioned, signed skill library
│   ├── recipes/                    # Reproducible retest recipes
│   ├── eval/                       # 4-layer evaluation harness
│   ├── prompts/                    # Compiled, versioned prompt registry
│   │   ├── phases/                 # Per-phase guidance (6 files)
│   │   └── tools/                  # Per-tool cards (5+ files)
│   ├── tools/                      # Basic tools (read/write/run_command)
│   ├── web/                        # FastAPI server + static frontend
│   │   ├── server.py
│   │   └── static/index.html
│   ├── loop_control/               # Steer channel
│   ├── oob/                        # Out-of-band catcher
│   ├── memory_service/             # Cross-device memory (Phase 2)
│   └── state/                      # Runtime state (sessions, audit, evidence, etc.)
│
├── datasets/                       # <<< DATA — training data + benchmarks
│   ├── qa/                         # QA scripts, corpus builds, reports
│   │   ├── DATASET_HANDOFF.md      # Training data detailed handoff
│   │   ├── gen_A*.py               # Data generation scripts
│   │   ├── merge_v*.py             # Corpus merge scripts
│   │   ├── train_lora.py           # LoRA training script
│   │   └── *.md                    # Step reports, plans, comparisons
│   ├── auto-pen-bench/             # AutoPenBench (33 Docker tasks)
│   ├── stage3_clean/               # Final clean SFT corpus
│   └── <source corpora>/           # Raw write-up sources
│
├── doc/                            # <<< DOCS — research + handoff
│   ├── handoff.md                  # Detailed session handoff
│   ├── gap-analysis-vs-xoffense.md # Gap analysis vs xOffense
│   ├── security-agent-research.md  # Original research document
│   ├── phase1-4.md                 # Phase design documents
│   └── fromGPTandHackerAI/        # External review documents
│
├── docs/                           # <<< SPECS — formal specifications
│   ├── ROADMAP.md                  # Phase-by-phase plan
│   ├── STATUS.md                   # Truthful implementation status
│   ├── hypothesis-graph-ui-spec.md # Hypothesis graph specification
│   ├── working-notebook-spec.md    # Notebook specification
│   ├── knowledge-rag-spec.md       # Knowledge RAG specification
│   └── adr/                        # Architecture Decision Records (7)
│
├── deploy/                         # systemd units, backup script
├── engagements/                    # Per-engagement runtime data
├── engagement/                     # Legacy single-engagement (Phase 3)
└── .venv/                          # Python virtual environment
```

---

## 16. เอกสารอ้างอิงภายในโปรเจกต์

| เอกสาร | เนื้อหา | เมื่อไรควรอ่าน |
|--------|---------|----------------|
| `doc/handoff.md` | Session handoff ละเอียดที่สุด | **อ่านก่อนเลย** — มี "Load-bearing facts" |
| `doc/gap-analysis-vs-xoffense.md` | เปรียบเทียบ xOffense, progress tracking | เมื่อทำ data/training/benchmark |
| `docs/ROADMAP.md` | แผน phase structure | เมื่อต้องตัดสินใจว่าทำอะไรก่อน |
| `docs/STATUS.md` | สถานะ per-milestone (527 lines) | เมื่อต้องรู้ว่าอะไรเสร็จจริง |
| `datasets/qa/DATASET_HANDOFF.md` | SFT dataset ละเอียด + training config | เมื่อทำ training |
| `docs/hypothesis-graph-ui-spec.md` | Hypothesis graph spec + MVP roadmap | เมื่อแก้ graph UI |
| `docs/working-notebook-spec.md` | Notebook + technique KB spec | เมื่อแก้ notebook |
| `docs/knowledge-rag-spec.md` | Knowledge RAG design | เมื่อแก้ RAG |
| `docs/adr/0001-0007` | Architecture Decision Records | เมื่อต้องเข้าใจ why |
| `doc/fromGPTandHackerAI/` | External reviews (6 files) | เมื่อต้อง context ทำไมออกแบบแบบนี้ |
| `datasets/qa/MODEL_COMPARISON_*.md` | Qwen3-32B vs Qwen3.6 comparison | เมื่อเลือก model |

---

## หมายเหตุสำหรับ Session ถัดไป

1. **อ่าน `doc/handoff.md` ก่อน** — มันเขียนมาเพื่อ Claude session ถัดไปโดยเฉพาะ
   มีรายละเอียดที่ document นี้ย่อไว้
2. **Run tests ก่อนแก้อะไร** — 55 test files, ควร green ทั้งหมด
3. **ดู task list** — C1 benchmark อาจเสร็จแล้ว, ต้อง evaluate results
4. **User สื่อสารภาษาไทย** — ตอบเป็นภาษาไทยได้เลย
5. **ระวัง hardware constraints** — 3.1 tok/s, 16k context, single-slot
   timeout ที่ดูเหมือน bug มักเป็นแค่ slow decode + contention

---

*Document generated 2026-09-13 for continuity across Claude sessions.
For questions or updates, see `git log --oneline` for the most recent
state — commit messages in this repo are designed to be read as documentation.*
