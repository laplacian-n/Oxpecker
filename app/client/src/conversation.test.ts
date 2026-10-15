// The conversation fold, tested against the semantics that are NOT guessable from the event names
// — which is the whole reason this fold is a function instead of logic inside a render path.
//
// Every test here was first run against a deliberately wrong fold. Replacing `+` with `=` in the
// delta cases fails tests 1-2; dropping the tail-clear on `message` fails 3; dropping the
// empty-content backfill fails 4; removing the user-echo guards fails 5-6; letting
// `injection_flagged` through unconditionally fails 9; routing taint/audit errors into the thread
// fails 10. A fold that passed all of these while doing the wrong thing would have to be right.

import { describe, expect, test } from "vitest";
import {
  emptyConversation, seedConversation, reduceConversation, HANDLED_EVENTS,
  Conversation, SessionEvent,
} from "./conversation";

const fold = (events: SessionEvent[], from: Conversation = emptyConversation()): Conversation =>
  events.reduce(reduceConversation, from);

describe("streaming deltas", () => {
  test("1. assistant deltas ACCUMULATE — dev_server pushes the chunk, not the total", () => {
    // dev_server.py: `collected_text += buf; session.push({"type": "assistant_delta", "text": buf})`.
    // A fold that assigns shows only the last token of every answer, which looks like a truncation
    // bug in the model rather than in the client.
    const c = fold([
      { type: "assistant_delta", text: "Port " },
      { type: "assistant_delta", text: "22 " },
      { type: "assistant_delta", text: "is open." },
    ]);
    expect(c.streamingText).toBe("Port 22 is open.");
  });

  test("2. reasoning deltas accumulate separately from the answer", () => {
    const c = fold([
      { type: "reasoning_delta", text: "Checking " },
      { type: "assistant_delta", text: "Answer" },
      { type: "reasoning_delta", text: "scope first." },
    ]);
    expect(c.streamingReasoning).toBe("Checking scope first.");
    expect(c.streamingText).toBe("Answer");
  });

  test("3. the finished record REPLACES the live tail — the answer is not rendered twice", () => {
    // Session.append() pushes a `message` for every record it stores, so the full answer arrives
    // right after the deltas that spelled it out. The tail is separate state precisely so this
    // cannot double up.
    const c = fold([
      { type: "assistant_delta", text: "Port 22 is open." },
      { type: "message", record: { role: "assistant", content: "Port 22 is open." } },
    ]);
    expect(c.messages).toEqual([{ role: "assistant", content: "Port 22 is open." }]);
    expect(c.streamingText).toBe("");
    expect(c.streamingReasoning).toBe("");
  });

  test("4. a record with no content keeps what the operator watched arrive", () => {
    // A turn that ends in tool calls stores an assistant record whose content is empty. Dropping
    // the tail there would erase prose the operator just read off the screen.
    const c = fold([
      { type: "assistant_delta", text: "Let me scan that." },
      { type: "message", record: { role: "assistant", content: "", tool_name: "port_discovery" } },
    ]);
    expect(c.messages[0].content).toBe("Let me scan that.");
    expect(c.messages[0].tool_name).toBe("port_discovery");
  });
});

describe("the user's own message echoing back", () => {
  test("5. the window that sent it does not double it", () => {
    const sent: Conversation = {
      ...emptyConversation(),
      messages: [{ role: "user", content: "scan the host" }],
      running: true,
    };
    const c = reduceConversation(sent, { type: "message", record: { role: "user", content: "scan the host" } });
    expect(c.messages).toHaveLength(1);
  });

  test("5b. a TORN-OFF window does see it — dedup is the only correct guard", () => {
    // The other window never added it optimistically. The classic UI drops the echo whenever a
    // turn is running, which is fine for one window and would leave this one rendering an answer
    // to a question it never showed. Gating on `running` instead of on an actual duplicate is
    // what this pins: with that guard restored, the message below disappears.
    const watcher: Conversation = {
      ...emptyConversation(),
      messages: [{ role: "assistant", content: "earlier answer" }],
      running: true,
    };
    const c = reduceConversation(watcher, { type: "message", record: { role: "user", content: "scan the host" } });
    expect(c.messages).toHaveLength(2);
    expect(c.messages[1]).toEqual({ role: "user", content: "scan the host" });
  });

  test("6. deduped against an identical last message, but a different one is kept", () => {
    const base: Conversation = {
      ...emptyConversation(),
      messages: [{ role: "user", content: "scan the host" }],
    };
    expect(
      reduceConversation(base, { type: "message", record: { role: "user", content: "scan the host" } }).messages,
    ).toHaveLength(1);
    expect(
      reduceConversation(base, { type: "message", record: { role: "user", content: "now exploit it" } }).messages,
    ).toHaveLength(2);
  });
});

describe("the end of a turn", () => {
  test("7. task_done stops the run and clears the tail", () => {
    const c = fold([
      { type: "assistant_delta", text: "half a sen" },
      { type: "task_done", status: "ok" },
    ], { ...emptyConversation(), running: true });
    expect(c.running).toBe(false);
    expect(c.streamingText).toBe("");
  });

  test("8. task_error says so in the thread and does not leave a dangling tail", () => {
    const c = fold([
      { type: "assistant_delta", text: "half a sen" },
      { type: "task_error", error: "LLM stream error: timeout" },
    ], { ...emptyConversation(), running: true });
    expect(c.running).toBe(false);
    expect(c.streamingText).toBe("");
    expect(c.messages).toHaveLength(1);
    expect(c.messages[0].role).toBe("system");
    expect(c.messages[0].content).toContain("LLM stream error: timeout");
  });
});

describe("what reaches the operator and what does not", () => {
  test("9. only a GATING injection flag becomes a line", () => {
    // The server flags benign encoded blobs too. Surfacing those would put faint recurring
    // warnings in the thread that the operator cannot act on and learns to scroll past — which is
    // how a real one gets missed.
    expect(fold([{ type: "injection_flagged", tool: "http_request", tainted: false }]).messages).toHaveLength(0);
    const c = fold([
      { type: "injection_flagged", tool: "http_request", tainted: true, verdict: "likely", reasons: ["ignore previous"] },
    ]);
    expect(c.messages).toHaveLength(1);
    expect(c.messages[0].content).toContain("http_request");
    expect(c.messages[0].content).toContain("ignore previous");
  });

  test("10. infrastructure write failures are notices, never turns in the conversation", () => {
    const c = fold([
      { type: "taint_error", error: "disk full" },
      { type: "audit_error", error: "permission denied" },
    ]);
    expect(c.messages).toHaveLength(0);
    expect(c.notices).toEqual(["Taint store write failed: disk full", "Audit write failed: permission denied"]);
  });

  test("11. an event from a newer server is ignored, not fatal", () => {
    const before = fold([{ type: "assistant_delta", text: "hi" }]);
    const after = reduceConversation(before, { type: "budget_exceeded", spent: 9 } as SessionEvent);
    expect(after).toEqual(before);
  });
});

describe("context, rag and stage", () => {
  test("12. a later context event without `max` keeps the known window", () => {
    // dev_server sends `max` from _context_window() on every context push today, but a fold that
    // took num(evt.max) unconditionally would zero the denominator the moment one arrived without
    // it — and a usage bar against 0 renders as either nothing or infinity.
    const c = fold([
      { type: "context", prompt_tokens: 1000, completion_tokens: 50, max: 32768 },
      { type: "context", prompt_tokens: 1200, completion_tokens: 80 },
    ]);
    expect(c.context).toEqual({ promptTokens: 1200, completionTokens: 80, max: 32768 });
  });

  test("13. rag and stage land where the surface reads them", () => {
    const c = fold([{ type: "rag_consulted", count: 4 }, { type: "stage", stage: "VALIDATION" }]);
    expect(c.rag).toEqual({ count: 4 });
    expect(c.stage).toBe("VALIDATION");
    // An empty stage must not wipe a known one.
    expect(reduceConversation(c, { type: "stage", stage: "" }).stage).toBe("VALIDATION");
  });

  test("14. approval events bump an epoch instead of writing chat lines", () => {
    const c = fold([{ type: "approval_required", request_id: "r1" }, { type: "approval_resolved", request_id: "r1" }]);
    expect(c.approvalsEpoch).toBe(2);
    expect(c.messages).toHaveLength(0);
    // A timeout is the exception: the panel has already dropped it, so the thread is the only
    // place the operator can still learn the action was refused.
    const t = reduceConversation(c, { type: "approval_timeout", tool: "run_command" });
    expect(t.messages).toHaveLength(1);
    expect(t.messages[0].content).toContain("did NOT run");
  });
});

describe("seeding from the REST snapshot", () => {
  test("15. history, running and stage come from the snapshot; usage stays unreported", () => {
    // The SSE stream does not replay. Everything before this client connected has to arrive over
    // REST, or a reload shows an empty conversation that the server believes is mid-turn.
    const c = seedConversation({
      messages: [{ role: "user", content: "hi" }, { role: "assistant", content: "hello" }],
      running: true, stage: "RECON", last_prompt_tokens: 900,
    });
    expect(c.messages).toHaveLength(2);
    expect(c.running).toBe(true);
    expect(c.stage).toBe("RECON");
    // max 0 = window not reported yet, so the surface hides the bar rather than drawing it
    // against a denominator nobody sent.
    expect(c.context).toEqual({ promptTokens: 900, completionTokens: 0, max: 0 });
    expect(seedConversation({}).context).toBeNull();
  });
});

test("16. HANDLED_EVENTS names every case the fold implements", () => {
  // The Python side (test_session_event_vocabulary.py) compares this list against what
  // dev_server actually pushes. That comparison is only meaningful if the list describes this
  // file — a name here that the switch does not implement would make the server-side check pass
  // while the event fell through to `default`.
  // Probed from a MID-TURN conversation, not an empty one. `task_done` on an idle conversation
  // changes nothing by definition (running is already false, the tail is already empty), so an
  // empty baseline would report the one terminal event as unhandled — the test failing on its own
  // baseline rather than on the code.
  const busy = (): Conversation => ({ ...emptyConversation(), running: true, streamingText: "x" });
  const unchanged: string[] = [];
  for (const type of HANDLED_EVENTS) {
    if (JSON.stringify(reduceConversation(busy(), probeFor(type))) === JSON.stringify(busy())) {
      unchanged.push(type);
    }
  }
  expect(unchanged).toEqual([]);
});

/** A minimal event of each type that must produce a visible change in an empty conversation. */
function probeFor(type: string): SessionEvent {
  switch (type) {
    case "message": return { type, record: { role: "assistant", content: "x" } };
    case "assistant_delta":
    case "reasoning_delta": return { type, text: "x" };
    case "context": return { type, prompt_tokens: 1, completion_tokens: 1, max: 8 };
    case "rag_consulted": return { type, count: 1 };
    case "stage": return { type, stage: "RECON" };
    case "steer_applied": return { type, text: "x" };
    case "compacted": return { type, folded: 2 };
    case "task_done": return { type, status: "ok" };
    case "task_error": return { type, error: "x" };
    case "autonomous_event": return { type, event: { type: "phase_entered", phase: "RECON" } };
    case "autonomous_done": return { type, result: { status: "ok" } };
    case "approval_required":
    case "approval_resolved": return { type, request_id: "r1" };
    case "approval_timeout": return { type, tool: "run_command" };
    case "injection_flagged": return { type, tool: "http_request", tainted: true };
    case "taint_error":
    case "audit_error": return { type, error: "x" };
    default: return { type };
  }
}
