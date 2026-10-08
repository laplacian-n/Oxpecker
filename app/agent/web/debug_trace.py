"""A debug sink, deliberately separate from the audit log.

The two have requirements that conflict, and merging them damages both. The audit log is
hash-chained, append-only, redacted through `_redact()` and capped at 2000-character excerpts,
because its job is accountability. Debugging needs the opposite: the prompt exactly as assembled,
the model's raw output, retrieval scores, and what compaction threw away — unbounded, and
unredacted, because a secret appearing in a prompt is often the thing being debugged. Putting
that in the audit log would bloat the hash chain and place secrets precisely where redaction was
designed to keep them out.

So: two sinks, correlated by `(session_id, turn_index)` and by the audit entry's `entry_id`.

The blind spot this exists for first is compaction. `_maybe_compact()` folds older messages into
an LLM-written summary and logged only `log.info("compacted %d msgs")` — the count, never which
messages went or what the summary said. "Why did the agent forget what it found at step 5" was
therefore unanswerable, and that is the failure mode long agent runs hit most. Both compaction
paths (session-level and the mid-turn one) now record their deltas here.

**These files contain unredacted prompts, model output and tool results.** On a pentest run that
means credentials, tokens and target data in plaintext. They live in their own directory so they
can be deleted independently of the audit trail, every file opens with a warning line, and the
sink can be turned off with `OXPECKER_DEBUG_TRACE=0`. Do not attach one to a bug report without
reading it first.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .. import config

log = logging.getLogger("agent.web.debug_trace")

TRACE_DIR = Path(os.environ.get("OXPECKER_DEBUG_TRACE_DIR", str(config.STATE_DIR / "debug_trace")))

# A cap rather than rotation: a trace is for diagnosing the run that is happening now, and a
# silently rotating file is a worse answer than a file that says plainly it stopped. When the cap
# is hit the sink writes one final marker and goes quiet for that session.
MAX_BYTES_PER_SESSION = 64 * 1024 * 1024

_WARNING = (
    "UNREDACTED DEBUG TRACE — contains prompts, model output and tool results verbatim, "
    "including any credentials or target data they carry. Review before sharing. "
    "Disable with OXPECKER_DEBUG_TRACE=0."
)


def enabled() -> bool:
    """Default on. A trace that has to be switched on before it is useful is usually switched on
    after the bug has already gone, and this is an operator-run lab tool rather than a service
    handling other people's data. The cost is stated in the module docstring and in every file's
    first line, not hidden."""
    return os.environ.get("OXPECKER_DEBUG_TRACE", "1").strip().lower() not in ("0", "false", "no")


def _preview(text, limit: int = 400) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit} chars]"


class DebugTrace:
    def __init__(self, session_id: str, trace_dir: Path | None = None):
        self.session_id = session_id
        resolved = trace_dir if trace_dir is not None else TRACE_DIR
        self._dir = resolved
        self.path = resolved / f"{session_id}.jsonl"
        self._stopped = False
        self._header_written = self.path.exists()

    def record(self, kind: str, *, turn_index: int | None = None, **fields) -> None:
        """Never raises. A debug sink that can break the run it is observing is worse than no
        sink — the one thing it must not do is become the bug."""
        if self._stopped or not enabled():
            return
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            if not self._header_written:
                with self.path.open("a") as f:
                    f.write(json.dumps({"kind": "_header", "warning": _WARNING,
                                        "session_id": self.session_id,
                                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                    time.gmtime())}) + "\n")
                self._header_written = True

            if self.path.exists() and self.path.stat().st_size > MAX_BYTES_PER_SESSION:
                with self.path.open("a") as f:
                    f.write(json.dumps({
                        "kind": "_truncated",
                        "detail": f"debug trace stopped at {MAX_BYTES_PER_SESSION} bytes; "
                                  "later events for this session are not recorded",
                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }) + "\n")
                self._stopped = True
                return

            entry = {
                "kind": kind,
                "ts": time.time(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_id": self.session_id,
                "turn_index": turn_index,
                **fields,
            }
            with self.path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception as e:
            log.warning("debug trace write failed (continuing): %s", e)

    # ---- the specific events, named so callers cannot disagree on field spelling ----

    def compaction(self, *, scope: str, folded_messages: list[dict], summary: str,
                   summary_upto: int | None = None, kept: int | None = None,
                   prompt_tokens: int | None = None, turn_index: int | None = None) -> None:
        """What compaction actually removed, and what replaced it.

        `scope` is "session" for _maybe_compact or "in_turn" for _compact_working_messages —
        two different code paths with the same failure mode, so they record the same shape.

        The folded messages are recorded as role/tool/preview rather than in full: the point is
        to answer "was the thing it forgot in here", which a preview settles, while the full
        text of 24 messages per compaction would dominate the file.
        """
        self.record(
            "compaction",
            turn_index=turn_index,
            scope=scope,
            folded_count=len(folded_messages),
            summary_upto=summary_upto,
            kept_verbatim=kept,
            prompt_tokens_at_compaction=prompt_tokens,
            summary=summary,
            summary_chars=len(summary or ""),
            folded=[
                {
                    "index": m.get("_index"),
                    "role": m.get("role"),
                    "tool_name": (m.get("extra") or {}).get("tool_name") or m.get("tool_name"),
                    "chars": len(m.get("content") or ""),
                    "preview": _preview(m.get("content") or ""),
                }
                for m in folded_messages
            ],
        )

    def prompt(self, *, turn_index: int, system_content: str, messages: list[dict],
               prompt_tokens: int | None = None, summary_present: bool = False) -> None:
        self.record(
            "prompt",
            turn_index=turn_index,
            prompt_tokens=prompt_tokens,
            summary_present=summary_present,
            system_chars=len(system_content or ""),
            system_content=system_content,
            message_count=len(messages),
            messages=[
                {"role": m.get("role"), "chars": len(m.get("content") or ""),
                 "preview": _preview(m.get("content") or "")}
                for m in messages
            ],
        )

    def model_output(self, *, turn_index: int, content: str, reasoning: str = "",
                     tool_calls: list | None = None, latency_ms: float | None = None) -> None:
        self.record(
            "model_output",
            turn_index=turn_index,
            latency_ms=latency_ms,
            content=content,
            reasoning=reasoning,
            reasoning_chars=len(reasoning or ""),
            tool_calls=tool_calls or [],
        )

    def retrieval(self, *, turn_index: int | None, query: str, results: list[dict]) -> None:
        """Chunk identity and score per hit. Without these, "why did it believe that" cannot be
        traced back to a bad retrieval."""
        self.record(
            "retrieval",
            turn_index=turn_index,
            query=query,
            result_count=len(results),
            hits=[
                {
                    "score": r.get("score"),
                    "source": r.get("source"),
                    "chunk_id": r.get("chunk_id") or r.get("id"),
                    "title": r.get("title"),
                    "preview": _preview(r.get("text") or r.get("content") or "", 200),
                }
                for r in results
            ],
        )

    def tool(self, *, turn_index: int, tool_name: str, requested_name: str, arguments: dict,
             result, audit_entry_id: str | None = None, latency_ms: float | None = None,
             injection_verdict: str | None = None) -> None:
        """The full untruncated result, alongside the audit entry's id so the redacted,
        hash-chained record and this one can be lined up."""
        self.record(
            "tool",
            turn_index=turn_index,
            tool_name=tool_name,
            requested_name=requested_name,
            arguments=arguments,
            result=result,
            audit_entry_id=audit_entry_id,
            latency_ms=latency_ms,
            injection_verdict=injection_verdict,
        )


_traces: dict[str, DebugTrace] = {}

# Bounded for the same reason as the audit-log cache: a long-lived server would otherwise keep
# one object per session seen since start. These hold no unflushed state — each write opens and
# closes the file — so evicting one loses nothing but the header flag, which is recomputed from
# the file's existence.
_TRACE_CACHE_MAX = 200


def for_session(session_id: str) -> DebugTrace:
    trace = _traces.get(session_id)
    if trace is None:
        if len(_traces) >= _TRACE_CACHE_MAX:
            for stale in list(_traces)[: len(_traces) - _TRACE_CACHE_MAX + 1]:
                _traces.pop(stale, None)
        trace = _traces[session_id] = DebugTrace(session_id)
    return trace
