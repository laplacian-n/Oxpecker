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
import os
import time
from pathlib import Path

from .. import config

TAINT_WINDOW_S = 300  # 5 minutes of elevated scrutiny after a suspicious/malicious/unknown scan
SAFE_WHILE_TAINTED = {"passive_recon"}  # action classes still allowed without extra approval

# AGENT_ARCHITECTURE.md §14.1 C: taint is per session, but some state is shared across all of an
# engagement's workers. The tools whose tainted output can reach another worker through that
# shared state — so their taint must escalate to the whole engagement, not stay on one session.
# Today that is the browser: §6.1 shares one browser profile (cookie jar, storage) per
# engagement, so content that tainted worker A's browser session is carried by worker B, which
# is untainted, through the same jar. `write_file`/`run_command` join this set once the workspace
# is engagement-shared too; today each session gets a fresh temp workspace, so their taint is
# genuinely session-local and must not over-escalate.
ENGAGEMENT_SHARED_TAINT_SOURCES = {"browser_fetch"}


class TaintStore:
    """Session taint, and — when an `engagement_id` is supplied and a mark is `shared` —
    engagement taint too (§14.1 C). A session is treated as tainted if its own record is active
    OR its engagement's is, because the engagement-shared vector (the browser profile) carries
    the attacker-controlled material between workers regardless of which session first saw it.

    Constructed with `session_id` alone, it behaves exactly as before — no engagement file is
    read or written — so every pre-§14.1-C caller is unchanged."""

    def __init__(
        self,
        session_id: str,
        engagement_id: str | None = None,
        taint_dir: Path = config.STATE_DIR / "broker" / "taint",
    ):
        self.session_id = session_id
        self.engagement_id = engagement_id
        self.path = taint_dir / f"{session_id}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Engagement records live in their own subdir so an engagement_id can never collide with a
        # session_id in the same namespace.
        if engagement_id is not None:
            self.engagement_path: Path | None = taint_dir / "engagement" / f"{engagement_id}.json"
            self.engagement_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.engagement_path = None

    def mark(self, *, reason: str, verdict: str, source: str, shared: bool = False) -> None:
        """Mark the session tainted. When `shared` is true and this store knows its engagement,
        also mark the engagement tainted — so a sibling worker sharing the browser profile is
        scrutinised even though its own session never saw the content. `shared` is the caller's
        statement that the tainting vector is engagement-shared state (see
        `ENGAGEMENT_SHARED_TAINT_SOURCES`); a plain session-local read leaves it false."""
        self._mark_path(self.path, reason=reason, verdict=verdict, source=source)
        if shared and self.engagement_path is not None:
            self._mark_path(self.engagement_path, reason=reason, verdict=verdict, source=source)

    def _mark_path(self, path: Path, *, reason: str, verdict: str, source: str) -> None:
        now = time.time()
        record, _ = self._read_path(path)
        record["tainted_until"] = now + TAINT_WINDOW_S
        record.setdefault("history", []).append(
            {"reason": reason, "verdict": verdict, "source": source, "at": now}
        )
        self._write_path(path, record)

    def is_tainted(self) -> tuple[bool, dict | None]:
        """Tainted if this session's record is active, or — when an engagement is known — the
        engagement's is. The session is checked first so its reason is the one reported when both
        apply; the engagement check is what catches the §14.1 C cross-worker carry."""
        tainted, info = self._check_path(self.path)
        if tainted:
            return True, info
        if self.engagement_path is not None:
            eng_tainted, eng_info = self._check_path(self.engagement_path)
            if eng_tainted:
                if eng_info is not None and "reason" in eng_info:
                    eng_info = {**eng_info, "scope": "engagement"}
                return True, eng_info
        return False, None

    def _check_path(self, path: Path) -> tuple[bool, dict | None]:
        record, unreadable = self._read_path(path)
        if unreadable:
            # Fail closed. A taint file that exists but cannot be parsed is not evidence that
            # the session is clean — it is the absence of evidence either way, and the whole
            # point of this store is that a session which handled attacker-controlled text gets
            # extra scrutiny. Reading it as clean was the actual bug: `write_text` truncates
            # before it writes, so a concurrent reader (the agent loop and the security-tools
            # MCP subprocess share this file by design) landed in the write window, saw an empty
            # or half-written file, and the broker skipped the human-approval escalation with
            # the audit trail recording `succeeded`/`n/a` — indistinguishable from an action on
            # a session that was never tainted.
            return True, {
                "reason": "taint record exists but could not be read; treating the session as "
                          "tainted because an unreadable record cannot show it is clean",
                "verdict": "unknown",
                "source": "taint_store",
                "at": time.time(),
            }
        tainted_until = record.get("tainted_until")
        if tainted_until is None or time.time() >= tainted_until:
            return False, None
        history = record.get("history") or []
        if history:
            return True, history[-1]
        # A record with a window but no history: still tainted, and the caller needs a dict it
        # can read a reason out of. Returning None here made `broker.dispatch` raise a
        # TypeError on `taint_info['reason']`, which left the caller with no ActionResponse and
        # the trail with no entry at all.
        return True, {
            "reason": "session marked tainted with no recorded history entry",
            "verdict": "unknown",
            "source": "taint_store",
            "at": time.time(),
        }

    def clear(self) -> None:
        """Clear this session's taint. The engagement record is deliberately NOT cleared here: it
        is shared, and one session deciding it is done does not make the shared browser profile
        clean for the others. The engagement window expires on its own."""
        self._write_path(self.path, {})

    def _write_path(self, path: Path, record: dict) -> None:
        """Write-to-temp-then-replace, with the pid in the temp name.

        `os.replace` is atomic, so a reader sees either the old file or the new one and never a
        truncated one. The pid matters because two processes sharing one temp name race on the
        replace itself and one of them gets FileNotFoundError — the bug the broker's idempotency
        cache had. `approval_queue._write_atomic` already does it this way; this store did not.
        """
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps(record))
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — the replace normally consumed it
                pass

    def _read_path(self, path: Path) -> tuple[dict, bool]:
        """(record, unreadable). `unreadable` separates "there is no record" from "there is a
        record and we cannot read it" — the first is a clean session, the second is not."""
        if not path.exists():
            return {}, False
        try:
            raw = path.read_text()
        except OSError:
            return {}, True
        if not raw.strip():
            # An empty file is a torn write, not a cleared session: `clear()` writes `{}`.
            return {}, True
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}, True
        if not isinstance(parsed, dict):
            return {}, True
        return parsed, False
