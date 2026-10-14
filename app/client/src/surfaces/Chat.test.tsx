// Chat as the control surface (§6.1): it starts an autonomous run with a goal, reflects "running"
// from the event stream (not a guess), and stops a run in flight. Pinned because this is what makes
// the new client able to drive an engagement, not only watch one.
import { act } from "react";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Chat } from "./Chat";
import { EngagementSubscription } from "../subscription";
import { applyEvent } from "../projection";
import { api } from "../api";

afterEach(cleanup);
beforeEach(() => { try { localStorage.clear(); } catch { /* ignore */ } });

// A subscription with no network: start() is a no-op, and we drive its projection directly.
function fakeSubscription(): EngagementSubscription {
  const sub = new EngagementSubscription({
    engagementId: "eng-x",
    fetchImpl: (async () => ({ ok: true, json: async () => ({ at_seq: 0, latest_seq: 0 }) })) as any,
    eventSourceFactory: () => ({ addEventListener() {}, close() {}, onerror: null }),
  });
  return sub;
}

let _seq = 0;
function feed(sub: EngagementSubscription, kinds: string[]) {
  // push synthetic events through the real projection fold so runIsActive sees them, with a
  // monotonic seq so event keys stay unique across calls
  act(() => {
    kinds.forEach((kind) => ((sub as any).projection = applyEvent(
      (sub as any).projection, { seq: ++_seq, ts: 0, kind, payload: {} })));
    (sub as any).emit();
  });
}

test("Start sends the goal to the engagement's start endpoint", async () => {
  const spy = vi.spyOn(api, "startRun").mockResolvedValue({} as any);
  const sub = fakeSubscription();
  const { getByTestId } = render(<Chat subscription={sub} engagementId="eng-x" />);
  fireEvent.change(getByTestId("chat-input"), { target: { value: "find auth flaws" } });
  fireEvent.click(getByTestId("start-run"));
  await waitFor(() =>
    expect(spy).toHaveBeenCalledWith("eng-x", { mode: "autonomous", user_message: "find auth flaws" }, {}));
  spy.mockRestore();
});

test("a run_started event flips the control to a Stop button", async () => {
  const sub = fakeSubscription();
  const { getByTestId, queryByTestId } = render(<Chat subscription={sub} engagementId="eng-x" />);
  expect(getByTestId("start-run")).toBeInTheDocument();
  feed(sub, ["phase_entered"]);
  await waitFor(() => getByTestId("stop-run"));
  expect(queryByTestId("start-run")).toBeNull();
  expect(getByTestId("run-active")).toBeInTheDocument();
});

test("an end event flips it back to Start", async () => {
  const sub = fakeSubscription();
  const { getByTestId } = render(<Chat subscription={sub} engagementId="eng-x" />);
  feed(sub, ["phase_entered"]);
  await waitFor(() => getByTestId("stop-run"));
  feed(sub, ["run_complete"]);
  await waitFor(() => getByTestId("start-run"));
});

test("Stop calls the engagement's stop endpoint", async () => {
  const spy = vi.spyOn(api, "stopRun").mockResolvedValue({} as any);
  const sub = fakeSubscription();
  const { getByTestId } = render(<Chat subscription={sub} engagementId="eng-x" />);
  feed(sub, ["worker_spawned"]);
  await waitFor(() => getByTestId("stop-run"));
  fireEvent.click(getByTestId("stop-run"));
  await waitFor(() => expect(spy).toHaveBeenCalledWith("eng-x", {}));
  spy.mockRestore();
});
