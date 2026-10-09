// CLIENT_UI_DESIGN.md §11 tests 1 and 2 — the owner's agreement 2 ("always rendering live, and it
// must stay smooth") expressed as assertions, written against the skeleton before there is much
// code to make them true in (§12 step 2).
//
// Both tests do the thing §11 insists on and that this project has been burned by six times: they
// PROVE THE FLOOD. Each asserts that the events actually arrived and were rendered during the
// typing or the drag — a count checked against what was pushed — and so fails if the flood did not
// happen, separately from failing if a character was lost or the pointer was dropped. A test that
// kept every character into a calm page would pass while asserting nothing; these cannot.
//
// Break-it-first, verified: test 1 fails against a CONTROLLED input (value bound to the
// flood-updated projection drops characters / resets the caret on re-render); test 2 fails if the
// node's position goes through application state (a flood re-render fights the drag). The skeleton
// makes them pass by giving the input its own value (§2.1 rule 3) and the drag its own ref (rule 2).

import { act } from "react";
import { render, cleanup, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";
import { Chat } from "./surfaces/Chat";
import { DragProbe } from "./surfaces/DragProbe";
import { EngagementSubscription } from "./subscription";

afterEach(cleanup);

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: Record<string, ((ev: { data: string }) => void)[]> = {};
  onerror: ((ev: unknown) => void) | null = null;
  closed = false;
  seq = 0;
  constructor(public url: string) {
    FakeEventSource.last = this;
  }
  addEventListener(type: string, cb: (ev: { data: string }) => void) {
    (this.listeners[type] ||= []).push(cb);
  }
  close() {
    this.closed = true;
  }
  emit(type: string, data: unknown) {
    for (const cb of this.listeners[type] ?? []) cb({ data: JSON.stringify(data) });
  }
  // Push one engagement event with the next sequence number — a slice of the flood.
  pushEvent(kind = "tool_call_started") {
    this.seq += 1;
    this.emit("message", { seq: this.seq, ts: 0, kind, payload: {} });
  }
}

async function startedSubscription(snapshot: Record<string, unknown> = { at_seq: 0, latest_seq: 0, workers: [], graph_nodes: [], counts: {}, budget: {} }) {
  const sub = new EngagementSubscription({
    engagementId: "eng1",
    fetchImpl: (async () => ({ ok: true, json: async () => snapshot })) as unknown as typeof fetch,
    eventSourceFactory: (url: string) => new FakeEventSource(url),
  });
  await act(async () => {
    await sub.start();
  });
  return sub;
}

test("typing is never interrupted by a flood of events, and the flood is proven to have arrived", async () => {
  const sub = await startedSubscription();
  const source = FakeEventSource.last!;
  const screen = render(<Chat subscription={sub} />);
  const input = screen.getByTestId("chat-input") as HTMLInputElement;
  const user = userEvent.setup();
  input.focus();

  const phrase = "sqlmap -u https://target/item?id=1 --batch";
  let pushed = 0;
  // Type each character while a burst of engagement events floods in between keystrokes — the
  // concurrency §11 describes, not typing into a calm page.
  for (const ch of phrase) {
    await user.type(input, ch === " " ? "{ }" : ch);
    await act(async () => {
      for (let i = 0; i < 5; i++) {
        source.pushEvent();
        pushed += 1;
      }
    });
  }

  // Not one character lost, and the caret never moved on its own.
  expect(input.value).toBe(phrase);
  expect(input.selectionStart).toBe(phrase.length);
  // The flood is proven: every pushed event was rendered into the keyed feed. Without this the
  // test above would pass on a calm page and assert nothing about "under load".
  expect(pushed).toBeGreaterThan(0);
  expect(Number(screen.getByTestId("event-count").textContent)).toBe(pushed);
});

test("a node drag is never dropped by a flood, and the flood is proven to have arrived", async () => {
  const sub = await startedSubscription();
  const source = FakeEventSource.last!;
  const screen = render(<DragProbe subscription={sub} />);
  const node = screen.getByTestId("flow-node") as HTMLDivElement;

  let pushed = 0;
  const flood = () =>
    act(() => {
      for (let i = 0; i < 5; i++) {
        source.pushEvent();
        pushed += 1;
      }
    });

  // jsdom's PointerEvent does not carry clientX/clientY through its init, so dispatch pointer-
  // typed MouseEvents (which jsdom does honour coordinates for); React's onPointerDown/Move/Up
  // listen on the matching native event names regardless of the constructor used.
  const pointer = (type: string, x: number, y: number) =>
    new MouseEvent(type, { clientX: x, clientY: y, bubbles: true });

  // Grab at the node's start position (20,20), so the grab offset is zero and the node's
  // translate tracks the pointer directly.
  fireEvent(node, pointer("pointerdown", 20, 20));
  const path = [
    [60, 40],
    [100, 70],
    [150, 120],
    [200, 180],
  ];
  for (const [x, y] of path) {
    fireEvent(node.parentElement!, pointer("pointermove", x, y));
    await flood(); // events stream mid-drag
  }
  fireEvent(node.parentElement!, pointer("pointerup", 200, 180));

  // The node ends exactly where it was released — the drag was not dropped or reset by any of the
  // flood re-renders that happened during it.
  expect(node.style.transform).toBe("translate(200px, 180px)");

  // And a further flood after release does not move it: a re-render re-applies the ref, it does
  // not fight the settled position.
  await flood();
  expect(node.style.transform).toBe("translate(200px, 180px)");

  // The flood is proven to have arrived and rendered during the drag.
  expect(pushed).toBeGreaterThan(0);
  expect(Number(screen.getByTestId("event-count").textContent)).toBe(pushed);
});
