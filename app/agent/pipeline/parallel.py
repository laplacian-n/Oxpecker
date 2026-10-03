"""Phase 6 — bounded deterministic parallel executor, for low-risk recon only. Deliberately a
plain thread pool over independent callables, not a multi-LLM/multi-agent scheme — Codex's §3.6
and the integration prompt both call multi-agent orchestration a hard line this project doesn't
cross. "Deterministic" here means: given the same inputs, the same set of outcomes is produced
regardless of scheduling order — results are returned in input order, not completion order, and
one task's exception never loses or corrupts another task's result.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class TaskOutcome:
    index: int
    ok: bool
    result: object | None
    error: str | None


def run_bounded(tasks: list[Callable[[], T]], max_workers: int) -> list[TaskOutcome]:
    """Runs `tasks` with at most `max_workers` executing concurrently. Returns one TaskOutcome
    per task, in the same order as `tasks` — never raises for an individual task failure, so a
    caller can inspect every outcome rather than losing the whole batch to one exception."""
    if max_workers < 1:
        raise ValueError(f"max_workers must be >= 1, got {max_workers}")

    outcomes: list[TaskOutcome | None] = [None] * len(tasks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_index = {pool.submit(task): i for i, task in enumerate(tasks)}
        for future in concurrent.futures.as_completed(future_to_index):
            i = future_to_index[future]
            try:
                outcomes[i] = TaskOutcome(index=i, ok=True, result=future.result(), error=None)
            except Exception as e:  # a single task's failure must never lose the rest of the batch
                outcomes[i] = TaskOutcome(index=i, ok=False, result=None, error=f"{type(e).__name__}: {e}")
    return outcomes  # type: ignore[return-value]
