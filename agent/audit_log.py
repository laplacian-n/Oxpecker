"""§8a: tamper-evident JSONL audit log.

Per the reviewed doc's correction: "a hash chain alone is not tamper-proof — an attacker able
to rewrite the log can recompute it." This implementation adds a second, separate append-only
checkpoint file (written after every entry) that records the running chain head. Verifying the
main log means recomputing the chain from record 0 AND confirming every recorded checkpoint
head matches the recomputed hash at that index — an attacker would have to rewrite both files
consistently, and the checkpoint file is opened in append-only mode and never read-modified by
normal operation, which at least raises the bar past "just recompute the hash chain."　This is
a Phase-1-appropriate baseline, not the "signed by a key unavailable to the agent runtime"
design the doc flags as the fuller fix (deferred: that needs a key management story this
single-process prototype doesn't have yet).
"""
from __future__ import annotations

import hashlib
import json
import socket
import time
import uuid
from pathlib import Path
from typing import Any

from . import config
from .evidence.store import _load_or_create_keys, compute_digest

GENESIS_HASH = "0" * 64


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class AuditLog:
    def __init__(
        self, session_id: str, audit_dir: Path | None = None, evidence_key_path: Path | None = None
    ):
        # Resolved from the module namespace at call time, not bound as a default-argument
        # value at import time — a `audit_dir: Path = config.AUDIT_DIR` default would silently
        # ignore `patch("agent.audit_log.config.AUDIT_DIR", tmp_dir)` in a test. Same bug already
        # found and fixed in agent/session.py, agent/internet/budget.py, agent/skills/signing.py,
        # agent/internet/cache.py this session.
        resolved = audit_dir if audit_dir is not None else config.AUDIT_DIR
        resolved.mkdir(parents=True, exist_ok=True)
        self.log_path = resolved / f"{session_id}.jsonl"
        self.checkpoint_path = resolved / f"{session_id}.checkpoints.jsonl"
        self.session_id = session_id
        self.client_machine = socket.gethostname()
        self._prev_hash = self._load_last_hash()
        # ADR-0005: content_digest uses the same HMAC-SHA256 scheme and key as
        # agent/evidence/store.py's EvidenceStore, so Broker._finalize()'s
        # `entry["content_digest"] == evidence_digest` cross-reference keeps holding — this is a
        # second, independent use of the existing evidence-encryption key, not a new one.
        key_path = evidence_key_path if evidence_key_path is not None else config.EVIDENCE_KEY_PATH
        self._keys, self._current_key_version = _load_or_create_keys(key_path)

    def _load_last_hash(self) -> str:
        if not self.log_path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        with self.log_path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                last = json.loads(line)["entry_hash"]
        return last

    def record(
        self,
        *,
        turn_index: int,
        tool_name: str,
        action_rationale: str,
        arguments: dict,
        raw_output: str,
        sanitized_output: str,
        scope_decision: str,
        approval_identity: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        latency_ms: float,
        exit_code: int | None,
        injection_flagged: bool,
        prompt_version: str | None = None,
    ) -> dict:
        # Re-sync from disk on every write, not just __init__: a separate process (e.g. the
        # Phase-3 security-tools MCP subprocess, which does its own broker-mediated audit
        # writes to this same session's log) may have appended entries since this AuditLog
        # instance was constructed. Writing with a stale cached _prev_hash would fork the
        # chain — verify() would then see a genuine break, not just a false alarm.
        self._prev_hash = self._load_last_hash()
        entry = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": self.session_id,
            "turn_index": turn_index,
            "client_machine": self.client_machine,
            "tool_name": tool_name,
            "action_rationale": action_rationale,
            "arguments": _redact(arguments),
            "content_digest": compute_digest(
                raw_output.encode("utf-8", "replace"), self._keys[self._current_key_version]
            ),
            "raw_output_excerpt": raw_output[:2000],
            "sanitized_output": sanitized_output[:2000],
            "scope_decision": scope_decision,
            "approval_identity": approval_identity,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": round(latency_ms, 1),
            "exit_code": exit_code,
            "injection_flagged": injection_flagged,
            "prompt_version": prompt_version,
            "prev_hash": self._prev_hash,
        }
        entry_hash = hashlib.sha256(
            (self._prev_hash + _canonical(entry)).encode("utf-8")
        ).hexdigest()
        entry["entry_hash"] = entry_hash

        with self.log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        with self.checkpoint_path.open("a") as f:
            f.write(json.dumps({"index_hash": entry_hash, "timestamp": entry["timestamp"]}) + "\n")

        self._prev_hash = entry_hash
        return entry


def _redact(arguments: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in arguments.items():
        if any(marker.lower() in k.lower() for marker in config.SECRET_ENV_MARKERS):
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def verify(session_id: str, audit_dir: Path | None = None) -> tuple[bool, str]:
    """Recompute the chain and cross-check it against the separate checkpoint file.

    Detects deletion, reordering, and modification of log entries (Phase-1 exit criterion).
    """
    resolved = audit_dir if audit_dir is not None else config.AUDIT_DIR
    log_path = resolved / f"{session_id}.jsonl"
    checkpoint_path = resolved / f"{session_id}.checkpoints.jsonl"
    if not log_path.exists():
        return False, "log file missing"

    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    checkpoints = (
        [json.loads(line) for line in checkpoint_path.read_text().splitlines() if line.strip()]
        if checkpoint_path.exists()
        else []
    )
    if len(entries) != len(checkpoints):
        return False, f"entry/checkpoint count mismatch: {len(entries)} vs {len(checkpoints)}"

    prev_hash = GENESIS_HASH
    for i, entry in enumerate(entries):
        if entry.get("prev_hash") != prev_hash:
            return False, f"chain break at index {i}: prev_hash mismatch"
        stored_hash = entry.get("entry_hash")
        recomputed = dict(entry)
        recomputed.pop("entry_hash", None)
        expected_hash = hashlib.sha256(
            (prev_hash + _canonical(recomputed)).encode("utf-8")
        ).hexdigest()
        if stored_hash != expected_hash:
            return False, f"hash mismatch at index {i}: entry was modified"
        if checkpoints[i]["index_hash"] != stored_hash:
            return False, f"checkpoint mismatch at index {i}: log and checkpoint disagree"
        prev_hash = stored_hash

    return True, f"verified {len(entries)} entries, chain intact"
