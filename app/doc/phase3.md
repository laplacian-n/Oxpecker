# Phase 3 — Execution Broker + Security Tools (ของจริงชิ้นแรก)

## ภาพรวม
เริ่มมี tool สำหรับทดสอบความปลอดภัยจริง 2 ตัวแรก (ไม่ใช่ generic terminal agent อีกต่อไป) — ทุกการเรียกต้องผ่าน **execution broker** ที่บังคับ scope ตาม Rules of Engagement (RoE) เสมอ ไม่ว่าโมเดลจะขออะไรก็ตาม

## โมเดลที่ใช้
เหมือน Phase 2 (imatrix i1-Q4_K_M) — ไม่ได้เปลี่ยนโมเดล

## Tools ที่มี (เพิ่ม 2 ตัวใหม่)
| Tool | หน้าที่ | หมายเหตุ |
|---|---|---|
| `http_recon` | เก็บ HTTP metadata (status, header, redirect chain) แบบ passive | ตาม redirect ทุก hop ต้องผ่านการตรวจ scope ใหม่ทุกครั้ง ไม่ auto-follow |
| `port_discovery` | สแกน TCP port แบบ connect-scan | **เขียนเองแทนการ wrap nmap** เพราะ nmap ไม่ได้ติดตั้งไว้ และการทำให้ nmap "แคบ" จริงๆ ต้อง allowlist flag เยอะกว่าการเขียนเองมาก |

ทั้งสองตัวไปผ่าน **broker** เสมอ (`agent/broker/broker.py`) ไม่ได้เรียกตรง

## เข้าถึงเน็ตได้แค่ไหน — จุดเปลี่ยนสำคัญของเฟสนี้
เดิม (Phase 1-2) run_command บล็อกด้วย blocklist เท่านั้น แต่ 2 tool ใหม่นี้ **ถูกจำกัด scope จริงจัง**:
- มีไฟล์ `engagement/scope.txt` (allowlist) และ `engagement/deny.txt` (denylist ตายตัว เช็คก่อนเสมอ ชนะทุกกรณี)
- ค่าเริ่มต้นตอนนี้: **จำกัดแค่ 127.0.0.1 / ::1 / localhost เท่านั้น** — เป้าหมายจริงคือ OWASP Juice Shop ที่รันใน Docker ผูกกับ loopback อย่างเดียว
- ก่อนต่อเชื่อมทุกครั้ง: resolve DNS ครั้งเดียว → เช็ค deny ก่อน → เช็ค allow → **ใช้ IP ที่ validate แล้วเชื่อมต่อ ไม่ resolve ซ้ำ** (กัน DNS-rebinding attack) — ทดสอบแล้วด้วย mock DNS ที่ตอบ IP ต่างกันสองครั้ง
- ทดสอบขอ target นอก scope (8.8.8.8) จริง → broker ปฏิเสธ โมเดลรายงานกลับให้ user แทนที่จะพยายามซ้ำ

## เข้าถึงเครื่องได้แค่ไหน
`run_command`/`read_file`/`write_file` เหมือน Phase 1-2 ทุกอย่าง — ยังไม่มี sandbox (มาใน Phase 4)

## กลไก broker (หัวใจของเฟสนี้)
- **RoE แบบ machine-readable** (`engagement/roe.json`) — กำหนดว่า action class ไหนอนุญาต, ช่วงเวลาที่ RoE ใช้ได้ (หมดอายุแล้วปฏิเสธทุกอย่างอัตโนมัติ)
- **Kill switch**: ไฟล์ flag เดียว เช็คก่อนทุกการ dispatch — engage แล้วการขอใหม่ถูกปฏิเสธภายใน <1ms และการสแกนที่กำลังทำอยู่หยุดกลางคันได้จริง (ทดสอบ deterministic แล้ว)
- **Rate limit**: cooldown ต่อ action class (ป้องกันยิงรัว)
- **Idempotency**: เรียกซ้ำด้วย key เดิมไม่ทำงานซ้ำ แค่ส่งผลลัพธ์เดิมกลับ
- **Audit ที่ reconstructable**: ทุกการตัดสินใจ (อนุญาต/ปฏิเสธ) มี record พร้อม policy_rule/policy_version ตรวจสอบย้อนหลังได้

## กันปัญหา context ระเบิด
เหมือน Phase 1-2 ทุกอย่าง ไม่ได้เปลี่ยน

## ทดสอบแล้ว (policy bypass suite 15/15 ข้อ)
IPv4/IPv6 ทั้ง in-scope และ out-of-scope, DNS resolve ไปนอก scope ถูกปฏิเสธ, deny.txt ชนะแม้ขัดกับ scope.txt, redirect ไปนอก scope ไม่ตามอัตโนมัติ, DNS-rebinding ป้องกันได้จริง, policy หาย/หมดอายุ → ปฏิเสธทุกอย่าง (fail closed), kill switch หยุดทันที + หยุดกลาง scan ได้

## เจอบั๊กจริงและแก้แล้ว
security tools MCP server รันเป็น subprocess แยกจาก process หลัก และเขียน audit log ไฟล์เดียวกัน — เดิม audit log แคช hash ล่าสุดไว้ในหน่วยความจำ ถ้าอีก process เขียนแทรกจะทำให้ chain พังเข้าใจผิดว่าโดน tamper แก้โดยให้อ่าน hash ล่าสุดจากไฟล์ใหม่ทุกครั้งก่อนเขียน

## ที่ตัดขอบเขตออก
ตั้งแค่ Juice Shop (ยังไม่ได้ตั้ง DVWA/Metasploitable — Metasploitable มี service เสี่ยงจริงเยอะ คิดว่าควรถามก่อนดึงเอง), approval workflow มีโครงในโบรกเกอร์แต่ยังไม่ผูกกับ tool ไหน (สองตัวนี้เสี่ยงต่ำพอ)

## วิธีรัน
```bash
python3 -m agent.main --security-tools --engagement-id lab-default
python3 -m agent.main --kill-switch status   # engage / disengage / status
python3 -m agent.broker.test_policy_bypass   # รัน policy bypass suite ซ้ำได้ตลอด
```
