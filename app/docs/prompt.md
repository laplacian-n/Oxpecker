# PentestAI Academic Proposal — Edit Instructions for Claude Design

You are editing a 16-page academic research proposal (PDF/document) titled **"PentestAI: A Self-Hosted Agentic Penetration Testing System with Domain-Adapted Large Language Models"**. This is a GPU allocation request (4× NVIDIA B200) and potential conference paper draft.

The document is written primarily in Thai with English section headers and technical terms. Maintain this bilingual style throughout all edits.

---

## 1. Content Corrections (Factual Fixes)

These are errors that must be fixed based on the actual codebase:

### Abstract / Section 4
- **Multi-turn conversation count**: The abstract says "8,887 บทสนทนา" — the actual number is **8,442 unique conversations** (16,085 multi-turn rows). Change to "8,442 บทสนทนา".
- **Dataset total**: The document says "123,447 ตัวอย่าง" for training — the actual file has **123,447 train + 6,498 eval = 129,945 total**. The 123,447 number is correct for training rows. Keep it, but mention the eval split explicitly: "123,447 ตัวอย่างฝึกสอน และ 6,498 ตัวอย่างประเมิน (รวม 129,945)"
- **"15 แหล่ง" claim in abstract**: Verify this. The actual distinct origins in the training data are 50+. The "15 แหล่ง" likely refers to the 15 RAG corpus sources (which is correct for RAG). Clarify: say "15+ แหล่งข้อมูล RAG" and "50+ แหล่งข้อมูลฝึกสอน" separately.
- **Table 2 (Dataset Overview v3.2)**: The totals 123,447 / 6,498 / 129,945 are correct. But the component breakdown (Pentest QA 41,943 / External Security 42,000 / Code SFT 21,990 / Agentic 24,012) should be verified — these are round numbers that look estimated. The actual breakdown by major category from the data:
  - Pentest/Security QA (writeups, A-series, main, wrn, knowledge): ~27K
  - External Security (ext_fenrir, ext_exploitdb, ext_cyberstrike_*, external_*): ~52K  
  - Code SFT (magicoder, glaive, codefeedback, self_oss, code_exercises): ~21K
  - Agentic/Multi-turn (v30r2_agentic, gen_multiturn_*, v30_multiturn, A2_trajectory, rag_trajectory, debug_scenario): ~22K
  
  Adjust Table 2 to match these actual numbers or add a footnote that these are approximate groupings.

### Section 3.5 (RAG Knowledge Base)
- **RAG chunks**: The document says "547,118 chunks" — this is correct per build output.
- **Figure 4 bar chart**: The numbers match the actual build output (NIST NVD 247,199 / CyberStrike 120,966 / Fenrir 99,749 / ExploitDB 46,505 / HackTricks 13,044 / SFT Knowledge 12,572 / GTFOBins+Atomic+Others 7,083). These are correct.

### Section 4.5 (Multi-Turn Conversation Generation)
- The document says "7 ประเภท" — the actual types are: Full Pentest, Tool Calling, Error Recovery, RAG-Assisted, Lateral Movement, Web App Exploit, CTF Challenge. That is 7. Correct.
- The table shows models as Qwen3-32B and DeepSeek V3.2 — verify these are accurate for what was actually used in generation.

### Section 10 (References)
- Reference 7: "Benchmarking Practices in LLM-driven Offensive Security, arXiv:2504.10112, 2025" — verify this arXiv ID actually exists. Several references came from GPT o3 output that may have fabricated citation IDs. **All 7 references need arXiv ID verification.** Known valid ones:
  - [1] xOffense: arXiv:2509.13021 ✓
  - [2] PentestGPT: arXiv:2308.06782 ✓  
  - [4] AutoPenBench: arXiv:2410.03225 ✓
  - [3] VulnBot: arXiv:2501.13411 — needs verification
  - [5] PentestAgent: arXiv:2411.05185 — needs verification
  - [6] arXiv:2410.17141 — needs verification
  - [7] arXiv:2504.10112 — needs verification

---

## 2. Structural / Content Additions

### 2.1 Add Section: Ethical Considerations (ส่วนที่ขาด — สำคัญมาก)
Insert as **Section 8** (shift Contributions to 9, Timeline to 10, References to 11). This section is critical for a security-related research proposal. Include:

- **Responsible disclosure**: ระบบถูกออกแบบให้ใช้ภายในองค์กรเท่านั้น ไม่มีช่องทางเผยแพร่ exploit สู่ภายนอก
- **Rules of Engagement enforcement**: Broker layer บังคับ scope/target ก่อนทุก action
- **Human-in-the-loop**: high-risk actions ต้องได้รับอนุมัติจากผู้ปฏิบัติงาน
- **Dual-use concern**: อธิบายว่าเหตุใดระบบนี้จึงไม่เป็นอาวุธ — ต้องมี authorized scope, ทำงานภายใน sandbox, มี audit trail, ไม่มี autonomous exploitation ที่ไร้การควบคุม
- **Data ethics**: training data มาจาก public writeups, open-source repos, และ publicly disclosed CVEs เท่านั้น ไม่มี private exploit data
- **Access control**: แนะนำว่าระบบจะ deploy เฉพาะบนเครือข่ายที่แยกจากระบบ production

### 2.2 Add Section: Cost-Effectiveness Analysis
Insert as subsection under GPU Resources (6.5) or as standalone comparison. Include:
- **เปรียบเทียบค่าใช้จ่าย**: 
  - GPT-4o API สำหรับ pentest engagement 1 วัน (est. 100K+ tokens) ≈ $30-100
  - PentestAI self-hosted inference (Qwen3-32B on single GPU) ≈ $0 marginal cost per engagement
  - Professional pentest firm: $15,000-50,000 per engagement
- **ROI argument**: training cost (80-150 GPU-hours on B200) is one-time; inference is free forever on-premises
- Frame as "Cybersecurity Poverty Line" argument — SMEs that can't afford $15K pentests can run this internally

### 2.3 Expand Introduction (Section 1)
The current introduction is only 2 short paragraphs. Strengthen it:
- **Opening hook**: Start with the "Cybersecurity Poverty Line" concept — the gap between organizations that can afford professional pentesting and those that cannot
- **Problem statement**: เพิ่มสถิติ — จำนวนการโจมตีทางไซเบอร์ที่เพิ่มขึ้น, สัดส่วน SMEs ที่ถูกโจมตี, ราคาของ professional pentest services
- **Research gap**: อธิบายว่าเหตุใด existing solutions (PentestGPT, VulnBot, xOffense) ยังไม่ตอบโจทย์ — proprietary API dependency, no safety mechanism, LoRA-only fine-tuning
- **Research Questions**: เพิ่ม RQ1-RQ3 อย่างชัดเจน:
  - **RQ1**: Full SFT domain adaptation สำหรับโมเดลขนาด 32B ให้ประสิทธิภาพเหนือกว่า LoRA fine-tuning ในงาน penetration testing หรือไม่?
  - **RQ2**: สถาปัตยกรรม safety-first agent (Broker + Sandbox + Injection Guard) สามารถป้องกัน uncontrolled exploitation ได้อย่างมีประสิทธิภาพหรือไม่?
  - **RQ3**: ระบบ self-hosted ที่ใช้โมเดลขนาดเล็กสามารถ cost-effective เพียงพอสำหรับ SMEs หรือไม่?

### 2.4 Strengthen Related Work (Section 2)
- The current Table 1 is good but needs **narrative analysis** after it — not just "xOffense shows domain adaptation works". Add 2-3 paragraphs analyzing:
  - Why prompting alone fails (PentestGPT 9.09%, GPT-4o 21.21%)
  - Why LoRA is limited (only <1% parameters changed, potential for catastrophic forgetting in multi-task scenarios)
  - What PentestAI contributes beyond xOffense (Full SFT, safety architecture, RAG, curriculum learning)
- Add a "Research Gap" subsection explicitly listing what no existing work covers

---

## 3. Diagrams to Add

### 3.1 Training Data Pipeline Diagram (Section 4)
Create a flowchart showing:
```
Raw Sources → Quality Filters → Format Conversion → Post-Processing → Curriculum Assembly
     │              │                    │                    │                  │
  Writeups      Min length         System prompt        Dedup by MD5      4-Phase ordering
  CVE data      Refusal filter     Think blocks         Provenance tags    Epoch 1: curriculum
  Datasets      Language filter    Tool-call format     Metadata inject    Epoch 2: shuffled
```

### 3.2 Data Flow / Inference Pipeline Diagram (Section 3)
Add a sequence diagram showing a complete pentest interaction:
```
User → Agent Loop → Prompt Compiler (6 layers) → Qwen3-32B → Response Parser
                                                                    │
                                                          ┌────────┼────────┐
                                                       <think>   text   <tool_call>
                                                          │        │        │
                                                       (log)   (display)  Broker
                                                                            │
                                                                    ┌───────┼───────┐
                                                                  RoE    Scope   Injection
                                                                  check  check   Guard
                                                                            │
                                                                        Sandbox
                                                                            │
                                                                      Tool Output
                                                                            │
                                                                    Evidence Store
```

### 3.3 Curriculum Learning Visualization (Section 4.6)
Improve Figure 5. Instead of just code block, create a visual diagram showing:
- 4 phases as stacked bars or a progression chart
- Arrow showing difficulty progression (easy → hard)
- Row counts per phase
- A small line showing the learning curve concept

### 3.4 Evaluation Framework Diagram (Section 7)
Add a diagram showing:
```
PentestAI ─┬─ AutoPenBench (33 tasks, 2 levels)
            │        ├─ In-vitro (isolated)
            │        └─ Real-world (networked)
            │
            ├─ AI-Pentest-Benchmark (13 machines, 152 subtasks)
            │
            └─ Ablation Studies
                     ├─ Full SFT vs LoRA (r=16,32,64)
                     ├─ With/Without RAG
                     ├─ With/Without Curriculum
                     └─ Multi-turn ratio (0%, 10%, 20%)
```

---

## 4. UI / Layout Improvements

### 4.1 Cover Page
- The cover page looks good but add **university/institution affiliation** if applicable
- Add a subtle **version badge** or QR code linking to the project repo (if public)
- Consider adding a 1-sentence English tagline under the Thai abstract

### 4.2 Table Styling
- All tables use consistent styling (good). But some tables have Thai headers and some English — standardize to bilingual headers (Thai primary, English in parentheses)
- Table 1 (Related Work) — add a column for "Safety Mechanism" to highlight PentestAI's unique contribution
- Table 5 (Multi-Turn Types) — add a "Count" column showing actual generated conversation counts per type

### 4.3 Figure Improvements
- **Figure 1 (AutoPenBench bar chart)**: Add PentestAI's target score as a dashed/projected bar (e.g., "PentestAI (target) >75%") to visually show the goal
- **Figure 2 (Architecture)**: The current wireframe-style diagram is functional but could use color coding:
  - Tier 1 (Reasoning): warm color (orange/amber)
  - Tier 2 (Execution): neutral (gray/blue)  
  - Tier 3 (Knowledge & Isolation): cool color (green/teal)
- **Figure 4 (RAG composition)**: Good as-is. Add total at the bottom of the chart.

### 4.4 Page Layout
- Several pages have large empty spaces (pages 5, 7, 8, 9, 10 bottom halves are mostly blank). Redistribute content or add the new diagrams to fill these gaps
- Consider adding margin notes or callout boxes for key statistics (e.g., "547K chunks", "123K training samples", "8,442 conversations")
- Add page breaks more intentionally — each major section should start on a new page

### 4.5 Typography
- The code blocks (Figures 3, 5, Section 6.3) use a good monospace font. Ensure consistent sizing
- Section headers use Thai + English bilingual naming which is excellent — maintain this throughout
- Consider adding a **Table of Contents** after the abstract

---

## 5. Wording & Language Improvements

### 5.1 Abstract Rewrite
The abstract is dense and could be restructured. Suggested structure:
1. **Problem** (2 sentences): Cybersecurity gap, SMEs can't access professional pentesting
2. **Approach** (2-3 sentences): PentestAI, Full SFT on Qwen3-32B, self-hosted
3. **Key contributions** (bullet-friendly): dataset, curriculum learning, safety architecture, RAG
4. **Expected results** (1-2 sentences): target AutoPenBench score, cost comparison

### 5.2 Specific Wording Fixes
- Abstract line 1: "การทดสอบเจาะระบบ (penetration testing) เป็นกระบวนการที่ขาดแคลนผู้เชี่ยวชาญอย่างรุนแรง" — this is a strong opening but could add a statistic
- Section 1, paragraph 2: "จึงเหมาะสำหรับองค์กรที่มีข้อกำหนดด้านความลับของข้อมูลสูง" — also mention cost savings, not just data sovereignty
- Section 3.3: "Injection Guard — สแกน tool output เพื่อตรวจจับ prompt injection" — add mention of "cross-turn taint tracking" as a distinctive feature
- Section 8 (Contributions): Reorder — put "Safety-First Agent Architecture" as #2 (right after Full SFT) since it's a key differentiator
- Throughout: Some sentences mix English and Thai awkwardly. Keep technical terms in English but ensure Thai sentence flow is natural

### 5.3 Academic Tone
- The document sometimes reads like a technical report rather than an academic proposal. Add:
  - More citations within the text body (e.g., "as demonstrated by [1]", "following the methodology of [4]")
  - Formal hypothesis statements for each RQ
  - More precise language: "approximately" → "ประมาณ", quantify claims where possible

---

## 6. Sections to Cut or Condense

- **Section 3.2 (Agent Loop & Tool Calling)**: The example in Figure 3 is helpful but the surrounding explanation could be shorter. The `<think>` / `<tool_call>` format is implementation detail — condense to 3-4 lines + the example
- **Section 6.3 (Training Configuration)**: The code block is fine for a technical appendix but excessive for the main body. Consider moving to an appendix or condensing to a table
- **Section 3.5 continued (Parsing Methodology, p.7)**: This is very implementation-specific. Condense the 4 bullet points about different parsing strategies into a single paragraph or table

---

## 7. Appendix Suggestions

Consider adding appendices:
- **Appendix A**: Full list of training data sources with row counts
- **Appendix B**: Sample multi-turn conversation (1 complete example showing the agent performing recon → exploit → post-exploit)
- **Appendix C**: Prompt layer examples (show what each of the 6 layers looks like in practice)
- **Appendix D**: Tool schema definitions (the actual JSON schemas for the 5 security tools)

---

## Summary of Priority Changes

**Must-fix (ต้องแก้)**:
1. Fix multi-turn count: 8,887 → 8,442
2. Add Ethical Considerations section
3. Verify all arXiv reference IDs
4. Fill empty page spaces with new diagrams

**Should-add (ควรเพิ่ม)**:
5. Expand Introduction with RQ1-RQ3 and statistics
6. Add Training Data Pipeline diagram
7. Add Cost-Effectiveness analysis
8. Add Table of Contents
9. Strengthen Related Work narrative

**Nice-to-have (ถ้ามีเวลา)**:
10. Color-code architecture diagram
11. Add appendices with examples
12. Add projected PentestAI bar to Figure 1
13. Standardize bilingual table headers
