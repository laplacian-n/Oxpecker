import { expect, test } from "vitest";
import { glowingHypotheses } from "./glow";
import { emptyProjection, applyEvent, Projection } from "../projection";

function withEvents(...events: { kind: string; payload: Record<string, unknown> }[]): Projection {
  let p = emptyProjection();
  events.forEach((e, i) => (p = applyEvent(p, { seq: i + 1, ts: 0, kind: e.kind, payload: e.payload })));
  return p;
}

test("a running worker's hypothesis glows; a finished worker's does not", () => {
  const p = withEvents(
    { kind: "worker_spawned", payload: { worker_id: "w1", hypothesis: "H-3" } },
    { kind: "worker_spawned", payload: { worker_id: "w2", hypothesis: "H-7" } },
    { kind: "worker_finished", payload: { worker_id: "w2", outcome: "done" } },
  );
  const glow = glowingHypotheses(p);
  expect(glow.has("H-3")).toBe(true); // still running
  expect(glow.has("H-7")).toBe(false); // finished — marking stops
});

test("no workers means nothing glows", () => {
  expect(glowingHypotheses(emptyProjection()).size).toBe(0);
});
