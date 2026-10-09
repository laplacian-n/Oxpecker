// The rail and its time scrubber (§6.5). The load-bearing assertion is that being in the past is
// unmistakable (a distinct frame and a persistent label) and that it is the SAME view replayed —
// scrubbing back to before a worker stored its output shows that worker with no output, and
// releasing returns to now. "Looks live but is old" is the lie §6.5 forbids, so the frame flag is
// asserted both ways.

import { act } from "react";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { WorkerRail } from "./WorkerRail";
import { EngagementSubscription } from "../subscription";

afterEach(cleanup);

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((ev: { data: string }) => void)[]> = {};
  onerror = null;
  seq = 0;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, cb: (ev: { data: string }) => void) {
    (this.listeners[type] ||= []).push(cb);
  }
  close() {}
  push(kind: string, payload: Record<string, unknown>) {
    this.seq += 1;
    for (const cb of this.listeners["message"] ?? []) cb({ data: JSON.stringify({ seq: this.seq, ts: 0, kind, payload }) });
  }
}

const emptySnap = { at_seq: 0, latest_seq: 0, workers: [], graph_nodes: [], counts: {}, budget: {} };

async function railWithHistory() {
  const sub = new EngagementSubscription({
    engagementId: "eng1",
    fetchImpl: (async () => ({ ok: true, status: 200, json: async () => emptySnap })) as unknown as typeof fetch,
    eventSourceFactory: (url: string) => new FakeEventSource(url),
  });
  await act(async () => {
    await sub.start();
  });
  const src = FakeEventSource.last!;
  const screen = render(<WorkerRail subscription={sub} initialWorkerId="w1" />);
  // seq1 spawn, seq2 tool call, seq3 artifact stored — a small history to scrub across.
  await act(async () => {
    src.push("worker_spawned", { worker_id: "w1", method: "sqli", hypothesis: "H-3" });
    src.push("tool_call_started", { call_id: "c1", worker: "w1", tool: "http_request" });
    src.push("artifact_stored", { worker: "w1", artifact: "resp.har", store: "evidence" });
  });
  return { screen, src };
}

test("at 'now' the frame is live and the worker's output is shown", async () => {
  const { screen } = await railWithHistory();
  await waitFor(() => screen.getByTestId("worker-activity"));
  expect(screen.getByTestId("activity-frame").getAttribute("data-past")).toBe("false");
  expect(screen.queryByTestId("past-label")).toBeNull();
  expect(screen.getByTestId("worker-artifacts").textContent).toContain("resp.har");
});

test("scrubbing back shows a distinct past frame with the worker pre-output, and release returns to now", async () => {
  const { screen } = await railWithHistory();
  await waitFor(() => screen.getByTestId("worker-activity"));
  const scrubber = screen.getByTestId("scrubber") as HTMLInputElement;

  // Drag back to sequence 2 — before the artifact was stored.
  fireEvent.change(scrubber, { target: { value: "2" } });
  expect(screen.getByTestId("activity-frame").getAttribute("data-past")).toBe("true");
  expect(screen.getByTestId("past-label").textContent).toContain("sequence 2");
  // The same view, replayed: the worker is mid-work with no output yet.
  expect(screen.getByTestId("worker-artifacts").textContent).not.toContain("resp.har");
  expect(screen.getByTestId("worker-current-tool").textContent).toContain("http_request");

  // Let go — back to now, output visible again, frame no longer flagged past.
  fireEvent.pointerUp(scrubber);
  await waitFor(() => expect(screen.getByTestId("activity-frame").getAttribute("data-past")).toBe("false"));
  expect(screen.getByTestId("worker-artifacts").textContent).toContain("resp.har");
});
