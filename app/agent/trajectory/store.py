"""Trajectory record v1 — §7.1 / §8.1.1 / §12 row 6a.

"More, faster, more severe, cheaper" cannot be measured unless cost, wall-clock and progress are
recorded from the first run. The unit of measurement is the *experiment*: one hypothesis tried by
one method, with what it cost and whether it moved the engagement forward. A *chain* is a link
between experiments, not a copy of them, so it stores ids only. A *strategist turn* is a record
too, and it stores the options the strategist declined as well as the ones it dispatched, because
"answer, dispatch nothing" and "considered X and rejected it" are both decisions worth measuring.

Storage follows the findings store: one append-only JSONL file per engagement. All three record
kinds share that file and are told apart by their `kind` field.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import config


def _check_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")


@dataclass
class ExperimentRecord:
    experiment_id: str
    hypothesis_id: str
    method_summary: str
    expected_observation: str = ""
    observed_result: str = ""
    cost: float = 0.0
    wall_clock_s: float = 0.0
    # True when a graph write, a new observation or a verdict happened during this experiment.
    # False experiments are the §7.1 waste: work that changed nothing the engagement can use.
    made_progress: bool = False
    query_count: int = 0
    action_count: int = 0  # together with query_count, the query:action ratio
    provenance: str = ""  # who/what ran it, e.g. a device_id or worker id
    kind: str = "experiment"
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self):
        _check_non_negative("cost", self.cost)
        _check_non_negative("wall_clock_s", self.wall_clock_s)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StrategistRecord:
    turn_id: str
    # The dispatched [hypothesis_id, method] pairs. Empty is valid: a turn that answers and
    # dispatches nothing is a decision, not a missing record.
    chosen: list[list] = field(default_factory=list)
    # Options the strategist considered and did not take. Free-form dicts, so each turn can say
    # what it rejected and why in whatever shape that turn's reasoning produced.
    declined: list[dict] = field(default_factory=list)
    cost: float = 0.0
    wall_clock_s: float = 0.0
    kind: str = "strategist"
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self):
        _check_non_negative("cost", self.cost)
        _check_non_negative("wall_clock_s", self.wall_clock_s)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChainLink:
    # A link, not a copy: references experiments by id and never duplicates their contents.
    chain_id: str
    experiment_ids: list[str] = field(default_factory=list)
    # The chain's eventual outcome. Written append-only: a later outcome is a new ChainLink with
    # the same chain_id, and readers take the latest one.
    outcome: str = ""
    kind: str = "chain"
    recorded_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class TrajectoryStore:
    def __init__(self, engagement_id: str, trajectory_dir: Path = config.TRAJECTORY_DIR):
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        self.engagement_id = engagement_id
        self.path = trajectory_dir / f"{engagement_id}.jsonl"

    def _append(self, record_dict: dict) -> None:
        # One line, one write, opened in append mode: concurrent writers cannot clobber each
        # other the way a read-modify-write rewrite (findings' _write_atomic) would. Records are
        # never rewritten, so there is nothing to replace atomically.
        with self.path.open("a") as f:
            f.write(json.dumps(record_dict) + "\n")

    def record_experiment(self, record: ExperimentRecord) -> ExperimentRecord:
        self._append(record.to_dict())
        # §4.2 trajectory_experiment — best-effort, like every other producer's emit.
        from ..engagement import emit as event_emit
        event_emit.emit(
            self.engagement_id,
            "trajectory_experiment",
            {
                "experiment_id": record.experiment_id,
                "hypothesis_id": record.hypothesis_id,
                "made_progress": record.made_progress,
            },
        )
        return record

    def record_strategist_turn(self, record: StrategistRecord) -> StrategistRecord:
        self._append(record.to_dict())
        return record

    def link_chain(self, record: ChainLink) -> ChainLink:
        self._append(record.to_dict())
        return record

    def list_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def experiments(self) -> list[ExperimentRecord]:
        return [ExperimentRecord(**d) for d in self.list_all() if d.get("kind") == "experiment"]

    def strategist_turns(self) -> list[StrategistRecord]:
        return [StrategistRecord(**d) for d in self.list_all() if d.get("kind") == "strategist"]

    def chains(self) -> list[ChainLink]:
        return [ChainLink(**d) for d in self.list_all() if d.get("kind") == "chain"]

    def query_action_ratio(self) -> float | None:
        experiments = self.experiments()
        actions = sum(e.action_count for e in experiments)
        if actions == 0:
            return None
        return sum(e.query_count for e in experiments) / actions

    def wasted_experiments(self) -> list[ExperimentRecord]:
        return [e for e in self.experiments() if e.made_progress is False]
