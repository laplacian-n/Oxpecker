// The Work surface: tabs, the live glow, and the frozen-layout-during-drag rule (§6.2). The glow
// is proven live (a node a running worker is on carries data-glowing), and the freeze is proven
// both directions — a tree change that arrives mid-drag does NOT re-lay-out the tree, and lands
// only on release.

import { act } from "react";
import { render, cleanup, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Work } from "./Work";
import { EngagementSubscription } from "../subscription";
import { emptyProjection, Projection } from "../projection";

// A test can hand Work a hand-built projection instead of the streamed one. Unset, the real
// subscription projection is used, so every other test is unaffected.
const override = vi.hoisted(() => ({ projection: null as Projection | null }));
vi.mock("../useEngagement", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../useEngagement")>();
  return {
    ...actual,
    useEngagement: (sub: EngagementSubscription) => {
      const streamed = actual.useEngagement(sub);
      return override.projection ?? streamed;
    },
  };
});

afterEach(() => {
  cleanup();
  override.projection = null;
});
beforeEach(() => {
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((ev: { data: string }) => void)[]> = {};
  onerror: ((ev: unknown) => void) | null = null;
  seq = 0;
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
  push(kind: string, payload: Record<string, unknown>) {
    this.seq += 1;
    this.emit("message", { seq: this.seq, ts: 0, kind, payload });
  }
}

const emptySnap = { at_seq: 0, latest_seq: 0, workers: [], graph_nodes: [], counts: {}, budget: {} };

// An api fetch whose graph grows by one node each time it is called, so a refetch is observable.
function makeApiFetch(state: { graphCalls: number; nodes: () => any[] }) {
  return (async (url: string) => {
    const u = String(url);
    if (u.includes("/hypothesis-graph")) {
      state.graphCalls += 1;
      return { ok: true, status: 200, json: async () => ({ exists: true, nodes: state.nodes(), edges: [] }) };
    }
    if (u.includes("/notebook")) {
      return { ok: true, status: 200, json: async () => ({ exists: true, notes: [{ ordinal: 1, category: "todo", note: "check /admin" }] }) };
    }
    if (u.includes("/findings")) {
      return { ok: true, status: 200, json: async () => ({ findings: [{ finding_id: "f1", title: "IDOR" }], count: 1 }) };
    }
    return { ok: true, status: 200, json: async () => emptySnap };
  }) as unknown as typeof fetch;
}

async function startedSub() {
  const sub = new EngagementSubscription({
    engagementId: "eng1",
    fetchImpl: (async () => ({ ok: true, status: 200, json: async () => emptySnap })) as unknown as typeof fetch,
    eventSourceFactory: (url: string) => new FakeEventSource(url),
  });
  await act(async () => {
    await sub.start();
  });
  return sub;
}

test("the tabs switch between tree, notebook and findings", async () => {
  const sub = await startedSub();
  const state = { graphCalls: 0, nodes: () => [{ ordinal: 3, title: "SQLi on /login", status: "testing" }] };
  const screen = render(<Work subscription={sub} engagementId="eng1" apiOptions={{ fetchImpl: makeApiFetch(state) }} />);

  await waitFor(() => within(screen.getByTestId("tree")).getByText(/SQLi on \/login/));

  fireEvent.click(screen.getByTestId("tab-notebook"));
  await waitFor(() => within(screen.getByTestId("notebook")).getByText(/check \/admin/));

  fireEvent.click(screen.getByTestId("tab-findings"));
  await waitFor(() => within(screen.getByTestId("findings")).getByText(/IDOR/));
});

test("a node a running worker is on glows, live from the stream", async () => {
  const sub = await startedSub();
  const source = FakeEventSource.last!;
  const state = { graphCalls: 0, nodes: () => [{ ordinal: 3, title: "SQLi", status: "testing" }] };
  const screen = render(<Work subscription={sub} engagementId="eng1" apiOptions={{ fetchImpl: makeApiFetch(state) }} />);
  await waitFor(() => screen.getByText(/SQLi/));

  // Node 3 is not glowing until a worker picks it up.
  expect(screen.getByText(/SQLi/).closest("li")!.getAttribute("data-glowing")).toBe("false");
  await act(async () => {
    source.push("worker_spawned", { worker_id: "w1", hypothesis: "3" });
  });
  expect(screen.getByText(/SQLi/).closest("li")!.getAttribute("data-glowing")).toBe("true");
});

test("a running worker's node glows on the tree; a finished worker's node and an idle node do not, and the glow survives a tab switch", async () => {
  const projection = emptyProjection();
  projection.workers = {
    w1: { worker_id: "w1", status: "running", hypothesis: "3" },
    w2: { worker_id: "w2", status: "finished", hypothesis: "5" },
  };
  projection.graphNodes = { "3": { node: "3", status: "testing" }, "5": { node: "5", status: "open" } };
  override.projection = projection;

  const sub = await startedSub();
  const state = {
    graphCalls: 0,
    nodes: () => [
      { ordinal: 3, title: "SQLi on /login", status: "testing" },
      { ordinal: 5, title: "XSS on /search", status: "open" },
      { ordinal: 7, title: "IDOR on /api", status: "open" },
    ],
  };
  const screen = render(<Work subscription={sub} engagementId="eng1" apiOptions={{ fetchImpl: makeApiFetch(state) }} />);
  await waitFor(() => screen.getByText(/SQLi on/));

  const glowOf = (text: RegExp) => screen.getByText(text).closest("li")!.getAttribute("data-glowing");
  expect(glowOf(/SQLi on/)).toBe("true");
  expect(glowOf(/XSS on/)).toBe("false");
  expect(glowOf(/IDOR on/)).toBe("false");

  // Switching away and back remounts the tree; the glow is derived from the projection, so it returns.
  fireEvent.click(screen.getByTestId("tab-notebook"));
  await waitFor(() => screen.getByTestId("notebook"));
  fireEvent.click(screen.getByTestId("tab-tree"));
  await waitFor(() => screen.getByText(/SQLi on/));
  expect(glowOf(/SQLi on/)).toBe("true");
});

test("the tree layout is frozen while dragging: a change queues and lands on release", async () => {
  const sub = await startedSub();
  const source = FakeEventSource.last!;
  let nodes = [{ ordinal: 1, title: "root", status: "open" }];
  const state = { graphCalls: 0, nodes: () => nodes };
  const screen = render(<Work subscription={sub} engagementId="eng1" apiOptions={{ fetchImpl: makeApiFetch(state) }} />);
  await waitFor(() => screen.getByText(/root/));
  const callsAfterLoad = state.graphCalls;

  const tree = screen.getByTestId("tree");
  fireEvent.pointerDown(tree); // pointer goes down — layout freezes

  // The server grows the graph and emits the change mid-drag.
  nodes = [...nodes, { ordinal: 2, title: "child", status: "open" }];
  await act(async () => {
    source.push("graph_node_changed", { node: "2", status: "open" });
  });

  // Frozen: the tree did NOT re-lay-out (no refetch), and a marker says one is waiting.
  expect(state.graphCalls).toBe(callsAfterLoad);
  expect(screen.getByTestId("pending-marker").textContent).toContain("1 new waiting");
  expect(screen.queryByText(/child/)).toBeNull();

  // Release — the queued change lands.
  fireEvent.pointerUp(tree);
  await waitFor(() => screen.getByText(/child/));
  expect(state.graphCalls).toBe(callsAfterLoad + 1);
  expect(screen.queryByTestId("pending-marker")).toBeNull();
});

test("the remembered tab is restored from localStorage", async () => {
  localStorage.setItem("oxpecker.work.tab", "findings");
  const sub = await startedSub();
  const state = { graphCalls: 0, nodes: () => [] };
  const screen = render(<Work subscription={sub} engagementId="eng1" apiOptions={{ fetchImpl: makeApiFetch(state) }} />);
  await waitFor(() => screen.getByTestId("findings"));
  expect(screen.getByTestId("tab-findings").getAttribute("aria-selected")).toBe("true");
});
