# Phase 2 — Shared Mind (memory service + MCP tools)

## ภาพรวม
เพิ่มความสามารถ "หนึ่งสมอง หลายร่าง" — session/ความจำแชร์กันได้ข้ามเครื่อง (แม้ตอนนี้ทดสอบบนเครื่องเดียว), และเปลี่ยน tool เดิมให้เป็น MCP server จริง แทนการเรียกฟังก์ชัน Python ตรงๆ ในโปรเซสเดียวกัน

## โมเดลที่ใช้
เหมือน Phase 1 แต่ **เปลี่ยนไปใช้ imatrix quant** (`Qwen3-32B-abliterated.i1-Q4_K_M.gguf`) — แม่นยำกว่า quant เดิมที่ bit-depth เท่ากัน ทดสอบ tool-calling ซ้ำหลังเปลี่ยนแล้ว (8/8 trial ผ่าน) และ **เปิด `--api-key` บน llama-server แล้ว** (เดิมเปิดโล่งไม่มี auth บน LAN)

## Tools ที่มี
เหมือน Phase 1 ทั้ง 3 ตัว (`run_command`, `read_file`, `write_file`) แต่ตอนนี้เลือกได้ว่าจะรันแบบเดิม (in-process) หรือผ่าน **MCP subprocess** (`--mcp-tools`) — เป็นการเตรียมสถาปัตยกรรมให้ tool รันบน "เครื่อง client" แยกจาก "เครื่อง GPU" ในอนาคต

## เข้าถึงเน็ตได้แค่ไหน
เหมือน Phase 1 ทุกอย่าง (ยังเป็น blocklist ระดับไบนารี) **บวกเพิ่ม**: มี memory service ใหม่ที่เปิดผ่าน network จริง (port 8443) — ป้องกันด้วย **TLS (self-signed cert)** + **device token แยกต่างหากจาก llama-server** (คนละ credential คนละ audience ตามที่เอกสารกำชับ) ทดสอบแล้วว่า token ที่ถูก revoke ใช้ต่อไม่ได้ทันที และ TLS ถูกตรวจสอบจริง (ไม่ใช่แค่เปิดไว้เฉยๆ)

## เข้าถึงเครื่องได้แค่ไหน
เหมือน Phase 1 — ยังไม่มี sandbox แค่เปลี่ยนว่า tool รันในโปรเซสเดียวกันหรือ subprocess

## Memory service (สมองที่แชร์กันได้)
- FastAPI + SQLite (WAL mode) บนเครื่อง GPU box, พอร์ต 8443
- เก็บ conversation แบบ append-only, มี **optimistic concurrency**: ถ้าสอง client พยายามเขียนพร้อมกันโดยไม่รู้ตัวว่าอีกฝ่ายเขียนไปแล้ว จะได้ error ทันที ไม่ใช่ข้อมูลทับกันเงียบๆ (ทดสอบจริง: สอง AgentLoop ใช้ session เดียวกัน resume บริบทของกันได้ถูกต้อง)
- ลบ session แบบเต็มรูปแบบได้ (`DELETE /sessions/{id}`), backup/restore แบบ copy ไฟล์ SQLite ตรงๆ

## กันปัญหา context ระเบิด
เหมือน Phase 1 ทุกอย่าง (ไม่ได้เปลี่ยน) — เพิ่มแค่ทางเลือกว่า session จะเก็บไว้ในเครื่อง (JSONL) หรือใน memory service ระยะไกล

## ที่ยังไม่ทำ (deferred ตรงๆ)
- CPU embedding server + vector long-term memory (ยังไม่จำเป็นเพราะ eviction/summarization ของ Phase 1 ยังพอไหว)
- llama-swap เป็น door รวม
- ทดสอบข้ามเครื่องจริง (มีแค่เครื่องเดียวตอนสร้าง ทดสอบด้วยสอง device identity บนเครื่องเดียวกันแทน)

## เจอบั๊กจริงและแก้แล้ว
- Path escape error เดิมที่หลุดจน loop พัง (แก้ตั้งแต่ตอนสร้าง MCP wrapper)
- MCP client เวอร์ชันแรก connect/close คนละ async task กัน ทำให้ปิดแล้ว error — แก้ด้วย worker task เดียวตลอดอายุ session

## วิธีรัน
```bash
source .venv/bin/activate     # Phase 2 ขึ้นไปต้องใช้ venv (มี fastapi/mcp)
python3 -m agent.manage_devices add my-device
python3 -m agent.run_memory_service &     # ต้องรันค้างไว้ก่อน
python3 -m agent.main --mcp-tools --device-id my-device --device-token <token>
```
