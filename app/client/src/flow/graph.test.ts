// buildGraph is the flow view's data→diagram mapping (CLIENT_UI_DESIGN.md §6.3). It is a pure
// function precisely so these behaviours can be pinned without rendering React Flow (jsdom has no
// viewport). The one §6.3.1 insists on — an edge per artifact, so output-that-goes-nowhere is
// visible — is asserted directly.

import { expect, test } from "vitest";
import { buildGraph, effectivePricePerMtok } from "./graph";
import { emptyProjection, applyEvent, Projection } from "../projection";

function withEvents(...events: { kind: string; payload: Record<string, unknown> }[]): Projection {
  let p = emptyProjection();
  events.forEach((e, i) => (p = applyEvent(p, { seq: i + 1, ts: 0, kind: e.kind, payload: e.payload })));
  return p;
}

test("the fixed architecture is always present", () => {
  const { nodes } = buildGraph(emptyProjection());
  const ids = nodes.map((n) => n.id);
  for (const fixed of ["strategist", "pool", "broker", "evidence", "notebook", "findings", "graph"]) {
    expect(ids).toContain(fixed);
  }
});

test("a live worker gets an instance node showing the tool it is calling now", () => {
  const p = withEvents(
    { kind: "worker_spawned", payload: { worker_id: "w1", hypothesis: "H-3", method: "sqli" } },
    { kind: "tool_call_started", payload: { call_id: "c1", worker: "w1", tool: "http_request" } },
  );
  const { nodes, edges } = buildGraph(p);
  const node = nodes.find((n) => n.id === "worker-w1");
  expect(node).toBeDefined();
  expect(node!.data.detail).toContain("http_request");
  expect(node!.data.detail).toContain("H:H-3");
  // Its broker wire animates while a tool call is in flight.
  expect(edges.find((e) => e.id === "e-worker-w1-broker")!.animated).toBe(true);
});

test("a finished worker drops out of the diagram", () => {
  const p = withEvents(
    { kind: "worker_spawned", payload: { worker_id: "w1" } },
    { kind: "worker_finished", payload: { worker_id: "w1", outcome: "done" } },
  );
  expect(buildGraph(p).nodes.find((n) => n.id === "worker-w1")).toBeUndefined();
});

test("every stored artifact draws an edge to the store it landed in (§6.3.1)", () => {
  const p = withEvents(
    { kind: "worker_spawned", payload: { worker_id: "w1" } },
    { kind: "artifact_stored", payload: { worker: "w1", artifact: "response.har", store: "evidence" } },
    { kind: "artifact_stored", payload: { worker: "w1", artifact: "note-7", store: "notebook" } },
  );
  const { edges } = buildGraph(p);
  const artifactEdges = edges.filter((e) => e.id.startsWith("e-artifact-"));
  expect(artifactEdges).toHaveLength(2);
  expect(artifactEdges.map((e) => e.target).sort()).toEqual(["evidence", "notebook"]);
  expect(artifactEdges.map((e) => e.label)).toContain("response.har");
  // Sourced from the producing worker, so a node whose output goes nowhere is the one with no
  // outgoing artifact edge — the §6.3.1 signal.
  expect(artifactEdges.every((e) => e.source === "worker-w1")).toBe(true);
});

// The model economics block (§6.3.1): models come from projection.modelRoster and nothing else.
function withRoster(roster: Projection["modelRoster"], workers: { worker_id: string; status?: string }[] = []): Projection {
  return { ...emptyProjection(), modelRoster: roster, workers: Object.fromEntries(workers.map((w) => [w.worker_id, w])) };
}

test("a model in the roster produces a model node carrying role and spend", () => {
  const p = withRoster({
    "gpt-x": { model_id: "gpt-x", role: "strategist", calls: 3, prompt_tokens: 800_000, completion_tokens: 200_000, cost: 0.5 },
  });
  const { nodes, edges } = buildGraph(p);
  const node = nodes.find((n) => n.id === "model-gpt-x");
  expect(node).toBeDefined();
  expect(node!.data.kind).toBe("model");
  expect(node!.data.label).toBe("gpt-x");
  expect(node!.data.detail).toContain("strategist");
  expect(node!.data.detail).toContain("spent $0.5000");
  // Placed in the left column, and wired into the strategist.
  expect(node!.position.x).toBe(-220);
  expect(edges.find((e) => e.id === "e-model-gpt-x-strat")).toMatchObject({ source: "model-gpt-x", target: "strategist" });
});

test("effective price is cost over million tokens, and shows a dash at zero tokens", () => {
  // 0.5 spent over 1,000,000 tokens (800k prompt + 200k completion): $0.5 per Mtok.
  expect(
    effectivePricePerMtok({ cost: 0.5, prompt_tokens: 800_000, completion_tokens: 200_000 }),
  ).toBe(0.5);
  // Rounded to 4 decimals: 0.001 over 3 tokens = 333.3333... per Mtok.
  expect(effectivePricePerMtok({ cost: 0.001, prompt_tokens: 2, completion_tokens: 1 })).toBe(333.3333);
  expect(effectivePricePerMtok({ cost: 0, prompt_tokens: 0, completion_tokens: 0 })).toBeNull();

  const withTokens = buildGraph(
    withRoster({ m: { model_id: "m", calls: 1, prompt_tokens: 800_000, completion_tokens: 200_000, cost: 0.5 } }),
  ).nodes.find((n) => n.id === "model-m")!;
  expect(withTokens.data.detail).toContain("$0.5000/Mtok");

  const noTokens = buildGraph(
    withRoster({ m: { model_id: "m", calls: 0, prompt_tokens: 0, completion_tokens: 0, cost: 0 } }),
  ).nodes.find((n) => n.id === "model-m")!;
  expect(noTokens.data.detail).toContain("—/Mtok");
  expect(noTokens.data.detail).not.toContain("$NaN");
});

test("model wires animate only while a worker is live", () => {
  const roster = { m: { model_id: "m", calls: 1, prompt_tokens: 1, completion_tokens: 1, cost: 0 } };
  const idle = buildGraph(withRoster(roster, [{ worker_id: "w1", status: "finished" }]));
  expect(idle.edges.find((e) => e.id === "e-model-m-strat")!.animated).toBe(false);
  const live = buildGraph(withRoster(roster, [{ worker_id: "w1", status: "running" }]));
  expect(live.edges.find((e) => e.id === "e-model-m-strat")!.animated).toBe(true);
});
