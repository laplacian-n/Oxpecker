# PentestAI — คู่มือเตรียมพบ Advisor & Q&A

> เอกสารนี้สรุปเนื้อหาทั้งหมดของโครงการ PentestAI เพื่อเตรียมตัวตอบคำถาม advisor
> อ้างอิงจาก proposal ฉบับจริง (23 หน้า, September 2026) และ codebase จริง

---

# ส่วนที่ 1: เนื้อหาหลัก — "หนังสือเรียน PentestAI"

---

## 1.1 ปัญหาที่ต้องการแก้ (Why This Project Exists)

**Cybersecurity Poverty Line** — แนวคิดที่ว่ามีเส้นแบ่งระหว่างองค์กรที่เข้าถึงบริการทดสอบเจาะระบบได้กับที่ไม่ได้:

- การจ้าง pentest มืออาชีพ: **$15,000–50,000 ต่อครั้ง**
- ผู้เชี่ยวชาญด้าน pentest มีจำนวนจำกัดทั่วโลก
- องค์กรขนาดกลาง-เล็ก (SMEs) ส่วนใหญ่ไม่เคยได้รับการทดสอบเจาะระบบเลย
- ผลคือ: ระดับความปลอดภัยพื้นฐานของภาคส่วนต่าง ๆ ต่ำกว่ามาตรฐาน

**PentestAI จึงเป็นเครื่องมือช่วยนักทดสอบ (augmentation)** — ไม่ใช่ทดแทน human pentester แต่ช่วยให้ทำงานเร็วขึ้น ต้นทุนต่ำลง เหมือน static analysis tool ที่คัดกรองช่องโหว่เบื้องต้นก่อน manual review

---

## 1.2 PentestAI คืออะไร

PentestAI เป็น **AI Agent สำหรับทดสอบเจาะระบบอัตโนมัติแบบ self-hosted** ประกอบด้วย:

1. **โมเดลภาษาขนาดใหญ่ (LLM)** — Qwen3-32B ที่ผ่านการ fine-tune เฉพาะทางด้าน offensive security
2. **Agent Framework** — ระบบ agent loop ที่สั่งงานเครื่องมือ (tools) ผ่าน structured format
3. **Safety Architecture** — Broker + Sandbox + Injection Guard ควบคุมทุก action
4. **RAG Knowledge Base** — ฐานความรู้ 547,118 chunks สำหรับเสริมความรู้ขณะทำงาน

**จุดเด่นที่ต่างจากงานอื่น:**
- **Self-hosted ทั้งหมด** → ไม่ส่งข้อมูลออกภายนอก เหมาะกับงานที่มีข้อกำหนดด้านความลับ
- **มี safety mechanism ครบ** → งานอื่น (xOffense, PentestGPT, VulnBot) ไม่มี
- **ศึกษา Full SFT vs LoRA อย่างเป็นระบบ** → ยังไม่มีใครทำในโดเมน offensive security

---

## 1.3 สถาปัตยกรรมระบบ (System Architecture)

ระบบแบ่งเป็น **3 Tiers**:

### Tier 1 — Reasoning Core (สมองของระบบ)

| Component | หน้าที่ |
|-----------|---------|
| **Fine-tuned Qwen3-32B** | โมเดลหลักที่ fine-tune ด้วย Full SFT สำหรับ offensive security |
| **Prompt Registry** | ระบบ prompt 6 ชั้น (6-layer compiled) ที่ deterministic และ versioned |
| **Pipeline Orchestrator** | State machine 6 เฟส ควบคุมลำดับการทำงานทั้ง engagement |

### Tier 2 — Guarded Execution (ชั้นควบคุม)

| Component | หน้าที่ |
|-----------|---------|
| **Tool-Call Parser** | แปลง response ของโมเดลเป็นคำสั่ง JSON ที่ validated |
| **Broker** | ตรวจสอบ RoE + Scope + Taint ก่อนอนุญาตให้ tool ทำงาน |
| **Security Tools** | เครื่องมือจริง: http_recon, port_discovery, browser_fetch, knowledge_search, knowledge_fetch |

### Tier 3 — Knowledge & Isolation (ชั้นข้อมูลและกันชน)

| Component | หน้าที่ |
|-----------|---------|
| **RAG Knowledge Base** | 547K chunks จาก 15+ แหล่ง สำหรับ retrieval ขณะทำงาน |
| **Sandbox** | bubblewrap + seccomp กักทุกคำสั่งในระดับ kernel |
| **Evidence Store** | เก็บหลักฐานทุก action เข้ารหัส + HMAC |

---

## 1.4 Agent Loop & Tool Calling — ระบบทำงานยังไง

เมื่อเริ่ม engagement, agent loop ทำงานดังนี้:

```
1. รับ context (target, scope, phase ปัจจุบัน)
2. Prompt Compiler รวม 6 prompt layers → ส่งเข้าโมเดล
3. โมเดลตอบเป็น structured format:
   - <think> block → Chain-of-Thought reasoning (เก็บ log)
   - Explanation text → แสดงให้ operator อ่าน
   - <tool_call> block → คำสั่งเครื่องมือในรูป JSON
4. Tool-Call Parser ดึง JSON ออกมา validate
5. Broker ตรวจ: RoE ✓ → Scope ✓ → Injection Guard ✓
6. ส่งคำสั่งเข้า Sandbox execute
7. Tool Output → เก็บ Evidence Store → ส่งกลับเข้า agent loop
8. วนซ้ำจนจบ phase หรือ operator หยุด
```

**ตัวอย่างจริง:**
```
<think>
Apache 2.4.49 บน port 443 → CVE-2021-41773 (path traversal)
ควรทดสอบด้วย curl request
</think>

ทดสอบ path traversal บน Apache 2.4.49:

<tool_call>{"name": "run_command", "arguments": {"argv":
["curl","-k","--path-as-is","https://10.10.10.42/cgi-bin/
.%2e/.%2e/.%2e/etc/passwd"]}}</tool_call>
```

---

## 1.5 Broker & Safety Layer — ระบบป้องกันอะไรบ้าง

**Broker เป็นหัวใจของ safety architecture** — ทุก tool call ต้องผ่าน Broker ก่อน:

| ชั้นป้องกัน | ทำอะไร | ทำไมถึงจำเป็น |
|------------|--------|---------------|
| **RoE Enforcement** | ตรวจว่า action อยู่ใน Rules of Engagement ที่ตกลงไว้ | ป้องกัน agent ทำเกินขอบเขตที่ได้รับอนุญาต |
| **Scope Checking (allow-list)** | ตรวจว่า target อยู่ใน allow-list เท่านั้น (**ไม่ใช่ deny-list**) | ถ้าโมเดล hallucinate IP/domain ที่ไม่อยู่ใน scope → block อัตโนมัติ |
| **Injection Guard** | สแกน tool output จับ prompt injection (base64/homoglyph/zero-width) + cross-turn taint tracking | ป้องกัน attacker ส่ง prompt injection กลับมาผ่าน tool output |
| **Rate Limiting** | จำกัดความถี่ action แต่ละประเภท | ป้องกัน agent loop ที่หลุดควบคุม |
| **Approval Queue** | action ที่เสี่ยงสูงต้องได้ human approval ก่อน | Human-in-the-loop สำหรับ critical actions |

**จุดสำคัญ**: Scope checking ใช้ **allow-list only** — หมายความว่าถ้าโมเดล hallucinate target ที่ไม่ได้อยู่ในรายชื่อที่อนุญาต จะถูก block ทันที ไม่ว่า format จะถูกต้องแค่ไหน ดีกว่า deny-list ที่ต้องรู้ล่วงหน้าว่าจะ block อะไร

---

## 1.6 Sandbox Isolation — กักทุกคำสั่งระดับ kernel

ทุกคำสั่งที่ agent สั่ง execute ถูกรันใน **bubblewrap sandbox**:

- **Filesystem isolation** — unshare-all, bind-mount เฉพาะ workspace ของ engagement นั้น
- **Network namespace isolation** — PID namespace แยก
- **seccomp deny-list** — block syscalls อันตราย: ptrace, mount, kernel module loading
- **Resource limits** — RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE ป้องกัน resource abuse

ผลคือ: แม้โมเดลจะสั่งคำสั่งอันตราย sandbox จะกันไม่ให้กระทบระบบ host

---

## 1.7 RAG Knowledge Base — ฐานความรู้ 547K chunks

### RAG คืออะไร
**Retrieval-Augmented Generation** — เทคนิคที่เสริมความรู้ให้โมเดลขณะทำงาน โดยค้นหาข้อมูลที่เกี่ยวข้องจากฐานความรู้มาใส่ใน context แทนที่จะพึ่ง knowledge ใน weights อย่างเดียว

### สร้างยังไง
1. **รวบรวมข้อมูลจาก 15+ แหล่ง** → NIST NVD, CyberStrike, Fenrir, ExploitDB, HackTricks, GTFOBins, Atomic Red Team ฯลฯ
2. **ตัดเป็น chunks** ตาม strategy ที่เหมาะกับแต่ละแหล่ง:
   - GTFOBins/LOLBAS → Structured YAML: 1 chunk ต่อ binary (function, context)
   - HackTricks → Header-boundary markdown splitting (##/###) + breadcrumb path
   - NIST NVD → System-prompt stripping + 2,800-char cap
   - CyberStrike/Fenrir/ExploitDB → Messages-format extraction + quality filters (min length, dedup by MD5)
3. **Embedding** ด้วย `nomic-embed-text-v1.5` (137M params, 768 มิติ)
4. **Index** เป็น numpy flat vector (L2-normalized, cosine similarity ผ่าน dot product)
5. **Prefix** แยก: `search_document:` สำหรับ indexing, `search_query:` สำหรับ retrieval

### องค์ประกอบ RAG
| แหล่งข้อมูล | จำนวน chunks |
|------------|-------------|
| NIST NVD | 247,199 |
| CyberStrike | 120,966 |
| Fenrir | 99,749 |
| ExploitDB | 46,505 |
| HackTricks | 13,044 |
| SFT Knowledge | 12,572 |
| GTFOBins · Atomic · Others | 7,083 |
| **รวม** | **547,118** |

### ใช้ยังไงขณะทำงาน
- Agent เจอ service/CVE ที่ไม่แน่ใจ → เรียก `knowledge_search` tool
- RAG ค้นหา chunks ที่เกี่ยวข้องที่สุด → ส่งกลับเป็น context
- โมเดลอ่าน context แล้วตัดสินใจ action ต่อไป
- ช่วยลด hallucination เพราะมีข้อมูล ground truth ให้อ้างอิง

---

## 1.8 Prompt Engineering — ระบบ Prompt 6 ชั้น

PentestAI ใช้ **6-layer compiled prompt** ที่ deterministic และ versioned:

| Layer | ไฟล์ | หน้าที่ |
|-------|------|---------|
| 1 · Core Identity | `core_identity.md` | บทบาท, ขอบเขตความสามารถ |
| 2 · Authorization | `authorization.md` | การอนุญาต, human-in-the-loop rules |
| 3 · Data Provenance | `data_provenance.md` | evidence, chain of custody |
| 4 · Action Protocol | `action_protocol.md` | ขั้นตอนใช้ tools, safety checks |
| 5 · Reporting | `reporting.md` | format การรายงานผล |
| 6 · Engagement (dynamic) | `engagement.md.j2` | Jinja2 template ที่เปลี่ยนตาม context ของ engagement |

**เสริมด้วย:**
- Phase-specific guidance (6 ไฟล์ ตาม phase ของ pipeline)
- Tool cards (เฉพาะ tools ที่ active ใน phase นั้น)
- Prompt digest แบบ SHA-256 สำหรับ reproducibility tracking

**ทำไมถึงสำคัญ:** ระบบ prompt แบบนี้ทำให้ behavior ของ agent **deterministic** — รันซ้ำด้วย prompt เดิมได้ผลเหมือนกัน ต่างจากการใช้ free-form prompt ที่ไม่ consistent

---

## 1.9 Pipeline Orchestrator — State Machine 6 เฟส

ระบบจัดการทั้ง engagement ผ่าน **6-phase state machine**:

```
INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT
```

| Phase | ทำอะไร |
|-------|--------|
| INTAKE | รับ scope, RoE, สร้าง engagement profile |
| RECON | สแกนและเก็บข้อมูลเป้าหมาย |
| ANALYSIS | วิเคราะห์ช่องโหว่ที่พบ |
| VALIDATION | ทดสอบ exploit เพื่อยืนยันช่องโหว่ |
| REPORT | สรุปผลเป็นรายงาน |
| CLOSEOUT | ปิด engagement, cleanup |

**คุณสมบัติ:**
- **Idempotent task planning** — สร้าง tasks ตาม state ปัจจุบัน ไม่สร้างซ้ำ
- **Budget-driven transition** — ไม่มีเงื่อนไข "ต้องพบช่องโหว่"; phase เสร็จได้แม้มี zero findings
- **Two pipeline profiles** — Web/API และ Network ใช้ state machine ร่วมกัน ต่างเฉพาะ recon tasks

---

## 1.10 ชุดข้อมูลฝึกสอน (Training Data v3.2)

### ภาพรวม
| Component | Train | Eval | Total |
|-----------|-------|------|-------|
| Pentest QA (custom) | 27,046 | 2,097 | 29,143 |
| External Security QA | 52,700 | 2,100 | 54,800 |
| Code SFT | 20,890 | 1,100 | 21,990 |
| Agentic / Multi-turn | 22,811 | 1,201 | 24,012 |
| **Total** | **123,447** | **6,498** | **129,945** |

จัดเตรียมจาก **50+ แหล่งข้อมูลสาธารณะ** (origin) ทั้ง writeups, CVE data, open-source datasets

### 4 กลุ่มข้อมูลหลัก

**กลุ่ม 1: Pentest QA (Custom)** — 27K train
- สร้างจาก writeups ของ HackTheBox, TryHackMe, VulnHub
- แปลงเป็น instruction-response pairs
- แต่ละ QA pair ครอบคลุม vulnerability เฉพาะ + exploit procedure
- ทุก response ใช้ `<think>` tags สำหรับ CoT reasoning
- Tool calls ในรูปแบบ `<tool_call>{"name": "run_command", ...}</tool_call>`

**กลุ่ม 2: External Security Knowledge** — 52K train
- ผสมผสานจาก 4 แหล่งหลัก:
  - CyberStrike (~30K) — cybersecurity instruction-response
  - Fenrir (~25K) — pentesting knowledge
  - ExploitDB (~15K) — exploit descriptions
  - NIST NVD (~10K) — vulnerability intelligence
- Quality filters: min response length, MD5 dedup, ตัด refusal responses, context limit

**กลุ่ม 3: Code SFT** — 21K train
- ให้โมเดลเขียน exploit code, scripts, automated tools ได้:
  - Magicoder (OSS-Instruct + Evol-Instruct)
  - CodeFeedback (multi-turn code debugging)
  - Glaive Code (diverse code instruction-response)
  - Self OSS Instruct (self-generated programming exercises)

**กลุ่ม 4: Agentic / Multi-turn** — 23K train (8,442 unique conversations, 16,085 multi-turn rows)
- **Core contribution ของงาน** — ทำให้โมเดลทำงานเป็น agent ต่อเนื่องได้
- 7 ประเภทสถานการณ์จำลอง:

| Type | Model | Count | Description |
|------|-------|-------|-------------|
| Full Pentest | Qwen3-32B | 1,205 | recon → vuln scan → exploit → post-exploit |
| Tool Calling | Qwen3-32B | 1,318 | nmap, gobuster, sqlmap, metasploit |
| Error Recovery | Qwen3-32B | 1,102 | แก้ไขเมื่อ exploit fail / credential ผิด |
| RAG-Assisted | DeepSeek V3.2 | 1,240 | ใช้ knowledge base ระหว่าง pentest |
| Lateral Movement | DeepSeek V3.2 | 1,156 | pivoting, port forwarding, credential reuse |
| Web App Exploit | DeepSeek V3.2 | 1,221 | SQLi, XSS, file upload, SSRF, IDOR |
| CTF Challenge | DeepSeek V3.2 | 1,200 | binary exploitation, crypto, forensics |

**ข้อจำกัดที่ disclose:**
- ความยาวเฉลี่ย ~1.9 turns/conversation — สั้นกว่า real pentest trajectory (หลายสิบ steps)
- 3 ประเภทแรกสร้างจาก Qwen3-32B (base model) = self-distillation ที่ ceiling จำกัด
- อีก 4 ประเภทจาก DeepSeek V3.2 = genuine distillation signal

---

## 1.11 Decontamination Pipeline — ป้องกัน Data Leakage

เนื่องจาก training data มาจาก writeups ของ vulnerable machines + ExploitDB ซึ่ง **อาจ overlap กับ benchmark** → ถ้าโมเดลเคยเห็น writeup ของเครื่องใน benchmark คะแนนจะพอง

**5 ขั้นตอน Decontamination:**
1. **CVE-level dedup** — ตรวจ CVE ID ทุกตัวใน training data เทียบกับ CVEs ใน AutoPenBench/AI-Pentest-Benchmark → ลบ rows ที่ overlap
2. **Machine-name matching** — dedup ชื่อเครื่อง/lab จาก writeups เทียบกับ benchmark targets
3. **N-gram overlap** — วัด textual overlap ระหว่าง training set กับ benchmark descriptions
4. **Hold-out protocol** — เครื่อง/CVEs ที่ "ใกล้" benchmark (same service + same vuln class) → hold out เป็น separate test set
5. **Overlap statistics** — รายงาน % overlap ก่อน/หลัง (target: CVE overlap after = **0**)

---

## 1.12 Curriculum Learning — การเรียงลำดับข้อมูลฝึก

จัดลำดับข้อมูลตามสมมติฐาน progressive difficulty (ง่าย → ยาก):

```
Phase 1: External Security QA (52,700) — ความรู้พื้นฐาน
Phase 2: Pentest QA (27,046) — pentest เฉพาะทาง
Phase 3: Code SFT (20,890) — เขียน exploit code
Phase 4: Agentic/Multi-turn (22,811) — ทำงานเป็น agent
```

- **Epoch 1**: curriculum-ordered ตาม difficulty progression
- **Epoch 2**: fully shuffled เพื่อ generalization
- รวม 246,894 rows (1,224 MB)

**สำคัญ:** งานนี้ **ไม่ assert ว่า curriculum ช่วยแน่นอน** — evidence ใน literature ยังก้ำกึ่ง จึงออกแบบ **ablation** (curriculum-ordered vs fully-shuffled) เพื่อทดสอบสมมติฐานนี้

---

## 1.13 Full SFT vs LoRA — คำถามวิจัยหลัก

### Full SFT คืออะไร
**Full Supervised Fine-Tuning** — ปรับ **ทุก parameter** ของโมเดล (~32 พันล้าน parameters) ด้วย training data ใหม่

### LoRA คืออะไร
**Low-Rank Adaptation** — ปรับเฉพาะ adapter weights เล็ก ๆ ที่แทรกเข้าไป (<1% ของ parameters ทั้งหมด) base weights ไม่เปลี่ยน

### ทำไมต้องเปรียบเทียบ
- xOffense (คู่แข่ง SOTA) ใช้ LoRA ได้ 72.72% บน AutoPenBench
- **ยังไม่มีใครศึกษาว่า Full SFT คุ้มค่า compute เพิ่มเติมหรือไม่** ในโดเมน offensive security
- Biderman et al. (2024) ชี้ว่า Full FT มักชนะเมื่อ domain shift ใหญ่ แต่ LoRA "forgets less"
- **คำถามยังเปิดอยู่** → งานนี้จะตอบ

### ข้อดี/ข้อเสียของแต่ละแบบ
| | Full SFT | LoRA |
|--|----------|------|
| Parameters ที่ปรับ | ทุกตัว (~32B) | <1% (~300M) |
| GPU ที่ต้องการ train | 4× B200 (768 GB) | 1× A100 (80 GB) |
| Training cost | ~$400-750 | ~$16-24 |
| Training time | 6-10 hr | 2-3 hr |
| ศักยภาพ domain shift | สูง (ปรับทุกชั้น) | จำกัด (ปรับเฉพาะ adapter) |
| Catastrophic forgetting risk | สูง | ต่ำ |
| Inference cost | เท่ากัน | เท่ากัน |

### Thesis ของงาน (Reframed)
> **ไม่ใช่** "Full SFT จะชนะ LoRA"
> **แต่เป็น** "การศึกษาเชิงเปรียบเทียบอย่างเข้มงวดระหว่าง Full SFT และ LoRA — ครอบคลุม in-domain performance, out-of-domain retention (catastrophic forgetting), และ cost-per-capability"

**ผลจะออกทางไหนก็เป็น contribution:**
- ถ้า Full SFT ชนะ → justify compute ที่ใช้เพิ่ม
- ถ้า LoRA เพียงพอ → บอก field ว่าไม่ต้องเสีย compute เพิ่ม
- ทั้งสองทางมีคุณค่าเท่ากัน

---

## 1.14 GPU Requirements — ทำไมต้อง 4× B200

### Memory Analysis สำหรับ Full SFT ของ Qwen3-32B

| Component | Size | สูตร |
|-----------|------|------|
| Model Weights (BF16) | 64 GB | 32B × 2 bytes |
| AdamW Momentum (FP32) | 128 GB | 32B × 4 bytes |
| AdamW Variance (FP32) | 128 GB | 32B × 4 bytes |
| Gradients (BF16) | 64 GB | 32B × 2 bytes |
| Activations + Buffers | 50-100 GB | ขึ้นกับ batch size, seq length |
| **Total** | **~450 GB** | |

Single NVIDIA B200 (192 GB) ไม่เพียงพอ → ต้องใช้ **DeepSpeed ZeRO-3** กระจาย optimizer states และ gradients ข้าม GPU

### ทำไม 4× ไม่ใช่ 2-3×
- 8-bit AdamW ลด optimizer states ได้ → อาจรันบน 2-3× B200 ได้
- แต่ขอ 4× เพื่อ: **(1)** headroom สำหรับ activation memory **(2)** รัน LoRA baselines ขนานกัน **(3)** ลด training time ให้ ablation suite เสร็จใน timeline

### GPU Hours ที่ขอ
| Item | Hours | GPU-Hours |
|------|-------|-----------|
| Full SFT Training (2 epochs) | 6-10 | 24-40 |
| Hyperparameter Tuning (2-3 runs) | 12-20 | 48-80 |
| LoRA Baseline (comparison) | 2-3 | 8-12 |
| Evaluation Runs | 2-4 | 8-16 |
| **Total** | — | **80-150** |

---

## 1.15 Training Configuration

```
Framework         Hugging Face TRL + DeepSpeed ZeRO-3
Precision         BF16 mixed precision
Optimizer         AdamW (lr=2e-5, weight_decay=0.01)
Warmup            3% of total steps
Batch Size        128-256 (gradient accumulation)
Max Seq Length     4,096 tokens
Grad Checkpointing  enabled
FlashAttention v2  enabled
Epochs            2 (curriculum-ordered + shuffled)
Total Rows        246,894
```

---

## 1.16 Total Cost of Ownership (TCO)

| Cost Component | PentestAI · Full SFT | PentestAI · LoRA | GPT-4o + Scaffold |
|---------------|---------------------|-------------------|-------------------|
| Training (one-time) | 80-150 B200-hr (~$400-750) | 8-12 A100-hr (~$16-24) | $0 |
| Inference hardware | 1× A100 / 2× 4090 | same | API only |
| Cost / engagement | ~$2-5 ไฟฟ้า | ~$2-5 | $30-100 API |
| Annual maintenance | GPU depr. + RAG update | same | scales w/ usage |
| **5-yr TCO (100 eng./yr)** | **~$3.5-5K** | **~$3-4.5K** | **~$15-50K** |

---

## 1.17 Evaluation Plan

### Benchmarks
| Benchmark | Tasks | Description |
|-----------|-------|-------------|
| AutoPenBench | 33 | Standard benchmark, 2 difficulty levels (in-vitro + real-world) |
| AI-Pentest-Benchmark | 13 · 152 | 13 machines, 152 subtasks — real vulnerable machines |
| Extended (TBD) | Cybench, NYU CTF, InterCode-CTF | ขยาย task count เป้าหมาย 200+ |

### Statistical Rigor
- **ทุก config รัน 5 seeds** (temperature/sampling variance)
- **ทุกตัวเลข report เป็น mean ± std พร้อม 95% CI**
- **McNemar's test** สำหรับ paired comparison Full SFT vs LoRA
- ที่ n=33 (AutoPenBench) → 95% CI ≈ ±15pp → ด้วย 200+ tasks จะ detect 5-10pp difference ได้

### Catastrophic Forgetting Evaluation
| Checkpoint | Metric | Purpose |
|-----------|--------|---------|
| Qwen3-32B base (pre-SFT) | MMLU · HumanEval · GSM8K | baseline general capability |
| Post Full-SFT | MMLU · HumanEval · GSM8K | retention หลัง full fine-tune |
| Post LoRA (r=16,32,64) | MMLU · HumanEval · GSM8K | เปรียบเทียบ forgetting |

### Ablation Studies
| Experiment | Purpose |
|-----------|---------|
| Full SFT vs LoRA (r=16, 32, 64) | ผลของ parameter coverage ต่อ domain adaptation |
| With / Without RAG | ผลของ knowledge retrieval ต่อ task completion |
| With / Without Curriculum | ผลของ training order ต่อ final performance |
| Multi-turn ratio (0%, 10%, 20%) | ผลของ multi-turn data ต่อ agentic capability |

### Safety Red-Team Evaluation
| Test | Method | Success Criteria |
|------|--------|-----------------|
| Scope violation | access นอก allow-list 100 ครั้ง | 100% blocked |
| RoE bypass | tool calls ละเมิด RoE แต่ format ถูก | 100% caught |
| Injection evasion | 50 adversarial prompts (novel encodings) | detection + FP rate |
| Sandbox escape | ptrace / mount / network attempts | 100% blocked |

### RAG Evaluation
| Experiment | Metrics | Purpose |
|-----------|---------|---------|
| With / Without RAG | completion + recall@5, recall@10, nDCG@10 | RAG ช่วยหรือ distract |
| RAG latency | p50 · p95 · p99 retrieval time | วัด overhead |
| Distraction test | completion เมื่อ inject irrelevant chunks | noisy retrieval ส่งผลเสียไหม |

### Human Expert Evaluation
- สุ่ม 100 บทสนทนา ให้ผู้เชี่ยวชาญ pentest ประเมิน
- ประเมิน: ความถูกต้อง exploit / tool output / error recovery
- รายงานเป็น inter-rater agreement (Cohen's κ)

---

## 1.18 จริยธรรมและ Responsible AI

### Dual-Use
- ยอมรับตรง ๆ ว่าเป็น dual-use technology
- Safety mechanism (Broker/sandbox) = **operational guard** ไม่ใช่ misuse guard
- ถ้า release weights → attacker ข้าม Broker ได้ → แต่ model train จาก public data → marginal uplift จำกัด (ข้อมูลเหล่านี้ attacker หาได้จากแหล่งอื่นอยู่แล้ว)

### Weights Release
- เลือก **Gated Release (Option C)** — ผู้ขอต้องยืนยัน institutional affiliation + วัตถุประสงค์ก่อนเข้าถึง weights
- Reproducibility รับประกันผ่าน: training data recipe, eval code, decontamination scripts (เปิดหมด)

### Refusal Removal
- เปิดเผยว่า training ตัด "I cannot"/"I'm unable" responses ออก
- เหตุผล: โมเดลออกแบบมาทำงาน offensive โดยเฉพาะ — refusal ขัดขวาง legitimate workflow
- Safety อยู่ที่ **Broker layer ไม่ใช่ model layer** + deployment เป็น self-hosted (ไม่มี public API)

### Regulatory
- สอดคล้อง PDPA (ไทย), PCI DSS pentest requirements
- ไม่ขัด พ.ร.บ. คอมพิวเตอร์ เพราะทำงานใน authorized scope เท่านั้น

---

## 1.19 Contributions ของงาน (5 ข้อ เรียงตามความแข็งแรง)

1. **Safety-Integrated Agent Architecture** — สถาปัตยกรรม agent ที่ผนวก sandbox, injection guard, audit trail + RoE enforcement + แผนประเมิน safety เชิง red-team (**unique — ยังไม่มีงานอื่นที่ทำ**)

2. **Rigorous Full SFT vs LoRA Cost-Benefit Study** — การเปรียบเทียบอย่างเข้มงวด ครอบคลุม in-domain, forgetting, cost-per-capability (**ตอบเป็น contribution ได้ไม่ว่าผลจะออกทางไหน**)

3. **Large-Scale Curated Dataset with Provenance** — ชุดข้อมูล 123K+ samples ครอบคลุม multi-turn agentic scenarios พร้อม metadata แสดงที่มา + decontamination กับ benchmark

4. **Comprehensive RAG Corpus** — knowledge base 547K chunks จาก 15+ sources สำหรับ offensive security

5. **Curriculum Learning Investigation** — การทดสอบสมมติฐาน progressive-difficulty ผ่าน ablation (**ยังไม่ยืนยัน จึงเสนอเป็นคำถามที่ออกแบบมาให้พิสูจน์ได้**)

---

## 1.20 Timeline

| Week | Activity |
|------|----------|
| **00 (pre-GPU)** | **Pilot run**: Full SFT + LoRA บน data subset 10% ด้วย A100/H100 หรือ cloud spot — พิสูจน์ว่า pipeline ทำงาน + loss converge |
| 01 | Setup training environment, transfer data + code to B200 cluster |
| 02 | Full SFT training (2 epochs with curriculum), monitor convergence |
| 03 | AutoPenBench + AI-Pentest-Benchmark evaluation |
| 04 | LoRA baseline training for comparison, ablation studies |
| 05 | Analysis, additional experiments, paper writing |
| 06 | Paper revision and submission |

---

## 1.21 งานที่เกี่ยวข้อง (Related Work) — สรุปสั้น

| System | Base Model | Method | AutoPenBench | Safety |
|--------|-----------|--------|-------------|--------|
| PentestGPT (2024) | GPT-4/Llama3.1-405B | Prompting | 9.09% | — |
| VulnBot (2025) | Llama3.1-405B | Multi-agent prompting | 30.30% | — |
| **xOffense (2025)** | **Qwen3-32B** | **LoRA fine-tuning** | **72.72%** | — |
| GPT-4o (baseline) | GPT-4o | Prompting | 21.21% | — |
| Qwen3-32B (base) | Qwen3-32B | Prompting | 30.30% | — |
| **PentestAI (this work)** | **Qwen3-32B** | **Full SFT (vs LoRA)** | **target** | **Broker + Sandbox + Guard** |

**Key takeaways:**
- Prompting อย่างเดียวไม่พอ (PentestGPT 9.09%)
- Domain adaptation ช่วยมาก (xOffense 72.72%)
- ยังไม่มีใครศึกษา Full SFT vs LoRA + วัด forgetting + มี safety mechanism

---

# ส่วนที่ 2: Q&A — คำถามที่ advisor อาจถาม (60+ ข้อ)

---

## A. ภาพรวมและเป้าหมาย (10 ข้อ)

**Q1: งานนี้ทำอะไร สรุปสั้น ๆ?**
> A: พัฒนาระบบ AI Agent สำหรับช่วยนักทดสอบเจาะระบบ โดยใช้ Qwen3-32B ที่ fine-tune เฉพาะทาง ทำงานบนเครื่องขององค์กรเอง มี safety mechanism ครบ และศึกษาเปรียบเทียบว่า Full SFT คุ้มค่ากว่า LoRA หรือไม่

**Q2: ทำไปเพื่ออะไร? มีคุณค่ายังไง?**
> A: ตอบคำถามวิจัยที่ยังไม่มีคำตอบ — Full SFT vs LoRA ในโดเมน offensive security คุ้มค่าไหม และสร้าง pentest agent ที่มี safety architecture จริง ๆ ซึ่งยังไม่มีในงานไหน ผลที่ได้ช่วยให้ field ตัดสินใจว่าควรลงทุน compute แค่ไหน + ยกระดับ cybersecurity ขั้นพื้นฐาน

**Q3: Penetration testing คืออะไร?**
> A: กระบวนการทดสอบความปลอดภัยของระบบคอมพิวเตอร์โดยจำลองการโจมตีจริง เพื่อค้นหาช่องโหว่ก่อนที่ attacker จริงจะหาเจอ ทำโดยผู้เชี่ยวชาญที่ได้รับอนุญาต (authorized)

**Q4: ทำไมต้อง self-hosted?**
> A: (1) ข้อมูล pentest เป็นความลับ — ส่งไป cloud API มีความเสี่ยงรั่วไหล (2) marginal cost ต่ำ — ไม่เสีย API cost ต่อ engagement (3) ไม่ขึ้นกับ provider — ไม่โดน rate limit, ไม่มีปัญหา content policy block legitimate pentest actions

**Q5: ทำไมถึงเลือก Qwen3-32B?**
> A: (1) xOffense (SOTA ปัจจุบัน) ใช้ Qwen3-32B → เปรียบเทียบ fair (2) ขนาด 32B parameters เป็น sweet spot — ใหญ่พอมี reasoning ดี แต่รัน inference บน single GPU ได้ (3) open-weight model ที่ fine-tune ได้เต็มที่

**Q6: Cybersecurity Poverty Line คืออะไร?**
> A: แนวคิดที่ว่ามีเส้นแบ่งระหว่างองค์กรที่มีทรัพยากรทำ security ได้กับที่ไม่มี — pentest ราคา $15K-50K ต่อครั้ง ทำให้ SMEs ส่วนใหญ่ไม่เคยได้ทดสอบเลย PentestAI ช่วยลดต้นทุนนี้

**Q7: PentestAI ทดแทน pentester มนุษย์ได้ไหม?**
> A: ไม่ได้ — เป็น augmentation tool เหมือน static analysis ช่วยคัดกรองเบื้องต้น ไม่ครอบคลุม business logic flaws, social engineering, physical security ยังต้องมี human expert ควบคุม

**Q8: Research Questions ของงานคืออะไร?**
> A: RQ1: Full SFT ให้ cost-adjusted benefit เหนือ LoRA ไหม เมื่อวัดทั้ง in-domain + forgetting + cost? RQ2: Safety architecture (Broker+Sandbox+Guard) ป้องกัน scope violation ได้ effective แค่ไหนภายใต้ adversarial testing? RQ3: Self-hosted 32B model ให้ TCO ที่ justify ได้ไหม เทียบกับ LoRA-on-A100 และ API?

**Q9: คู่แข่งหลักคือใคร?**
> A: xOffense (arXiv:2509.13021) — ใช้ Qwen3-32B + LoRA ได้ 72.72% บน AutoPenBench เป็น SOTA ปัจจุบัน แต่ไม่มี safety mechanism, ไม่มี decontamination report, ไม่เปิดเผย dataset construction details

**Q10: ถ้า LoRA ดีพอ ๆ กับ Full SFT งานนี้จะสูญเปล่าไหม?**
> A: ไม่ — เพราะ reframe thesis แล้ว ถ้า LoRA เพียงพอก็เป็น contribution ที่บอก field ว่าไม่ต้องเสีย compute เพิ่ม + ยังมี safety architecture, dataset, RAG corpus เป็น contribution อื่นที่ไม่ขึ้นกับผล Full SFT vs LoRA

---

## B. สิ่งที่ทำไปแล้ว / Timeline (8 ข้อ)

**Q11: ทำอะไรไปแล้วบ้าง?**
> A: implement เสร็จเกือบทั้งหมด — agent framework, broker, sandbox, injection guard, RAG (547K chunks), prompt system 6 ชั้น, pipeline orchestrator, dataset v3.2 (130K samples), multi-turn conversations (8,442 ชุด), evaluation harness เหลือแค่ training + evaluation ที่ต้องใช้ GPU

**Q12: Dataset สร้างมายังไง ใช้เวลาเท่าไหร่?**
> A: รวบรวมจาก 50+ แหล่งสาธารณะ → filter คุณภาพ → แปลง format → generate multi-turn conversations ผ่าน OpenRouter API (ใช้ Qwen3-32B + DeepSeek V3.2) → post-processing → decontamination งบ multi-turn generation ~$20

**Q13: RAG index สร้างยังไง ใช้เวลาเท่าไหร่?**
> A: download 15+ แหล่ง → parse ตาม strategy เฉพาะแต่ละแหล่ง → embed ด้วย nomic-embed-text-v1.5 → สร้าง numpy flat vector index ระบบมี checkpoint/resume support รันบน CPU ได้

**Q14: ทำไมถึงมี pilot run (Week 00)?**
> A: เพื่อ de-risk — แสดงว่า training pipeline ทำงานจริง, loss curve converge, มี preliminary signal ก่อนใช้ GPU จริง รันบน A100/H100 หรือ cloud spot ด้วย 10% ของ data

**Q15: Week 00 ใช้ GPU อะไร?**
> A: ใช้ GPU ที่มีอยู่ (ถ้ามี A100/H100) หรือเช่า cloud spot instance — ยังไม่ใช้ B200 ที่ขอ เป็นการพิสูจน์ concept ก่อน

**Q16: เสร็จทั้งหมดภายในกี่สัปดาห์?**
> A: 6 สัปดาห์หลังได้ GPU (+ Week 00 pre-GPU) รวมตั้งแต่ training จนส่ง paper

**Q17: ตอนนี้ยังขาดอะไร?**
> A: ขาดแค่ GPU สำหรับ (1) train Full SFT + LoRA baselines (2) รัน benchmarks (AutoPenBench, AI-Pentest-Benchmark) (3) ablation studies (4) catastrophic forgetting evaluation ทุกอย่างอื่นพร้อมหมดแล้ว

**Q18: จะส่ง paper ที่ไหน?**
> A: เป้าหมายคือ security/AI conference — ยังไม่ระบุ venue เฉพาะ เตรียม evaluation ให้ครอบคลุมพอสำหรับ top-tier venue ได้

---

## C. เทคนิคเชิงลึก (18 ข้อ)

**Q19: Full SFT คืออะไร ต่างจาก LoRA ยังไง?**
> A: Full SFT ปรับทุก parameter (~32B) ของโมเดล LoRA ปรับเฉพาะ adapter weights เล็ก ๆ (<1%) Full SFT ได้ศักยภาพ domain shift สูงกว่าแต่เสี่ยง catastrophic forgetting และใช้ GPU มากกว่า

**Q20: DeepSpeed ZeRO-3 คืออะไร ทำไมต้องใช้?**
> A: เทคนิค distributed training ที่กระจาย optimizer states, gradients, และ model parameters ข้าม GPU หลายตัว ทำให้ train โมเดลที่ใหญ่กว่า VRAM ของ GPU เดี่ยวได้ จำเป็นเพราะ Full SFT ของ 32B model ต้องการ ~450 GB แต่ B200 มีแค่ 192 GB ต่อตัว

**Q21: Catastrophic forgetting คืออะไร?**
> A: ปรากฏการณ์ที่โมเดลที่ถูก fine-tune "ลืม" ความรู้ทั่วไปที่เคยมี เช่น fine-tune ให้เก่ง pentest แต่ทำ math ไม่ได้ วัดได้จากคะแนน MMLU/HumanEval/GSM8K ที่ลดลง Full SFT เสี่ยงมากกว่า LoRA เพราะเปลี่ยนทุก weight

**Q22: BF16 mixed precision คืออะไร?**
> A: Brain Float 16 — format ตัวเลขที่ใช้ 16 bits แต่มี exponent range เท่า FP32 ลด memory ลงครึ่งหนึ่งโดยแทบไม่สูญเสียคุณภาพ "mixed" คือ forward pass ใช้ BF16 แต่ gradient accumulation ใช้ FP32

**Q23: Gradient accumulation คืออะไร ทำไม batch size 128-256?**
> A: เทคนิคสะสม gradients จากหลาย mini-batch ก่อน update weights ครั้งหนึ่ง ทำให้ได้ effective batch size ใหญ่โดยไม่ต้องใช้ memory มาก batch size 128-256 ช่วยให้ training stable

**Q24: FlashAttention v2 คืออะไร?**
> A: อัลกอริทึมที่คำนวณ attention mechanism ได้เร็วขึ้น 2-4× โดยใช้ memory น้อยลง (IO-aware) ทำให้ train ด้วย seq length 4,096 tokens ได้โดยไม่ OOM

**Q25: Embedding model nomic-embed-text-v1.5 คืออะไร?**
> A: โมเดลขนาดเล็ก (137M params) ที่แปลงข้อความเป็น vector 768 มิติ ใช้สำหรับ RAG — ทำให้ค้นหาข้อมูลที่มีความหมายคล้ายกันได้ (semantic search) โดยไม่ต้อง keyword match

**Q26: Cosine similarity ใน RAG ทำงานยังไง?**
> A: วัดมุมระหว่าง 2 vectors — ถ้ามุมเล็ก (ค่าใกล้ 1) แปลว่าเนื้อหาคล้ายกัน Query ของ agent ถูกแปลงเป็น vector แล้วหา chunks ที่ cosine similarity สูงสุด

**Q27: McNemar's test คืออะไร ทำไมใช้ตัวนี้?**
> A: Statistical test สำหรับเปรียบเทียบ paired binary outcomes — เหมาะกับ pentest benchmark ที่แต่ละ task เป็น pass/fail เปรียบเทียบว่า Full SFT กับ LoRA ทำ task เดียวกันผ่าน/ไม่ผ่านต่างกัน significant ไหม

**Q28: 95% Confidence Interval คืออะไร ทำไมสำคัญ?**
> A: ช่วง 95% ที่คะแนนจริงน่าจะอยู่ ที่ n=33 tasks (AutoPenBench) ถ้าได้ 75% → 95% CI ≈ 60-90% → กว้างมาก แยก signal จาก noise ไม่ได้ ต้องขยาย task count เป็น 200+ ถึงจะ detect 5-10pp difference

**Q29: AutoPenBench คืออะไร?**
> A: Benchmark มาตรฐานสำหรับวัด LLM-driven pentest agent มี 33 tasks, 2 ระดับความยาก (in-vitro = isolated, real-world = networked) xOffense ได้ 72.72% เป็น SOTA ปัจจุบัน

**Q30: AI-Pentest-Benchmark คืออะไร ต่างจาก AutoPenBench ยังไง?**
> A: Benchmark ที่ใช้ real vulnerable machines (13 เครื่อง, 152 subtasks) ซับซ้อนกว่า AutoPenBench ช่วยขยาย task count ให้ statistical power ดีขึ้น

**Q31: nDCG@10 คืออะไร?**
> A: Normalized Discounted Cumulative Gain at rank 10 — วัดคุณภาพ ranking ของ search results ไม่ใช่แค่ "เจอหรือไม่เจอ" แต่ "เจอในอันดับที่ถูกต้องไหม" ใช้วัดคุณภาพ RAG retrieval

**Q32: Cohen's κ (kappa) คืออะไร?**
> A: ค่าที่วัดระดับ agreement ระหว่าง raters สองคนขึ้นไป โดยหัก chance agreement ออก ใช้วัดว่าผู้เชี่ยวชาญ pentest ที่ประเมิน synthetic data เห็นตรงกันแค่ไหน κ > 0.6 ถือว่าดี

**Q33: Prompt injection คืออะไร? Injection Guard ป้องกันยังไง?**
> A: การโจมตีที่ attacker ซ่อนคำสั่งไว้ใน tool output เพื่อหลอกให้ LLM ทำตาม เช่น web page ที่มีข้อความ "ignore previous instructions and..." Injection Guard สแกนหา patterns: base64 encoding, homoglyph characters, zero-width characters + ติดตาม taint ข้ามหลาย turns

**Q34: Chain-of-Thought (CoT) reasoning คืออะไร?**
> A: เทคนิคที่ให้โมเดลแสดงกระบวนการคิดทีละขั้นก่อนตัดสินใจ ใน PentestAI ใช้ `<think>` block — โมเดลวิเคราะห์ service version → หา CVE → วางแผน exploit ก่อนสั่ง tool call ช่วยให้ reasoning ดีขึ้นและ operator ตรวจสอบได้

**Q35: Self-distillation คืออะไร ทำไมเป็นข้อจำกัด?**
> A: การสร้าง training data โดยใช้โมเดลเดียวกับที่จะ fine-tune (Qwen3-32B สร้าง data → fine-tune Qwen3-32B) ข้อจำกัดคือ data quality ถูก ceiling โดยความสามารถของ generator — ไม่สอนอะไรที่โมเดลทำไม่เป็นอยู่แล้ว ต่างจาก distillation จาก teacher ที่แรงกว่า (DeepSeek V3.2)

**Q36: Curriculum learning คืออะไร ทำไม evidence ยังก้ำกึ่ง?**
> A: การจัดลำดับ training data จากง่ายไปยาก สมมติฐานคือช่วยการเรียนรู้ แต่ research ในด้าน LLM fine-tuning ยังไม่ฟันธง — บาง paper เห็นผล บางอันไม่เห็น ยิ่ง Epoch 2 shuffle ทั้งหมดอาจล้าง curriculum effect จาก Epoch 1 จึงออกแบบ ablation เพื่อทดสอบ

---

## D. Data & Training (8 ข้อ)

**Q37: Training data มาจากไหนบ้าง?**
> A: 50+ แหล่งสาธารณะ — HackTheBox/TryHackMe/VulnHub writeups, CyberStrike, Fenrir, ExploitDB, NIST NVD, Magicoder, CodeFeedback, Glaive Code, Self OSS Instruct + 8,442 synthetic multi-turn conversations ที่เราสร้างเอง

**Q38: ทำไมต้อง 130K samples? มากหรือน้อยไป?**
> A: เป็นขนาดที่ balance ระหว่าง coverage กับ quality — ใหญ่พอครอบคลุมทุกด้าน (security QA, code, agentic) แต่ไม่ใหญ่จนคุณภาพลด xOffense ไม่เปิดเผยขนาด dataset ทำให้เปรียบเทียบตรง ๆ ไม่ได้

**Q39: ทำไมตัดคำตอบ "I cannot" ออก?**
> A: เพราะโมเดลนี้ออกแบบมาให้ทำ offensive security โดยเฉพาะ — ถ้ายังมี refusal มันจะปฏิเสธทำ legitimate pentest tasks (เช่น scan port, run exploit) Safety ถูกวางไว้ที่ Broker layer แทน ไม่ใช่ที่ model layer

**Q40: Data contamination คืออะไร ทำไมต้องกังวล?**
> A: Training data มี writeups ของ vulnerable machines → benchmark ก็ใช้ vulnerable machines → ถ้า overlap โมเดลเหมือน "เคยเห็นข้อสอบ" → คะแนนพอง ไร้ความหมาย จึงต้องทำ decontamination pipeline

**Q41: Multi-turn conversations 8,442 ชุดพอไหม?**
> A: เป็นจุดเริ่มที่ดี แต่มีข้อจำกัด — เฉลี่ย ~1.9 turns/conversation ซึ่งสั้นกว่า real pentest trajectory (หลายสิบ steps) งานในอนาคตควรขยายทั้งจำนวนและความยาว + ใช้ teacher model ที่แรงกว่าสำหรับทุกประเภท

**Q42: ทำไมใช้ 2 โมเดลสร้าง multi-turn (Qwen3-32B + DeepSeek V3.2)?**
> A: Qwen3-32B สร้าง 3 ประเภทแรก (Full Pentest, Tool Calling, Error Recovery) — เป็น self-distillation DeepSeek V3.2 สร้าง 4 ประเภทที่เหลือ — แรงกว่า Qwen3-32B จึงให้ genuine distillation signal

**Q43: Quality filters มีอะไรบ้าง?**
> A: (1) Minimum response length (ตัดสั้นเกินไป) (2) MD5 hash dedup 500 ตัวอักษรแรก (3) ตัด refusal responses (4) Context limit capping

**Q44: Post-processing pipeline ของ multi-turn ทำอะไร?**
> A: (1) แทน system prompt ยาว 953 chars ด้วย standard prompt สั้น (2) ลบ empty assistant turns + preceding user turn (3) แปลง explanation text ก่อน `<tool_call>` เป็น `<think>` blocks (4) เพิ่ม metadata: origin, model, conversation ID, multiturn flag

---

## E. Safety & Ethics (8 ข้อ)

**Q45: ถ้าปล่อย model weights attacker เอาไปใช้ได้เลยไหม?**
> A: เทคนิคแล้วใช่ — attacker ข้าม Broker/sandbox ได้ แต่ (1) model train จาก public data ที่ attacker หาได้อยู่แล้ว → marginal uplift จำกัด (2) xOffense อาจ release weights อยู่แล้ว (3) เราเลือก Gated Release — ต้อง verify institutional affiliation ก่อนเข้าถึง

**Q46: Allow-list vs deny-list ต่างกันยังไง ทำไมเลือก allow-list?**
> A: Allow-list = อนุญาตเฉพาะที่ระบุ, block ที่เหลือทั้งหมด Deny-list = block เฉพาะที่ระบุ, อนุญาตที่เหลือ Allow-list ปลอดภัยกว่ามาก — ถ้าโมเดล hallucinate target ใหม่ที่ไม่เคยเห็น allow-list block อัตโนมัติ deny-list ปล่อยผ่าน

**Q47: Human-in-the-loop ทำงานยังไง?**
> A: Action ที่มีความเสี่ยงสูง (เช่น exploit ที่อาจทำให้ service crash) ต้องเข้าคิว approval → operator ดูรายละเอียด → approve/reject ก่อน agent ทำต่อ Action ทั่วไป (recon, scan) ผ่าน Broker อัตโนมัติ

**Q48: Evidence store เก็บอะไร?**
> A: ทุก action ที่ agent ทำ — tool call, output, timestamps — เข้ารหัส + HMAC สำหรับ integrity verification ใช้เป็น audit trail และ reproducibility

**Q49: PDPA เกี่ยวข้องยังไง?**
> A: พ.ร.บ. คุ้มครองข้อมูลส่วนบุคคล — PentestAI ทำงานบนเครือข่ายแยก (isolated) ไม่ส่งข้อมูลออกภายนอก จัดการข้อมูลที่พบระหว่าง pentest ตาม PDPA

**Q50: PCI DSS เกี่ยวข้องยังไง?**
> A: มาตรฐานความปลอดภัยของข้อมูลการชำระเงิน — กำหนดให้ต้องทำ pentest เป็นประจำ PentestAI ช่วยให้องค์กรทำได้บ่อยขึ้นในต้นทุนต่ำ

**Q51: ถ้า agent ทำ damage ให้ target system ใครรับผิดชอบ?**
> A: Operator (นักทดสอบ) ที่ set scope และ approve actions — agent เป็นเครื่องมือ human-in-the-loop สำหรับ high-risk actions + sandbox จำกัด blast radius + audit trail ทุก action เพื่อ accountability

**Q52: Cross-turn taint tracking คืออะไร?**
> A: ความสามารถของ Injection Guard ในการติดตาม "taint" (ข้อมูลที่อาจถูก inject) ข้ามหลาย turns ของ conversation ป้องกัน multi-step injection attack ที่ attacker แบ่งคำสั่งออกเป็นหลายส่วน

---

## F. Evaluation & Results (8 ข้อ)

**Q53: ผลยังไม่มีเลยเหรอ?**
> A: ถูกต้อง — นี่คือ proposal, ยังไม่ได้ train เพราะต้องรอ GPU ทุก claim เป็น "expected" แต่ระบบ implement เสร็จ + pilot run จะทำก่อนใช้ B200 เพื่อแสดงว่า pipeline ทำงานจริง

**Q54: ทำไมไม่ train บน cloud แทนขอ GPU?**
> A: (1) Cloud cost สำหรับ 4× B200 80-150 ชั่วโมง ≈ $1,000+ (2) ต้องทำ hyperparameter tuning + ablation อีกหลายรอบ → ค่าใช้จ่ายพอกพูน (3) มี GPU allocation ให้ขอจาก institution ซึ่งเป็น resource ที่ available

**Q55: ทำไมต้อง 5 seeds ไม่ใช่ 1?**
> A: เพราะ LLM มี randomness จาก temperature/sampling → รันครั้งเดียวอาจได้ผลที่ lucky/unlucky 5 seeds ให้ mean ± std ที่น่าเชื่อถือ + สร้าง 95% CI ได้

**Q56: ถ้า Full SFT กับ LoRA ได้คะแนนใกล้กัน จะสรุปอะไร?**
> A: สรุปว่า LoRA เพียงพอ → ไม่ต้องเสีย compute เพิ่ม → เป็น recommendation ที่มีค่าสำหรับ field โดยเฉพาะถ้า LoRA forgetting ก็น้อยกว่า → ยิ่ง justify LoRA

**Q57: Ablation study คืออะไร ทำไมสำคัญ?**
> A: การทดลองที่ "ตัดออก" ทีละ component เพื่อวัดว่าแต่ละส่วนมีผลเท่าไหร่ เช่น ตัด RAG ออก → คะแนนลดเท่าไหร่? ตัด curriculum → ต่างไหม? ทำให้รู้ว่า contribution ไหนมีค่าจริง

**Q58: Red-team testing ของ safety ทำยังไง?**
> A: ออกแบบ 4 ชุดทดสอบ: (1) พยายาม access นอก allow-list 100 ครั้ง (2) craft tool calls ที่ละเมิด RoE แต่ format ถูก (3) ส่ง 50 adversarial prompts ด้วย novel encodings (4) พยายาม escape sandbox ด้วย ptrace/mount/network วัด success criteria: 100% blocked / caught

**Q59: RAG distraction test คืออะไร?**
> A: ทดสอบว่าถ้า RAG ดึง chunks ที่ไม่เกี่ยวข้องมาให้ agent (noisy retrieval) จะส่งผลเสียต่อ task completion ไหม — ถ้าส่งผลเสียมากแสดงว่า agent พึ่ง RAG เกินไป

**Q60: Power analysis คืออะไร ทำไมต้องมี?**
> A: การคำนวณว่า sample size ที่มี (task count) สามารถ detect effect size เท่าไหร่ได้ ถ้า n=33 → CI กว้าง → detect 1-2 tasks difference ไม่ได้ ต้อง n=200+ ถึงจะ detect 5-10pp อย่างมี confidence

---

## G. คำถามเชิงท้าทาย / Devil's Advocate (8 ข้อ)

**Q61: งานนี้ต่างจาก xOffense ยังไง? ทำไมไม่ใช้ xOffense เลย?**
> A: ต่าง 4 จุดหลัก: (1) ศึกษา Full SFT vs LoRA — xOffense ทำแค่ LoRA (2) มี safety architecture ครบ — xOffense ไม่มี (3) มี decontamination pipeline — xOffense ไม่ report (4) เปิดเผย dataset construction — xOffense ไม่เปิด ใช้ xOffense ตรง ๆ ไม่ได้เพราะไม่ตอบ research questions ของเรา

**Q62: ทำไมไม่ใช้โมเดลใหญ่กว่า เช่น 70B?**
> A: (1) 70B ต้อง GPU เพิ่ม 2× ทั้ง training + inference (2) Inference บน single GPU ไม่ได้ ขัดกับ self-hosted value proposition (3) xOffense พิสูจน์แล้วว่า 32B + domain adaptation เอาชนะ 405B prompting ได้

**Q63: RAG 547K chunks ไม่ใหญ่เกินไปหรือ? Noisy retrieval ไม่เป็นปัญหา?**
> A: จึงออกแบบ RAG evaluation 3 ชุด: with/without RAG, latency test, distraction test ถ้า RAG ไม่ช่วยจะรู้จาก ablation + distraction test วัดว่า noise เป็นปัญหาแค่ไหน

**Q64: 8,442 conversations แค่ ~1.9 turns พอทำให้เป็น agent จริงไหม?**
> A: เป็นข้อจำกัดที่ disclose ไว้ชัดเจน — สั้นกว่า real pentest trajectory แต่ (1) ยังดีกว่าไม่มี multi-turn เลย (2) ablation study multi-turn ratio (0%, 10%, 20%) จะบอกว่า multi-turn data ช่วยจริงแค่ไหน (3) งานอนาคตต้องขยาย

**Q65: ทำไมไม่ fine-tune โมเดลที่ใหม่กว่า?**
> A: Qwen3-32B เป็น base model เดียวกับ xOffense → เปรียบเทียบ fair ถ้าใช้โมเดลอื่นจะไม่รู้ว่าผลต่างมาจาก model หรือ method ใน future work อาจทดสอบกับ models อื่น

**Q66: ระบบรองรับ engagement scope แบบไหนได้จริง?**
> A: ปัจจุบันรองรับ Web/API และ Network pentest 2 profiles ไม่ครอบคลุม Active Directory, cloud-specific, mobile, IoT, physical, social engineering — ข้อจำกัดที่ต้อง acknowledge

**Q67: ถ้า benchmark มีแค่ 33 tasks ผลจะน่าเชื่อถือไหม?**
> A: AutoPenBench เดี่ยว ไม่พอ — จึงเพิ่ม AI-Pentest-Benchmark (152 subtasks) + พิจารณา Cybench/NYU CTF/InterCode-CTF เป้าหมาย 200+ tasks + 5 seeds + McNemar's test ทำให้ statistical power เพียงพอ

**Q68: ค่าใช้จ่าย $400-750 สำหรับ training คุ้มไหม เทียบกับ LoRA $16-24?**
> A: นี่คือคำถามที่งานจะตอบ — ถ้า Full SFT ดีกว่า LoRA อย่างมีนัยสำคัญ $400-750 one-time cost อาจคุ้มค่า ถ้าไม่ดีกว่า ก็ contribute ว่า LoRA เพียงพอ ไม่ต้องเสีย compute เพิ่ม

---

## H. ศัพท์เทคนิคที่อาจถูกถามให้อธิบาย (เพิ่มเติม)

**Q69: LLM (Large Language Model) คืออะไร?**
> A: โมเดล AI ขนาดใหญ่ที่ถูก train ให้เข้าใจและสร้างภาษา เช่น GPT-4, Qwen3, Llama ทำงานโดยทำนายคำถัดไปจาก context ที่ให้

**Q70: Fine-tuning คืออะไร?**
> A: การนำ pre-trained model มาปรับแต่งเพิ่มเติมด้วย domain-specific data ให้เก่งในงานเฉพาะทาง เหมือนหมอที่เรียนแพทย์ทั่วไปแล้วมา specialize เฉพาะทาง

**Q71: Token คืออะไร?**
> A: หน่วยย่อยที่โมเดลประมวลผล — ประมาณ 3/4 ของคำภาษาอังกฤษ Max Seq Length 4,096 tokens ≈ ~3,000 คำที่โมเดลเห็นพร้อมกัน

**Q72: VRAM คืออะไร ต่างจาก RAM ยังไง?**
> A: Video RAM — memory บน GPU ที่เร็วกว่า system RAM มาก ใช้เก็บ model weights, gradients, activations ระหว่าง training B200 มี 192 GB VRAM ต่อตัว

**Q73: Inference คืออะไร?**
> A: ขั้นตอนที่โมเดลที่ train เสร็จแล้วรับ input แล้วสร้าง output — ตอนใช้งานจริง ต่างจาก training ที่เป็นขั้นตอนเรียนรู้ Inference ใช้ GPU น้อยกว่า training มาก

**Q74: Agent คืออะไร ต่างจาก chatbot ยังไง?**
> A: Agent = LLM + tools + decision loop — ไม่ใช่แค่ตอบคำถาม แต่ทำ action ได้ (รัน command, scan port, search knowledge) แล้ววนกลับมาวิเคราะห์ผลแล้วทำต่อ Chatbot แค่รับคำถาม → ตอบ → จบ

**Q75: Benchmark คืออะไร?**
> A: ชุดทดสอบมาตรฐานที่ใช้วัดและเปรียบเทียบประสิทธิภาพ เหมือนข้อสอบกลางที่ทุกคนทำข้อเดียวกัน AutoPenBench เป็น benchmark สำหรับ pentest agents

**Q76: Ablation study คืออะไร อธิบายง่าย ๆ?**
> A: เหมือนถอดอะไหล่รถออกทีละชิ้นแล้วดูว่ารถยังวิ่งได้ดีไหม — ถอด RAG ออก ลดลงเท่าไหร่? ถอด curriculum ออก ลดไหม? ทำให้รู้ว่าแต่ละส่วนสำคัญจริงหรือเปล่า

---

## I. คำถามเรื่อง GPU โดยเฉพาะ (6 ข้อ)

**Q77: จะเอา GPU ไปทำอะไรบ้าง ให้ละเอียด?**
> A: (1) Train Full SFT บน Qwen3-32B ด้วย dataset 130K samples, 2 epochs — 24-40 GPU-hours (2) Hyperparameter tuning 2-3 runs — 48-80 GPU-hours (3) Train LoRA baselines r=16,32,64 สำหรับเปรียบเทียบ — 8-12 GPU-hours (4) รัน benchmarks AutoPenBench + AI-Pentest-Benchmark + ablation evaluation — 8-16 GPU-hours รวม 80-150 GPU-hours

**Q78: ใช้เวลากี่สัปดาห์?**
> A: 6 สัปดาห์ (ไม่รวม Week 00 pilot) — พอดีกับ research cycle + paper writing

**Q79: NVIDIA B200 คืออะไร ทำไมต้อง B200?**
> A: GPU รุ่นใหม่สุด (Blackwell architecture) มี 192 GB HBM3e VRAM — จำเป็นเพราะ Full SFT ของ 32B model ต้องการ ~450 GB total memory ต้องกระจายข้าม 4 ตัว

**Q80: ใช้ A100 แทนได้ไหม?**
> A: A100 (80 GB) ต้องใช้ 8+ ตัว = resource มากกว่า + ช้ากว่า B200 มี 192 GB ต่อตัว ทำให้ 4 ตัวเพียงพอ ถ้า institution มี A100 cluster ก็ทำได้ แต่ต้อง 8+ ตัว

**Q81: ทำไมไม่ train แค่ LoRA แล้วจบ? ไม่ต้องใช้ B200 เลย**
> A: เพราะ research question คือ "Full SFT คุ้มค่าไหม" — ถ้าไม่ลอง Full SFT ก็ตอบไม่ได้ LoRA train บน A100 ได้แต่ไม่ตอบ RQ1 นี่คือเหตุผลหลักที่ต้องขอ B200

**Q82: GPU จะ idle ระหว่างรอหรือเปล่า?**
> A: ไม่ — timeline ออกแบบให้ใช้ GPU ต่อเนื่อง Week 02 training, Week 03 evaluation, Week 04 LoRA + ablation, Week 05 additional experiments ไม่มีช่วง idle ยาว

---

*เอกสารนี้จัดทำจาก PentestAI Research Proposal (23 หน้า, September 2026) และ codebase จริง*
*อัปเดตล่าสุด: 25 กันยายน 2569*
