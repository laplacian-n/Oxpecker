"""Per-engagement spend ledger with atomic reservation — AGENT_ARCHITECTURE.md §14.1 D and §7.2.

§14.1 D: "Parallel spend has the same race the cooldown had. With N workers each checking 'is
there budget left' before spending, all N can pass and collectively overshoot. This is the
identical shape to the cooldown race already fixed in the broker, and the fix is the same:
reserve atomically, do not check and then spend."

§7.2: "Budget exhaustion stops, it does not degrade." So a reservation that would cross the cap
is refused (`BudgetExhausted`) rather than clamped — the worker stops, it does not quietly run a
cheaper model or a shorter prompt.

This is the third appearance of the one bug shape this project keeps naming: anywhere a shared
value is read and then written, the read and the write must be one operation. The event log's
sequence number (`event_log.py`) solved it for a counter; this solves it for a budget. `reserve()`
reads the committed total and writes the new reservation **inside a single `BEGIN IMMEDIATE`
transaction**, which SQLite grants to exactly one writer at a time — so two workers reserving at
once queue, the second sees the first's reservation, and they cannot both pass a check the budget
could only satisfy for one. `test_spend.py` proves the naive check-then-spend really overshoots
under threads before trusting that.

A spend has two moments because an estimate is not an actual: `reserve(estimate)` holds budget
before the model call, `settle(reservation_id, actual)` records what it truly cost afterwards
(and `release()` returns a reservation whose work never ran). "Remaining" is the cap minus what
is settled minus what is still reserved, so an in-flight wave cannot be double-committed by a
late settle.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class BudgetExhausted(RuntimeError):
    """A reservation was refused because it would cross the cap. Carries the numbers so the
    caller (and the audit trail) can say by how much, rather than only that it stopped."""

    def __init__(self, requested: float, remaining: float, cap: float):
        self.requested = requested
        self.remaining = remaining
        self.cap = cap
        super().__init__(
            f"spend budget exhausted: requested {requested:.6f} but only {remaining:.6f} of the "
            f"{cap:.6f} cap remains. Nothing was reserved — stop, do not degrade (§7.2)."
        )


class SpendLedger:
    """One per engagement dir; the ledger lives at `<engagement_dir>/spend.db`, colocated with the
    engagement's other state. A cap of None means unmetered (no budget was set) — distinct from a
    cap of 0, which permits nothing."""

    def __init__(self, engagement_dir: Path):
        engagement_dir.mkdir(parents=True, exist_ok=True)
        self.engagement_dir = engagement_dir
        self.db_path = engagement_dir / "spend.db"
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS budget ("
                "  id  INTEGER PRIMARY KEY CHECK (id = 1),"
                "  cap REAL"  # NULL = unmetered
                ")"
            )
            conn.execute("INSERT OR IGNORE INTO budget (id, cap) VALUES (1, NULL)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reservations ("
                "  reservation_id TEXT PRIMARY KEY,"
                "  amount         REAL NOT NULL,"       # the estimate held
                "  actual         REAL,"                # the settled cost, NULL while outstanding
                "  status         TEXT NOT NULL,"       # reserved | settled | released
                "  worker         TEXT,"
                "  created_at     REAL NOT NULL,"
                "  settled_at     REAL"
                ")"
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def set_budget(self, cap: float | None) -> None:
        """Set (or clear, with None) the engagement's spend cap. Lowering it below what is already
        committed is allowed and simply means the next reservation is refused — it never claws back
        spend that already happened."""
        if cap is not None and cap < 0:
            raise ValueError("a spend cap cannot be negative")
        with self._connect() as conn:
            conn.execute("UPDATE budget SET cap = ? WHERE id = 1", (cap,))

    @staticmethod
    def _committed(conn: sqlite3.Connection) -> float:
        """Budget already spoken for: settled actuals plus still-outstanding reservations. The
        one number a new reservation must fit under."""
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN actual "
            "                         WHEN status = 'reserved' THEN amount "
            "                         ELSE 0 END), 0) AS c FROM reservations"
        ).fetchone()
        return float(row["c"])

    def reserve(self, amount: float, *, worker: str | None = None) -> str:
        """Hold `amount` against the budget and return a reservation id, or raise
        `BudgetExhausted` if it would cross the cap. The read of the committed total and the write
        of the reservation are one `BEGIN IMMEDIATE` transaction, so N concurrent reservers cannot
        all pass a check the budget could satisfy for only some of them (§14.1 D)."""
        if amount < 0:
            raise ValueError("cannot reserve a negative amount")
        reservation_id = f"r_{uuid.uuid4().hex[:16]}"
        now = time.time()
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")  # claim the single writer slot before reading the total
            cap_row = conn.execute("SELECT cap FROM budget WHERE id = 1").fetchone()
            cap = cap_row["cap"]
            if cap is not None:
                committed = self._committed(conn)
                remaining = cap - committed
                if amount > remaining:
                    conn.rollback()
                    raise BudgetExhausted(amount, remaining, cap)
            conn.execute(
                "INSERT INTO reservations (reservation_id, amount, status, worker, created_at) "
                "VALUES (?, ?, 'reserved', ?, ?)",
                (reservation_id, amount, worker, now),
            )
            conn.commit()
        finally:
            conn.close()
        return reservation_id

    def settle(self, reservation_id: str, actual: float) -> None:
        """Record what a reserved call actually cost. `actual` may differ from the estimate — it
        replaces the reservation's hold in the committed total — but a settle never itself fails
        on the cap: the spend already happened, and hiding it would make the ledger lie. A later
        reservation absorbs any overrun by having less room."""
        if actual < 0:
            raise ValueError("a settled cost cannot be negative")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE reservations SET actual = ?, status = 'settled', settled_at = ? "
                "WHERE reservation_id = ? AND status = 'reserved'",
                (actual, time.time(), reservation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no outstanding reservation {reservation_id!r} to settle")

    def release(self, reservation_id: str) -> None:
        """Return a reservation whose work never ran (a worker that reserved then was stopped). Its
        hold leaves the committed total; it is recorded as released, not deleted, so the trail
        shows it existed."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE reservations SET actual = 0, status = 'released', settled_at = ? "
                "WHERE reservation_id = ? AND status = 'reserved'",
                (time.time(), reservation_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no outstanding reservation {reservation_id!r} to release")

    def snapshot(self) -> dict:
        """The `budget_updated` read model (CLIENT_UI_DESIGN.md §4.2): settled spend, still-held
        reservations, and what remains. `remaining` is None when unmetered, and never below zero
        when a settle overran the cap — a negative number there would read as "owed", which the
        budget has no notion of."""
        with self._connect() as conn:
            cap_row = conn.execute("SELECT cap FROM budget WHERE id = 1").fetchone()
            cap = cap_row["cap"]
            spent_row = conn.execute(
                "SELECT COALESCE(SUM(actual), 0) AS s FROM reservations WHERE status = 'settled'"
            ).fetchone()
            reserved_row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS r FROM reservations WHERE status = 'reserved'"
            ).fetchone()
            spent = float(spent_row["s"])
            reserved = float(reserved_row["r"])
        remaining = None if cap is None else max(cap - spent - reserved, 0.0)
        return {"cap": cap, "spent": spent, "reserved": reserved, "remaining": remaining}
