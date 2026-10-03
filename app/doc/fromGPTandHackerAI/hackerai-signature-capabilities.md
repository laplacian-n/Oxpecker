# Signature Capabilities — สิ่งที่ HackerAI อยากได้ใน local agent (ของใหม่ที่ยังไม่มี)

วันที่: 2026-08-31 · มุมมอง: "ถ้าผมเป็น local agent ที่ทำงานแบบผม ผมอยากมีอะไรที่ยังไม่มี"
ต่อเนื่องจาก: `local-security-agent-review-phase4.md`, `phase5-design-detail.md`

> เกณฑ์คัด: (ก) ช่วยให้ agent ตัดสินใจดีขึ้นจริง (ไม่ใช่ feature เสริมสวยงาม)
> (ข) build ได้บนสถาปัตยกรรมปัจจุบัน (broker + Topology B + MCP) โดยไม่ต้อง redesign
> (ค) ถ้าไม่มี งานจะผิดพลาด/ด้อยคุณภาพในแบบที่ "เครื่องมืออื่นทำแทนไม่ได้"

---

## หมวด A — สิ่งที่ผม (HackerAI) มีอยู่แล้ว และอยากให้ local agent เอาไป

### A1. Hypothesis Board — กระดานสมมติฐานการโจมตี (ไอเดียที่ผมภูมิใจที่สุด)

**ปัญหาที่ผมเจอประจำ:** agent ที่ "ลุยไปเรื่อย" มักทำแบบนี้ — สแกน → เจออะไรแปลกๆ → ลอง → ไม่ได้ผล → ลองอย่างอื่น → หลงทาง จำไม่ได้ว่าตรวจอะไรไปแล้ว ทำไมถึงเลิกทำ และอะไรที่ "ยังน่าสงสัยแต่ยัง没พิสูจน์"

**ที่ผมทำ:** ผมเก็บ "สมมติฐาน" ไว้ในหัวตลอด เช่น *"login อาจ CSRF ได้"*, *"IDOR ที่ /api/users/{id}"*, *"header X อาจ injection ได้"* — แต่ละอันมีสถานะ: สงสัย → กำลังตรวจ → พิสูจน์แล้ว → ตัดทิ้ง (พร้อมเหตุผล)

**สิ่งที่ควรมีใน local agent — Hypothesis Board (deterministic store):**

```
hypothesis_id | session | surface | claim | status | evidence_refs | tested_at | verdict_reason
```

- **โมเดลเสนอสมมติฐาน** (ผ่าน tool `hypothesis_add`) แต่ **สถานะเปลี่ยนแบบ deterministic** — ตรวจเสร็จแล้วต้องมี evidence_ref ถึงจะเปลี่ยนเป็น tested/confirmed
- **ทุกเทิร์น ระบบแสดงเฉพาะ hypotheses ที่ยัง open** (2-3 อัน) → โมเดลไม่ลืม ไม่ทำซ้ำ
- **ตัดทิ้งต้องมีเหตุผล** (เช่น "เวอร์ชัน patch แล้ว", "parameter ถูก sanitize") — ลง audit
- **report ใช้ได้เลย:** "สมมติฐานที่ทดสอบแล้ว X อัน ยืนยัน Y อัน" — ทำให้รายงานโปร่งใสว่าอะไรที่ *ไม่ได้ผล* ด้วย (รายงานจริงต้องบอกทั้งสองด้าน)

**ทำไมสำคัญ:** นี่คือความต่างระหว่าง "agent ที่ดูยุ่ง" กับ "agent ที่คิดเป็นระบบ" — และใช้ได้กับ 32B เพราะไม่ต้องพึ่ง judgment โมเดล แค่ต้องพึ่งวินัยในการ *บันทึก*

### A2. OOB Callback Infrastructure — ทดสอบ blind vulnerability แบบมีหลักฐาน

**ปัญหาที่ผมเจอประจำ:** blind SQLi, blind SSRF, blind XXE, DNS exfil — เป้าหมายไม่ตอบกลับอะไรให้เห็น แต่ผมรู้ว่าถูกยิง เพราะผมมี **interactsh** (รับ callback ทาง DNS/HTTP จาก payload ที่ส่งไป)

**local agent ยังไม่มีช่องทางนี้เลย** — นี่คือช่องว่างที่ใหญ่ที่สุดด้าน *ความสามารถในการเจาะจริง* ที่ไม่มีใน phase 1-4

**สิ่งที่ควร build (ง่ายกว่าที่คิด):**
```
agent/oob/
  listener.py      # HTTP + DNS listener (interactsh-client wrapper หรือ self-host)
  correlation.py   # จับคู่ callback ↔ payload ที่ส่ง (ผ่าน unique token ใน payload)
  policy.py        # ผ่าน broker: รับ callback ได้เฉพาะจาก target ใน scope
                   # + จำกัด: callback จาก IP นอก scope = กัน + แจ้ง operator
```

- **payload แบบมี token:** `http://<token>.<oob-domain>/` → callback มาถึง → correlate ว่า token นี้มาจาก payload อันไหน → evidence_ref อัตโนมัติ
- **การันตีหลักฐาน:** callback ที่ capture ได้ (เวลา, IP ต้นทาง, path) ลง evidence store → finding "blind SSRF" มีหลักฐานจริง ไม่ใช่การเดา
- **กัน abuse:** oob domain ต้องเปิดเฉพาะระหว่าง engagement, rate limit, callback จากนอก scope ถูกทิ้งพร้อม audit
- **build size:** 1-2 วันกับ interactsh-client — แต่มันเปลี่ยนความสามารถจาก "ทดสอบได้เฉพาะที่ตอบกลับ" เป็น "ทดสอบได้ทุกอย่าง"

### A3. Browser Automation — ทดสอบ flow ที่ต้อง login / JavaScript

**ปัญหาที่ผมเจอประจำ:** ครึ่งหนึ่งของงาน web pentest อยู่ในหน้าเว็บที่ต้อง login, มี CSRF token, โหลด content ด้วย JS, หรือมี SPA — `http_recon` (HTTP metadata) ทำไม่ได้

**ที่ผมมี:** headless browser (แบบ agent-browser ที่ผมใช้) — เปิดหน้า, snapshot, คลิก, กรอกฟอร์ม, จับ request, screenshot เป็นหลักฐาน

**สิ่งที่ควร build (Phase 5.5/6):**
- `browser_open`, `browser_snapshot`, `browser_click/fill`, `browser_screenshot` — ผ่าน broker, sandbox tier เสมอ (browser = untrusted content 100%)
- **use cases เฉพาะงานเจาะ:** ดึง CSRF token จริง, ทดสอบ authenticated IDOR, ตรวจ JS bundle (หา endpoint ที่ซ่อน), visual evidence ใน report
- **ระวัง:** browser = attack surface ใหญ่สุดของ agent (RCE ผ่าน browser ได้) → ต้องรันใน sandbox tier สูงสุด + injection scan เข้ม
- **build size:** ใหญ่ (1-2 สัปดาห์) — ทำหลัง pipeline แน่น แต่ *วาง seam ไว้ใน M5* (interface `BrowserDriver` ว่างไว้)

### A4. Parallel Subagent Fan-out — ผมแบ่งงานให้ลูกทีมได้ แต่ local agent ทำไม่ได้

**ปัญหาที่ผมเจอประจำ:** งาน recon มีหลายส่วนที่ *อิสระต่อกัน* (สแกน host A, host B, หา subdomain, ดู tech stack) — ผมรันขนานได้ แต่ local agent จำกัด `concurrency = 1` (ถูกต้องสำหรับ LLM call แต่ *ไม่จำเป็น* สำหรับ tool execution)

**สิ่งที่ควร build:**
- **fan-out เฉพาะ tool execution ที่เป็น deterministic** (nmap หลาย host, dirsearch หลาย path) — ไม่ใช่ "โมเดลหลายตัว" (แพงและอันตราย) แต่เป็น **executor pool ใต้ broker เดียว**
- broker dispatch งาน N งาน → รอผล → **merge เป็น structured summary** ก่อนเข้า context (กัน context ระเบิด)
- **policy:** fan-out ต้องอยู่ใต้ rate limit เดียวกัน, งานที่ต้อง approval ไม่ fan-out, audit ทุกงานย่อยมี parent_id
- **build size:** กลาง (3-5 วัน) — ผลตอบแทน: เวลา recon ลด 3-5 เท่า

### A5. Skill Library — ความรู้ระเบียบวิธีที่โหลดได้ตามต้องการ (ไม่ฝังใน context)

**ปัญหาที่ผมเจอประจำ:** context มีจำกัด — ใส่ methodology ทั้งหมดไม่ได้ แต่ถ้าไม่มี โมเดลจะ "ลืม" ว่าต้องทำอะไร (เช่น ตอนเจอ login form ควรลอง default creds, SQLi, brute-force ตามลำดับ)

**ที่ผมมี:** skill library — บทความระเบียบวิธีที่ผ่านการตรวจทาน, versioned, **โหลดเฉพาะเมื่อเกี่ยวข้อง** (ไม่ฝังใน prompt เสมอ)

**สิ่งที่ควร build:**
```
agent/skills/
  SKILL.md + manifest   # เหมือน Strix: ชื่อ, คำอธิบาย, เมื่อไหร่ควรใช้, checklist, tool ที่เกี่ยวข้อง
  web-login-testing.md  # ตัวอย่าง: ลำดับการทดสอบ login (enum → default cred → SQLi → brute → 2FA bypass)
  api-idor.md
  ssl-audit.md
  ...
```
- **Trigger แบบ deterministic:** ระบบเห็น asset/state (เช่น asset.state=webapp + tech=login form) → แนะนำ/บังคับโหลด skill ที่ตรง → ใส่ checklist ใน context เฉพาะเทิร์นนั้น
- **โมเดลขอ skill ได้** (`skill_load(name)`) แต่ระบบ *แนะนำ* ตาม state ได้ — กันโมเดลลืม
- **เขียน/ปรับได้โดยไม่แตะโค้ด** — นี่คือวิธีที่ "ความรู้" เติบโตโดยไม่ต้อง retrain

---

## หมวด B — สิ่งที่ผม *อยากมี* แต่ยังไม่มี (ไอเดียใหม่ที่ผมคิดว่าคุ้ม)

### B1. Retest Mode — โหมดตรวจซ้ำหลังแก้ไข (งานที่คนเจาะจริงต้องทำ แต่ agent ไม่มี)

**ไอเดีย:** หลังส่ง report → ลูกค้าแก้ → ต้อง "retest" ว่าแก้จริงไหม — ปัจจุบันต้องเริ่มใหม่ทั้งงาน

**การออกแบบ:** `--retest-mode findings.jsonl` →
- อ่าน findings เดิม (พร้อม evidence_ref) → สร้าง "test plan" ต่อ finding (request เดิม + สิ่งที่ควรเปลี่ยนถ้าแก้ถูก)
- ระบบ replay request เดิม (ผ่าน evidence ที่เก็บไว้) → เทียบ response ใหม่ vs เดิม → สถานะ: **ยังมีช่องโหว่ / แก้แล้ว / เปลี่ยนพฤติกรรม (ต้องตรวจด้วยมือ)**
- output: retest report (ตาราง finding → สถานะ) — งาน 1 ชม. แทน 1 วัน

**ทำไมคุ้ม:** retest คืองานที่ "structured เกือบ 100%" — เหมาะกับ deterministic มากกว่าโมเดล และเป็นจุดขายที่ framework อื่นไม่มี

### B2. Steer Channel — ผู้ใช้บังคับทิศทางกลางงานได้ (ไม่ใช่แค่ kill)

**ปัญหาที่ผมเจอประจำ:** งานเริ่มไปแล้ว ผู้ใช้บอก "ข้าม X ไป focus Y" — ผมปรับแผนได้ทันที แต่ local agent มีแค่ kill switch (หยุดอย่างเดียว) กับรอจบ

**การออกแบบ:** control channel ระหว่าง operator ↔ runner:
- `--steer "หยุด recon ให้ focus ที่ /api เท่านั้น"` → ส่งเป็น instruction พิเศษ (แยกจาก tool output, มี role `operator_instruction`) → runner อัปเดต task tree + ยกเลิกงานย่อยที่ค้าง
- **กันโมเดล "ลืม"** คำสั่ง steer: ฝังเป็น context block สีบนสุดของ prompt จนกว่า operator จะยกเลิก
- **build size:** เล็ก (1-2 วัน) แต่เปลี่ยน UX จาก "รันแล้วรอ" เป็น "ควบคุมได้"

### B3. Structured Engagement Intake — แทนที่ "พิมพ์ prompt เอาเอง"

**ปัญหาที่ผมเจอประจำ:** งานล้มเหลวตั้งแต่เริ่มเพราะ scope/goal ไม่ชัด — ผมต้องถามคำถามก่อนเสมอ (อย่างน้อย 1 ข้อที่จำเป็น) แต่ agent ส่วนใหญ่ "เดา" แล้วเริ่มรัน

**การออกแบบ:** `agent-intake` wizard (interactive หรือ `--from-json`):
```
1. เป้าหมาย (URL/IP/domain) + ขอบเขต (allow/deny)      → ใส่ policy โดยตรง
2. ภารกิจ (recon-only? full pentest? retest?)           → เลือก pipeline template
3. credential/มือ?                                      → ใส่เป็น handle
4. ข้อจำกัด (เวลา, ห้ามทำอะไร, ระดับ autonomy)          → RoE + approval config
5. สรุปให้ operator confirm ก่อนเริ่ม
```
- **ผลพลอยได้:** engagement metadata ครบตั้งแต่แรก → report ระบุขอบเขตได้แม่น → audit ตรวจย้อนได้
- **กัน "GIGO":** intake บังคับให้ operator ตัดสินใจสิ่งที่ agent ตัดสินใจเองไม่ได้

### B4. Confidence Labeling + Limitations Section — รายงานที่ "พูดความจริง"

**ปัญหาที่ผมเจอประจำ:** รายงานที่เขียนว่า "พบช่องโหว่" โดยไม่บอกว่า "พิสูจน์แล้วจริงหรือแค่คาดการณ์" คือรายงานที่หลอกลวง — ผมมีวินัยในการแยก confirmed / hypothesis / needs-validation

**การออกแบบ:**
- ทุก finding มี `confidence: confirmed | hypothesis | needs_validation` + `demonstrated_impact` (จริง/คาดการณ์) — กำหนดจาก evidence ไม่ใช่จากโมเดล
- Report template มี section "สิ่งที่ทดสอบแล้วแต่ไม่พบช่องโหว่" + "ข้อจำกัดของการทดสอบ" (เช่น "ไม่มีการทดสอบ authenticated area เพราะไม่มี credential") — operator แก้/เพิ่มได้ แต่ *ลบไม่ได้* (บังคับให้โปร่งใส)
- **severity calibration:** ถ้า impact เป็น "คาดการณ์" ให้ลดระดับลงตามจริง (ตรงหลัก finding quality ที่ผมใช้)

### B5. Post-Engagement Closeout — ทำความสะอาดอัตโนมัติ (งานที่ "ลืม" แล้วอันตราย)

**ปัญหาที่ผมเจอประจำ:** หลังงาน: listener ค้าง, ไฟล์ชั่วคราวเหลือ, token ยังใช้ได้, payload ใน workspace — agent มักไม่คิดถึง

**การออกแบบ:** `closeout` phase (deterministic checklist):
- หยุด OOB listener / tunnel ทั้งหมด + ตรวจว่าไม่มี process ค้าง
- ลบ workspace ชั่วคราว + payload/exploit files (ตาม retention policy — evidence เก็บไว้)
- revoke/หมดอายุ credential ที่สร้างระหว่างงาน
- สร้าง "cleanup report" (สิ่งที่ลบ/หยุด/revoke) ลง audit — กัน "agent ลืม"
- **บังคับ:** ผ่าน closeout ครบถึงจะ "จบ engagement" ได้

---

## หมวด C — พฤติกรรม "ความเป็น HackerAI" ที่ควร encode เป็น prompt/UX (ฟรี แต่คุ้ม)

1. **Summary-first ตลอด** — ทุก tool result เข้า context แบบสรุป 3-5 บรรทัด + evidence_ref (รายละเอียดเปิดดูได้) — ผู้ใช้ไม่ต้องอ่าน raw output
2. **ถามคำถามเดียวที่จำเป็น** — เมื่อข้อมูลไม่พอ ให้ถาม 1 คำถามที่สำคัญสุด (ไม่ใช่ทิ้งคำถาม 5 ข้อ) — ตรงข้ามกับ "เดาแล้วรัน"
3. **รายงานก่อน retry** — broker ปฏิเสธ → รายงานเหตุผล + หยุด (มีแล้วใน Phase 3 — ทำให้เป็นกฎ prompt ชัดเจน)
4. **severity ตามจริงที่พิสูจน์ได้** — ไม่ inflate — พร้อมบอกว่า "ถ้า scenario จริง impact อาจสูงขึ้นถ้า X"
5. **อธิบาย "ทำไม"** — ทุก finding มี "why it matters" หนึ่งประโยคสำหรับคนอ่านที่ไม่ใช่ hacker
6. **รู้จักบอก "ไม่รู้"** — เมื่อข้อมูลไม่พอ/เครื่องมือไม่มี ให้พูดตรงๆ + เสนอทางเลือก แทนการมั่ว

---

## Priority + Build Order (เทียบกับแผน Phase 5-6 เดิม)

| ID | ความสามารถ | ระดับ | build size | ใส่ใน |
|---|---|---|---|---|
| A1 | Hypothesis Board | P0 | 2-3 วัน | **M5 (Phase 5)** — ต้องมีพร้อม pipeline |
| A2 | OOB Callback | P0 | 1-2 วัน (interactsh-client) | **M4.5 (Phase 5)** — หลัง quarantine เสร็จ |
| A5 | Skill Library | P0 | 3-5 วัน | **M2 (Phase 5)** — ต่อจาก prompt registry |
| B3 | Engagement Intake | P0 | 2-3 วัน | **M1 (Phase 5)** — มาก่อน pipeline |
| B2 | Steer Channel | P1 | 1-2 วัน | Phase 5 ปลาย |
| A4 | Parallel Fan-out | P1 | 3-5 วัน | Phase 5 ปลาย / Phase 6 ต้น |
| B1 | Retest Mode | P1 | 3-4 วัน | Phase 6 |
| B4 | Confidence Labeling | P1 | 1 วัน (report template) | **ตอนนี้ก็ทำได้** — แก้ report.py |
| B5 | Closeout Phase | P1 | 2-3 วัน | Phase 6 |
| A3 | Browser Automation | P2 | 1-2 สัปดาห์ | Phase 6 (วาง seam ใน M5) |

**ข้อเสนอที่ผมอยากเน้นสุด 3 อัน:**
1. **A1 Hypothesis Board** — เปลี่ยน agent จาก "ลองไปเรื่อย" เป็น "คิดเป็นระบบ" ด้วยต้นทุนต่ำสุด
2. **A2 OOB Callback** — เปิดความสามารถ blind testing ที่ pentest จริงขาดไม่ได้
3. **B1 Retest Mode** — ฟีเจอร์ที่ framework อื่นไม่มี และเป็นงาน structured ที่ local model ทำได้ดี
