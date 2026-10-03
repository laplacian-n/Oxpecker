"""Cross-turn taint tracking for M4.5 — connects injection_guard's verdicts to the broker's
approval gate. File-based (matches the kill_switch/audit_log pattern already used throughout
this project) so it works across process boundaries: the generic agent loop and the
security-tools MCP subprocess are separate processes, and both need to see the same taint state
for a session.

Design, deliberately simple for a Phase-4-appropriate baseline: any tool output scanned as
`suspicious`, `malicious`, or `unknown` marks the session tainted for a time window (not a
precise per-value data-flow taint — that would need real dataflow tracking through the model's
own reasoning, which isn't observable). While tainted, the broker escalates any action beyond
`passive_recon` to require human approval, on the theory that a session which just processed
untrusted content deserves a closer look before it does anything with side effects — a
deliberately conservative, session-wide precaution rather than a precise per-argument trace.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config

TAINT_WINDOW_S = 300  # 5 minutes of elevated scrutiny after a suspicious/malicious/unknown scan
SAFE_WHILE_TAINTED = {"passive_recon"}  # action classes still allowed without extra approval


class TaintStore:
    def __init__(self, session_id: str, taint_dir: Path = config.STATE_DIR / "broker" / "taint"):
        self.session_id = session_id
        self.path = taint_dir / f"{session_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def mark(self, *, reason: str, verdict: str, source: str) -> None:
        now = time.time()
        record = self._read()
        record["tainted_until"] = now + TAINT_WINDOW_S
        record.setdefault("history", []).append(
            {"reason": reason, "verdict": verdict, "source": source, "at": now}
        )
        self.path.write_text(json.dumps(record))

    def is_tainted(self) -> tuple[bool, dict | None]:
        record = self._read()
        tainted_until = record.get("tainted_until")
        if tainted_until is None or time.time() >= tainted_until:
            return False, None
        history = record.get("history") or []
        return True, (history[-1] if history else None)

    def clear(self) -> None:
        self.path.write_text(json.dumps({}))

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
