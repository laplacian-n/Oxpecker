// The flow view is a wiring diagram, not a timeline (CLIENT_UI_DESIGN.md §6.3): the diagram is
// fixed — strategist, worker pool, broker, hypothesis graph, evidence store, notebook, findings —
// and what moves is the traffic on the wires and the worker instances inside the pool. This maps a
// projection to React Flow nodes/edges as a PURE function, so the mapping (which worker nodes
// exist, which tool each is calling now, where each artifact's output went) is unit-tested without
// rendering React Flow — which needs a real viewport jsdom does not provide. The component below
// only wires this output into <ReactFlow>.
//
// §6.3.1, deliberately preserved: an edge is drawn per stored artifact, so a worker whose output
// goes nowhere is visible at a glance — the thing §6.3.1 says not to simplify away.

import type { Projection } from "../projection";

export interface FlowNode {
  id: string;
  position: { x: number; y: number };
  data: { label: string; kind: string; detail?: string };
}
export interface FlowEdge {
  id: string;
  source: string;
  target: string;
  label?: string;
  animated?: boolean;
}

// The fixed architecture. Positions are fixed because the architecture does not move; only its
// traffic does.
const FIXED: FlowNode[] = [
  { id: "strategist", position: { x: 300, y: 0 }, data: { label: "Strategist", kind: "role" } },
  { id: "pool", position: { x: 300, y: 120 }, data: { label: "Worker pool", kind: "pool" } },
  { id: "broker", position: { x: 300, y: 380 }, data: { label: "Broker", kind: "broker" } },
  { id: "evidence", position: { x: 80, y: 500 }, data: { label: "Evidence store", kind: "store" } },
  { id: "notebook", position: { x: 300, y: 500 }, data: { label: "Notebook", kind: "store" } },
  { id: "findings", position: { x: 520, y: 500 }, data: { label: "Findings", kind: "store" } },
  { id: "graph", position: { x: 560, y: 120 }, data: { label: "Hypothesis graph", kind: "graph" } },
];

const STATIC_EDGES: FlowEdge[] = [
  { id: "e-strat-pool", source: "strategist", target: "pool" },
  { id: "e-pool-broker", source: "pool", target: "broker" },
  { id: "e-broker-evidence", source: "broker", target: "evidence" },
  { id: "e-strat-graph", source: "strategist", target: "graph" },
];

// store name on an artifact_stored event -> the fixed node it lands in.
const STORE_NODE: Record<string, string> = {
  evidence: "evidence",
  notebook: "notebook",
  findings: "findings",
};

export function buildGraph(projection: Projection): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const nodes: FlowNode[] = [...FIXED];
  const edges: FlowEdge[] = [...STATIC_EDGES];

  // A live instance per worker, laid out in a row beneath the pool. Each shows the tool it is
  // calling right now (from the in-flight set) and the hypothesis it is working on.
  const workers = Object.values(projection.workers).filter((w) => w.status !== "finished");
  workers.forEach((w, i) => {
    const wid = `worker-${w.worker_id}`;
    const currentTool = Object.values(projection.toolCalls).find((c) => String(c.worker) === String(w.worker_id));
    nodes.push({
      id: wid,
      position: { x: 160 + i * 150, y: 240 },
      data: {
        label: `worker ${w.worker_id}`,
        kind: "worker",
        detail: [w.method, currentTool ? `→ ${currentTool.tool}` : null, w.hypothesis ? `H:${w.hypothesis}` : null]
          .filter(Boolean)
          .join("  "),
      },
    });
    // Traffic: the strategist dispatched it; its tool calls go through the broker.
    edges.push({ id: `e-strat-${wid}`, source: "strategist", target: wid });
    edges.push({ id: `e-${wid}-broker`, source: wid, target: "broker", animated: !!currentTool });
  });

  // An edge per artifact, from the producing worker (or the pool, if the worker is gone) to the
  // store it landed in — so where every output went is literal, not decorative.
  projection.artifacts.forEach((a, i) => {
    const target = STORE_NODE[String(a.store)] ?? "evidence";
    const sourceNode = a.worker && projection.workers[String(a.worker)] ? `worker-${a.worker}` : "pool";
    edges.push({
      id: `e-artifact-${a.seq}-${i}`,
      source: sourceNode,
      target,
      label: a.artifact ? String(a.artifact) : "artifact",
    });
  });

  return { nodes, edges };
}
