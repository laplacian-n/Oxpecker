import { useEffect, useMemo, useRef, useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { api, ApiOptions } from "../api";
import { EngagementEvent } from "../projection";
import { Conversation } from "./Conversation";
import { ConversationSubscription } from "../conversationStream";
import { useChatSession } from "../useChatSession";
import { useSessionDetail } from "../useSessionDetail";
import { useClientExecutor } from "../useClientExecutor";
import { SessionPanel } from "./SessionPanel";

// Chat — the primary, always-present surface (CLIENT_UI_DESIGN.md §6.1). It now holds BOTH things
// the operator does here: the conversation (<Conversation/>, streamed off the session event
// stream) and the autonomous run controls plus the engagement's live event feed.
//
// The two halves draw from two different streams and neither can substitute for the other — see
// conversationStream.ts. They are kept in one surface because §6.1 says this is where the
// operator drives the engagement, and splitting "talk to it" from "set it running" into separate
// surfaces is what made the classic UI the only place you could do both. The feed and the goal input never share a value (§2.1 rule 3: the goal box is
// uncontrolled), so the engagement event flood — which re-renders this component as the feed grows
// — can never drop a character or move the caret while the operator types a goal.

// A run's lifecycle, read from the event stream (the one data path): the driver mirrors its phase
// and lifecycle events there, so "is a run going" is derived from events, not guessed. The last
// lifecycle marker wins.
const RUN_STARTED = new Set(["phase_entered", "wave_started", "worker_spawned", "phase_advanced"]);
const RUN_ENDED = new Set([
  "run_complete", "autonomous_done", "stopped_by_operator", "blocked",
  "wall_clock_exceeded", "task_error",
]);

function runIsActive(events: EngagementEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const k = events[i].kind;
    if (RUN_ENDED.has(k)) return false;
    if (RUN_STARTED.has(k)) return true;
  }
  return false;
}

export function Chat({
  subscription,
  engagementId = "",
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  engagementId?: string;
  apiOptions?: ApiOptions;
}) {
  const projection = useEngagement(subscription);
  const goalRef = useRef<HTMLTextAreaElement>(null);
  const session = useChatSession(engagementId, apiOptions);
  const { sessionId, error: sessionError } = session;
  const { detail, refresh: refreshDetail } = useSessionDetail(sessionId, apiOptions);
  // A this_computer session's tool I/O is handed to the desktop shell, which only runs it while
  // something is pulling. Driven by the session's own mode, so switching conversations switches
  // the executor with it.
  useClientExecutor(detail?.isolation_mode);
  // One subscription per (window, session) — rebuilt only when the session changes, never on a
  // token, or every delta would tear down the stream that delivered it.
  const conversation = useMemo(
    () => (sessionId ? new ConversationSubscription({ sessionId, ...apiOptions }) : null),
    // apiOptions is a fresh literal each render; see the same note in useChatSession.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionId],
  );
  useEffect(() => {
    if (!conversation) return;
    // A refused snapshot is reported by <Conversation/> staying empty rather than by an
    // exception: the autonomous controls below must survive a server whose session API is down.
    void conversation.start().catch(() => {});
    return () => conversation.close();
  }, [conversation]);
  // Optimistic "busy" so the button reacts the instant it is clicked; the event stream then takes
  // over as the source of truth (a run that actually started keeps it true; an end event clears it).
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const streamSaysRunning = runIsActive(projection.events);
  const running = streamSaysRunning || busy;

  useEffect(() => {
    if (streamSaysRunning) setBusy(false); // the stream confirms a run — stop guessing
  }, [streamSaysRunning]);

  const start = async () => {
    const goal = goalRef.current?.value.trim() ?? "";
    setError("");
    setBusy(true);
    try {
      await api.startRun(engagementId, { mode: "autonomous", user_message: goal }, apiOptions);
    } catch (e: any) {
      setBusy(false);
      setError(String(e?.message ?? e));
    }
  };

  const stop = async () => {
    try {
      await api.stopRun(engagementId, apiOptions);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  };

  return (
    <div className="surface" data-surface="chat">
      <header>
        <h1>Chat</h1>
        {running && <span className="run-badge" data-testid="run-active">● running</span>}
        {projection.missed > 0 && (
          <span role="status" data-testid="missed">
            disconnected — you missed {projection.missed} events
          </span>
        )}
      </header>

      {/* Sidebar beside the conversation, not above it: which conversation is open is a standing
          choice, and the transcript is what the operator reads. */}
      <SessionPanel state={session} detail={detail} onDetailChanged={refreshDetail}
                    apiOptions={apiOptions} />

      {conversation && sessionId ? (
        <Conversation subscription={conversation} sessionId={sessionId} apiOptions={apiOptions} />
      ) : (
        <p data-testid="no-conversation" className="landing-error">
          {sessionError
            ? `No conversation: ${sessionError}`
            : "Opening a conversation for this engagement…"}
        </p>
      )}

      {/* The control: start an autonomous run with a goal, or stop one in flight. §6.1 — chat is
          where the operator drives the engagement. */}
      <div className="run-control" data-testid="run-control">
        {running ? (
          <button onClick={stop} data-testid="stop-run">Stop run</button>
        ) : (
          <>
            <textarea
              ref={goalRef} data-testid="chat-input" rows={2} defaultValue=""
              placeholder="Goal for the run — e.g. find auth and injection flaws on the in-scope target"
            />
            <button onClick={start} data-testid="start-run">Start autonomous run</button>
          </>
        )}
      </div>
      {error && <p className="landing-error" data-testid="run-error">{error}</p>}

      {/* §7: approvals surface in chat too, because chat is the surface the operator is actually
          looking at. One request, one state — resolving here clears it everywhere. */}
      <ApprovalsPanel projection={projection} apiOptions={apiOptions} />

      {/* Keyed by identity (seq), never by position (§2.1 rule 4): an event arriving mid-list
          never recreates the rows below it. */}
      <ol data-testid="event-feed" className="chat-log">
        {projection.events.map((e) => (
          <li key={e.seq} data-kind={e.kind}>
            #{e.seq} {e.kind}
            {e.payload?.detail ? ` — ${e.payload.detail}` : ""}
          </li>
        ))}
      </ol>
      <div data-testid="event-count">{projection.events.length}</div>
    </div>
  );
}
