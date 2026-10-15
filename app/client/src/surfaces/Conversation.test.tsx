// The conversation surface. The fold is tested in conversation.test.ts; these are the things only
// a rendered component can get wrong — and each was run first against a broken version: a
// controlled textarea (test 4), a composer that clears before the message is shown (test 2), a
// send that leaves the optimistic state behind on failure (test 3), the live tail rendered as a
// transcript row (test 5).

import { afterEach, expect, test, vi } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Conversation } from "./Conversation";
import { ConversationSubscription } from "../conversationStream";
import { api } from "../api";

class FakeEventSource {
  static last: FakeEventSource | null = null;
  listeners: ((ev: { data: string }) => void)[] = [];
  onerror = null;
  constructor(public url: string) { FakeEventSource.last = this; }
  addEventListener(_t: string, cb: (ev: { data: string }) => void) { this.listeners.push(cb); }
  close() {}
  /** Deliver a frame. Wrapped in act() because the fold it drives sets React state — unwrapped,
   *  every test that streams would pass while printing an act() warning, which trains the next
   *  reader to ignore those warnings. */
  push(event: unknown) {
    act(() => { for (const cb of this.listeners) cb({ data: JSON.stringify(event) }); });
  }
}

const SNAPSHOT = { session_id: "s1", messages: [], running: false, stage: "RECON" };

async function mount(snapshot: object = SNAPSHOT) {
  const sub = new ConversationSubscription({
    sessionId: "s1",
    fetchImpl: (async () => ({ ok: true, status: 200, json: async () => snapshot })) as unknown as typeof fetch,
    eventSourceFactory: (url) => new FakeEventSource(url),
  });
  await sub.start();
  const view = render(<Conversation subscription={sub} sessionId="s1" />);
  return { sub, view, src: FakeEventSource.last! };
}

afterEach(() => vi.restoreAllMocks());

test("1. the snapshot's history renders, because the stream never replays it", async () => {
  await mount({
    ...SNAPSHOT,
    messages: [{ role: "user", content: "scan it" }, { role: "assistant", content: "port 22 open" }],
  });
  expect(screen.getByTestId("msg-0")).toHaveTextContent("scan it");
  expect(screen.getByTestId("msg-1")).toHaveTextContent("port 22 open");
  expect(screen.getByTestId("conv-stage")).toHaveTextContent("RECON");
});

test("2. sending shows the message at once and clears the box", async () => {
  // The POST plus the first token is seconds. A composer that cleared itself and displayed
  // nothing reads as a dropped message, and the operator sends again into the server's 409.
  const spy = vi.spyOn(api, "sendMessage").mockResolvedValue({ status: "started" });
  await mount();
  const box = screen.getByTestId("message-input") as HTMLTextAreaElement;
  await userEvent.type(box, "scan the host");
  await userEvent.click(screen.getByTestId("send-message"));
  await waitFor(() => expect(spy).toHaveBeenCalledWith("s1", "scan the host", {}));
  expect(screen.getByTestId("msg-0")).toHaveTextContent("scan the host");
  expect(box.value).toBe("");
});

test("3. a refused send says why and takes the turn out of 'running'", async () => {
  // Without markSendFailed the surface sits in the running state forever on a 409 — the composer
  // reads "busy" against a turn that never started.
  vi.spyOn(api, "sendMessage").mockRejectedValue(new Error("a turn is already running in this session"));
  const { sub } = await mount();
  await userEvent.type(screen.getByTestId("message-input"), "again");
  await userEvent.click(screen.getByTestId("send-message"));
  await waitFor(() =>
    expect(screen.getByTestId("send-error")).toHaveTextContent("a turn is already running"));
  expect(sub.getConversation().running).toBe(false);
});

test("4. the operator can keep typing through a flood of deltas", async () => {
  // §2.1 rule 3. A controlled textarea re-rendered on every token drops characters and resets the
  // caret, so the composer is unmanaged and read on send. This test fails on a controlled one.
  vi.spyOn(api, "sendMessage").mockResolvedValue({ status: "started" });
  const { src } = await mount();
  const box = screen.getByTestId("message-input") as HTMLTextAreaElement;
  await userEvent.type(box, "half a thought");
  for (let i = 0; i < 40; i++) src.push({ type: "assistant_delta", text: `tok${i} ` });
  await waitFor(() => expect(screen.getByTestId("live-text")).toHaveTextContent("tok39"));
  expect(box.value).toBe("half a thought");
  await userEvent.type(box, " finished");
  expect(box.value).toBe("half a thought finished");
});

test("5. the live tail is not a transcript row, and the record replaces it", async () => {
  const { src } = await mount();
  src.push({ type: "assistant_delta", text: "port 22 " });
  src.push({ type: "assistant_delta", text: "is open" });
  await waitFor(() => expect(screen.getByTestId("live-text")).toHaveTextContent("port 22 is open"));
  expect(screen.getByTestId("transcript").children).toHaveLength(0);

  src.push({ type: "message", record: { role: "assistant", content: "port 22 is open" } });
  await waitFor(() => expect(screen.queryByTestId("live-tail")).toBeNull());
  // Exactly one copy — a tail appended INTO the list would show two.
  expect(screen.getByTestId("transcript").children).toHaveLength(1);
  expect(screen.getByTestId("msg-0")).toHaveTextContent("port 22 is open");
});

test("6. reasoning streams in its own block, open while it arrives", async () => {
  const { src } = await mount();
  src.push({ type: "reasoning_delta", text: "scope first" });
  await waitFor(() => expect(screen.getByTestId("live-reasoning")).toHaveTextContent("scope first"));
  expect(screen.queryByTestId("live-text")).toBeNull();
});

test("7. Enter sends, Shift+Enter does not, and an IME confirmation never does", async () => {
  const spy = vi.spyOn(api, "sendMessage").mockResolvedValue({ status: "started" });
  await mount();
  const box = screen.getByTestId("message-input") as HTMLTextAreaElement;

  box.value = "ไทย";
  // Confirming a candidate in an IME is an Enter press with composition in progress. Sending on
  // it ships a half-composed word — the kind of bug only a non-Latin keyboard ever hits.
  fireEvent.keyDown(box, { key: "Enter", isComposing: true });
  expect(spy).not.toHaveBeenCalled();

  fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
  expect(spy).not.toHaveBeenCalled();

  fireEvent.keyDown(box, { key: "Enter" });
  await waitFor(() => expect(spy).toHaveBeenCalledWith("s1", "ไทย", {}));
});

test("8. an empty or whitespace-only message is not sent", async () => {
  const spy = vi.spyOn(api, "sendMessage").mockResolvedValue({ status: "started" });
  await mount();
  await userEvent.type(screen.getByTestId("message-input"), "   ");
  await userEvent.click(screen.getByTestId("send-message"));
  expect(spy).not.toHaveBeenCalled();
});

test("9. infrastructure failures surface as notices, not as turns", async () => {
  const { src } = await mount();
  src.push({ type: "audit_error", error: "permission denied" });
  await waitFor(() => expect(screen.getByTestId("conv-notices")).toHaveTextContent("permission denied"));
  expect(screen.getByTestId("transcript").children).toHaveLength(0);
});

test("10. the context chip waits for a window the server actually reported", async () => {
  // seedConversation sets max 0 from last_prompt_tokens alone. Drawing a usage figure then would
  // measure against a denominator nobody sent.
  const { src } = await mount({ ...SNAPSHOT, last_prompt_tokens: 900 });
  expect(screen.queryByTestId("conv-context")).toBeNull();
  src.push({ type: "context", prompt_tokens: 1000, completion_tokens: 20, max: 32768 });
  await waitFor(() => expect(screen.getByTestId("conv-context")).toHaveTextContent("1000 / 32768"));
});
