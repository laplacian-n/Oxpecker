// Subscription behaviour: fresh catch-up, resume from a Last-Event-ID with the missed-count, and
// the §4.3 version refusal. These exercise the mechanism §3 depends on — a window rebuilds its
// projection from a snapshot plus the stream — without a browser or a server.

import { expect, test } from "vitest";
import { EngagementSubscription } from "./subscription";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((ev: { data: string }) => void)[]> = {};
  onerror: ((ev: unknown) => void) | null = null;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, cb: (ev: { data: string }) => void) {
    (this.listeners[type] ||= []).push(cb);
  }
  close() {}
  emit(type: string, data: unknown) {
    for (const cb of this.listeners[type] ?? []) cb({ data: JSON.stringify(data) });
  }
}

function sub(snapshot: Record<string, unknown>, opts: { status?: number; lastEventId?: number } = {}) {
  return new EngagementSubscription({
    engagementId: "eng1",
    lastEventId: opts.lastEventId,
    fetchImpl: (async () => ({
      ok: (opts.status ?? 200) < 400,
      status: opts.status ?? 200,
      json: async () => snapshot,
    })) as unknown as typeof fetch,
    eventSourceFactory: (url: string) => new FakeEventSource(url),
  });
}

const emptySnap = { at_seq: 0, latest_seq: 0, workers: [], graph_nodes: [], counts: {}, budget: {} };

test("a fresh subscriber catches up in order and reports no missed events", async () => {
  const s = sub(emptySnap);
  await s.start();
  const src = FakeEventSource.last!;
  src.emit("control", { seq: 0, kind: "subscription_resumed", payload: { resumed_after: 0, latest_seq: 0, missed: 0 } });
  for (const seq of [1, 2, 3]) src.emit("message", { seq, ts: 0, kind: "note_added", payload: {} });

  const p = s.getProjection();
  expect(p.events.map((e) => e.seq)).toEqual([1, 2, 3]);
  expect(p.latestSeq).toBe(3);
  expect(p.missed).toBe(0);
  expect(p.counts.note_added).toBe(3);
});

test("a resume replays only past the cursor and the missed-count closes as events arrive", async () => {
  // Disconnected at 2, server is now at 5: the snapshot reflects state through 2, the control
  // event says 3 were missed, and the three replayed events bring the window back to live.
  const s = sub({ ...emptySnap, at_seq: 2, latest_seq: 2 }, { lastEventId: 2 });
  await s.start();
  const src = FakeEventSource.last!;
  expect(src.url).toContain("last_event_id=2");
  expect(src.url).toContain("api_version=1.0.0");

  src.emit("control", { seq: 0, kind: "subscription_resumed", payload: { resumed_after: 2, latest_seq: 5, missed: 3 } });
  expect(s.getProjection().missed).toBe(3);

  src.emit("message", { seq: 3, ts: 0, kind: "note_added", payload: {} });
  src.emit("message", { seq: 4, ts: 0, kind: "note_added", payload: {} });
  expect(s.getProjection().missed).toBe(1); // two of the three gap events have now arrived
  src.emit("message", { seq: 5, ts: 0, kind: "note_added", payload: {} });

  const p = s.getProjection();
  expect(p.events.map((e) => e.seq)).toEqual([3, 4, 5]);
  expect(p.latestSeq).toBe(5);
  expect(p.missed).toBe(0);
});

test("a server that cannot serve this API version is refused, not half-run", async () => {
  const s = sub(emptySnap, { status: 409 });
  await expect(s.start()).rejects.toThrow(/will not run against a server/);
});
