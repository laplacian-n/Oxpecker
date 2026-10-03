 # แนวทางหลักสำหรับ Local AI Red-Team Agent: Phase 4 เป็นต้นไป

วันที่จัดทำ: 2026-08-31  
สถานะอ้างอิงจากผู้ใช้: **Phase 1–3 เสร็จแล้ว และกำลังจะเริ่ม Phase 4**

> เอกสารนี้เป็นข้อเสนอเชิงสถาปัตยกรรมและแผนพัฒนา ไม่ใช่หลักฐานว่าโค้ดถูกสร้างหรือ
> ทดสอบแล้ว การยืนยันสถานะต้องดู repository, test output และ artifact จริงเสมอ

## 1. ข้อสรุปสั้นที่สุด

แนวทางหลักเดิมควร **คงไว้** ได้แก่ broker-centric architecture, Topology B, assisted mode,
scope enforcement นอกโมเดล, evidence/audit แยกจาก conversation, phase-gated tools และ
sandbox หลายระดับ สิ่งเหล่านี้เป็นฐานที่ถูกต้องสำหรับระบบแบบ HackerAI

สิ่งที่ควรเปลี่ยนคือ:

1. อย่าถือ `phase4.md` ที่เขียนเป็นอดีตกาลว่า Phase 4 เสร็จแล้ว ให้เปลี่ยนเป็น
   **target specification + acceptance criteria** ตามสถานะจริงที่ผู้ใช้ยืนยัน
2. Phase 4 ต้องเป็น **Trust & Evidence Gate** ก่อนเพิ่มเครื่องมือจำนวนมาก: ทำ sandbox,
   evidence lifecycle, finding schema, injection quarantine, HTTPS และ eval ให้ผ่านก่อน
3. Phase 5 ค่อยสร้าง pipeline, asset/hypothesis/coverage state, prompt registry เต็มรูป,
   skills และ internet knowledge access
4. Browser, OOB, parallel execution, retest และ UI ควรอยู่ Phase 6 หลัง policy/egress/
   isolation แข็งแรงแล้ว
5. Network post-exploitation, tunneling, credential attacks และ runtime-generated tools
   ไม่ควรปนกับ web pipeline แรก ให้เป็น profile/phase หลังจากนั้นและเปิดแบบ assisted เท่านั้น

หลักคิดกลางของระบบควรเป็น:

> **โมเดลเสนอแผน — state store เก็บข้อเท็จจริง — broker อนุญาตการกระทำ — evaluator ตัดสินคุณภาพ**

## 2. สิ่งที่ควรคงไว้ เปลี่ยน และเลื่อน

| การตัดสินใจ | ข้อเสนอ | เหตุผล |
|---|---|---|
| คงไว้ | Topology B: orchestrator/tool อยู่ client; inference+memory อยู่ GPU box | ลด inbound connectivity และทำให้ execution local ตามเครื่องที่ใช้งาน |
| คงไว้ | Broker เป็น authority เดียว | prompt, model และ detector ไม่ใช่ security boundary |
| คงไว้ | RoE, deny-before-allow, DNS pinning, redirect revalidation, kill switch | เป็น boundary ที่จำเป็นสำหรับ network actions |
| คงไว้ | Assisted mode เป็นค่าเริ่มต้น | โมเดลท้องถิ่นยังต้องมีโครงสร้างและคนอนุมัติการกระทำผลกระทบสูง |
| คงไว้ | JSONL audit + encrypted evidence + findings แยกกัน | แต่ต้องเพิ่ม external/signed checkpoint และ data lifecycle |
| เปลี่ยน | `direct` ไม่ควรเป็น isolation ค่าเริ่มต้น | ให้ `bubblewrap` เป็นค่าเริ่มต้นของ generic command; direct ต้องเป็น explicit dangerous override |
| เปลี่ยน | จาก “working memory ต้อง verbatim 100% ทุกอย่าง” เป็น “critical state ต้อง exact” | raw output ขนาดใหญ่ควรอยู่ evidence; context เก็บ canonical excerpt/typed facts |
| เปลี่ยน | จาก pipeline เดียวเป็น web/API และ network profiles ที่ใช้ core state ร่วมกัน | recon/exploitation ของ web และ network มีเครื่องมือ ความเสี่ยง และ exit criteria ต่างกัน |
| เปลี่ยน | จาก local-first แบบตายตัวเป็น freshness-aware local-first | local DB ที่เก่าไม่ควรชนะข้อมูลปัจจุบันโดยอัตโนมัติ |
| เลื่อน | Firecracker/microVM | ทำเมื่อมี exploit-grade arbitrary code และทีมพร้อมดูแล kernel/rootfs/vsock/image supply chain |
| เลื่อน | Metasploit, credential attacks, pivoting, tunneling | blast radius สูงและยังไม่จำเป็นต่อการพิสูจน์ pipeline แรก |
| เลื่อน | agent สร้าง/ติดตั้ง tool ใหม่ระหว่าง runtime | เปลี่ยนเป็น offline developer workflow ที่ต้อง review, test, policy และ signing |

## 3. Roadmap ที่แนะนำใหม่

### Phase 4A — Isolation, Evidence, Findings, Eval

เป้าหมาย: ทำให้สิ่งที่มีอยู่จาก Phase 3 สามารถรันซ้ำ ตรวจสอบย้อนหลัง และล้มเหลวแบบ
fail-closed ได้ โดยยังไม่เพิ่มเครื่องมือ pentest จำนวนมาก

#### M4.0 — Truthful status baseline

- เปลี่ยนข้อความ Phase 4 จาก “built/exit-tested” เป็น `planned` จนกว่าจะมี test artifact
- สร้าง `STATUS.md` ที่แยก `planned | implemented | tested | accepted | deferred`
- บันทึก command, timestamp, commit, model/template/policy digest ของทุก acceptance test
- รัน regression Phase 1–3 ก่อนแก้โค้ดและเก็บ baseline

**ผ่านเมื่อ:** เอกสารไม่อ้างความสามารถที่ repository/test artifact ยืนยันไม่ได้

#### M4.1 — Bubblewrap profile ที่นิยาม security model ชัดเจน

- ใช้ `bubblewrap` เป็น low-level primitive ไม่ใช่ “sandbox สำเร็จรูป”; profile เป็นผู้กำหนด
  security boundary ตามคำเตือนของโครงการ bubblewrap
- `run_command` ใช้ bubblewrap เป็นค่าเริ่มต้น; `direct` ต้องมี `--dangerous-local`,
  interactive confirmation และ audit flag
- ใช้ namespaces ที่จำเป็น: user, mount, pid, ipc, uts, cgroup และ network ตาม profile
- mount แบบ allowlist: workspace rw, runtime/library ที่จำเป็น ro; ห้าม host home, SSH agent,
  Docker socket, D-Bus, Wayland/X11, cloud metadata และ credential paths
- `clearenv`; ส่งเฉพาะ environment allowlist
- เพิ่ม process-count, file-size, open-file, memory, CPU, wall-clock และ output limits
- ใช้ cgroup v2 เมื่อทำได้; rlimit เป็นอีกชั้น ไม่ใช่ตัวแทนทั้งหมด
- พิจารณา seccomp จาก threat model ของแต่ละ profile; ห้าม copy profile ทั่วไปแล้วอ้างว่าปลอดภัย
- ตรวจ `--die-with-parent`, process-tree cleanup, isolated `/proc`, minimal `/dev`, no-new-privs,
  nested-userns policy และ mount propagation
- ระบุชัด: bubblewrap tier = strong local isolation profile; ไม่ใช่ microVM-grade

**ชุดทดสอบขั้นต่ำ:** filesystem/symlink escape, inherited FD, Unix socket, `/proc`, ptrace,
TIOCSTI/TTY, nested namespace, fork bomb, disk/output bomb, memory/CPU, signal/cancellation,
network to host loopback/external/IPv6 และ parent death

**ผ่านเมื่อ:** advertised profile ทุกตัวผ่านชุดทดสอบของตัวเอง และไม่มี silent downgrade

#### M4.2 — Evidence และ audit lifecycle

- Raw evidence เข้ารหัสและ content-addressed; audit เก็บ metadata, excerpt ที่ redact และ ref
- หลีกเลี่ยงใช้ plaintext SHA-256 เป็น identifier ภายนอกหาก equality leakage สำคัญ;
  ใช้ HMAC/content ID แบบ keyed ได้
- แยก key ออกจาก evidence directory; permission แคบ; รองรับ key version/rotation
- เพิ่ม canonical record serialization และ signed/external audit checkpoint
- schema ต้องมี sensitivity, retention class, origin, tool/version, policy/prompt digest,
  encryption-key version, content type, size และ deletion tombstone
- ป้องกัน decompression bomb, oversized body, malicious filename/MIME และ partial writes
- เพิ่ม export/delete/retention job และ test restore; summary/vector เป็น derivative ที่ rebuild ได้

**ผ่านเมื่อ:** ตรวจ tamper, wrong key, truncated write, restore, retention, deletion propagation
และ unauthorized read ได้จริง

#### M4.3 — Finding schema ที่บังคับ evidence-first

ขั้นต่ำควรมี:

```text
finding_id, engagement_id, asset_id, title, status,
confidence, severity, cvss_version, cvss_vector,
cwe_ids, cve_ids, affected_component, preconditions,
observation_refs, evidence_refs, demonstrated_impact,
reproduction_recipe_ref, remediation, references,
first_seen, last_verified, verifier, limitations
```

- `confirmed` ต้องมี evidence และ verification rule; โมเดลเปลี่ยน hypothesis เป็น confirmed เองไม่ได้
- severity ต้องแยกจาก confidence และ demonstrated impact
- `no finding` เป็นผลลัพธ์ที่ถูกต้อง; pipeline ห้ามบังคับว่าต้องพบอย่างน้อยหนึ่งช่องโหว่
- report ต้องมี scope, coverage, negative tests, exclusions, limitations และ unresolved hypotheses
- narrative ที่โมเดลเขียนต้องอ้าง refs; deterministic renderer สร้างตารางและ metadata
- รองรับ Unicode/ภาษาไทยตั้งแต่ Phase 4 ด้วย bundled font ที่ตรวจ license แล้ว

**ผ่านเมื่อ:** claim ทุกข้อ trace กลับ asset/observation/evidence/audit ได้ และ report ไม่สร้าง
ข้อเท็จจริงที่ไม่มี source

#### M4.4 — Eval harness ที่มากกว่า smoke test 3 ข้อ

แยก eval เป็น 4 ชั้น:

1. deterministic regression: policy, sandbox, audit, evidence, memory concurrency
2. model/tool: correct tool, correct args, no-call, missing args, clarification, distractors,
   malformed output, denied action, truncated output
3. security reasoning: hypothesis quality, false-positive control, evidence-to-finding,
   severity calibration และ current-data lookup
4. end-to-end milestone: Juice Shop ก่อน แล้วค่อย DVWA/network lab

รันหลาย seed/temperature profile และรายงาน pass rate พร้อม confidence interval; ห้ามใช้
“3/3” หรือ “0/N” เป็นหลักฐานความปลอดภัยทั่วไป

**ผ่านเมื่อ:** regression เดิมไม่ตก, model promotion gate ถูกกำหนด, และมี failure taxonomy
ที่บอกว่า fail เพราะ model/tool/policy/target/infrastructure

### Phase 4B — Safety prerequisites ก่อนเปิดอินเทอร์เน็ต

#### M4.5 — Injection quarantine เชื่อม broker

- scanner ให้ label `clean | suspicious | malicious | unknown`; non-match ไม่ใช่ allow
- raw เก็บใน evidence; context ใช้ bounded structured extraction และ provenance/taint
- action ที่อ้าง tainted/unknown evidence ถูกยกระดับ risk หรือขอ approval
- อย่าให้โมเดลตัวเดิม “สรุปข้อความอันตรายแล้วถือว่าปลอดภัย”; summary ยังเป็น untrusted derivative
- ทดสอบ encoded, Unicode, multilingual, fragmented, cross-turn, tool-schema, memory และ browser cases

#### M4.6 — HTTPS ใน `http_recon`

- รองรับ TLS 1.2/1.3, SNI, ALPN, IPv4/IPv6, timeout, body/decompression limits และ redirect cap
- ค่าเริ่มต้น verify certificate; รองรับ per-engagement custom CA bundle
- กรณี invalid/self-signed cert ให้ RoE กำหนด `tls_policy`; บันทึก verification error เสมอ
- ต้อง connect ไป validated IP แต่ verify certificate กับ original hostname/SNI
- revalidate scope ทุก redirect และห้าม credential/header รั่วข้าม origin
- เพิ่ม test: SAN mismatch, expired cert, self-signed, custom CA, IP literal, redirect http↔https,
  DNS rebinding, mixed DNS answers, proxy env leakage และ oversized compressed response

#### M4.7 — Prompt registry skeleton

ยังไม่ต้องสร้าง methodology ทั้งหมด แต่ต้องมี compiler/versioning ก่อน Phase 5:

```text
prompts/
  manifest.yaml
  core_identity.md
  authorization.md
  data_provenance.md
  action_protocol.md
  reporting.md
  engagement.md.j2
  phases/
  tools/
  exemplars/<model-profile>/
```

- dynamic/user/target text ต้องอยู่ใน delimiters และ sensitivity/provenance ชัดเจน
- phase prompt ไม่มีสิทธิ์เปลี่ยน core authorization; broker ยังเป็นผู้บังคับจริง
- compile แบบ deterministic, digest, budget และ golden tests
- tool schema ที่ส่งโมเดลต้องตรงกับ tools ที่ broker อนุญาตใน phase นั้น

### Phase 5 — Engagement Intelligence + Pipeline + Internet

#### M5.1 — Structured engagement intake

สร้าง RoE จาก wizard/JSON: target, allow/deny, time window, action classes, credential handles,
rate/blast-radius, approval policy, evidence retention, contact/kill procedure และ operator sign-off

#### M5.2 — Authoritative engagement-state store

SQLite ก่อน ไม่ต้อง Neo4j ประกอบด้วย:

- assets/endpoints/services/technologies/relationships
- observations + evidence refs
- hypotheses + test plans + verdict/negative evidence
- coverage ledger: surface ใดทดสอบอะไรแล้ว/ยังไม่ได้ทดสอบ/เหตุผลที่ข้าม
- phase/task state + transition history
- findings/retests/closeout state

ใช้ append/history tables, foreign keys, unique constraints, migrations และ optimistic concurrency

#### M5.3 — สอง pipeline profile

Common super-state:

```text
INTAKE → RECON → ANALYSIS → VALIDATION → REPORT → CLOSEOUT
```

Web/API profile แยก task tree จาก Network profile แต่ใช้ broker, evidence, findings และ report
ร่วมกัน Phase transition เกิดจาก coverage/budget/criteria ไม่ใช่เงื่อนไข “ต้องเจอช่องโหว่”

#### M5.4 — Hypothesis board + skill library

- hypothesis มี claim, surface, priority, preconditions, evidence for/against, planned test,
  status, confidence, owner และ verdict reason
- skill เป็น curated, read-only, versioned artifact; มี applicability, checklist, permitted tools,
  expected evidence, stop conditions และ references
- skill update ต้องผ่าน review/test/license check; target content เขียน skill ไม่ได้

#### M5.5 — Internet access แยก 4 ช่องทาง

1. `target_http`: ติดต่อ target ใน RoE เท่านั้น
2. `knowledge_search`: query search provider ที่อยู่ใน egress allowlist
3. `knowledge_fetch`: fetch URL จากผลค้นหา ผ่าน DNS/redirect/content policy แยก
4. `osint_discovery`: ค้นพบ asset; asset นอก scope บันทึกเป็น observed-out-of-scope แต่ห้าม active probe

ทุกช่องทางมี query/time/byte budget, caching, freshness, provenance, content-type limits,
injection quarantine และ audit ผู้ใช้กำหนด policy ต่อ engagement ได้

### Phase 6 — Advanced web capability และ operator UX

- Browser automation ผ่าน isolated Playwright service, non-root, browser sandbox+seccomp,
  ephemeral context, download disabled/isolated, target-only egress proxy, screenshot/HAR evidence
- OOB/Interactsh แบบ self-host: correlation token, lifecycle, evidence, dedicated domain,
  authentication และ retention; **ห้ามทิ้ง callback เพียงเพราะ source IP อยู่นอก scope**
- Approval queue, steer channel, session handoff และ web UI
- Retest mode จาก versioned test recipe ไม่ใช่ replay raw request แบบไม่ตรวจสอบ
- Mandatory closeout: stop listener/tunnel/process, revoke handles, clean workspace, cleanup report
- Deterministic parallel executor สำหรับ low-risk recon หลังมี shared blast-radius/rate budget

### Phase 7 — Network red-team profile และ stronger isolation

- ค่อยเพิ่ม constrained nmap/service enumeration, authenticated protocols และ lab network
- credential testing, post-exploitation, pivoting/tunneling เป็น action class สูงและ disabled by default
- microVM/container images pinned by digest พร้อม SBOM/signing/provenance ก่อน arbitrary exploit code
- runtime self-modification ไม่อนุญาต; tool scaffolding เป็น offline PR-like workflow เท่านั้น

## 4. คำตอบคำถามทั้ง 6 ข้อ

### 4.1 Qwen3-32B ที่ใช้อยู่ดีพอหรือยัง

**คำตอบ:** ดีพอให้พัฒนาต่อและใช้แบบ assisted; ยังไม่มีหลักฐานพอให้ autonomous red team

แยกวัด 4 เรื่อง ห้ามรวมเป็นคำว่า “เก่ง” ค่าเดียว:

1. syntax/tool-call validity
2. tool/argument selection ภายใต้ ambiguity
3. cybersecurity reasoning และ false-positive control
4. end-to-end task completion ภายใต้ broker/policy จริง

Qwen ระบุว่า Qwen3 รองรับ agentic/tool calling และมี thinking/non-thinking modes แต่ผลของ
official model ไม่เท่ากับผลของ abliterated i1-Q4_K_M บน llama.cpp/template ของเครื่องนี้
คะแนน BFCL ที่เอกสาร HackerAI ยกมาไม่ควรถูกใช้จนกว่าจะระบุ model revision, subset และ evaluator
ตรงกัน การตัดสินต้องมาจาก local eval หลาย seed

ข้อเสนอ:

- ใช้ Qwen3-32B เป็น primary ต่อใน Phase 4–5
- A/B กับ **aligned Qwen3-32B** ถ้าหา quant ที่เหมาะได้ เพื่อวัดผลของ abliteration
- A/B กับ Qwen3-Coder-30B-A3B-Instruct เฉพาะ coding/tool tasks ถ้ารันบนเครื่องได้จริง
- ห้าม promote model เพราะ benchmark ภายนอกอย่างเดียว; ใช้ model registry + local promotion gate

### 4.2 Memory ควรแม่นแค่ไหน

ต้องแม่นตามชนิดข้อมูล:

| ชั้น | มาตรฐาน |
|---|---|
| RoE/scope/credentials policy | exact, typed, versioned, model แก้ไม่ได้ |
| Asset/observation/finding/hypothesis state | exact fields + provenance + history |
| Raw evidence/audit | immutable/verifiable; ไม่สรุปทับ source |
| Recent conversation | verbatim เท่าที่ budget อนุญาต |
| Condensed summary | lossy ได้ แต่ต้องอ้าง source range และห้ามเป็น authority |
| Vector retrieval | วัด recall/precision; retrieved text เป็น untrusted data |

เป้าหมายที่ควรเป็น 100% คือการดึง **critical facts จาก authoritative store** ไม่ใช่การให้
โมเดลจำข้อความทุกคำ Vector memory ที่ผิดห้ามนำไป execute โดยไม่ revalidate กับ source

### 4.3 ควรมีฐานข้อมูลอะไร

ลำดับที่แนะนำ:

1. Engagement/RoE/policy store
2. Asset + endpoint + service + relationship store
3. Observation/evidence index
4. Hypothesis/test-plan/coverage ledger
5. Findings/retest/closeout store
6. Tool registry, capability manifests, tool cards และ version/license/digest
7. Curated knowledge index
8. Conversation/summary/vector memory
9. Audit/checkpoint store

Knowledge sources ควรมี metadata/version/freshness:

- CVE List V5 เป็น record ต้นทาง; NVD สำหรับ CVSS/CPE enrichment
- CISA KEV สำหรับ known exploitation; EPSS เป็น likelihood signal ไม่ใช่ severity
- OSV สำหรับ package/ecosystem vulnerability lookup
- CWE/CAPEC สำหรับ weakness/attack pattern
- OWASP WSTG/ASVS และ PTES สำหรับ methodology/checklist
- MITRE ATT&CK สำหรับ tactic/technique mapping โดยเฉพาะ network/red-team report
- Exploit-DB/searchsploit เป็น lead ไม่ใช่หลักฐานว่า exploit ใช้ได้
- vendor advisory เป็นแหล่งยืนยัน version/mitigation ที่มีน้ำหนักสูง

อย่า dump ทุกอย่างเข้า vector DB; เก็บ structured metadata ใน SQLite และ embed เฉพาะข้อความที่
ต้องค้นเชิงความหมาย พร้อม provenance และ update timestamp

### 4.4 ควรค้นอินเทอร์เน็ตไหม และบ่อยแค่ไหน

ควรค้นแบบ **policy- and freshness-driven**:

- target action: ตาม task tree และ RoE ไม่ใช่ “ค้นอินเทอร์เน็ต”
- CVE/vendor fact: local ก่อนถ้าข้อมูลสด; fallback web เมื่อไม่มี/เก่า/ขัดแย้ง
- OSINT: เฉพาะ recon และมี query/target/time budget
- payload/methodology: curated skill/tool card ก่อน; web สำหรับ edge case
- report reference: fetch primary advisory เพื่อยืนยันก่อน final report

อย่ากำหนดเลขเดียว เช่น “50 queries ทุกงาน” ให้ policy profile กำหนดตามขนาด engagement และ
เก็บ metric `queries_per_confirmed_fact`, cache hit, stale fallback และ search-with-local-answer

### 4.5 ต้องติดตั้งอะไรบน Ubuntu

หลักการ: ติดตั้งตาม phase, version-pin, มี wrapper+policy+tool card+tests ก่อน expose ให้โมเดล

**Phase 4 host/runtime:**

- bubblewrap จากแหล่งที่ได้รับ security updates พร้อมบันทึก version
- cgroup v2/systemd integration และเครื่องมือสร้าง seccomp profileตามที่เลือก
- Python venv/lockfile; SQLite; cryptography; report renderer; licensed Unicode font
- test tooling: pytest, coverage, ruff/mypy ตามมาตรฐานโปรเจกต์
- SBOM/vulnerability scan สำหรับ dependencies เมื่อ pipeline CI พร้อม

**Phase 5 minimal recon set:**

- `nmap` เฉพาะ constrained wrapper; ใช้ XML `-oX -` แล้ว parse เป็น JSON—**nmap ไม่มี `-oJ`**
- `subfinder` หรือ DNS wrapper หนึ่งตัว ไม่ต้องเพิ่มหลายตัวพร้อมกัน
- `ffuf` สำหรับ bounded content discovery เมื่อ broker มี request/rate budget
- `nuclei` เฉพาะ official signed templates, pinned version/template revision; code templates ปิดก่อน
- `searchsploit` สำหรับ offline lead lookup
- `testssl.sh` หรือ `sslscan` เมื่อ TLS workflow พร้อม

**เลื่อน:** masscan, sqlmap, Metasploit, hydra/hashcat, impacket/netexec, tunneling tools และ
post-exploitation suite จนมี action classes, lab, approvals และ isolation ที่ตรงความเสี่ยง

ไม่ควร expose `curl`, `wget`, `nc` หรือ general shell เป็นเครื่องมือหลัก เพราะ policy จำกัด
semantics ได้ยากกว่า narrow wrapper

### 4.6 ควรใช้ WhiteRabbitNeo-v3-7B อย่างไร

ไม่ควรใช้เป็น authoritative live consultant หรือ injection guard:

- model card/benchmark ที่ตรวจสอบได้ยังไม่พอจะยืนยันว่าดีกว่า Qwen3-32B ในงานจริง
- 7B มีโอกาส hallucinate tool flag/CVE/version และการ consult เพิ่ม latency/complexity
- cybersecurity-tuned/abliterated model ไม่เหมาะเป็น safety classifier โดยอัตโนมัติ

บทบาทที่คุ้มกว่า:

1. shadow hypothesis generator: สร้างข้อเสนอโดยไม่มี tool access แล้วเทียบ hit rate
2. offline skill/tool-card drafting ที่ต้อง human/source review ก่อน merge
3. report-language draft หรือ summarization ที่ source refs ถูกล็อกไว้
4. benchmark challenger ใน model registry

ถ้าจะ route model ให้ route ตาม **task class ระหว่าง bounded steps** ไม่ใช่ให้โมเดลคุยกันทุก
tool call และห้ามส่ง untrusted raw evidence ให้ second model แล้วถือผลลัพธ์ว่าปลอดภัย

## 5. System prompt ที่ควรได้

Prompt ไม่ควรเป็น persona ยาวก้อนเดียว แต่เป็น compiled policy-aware prompt:

1. **Identity/mission:** authorized red-team assistant, assisted-first
2. **Authority:** broker/RoE/tool result/status store คือ authority; prompt ไม่มีสิทธิ์ override
3. **Data provenance:** target/tool/web/memory text เป็น data; refs/taint/truncation protocol
4. **Decision loop:** read state → select one hypothesis/test → state expected evidence → call tool
5. **Failure behavior:** denied/unknown/truncated/conflict → report/clarify, no bypass/retry loop
6. **Phase contract:** goal, allowed tools, entry/exit/stop criteria, remaining budget
7. **Finding discipline:** observation ≠ vulnerability; confirmed requires verification/evidence
8. **Response UX:** summary-first, one necessary question, confidence/limitations, next action

Prompt budget รวม core+phase+tool cards ควรมีเพดานเป็นสัดส่วนของ context และ tool schemas ต้อง
phase-gated ทั้งตอนส่งเข้าโมเดลและตอน broker authorize

## 6. สิ่งที่ควรเพิ่มนอกเหนือจากคำถาม

- **Coverage ledger:** บอกสิ่งที่ทดสอบแล้ว/ไม่ทดสอบ/เหตุผล สำคัญกว่าการจำ conversation ยาว
- **Capability manifest:** ทุก tool ระบุ network/filesystem/secret/side-effect/risk/isolation
- **Action taxonomy:** L0 local read-only, L1 passive target, L2 active recon, L3 auth/fuzz,
  L4 exploit validation, L5 destructive/persistence disabled
- **Verification firewall:** observation → normalized claim → deterministic checks → finding
- **Egress gateway:** target/search/fetch/update/OOB มี policy แยกแต่บังคับที่จุดร่วม
- **Model registry:** model/template/quant/seed/eval results/promotion status
- **Reproducible toolchain:** lockfile, tool/image digest, SBOM, signed releases, rollback
- **Closeout เป็น phase บังคับ:** ไม่ใช่ของเสริมหลัง UI
- **Negative-result reporting:** รายงานความครอบคลุมและข้อจำกัด ไม่บังคับให้ “ต้องเจอช่องโหว่”

## 7. แหล่งต้นทางที่ตรวจเพื่อแก้ข้อเท็จจริง

- [Qwen3-32B model card](https://huggingface.co/Qwen/Qwen3-32B)
- [Qwen3 Technical Report](https://arxiv.org/abs/2505.09388)
- [Qwen3-Coder-30B-A3B-Instruct model card](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct)
- [bubblewrap security model and limitations](https://github.com/containers/bubblewrap)
- [Nmap official output formats](https://nmap.org/book/man-output.html)
- [CVE List downloads / CVE JSON 5](https://www.cve.org/Downloads)
- [NVD API 2.0](https://nvd.nist.gov/developers/vulnerabilities)
- [CISA Known Exploited Vulnerabilities](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)
- [FIRST EPSS data](https://www.first.org/epss/data.html)
- [OSV API](https://google.github.io/osv.dev/api/)
- [Interactsh](https://github.com/projectdiscovery/interactsh)
- [Nuclei template signing](https://docs.projectdiscovery.io/templates/reference/template-signing)
- [Playwright Docker security guidance](https://playwright.dev/docs/docker)

