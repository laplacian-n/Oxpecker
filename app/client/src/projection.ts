// The projection a window holds (CLIENT_UI_DESIGN.md §3): no window owns state — each one holds
// only a projection it can rebuild from the engagement event stream. This is the client-side fold
// that mirrors the server's `project()` (agent/engagement/event_log.py): a snapshot seeds it, and
// each streamed event is applied incrementally, so a window opened three hours apart from another
// — or one replayed to an earlier sequence number by the scrubber — computes identical content.

export interface EngagementEvent {
  seq: number;
  ts: number;
  kind: string;
  payload: Record<string, unknown>;
}

export interface Projection {
  // The last sequence number folded in, and how far behind the server the window is (§5.5). A
  // window that fell behind on a reconnect shows `missed > 0` until it catches up.
  atSeq: number;
  latestSeq: number;
  missed: number;
  // Kept in arrival order and keyed by `seq` when rendered, never by position (§2.1 rule 4), so an
  // event arriving mid-list never recreates the rows below it.
  events: EngagementEvent[];
  workers: Record<string, Record<string, any>>;
  graphNodes: Record<string, Record<string, any>>;
  // In-flight tool calls keyed by call id: a finished call is removed, so what remains is
  // "running now" — what the flow view and rail draw (mirrors the server fold).
  toolCalls: Record<string, Record<string, any>>;
  // Keyed by model id: the flow view's model roster (§6.3), accumulated from model_call events.
  modelRoster: Record<string, { model_id: string; role?: string; calls: number; prompt_tokens: number; completion_tokens: number; cost: number }>;
  // Where each worker's output went (§4.2 artifact_stored / §6.3.1): a list, not collapsed, so the
  // flow view can draw an edge per artifact and a node whose output goes nowhere stays visible.
  artifacts: { seq: number; worker?: string; artifact?: string; store?: string; ref?: string }[];
  counts: { note_added: number; finding_recorded: number };
  budget: Record<string, unknown>;
  // Approvals keyed by request id (§7: one request, one state, three renderings — flow node,
  // drawer, chat). Pending and resolved both live here; a surface filters by status.
  approvals: Record<string, Record<string, any>>;
  // The engagement's orchestration tier (§2.6.2), carried on the snapshot so the client offers
  // exactly the surfaces that tier has (§8). "high" until a snapshot says otherwise.
  tier: string;
}

export function emptyProjection(): Projection {
  return {
    atSeq: 0,
    latestSeq: 0,
    missed: 0,
    events: [],
    workers: {},
    graphNodes: {},
    toolCalls: {},
    modelRoster: {},
    artifacts: [],
    counts: { note_added: 0, finding_recorded: 0 },
    budget: {},
    approvals: {},
    tier: "high",
  };
}

// Seed from the server snapshot (GET /snapshot). The snapshot is the server's own projection, so
// the client adopts its derived slices rather than re-folding the whole history — then applies
// live events on top.
export function fromSnapshot(snapshot: Record<string, any>): Projection {
  const p = emptyProjection();
  p.atSeq = snapshot.at_seq ?? 0;
  p.latestSeq = snapshot.latest_seq ?? 0;
  for (const w of snapshot.workers ?? []) p.workers[String(w.worker_id)] = w;
  for (const n of snapshot.graph_nodes ?? []) p.graphNodes[String(n.node)] = n;
  for (const c of snapshot.tool_calls_in_flight ?? []) p.toolCalls[String(c.call_id)] = c;
  for (const m of snapshot.model_roster ?? []) p.modelRoster[String(m.model_id)] = { calls: 0, prompt_tokens: 0, completion_tokens: 0, cost: 0, ...m };
  p.artifacts = [...(snapshot.artifacts ?? [])];
  for (const a of snapshot.approvals ?? []) p.approvals[String(a.request_id)] = a;
  if (snapshot.counts) p.counts = { ...p.counts, ...snapshot.counts };
  if (snapshot.budget) p.budget = snapshot.budget;
  if (snapshot.tier) p.tier = String(snapshot.tier);
  return p;
}

// Re-fold the held events up to and including `atSeq` — the time scrubber (§6.5), which is "the
// same projection replayed to an earlier sequence number, not a second data path" (§3). A window
// holds its events, so dragging the scrubber back is a pure re-fold of what it already has; only
// ranges older than the in-memory window need a server snapshot. Pure, so a given `atSeq` always
// yields the same past.
export function projectionAt(events: EngagementEvent[], atSeq: number): Projection {
  let p = emptyProjection();
  for (const e of events) {
    if (e.seq > atSeq) break; // events arrive in order
    p = applyEvent(p, e);
  }
  return p;
}

// What one worker is doing, at whatever projection is passed (now, or a scrubbed-to past). The
// same selector feeds the rail, the tear-off window and the flow-node summary — "one component,
// three mounts" (§6.5); only the level of detail differs, not the data path.
export function workerView(projection: Projection, workerId: string) {
  return {
    worker: projection.workers[workerId] ?? { worker_id: workerId },
    inFlightTool: Object.values(projection.toolCalls).find((c) => String(c.worker) === String(workerId)) ?? null,
    toolHistory: projection.events.filter(
      (e) => e.kind === "tool_call_started" && String((e.payload as any).worker) === String(workerId),
    ),
    artifacts: projection.artifacts.filter((a) => String(a.worker) === String(workerId)),
  };
}

// Apply one streamed event. Pure in its inputs: returns a new Projection (never mutates) so React
// can compare references. Unknown kinds are still appended to `events` — a producer emitting a
// kind this fold has not learned yet still rides the stream and the counts, never silently
// vanishing, exactly as the server's projection keeps them on its raw timeline.
export function applyEvent(prev: Projection, event: EngagementEvent): Projection {
  const next: Projection = {
    ...prev,
    events: [...prev.events, event],
    atSeq: Math.max(prev.atSeq, event.seq),
    latestSeq: Math.max(prev.latestSeq, event.seq),
    missed: Math.max(prev.missed - 1, 0), // each replayed event closes one of the gap it reported
  };
  const p = event.payload as Record<string, any>;
  switch (event.kind) {
    case "worker_spawned":
      next.workers = { ...prev.workers, [String(p.worker_id)]: { ...p, status: "running" } };
      break;
    case "worker_finished":
      next.workers = {
        ...prev.workers,
        [String(p.worker_id)]: { ...(prev.workers[String(p.worker_id)] ?? {}), ...p, status: "finished" },
      };
      break;
    case "tool_call_started":
      next.toolCalls = { ...prev.toolCalls, [String(p.call_id)]: p };
      break;
    case "tool_call_finished": {
      // Drop the finished call from the in-flight set (what remains is "running now").
      const { [String(p.call_id)]: _done, ...rest } = prev.toolCalls;
      next.toolCalls = rest;
      break;
    }
    case "model_call": {
      const id = String(p.model_id);
      const m = prev.modelRoster[id] ?? { model_id: id, calls: 0, prompt_tokens: 0, completion_tokens: 0, cost: 0 };
      next.modelRoster = {
        ...prev.modelRoster,
        [id]: {
          ...m,
          role: p.role ?? m.role,
          calls: m.calls + 1,
          prompt_tokens: m.prompt_tokens + (Number(p.prompt_tokens) || 0),
          completion_tokens: m.completion_tokens + (Number(p.completion_tokens) || 0),
          cost: m.cost + (Number(p.cost) || 0),
        },
      };
      break;
    }
    case "artifact_stored":
      next.artifacts = [...prev.artifacts, { seq: event.seq, worker: p.worker, artifact: p.artifact, store: p.store, ref: p.ref }];
      break;
    case "approval_required":
      next.approvals = { ...prev.approvals, [String(p.request_id)]: { ...p, status: "pending" } };
      break;
    case "approval_resolved":
      next.approvals = {
        ...prev.approvals,
        [String(p.request_id)]: { ...(prev.approvals[String(p.request_id)] ?? { request_id: p.request_id }), status: p.status },
      };
      break;
    case "graph_node_changed":
      next.graphNodes = { ...prev.graphNodes, [String(p.node)]: p };
      break;
    case "note_added":
      next.counts = { ...prev.counts, note_added: prev.counts.note_added + 1 };
      break;
    case "finding_recorded":
      next.counts = { ...prev.counts, finding_recorded: prev.counts.finding_recorded + 1 };
      break;
    case "budget_updated":
      next.budget = p;
      break;
    default:
      break; // kept on `events`, not interpreted
  }
  return next;
}
