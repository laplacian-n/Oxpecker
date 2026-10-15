import { Projection, workerView } from "../projection";

// The worker-activity view, written once and mounted in three places (CLIENT_UI_DESIGN.md §6.5):
// the right rail, its own torn-off window, and — in summary — a flow node. Only the level of
// detail differs, so there is one implementation and nothing drifts. It renders whatever
// projection it is handed, which is how the time scrubber reuses it unchanged: a scrubbed-to past
// is just an earlier projection. It is a pure function of its props — no fetching here.
//
// It shows what the engagement stream carries about a worker: its state, the tool it is calling
// now, the tools it has called (newest first), and where its output went. The fuller detail §6.5
// also lists — full tool arguments/results and the worker's reasoning — is session/worker-level
// content the §4.2 event kinds do not carry; surfacing it needs the evidence store or a per-worker
// channel, which is the open data-path decision noted for the owner, not built here.

export type ActivityState = "running" | "idle" | "finished";

// The one state a worker is in, derived from what the stream says: finished once it has finished;
// running while a tool call of its is in flight; idle otherwise (spawned, between tool calls).
// The rail's rows use the same derivation, so the list and the detail never disagree.
export function activityState(status: unknown, hasInFlightTool: boolean): ActivityState {
  if (String(status) === "finished") return "finished";
  return hasInFlightTool ? "running" : "idle";
}

const STATE_COLOR: Record<ActivityState, string> = {
  running: "var(--accent)",
  idle: "var(--muted)",
  finished: "var(--text)",
};

export function WorkerActivity({ projection, workerId }: { projection: Projection; workerId: string }) {
  const { worker, inFlightTool, toolHistory, artifacts } = workerView(projection, workerId);
  const state = activityState(worker.status, inFlightTool !== null);

  // Tool calls grouped by tool name (with a count, ordered by most recent use), and the history
  // itself listed newest first. The stream is in arrival order, so reversing it gives newest first.
  const newestFirst = [...toolHistory].reverse();
  const groups = new Map<string, number>();
  for (const e of newestFirst) {
    const tool = String((e.payload as any).tool);
    groups.set(tool, (groups.get(tool) ?? 0) + 1);
  }

  return (
    <div className="worker-activity" data-testid="worker-activity" data-worker={workerId}>
      <h2>
        worker {workerId}{" "}
        <span
          data-testid="worker-state"
          data-state={state}
          style={{
            fontSize: 11,
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            color: STATE_COLOR[state],
            border: `1px solid ${STATE_COLOR[state]}`,
            borderRadius: 4,
            padding: "1px 6px",
            verticalAlign: "middle",
          }}
        >
          {state}
        </span>
      </h2>
      <dl>
        <dt>status</dt>
        <dd data-testid="worker-status">{String(worker.status ?? "unknown")}</dd>
        {worker.method && (
          <>
            <dt>method</dt>
            <dd>{String(worker.method)}</dd>
          </>
        )}
        {worker.hypothesis && (
          <>
            <dt>hypothesis</dt>
            <dd>{String(worker.hypothesis)}</dd>
          </>
        )}
        <dt>calling now</dt>
        <dd data-testid="worker-current-tool">{inFlightTool ? String(inFlightTool.tool) : "—"}</dd>
      </dl>

      <h3>tool calls</h3>
      {groups.size > 0 && (
        <p data-testid="worker-tool-groups" style={{ color: "var(--muted)", fontSize: 12, margin: "0 0 6px" }}>
          {[...groups].map(([tool, n]) => `${tool} ×${n}`).join(" · ")}
        </p>
      )}
      <ol data-testid="worker-tool-history" style={{ paddingLeft: 20 }}>
        {newestFirst.map((e) => {
          const digest = (e.payload as any).argument_digest;
          return (
            <li key={e.seq}>
              #{e.seq} {String((e.payload as any).tool)}
              {digest ? (
                <span data-testid="tool-digest" style={{ color: "var(--muted)" }}>
                  {" "}
                  · {String(digest)}
                </span>
              ) : null}
            </li>
          );
        })}
        {toolHistory.length === 0 && <li data-testid="worker-no-tools">no tool calls yet</li>}
      </ol>

      <h3>output</h3>
      <ul data-testid="worker-artifacts">
        {artifacts.map((a) => (
          <li key={a.seq}>
            {a.artifact ?? "artifact"} → {a.store ?? "evidence"}
          </li>
        ))}
        {artifacts.length === 0 && <li>nothing stored yet</li>}
      </ul>
    </div>
  );
}
