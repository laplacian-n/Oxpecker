"""Local, single-file session persistence (Phase 1: no FastAPI memory service — one machine).

Raw messages are appended as immutable JSONL records. Working-memory eviction (budget.py)
removes turns from the *live* context window but always persists them here first — nothing is
lost, only made no longer part of the next LLM call.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import config

SCHEMA_VERSION = 1


class SessionStore:
    def __init__(self, session_id: str, sessions_dir: Path | None = None):
        # Resolved from the module namespace at call time, not bound as a default-argument
        # value at import time — a `sessions_dir: Path = config.SESSIONS_DIR` default would
        # silently ignore `patch("agent.session.config.SESSIONS_DIR", tmp_dir)` in a test. This
        # exact bug already leaked real state three times this session (agent/internet/budget.py,
        # agent/skills/signing.py, agent/internet/cache.py); fixed here pre-emptively once a test
        # (agent/test_loop_steer.py) actually needed to isolate it.
        resolved = sessions_dir if sessions_dir is not None else config.SESSIONS_DIR
        resolved.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.path = resolved / f"{session_id}.jsonl"

    @staticmethod
    def new_session_id() -> str:
        return str(uuid.uuid4())

    def append(self, role: str, content: str, extra: dict | None = None) -> dict:
        record = {
            "message_id": str(uuid.uuid4()),
            "session_id": self.session_id,
            "role": role,
            "origin_device": "local",
            "content": content,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "schema_version": SCHEMA_VERSION,
        }
        if extra:
            record.update(extra)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return record

    def load_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def load_as_messages(self) -> list[dict]:
        """Reconstruct an OpenAI-style messages list from the persisted record stream."""
        messages = []
        for record in self.load_all():
            msg = {"role": record["role"], "content": record["content"]}
            if "tool_call_id" in record:
                msg["tool_call_id"] = record["tool_call_id"]
            if "tool_calls" in record:
                msg["tool_calls"] = record["tool_calls"]
            if "name" in record:
                msg["name"] = record["name"]
            messages.append(msg)
        return messages
