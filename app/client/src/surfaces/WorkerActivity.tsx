import { Projection, workerView } from "../projection";

// The worker-activity view, written once and mounted in three places (CLIENT_UI_DESIGN.md §6.5):
// the right rail, its own torn-off window, and — in summary — a flow node. Only the level of
// detail differs, so there is one implementation and nothing drifts. It renders whatever
// projection it is handed, which is how the time scrubber reuses it unchanged: a scrubbed-to past
// is just an earlier projection.
//
// It shows what the engagement stream carries about a worker: its state, the tool it is calling
// now, the tools it has called, and where its output went. The fuller detail §6.5 also lists —
// full tool arguments/results and the worker's reasoning — is session/worker-level content the
// §4.2 event kinds do not carry; surfacing it needs the evidence store or a per-worker channel,
// which is the open data-path decision noted for the owner, not built here.
export function WorkerActivity({ projection, workerId }: { projection: Projection; workerId: string }) {
  const { worker, inFlightTool, toolHistory, artifacts } = workerView(projection, workerId);
  return (
    <div className="worker-activity" data-testid="worker-activity" data-worker={workerId}>
      <h2>worker {workerId}</h2>
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
      <ol data-testid="worker-tool-history">
        {toolHistory.map((e) => (
          <li key={e.seq}>
            #{e.seq} {String((e.payload as any).tool)}
            {(e.payload as any).argument_digest ? ` · ${(e.payload as any).argument_digest}` : ""}
          </li>
        ))}
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
