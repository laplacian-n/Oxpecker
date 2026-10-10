"""Read-only spend accounting view over a `SpendLedger` — the first half of the waste report
(AGENT_ARCHITECTURE.md step 7).

The question this answers is "where did the money go": how much is settled against real work,
how much is still held by in-flight reservations, and how much was reserved and then released
because the work never ran (abandoned spend). It is a pure read over the ledger's existing
`reservations` table. It never writes, and it never re-derives the committed total: that comes
from `SpendLedger._committed`, the same helper `reserve()` checks against, so the report and the
cap enforcement cannot disagree.

Amount semantics for `by_worker`: each reservation contributes what it currently commits to the
budget, exactly as `_committed` counts it. A settled reservation contributes its `actual` cost. A
still-open (reserved) one contributes its `amount` estimate, since that is the hold it occupies.
A released one contributes 0, because its hold has left the committed total. So the values in
`by_worker` sum to `committed`. The released estimate is reported separately as `abandoned`.
"""
from __future__ import annotations

from agent.engagement.spend import SpendLedger

UNATTRIBUTED = "unattributed"


def spend_report(ledger: SpendLedger) -> dict:
    """Return a read-only accounting view of the ledger. See the module docstring for semantics.

    Keys:
      cap              the budget cap, or None when unmetered
      committed        settled actuals plus open reservations (the number reserve() fits under)
      remaining        max(cap - committed, 0) when capped, else None (clamped like snapshot())
      by_worker        worker -> committed amount attributed to it; None worker keyed "unattributed"
      settled_count    reservations settled
      released_count   reservations released (abandoned, never ran)
      open_count       reservations still reserved (neither settled nor released)
      abandoned        sum of the estimates held by released reservations (the waste)
    """
    with ledger._connect() as conn:
        cap = conn.execute("SELECT cap FROM budget WHERE id = 1").fetchone()["cap"]
        committed = SpendLedger._committed(conn)

        by_worker: dict[str, float] = {}
        rows = conn.execute(
            "SELECT worker, "
            "       COALESCE(SUM(CASE WHEN status = 'settled' THEN actual "
            "                         WHEN status = 'reserved' THEN amount "
            "                         ELSE 0 END), 0) AS c "
            "FROM reservations GROUP BY worker"
        ).fetchall()
        for row in rows:
            key = UNATTRIBUTED if row["worker"] is None else row["worker"]
            by_worker[key] = by_worker.get(key, 0.0) + float(row["c"])

        counts = {"settled": 0, "released": 0, "reserved": 0}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM reservations GROUP BY status"
        ).fetchall():
            counts[row["status"]] = int(row["n"])

        abandoned = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) AS a FROM reservations WHERE status = 'released'"
            ).fetchone()["a"]
        )

    remaining = None if cap is None else max(cap - committed, 0.0)
    return {
        "cap": cap,
        "committed": committed,
        "remaining": remaining,
        "by_worker": by_worker,
        "settled_count": counts["settled"],
        "released_count": counts["released"],
        "open_count": counts["reserved"],
        "abandoned": abandoned,
    }
