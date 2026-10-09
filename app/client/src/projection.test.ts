// The scrubber's foundation (§6.5): projectionAt is a pure re-fold to an earlier sequence number,
// and workerView slices one worker out of whatever projection it is given — so "now" and "the
// past" go through the identical code, which is what keeps the scrubber from being a second data
// path.

import { expect, test } from "vitest";
import { applyEvent, emptyProjection, projectionAt, workerView, EngagementEvent } from "./projection";

function feed(kinds: { kind: string; payload: Record<string, unknown> }[]): EngagementEvent[] {
  return kinds.map((k, i) => ({ seq: i + 1, ts: 0, kind: k.kind, payload: k.payload }));
}

test("projectionAt re-folds exactly the prefix up to the sequence number", () => {
  const events = feed([
    { kind: "worker_spawned", payload: { worker_id: "w1" } },
    { kind: "note_added", payload: {} },
    { kind: "note_added", payload: {} },
    { kind: "finding_recorded", payload: {} },
  ]);
  // Scrubbed to seq 2: only the worker and the first note exist.
  const at2 = projectionAt(events, 2);
  expect(at2.atSeq).toBe(2);
  expect(at2.counts.note_added).toBe(1);
  expect(at2.counts.finding_recorded).toBe(0);
  expect(at2.events.map((e) => e.seq)).toEqual([1, 2]);

  // And it equals folding the prefix by hand — same code path, no second truth.
  let byHand = emptyProjection();
  for (const e of events.filter((e) => e.seq <= 2)) byHand = applyEvent(byHand, e);
  expect(at2).toEqual(byHand);
});

test("workerView slices one worker's tools and output from whatever projection it is handed", () => {
  const events = feed([
    { kind: "worker_spawned", payload: { worker_id: "w1", method: "sqli", hypothesis: "H-3" } },
    { kind: "tool_call_started", payload: { call_id: "c1", worker: "w1", tool: "http_request", argument_digest: "GET /x" } },
    { kind: "tool_call_started", payload: { call_id: "c2", worker: "w2", tool: "nmap" } },
    { kind: "artifact_stored", payload: { worker: "w1", artifact: "resp.har", store: "evidence" } },
  ]);
  let p = emptyProjection();
  for (const e of events) p = applyEvent(p, e);

  const v = workerView(p, "w1");
  expect(v.worker.method).toBe("sqli");
  expect(v.inFlightTool!.tool).toBe("http_request"); // w1's call, not w2's
  expect(v.toolHistory.map((e) => (e.payload as any).tool)).toEqual(["http_request"]);
  expect(v.artifacts).toHaveLength(1);

  // Replayed to before the artifact, the same selector shows the worker mid-work with no output
  // yet — the scrubber's exact use.
  const past = workerView(projectionAt(events, 2), "w1");
  expect(past.artifacts).toHaveLength(0);
  expect(past.inFlightTool!.tool).toBe("http_request");
});
