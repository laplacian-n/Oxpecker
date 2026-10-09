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
  workers: Record<string, Record<string, unknown>>;
  graphNodes: Record<string, Record<string, unknown>>;
  counts: { note_added: number; finding_recorded: number };
  budget: Record<string, unknown>;
}

export function emptyProjection(): Projection {
  return {
    atSeq: 0,
    latestSeq: 0,
    missed: 0,
    events: [],
    workers: {},
    graphNodes: {},
    counts: { note_added: 0, finding_recorded: 0 },
    budget: {},
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
  if (snapshot.counts) p.counts = { ...p.counts, ...snapshot.counts };
  if (snapshot.budget) p.budget = snapshot.budget;
  return p;
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
