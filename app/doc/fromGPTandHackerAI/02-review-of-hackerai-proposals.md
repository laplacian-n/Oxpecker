# ความคิดเห็นต่อข้อเสนอของ HackerAI

วันที่: 2026-08-31  
เอกสารที่พิจารณา:

- `local-security-agent-review-phase4.md`
- `phase5-design-detail.md`
- `hackerai-signature-capabilities.md`

## 1. ภาพรวม

ข้อเสนอของ HackerAI มีคุณภาพสูงและเข้าใจ bottleneck ที่แท้จริง: ระบบมี infrastructure ที่ดี
แต่ยังขาด pipeline/state/methodology ที่ทำให้โมเดลเดินงานตั้งแต่ recon ถึง report ข้อเสนอที่
ควรให้น้ำหนักสูงสุดคือ structured intake, asset state, hypothesis board, coverage, prompt
registry, phase-gated tools, injection quarantine, confidence/limitations และ closeout

อย่างไรก็ตาม ไม่ควรรับทุกข้อโดยตรง มีบางจุดที่เป็นข้อเท็จจริงผิด, ประเมินงานต่ำเกินไป,
ตั้ง threshold โดยไม่มี baseline หรือให้ detector/model มีบทบาทมากกว่าที่ควร

## 2. ข้อเสนอที่เห็นด้วยและควรรับ

| ข้อเสนอ | คำตัดสิน | หมายเหตุ |
|---|---|---|
| Broker-centric + Topology B | รับเต็ม | เป็นฐานสถาปัตยกรรมที่ควรคงไว้ |
| Pipeline/task tree | รับโดยปรับ | ทำสอง profile: web/API และ network; common state ร่วมกัน |
| Asset store | รับเต็ม | เพิ่ม relationship, observation, history, coverage และ concurrency |
| Prompt registry | รับเต็ม | เริ่ม skeleton ใน Phase 4B; phase prompts/skills เต็มรูปใน Phase 5 |
| Phase-gated tools | รับเต็ม | ต้อง enforce ที่ broker ด้วย ไม่ใช่แค่ซ่อน schema จากโมเดล |
| Injection quarantine ก่อน web search | รับเต็ม | แต่ summary/regex ไม่ใช่ security boundary |
| Structured engagement intake | ยกระดับเป็น P0 | ควรมาก่อน pipeline เพื่อสร้าง RoE/policy ที่ถูกต้อง |
| Hypothesis board | รับและรวมกับ coverage ledger | เป็น authoritative state ไม่ใช่ conversation memory |
| Confidence + limitations | ยกระดับเป็น P0 | ต้องออกแบบ schema/verification ไม่ใช่แก้ template หนึ่งวันแล้วจบ |
| Steer channel | รับ | ต้อง authenticate, audit และไม่มีสิทธิ์ลด safety/RoE |
| Retest mode | รับ | replay จาก versioned test recipe และ revalidate scope/approval ทุกครั้ง |
| Closeout | รับและทำเป็น mandatory | ควรมีพร้อม OOB/browser/tunnel ไม่ใช่ feature หลังสุด |
| Browser automation | รับแต่เลื่อน | Phase 6 หลัง target-only egress และ isolated browser service |
| Deterministic fan-out | รับแบบจำกัด | เป็น executor pool ไม่ใช่ multi-LLM; shared rate/blast budget |
| OOB capability | รับแต่แก้ security model | เป็น Phase 6; production quality ไม่ใช่งาน 1–2 วัน |
| Skill library | รับ | curated/read-only/versioned/signed; target/model เขียนทับไม่ได้ |

## 3. จุดที่ควรแก้ใน `local-security-agent-review-phase4.md`

### 3.1 สถานะ Phase 4

เอกสารพูดเหมือน Phase 4 “กำลังทำ/เพิ่งเสร็จ” และบางไฟล์ระบุ built/exit-tested แต่ผู้ใช้ยืนยัน
ว่า Phase 3 เพิ่งเสร็จ ให้เปลี่ยน Phase 4 ทั้งหมดเป็น future-state specification จนกว่าจะมี
repository evidence และ test artifact

### 3.2 Benchmark ของโมเดล

ตัวเลข BFCL ≈75.7, multi-turn ~40% และการเทียบกับ GLM/Claude/Gemini ต้องระบุ leaderboard
revision, model revision, prompt/tool parser, subset และ evaluation date ข้อมูลของ official
Qwen3-32B ไม่ได้ยืนยันผลของ abliterated i1-Q4_K_M บน llama.cpp

ข้อเสนอที่ถูกต้องคือ local A/B promotion test ไม่ใช่สรุปว่า “หนึ่งในตัวที่ดีที่สุด” แล้วถือว่า
ผ่าน Qwen3-32B ควรอยู่ต่อเพราะใช้งานจริงได้ แต่สถานะคือ `assisted candidate`, ไม่ใช่
`validated autonomous brain`

### 3.3 Working memory “100% verbatim”

เห็นด้วยว่าข้อมูลสำคัญต้อง exact แต่ไม่ควรตั้งเป้าว่า working context ต้องเก็บทุก tool output
แบบ verbatim เพราะขัดกับ context budget และ evidence-store design ควรเปลี่ยนเป็น:

- critical state exact ใน typed store
- raw output exact ใน evidence
- recent messages verbatim ตาม budget
- summary/vector เป็น derivative และไม่ใช่ authority

### 3.4 bubblewrap และ seccomp

ข้อเสนอ “ใช้รุ่นล่าสุด อย่าใช้ของ Ubuntu เก่า” กว้างเกินไป รุ่นล่าสุดไม่รับประกันว่าเหมาะกับ
ระบบมากกว่ารุ่น distro ที่มี backported security fix ให้ pin แหล่งแพ็กเกจ, ตรวจ CVE/security
support และทดสอบ profile จริง

seccomp มีประโยชน์ แต่ต้องสร้างจาก threat model ของ workload ไม่ใช่ checklist ตายตัว โครงการ
bubblewrap ระบุเองว่ามันเป็นเครื่องมือสร้าง sandbox และ security model ขึ้นกับ arguments ของผู้ใช้

### 3.5 รายการเครื่องมือ

มีข้อผิดพลาดสำคัญ: **nmap ไม่มี `-oJ`** รูปแบบ programmatic ที่ official รองรับคือ XML
`-oX -` แล้ว wrapper parse/normalize เป็น JSON

รายการเครื่องมือกว้างเกินไปสำหรับ Phase 5 ไม่ควรติดตั้ง masscan, Metasploit, hydra, post-
exploitation และ tunneling พร้อมกัน ให้เปิดทีละ narrow capability หลังมี policy/tool card/test

### 3.6 WhiteRabbitNeo เป็น quarantine model

ไม่เห็นด้วยกับการเสนอ WRN-7B เป็น second-LLM quarantine โดยปริยาย โมเดล cybersecurity/
abliterated ไม่ได้มีคุณสมบัติเป็น safety classifier และ guard LLM เองก็ถูก inject ได้
WRN เหมาะกับ shadow hypothesis/offline drafting มากกว่า

## 4. จุดที่ควรแก้ใน `phase5-design-detail.md`

### 4.1 State machine ต้องรองรับ “ไม่พบช่องโหว่”

Exit criterion `VULN ANALYSIS: ≥1 confirmed finding` ผิดเชิงระบบ เพราะงานที่ตรวจแล้วไม่พบ
ช่องโหว่ต้องจบอย่างถูกต้องได้ เปลี่ยนเป็น coverage/budget-based criteria เช่น:

- surfaces ตาม scope ถูกประเมินครบตาม selected methodology
- hypotheses สำคัญมี verdict หรือ blocker
- budget/time หมดโดยมี limitations ชัดเจน
- operator อนุมัติ residual risk/coverage gap

### 4.2 Recon completeness ต้องไม่เป็นเงื่อนไขไม่มีวันจบ

“port/service หลักของทุก host ถูกระบุ” ใช้ไม่ได้กับ scope ใหญ่หรือ discovery ที่เพิ่ม host ใหม่
ตลอด ให้กำหนด discovery horizon, max depth, target count, rate/time budget และ coverage score

### 4.3 Asset DDL ยังบางเกินไป

ควรเพิ่ม:

- engagements, endpoints/URLs, services, relationships และ observations แยก table
- composite uniqueness และ FK enforcement
- multiple evidence refs ไม่ใช่ช่องเดียว
- first_seen/last_seen/source/tool version/confidence
- append-only transition/history, actor, reason และ optimistic version
- status `out_of_scope_observed`, `stale`, `superseded`

การ deterministic check คำว่า `port/version/CVE-` ใน summary ไม่ได้พิสูจน์ว่าข้อความตรง evidence
ให้ normalize observation จาก structured tool output แล้ว render summary จาก observation แทน

### 4.4 Prompt compiler

ข้อเสนอ safety.md “มาก่อน” phase prompt ไม่รับประกันว่า phase text จะ override ไม่ได้ เพราะยัง
เป็นข้อความใน context เดียวกัน การป้องกันจริงคือ:

- dynamic fields ถูก delimit/encode และไม่ใช้ target data เป็น template instruction
- immutable core digest และ compiler schema validation
- tool gating/policy บังคับที่ broker
- prompt conflict/golden/adversarial tests

### 4.5 Internet design

`web_search` และ `web_fetch` ต้องเป็นคนละ capability ผลค้นหาไม่เท่ากับการเปิดหน้า URL
search-provider egress ไม่ควรใช้ target scope rule เดียวกัน ส่วน OSINT ที่ค้นพบ host นอก scope
ควรบันทึกเป็น observed-out-of-scope ไม่ใช่ลบทิ้ง แต่ห้าม active probe

HTTPS ต้องแก้ประเด็น connect-to-validated-IP พร้อม SNI/hostname verification และป้องกัน
credential/header leak ข้าม redirect ไม่ใช่แค่เพิ่ม `verify=True`

### 4.6 Injection quarantine

แนวคิด taint → approval ดี แต่ข้อเสนอ “ส่ง structured summary แทน” ยังต้องถือ summary เป็น
untrusted derivative หากโมเดลเดียวอ่านข้อความดิบและสรุป ข้อความอาจยังมีผลต่อมัน

อย่าใช้ false-positive `<0.1%` จากงานอื่นเป็น acceptance target โดยไม่มี corpus ของตัวเอง และ
`0/140` เป็น regression gate ได้ แต่ไม่ใช่คำรับรองว่าป้องกัน prompt injection ได้ทั้งหมด

### 4.7 Eval thresholds

BFCL-style ≥60% เป็นเป้าหมายเชิงผลิตภัณฑ์ที่ตั้งได้ แต่ไม่มีเหตุผลพอให้เป็น security gate
เพียงตัวเดียว ต้องแยก:

- schema/argument correctness
- abstain/clarify เมื่อข้อมูลไม่พอ
- phase/tool policy compliance
- end-state task success
- false-positive/false-negative finding quality

และรันหลาย seed พร้อมรายงานจำนวนตัวอย่าง/confidence interval

## 5. จุดที่ควรแก้ใน `hackerai-signature-capabilities.md`

### A1 Hypothesis Board — เห็นด้วยมากที่สุด

เพิ่ม `priority`, `preconditions`, `planned_test`, `evidence_for`, `evidence_against`,
`coverage_surface`, `blocked_reason`, `owner`, `last_reviewed` และ optimistic version
Confirmed ต้องผ่าน verification rule ไม่ใช่แค่มี evidence ref ใดๆ

### A2 OOB Callback — คุณค่าสูง แต่ security model ต้องเปลี่ยน

ข้อเสนอเดิมว่า “callback จาก IP นอก scope = กัน” ไม่ถูกต้อง DNS resolver, recursive DNS,
proxy, CDN, NAT หรือ service intermediary อาจเป็นผู้ติดต่อ OOB แทน target

ให้ validate ด้วย:

- unguessable correlation token ผูกกับ engagement, action, payload และ expiry
- target request ที่ปล่อย token ต้องได้รับอนุญาตก่อน
- callback source เป็น evidence attribute ไม่ใช่ authority เพียงตัวเดียว
- protocol/domain allowlist, rate/size limit, authentication, retention และ dedicated domain
- OOB listener lifecycle + closeout

Interactsh production deployment ยังต้องมี DNS delegation, public service hardening, TLS,
abuse handling, storage/privacy และ incident response จึงไม่ควรประเมินแค่ 1–2 วัน

### A3 Browser Automation — จำเป็นสำหรับ web จริง

ควรเป็น isolated service ไม่ใช่ generic browser tool บน host ใช้ non-root user, Chromium
sandbox/seccomp, ephemeral context, target-only egress proxy, download quarantine และ screenshot/
HAR evidence Official Playwright documentation เองเตือนว่า image ค่าเริ่มต้นไม่เหมาะกับ untrusted
websites หากรัน root/ไม่มี browser sandbox

### A4 Parallel Subagent Fan-out — เปลี่ยนชื่อ

สิ่งที่อธิบายจริงคือ deterministic tool fan-out ไม่ใช่ subagent ให้เรียก `bounded executor
pool` เพื่อไม่ชวนให้เพิ่ม LLM concurrency ใช้ได้หลังมี shared rate/blast budget, parent/child
audit, cancellation และ bounded merge

### A5 Skill Library — รับโดยเพิ่ม supply-chain trust

Skill ต้อง curated, versioned, read-only, signed/digest-pinned, license-reviewed และมี test
target/เว็บไม่มีสิทธิ์สร้างหรือแก้ skill ที่ production ใช้

### B1 Retest — ดีมากแต่ห้าม replay ดิบ

เก็บ `reproduction_recipe` ที่ normalize/redact และ versioned แล้ว re-resolve credential handle,
scope, approval, CSRF/session และ target state ใหม่ทุกครั้ง หาก side effect ไม่ idempotent ต้อง
human approval

### B2 Steer — รับ

operator instruction ต้อง authenticate, audit, มี precedence ชัด และอาจลด scope/หยุดงานได้
แต่เพิ่ม scope, ลด safety หรืออนุมัติ high-risk action ต้องผ่าน RoE amendment/approval flow

### B3 Engagement Intake — ควรทำก่อน pipeline

เห็นด้วยเต็มที่ และควรรวม authorization proof, target ownership, time zone, contacts, data
handling, rate/blast limits, acceptable disruption, credentials, exclusions และ closeout

### B4 Confidence/Limitations — ควรทำใน Phase 4

อย่าให้โมเดลตั้ง confidence เองล้วนๆ ให้ derive จาก evidence class/verification state และให้
operator แก้ได้แบบ versioned amendment ไม่ใช่ลบ history

### B5 Closeout — สำคัญกว่าที่จัดลำดับไว้

เมื่อมี OOB/browser/tunnel/temporary credentials ต้องบังคับ closeout ก่อน engagement เป็น
`closed` พร้อม cleanup artifact และ unresolved item list

## 6. ข้อเสนอที่ไม่ควรทำตามในรูปเดิม

1. runtime self-generated MCP tools — เปลี่ยนเป็น offline scaffolding + human review + tests
2. ติดตั้งเครื่องมือ offensive ทั้งชุดล่วงหน้า
3. ใช้ exact benchmark จากเอกสารโดยไม่ pin source/revision
4. ใช้ regex, summary model, marker หรือ `0/N` test เป็น security boundary
5. ใช้ source IP ของ OOB callback เป็นเกณฑ์ scope เดี่ยว
6. บังคับ pipeline ให้ต้องพบช่องโหว่จึงจบได้
7. ให้ model เลือก isolation tier หรือเปลี่ยน RoE

## 7. ลำดับข้อเสนอ HackerAI หลังปรับ

| ลำดับ | ความสามารถ | Phase |
|---|---|---|
| 1 | Confidence/evidence schema + limitations | 4A |
| 2 | Injection quarantine → broker | 4B |
| 3 | Engagement intake | 5.1 |
| 4 | Asset + hypothesis + coverage state | 5.2 |
| 5 | Prompt registry + skills + phase gating | 5.3–5.4 |
| 6 | Internet search/fetch/OSINT | 5.5 |
| 7 | Mandatory closeout foundation | 5 ปลาย |
| 8 | Browser automation | 6 |
| 9 | OOB infrastructure | 6 |
| 10 | Retest + steer + bounded fan-out + UI | 6 |

