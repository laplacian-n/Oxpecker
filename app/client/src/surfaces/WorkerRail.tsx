import { useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { Projection, projectionAt, workerView } from "../projection";
import { WorkerActivity, activityState, ActivityState } from "./WorkerActivity";

// The right rail (CLIENT_UI_DESIGN.md §6.5), reserved for the live work of the workers; torn off,
// it becomes a full window for one worker. It lists every worker (running first, then finished)
// and mounts the one WorkerActivity component for the selected one.
//
// The time scrubber lives here: drag it to watch what a worker did earlier; let go and it returns
// to now. It is the same projection replayed to an earlier sequence number (§3) — a pure re-fold
// of the events the window already holds, not a second data path. Being in the past must be
// unmistakable (§6.5), so a scrubbed view gets a distinct frame and a persistent label, never a
// subtle one — a screen showing old data that looks live is the same lie as stale data after a
// disconnect.
//
// "Pop out" opens the same worker view in its own window through the app's existing
// `/:engagementId/worker/:workerId` route, so the torn-off window is a second mount of the same
// projection, not a new state path. The engagement id comes from the prop when given, else from
// the current hash route (the app is a HashRouter, so `#/:engagementId/...` is always there).
export function WorkerRail({
  subscription,
  initialWorkerId,
  engagementId: engagementIdProp,
}: {
  subscription: EngagementSubscription;
  initialWorkerId?: string;
  engagementId?: string;
}) {
  const live = useEngagement(subscription);
  // null = live ("now"); a number = scrubbed to that sequence number.
  const [scrubSeq, setScrubSeq] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | undefined>(initialWorkerId);

  const inPast = scrubSeq !== null && scrubSeq < live.latestSeq;
  const projection = inPast ? projectionAt(live.events, scrubSeq!) : live;

  // Every worker, running first then finished; within each group, arrival order (keyed by
  // worker_id, never by position, so a row is the same element as the stream grows).
  const rows = sortWorkers(projection);
  const workerId = selected ?? rows[0]?.id;
  const engagementId = engagementIdProp ?? engagementFromHash();

  return (
    <div className="surface" data-surface="worker-rail">
      <ul
        className="worker-list"
        data-testid="worker-picker"
        aria-label="workers"
        style={{ listStyle: "none", padding: 0, margin: "0 0 10px", display: "grid", gap: 4 }}
      >
        {rows.length === 0 && <li data-testid="worker-list-empty" style={{ color: "var(--muted)" }}>no workers yet</li>}
        {rows.map((r) => {
          const active = r.id === workerId;
          return (
            <li key={r.id}>
              <button
                type="button"
                data-testid="worker-row"
                data-worker={r.id}
                data-status={r.state}
                aria-pressed={active}
                onClick={() => setSelected(r.id)}
                style={{
                  width: "100%",
                  textAlign: "left",
                  background: active ? "var(--panel-2)" : "none",
                  border: `1px solid ${active ? "var(--accent)" : "transparent"}`,
                  borderRadius: 4,
                  color: r.state === "finished" ? "var(--muted)" : "var(--text)",
                  padding: "6px 8px",
                  cursor: "pointer",
                  font: "inherit",
                }}
              >
                <span style={{ fontWeight: 600 }}>{r.id}</span>{" "}
                <span style={{ fontSize: 11, color: "var(--muted)" }}>{r.state}</span>
                {r.label && <div style={{ fontSize: 12, color: "var(--muted)" }}>{r.label}</div>}
                {r.tool && (
                  <div data-testid="worker-row-tool" style={{ fontSize: 12 }}>
                    ▸ {r.tool}
                  </div>
                )}
              </button>
            </li>
          );
        })}
      </ul>

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
          <>
            {engagementId && (
              <button
                type="button"
                data-testid="worker-pop-out"
                onClick={() =>
                  window.open(
                    `${window.location.pathname}#/${engagementId}/worker/${workerId}`,
                    "_blank",
                    "width=720,height=800",
                  )
                }
                style={{
                  background: "none",
                  border: "1px solid var(--muted)",
                  borderRadius: 4,
                  color: "var(--text)",
                  padding: "2px 8px",
                  marginBottom: 8,
                  cursor: "pointer",
                  font: "inherit",
                  fontSize: 12,
                }}
              >
                ⧉ pop out
              </button>
            )}
            <WorkerActivity projection={projection} workerId={workerId} />
          </>
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

interface WorkerRow {
  id: string;
  state: ActivityState;
  label: string;
  tool: string | null;
}

// Rows for every worker in the projection shown: running (or idle) before finished, each group in
// arrival order. The state and the in-flight tool come from the same selector the detail uses.
function sortWorkers(projection: Projection): WorkerRow[] {
  const rows = Object.keys(projection.workers).map((id): WorkerRow => {
    const { worker, inFlightTool } = workerView(projection, id);
    const parts = [worker.method, worker.hypothesis].filter(Boolean).map(String);
    return {
      id,
      state: activityState(worker.status, inFlightTool !== null),
      label: parts.join(" · "),
      tool: inFlightTool ? String(inFlightTool.tool) : null,
    };
  });
  return [...rows.filter((r) => r.state !== "finished"), ...rows.filter((r) => r.state === "finished")];
}

// The engagement id from the current hash route, e.g. `#/eng1/worker/w1` -> "eng1".
function engagementFromHash(): string | undefined {
  const m = window.location.hash.match(/^#\/([^/]+)\//);
  return m ? decodeURIComponent(m[1]) : undefined;
}
