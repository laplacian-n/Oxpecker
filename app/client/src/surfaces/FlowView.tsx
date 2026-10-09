import { useMemo } from "react";
import { ReactFlow, Background, Controls, Panel } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { buildGraph } from "../flow/graph";

// Flow — the architecture in motion (CLIENT_UI_DESIGN.md §6.3), built on React Flow (the design's
// §5 choice: pan/zoom/hit-testing/viewport culling, and crucially the viewport transform stays out
// of React state, which is what keeps a drag smooth while events stream). The diagram is fixed;
// the worker instances and the traffic on the wires are live, mapped from the projection by the
// pure buildGraph(). Read-only except for stop-worker / approve (§6.3) — those write paths are
// backend actions not yet wired, so they are shown, not yet actionable.
export function FlowView({ subscription }: { subscription: EngagementSubscription }) {
  const projection = useEngagement(subscription);
  const { nodes, edges } = useMemo(() => buildGraph(projection), [projection]);

  const rfNodes = nodes.map((n) => ({
    id: n.id,
    position: n.position,
    data: { label: n.data.detail ? `${n.data.label}\n${n.data.detail}` : n.data.label },
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
        <ReactFlow nodes={rfNodes} edges={rfEdges} fitView proOptions={{ hideAttribution: true }}>
          <Background />
          <Controls />
          {/* The model roster (§6.3): every model in use, the role it fills, and what it has spent
              so far. Price per million tokens comes from the OpenRouter probe (agent/llm/
              openrouter.py), a source this engagement stream does not carry — so the roster shows
              usage and spend from the stream and leaves price to that separate source. */}
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
