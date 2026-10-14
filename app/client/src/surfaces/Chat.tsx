import { useEffect, useRef, useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { api, ApiOptions } from "../api";
import { EngagementEvent } from "../projection";

// Chat — the primary, always-present surface (CLIENT_UI_DESIGN.md §6.1), and the control surface:
// from here the operator starts the autonomous run and stops it, and watches the engagement's live
// event feed. The feed and the goal input never share a value (§2.1 rule 3: the goal box is
// uncontrolled), so the engagement event flood — which re-renders this component as the feed grows
// — can never drop a character or move the caret while the operator types a goal.

// A run's lifecycle, read from the event stream (the one data path): the driver mirrors its phase
// and lifecycle events there, so "is a run going" is derived from events, not guessed. The last
// lifecycle marker wins.
const RUN_STARTED = new Set(["phase_entered", "wave_started", "worker_spawned", "phase_advanced"]);
const RUN_ENDED = new Set([
  "run_complete", "autonomous_done", "stopped_by_operator", "blocked",
  "wall_clock_exceeded", "task_error",
]);

function runIsActive(events: EngagementEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const k = events[i].kind;
    if (RUN_ENDED.has(k)) return false;
    if (RUN_STARTED.has(k)) return true;
  }
  return false;
}

export function Chat({
  subscription,
  engagementId = "",
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  engagementId?: string;
  apiOptions?: ApiOptions;
}) {
  const projection = useEngagement(subscription);
  const goalRef = useRef<HTMLTextAreaElement>(null);
  // Optimistic "busy" so the button reacts the instant it is clicked; the event stream then takes
  // over as the source of truth (a run that actually started keeps it true; an end event clears it).
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const streamSaysRunning = runIsActive(projection.events);
  const running = streamSaysRunning || busy;

  useEffect(() => {
    if (streamSaysRunning) setBusy(false); // the stream confirms a run — stop guessing
  }, [streamSaysRunning]);

  const start = async () => {
    const goal = goalRef.current?.value.trim() ?? "";
    setError("");
    setBusy(true);
    try {
      await api.startRun(engagementId, { mode: "autonomous", user_message: goal }, apiOptions);
    } catch (e: any) {
      setBusy(false);
      setError(String(e?.message ?? e));
    }
  };

  const stop = async () => {
    try {
      await api.stopRun(engagementId, apiOptions);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  return (
    <div className="surface" data-surface="chat">
      <header>
        <h1>Chat</h1>
        {running && <span className="run-badge" data-testid="run-active">● running</span>}
        {projection.missed > 0 && (
          <span role="status" data-testid="missed">
            disconnected — you missed {projection.missed} events
          </span>
        )}
      </header>

      {/* The control: start an autonomous run with a goal, or stop one in flight. §6.1 — chat is
          where the operator drives the engagement. */}
      <div className="run-control" data-testid="run-control">
        {running ? (
          <button onClick={stop} data-testid="stop-run">Stop run</button>
        ) : (
          <>
            <textarea
              ref={goalRef} data-testid="chat-input" rows={2} defaultValue=""
              placeholder="Goal for the run — e.g. find auth and injection flaws on the in-scope target"
            />
            <button onClick={start} data-testid="start-run">Start autonomous run</button>
          </>
        )}
      </div>
      {error && <p className="landing-error" data-testid="run-error">{error}</p>}

      {/* §7: approvals surface in chat too, because chat is the surface the operator is actually
          looking at. One request, one state — resolving here clears it everywhere. */}
      <ApprovalsPanel projection={projection} apiOptions={apiOptions} />

      {/* Keyed by identity (seq), never by position (§2.1 rule 4): an event arriving mid-list
          never recreates the rows below it. */}
      <ol data-testid="event-feed" className="chat-log">
        {projection.events.map((e) => (
          <li key={e.seq} data-kind={e.kind}>
            #{e.seq} {e.kind}
            {e.payload?.detail ? ` — ${e.payload.detail}` : ""}
          </li>
        ))}
      </ol>
      <div data-testid="event-count">{projection.events.length}</div>
    </div>
  );
}
