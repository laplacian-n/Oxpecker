import { useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { projectionAt } from "../projection";
import { WorkerActivity } from "./WorkerActivity";

// The right rail (CLIENT_UI_DESIGN.md §6.5), reserved for the live work of the workers; torn off,
// it becomes a full window for one worker. It mounts the one WorkerActivity component.
//
// The time scrubber lives here: drag it to watch what a worker did earlier; let go and it returns
// to now. It is the same projection replayed to an earlier sequence number (§3) — a pure re-fold
// of the events the window already holds, not a second data path. Being in the past must be
// unmistakable (§6.5), so a scrubbed view gets a distinct frame and a persistent label, never a
// subtle one — a screen showing old data that looks live is the same lie as stale data after a
// disconnect.
export function WorkerRail({
  subscription,
  initialWorkerId,
}: {
  subscription: EngagementSubscription;
  initialWorkerId?: string;
}) {
  const live = useEngagement(subscription);
  const workerIds = Object.keys(live.workers);
  const [selected, setSelected] = useState<string | undefined>(initialWorkerId ?? workerIds[0]);
  // null = live ("now"); a number = scrubbed to that sequence number.
  const [scrubSeq, setScrubSeq] = useState<number | null>(null);

  const workerId = selected ?? workerIds[0];
  const inPast = scrubSeq !== null && scrubSeq < live.latestSeq;
  const projection = inPast ? projectionAt(live.events, scrubSeq!) : live;

  return (
    <div className="surface" data-surface="worker-rail">
      <div className="worker-picker">
        <label>
          worker{" "}
          <select
            data-testid="worker-picker"
            value={workerId ?? ""}
            onChange={(e) => setSelected(e.target.value)}
          >
            {workerIds.length === 0 && <option value="">no workers yet</option>}
            {workerIds.map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
          </select>
        </label>
      </div>

      {/* The distinct frame + persistent label while viewing the past. */}
      <div
        className={inPast ? "scrubbed-past" : "live"}
        data-testid="activity-frame"
        data-past={inPast}
        style={inPast ? { outline: "3px solid var(--past, #b5651d)", opacity: 0.95 } : undefined}
      >
        {inPast && (
          <div data-testid="past-label" role="status">
            ⟲ VIEWING THE PAST — sequence {scrubSeq} of {live.latestSeq}
          </div>
        )}
        {workerId ? (
          <WorkerActivity projection={projection} workerId={workerId} />
        ) : (
          <p>No worker selected.</p>
        )}
      </div>

      <div className="scrubber">
        <input
          type="range"
          aria-label="time scrubber"
          data-testid="scrubber"
          min={0}
          max={live.latestSeq}
          value={scrubSeq ?? live.latestSeq}
          onChange={(e) => setScrubSeq(Number(e.target.value))}
          // Let go and it returns to now (§6.5).
          onPointerUp={() => setScrubSeq(null)}
          onMouseUp={() => setScrubSeq(null)}
          onKeyUp={() => setScrubSeq(null)}
        />
      </div>
    </div>
  );
}
