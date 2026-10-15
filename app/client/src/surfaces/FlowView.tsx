import { useMemo } from "react";
import { ReactFlow, Background, Controls, Panel, Handle, Position, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { buildGraph } from "../flow/graph";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { ApiOptions } from "../api";

// A model node (§6.3.1 model economics): a small card, the model id on top and its role, price and
// spend in a muted monospace line below. Every other node keeps React Flow's default shape, so this
// is the one kind that is not a bare label. Colors come from the index.css tokens, not new hexes.
function ModelNode({ data }: NodeProps) {
  const d = data as { label: string; detail?: string };
  return (
    <div
      style={{
        background: "var(--panel)",
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: "6px 10px",
        minWidth: 180,
        color: "var(--text)",
      }}
    >
      <div style={{ fontWeight: 600, fontSize: 13 }}>{d.label}</div>
      {d.detail && (
        <div
          style={{
            marginTop: 2,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
            fontSize: 11,
            color: "var(--muted)",
          }}
        >
          {d.detail}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: "var(--accent)", border: "none" }} />
    </div>
  );
}

// Defined at module level so React Flow sees a stable nodeTypes object across renders.
const NODE_TYPES = { model: ModelNode };

// Flow — the architecture in motion (CLIENT_UI_DESIGN.md §6.3), built on React Flow (the design's
// §5 choice: pan/zoom/hit-testing/viewport culling, and crucially the viewport transform stays out
// of React state, which is what keeps a drag smooth while events stream). The diagram is fixed;
// the worker instances and the traffic on the wires are live, mapped from the projection by the
// pure buildGraph(). Read-only except for stop-worker / approve (§6.3) — those write paths are
// backend actions not yet wired, so they are shown, not yet actionable.
export function FlowView({
  subscription,
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  apiOptions?: ApiOptions;
}) {
  const projection = useEngagement(subscription);
  void apiOptions; // the flow rendering is the count only (§7); resolving happens in chat/drawer
  const { nodes, edges } = useMemo(() => buildGraph(projection), [projection]);

  const rfNodes = nodes.map((n) => ({
    id: n.id,
    position: n.position,
    // Model nodes get their own card (NODE_TYPES) so the label and the detail line stay separate.
    type: n.data.kind === "model" ? "model" : undefined,
    data:
      n.data.kind === "model"
        ? { label: n.data.label, detail: n.data.detail }
        : { label: n.data.detail ? `${n.data.label}\n${n.data.detail}` : n.data.label },
    // worker/store/role/etc. carried as a class for styling; the diagram's shape is fixed.
    className: `flow-${n.data.kind}`,
  }));
  const rfEdges = edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label,
    animated: e.animated,
  }));

  const roster = Object.values(projection.modelRoster);

  return (
    <div className="surface" data-surface="flow" style={{ width: "100%", height: "100%" }}>
      <div style={{ width: "100%", height: "70vh" }}>
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={NODE_TYPES}
          fitView
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls />
          {/* The model roster (§6.3): every model in use, the role it fills, and what it has spent
              so far. The model nodes in the diagram carry the same figures plus the realized price
              per million tokens (effectivePricePerMtok in flow/graph.ts); the list price is not in
              this stream, so that rate is blended, not quoted. */}
          {/* §7 flow-node rendering: the count of what is blocked, which is what decides how
              urgent an approval is — one worker waiting vs the wave stalled. */}
          <Panel position="top-left">
            <ApprovalsPanel projection={projection} compact />
          </Panel>
          <Panel position="top-right">
            <div data-testid="model-roster" className="roster">
              <h2>Models</h2>
              {roster.length === 0 && <div data-testid="roster-empty">no model calls yet</div>}
              <ul>
                {roster.map((m) => (
                  <li key={m.model_id} data-model={m.model_id}>
                    <strong>{m.model_id}</strong>
                    {m.role ? ` · ${m.role}` : ""} · {m.calls} calls · {m.prompt_tokens + m.completion_tokens} tok ·
                    ${m.cost.toFixed(4)}
                  </li>
                ))}
              </ul>
            </div>
          </Panel>
        </ReactFlow>
      </div>
    </div>
  );
}
