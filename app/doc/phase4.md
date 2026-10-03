# Phase 4 — Sandbox + Evidence + Findings + Eval

## ภาพรวม
เพิ่ม sandbox จริงระดับ kernel สำหรับ `run_command`, ที่เก็บหลักฐานแบบเข้ารหัส, โครงสร้าง findings พร้อม export เป็น SARIF/PDF, และชุดทดสอบวัดความสามารถ (evaluation harness)

## โมเดลที่ใช้
เหมือน Phase 1-3 (imatrix i1-Q4_K_M) — ไม่ได้เปลี่ยน แต่เพิ่ม `seed` แบบ fix ได้ (สำหรับ eval harness ให้ reproducible จริง)

## Tools ที่มี
เหมือน Phase 3 ทั้งหมด (5 ตัว: `run_command`, `read_file`, `write_file`, `http_recon`, `port_discovery`) — ไม่มี tool ใหม่ แต่ `run_command` เลือก **isolation tier** ได้แล้ว

## เข้าถึงเน็ตได้แค่ไหน — จุดเปลี่ยนสำคัญของเฟสนี้
เดิม (Phase 1-3) run_command กันเน็ตด้วย blocklist ไบนารีเท่านั้น (python3 ยังแอบเปิด socket เองได้) **ตอนนี้ถ้าเปิด `--isolation-tier bubblewrap`:**
- Sandbox ไม่มี network device ให้ใช้เลยแม้แต่ loopback ของจริง (มีแค่ loopback ส่วนตัวของตัวเอง แยกขาดจาก host) — **นี่คือการตัดเน็ตระดับ kernel จริง ไม่ใช่ blocklist**
- ทดสอบจริง: เชื่อมต่อ IP ภายนอกไม่ได้เลย (ไม่มี route), เชื่อมต่อ service จริงบน host (llama-server ที่ port 8080) ไม่ได้เลย

## เข้าถึงเครื่องได้แค่ไหน — จุดเปลี่ยนสำคัญของเฟสนี้
`--isolation-tier bubblewrap`:
- มองเห็นแค่ path ที่ bind ไว้ชัดเจน (`/usr`, `/lib`, workspace) — path อื่นของเครื่องจริง **ไม่มีอยู่เลย** ในมุมมองของ sandbox (ไม่ใช่แค่ "ห้ามเข้า" แต่ "ไม่มีให้เข้า")
- ทดสอบจริง: อ่าน `/etc/shadow` หรือโฟลเดอร์ home จริงไม่ได้เลย
- จำกัดหน่วยความจำ (512MB) และ CPU time (10 วินาที) ต่อคำสั่ง — ทดสอบจริงว่า allocate 2GB ล้มเหลว และ loop ไม่รู้จบถูกฆ่าที่ CPU cap พอดี ไม่ต้องรอ timeout เต็ม
- `--isolation-tier direct` (ค่าเริ่มต้น) ยังเป็นแบบ Phase 1-3 เหมือนเดิมทุกอย่าง — **operator เลือกเอง ไม่ใช่โมเดลเลือกเอง**
- **microVM tier (Firecracker) ยังไม่ทำ** — ตรวจสอบแล้วว่า KVM มีอยู่จริงบนเครื่องนี้ (ทำได้ในทางเทคนิค) แต่ทำให้สมบูรณ์จริงต้องมี guest kernel + rootfs + vsock pipeline ซึ่งเป็นงาน infra ขนาดใหญ่ ตัดสินใจร่วมกันว่าทำ bubblewrap ให้เสร็จสมบูรณ์จริงก่อน ดีกว่าทำ Firecracker แบบครึ่งๆ กลางๆ แล้วอ้างว่าปลอดภัยทั้งที่ยังไม่ผ่านการทดสอบจริง

## กันปัญหา context ระเบิด
เหมือน Phase 1-3 ทุกอย่าง ไม่ได้เปลี่ยน

## Evidence store (ที่เก็บหลักฐาน)
- เข้ารหัสด้วย Fernet, content-addressed (เนื้อหาเดียวกัน = digest เดียวกัน), ตรวจสอบความถูกต้องทุกครั้งที่อ่าน (ทดสอบด้วยการแก้ไข blob แล้วยืนยันจับได้)
- Raw output เต็มของทุกการเรียก broker-mediated tool เก็บที่นี่ — audit log เก็บแค่ excerpt 2000 ตัวอักษร + digest อ้างอิงมาที่นี่

## Findings + Report
- โครงสร้าง finding (title, severity, target, evidence, remediation) เก็บเป็น JSONL
- Export เป็น **SARIF** (มาตรฐานสำหรับเครื่องมือความปลอดภัยอื่นอ่านต่อได้) และ **Markdown → PDF**
- ทดสอบด้วย finding จริงจากการสแกน Juice Shop จริง 2 รายการ พร้อมอ้างอิง evidence/audit ย้อนกลับได้ครบ

## Evaluation harness (วัดความสามารถ)
รัน task จริง 3 อย่างผ่านเครื่องมือจริงกับ Juice Shop จริง (ไม่ mock) แล้ววัดว่าโมเดล+เครื่องมือทำสำเร็จไหม:
1. หา header `X-Recruiting` ผ่าน `http_recon`
2. หา port 3000 ที่เปิดอยู่ผ่าน `port_discovery`
3. รายงาน scope denial ถูกต้องเมื่อขอ target นอกขอบเขต

**ผลทดสอบจริง: 3/3 ผ่าน** พร้อมบันทึก model/quant, seed, policy version, target ไว้ครบเพื่อรันซ้ำแล้วได้ผลเดิม

## เจอบั๊กจริงและแก้แล้ว (คุ้มที่จะรู้ไว้)
1. **mount order ผิด**: `--tmpfs /tmp` ทับ workspace bind (workspace อยู่ใต้ /tmp โดย default) — แก้ลำดับแล้ว
2. **ConnectionRefusedError ดูเหมือนรั่วแต่จริงๆ ไม่รั่ว**: sandbox มี loopback ส่วนตัวแยกขาด การเชื่อมต่อ 127.0.0.1:8080 จากใน sandbox ถูกปฏิเสธเพราะไม่มีอะไรฟังอยู่ที่ loopback ส่วนตัวนั้น (ไม่ใช่ไปเจอ llama-server จริงบน host) — ตรวจสอบยืนยันด้วยการดู interface และเทียบ error กับการต่อ IP ภายนอกจริง
3. **PDF generator กิน RAM หลาย GB วนไม่จบ**: การพยายามแก้ปัญหาข้อความยาวเกินด้วย `WrapMode.CHAR` กลับทำให้ library วนลูปหนัก แก้ด้วยการตัดข้อความยาวเป็นท่อนสั้นเองแทน
4. **PDF "พื้นที่ไม่พอ" ทั้งที่ข้อความสั้น**: ต้นเหตุจริงคือ cursor ค้างอยู่ขอบขวาหลัง render แต่ละบรรทัด ไม่เกี่ยวกับความยาวข้อความเลย

## ที่ตัดขอบเขตออกอย่างตรงไปตรงมา
microVM tier, image signing (ยังไม่มี image ที่ tier นี้), automated evidence retention/deletion, PDF รองรับภาษาที่ไม่ใช่ Latin

## วิธีรัน
```bash
python3 -m agent.main --security-tools --isolation-tier bubblewrap
python3 -m agent.sandbox.test_isolation      # ทดสอบ sandbox escape/egress/resource-limit ซ้ำได้
python3 -m agent.eval.harness                # รัน benchmark ซ้ำได้ (ผลเก็บที่ agent/state/eval_results/)
```
