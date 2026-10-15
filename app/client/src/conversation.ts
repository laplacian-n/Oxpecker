// The conversation: a pure fold of the SESSION event stream into what the chat surface draws.
//
// Two streams reach this client and they are not interchangeable (A2 in the architecture review):
//   * the ENGAGEMENT stream (event_log.py -> projection.ts) carries workers, waves and flow — what
//     the other four surfaces render;
//   * the SESSION stream (/api/sessions/{id}/events) carries the turn: token deltas, finished
//     message records, context usage, errors.
// A conversation can only be built from the second. Nothing in this client read it before.
//
// Why a pure reducer rather than mutation inside the component: the vocabulary is 20 event types,
// several with semantics that are not guessable from their names (see below), and the streaming
// bug the classic UI shipped for weeks — renderStreamingMessage() was dead code, so deltas never
// appeared — was invisible precisely because the fold lived inside a render path nothing could
// test. Here the fold is a function: every event type is exercised against it directly.
//
// The semantics below are READ OUT of dev_server.py and static/index.html's handleServerEvent(),
// not inferred:
//
//  1. Deltas are INCREMENTAL, not cumulative. dev_server does `collected_text += buf` and then
//     pushes `buf`. A client that assigns instead of appends shows only the final token.
//  2. The live tail is NOT part of the message list. Session.append() pushes a `message` event for
//     every record it stores, including the finished assistant turn, so a client that appended
//     deltas INTO the list and then also appended the record would render the answer twice.
//     streamingText/streamingReasoning are separate fields, cleared when the record lands.
//  3. The user's own message comes back as a `message` event. The composer already added it
//     optimistically, so it is deduped against an identical last user message. Deduping is the
//     ONLY correct guard here: the classic UI also drops the echo outright while a turn is
//     running, which is safe for one window and wrong for this client, where a surface can be
//     torn off (§3) and two windows watch one session. The second window never added the message
//     optimistically, so a running-based guard would leave it showing an answer to a question it
//     never displays.
//  4. `injection_flagged` fires for benign encoded blobs too. Only a flag that GATES the session
//     (tainted) is worth a line; the rest would be faint recurring noise the operator cannot act on.
//  5. `taint_error` / `audit_error` are infrastructure failures, not conversation. They surface as
//     transient notices, never as a turn in the thread.

export interface ConversationMessage {
  role: string;
  content: string;
  message_id?: string | null;
  reasoning_content?: string | null;
  tool_calls?: unknown;
  tool_name?: string | null;
}

export interface ContextUsage {
  promptTokens: number;
  completionTokens: number;
  max: number;
}

export interface Conversation {
  messages: ConversationMessage[];
  /** The answer being streamed right now. Not in `messages` — see note 2. */
  streamingText: string;
  /** The model's reasoning for the in-flight turn, streamed the same way. */
  streamingReasoning: string;
  running: boolean;
  context: ContextUsage | null;
  /** The last knowledge-base consultation, so the operator can see the answer is grounded. */
  rag: { count: number } | null;
  stage: string;
  /** Infrastructure failures (note 5), newest last. The surface shows and forgets these. */
  notices: string[];
  /** Bumped whenever an approval event lands, so the surface can refetch the approval list. */
  approvalsEpoch: number;
}

export interface SessionEvent {
  type: string;
  [key: string]: unknown;
}

export function emptyConversation(): Conversation {
  return {
    messages: [], streamingText: "", streamingReasoning: "", running: false,
    context: null, rag: null, stage: "", notices: [], approvalsEpoch: 0,
  };
}

/** Seed from GET /api/sessions/{id} — the durable history the stream does not replay. */
export function seedConversation(snapshot: {
  messages?: ConversationMessage[];
  running?: boolean;
  stage?: string;
  last_prompt_tokens?: number;
}): Conversation {
  const base = emptyConversation();
  return {
    ...base,
    messages: snapshot.messages ?? [],
    running: !!snapshot.running,
    stage: snapshot.stage ?? "",
    // No completion count and no window are known until the first `context` event of this
    // client's lifetime, and inventing either would render a usage bar against a made-up
    // denominator. 0 means "not reported yet" and the surface hides the bar.
    context: snapshot.last_prompt_tokens
      ? { promptTokens: snapshot.last_prompt_tokens, completionTokens: 0, max: 0 }
      : null,
  };
}

function withSystemLine(c: Conversation, content: string): Conversation {
  return { ...c, messages: [...c.messages, { role: "system", content }] };
}

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const num = (v: unknown, fallback = 0): number => (typeof v === "number" ? v : fallback);

/** Fold one session event. Pure: same input, same output, no I/O. */
export function reduceConversation(c: Conversation, evt: SessionEvent): Conversation {
  switch (evt.type) {
    case "message": {
      const record = evt.record as ConversationMessage | undefined;
      if (!record) return c;
      if (record.role === "user") {
        // Note 3: dedup only, deliberately not gated on `running`.
        const last = c.messages[c.messages.length - 1];
        if (last && last.role === "user" && last.content === record.content) return c;
      }
      const messages = [...c.messages, record];
      if (c.streamingText && record.role === "assistant") {
        // Note 2: the record normally carries the whole answer and replaces the tail. When it
        // does not (a turn that ended with tool calls and no prose), the tail is the only copy
        // of what the operator watched arrive, so it is kept rather than dropped.
        if (!record.content) {
          messages[messages.length - 1] = { ...record, content: c.streamingText };
        }
        return { ...c, messages, streamingText: "", streamingReasoning: "" };
      }
      return { ...c, messages };
    }

    // Note 1: append.
    case "assistant_delta":
      return { ...c, streamingText: c.streamingText + str(evt.text) };
    case "reasoning_delta":
      return { ...c, streamingReasoning: c.streamingReasoning + str(evt.text) };

    case "context":
      return {
        ...c,
        context: {
          promptTokens: num(evt.prompt_tokens),
          completionTokens: num(evt.completion_tokens),
          // `max` is only present on some pushes; keep the last known window rather than
          // resetting the denominator to zero mid-turn.
          max: num(evt.max) || c.context?.max || 0,
        },
      };

    case "rag_consulted":
      return { ...c, rag: { count: num(evt.count) } };

    case "stage":
      return str(evt.stage) ? { ...c, stage: str(evt.stage) } : c;

    case "steer_applied":
      return withSystemLine(c, `Steer applied mid-run: ${str(evt.text)}`);

    case "compacted":
      return withSystemLine(
        c,
        `${str(evt.scope) === "in-turn" ? "Compacted mid-run — folded " : "Context compacted — folded "}` +
          `${num(evt.folded)} earlier messages into a summary`,
      );

    // Terminal for the turn. Both clear the tail: whatever it held has either landed as a record
    // or died with the turn, and leaving it would pin a half-sentence under the thread forever.
    case "task_done":
      return { ...c, running: false, streamingText: "", streamingReasoning: "" };
    case "task_error":
      return {
        ...withSystemLine(c, `⚠ Error: ${str(evt.error, "unknown error")}`),
        running: false, streamingText: "", streamingReasoning: "",
      };

    case "autonomous_event": {
      const e = (evt.event ?? {}) as Record<string, unknown>;
      // Different event types put their text under different keys, so a run that blocks or
      // crashes shows WHY instead of an empty "[blocked]" line.
      const info = str(e.detail) || str(e.phase) || str(e.reason) || str(e.error) || str(e.message);
      return withSystemLine(c, `[${str(e.type, "event")}] ${info}`.trimEnd());
    }
    case "autonomous_done": {
      const r = (evt.result ?? evt) as Record<string, unknown>;
      return {
        ...withSystemLine(
          c,
          `Autonomous run finished — ${str(r.status, "done")}${str(r.reason) ? ` · ${str(r.reason)}` : ""}`,
        ),
        running: false, streamingText: "", streamingReasoning: "",
      };
    }
    // Approvals live in their own panel (§7) and deliberately do not become chat lines — except a
    // timeout, which is the one the operator never sees in the panel because it has already gone.
    case "approval_required":
    case "approval_resolved":
      return { ...c, approvalsEpoch: c.approvalsEpoch + 1 };
    case "approval_timeout":
      return {
        ...withSystemLine(
          c,
          `Approval for ${str(evt.tool, "an action")} went unanswered and was treated as ` +
            `declined — the action did NOT run`,
        ),
        approvalsEpoch: c.approvalsEpoch + 1,
      };

    case "injection_flagged": {
      if (!evt.tainted) return c; // Note 4.
      const reasons = Array.isArray(evt.reasons) ? (evt.reasons as unknown[]).map((r) => str(r)) : [];
      return withSystemLine(
        c,
        `⚠ Prompt injection flagged in ${str(evt.tool, "tool")} output ` +
          `(${str(evt.verdict, "flagged")}${reasons.length ? `: ${reasons.join(", ")}` : ""}). ` +
          `The session is held to passive recon — further actions need your approval.`,
      );
    }

    // Note 5.
    case "taint_error":
      return { ...c, notices: [...c.notices, `Taint store write failed: ${str(evt.error)}`] };
    case "audit_error":
      return { ...c, notices: [...c.notices, `Audit write failed: ${str(evt.error)}`] };

    // `autonomous_error` is deliberately NOT handled. static/index.html has a case for it, but
    // the only code that pushes it is agent/web/server.py -- the stripped early scaffold nothing
    // launches (see its entry in doc/PROJECT_OVERVIEW.md). dev_server.py, the backend every
    // deployment goes through, cannot emit it, so a case here would be unreachable by
    // construction. test_session_event_vocabulary.py fails if that ever stops being true.
    default:
      // An event this client does not know is not an error: the server may be newer. Dropping it
      // is correct; crashing the fold over it would take the whole conversation down.
      return c;
  }
}

/** Every event type this fold handles. Exported so a test can assert the vocabulary is complete. */
export const HANDLED_EVENTS: readonly string[] = [
  "message", "assistant_delta", "reasoning_delta", "context", "rag_consulted", "stage",
  "steer_applied", "compacted", "task_done", "task_error", "autonomous_event", "autonomous_done",
  "approval_required", "approval_resolved", "approval_timeout",
  "injection_flagged", "taint_error", "audit_error",
];
