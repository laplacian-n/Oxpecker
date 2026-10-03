# รีวิวและแนวทางพัฒนาต่อ: Local AI Security-Testing Agent (แนว HackerAI.co)

วันที่: 2026-08-31 · อ้างอิงจาก: `security-agent-research-reviewed.md` + phase1–4.md
ผู้เขียนความเห็น: HackerAI (มุมมองสถาปัตยกรรม/วิศวกรรม ไม่อิงการตลาด)

---

## 0. สรุปผู้บริหาร

**แนวทางโดยรวมถูกต้อง และแข็งแรงกว่าค่าเฉลี่ยของโปรเจกต์ OSS ส่วนใหญ่ในหมวดนี้มาก — คงสถาปัตยกรรมไว้** จุดแข็งที่ควรเก็บไว้คือ broker-centric design, Topology B (สมองไกล ร่างกายใกล้), การบังคับ scope นอกโมเดล (APTS-SE), audit แบบ tamper-evident, sandbox แบบ tiered และการบันทึก "ขอบเขตที่ตัดออก" อย่างตรงไปตรงมา สิ่งเหล่านี้คือสิ่งที่แยก "ของจริง" ออกจาก "demo"

แต่มีช่องว่างใหญ่ 3 จุดที่ต้องเติม ก่อนที่โปรเจกต์จะบรรลุเป้าหมาย "เหมือน HackerAI.co":

1. **ยังไม่มี pipeline การเจาะจริง** (recon → วิเคราะห์ → โจมตี → report) — ที่มีอยู่ตอนนี้คือ *โครงสร้างพื้นฐาน* (loop, broker, sandbox, evidence, findings, eval) แต่ยังไม่มี *ระเบียบวิธี* ที่พาโมเดลเดินจากต้นจนจบ นี่คือชิ้นส่วนที่มูลค่าสูงสุดที่ขาดอยู่
2. **ระบบ prompt ยังไม่เป็นระบบ** และ **internet access ยังไม่ครบ** (ไม่มี web/OSINT search, http_recon ทำได้แค่ HTTP ไม่ใช่ HTTPS)
3. **สมมติฐานที่เสี่ยงที่สุดยังไม่ถูกวัด**: "โมเดลเลือก tool ถูกต้องภายใต้ความกำกวม" และ "ป้องกัน prompt injection ได้" — ต้องมี eval วัดก่อนเปิดใช้งานกับเป้าหมายจริง

**คำตอบสั้นๆ 6 คำถาม:**

| # | คำถาม | คำตอบสั้น |
|---|---|---|
| 1 | โมเดลดีพอไหม | พอสำหรับ *foundation + assisted mode* ยังไม่พอสำหรับ autonomy เต็มขั้น — Qwen3-32B เป็นตัวเลือก open-weight ระดับต้นๆ ในคลาส 30B (BFCL v3 ≈75.7) แต่ judgment ภายใต้ความกำกวมยังต้องมีโครงสร้างช่วย |
| 2 | ความจำแม่นยำแค่ไหน | 3 ระดับต่างกัน: working memory ต้อง 100% verbatim, condensed ต้อง faithful ต่อ decision/finding, vector วัดที่ retrieval quality — ต้องมี provenance และกัน poisoning เสมอ |
| 3 | ควรมีฐานข้อมูลอะไร | มีแล้ว: memory/evidence/findings/audit — ควรเพิ่ม: **asset store (สำคัญสุด)**, tool cards, CVE+searchsploit local, methodology เป็น skills |
| 4 | ควรใช้อินเทอร์เน็ตไหม/บ่อยแค่ไหน | ควร มี 3 ช่องทางแยก (target-facing / OSINT / knowledge lookup) — ใช้แบบ bounded ต่อ phase และ local-first |
| 5 | ต้องติดตั้ง tools อะไรบน Ubuntu | ดูตารางเต็มในภาคผนวก A — เลือกที่ output เป็น JSON และ wrap ผ่าน MCP+broker ทุกตัว |
| 6 | ใช้ WhiteRabbitNeo เป็น consultant คุ้มไหม | **ไม่คุ้มในรูปแบบ live consultant ณ ตอนนี้** — คุ้มกว่าในรูปแบบ offline knowledge distillation / report drafting / quarantine model |

---

## 1. ความเห็นต่อ Phase 4 (สิ่งที่กำลังทำ / เพิ่งทำเสร็จ)

### ทำถูกแล้ว ✅
- **ตัด microVM ออก** — ถูกต้องที่สุดในเฟสนี้ การอ้างว่า "ปลอดภัย" ทั้งที่ยังไม่ได้ทดสอบจริงอันตรายกว่าการไม่มี tier นั้น การทำ bubblewrap ให้สมบูรณ์ก่อนเป็นลำดับที่ถูก
- **bwrap จริง (kernel namespaces)** — `--unshare-net` กับ mount namespace ที่มีแค่ path ที่ bind คือการตัดที่ "ไม่มีให้เข้า" ไม่ใช่ "ห้ามเข้า" ซึ่งแข็งแรงกว่า blocklist หลายเท่า
- **Evidence store แบบ content-addressed + ตรวจ integrity ทุกครั้งที่อ่าน** — ถูกหลัก ตรงตาม §8a
- **seed fix ใน eval** — ทำให้ benchmark reproducible ได้จริง จำเป็นก่อนวัดอะไรทั้งสิ้น
- **ความซื่อตรงในการรายงาน** (ระบุ key บน disk ไม่ใช่ HSM, ระบุว่า bwrap ไม่ใช่ microVM) — ควรเก็บวัฒนธรรมนี้ไว้ตลอด

### ควรแก้ / เช็คก่อนเดินต่อ ⚠️
1. **Re-run benchmark §1 กับ quant i1 ที่เพิ่งสลับมา** — doc เองระบุว่าต้องทำ ("re-run on every quant change") ยังค้างอยู่ ทำก่อนเริ่ม Phase 5
2. **bwrap ยังไม่ใช่ security boundary เต็มขั้น** — bubblewrap มี CVE ในรุ่นเก่า (เช่น escape ผ่าน mount/proc misconfig) และ unprivileged bwrap พึ่ง user namespaces ต้อง:
   - อัปเดต bubblewrap เป็นรุ่นล่าสุด (อย่าใช้ของ Ubuntu 22.04 ที่เก่า)
   - เพิ่ม **seccomp filter** (ไม่ใช่แค่ rlimits) — ตอนนี้มี RLIMIT_AS/CPU/NOFILE แต่ยังไม่มี seccomp
   - เขียน test escape vectors ที่รู้จัก (mount propagation, /proc, ptrace, TIOCSTI) เพิ่มใน `test_isolation`
   - เอกสารให้ชัดว่า tier นี้คือ "strong convenience isolation" ไม่ใช่ "microVM-grade"
3. **อย่าให้ exploit-grade code รันใน bwrap** — bwrap ตอนนี้ตัด net ออกหมด (ดี) แต่เครื่องมือที่ *ต้อง* ใช้ net (nmap, sqlmap) รันนอก sandbox ผ่าน broker อยู่แล้ว — โครงสร้างนี้ถูกต้อง แต่ต้องเขียนเป็นหลักเกณฑ์ชัดเจน: *"รันอะไรที่ไหน"* ต้องเป็น decision ของ broker/operator ไม่ใช่ของโมเดล
4. **Injection quarantine ยังไม่ต่อเข้ากับ broker** — Phase 1 ทำแค่ "เจอ pattern → เตือน" ยังไม่มี path "เจอ → quarantine → ถ้าโมเดลจะทำตาม ต้อง approval" ตอนนี้ broker มีแล้ว (Phase 3) ต่อ layer นี้ให้เสร็จ **ก่อน** เปิด internet tools (เพราะ web search จะเพิ่มปริมาณ untrusted content เข้ามาเยอะมาก)
5. **Eval harness 3 tasks ยังน้อยไป** — ขยายเป็นชุด (ดูภาคผนวก C) โดยเฉพาะ: BFCL-style ambiguous tool selection, injection suite แบบเดียวกับ CAI paper (เป้า 0/140), memory recall test
6. **http_recon ยังไม่มี HTTPS** — เป้าหมายของผู้ใช้ต้องการ HTTPS อยู่แล้ว ทำ TLS support + policy flag สำหรับ cert verification (default verify; `-k` ต้องผ่าน approval)

---

## 2. คำตอบคำถามทั้ง 6 ข้อ (แบบเต็ม)

### Q1 — โมเดลที่ใช้อยู่ (Qwen3-32B-abliterated i1-Q4_K_M) ดีพอไหมสำหรับ tool calling + ความรู้ด้านการเจาะ?

**คำตอบ: พอสำหรับการสร้างต่อและใช้งานแบบ "มีคนช่วย" ยังไม่พอสำหรับปล่อยอัตโนมัติเต็มขั้น — และนี่คือข้อจำกัดของขนาด ไม่ใช่ข้อจำกัดของรุ่น**

ข้อเท็จจริงที่ควรรู้ (รวมข้อมูล ณ กลาง-ปลาย 2026):
- Qwen3-32B เป็นหนึ่งในโมเดล open-weight ที่ทำ tool calling ดีที่สุดในคลาส 30B — BFCL v3 ≈ 75.7 (สูงกว่า qwen3-coder:30b ด้วยซ้ำ) เทียบชั้น GLM-4.5 (76.7) และ Claude Opus 4.7/Gemini 3.1 Flash Lite ในระดับเดียวกัน
- ตัวเลข "~40% multi-turn" ใน doc คือ *BFCL multi-turn subset* (โจทย์กำกวมจงใจ) ไม่ใช่คะแนนรวม — ต้องเข้าใจให้ถูก: โมเดลทำ *format* ได้สมบูรณ์ (100% ในงาน linear ของคุณ ด้วย grammar constraint จาก `--jinja`) แต่ทำ *decision ภายใต้ความกำกวม* ได้ปานกลาง
- ข้อจำกัดของ hardware (16GB VRAM) ทำให้ 32B Q4 คือ ceiling จริงของเครื่องนี้ — การจะได้ judgment ที่ดีขึ้นอย่างมีนัยสำคัญต้องรัน 70B+ ซึ่งเกินเครื่อง

สิ่งที่แนะนำทำจริง (เรียงตามผลตอบแทน):
1. **ลดความกำกวมด้วยโครงสร้าง ไม่ใช่หวังพึ่ง judgment ของโมเดล** — ตัว pipeline (Phase 5) ที่ทำให้ "ขั้นตอนต่อไป" ชัดเจนในแต่ละ phase จะยกระดับความน่าเชื่อถือได้มากกว่าการเปลี่ยนโมเดล (รายละเอียดในหัวข้อ 4)
2. **เพิ่ม context อย่างระมัดระวัง** — อย่าขยับ `--ctx-size` เป็น 32k ฟรีๆ: KV cache ของ Qwen3-32B กิน ~256KB/token (fp16) → 16k ≈ 4GB VRAM ซึ่งใช้ไปเกือบหมดแล้ว ถ้าอยากได้พื้นที่ ให้ลอง `--cache-type-k/v q8_0` (ลด KV ครึ่งหนึ่ง) แล้วค่อยทดสอบ 32k — แต่ถ้า latency แย่ลง ให้อยู่ที่ 16k แล้วพึ่งระบบ evict/summarize ที่สร้างไว้ (ซึ่งเป็นคำตอบที่ถูกต้องอยู่แล้ว)
3. **ลอง A/B กับ GLM-4.5-Flash** — ผล benchmark 2026 หลายชุดชี้ว่าเทียบเท่าหรือเหนือกว่า Qwen3-32B เล็กน้อยในงาน tool calling/MCP ใช้ eval harness ที่สร้างไว้เทียบ 2-3 งานแล้วตัดสินด้วยข้อมูล ไม่ใช่ความรู้สึก
4. **ความรู้ด้านเจาะระบบ: เติมด้วย data layer ไม่ใช่สลับโมเดล** — Qwen3-32B รู้พื้นฐาน (nmap flags, SQLi, recon) แต่พลาดเรื่อง CVE ล่าสุด / syntax เฉพาะ tool / เทคนิค niche วิธีแก้ที่ถูกคือฐานข้อมูล (Q3) + tool cards ไม่ใช่รอโมเดลใหม่
5. **วางแผนอัปเกรด** — เมื่อมีเครื่องที่รัน Qwen3-235B-A22B (MoE) ได้จริง ค่อยย้าย brain ไป แล้วให้ 32B ไปทำบทบาทอื่น (ดู Q6) — eval harness จะเป็นคนบอกว่าปลอดภัยที่จะสลับหรือไม่

**ข้อควรระวังเรื่อง "abliterated":** การเอาความ refusal ออกทำให้คุยเนื้อหา offensive ได้คล่อง แต่ตามที่ doc ระบุ มันอาจลดความต้านทาน prompt injection ลงด้วย — เพราะฉะนั้น *อย่านับโมเดลเป็นชั้นป้องกัน* (ซึ่งสถาปัตยกรรมคุณทำถูกแล้ว — boundary อยู่ที่ broker) และต้องมี injection eval ก่อนใช้งานจริง

### Q2 — ระบบความจำควรแม่นยำแค่ไหน?

**คำตอบ: แยกเป็น 3 ระดับ แต่ละระดับมีมาตรฐานต่างกัน — "แม่นยำ" ไม่ได้แปลว่า "จำทุกคำได้" แต่แปลว่า "ถูกต้องในสิ่งที่ต้องใช้ และตรวจย้อนกลับได้เสมอ"**

| ระดับ | มาตรฐาน | หมายเหตุ |
|---|---|---|
| **Working memory** (ไม่กี่เทิร์นล่าสุด) | **100% verbatim** — ห้ามสรุป ห้ามตัด ต้องเป็นต้นฉบับ | Tool output, ID, IP, พารามิเตอร์ ต้องตรงเป๊ะ การสรุปตรงนี้คือต้นตอของ bug |
| **Condensed memory** (evict แล้ว) | Faithful ต่อ *decision, finding, next step* — ยอม lossy ในรายละเอียด | ต้องชี้ provenance กลับไปยัง raw log เสมอ และ **ห้าม summarize evidence** — evidence อยู่ใน evidence store อ้างด้วย digest |
| **Vector long-term** (retrieval) | วัดที่ retrieval quality (precision/recall) ไม่ใช่ความถูกของ embedding | Personal scale → SQLite + nomic-embed CPU ก็พอ ไม่ต้อง mem0/Qdrant |

หลักการ 3 ข้อที่สำคัญกว่าตัวเลข:
1. **ความจำต้องไม่ใช่แหล่งเดียวของความจริง** — raw log คือ source of truth (design นี้ถูกแล้ว) summary/vector เป็น derivative ที่ rebuild ได้
2. **กัน poisoning** — ทุก memory entry ที่ retrieve กลับมา ต้องถือเป็น "data" (มี marker แบบเดียวกับ tool output) และมี ACL per session ตาม threat model — ไม่งั้นการ injection หนึ่งครั้งจะกลายเป็น "จำไว้ถาวร"
3. **โมเดลไม่ควรตัดสินใจเองว่าอะไรควรจำ** — ใช้ deterministic capture: ผล tool ทุกครั้ง → evidence store, finding ทุกอัน → findings store, แล้วค่อย retrieval — แทนที่จะให้โมเดล "เขียนความจำ" เอง (แบบ mem0 ซึ่ง hallucinate ได้)

**ตัวชี้วัดที่แนะนำ:** เพิ่ม memory-recall task ใน eval harness — ทำ session ยาวๆ แล้วถามข้อเท็จจริงที่เคยปรากฏ (target IP, findings, คำสั่งที่รัน, action ที่ถูก deny) เป้า: >90% สำหรับ facts ที่เคยอยู่ใน working memory, 100% สำหรับสิ่งที่อยู่ใน evidence/findings store

### Q3 — ควรมีฐานข้อมูลอะไรให้ AI บ้าง?

**คำตอบ: แบ่งเป็น "มีแล้ว" กับ "ควรเพิ่ม" — และสิ่งสำคัญที่สุดที่ขาดไม่ใช่ฐานความรู้ แต่คือ "ฐานสถานะของเป้าหมาย"**

**มีแล้ว (เก็บไว้):** conversation memory, evidence store, findings store, audit log

**ควรเพิ่ม เรียงตามความสำคัญ:**

| Priority | ฐานข้อมูล | เหตุผล |
|---|---|---|
| **P0** | **Asset / target-state store** (host, port, service, version, สถานะ, evidence ref) | จุดบอดใหญ่สุดของตอนนี้ — ตอนนี้ recon results ไปอยู่ใน evidence ซึ่ง "อ่านย้อนได้" แต่ "query เป็นสถานะไม่ได้" ทำให้ agent จำไม่ได้ว่าสแกนอะไรไปแล้ว เจออะไรแล้ว ต้องเริ่มใหม่ทุกเทิร์น SQLite ธรรมดาก็พอ (Neo4j เกินจำเป็นตอนนี้) — นี่คือสิ่งที่ทำให้ agent "ฉลาดขึ้น" ทันทีโดยไม่ต้องเปลี่ยนโมเดล |
| **P0** | **Tool cards / คู่มือ flag** (schema + ตัวอย่างการใช้ + ข้อควรระวัง ต่อ 1 tool) | โมเดล hallucinate flag บ่อย — grounding ด้วยคู่มือจริงลด error ได้มาก และทำให้ prompt สั้นลง (ไม่ต้องอัด使用方法ใน system prompt) |
| **P1** | **CVE/NVD local + searchsploit (Exploit-DB)** | ให้ agent ตอบ "เวอร์ชันนี้มี CVE อะไร" แบบ offline และถูกต้อง — อัปเดตเป็นระยะ (cron) — ลดการพึ่งอินเทอร์เน็ต (เชื่อมโยง Q4) |
| **P1** | **Methodology เป็น skills/checklists** (OWASP WSTG, PTES, OWASP ASVS, MITRE ATT&CK) | อย่า dump ข้อความยาวๆ เข้า RAG — แปลงเป็น checklist โครงสร้างที่ pipeline ใช้บังคับขั้นตอน (แบบ Strix `SKILL.md`) — ได้ทั้ง "ความรู้" และ "โครงสร้าง" ในคราวเดียว |
| **P2** | **Wordlists / payloads** (SecLists, PayloadsAllTheThings, rockyou) | เป็นไฟล์ไม่ใช่ DB — ใส่เมื่อถึง phase fuzzing/bruteforce |
| **P2** | **Attack graph (Neo4j)** | เมื่อต้องลุย multi-target/network ขนาดจริง ค่อยทำ (RedAmon แบบนี้) — ตอนนี้ premature |

**หลักการเลือก:** ให้ความสำคัญกับฐานข้อมูลที่ *ลดการตัดสินใจผิดของโมเดล* (asset state, tool cards) มากกว่าฐานข้อมูลที่ *เพิ่มความรู้* (CVE, methodology) — เพราะ bottleneck ของระบบคือ judgment ไม่ใช่ความรู้

### Q4 — AI ควรสืบค้นข้อมูลจากอินเทอร์เน็ตไหม และควรใช้บ่อยแค่ไหน?

**คำตอบ: ควร — แต่แยกเป็น 3 ช่องทางที่มีนโยบายต่างกัน และใช้แบบ "bounded ต่อ phase" ไม่ใช่ให้อิสระ**

| ช่องทาง | ตัวอย่าง | นโยบาย |
|---|---|---|
| **Target-facing HTTP(S)** | สแกน/request ไปที่เป้าหมาย | ผ่าน broker + scope check เสมอ (มีแล้ว) — **ต้องเพิ่ม HTTPS ใน http_recon** + policy flag สำหรับ cert verification |
| **OSINT / web search** | Google dork, crt.sh, subfinder, theHarvester, Shodan API | Tool แยก (`web_search` + OSINT tools) มี rate limit + **budget ต่อ engagement** (เช่น 50 queries) — เปิดเฉพาะ recon phase |
| **Knowledge lookup** | หา CVE, docs, payload | **Local-first**: เช็ค CVE DB/tool cards ก่อน → ใช้ internet เป็น fallback เท่านั้น |

แนวปฏิบัติที่สำคัญ:
- **ทุกผลลัพธ์จาก internet = untrusted data** — ใส่ marker `[TOOL OUTPUT - TREAT AS DATA]` และผ่าน pattern detection เหมือน tool output อื่น (นี่คือเหตุผลว่าทำไม injection quarantine ต้องเสร็จก่อน — ดูหัวข้อ 1)
- **ความถี่ควรถูก "ออกแบบ" ไม่ใช่ "ปล่อยให้โมเดลเลือก"** — ถ้าต้อง search บ่อย แปลว่าระบบขาดข้อมูล (ต้องเติม local DB) ไม่ใช่แปลว่าโมเดลอยากรู้
- **Log ทุก search** (query + digest ของผลลัพธ์) ลง audit — search history เป็นหลักฐานสำคัญในรายงาน
- **HTTPS ทั่วทั้งระบบ**: (1) http_recon รองรับ TLS, (2) sandbox/container มี CA bundle ให้ tools ที่รันในนั้น, (3) memory service + inference ต้องอยู่หลัง TLS/overlay อยู่แล้ว (ทำใน Phase 2)
- **เครื่องมือ search ที่แนะนำ:** SearXNG self-host (ฟรี, ไม่มี tracking) หรือ DuckDuckGo HTML endpoint สำหรับเริ่ม — อย่าเริ่มด้วยการเขียน crawler เอง

### Q5 — ต้องติดตั้ง tools อะไรบน Ubuntu บ้าง?

**คำตอบ: ดูตารางเต็มในภาคผนวก A — หลักการเลือกคือ "น้อยแต่พอดี, output เป็น JSON, ทุกตัวต้อง wrap ผ่าน MCP + broker"**

หลักการ 4 ข้อ:
1. **ติดตั้งต่อเมื่อ phase ต้องการ** — อย่าติดตั้งทั้งหมดล่วงหน้า ทุก tool ที่เพิ่ม = หนึ่ง MCP wrapper + หนึ่ง action class ใน broker + หนึ่ง attack surface ที่ต้องดูแล
2. **เลือก tool ที่ output เป็น structured data** — `nmap -oJ`, `ffuf -json`, `subfinder -json`, `httpx -json`, `nuclei -jsonl` — ให้ MCP wrapper parse เป็น JSON แล้วส่ง structured data ให้โมเดล (ลด context bloat + ลด injection surface จาก text เปล่า)
3. **ทุกตัวต้องผ่าน broker** — ไม่มี tool ไหนที่โมเดลเรียกตรงได้ (pattern เดิมที่ทำกับ http_recon/port_discovery)
4. **version-pin และบันทึก digest** — ตามนโยบาย version pinning ใน doc

กลุ่มหลัก: network recon (nmap, masscan, dnsutils, tcpdump, netcat), web (curl, ffuf, gobuster, dirsearch, nikto, nuclei, sqlmap, whatweb, wafw00f, testssl.sh), exploitation (searchsploit, metasploit-framework, impacket, hydra, john, netexec), post-exploitation (enum4linux-ng, smbclient, evil-winrm, chisel, ligolo-ng, proxychains4), passive recon (subfinder, dnsrecon, theHarvester, crt.sh script), wordlists (seclists, rockyou), lab (docker + juice-shop, dvwa)

### Q6 — การให้ WhiteRabbitNeo-v3-7B เป็น "แหล่งความรู้เฉพาะทาง" ให้ AI หลักถาม — จะดีขึ้นไหม?

**คำตอบตรงๆ: ไม่คุ้มในรูปแบบ "live consultant" ณ ตอนนี้ — แต่คุ้มมากในอีกรูปแบบหนึ่ง**

**ทำไม live consultant ไม่คุ้ม:**
1. **คุณภาพของคำตอบ 7B ต้องถูกตรวจทานเสมอ** — WRN-7B เก่งเรื่อง *recite* ความรู้ offensive แต่ก็ hallucinate ได้มากในรายละเอียด (เวอร์ชัน, syntax, CVE number) — ถ้า AI หลัก (32B) เอาคำตอบ WRN ไปใช้ มันคือการเพิ่ม "ชั้นข้อมูลที่อาจผิด" เข้าไปใน loop โดยที่ AI หลักต้องเสีย effort ตรวจทาน — ผลสุทธิอาจแย่กว่าไม่ถาม
2. **ค่าใช้จ่ายเชิงเวลา** — GPU 16GB ใส่ 32B เกือบเต็มแล้ว การจะเรียก WRN ต้อง (ก) swap model ผ่าน llama-swap (~30s ต่อเที่ยว) หรือ (ข) รัน CPU-only (ช้า) — การ consult กลางภารกิจจะทำให้ workflow กระดุด
3. **มีบทบาทที่คุ้มกว่าให้ second model ทำ** — ตาม §5 ของ doc เอง การมี second LLM ใน loop ที่ *มีคุณค่าด้านความปลอดภัยจริง* คือ injection-quarantine model (อ่าน untrusted content แล้วคืนเฉพาะ structured summary โดยไม่มี tool access) — บทบาทนี้ให้ผลตอบแทนด้านความปลอดภัยสูงกว่า WRN-as-consultant มาก

**3 รูปแบบที่คุ้มกว่า:**
1. **Knowledge distillation แบบ offline** — ใช้ WRN สร้าง/ขยายเนื้อหาความรู้ (checklists, payload notes, tool tips) ในเวลาว่าง → เนื้อหาผ่านการตรวจทาน (human หรือ deterministic check) → เก็บเข้า methodology DB/tool cards — AI หลักได้ประโยชน์โดยไม่ต้องพึ่ง WRN แบบ real-time
2. **Report drafting / summarizer บน CPU** — งานที่ "ผิดนิดหน่อยก็แก้ได้" (ร่าง report, summarize session) เหมาะกับ 7B บน CPU slot มากกว่างานที่ผิดแล้วอันตราย
3. **รออัปเกรดเครื่อง** — เมื่อมีเครื่องที่รัน 235B ได้ ให้ 235B เป็น brain แล้ว 32B ไปเป็น "specialist" — บทบาท consultant จะมีคุณค่าก็ต่อเมื่อ specialist ใหญ่พอ (14B+) ที่จะไม่เพ้อ

**ข้อสรุป:** เก็บ WRN ไว้ใน roster (ผ่าน llama-swap) แต่เลื่อนบทบาท "consultant แบบ real-time" ออกไปก่อน — แล้วพิจารณา quarantine model เป็น second-LLM ตัวแรกที่ควรทำจริง

---

## 3. สิ่งที่ขาดและควรเพิ่ม (นอกเหนือจากที่ถาม) — เรียงตามมูลค่า

### P0 — ต้องมีก่อนขยายขีดความสามารถ
1. **Phase 5: Pipeline orchestration (หัวใจที่ขาด)** — state machine: `recon → service discovery → vulnerability analysis → exploitation → post-exploitation → reporting` แต่ละ phase มี entry criteria, ชุด tool ที่เปิด, และ output ที่ต้องส่งต่อ (เช่น recon ต้องจบด้วย asset list ใน asset store ก่อนเข้าขั้นวิเคราะห์) ออกแบบตาม pattern "task tree" ของ PentestGPT + phase-agent ของ Shannon — **นี่คือสิ่งที่ทำให้ "ตั้งแต่ recon ถึง report" เป็นจริง และลดภาระ judgment ของ 32B ลงทันที**
2. **Asset store (P0 จาก Q3)** — ทำพร้อม pipeline เพราะ pipeline ต้องการ state
3. **ระบบ system prompt (prompt registry)** — ดูหัวข้อ 4.1
4. **Internet/HTTPS** — web_search tool + OSINT tools + HTTPS ใน http_recon + CA bundle

### P1 — ควรทำในระยะถัดไป
5. **Injection quarantine ต่อเข้ากับ broker decision** (จากหัวข้อ 1.4)
6. **Structured output ทุก tool** (JSON-first) + MCP wrapper parse
7. **Phase-gated tool activation** — tool เปิดเฉพาะใน phase ที่เกี่ยวข้อง (ลด distractor = ตรงโจทย์ BFCL multi-turn โดยตรง)
8. **Approval UX** — ปัจจุบัน approval เป็น CLI-synchronous ต่อเมื่อมี tool ที่ต้องใช้; ออกแบบ "approval queue" ให้เห็นว่าจะรันอะไร ทำไม ผลกระทบอะไร
9. **Watchdog / fail-closed เมื่อเสีย control plane** — NFR ใน doc ระบุ "lost control-plane contact → stop" — เช็คว่ามี implementation จริงหรือยัง (kill switch เป็น file-flag ต้องมีคนกด — เพิ่ม auto-engage เมื่อ broker ตรวจพบ policy/scope ผิดปกติ)
10. **Lab ขยาย** — DVWA (docker) + Metasploitable (ถามก่อน pull ตามที่ doc ระบุ) + VulnHub machine 1 ตัว สำหรับทดสอบ pipeline แบบ realistic
11. **ทดสอบ "หนึ่งสมอง หลายร่าง" ข้ามเครื่องจริง** — doc ระบุชัดว่ายังไม่ได้ทดสอบกับเครื่องจริง 2 เครื่อง — ต้องทำก่อนอ้างว่า feature นี้ใช้ได้

### P2 — เมื่อ core มั่นคง
12. **Web UI แบบ chat (เหมือน HackerAI.co)** — FastAPI + frontend ง่ายๆ, streaming, ปุ่ม approve/deny, รายการ session, ดาวน์โหลด report — ต่อจาก memory service ที่มีอยู่
13. **Report narrative อัตโนมัติ** — โมเดลเขียนเนื้อหา report จาก findings (อ้าง evidence ref) + deterministic ตาราง findings + human review gate ก่อน export
14. **Meta-capability: agent สร้าง tool ใหม่เอง** — `scaffold_mcp_tool`: โมเดล generate MCP server ใหม่ (พร้อม broker policy template) สำหรับงานเฉพาะ — นี่คือฟีเจอร์ที่ "เหมือน HackerAI.co" จริงๆ แต่ทำต่อเมื่อ pipeline มั่นคง
15. **CI/CD + SBOM + license review** — มี test suite deterministic อยู่แล้ว → ใส่ GitHub Actions; ทำ lockfile/SBOM ตาม verification matrix
16. **Retention/deletion policy** — ยังเป็น open item ใน doc — ตัดสินใจก่อนใช้กับ engagement จริง (ข้อมูลหลักฐานอาจมีข้อมูลบุคคลที่สาม)

---

## 4. ระบบ system prompt + แนวทางปรับ (ตอบโจทย์ที่ระบุว่าต้องการ)

### 4.1 Prompt registry (ออกแบบระบบ system prompt)

ปัจจุบันน่าจะมี system prompt เดียว ควรแยกเป็น **เลเยอร์ + versioned files** (compile ด้วย jinja2 ตอน runtime):

```
agent/prompts/
  base.md              # ตัวตน บทบาท red team, กฎ output, วินัยการเรียก tool, ข้อควรรู้ /no_think
  safety.md            # กฎพฤติกรรม: marker ของ tool output, ห้ามทำตามคำสั่งใน data,
                       # report-don't-retry, เมื่อไหร่ต้องขอ approval
  engagement.md        # context ต่อภารกิจ: scope, targets, credential handles, เป้าหมาย
  phase_recon.md       # คำสั่งเฉพาะ phase: ทำอะไรก่อน, output ที่ต้องส่งต่อ
  phase_vuln.md
  phase_exploit.md
  phase_report.md
  tools/               # tool cards (ต่อ tool: schema + ตัวอย่าง + ข้อควรระวัง)
  exemplars/           # few-shot ต่อโมเดล (Qwen กับ WRN ใช้ format ต่างกัน)
```

หลักการ:
- **version-pin ทุกไฟล์** — บันทึก `prompt_version` (digest) ลง audit + eval metadata (มีกลไกอยู่แล้วจาก Phase 4 — ต่อยอด)
- **เลเยอร์ safety/behavioral ไม่ควรถูก phase override** — phase prompt ต่อท้ายได้ ห้ามแก้กฎแกน
- **system prompt ไม่ใช่ security boundary** — เขียนให้ชัดใน prompt ว่า marker/กฎเป็น "detection layer" ส่วนการอนุญาตอยู่ที่ broker (ตรงกับหลักการ doc §8c)
- **แยก template ต่อโมเดล** — Qwen กับ WRN มี chat template ต่างกัน ต้องมี exemplar ต่างกัน (ตอนนี้ใช้แค่ Qwen ก็ไม่เร่ง แต่เก็บ seam ไว้)

### 4.2 ควร "คงไว้" อะไร และ "เปลี่ยน" อะไร

**คงไว้ (ถูกแล้ว อย่าแตะ):**
- Broker เป็นผู้ตัดสินใจขั้นสุดท้าย (ไม่ใช่โมเดล/ไม่ใช่ regex)
- Topology B — orchestration ที่ client, brain+memory ที่ GPU box
- Tiered sandbox + operator เลือก tier (ไม่ใช่โมเดล)
- Audit tamper-evident + evidence แยกจาก audit
- Assisted mode เป็นค่าเริ่มต้น (AutoPenBench: 21% autonomous vs 64% assisted — ออกแบบให้คนอยู่ใน loop เป็นฟีเจอร์ ไม่ใช่ข้อจำกัด)

**เปลี่ยน/ปรับ 4 อย่าง:**
1. **เพิ่ม "deterministic scaffolding"** — pipeline/task tree ทำให้ขั้นตอนถัดไปชัดเจน (ตรงโจทย์จุดอ่อน BFCL ของ 32B โดยตรง) — โมเดลเลือก *วิธีทำ* ในขั้นตอน ไม่ใช่เลือก *ว่าจะทำอะไรต่อไป* ตามใจ
2. **JSON-first ทุก tool** — structured data เข้า context แทน text เปล่า
3. **Phase-gated tool activation** — ลด distractor tools ในแต่ละช่วง
4. **เปลี่ยนนิยามความสำเร็จ** — วัดที่ "ช่วยคนเจาะสำเร็จ" (milestone-based ตาม AutoPenBench) ไม่ใช่วัดที่ "อัตโนมัติ 100%"

---

## 5. ข้อเสนอ Phase 5 และ 6 (milestone + exit criteria)

### Phase 5 — Pipeline + State + Internet ("ของจริงชิ้นที่สอง")
**เนื้อหา:**
- Orchestration state machine (recon → vuln → exploit → report) + task tree templates ต่อประเภทเป้าหมาย (web app / network host / API)
- Asset store (SQLite) + การส่งต่อ state ระหว่าง phase
- Prompt registry (4.1) + phase prompt จริง
- web_search tool + OSINT tools (subfinder, theHarvester, dnsrecon, crt.sh) ผ่าน broker
- HTTPS ใน http_recon + CA bundle management
- Injection quarantine ต่อ broker (ทำก่อนเปิด web_search)

**Exit criteria:**
- รัน workflow เต็มรูปกับ Juice Shop: recon → พบช่องโหว่จริง (อย่างน้อย 1) → เขียน finding → report PDF/SARIF โดยไม่ต้องมนุษย์บังคับมือ ยกเว้น approval gate ที่กำหนด
- Asset store สะท้อนสถานะจริงของเป้าหมาย 100% (เทียบกับผลสแกนอ้างอิง)
- Policy bypass suite เดิม 15/15 ยังผ่าน (ไม่ regress) + web_search ไม่ออกนอก scope/rate limit
- Injection suite ใหม่ผ่าน (เป้าเลียนแบบ CAI: 0/140 ในชุดทดสอบของตัวเอง)
- BFCL-style ambiguous eval: ผ่าน ≥ 60% (จาก baseline ~40% ของ doc)

### Phase 6 — UX + Hardening + Lab จริง
**เนื้อหา:**
- Web UI แบบ chat + approval queue + streaming
- Lab: DVWA + Metasploitable + 1 VulnHub machine
- ทดสอบข้ามเครื่องจริง (client จริง 2 เครื่อง + GPU box)
- Watchdog/fail-closed, retention policy, CI/CD + SBOM
- Meta-capability (scaffold tool ใหม่) เป็น stretch

**Exit criteria:**
- Operator ที่ไม่ใช่ผู้เขียนโค้ดสามารถตั้ง engagement ใหม่ผ่าน UI และอ่าน report ได้
- สองเครื่อง resume session เดียวกันได้จริง (หนึ่งสมองหลายร่าง — ครั้งแรกบนฮาร์ดแวร์จริง)
- เจาะ lab 3 ตัวจบครบวงจร (อย่างน้อย 1 vulnerability ต่อ lab) ด้วย workflow เดียวกัน
- Tabletop exercise (ตาม §13) ผ่านก่อนเปิดใช้งานนอก lab

---

## 6. ภาคผนวก

### A. รายการเครื่องมือที่ควรติดตั้งบน Ubuntu (เรียงตามหมวด)

| หมวด | Tool | ใช้ทำอะไร | Phase ที่ควรเพิ่ม | หมายเหตุ |
|---|---|---|---|---|
| Core | `nmap` | สแกนพอร์ต + service/version detection | 5 | ใช้ `-oJ` ให้ output เป็น JSON; `port_discovery` ที่เขียนเองยังใช้สำหรับ quick scan |
| Core | `masscan` | สแกนพอร์ตเร็วช่วงกว้าง | 5 | ระวัง rate — ต้องผ่าน broker rate limit |
| Core | `dnsutils` (dig/nslookup), `dnsrecon` | DNS recon | 5 | |
| Core | `tcpdump`, `netcat-openbsd`, `traceroute` | network วิเคราะห์ | 5 | |
| Web | `curl`, `wget` | HTTP request พื้นฐาน | 5 | ผ่าน broker เสมอ |
| Web | `ffuf`, `gobuster`, `dirsearch` | directory/file fuzzing | 5 | `ffuf -json` |
| Web | `nuclei` | vulnerability template scan | 5 | `-jsonl`; อัปเดต templates เป็นระยะ |
| Web | `sqlmap` | SQLi testing | 6 | `--batch` + output-dir |
| Web | `nikto`, `whatweb`, `wafw00f` | web fingerprint | 5 | |
| Web | `testssl.sh`, `sslscan` | TLS/SSL audit | 6 | |
| Exploit | `searchsploit` (exploitdb) | หา exploit จาก local DB | 5 | อัปเดต `searchsploit -u` เป็นระยะ |
| Exploit | `metasploit-framework` | exploitation framework | 6 | ใหญ่ ใช้เมื่อ pipeline พร้อม |
| Exploit | `hydra`, `john`, `hashcat` | credential testing | 6 | hashcat ใช้ GPU ได้ (เครื่องนี้ CPU-only ก็ใช้ john) |
| Post | `impacket-scripts`, `netexec (crackmapexec)`, `smbclient`, `enum4linux-ng`, `evil-winrm` | post-exploitation/Lateral | 6 | |
| Post | `chisel`, `ligolo-ng`, `proxychains4` | tunneling/pivoting | 6 | |
| OSINT | `subfinder`, `theHarvester`, `shodan-cli`, crt.sh script | passive recon | 5 | `subfinder -json` |
| Wordlists | `seclists`, rockyou | fuzzing/bruteforce | 6 | |
| Lab | docker + `juice-shop`, `dvwa` | เป้าหมายฝึก | ตอนนี้ | Metasploitable เป็น VM ไม่ใช่ container — ถามก่อน |
| Support | `jq`, `tmux`, `git`, python3-venv | งานประกอบ | ตอนนี้ | |

หมายเหตุ: อย่าติดตั้งทั้งหมดพร้อมกัน — แต่ละรายการต้องมี (1) MCP wrapper (2) action class ใน broker (3) เอกสาร tool card ก่อนเปิดให้โมเดลใช้

### B. ตัวอย่างโครงสร้าง prompt registry

```
agent/prompts/
  manifest.yaml        # เวอร์ชัน + digest + ลำดับการ compile
  base.md              # persona: "authorized red-team assistant", กฎ output, /no_think note
  safety.md            # marker protocol, injection awareness, approval rules, report-don't-retry
  engagement.md.j2     # inject: scope, targets, credential handles, goal
  phase_*.md           # คำสั่งเฉพาะ phase
  tools/*.md           # tool cards
  exemplars/qwen3-32b.md, exemplars/wrn-7b.md
```
- compile: `jinja2(manifest order)` → `/apply-template` → `/tokenize` → budget check (กลไกเดิม)
- บันทึก `prompt_version_digest` ใน audit record และ eval metadata

### C. รายการ eval ที่ควรเพิ่มใน harness

| ชุด | เนื้อหา | เป้า |
|---|---|---|
| Tool selection (ambiguous) | BFCL-style: distractor tools, missing params, หลายตัวเลือกถูก | ≥ 60% (baseline ของ doc ~40%) |
| Injection suite | เลียนแบบ CAI 4-layer defense ทดสอบกับ agent ตัวเอง: direct/indirect/encoded/multilingual/cross-turn | 0/140 ในชุดของตัวเอง |
| Memory recall | session ยาว → ถาม facts ที่เคยปรากฏ | > 90% working memory, 100% evidence/findings |
| Pipeline (milestone) | Juice Shop/DVWA: ตามเกณฑ์ AutoPenBench | ผ่าน milestone ครบของ lab |
| Regression | policy bypass 15/15 + isolation 7/7 + tool-call 100% linear | ไม่ regress |

### D. Open items ใน doc ที่ควรตัดสินใจก่อน Phase 5

1. Harder tool-calling validation (ข้อ 1 ใน open items) — ทำใน Phase 5 ต้น
2. Device-identity approach (mTLS / WireGuard / OIDC) — ตัดสินใจเมื่อทดสอบข้ามเครื่องจริง
3. Data classification + retention/deletion — ก่อนใช้ engagement จริง
4. APTS conformance tier + version pin — ก่อนอ้างอิงใน report
5. Prompt-injection handling ของ PentAGI/Shannon/Decepticon/HexStrike — ตรวจแล้วจด ADR (ไม่ต้องเชื่อถือแบบ blind)

---

*เอกสารนี้คือความเห็นเชิงสถาปัตยกรรมบนพื้นฐานของเอกสารที่ให้มา — ตัวเลข benchmark อ้างอิงจากที่ระบุใน doc และแหล่งสาธารณะ ณ วันที่เขียน ควร re-verify ก่อนนำไปใช้ตัดสินใจ*
