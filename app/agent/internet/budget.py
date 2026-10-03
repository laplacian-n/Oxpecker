"""M5.5 — per-channel, per-session budget tracking. File-based (same pattern as
agent/broker/taint.py) so it works across process boundaries and survives a process restart
within a session — a budget that silently reset on every restart would be a real gap, not a
convenience.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config


@dataclass(frozen=True)
class ChannelBudget:
    max_queries: int
    max_bytes: int
    window_s: float


DEFAULT_BUDGETS: dict[str, ChannelBudget] = {
    "target_http": ChannelBudget(max_queries=200, max_bytes=20_000_000, window_s=3600),
    "knowledge_search": ChannelBudget(max_queries=20, max_bytes=2_000_000, window_s=3600),
    "knowledge_fetch": ChannelBudget(max_queries=50, max_bytes=10_000_000, window_s=3600),
    "osint_discovery": ChannelBudget(max_queries=100, max_bytes=2_000_000, window_s=3600),
}

BUDGETS_STATE_DIR = config.STATE_DIR / "internet" / "budgets"


class UnknownChannelError(RuntimeError):
    pass


class BudgetExceededError(RuntimeError):
    pass


class ChannelBudgetTracker:
    def __init__(
        self, session_id: str, channel: str, budget: ChannelBudget | None = None,
        state_dir: Path | None = None,
    ):
        if budget is None and channel not in DEFAULT_BUDGETS:
            raise UnknownChannelError(f"unknown channel {channel!r}, must be one of {list(DEFAULT_BUDGETS)}")
        self.channel = channel
        self.budget = budget or DEFAULT_BUDGETS[channel]
        # Read the module-level default at call time, not at function-definition time, so tests
        # can redirect it via `patch("agent.internet.budget.BUDGETS_STATE_DIR", tmp_dir)` — a
        # `Path = BUDGETS_STATE_DIR` default argument would bind once at import and silently
        # ignore that patch, which is exactly what leaked real files into agent/state/internet/
        # the first time this was tested.
        resolved_dir = state_dir if state_dir is not None else BUDGETS_STATE_DIR
        self.path = resolved_dir / f"{session_id}__{channel}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"window_start": time.time(), "queries": 0, "bytes": 0}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"window_start": time.time(), "queries": 0, "bytes": 0}

    def _current_window(self, record: dict) -> dict:
        now = time.time()
        if now - record["window_start"] >= self.budget.window_s:
            return {"window_start": now, "queries": 0, "bytes": 0}
        return record

    def check_and_consume(self, *, query_cost: int = 1, byte_cost: int = 0) -> dict:
        record = self._current_window(self._read())
        if record["queries"] + query_cost > self.budget.max_queries:
            self.path.write_text(json.dumps(record))
            raise BudgetExceededError(
                f"{self.channel}: query budget exceeded "
                f"({record['queries']}+{query_cost} > {self.budget.max_queries})"
            )
        if record["bytes"] + byte_cost > self.budget.max_bytes:
            self.path.write_text(json.dumps(record))
            raise BudgetExceededError(
                f"{self.channel}: byte budget exceeded "
                f"({record['bytes']}+{byte_cost} > {self.budget.max_bytes})"
            )
        record["queries"] += query_cost
        record["bytes"] += byte_cost
        self.path.write_text(json.dumps(record))
        return record

    def usage(self) -> dict:
        return self._current_window(self._read())
