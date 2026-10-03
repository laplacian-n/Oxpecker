# PentestAI: A Self-Hosted Agentic Penetration Testing System with Domain-Adapted Large Language Models

## 1. Introduction

การทดสอบเจาะระบบ (Penetration Testing) เป็นกระบวนการสำคัญในการประเมินความปลอดภัยขององค์กร แต่ต้องพึ่งพาผู้เชี่ยวชาญที่มีจำนวนจำกัดและค่าจ้างสูง ส่งผลให้องค์กรขนาดกลาง-เล็กจำนวนมากไม่สามารถเข้าถึงบริการ Pentest ที่มีคุณภาพได้ โครงการนี้มุ่งพัฒนา **AI Penetration Testing Agent แบบ Self-Hosted** ที่สามารถดำเนินการทดสอบเจาะระบบได้อย่างอัตโนมัติในบทบาท Red Team โดยทำงานได้ครบวงจรตั้งแต่ Reconnaissance, Vulnerability Assessment, Exploitation ไปจนถึง Post-Exploitation และ Reporting

ระบบถูกออกแบบให้ทำงานบนโครงสร้างพื้นฐานภายในองค์กร (On-Premises) ทั้งหมด โดยไม่ต้องส่งข้อมูลใดๆ ออกสู่ cloud service ภายนอก ทำให้เหมาะสำหรับองค์กรที่มีข้อกำหนดด้านความลับของข้อมูลสูง ถือเป็นจุดแตกต่างสำคัญจากระบบที่อิงกับ proprietary API อย่าง GPT-4o หรือ Claude

## 2. Related Work

งานวิจัยด้าน LLM-driven Penetration Testing มีพัฒนาการอย่างรวดเร็วในช่วง 2024-2025:

| System | Base Model | Method | AutoPenBench | Architecture |
|--------|-----------|--------|-------------|-------------|
| PentestGPT (Deng et al., 2024) | GPT-4 / Llama3.1-405B | Prompting | 9.09% | Single-agent, human-in-the-loop |
| VulnBot (2025) | Llama3.1-405B | Multi-agent prompting | 30.30% | Multi-agent, task coordination |
| **xOffense** (Luong et al., Sep 2025) | **Qwen3-32B** | **LoRA fine-tuning** | **72.72%** | Multi-agent, RAG-enhanced |
| GPT-4o (baseline) | GPT-4o | Prompting | 21.21% | Single-agent |
| Qwen3-32B (base, no fine-tune) | Qwen3-32B | Prompting | 30.30% | — |

ผลจาก xOffense (arXiv:2509.13021) แสดงให้เห็นว่า Domain Adaptation ผ่าน Fine-tuning มีผลอย่างมากต่อประสิทธิภาพ — Qwen3-32B ที่ fine-tune ด้วย LoRA ให้ผลดีกว่าโมเดลขนาดใหญ่กว่า 12 เท่า (Llama3.1-405B, 30.30%) และ proprietary model อย่าง GPT-4o (21.21%) อย่างมีนัยสำคัญ

อย่างไรก็ตาม xOffense ยังมีข้อจำกัดที่โครงการนี้ต้องการแก้ไข:
1. ใช้เพียง **LoRA** (ปรับ <1% ของพารามิเตอร์) — ยังไม่มีการศึกษาผลของ Full SFT ในโดเมนนี้
2. ไม่เปิดเผยขนาดและวิธีการสร้างชุดข้อมูลฝึกสอนอย่างละเอียด
3. ระบบ Agent Framework ไม่มี Safety mechanism ที่ครบถ้วน (Sandbox, Injection Guard, Audit Trail)

## 3. System Architecture

### 3.1 Overview

PentestAI ประกอบด้วย 3 ส่วนหลัก:

```
┌─────────────────────────────────────────────────────────────┐
│                    PentestAI Agent System                     │
├─────────────────────────────────────────────────────────────┤
│  ┌───────────┐   ┌──────────┐   ┌──────────────────────┐   │
│  │ Fine-tuned │   │ Prompt   │   │ Pipeline             │   │
│  │ Qwen3-32B │◄──│ Registry │   │ Orchestrator         │   │
│  │ (SFT)     │   │ (6-layer)│   │ (6-phase state       │   │
│  └─────┬─────┘   └──────────┘   │  machine)            │   │
│        │                         └──────────┬───────────┘   │
│        ▼                                    │               │
│  ┌───────────┐   ┌──────────┐   ┌──────────▼───────────┐   │
│  │ Tool-Call  │──►│ Broker   │──►│ Security Tools       │   │
│  │ Parser     │   │ (RoE,   │   │ • http_recon (HTTPS) │   │
│  │            │   │  Scope,  │   │ • port_discovery     │   │
│  │            │   │  Taint)  │   │ • browser_fetch      │   │
│  └────────────┘   └──────────┘   │ • knowledge_search   │   │
│                                  │ • knowledge_fetch    │   │
│  ┌───────────┐   ┌──────────┐   └──────────────────────┘   │
│  │ RAG       │   │ Sandbox  │                               │
│  │ Knowledge │   │ (bwrap + │   ┌──────────────────────┐   │
│  │ Base      │   │ seccomp) │   │ Evidence Store       │   │
│  │ (547K     │   └──────────┘   │ (encrypted, HMAC,    │   │
│  │  chunks)  │                  │  key rotation)       │   │
│  └───────────┘                  └──────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Agent Loop & Tool Calling

โมเดลสื่อสารกับ Tools ผ่าน structured tool-call format:

```
<think>
Target มี Apache 2.4.49 running บน port 443 ซึ่งเป็นเวอร์ชันที่มีช่องโหว่ CVE-2021-41773
(path traversal) ควรทดสอบด้วย curl request
</think>

ทดสอบ path traversal บน Apache 2.4.49:

<tool_call>{"name": "run_command", "arguments": {"argv": ["curl", "-k", "--path-as-is", "https://10.10.10.42/cgi-bin/.%2e/.%2e/.%2e/.%2e/etc/passwd"]}}</tool_call>
```

ทุก response ของโมเดลถูกออกแบบให้ประกอบด้วย:
- **`<think>` block** — Chain-of-Thought reasoning แสดงกระบวนการวิเคราะห์ก่อนตัดสินใจ
- **Explanation text** — สรุปสิ่งที่จะทำให้ operator เข้าใจ
- **`<tool_call>` block** — คำสั่ง tool ในรูปแบบ JSON ที่ validated ก่อนส่งเข้า sandbox

### 3.3 Broker & Safety Layer

ทุก tool call ต้องผ่าน **Broker** ซึ่งทำหน้าที่:
- **RoE Enforcement** — ตรวจสอบว่า action อยู่ใน Rules of Engagement ที่ได้รับอนุญาต
- **Scope Checking** — ตรวจสอบ target อยู่ใน scope (allow/deny lists)
- **Injection Guard** — สแกน tool output เพื่อตรวจจับ prompt injection (base64/homoglyph/zero-width) พร้อม cross-turn taint tracking
- **Rate Limiting** — จำกัดความถี่ของ action แต่ละประเภท
- **Approval Queue** — action ที่มีความเสี่ยงสูงต้องได้รับ human approval

### 3.4 Sandbox Isolation

คำสั่งทุกอันถูก execute ภายใน **bubblewrap sandbox** (default) ที่มี:
- Filesystem isolation (unshare-all, bind-mount เฉพาะ workspace)
- Network namespace isolation
- PID namespace isolation
- seccomp deny-list (ptrace, mount, kernel module loading)
- Resource limits (RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE)

### 3.5 RAG Knowledge Base

ระบบ Retrieval-Augmented Generation สำหรับเสริมความรู้ด้าน Offensive Security ขณะทำงาน:

**Embedding Pipeline:**
- Model: nomic-embed-text-v1.5 (137M params, 768 dimensions)
- Prefix: "search_document:" สำหรับ indexing, "search_query:" สำหรับ retrieval
- Index: numpy flat vector, L2-normalized, cosine similarity via dot product

**Knowledge Sources (547,118 chunks):**

| Source | Chunks | Description |
|--------|--------|-------------|
| NIST NVD | 247,199 | Vulnerability descriptions จาก National Vulnerability Database |
| CyberStrike | 120,966 | Cybersecurity instruction-response pairs |
| Fenrir | 99,749 | Penetration testing knowledge QA |
| ExploitDB | 46,505 | Exploit descriptions and techniques |
| HackTricks | 13,044 | Practical pentesting guides (header-bounded markdown sections) |
| SFT Knowledge | 12,572 | Curated security QA from training pipeline |
| GTFOBins | 2,125 | Unix binary exploitation techniques (structured YAML) |
| Atomic Red Team | 1,862 | MITRE ATT&CK technique implementations |
| PayloadsAllTheThings | 1,067 | Payload reference and bypass techniques |
| Others | 2,029 | LOLBAS, LOLDrivers, HijackLibs, WADComs, cheatsheets |

**Parsing Methodology:**
- GTFOBins/LOLBAS: Structured YAML — หนึ่ง chunk ต่อ (binary, function, context) เพื่อ semantic completeness
- HackTricks/PayloadsAllTheThings: Header-boundary markdown splitting (`##`/`###`) พร้อม breadcrumb path
- NIST NVD: System prompt stripping (ตัด instruction prefix ก่อน "related documents." marker) + 2,800-char cap
- CyberStrike/Fenrir/ExploitDB: Messages format extraction พร้อม quality filters (min length, dedup by MD5)

### 3.6 Pipeline Orchestrator

ระบบ 6-phase state machine สำหรับจัดการ engagement:

```
INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT
```

- **Idempotent task planning** — สร้าง tasks ตาม state ปัจจุบัน ไม่สร้างซ้ำ
- **Budget-driven transition** — ไม่มีเงื่อนไข "ต้องพบช่องโหว่" — phase สามารถเสร็จสิ้นด้วย zero findings
- **Two pipeline profiles** — Web/API และ Network แชร์ state machine เดียวกัน ต่างเฉพาะ recon tasks

## 4. Training Data Construction

### 4.1 Dataset Overview (v3.2)

| Component | Train | Eval | Total |
|-----------|-------|------|-------|
| Pentest QA (custom) | ~39,846 | ~2,097 | 41,943 |
| External Security QA | ~39,900 | ~2,100 | 42,000 |
| Code SFT | ~20,890 | ~1,100 | 21,990 |
| Agentic/Multi-turn | ~22,811 | ~1,201 | 24,012 |
| **Total** | **123,447** | **6,498** | **129,945** |

### 4.2 Custom Pentest QA Data

สร้างจาก writeups ของ HackTheBox, TryHackMe, VulnHub ที่ถูกแปลงเป็น instruction-response pairs:
- แต่ละ QA pair ครอบคลุม vulnerability ที่เฉพาะเจาะจง พร้อม exploit procedure
- ทุก response ใช้ `<think>` tags สำหรับ Chain-of-Thought reasoning
- Tool calls ในรูปแบบ `<tool_call>{"name": "run_command", "arguments": {"argv": [...]}}</tool_call>`

### 4.3 External Security Knowledge

เพื่อเสริมความรู้ด้าน cybersecurity ทั่วไปให้โมเดล ผสมผสานจาก 4 แหล่ง:

| Dataset | Rows Used | Format | Content |
|---------|----------|--------|---------|
| CyberStrike (HuggingFace) | ~30K | Messages (multi-turn) | Cybersecurity instruction-response |
| Fenrir (HuggingFace) | ~25K | System/User/Assistant | Pentesting knowledge |
| ExploitDB (HuggingFace) | ~15K | Input/Output | Exploit descriptions |
| NIST NVD (HuggingFace) | ~10K | Text chunks | Vulnerability intelligence |

Quality Filters:
- Minimum response length (skip overly brief responses)
- Deduplication by MD5 hash of first 500 characters
- Skip responses containing "I cannot" / "I'm unable" refusals
- Text capped at context limit for consistency

### 4.4 Code SFT Data

เพื่อให้โมเดลเขียน exploit code, scripts และ automated tools ได้:
- **Magicoder** — OSS-Instruct และ Evol-Instruct code generation
- **CodeFeedback** — Multi-turn code debugging and refinement
- **Glaive Code** — Diverse code instruction-response pairs
- **Self OSS Instruct** — Self-generated programming exercises

### 4.5 Multi-Turn Conversation Generation (Core Contribution)

Multi-turn data เป็นส่วนสำคัญที่ทำให้โมเดลทำงานเป็น agent ที่ต่อเนื่องได้ ไม่ใช่แค่ตอบคำถามเดี่ยว เราสร้างข้อมูล 8,887 บทสนทนาแบบ multi-turn จำลองสถานการณ์ penetration testing จริง 7 ประเภท:

**Scenario Types:**

| Type | Model | Description |
|------|-------|-------------|
| Full Pentest | Qwen3-32B | ครบวงจร: recon → vuln scan → exploit → post-exploit |
| Tool Calling | Qwen3-32B | การใช้ nmap, gobuster, sqlmap, metasploit อย่างถูกต้อง |
| Error Recovery | Qwen3-32B | การแก้ไขเมื่อ exploit fail, service ไม่ตอบ, credential ผิด |
| RAG-Assisted | DeepSeek V3.2 | การใช้ knowledge base ระหว่าง pentest |
| Lateral Movement | DeepSeek V3.2 | Pivoting, port forwarding, credential reuse |
| Web App Exploit | DeepSeek V3.2 | SQLi, XSS, file upload, SSRF, IDOR |
| CTF Challenge | DeepSeek V3.2 | Binary exploitation, crypto, forensics |

**Generation Methodology:**
- ใช้ 2 โมเดลผ่าน OpenRouter API: Qwen3-32B (tool-heavy scenarios) และ DeepSeek V3.2 (complex reasoning scenarios)
- Randomized parameters: IP ranges, hostnames, service versions, credentials, vulnerability types
- Quality validation: อนุญาต short turns ไม่เกิน 30%, ต้องมีอย่างน้อย 1 `<tool_call>` ต่อบทสนทนา
- Parallel generation (configurable workers) พร้อม resume capability

**Post-Processing Pipeline:**
1. แทนที่ system prompt ยาว (953 chars) ด้วย standard prompt สั้น
2. ลบ empty assistant turns (<10 chars) พร้อม preceding user turn
3. แปลง explanation text ก่อน `<tool_call>` เป็น `<think>` blocks (โมเดลปฏิเสธสร้าง think tags โดยตรง)
4. เพิ่ม metadata: origin, model, conversation ID, multiturn flag

**ตัวอย่างผลลัพธ์:**

```json
{
  "messages": [
    {"role": "system", "content": "You are a penetration tester performing authorized security assessments..."},
    {"role": "user", "content": "Target: 10.10.14.73 running Apache 2.4.49 on port 443. Begin assessment."},
    {"role": "assistant", "content": "<think>\nTarget is Apache 2.4.49 which is vulnerable to CVE-2021-41773...\n</think>\n\nI'll start by testing for the path traversal vulnerability.\n\n<tool_call>{\"name\": \"run_command\", \"arguments\": {\"argv\": [\"curl\", \"-k\", \"--path-as-is\", \"https://10.10.14.73/cgi-bin/.%2e/.%2e/etc/passwd\"]}}</tool_call>"},
    {"role": "user", "content": "root:x:0:0:root:/root:/bin/bash\ndaemon:x:1:1:..."},
    {"role": "assistant", "content": "<think>\nPath traversal confirmed. I can read /etc/passwd. Now escalating...\n</think>\n\nPath traversal works. Attempting RCE via mod_cgi.\n\n<tool_call>{\"name\": \"run_command\", \"arguments\": {\"argv\": [\"curl\", \"-k\", \"--path-as-is\", \"-d\", \"echo Content-Type: text/plain; echo; id\", \"https://10.10.14.73/cgi-bin/.%2e/.%2e/bin/sh\"]}}</tool_call>"}
  ],
  "meta": {"origin": "gen_multiturn_full_pentest", "model": "qwen", "multiturn": true}
}
```

### 4.6 Curriculum Learning (4-Phase Training)

ข้อมูลถูกจัดเรียงตาม difficulty progression เพื่อให้โมเดลเรียนรู้จากง่ายไปยาก:

```
Epoch 1 (Curriculum-ordered):
  Phase 1: External Security QA (39,900) — ความรู้พื้นฐาน cybersecurity
      ↓
  Phase 2: Pentest QA (39,846) — การวิเคราะห์ช่องโหว่และ exploitation
      ↓
  Phase 3: Code SFT (20,890) — การเขียน exploit scripts
      ↓
  Phase 4: Agentic/Multi-turn (22,811) — การทำงานอัตโนมัติ multi-step

Epoch 2 (Fully shuffled):
  123,447 rows สุ่มลำดับทั้งหมด — เพื่อ generalization

Total: 246,894 training rows (1,224 MB)
```

**Phase Transition Points:**
- Row 39,900: Phase 1 → Phase 2
- Row 79,746: Phase 2 → Phase 3
- Row 100,636: Phase 3 → Phase 4
- Row 123,447: Epoch 1 → Epoch 2

## 5. Prompt Engineering

ระบบ Prompt ใช้ **6-layer compiled architecture** ที่ deterministic และ versioned:

| Layer | File | Purpose |
|-------|------|---------|
| 1. Core Identity | `core_identity.md` | บทบาท, ขอบเขตความสามารถ |
| 2. Authorization | `authorization.md` | ข้อกำหนดการอนุญาต, human-in-the-loop |
| 3. Data Provenance | `data_provenance.md` | การจัดการ evidence, chain of custody |
| 4. Action Protocol | `action_protocol.md` | ขั้นตอนการใช้ tools, safety checks |
| 5. Reporting | `reporting.md` | format การรายงานผล |
| 6. Engagement (dynamic) | `engagement.md.j2` | Jinja2 template ตาม engagement context |

เพิ่มเติม:
- **Phase-specific guidance** (6 files, ตาม pipeline phase ปัจจุบัน)
- **Tool cards** (per-tool documentation, เฉพาะ tools ที่ active ในขณะนั้น)
- **Prompt digest** — SHA-256 hash สำหรับ reproducibility tracking

## 6. GPU Resource Requirements

### 6.1 Full SFT Memory Analysis

Full Supervised Fine-Tuning ของ Qwen3-32B ต้องเก็บข้อมูลต่อไปนี้ใน GPU memory พร้อมกัน:

| Component | Size | Description |
|-----------|------|-------------|
| Model Weights (BF16) | 64 GB | 32B × 2 bytes |
| AdamW Momentum (FP32) | 128 GB | 32B × 4 bytes |
| AdamW Variance (FP32) | 128 GB | 32B × 4 bytes |
| Gradients (BF16) | 64 GB | 32B × 2 bytes |
| Activations + Buffers | 50-100 GB | ขึ้นกับ batch size, seq length |
| **Total** | **~450 GB** | |

Single NVIDIA B200 (192 GB HBM3e) ไม่เพียงพอ — ต้องใช้ DeepSpeed ZeRO-3 กระจายข้าม GPU

### 6.2 Recommended Configuration

| Config | GPUs × VRAM | Total VRAM | Est. Time | Notes |
|--------|-------------|-----------|-----------|-------|
| **4× B200** | 4 × 192 GB | **768 GB** | 6-10 hr | **แนะนำ** — เพียงพอ มี headroom |
| 8× B200 | 8 × 192 GB | 1,536 GB | 3-5 hr | เร็วกว่า ใช้ทรัพยากรมากกว่า |

### 6.3 Training Configuration (Planned)

```yaml
Framework: Hugging Face TRL + DeepSpeed ZeRO-3
Precision: BF16 mixed precision
Optimizer: AdamW (lr=2e-5, weight_decay=0.01)
Warmup: 3% of total steps
Batch Size: 128-256 (gradient accumulation)
Max Sequence Length: 4,096 tokens
Gradient Checkpointing: enabled
FlashAttention v2: enabled
Epochs: 2 (curriculum-ordered + shuffled)
Total Training Rows: 246,894
```

### 6.4 Estimated GPU Hours

| Item | Hours | GPU-Hours |
|------|-------|-----------|
| Full SFT Training (2 epochs) | 6-10 | 24-40 |
| Hyperparameter Tuning (2-3 runs) | 12-20 | 48-80 |
| LoRA Baseline (for comparison) | 2-3 | 8-12 |
| Evaluation Runs | 2-4 | 8-16 |
| **Total Estimated** | — | **80-150** |

## 7. Evaluation Plan

### 7.1 Benchmarks

| Benchmark | Tasks | Description |
|-----------|-------|-------------|
| AutoPenBench | 33 | Standard benchmark, 2 difficulty levels (in-vitro + real-world) |
| AI-Pentest-Benchmark | 13 machines, 152 subtasks | Real vulnerable machines |

### 7.2 Ablation Studies

| Experiment | Purpose |
|-----------|---------|
| Full SFT vs LoRA (r=16, 32, 64) | ผลของ parameter coverage ต่อ domain adaptation |
| With/Without RAG | ผลของ knowledge retrieval ต่อ task completion |
| With/Without Curriculum | ผลของ training order ต่อ final performance |
| Multi-turn ratio (0%, 10%, 20%) | ผลของ multi-turn data ต่อ agentic capability |

### 7.3 Baseline Comparisons

เปรียบเทียบกับ:
- Qwen3-32B base (no fine-tune)
- xOffense (LoRA fine-tuned Qwen3-32B) — current SOTA
- GPT-4o (proprietary baseline)
- PentestGPT / VulnBot (existing open systems)

## 8. Expected Contributions

1. **Full SFT Methodology for Offensive Security LLMs** — การศึกษา Full SFT แรกสำหรับโมเดลขนาด 32B ในโดเมน penetration testing พร้อมเปรียบเทียบกับ LoRA
2. **Large-Scale Curated Dataset** — ชุดข้อมูล 123K+ samples ที่ครอบคลุม multi-turn agentic scenarios, tool calling, error recovery
3. **Curriculum Learning for Security AI** — 4-phase progressive training methodology ที่ออกแบบตาม cognitive difficulty
4. **Comprehensive RAG Corpus** — Knowledge base 547K chunks จาก 15+ sources สำหรับ offensive security
5. **Safety-First Agent Architecture** — ระบบที่มี sandbox isolation, injection guard, audit trail, RoE enforcement ครบถ้วน ต่างจากงานวิจัยที่มีอยู่ที่เน้นเฉพาะ performance

## 9. Timeline

| Week | Activity |
|------|----------|
| 1 | Setup training environment, transfer data + code to B200 cluster |
| 2 | Full SFT training (2 epochs with curriculum), monitor convergence |
| 3 | AutoPenBench + AI-Pentest-Benchmark evaluation |
| 4 | LoRA baseline training for comparison, ablation studies |
| 5 | Analysis, additional experiments, paper writing |
| 6 | Paper revision and submission |

## 10. References

1. Luong et al., "xOffense: An Autonomous Multi-Agent Framework for Penetration Testing with Domain-Adapted Large Language Models," arXiv:2509.13021, Sep 2025
2. Deng et al., "PentestGPT: An LLM-empowered Automatic Penetration Testing Tool," arXiv:2308.06782, 2024
3. "VulnBot: Autonomous Penetration Testing for A Multi-Agent Collaborative Framework," arXiv:2501.13411, 2025
4. "AutoPenBench: Benchmarking Generative Agents for Penetration Testing," arXiv:2410.03225, 2024
5. "PentestAgent: Incorporating LLM Agents to Automated Penetration Testing," arXiv:2411.05185, 2024
6. "Towards Automated Penetration Testing: Introducing LLM Benchmark, Analysis, and Improvements," arXiv:2410.17141, 2024
7. "Benchmarking Practices in LLM-driven Offensive Security," arXiv:2504.10112, 2025
