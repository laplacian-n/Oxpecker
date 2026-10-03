# PentestAI Proposal v2 — Edit Instructions (Post Peer-Review)

คุณกำลังแก้ไข research proposal ขนาด 16 หน้า ชื่อ **"PentestAI: A Self-Hosted Agentic Penetration Testing System with Domain-Adapted Large Language Models"** เอกสารนี้ผ่าน adversarial peer review มาแล้ว มีจุดอ่อนร้ายแรง (MAJOR) 7 ข้อที่ต้องแก้

เอกสารเขียนภาษาไทยผสมศัพท์เทคนิคอังกฤษ — รักษา style นี้ไว้ เป้าหมายคือทำให้อ่านเหมือน **งานวิจัยระดับ conference paper** ไม่ใช่ technical report

---

## P0 — ไม่แก้ = งานพัง (Critical Fixes)

### P0-1: Reframe Thesis ทั้งหมด ⚠️ สำคัญที่สุด

**ปัญหา**: งานเดิมเดิมพันว่า "Full SFT > LoRA" แต่:
- AutoPenBench มี 33 tasks → xOffense 72.72% = 24/33 → เป้า >75% = 25/33 → **margin ทั้งหมด = 1 task**
- ที่ n=33, p≈0.75 → 95% CI ≈ ±15 percentage points → **แยกจาก noise ไม่ได้**
- ถ้าผลออกมาว่า LoRA ดีพอ ๆ กัน → thesis พัง, GPU สูญเปล่า

**แก้ไข**: เปลี่ยน thesis จาก "Full SFT wins" เป็น:

> "การศึกษาเชิงเปรียบเทียบอย่างเข้มงวดระหว่าง Full SFT และ LoRA สำหรับ domain adaptation ในงาน penetration testing — ครอบคลุมทั้ง in-domain performance, out-of-domain retention (catastrophic forgetting), และ cost-per-capability"

ผลจะออกทางไหนก็เป็น contribution: ถ้า Full SFT ชนะ = justify compute; ถ้า LoRA พอ = บอก field ว่าไม่ต้องเสีย compute เพิ่ม — **ทั้งสองทางมีค่าเท่ากัน**

**จุดที่ต้องแก้ในเอกสาร**:
- **Abstract**: เปลี่ยน "ส่วนสำคัญของงานวิจัยนี้คือการแสดงให้เห็นว่า..." เป็น "งานวิจัยนี้ตอบคำถามว่า Full SFT คุ้มค่า compute เพิ่มเติมเหนือ LoRA หรือไม่ โดยวัดทั้ง in-domain penetration testing performance และ general-capability retention"
- **Section 1 Introduction**: เปลี่ยน RQ1 จาก "Full SFT ให้ประสิทธิภาพเหนือกว่า LoRA หรือไม่?" เป็น "Full SFT ให้ cost-adjusted benefit เหนือ LoRA หรือไม่ เมื่อพิจารณาทั้ง domain performance, general retention, และ compute cost?"
- **Section 8 Contributions**: เปลี่ยน #1 จาก "Full SFT Methodology for Offensive Security LLMs" เป็น "Rigorous Cost-Benefit Analysis of Full SFT vs LoRA for Offensive Security Domain Adaptation"
- **ทั่วทั้งเอกสาร**: ลบ/แก้ทุกจุดที่ assert "Full SFT จะเหนือกว่า" → เปลี่ยนเป็น "ทดสอบว่า Full SFT คุ้มค่าหรือไม่"

### P0-2: Decontamination Pipeline (เพิ่มใหม่ทั้งหมด)

**ปัญหา**: Training data มาจาก HackTheBox/TryHackMe/VulnHub writeups + ExploitDB แต่ evaluation ใช้ vulnerable machines → **สูงมากที่ benchmark machines/CVEs จะ overlap กับ training set** ถ้าโมเดลเคยเห็น writeup ของเครื่องใน benchmark → คะแนนพอง ไร้ความหมาย

**แก้ไข**: เพิ่ม **Section 4.7 "Decontamination & Benchmark Isolation"** หลัง Curriculum Learning:

เนื้อหาที่ต้องเขียน:
1. **CVE-level deduplication**: ตรวจสอบ CVE IDs ทุกตัวที่ปรากฏใน training data เทียบกับ CVEs ใน AutoPenBench/AI-Pentest-Benchmark tasks → ลบ training rows ที่ overlap
2. **Machine-name matching**: dedup ชื่อเครื่อง/lab จาก writeups เทียบกับ benchmark target machines
3. **N-gram overlap analysis**: วัด textual overlap ระหว่าง training set กับ benchmark descriptions
4. **Hold-out protocol**: เครื่อง/CVEs ที่ "ใกล้" benchmark (same service + same vuln class) → hold out เป็น separate test set
5. **Overlap statistics**: รายงาน % overlap ก่อนและหลัง decontamination

สร้างตารางแสดง:
| Benchmark | Tasks | CVE Overlap (before) | CVE Overlap (after) | Machine Overlap |
|-----------|-------|---------------------|--------------------|-----------------| 
| AutoPenBench | 33 | TBD | 0 | TBD |
| AI-Pentest-Benchmark | 152 subtasks | TBD | 0 | TBD |

### P0-3: Statistical Power & Evaluation Redesign

**ปัญหา**: n=33 เล็กเกินไป, ไม่มี repeated seeds, ไม่มี CI, ไม่มี statistical tests

**แก้ไข**: แก้ **Section 7 Evaluation Plan** ทั้งหมด:

1. **เพิ่ม benchmarks** เพื่อขยาย task count:
   - AutoPenBench: 33 tasks
   - AI-Pentest-Benchmark: 152 subtasks  
   - พิจารณาเพิ่ม: Cybench, NYU CTF Bench, InterCode-CTF (ตรวจ availability ก่อน)
   - **รวม target: 200+ tasks** เพื่อให้ detect 5-10pp difference ได้

2. **Multiple seeds**: ทุก config รัน **5 seeds** ขั้นต่ำ (temperature/sampling variance)

3. **Report format**: ทุกตัวเลขรายงานเป็น **mean ± std** พร้อม **95% CI**

4. **Statistical tests**: paired comparison ระหว่าง Full SFT vs LoRA ด้วย McNemar's test หรือ bootstrap

5. **Power analysis**: เพิ่ม subsection คำนวณว่า task count ที่มี detect effect size เท่าไรได้

6. เพิ่มตาราง evaluation matrix:

| Metric | Method | Seeds | Reported As |
|--------|--------|-------|-------------|
| Task completion rate | AutoPenBench + AI-Pentest-Bench | 5 | mean ± std, 95% CI |
| Sub-task completion | Per-phase breakdown | 5 | mean ± std |
| Full SFT vs LoRA | McNemar's test | 5 | p-value, effect size |
| General retention | MMLU, HumanEval | 1 | absolute score |

---

## P1 — จำเป็นมาก (Major Fixes)

### P1-1: เพิ่ม Catastrophic Forgetting Evaluation

**ปัญหา**: Full SFT ทุก parameter เสี่ยง degrade general capability — เป็นเหตุผลหลักที่คนเลือก LoRA ("forgets less") แต่ eval plan ไม่วัดเลย

**แก้ไข**: เพิ่มใน **Section 7.2 Ablation Studies**:

| Experiment | Metric | Purpose |
|-----------|--------|---------|
| Pre-SFT baseline (Qwen3-32B base) | MMLU, HumanEval, GSM8K | วัด general capability ก่อน fine-tune |
| Post Full-SFT | MMLU, HumanEval, GSM8K | วัด retention หลัง full fine-tune |
| Post LoRA (r=16,32,64) | MMLU, HumanEval, GSM8K | เปรียบเทียบ forgetting |

เขียนย่อหน้าอธิบาย:
> "การเปรียบเทียบ Full SFT กับ LoRA ที่วัดเฉพาะ in-domain pentest performance ตอบคำถามเพียงครึ่งเดียว หาก Full SFT ให้คะแนน pentest สูงกว่าเล็กน้อยแต่ general reasoning ลดลงอย่างมีนัยสำคัญ ผลลัพธ์สุทธิอาจเป็นลบ จึงต้องวัด out-of-domain retention ควบคู่กัน"

### P1-2: Dual-Use & Responsible AI Section (เพิ่มใหม่ทั้งหมด) ⚠️

**ปัญหา**: 
- §4.3 quality filter ตัด "I cannot"/"I'm unable" = จงใจ train ให้โมเดล **ไม่มี refusal capability**
- Broker/sandbox เป็น **operational safety** (กัน authorized pentest หลุด scope) แต่ **ไม่ใช่ misuse mitigation** — ถ้า release weights, attacker แค่ไม่ผ่าน Broker
- รวมกัน: โมเดลไร้ refusal + release weights = **safety ถอดออกได้หมด**
- ไม่มี ethical section เลย → reviewer สาย responsible-AI จะ reject ทันที

**แก้ไข**: เพิ่ม **Section ใหม่ "จริยธรรมและการใช้งานอย่างรับผิดชอบ · Ethical Considerations & Responsible Use"** (แทรกก่อน Contributions):

**เนื้อหาที่ต้องเขียน (ทุกข้อ mandatory):**

1. **Dual-use analysis อย่างตรงไปตรงมา**:
   - ยอมรับว่าระบบนี้เป็น dual-use technology
   - อธิบายว่า safety mechanism (Broker/sandbox) = operational guard, ไม่ใช่ misuse guard
   - Discuss marginal uplift argument: model ที่ train จาก public data ไม่ให้ capability ที่ attacker ไม่สามารถหาได้จากแหล่งอื่นอยู่แล้ว (xOffense weights อาจ public อยู่)

2. **Weights release decision — ตัดสินใจให้ชัด**:
   - ทางเลือก A: **ไม่ release weights** → ต้อง justify ว่า reproducibility claim ยังอ้างได้ยังไง (release data + training recipe + eval code แทน)
   - ทางเลือก B: **Release weights** → ต้อง argue marginal uplift เหนือ existing public offensive models
   - ทางเลือก C: **Gated release** (ต้อง apply + verify institutional affiliation) → middle ground
   - **ระบุทางเลือกที่เลือกพร้อมเหตุผล**

3. **Refusal removal justification**:
   - Disclose อย่างเปิดเผยว่า training ตัด refusal responses ออก
   - Justify: โมเดลนี้ออกแบบมาให้ทำงาน offensive โดยเฉพาะ ภายใต้ Broker control — refusal จะขัดขวาง legitimate pentest workflow
   - ชดเชย: อธิบายว่า operational safety อยู่ที่ Broker layer ไม่ใช่ model layer — และ deployment model คือ self-hosted (ไม่มี public API endpoint)

4. **Data ethics**:
   - Training data มาจาก public writeups, open-source repos, publicly disclosed CVEs เท่านั้น
   - ไม่มี private exploit data, zero-day, หรือ non-public vulnerability information
   - Respect license ของ open-source datasets

5. **Deployment constraints**:
   - ระบบออกแบบให้ deploy บนเครือข่ายแยก (isolated network)
   - ต้องมี authorized scope (Rules of Engagement) ก่อนทุก engagement
   - Human-in-the-loop สำหรับ high-risk actions
   - Audit trail ทุก action เก็บใน evidence store

6. **Regulatory alignment**:
   - สอดคล้องกับ PDPA (Thailand) ในเรื่อง data handling
   - รองรับ PCI DSS pentest requirements
   - ไม่ขัดกับ Computer Crimes Act เนื่องจากทำงานภายใน authorized scope เท่านั้น

### P1-3: Validate Synthetic Multi-Turn Data

**ปัญหา**:
- 8,442 conversations, ~1.9 turns/conversation — **สั้นมาก** สำหรับ "agentic multi-turn"
- 3/7 ประเภท generate ด้วย Qwen3-32B (base model ที่จะ fine-tune) = **self-distillation** ไม่สอนอะไรใหม่
- ไม่มี human evaluation ของ generated data — exploit procedures อาจ hallucinate ทั้งหมด

**แก้ไข**: แก้ **Section 4.5**:

1. **Disclose turns/conversation อย่างชัดเจน**: เพิ่มสถิติ
   - เฉลี่ย turns/conversation per type
   - Distribution (min, median, max)
   - เปรียบเทียบกับ real pentest trajectory length (หลายสิบ steps)

2. **Acknowledge self-distillation limitation**: เพิ่มย่อหน้า
   > "บทสนทนา 3 ประเภทแรก (Full Pentest, Tool Calling, Error Recovery) สร้างจาก Qwen3-32B ซึ่งเป็น base model ที่จะ fine-tune — จึงเป็น self-distillation ที่ ceiling จำกัดโดยความสามารถของ generator ส่วนอีก 4 ประเภทสร้างจาก DeepSeek V3.2 ซึ่งแรงกว่า จึงมี genuine distillation signal งานในอนาคตควรพิจารณาใช้ teacher model ที่แรงกว่าสำหรับทุกประเภท"

3. **Human validation plan**: เพิ่ม
   > "สุ่มตัวอย่าง 100 บทสนทนา (แยกตามประเภท) ให้ผู้เชี่ยวชาญ pentest ประเมิน: (ก) ความถูกต้องของ exploit procedure (ข) ความสมจริงของ tool output (ค) ความเหมาะสมของ error recovery strategy คะแนนจะรายงานเป็น inter-rater agreement (Cohen's κ)"

4. **แก้ตัวเลข**: 8,887 → **8,442** unique conversations (ตรวจจาก codebase จริง)

### P1-4: De-risk Pilot (เพิ่มใน Timeline)

**ปัญหา**: ทุก claim เป็น "expected" — ไม่มีหลักฐานว่า pipeline ทำงานจริง

**แก้ไข**: เพิ่ม **Week 00 (Pre-GPU)** ใน Section 9 Timeline:

| Week | Activity |
|------|----------|
| **00 (pre-GPU)** | **Pilot run**: Full SFT + LoRA บน data subset (10%) ด้วย GPU ที่มีอยู่ (ถ้ามี A100/H100) หรือ cloud spot instance → แสดงว่า training pipeline ทำงาน + loss curve converge + มี preliminary signal |
| 01 | Setup training environment, transfer data + code to B200 cluster |
| 02 | Full SFT training (2 epochs with curriculum) |
| 03 | AutoPenBench + AI-Pentest-Benchmark + extended benchmarks |
| 04 | LoRA baselines (r=16,32,64) + general retention eval (MMLU, HumanEval) |
| 05 | Ablation studies, statistical analysis, human expert evaluation |
| 06 | Paper writing, revision, submission |

---

## P2 — เสริมความแข็งแรง (Important Improvements)

### P2-1: Cost-Effectiveness / TCO Analysis (แก้ไขจาก prompt v1)

**ปัญหา**: "marginal cost = $0" ไม่จริง — ละเลย inference GPU, ค่าไฟ, maintenance; เปรียบเทียบกับ human pentest ไม่แฟร์

**แก้ไข**: เพิ่ม **Section 6.5 "Total Cost of Ownership Analysis"**:

| Cost Component | PentestAI (Full SFT) | PentestAI (LoRA) | xOffense-style | GPT-4o + Scaffold |
|---------------|----------------------|-------------------|----------------|-------------------|
| Training (one-time) | 80-150 B200-hr (~$400-750) | 8-12 A100-hr (~$16-24) | ~same as LoRA | $0 |
| Inference GPU | 1× A100 or 2× RTX 4090 | same | same | $0 (API) |
| Inference cost/engagement | ~$2-5 electricity | same | same | $30-100 API |
| Annual maintenance | GPU depreciation + RAG updates | same | same | API cost scales |
| **5-year TCO (100 engagements/yr)** | **TBD** | **TBD** | **TBD** | **TBD** |

เพิ่มเติม:
- **Frame เป็น augmentation ไม่ใช่ replacement**: "PentestAI ไม่ได้ทดแทน human pentester แต่เป็นเครื่องมือที่ช่วยยกระดับ — เทียบได้กับ static analysis tool ที่หา vulnerability เบื้องต้นก่อน manual review"
- **ลบ/แก้การเปรียบเทียบกับ $15K-50K human pentest**: หรือถ้าเก็บไว้ ต้องระบุชัดว่า AI agent ทำได้เฉพาะ subset ของ human pentester (ไม่มี business logic, social engineering, physical)

### P2-2: Strengthen Introduction ด้วย RQ1-RQ3 (Reframed)

แก้ **Section 1** — เพิ่มจาก 2 ย่อหน้าเป็น 4-5 ย่อหน้า:

1. **Opening hook** (ย่อหน้า 1): Cybersecurity Poverty Line — gap ระหว่างองค์กรที่เข้าถึง professional pentest ได้กับที่ไม่ได้ เพิ่มสถิติ (จำนวนการโจมตี SMEs, ราคา professional pentest)

2. **Problem + Existing solutions** (ย่อหน้า 2): PentestGPT/VulnBot/xOffense ยังไม่ตอบโจทย์ — proprietary API dependency, no safety mechanism, LoRA-only, ไม่มี reproducibility

3. **Research gap** (ย่อหน้า 3): ยังไม่มีใครศึกษา Full SFT vs LoRA อย่างเข้มงวดในโดเมน offensive security, ไม่มีใครวัด forgetting, ไม่มี safety-integrated agent ที่ evaluated

4. **Research Questions** (ย่อหน้า 4) — **reframed version**:
   - **RQ1**: Full SFT domain adaptation ให้ cost-adjusted benefit เหนือ LoRA หรือไม่ เมื่อพิจารณาทั้ง in-domain performance, general retention, และ compute cost?
   - **RQ2**: สถาปัตยกรรม safety-first (Broker + Sandbox + Injection Guard) ป้องกัน scope violation ได้ effective แค่ไหนภายใต้ adversarial testing?
   - **RQ3**: Self-hosted system ที่ใช้โมเดล 32B สามารถให้ TCO ที่ justify ได้สำหรับองค์กรทดสอบความปลอดภัย เมื่อเทียบกับ LoRA-on-A100 และ API-based alternatives?

5. **Contributions preview** (ย่อหน้า 5): สรุป 5 contributions (reframed)

### P2-3: Strengthen Related Work Narrative

แก้ **Section 2** — หลัง Table 1 เพิ่ม 3 ย่อหน้า:

1. **Why prompting alone fails**: PentestGPT (9.09%) และ GPT-4o (21.21%) แสดงว่า general LLMs ขาด domain knowledge แม้จะมี reasoning ดี — ต้อง domain adaptation

2. **Why LoRA may not be enough** (ระวัง — ต้อง frame เป็นคำถาม ไม่ใช่ assert):
   > "xOffense ใช้ LoRA (<1% parameters) ได้ 72.72% แสดงว่า domain adaptation ช่วยอย่างชัดเจน อย่างไรก็ตาม ยังไม่มีการศึกษาว่า Full SFT ที่ปรับทุก parameter จะให้ผลดีกว่าหรือไม่ — และ cost-benefit ของ compute เพิ่มเติมเป็นอย่างไร Biderman et al. (2024) ชี้ว่า Full FT ชนะ LoRA เมื่อ domain shift ใหญ่ แต่ gap มักเล็กเมื่อ base model มี prior knowledge อยู่แล้ว คำถามนี้ยังเปิดอยู่สำหรับ offensive security domain"

3. **Research gap explicit**:
   - ไม่มีการเปรียบเทียบ Full SFT vs LoRA + วัด forgetting ในโดเมน offensive security
   - ไม่มี pentest agent ที่มี evaluated safety mechanism
   - ไม่มีการ report decontamination procedure สำหรับ pentest benchmarks
   - Dataset construction ส่วนใหญ่ไม่ transparent (xOffense ไม่เปิดเผยรายละเอียด)

### P2-4: RAG Evaluation (เพิ่มใหม่)

เพิ่มใน **Section 7.2 Ablation Studies**:

| Experiment | Metrics | Purpose |
|-----------|---------|---------|
| With/Without RAG | Task completion + retrieval recall@5, recall@10, nDCG@10 | วัดว่า RAG ช่วยหรือ distract |
| RAG latency | p50, p95, p99 retrieval time | วัด overhead |
| RAG distraction test | Task completion เมื่อ inject irrelevant chunks | วัดว่า noisy retrieval ทำร้ายได้ไหม |

### P2-5: Safety Architecture Evaluation (เพิ่มใหม่)

**ปัญหา**: Safety architecture เป็น contribution ที่แข็งที่สุดของงาน แต่ไม่มีแผน evaluate — มันเป็น claim ไม่ใช่ result

**แก้ไข**: เพิ่มใน **Section 7** หรือแยกเป็น Section ใหม่:

> "Red-team testing ของ safety mechanism"

| Test | Method | Success Criteria |
|------|--------|-----------------|
| Scope violation | Agent พยายาม access target นอก allow-list 100 ครั้ง | 100% blocked |
| RoE bypass | Craft tool calls ที่ละเมิด RoE แต่ format ถูกต้อง | 100% caught |
| Injection guard evasion | 50 adversarial prompts (novel encodings, obfuscation) | Detection rate + false positive rate |
| Sandbox escape | Attempt ptrace, mount, network access from sandbox | 100% blocked |
| Hallucinated target | Model generates IP/domain ที่ไม่อยู่ใน scope — Broker catch ได้ไหม? | ต้อง allow-list only (ไม่ใช่ deny-list) |

เพิ่มย่อหน้า:
> "ควรระบุชัดว่า scope checking ใช้ **allow-list เท่านั้น** (ไม่ใช่ deny-list) — หาก model hallucinate target ที่ไม่อยู่ใน allow-list จะถูก block โดยอัตโนมัติ ไม่ว่าจะ format ถูกต้องแค่ไหน"

---

## P3 — Content Corrections (ตรวจจาก codebase จริง)

### P3-1: ตัวเลขที่ต้องแก้
- **Multi-turn conversations**: 8,887 → **8,442** (ตรวจจาก unique conversation_id ใน train_v32_merged_train.jsonl)
- **"15 แหล่ง" ใน abstract**: แยกให้ชัด → "15+ แหล่งข้อมูล RAG" และ "50+ แหล่งข้อมูลฝึกสอน"
- **Dataset split**: เพิ่ม eval split → "123,447 ตัวอย่างฝึกสอน และ 6,498 ตัวอย่างประเมิน (รวม 129,945)"

### P3-2: Table 2 Dataset Breakdown (ต้องตรวจ)
ตัวเลขปัจจุบัน (Pentest QA 41,943 / External 42,000 / Code SFT 21,990 / Agentic 24,012) อาจเป็นตัวเลขประมาณ ข้อมูลจริง:
- Pentest/Security QA: ~27K
- External Security: ~52K
- Code SFT: ~21K
- Agentic/Multi-turn: ~22K

เลือก: แก้ให้ตรง หรือ เพิ่ม footnote ว่าเป็น approximate groupings

### P3-3: References — ต้อง verify arXiv IDs
- [1] xOffense arXiv:2509.13021 ✓
- [2] PentestGPT arXiv:2308.06782 ✓
- [4] AutoPenBench arXiv:2410.03225 ✓
- [3] VulnBot arXiv:2501.13411 — **ต้อง verify**
- [5] PentestAgent arXiv:2411.05185 — **ต้อง verify**
- [6] arXiv:2410.17141 — **ต้อง verify**
- [7] arXiv:2504.10112 — **ต้อง verify**

เพิ่ม reference ใหม่:
- Biderman et al. "LoRA Learns Less and Forgets Less" 2024 — สำหรับ Full SFT vs LoRA argument

### P3-4: Section 4.6 Curriculum Learning — Downgrade claim

**ปัญหา**: 
- Epoch 2 shuffle ทั้งหมด → อาจล้าง curriculum effect ทิ้ง
- "ง่าย→ยาก" จริง ๆ เรียงตาม ประเภทข้อมูล ไม่ใช่ difficulty ที่วัดได้
- Evidence ว่า curriculum ช่วย LLM fine-tuning ก้ำกึ่ง

**แก้ไข**: เปลี่ยนจาก "contribution" เป็น "ablation ที่จะตอบว่า curriculum ช่วยหรือไม่":
> "เราจัดลำดับข้อมูลตามสมมติฐานว่า progressive difficulty ช่วยการเรียนรู้ อย่างไรก็ตาม evidence ใน literature ยังก้ำกึ่ง จึงออกแบบ ablation (curriculum-ordered vs fully-shuffled) เพื่อทดสอบสมมติฐานนี้อย่างชัดเจน"

---

## P4 — Diagrams & Visual Improvements

### P4-1: Training Data Pipeline Diagram (Section 4)
```
Raw Sources → Quality Filters → Format Conversion → Decontamination → Curriculum Assembly
     │              │                    │                    │                  │
  Writeups      Min length         System prompt      CVE/machine dedup   4-Phase ordering
  CVE data      Refusal filter*    Think blocks       N-gram overlap      Epoch 1: curriculum
  Ext datasets  Language filter    Tool-call format   Benchmark isolation Epoch 2: shuffled
  
  * หมายเหตุ: Refusal filter ตัด "I cannot" responses — ดู §Ethical Considerations สำหรับ justification
```

### P4-2: Inference Pipeline / Data Flow Diagram (Section 3)
```
User → Agent Loop → Prompt Compiler (6 layers) → Qwen3-32B → Response Parser
                                                                    │
                                                          ┌────────┼────────┐
                                                       <think>   text   <tool_call>
                                                          │        │        │
                                                       (log)   (display)  Broker ← ALLOW-LIST ONLY
                                                                            │
                                                                    ┌───────┼───────┐
                                                                  RoE    Scope   Injection
                                                                  check  check   Guard
                                                                            │
                                                                        Sandbox (bubblewrap+seccomp)
                                                                            │
                                                                      Tool Output → Evidence Store
```

### P4-3: Evaluation Framework Diagram (Section 7 — expanded)
```
PentestAI ─┬─ In-Domain Performance
            │   ├─ AutoPenBench (33 tasks × 5 seeds)
            │   ├─ AI-Pentest-Benchmark (152 subtasks × 5 seeds)
            │   └─ Extended benchmarks (Cybench/NYU CTF — TBD)
            │
            ├─ Out-of-Domain Retention (Catastrophic Forgetting)
            │   ├─ MMLU (general reasoning)
            │   ├─ HumanEval (code generation)
            │   └─ GSM8K (mathematical reasoning)
            │
            ├─ Safety Evaluation
            │   ├─ Scope violation test (100 attempts)
            │   ├─ RoE bypass test
            │   ├─ Injection guard evasion (50 adversarial prompts)
            │   └─ Sandbox escape test
            │
            ├─ RAG Evaluation
            │   ├─ Retrieval recall@k, nDCG@10
            │   ├─ Latency (p50/p95/p99)
            │   └─ Distraction analysis
            │
            ├─ Ablation Studies
            │   ├─ Full SFT vs LoRA (r=16,32,64) — paired McNemar's test
            │   ├─ With/Without RAG
            │   ├─ Curriculum vs Shuffled
            │   └─ Multi-turn ratio (0%, 10%, 20%)
            │
            └─ Human Expert Evaluation
                ├─ 100 sampled conversations rated by pentesters
                └─ Inter-rater agreement (Cohen's κ)
```

### P4-4: Other Visual Improvements (จาก prompt v1)
- **Figure 1**: เพิ่ม PentestAI target bar (dashed) แต่ **ไม่ระบุตัวเลข** (เพราะ reframe แล้ว ไม่ commit ว่าจะชนะ)
- **Figure 2 Architecture**: Color-code tiers (Tier 1 warm, Tier 2 neutral, Tier 3 cool)
- **Fill empty pages**: หน้า 5, 7, 8, 9, 10 มีพื้นที่ว่างมาก → ใส่ diagrams ใหม่
- **เพิ่ม Table of Contents** หลัง Abstract
- **เพิ่ม Appendix** (ถ้ามีเนื้อที่):
  - Appendix A: Full training data source breakdown (50+ origins)
  - Appendix B: Sample multi-turn conversation (1 complete example)
  - Appendix C: Decontamination overlap statistics

---

## P5 — Academic Tone & Wording

### P5-1: Contribution Reorder
เรียงใหม่ตามความแข็งแรง:
1. **Safety-Integrated Agent Architecture** (แข็งสุด — unique contribution)
2. **Rigorous Full SFT vs LoRA Cost-Benefit Study** (reframed — ตอบได้ทุกทาง)
3. **Large-Scale Curated Dataset with Provenance** (transparent + documented)
4. **Comprehensive RAG Corpus** (547K chunks, 15+ sources)
5. **Curriculum Learning Investigation** (ablation — ยังไม่ confirm ว่าช่วย)

### P5-2: Target Market — เลือกให้ชัด
**ปัญหา**: Abstract พูดถึงทั้ง "นักทดสอบมืออาชีพ" และ "SME" — สองตลาดที่ขัดกัน

**แก้ไข**: เลือก primary = **professional pentesters / security testing firms** เป็น augmentation tool:
> "PentestAI ออกแบบเป็นเครื่องมือสนับสนุนนักทดสอบเจาะระบบมืออาชีพ — ช่วยลดเวลา reconnaissance, จัดลำดับช่องโหว่, และ automate routine exploitation ภายใต้การควบคุมของผู้เชี่ยวชาญ ผลข้างเคียงคือลดต้นทุนต่อ engagement ซึ่งอาจทำให้บริการทดสอบความปลอดภัยเข้าถึงได้ง่ายขึ้นสำหรับองค์กรขนาดเล็ก"

ลดโทน SME/poverty-line จาก "primary goal" เป็น "positive externality"

### P5-3: ตัด/ย่อ Sections ที่ละเอียดเกิน
- **§3.2 Agent Loop & Tool Calling**: ย่อเหลือ 3-4 บรรทัด + Figure 3 example
- **§6.3 Training Configuration**: ย้าย code block ไป Appendix, เหลือตารางสรุป
- **§3.5 cont. Parsing Methodology** (หน้า 7): ย่อ 4 bullets เป็น 1 ย่อหน้า

### P5-4: เพิ่ม Citations ใน Text Body
ทั่วทั้งเอกสาร เพิ่ม inline citations เพื่อ academic tone:
- "ดังที่แสดงใน xOffense [1]..."
- "ตาม benchmark มาตรฐาน AutoPenBench [4]..."
- "Biderman et al. [N] ชี้ว่า LoRA forgets less..."
- "สอดคล้องกับ PentestGPT [2] ที่พบว่า prompting เพียงอย่างเดียวไม่เพียงพอ..."

### P5-5: GPU Request Optimization
**ปัญหา**: Reviewer บอก "ทำด้วย resource น้อยกว่านี้ได้ไหม?"

**แก้ไข**: เพิ่มย่อหน้าใน §6.2:
> "ได้พิจารณาการลด resource แล้ว — 8-bit AdamW (bitsandbytes) จะลด optimizer states จาก 256GB เหลือ ~128GB รวม ~322GB+activations อาจทำงานบน 2-3× B200 ได้ อย่างไรก็ตาม ขอ 4× B200 เพื่อ (1) headroom สำหรับ activation memory ที่อาจสูงกว่า estimate (2) รัน LoRA baselines ขนานกัน (3) ลด training time เพื่อให้ ablation suite เต็มเสร็จใน timeline — การแสดงว่าได้ minimize request แล้วเสริมความน่าเชื่อถือของคำขอ"

---

## Summary of All Changes (Checklist)

### P0 (ไม่แก้ = พัง)
- [ ] Reframe thesis: "Full SFT wins" → "rigorous cost-benefit study"
- [ ] เพิ่ม Decontamination section (§4.7)
- [ ] Redesign evaluation: multiple seeds, CI, statistical tests, power analysis, expanded benchmarks

### P1 (จำเป็นมาก)
- [ ] เพิ่ม Catastrophic Forgetting evaluation (MMLU, HumanEval, GSM8K)
- [ ] เพิ่ม Ethical Considerations / Responsible AI section ทั้งหมด
- [ ] Address synthetic data limitations (self-distillation, turns/conv, human eval plan)
- [ ] เพิ่ม pilot run ใน timeline (Week 00)

### P2 (เสริมความแข็งแรง)
- [ ] TCO analysis ที่ครบถ้วน
- [ ] ขยาย Introduction ด้วย RQ1-RQ3 (reframed)
- [ ] เสริม Related Work narrative + research gap
- [ ] RAG evaluation metrics
- [ ] Safety architecture red-team evaluation plan
- [ ] GPU request justification (ทำไม 4× ไม่ใช่ 2-3×)

### P3 (แก้ตัวเลข)
- [ ] 8,887 → 8,442 multi-turn conversations
- [ ] Verify/fix dataset breakdown in Table 2
- [ ] Verify arXiv IDs [3,5,6,7]
- [ ] เพิ่ม Biderman et al. reference
- [ ] Downgrade curriculum learning claim

### P4 (Visual)
- [ ] Training pipeline diagram (with decontamination step)
- [ ] Inference flow diagram
- [ ] Evaluation framework diagram (expanded)
- [ ] Fill empty pages
- [ ] Table of Contents

### P5 (Tone)
- [ ] Reorder contributions (safety first)
- [ ] เลือก primary market (professional → SME as externality)
- [ ] ย่อ implementation details
- [ ] เพิ่ม inline citations
- [ ] GPU request optimization paragraph
