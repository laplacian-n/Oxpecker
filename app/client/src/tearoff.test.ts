// §11 test 3 — tear-off parity: the same surface, embedded and in its own window, at the same
// sequence number, shows the same content. This is what keeps §3 ("no window owns state") true as
// the code grows: because every surface is a pure function of the projection, and the projection
// is a pure fold of the stream, two windows that have seen the same events — or are scrubbed to
// the same sequence number — hold byte-identical projections. A window that quietly accumulated
// its own private state would break this.

import { expect, test } from "vitest";
import { EngagementSubscription } from "./subscription";
import { projectionAt } from "./projection";

class FakeEventSource {
  static sources: FakeEventSource[] = [];
  listeners: Record<string, ((ev: { data: string }) => void)[]> = {};
  onerror = null;
  constructor(public url: string) {
    FakeEventSource.sources.push(this);
  }
  addEventListener(type: string, cb: (ev: { data: string }) => void) {
    (this.listeners[type] ||= []).push(cb);
  }
  close() {}
  push(seq: number, kind: string, payload: Record<string, unknown>) {
    for (const cb of this.listeners["message"] ?? []) cb({ data: JSON.stringify({ seq, ts: 0, kind, payload }) });
  }
}

const snap = { at_seq: 0, latest_seq: 0, workers: [], graph_nodes: [], counts: {}, budget: {}, tier: "high" };

async function openWindow() {
  const sub = new EngagementSubscription({
    engagementId: "eng1",
    fetchImpl: (async () => ({ ok: true, status: 200, json: async () => snap })) as unknown as typeof fetch,
    eventSourceFactory: (url: string) => new FakeEventSource(url),
  });
  await sub.start();
  return { sub, src: FakeEventSource.sources[FakeEventSource.sources.length - 1] };
}

const HISTORY: [number, string, Record<string, unknown>][] = [
  [1, "worker_spawned", { worker_id: "w1", hypothesis: "H-3", method: "sqli" }],
  [2, "tool_call_started", { call_id: "c1", worker: "w1", tool: "http_request" }],
  [3, "model_call", { model_id: "qwen", role: "worker", prompt_tokens: 10, completion_tokens: 4, cost: 0.01 }],
  [4, "artifact_stored", { worker: "w1", artifact: "resp.har", store: "evidence" }],
  [5, "finding_recorded", { ordinal: 1 }],
];

test("two windows that saw the same stream hold identical projections", async () => {
  const a = await openWindow();
  const b = await openWindow();
  for (const [seq, kind, payload] of HISTORY) {
    a.src.push(seq, kind, payload);
    b.src.push(seq, kind, payload);
  }
  expect(a.sub.getProjection()).toEqual(b.sub.getProjection());
});

test("a window opened late catches identical content once it has replayed the same events", async () => {
  // The embedded surface saw events live; the torn-off one opens later and replays. At the same
  // sequence number they match — the §3 guarantee the time scrubber and resume both lean on.
  const live = await openWindow();
  for (const [seq, kind, payload] of HISTORY) live.src.push(seq, kind, payload);

  const late = await openWindow();
  for (const [seq, kind, payload] of HISTORY) late.src.push(seq, kind, payload);

  // Same "now", and same arbitrary earlier sequence number via the scrubber's pure re-fold.
  expect(late.sub.getProjection()).toEqual(live.sub.getProjection());
  expect(projectionAt(late.sub.getProjection().events, 3)).toEqual(
    projectionAt(live.sub.getProjection().events, 3),
  );
});
