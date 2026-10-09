import { useRef } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { ApiOptions } from "../api";

// Chat — the primary, always-present surface (CLIENT_UI_DESIGN.md §6.1). In the skeleton it shows
// the live event feed and the control input; the directives/approvals/strategist content are
// steps 3+.
//
// The input is UNCONTROLLED (§2.1 rule 3: "input elements own their own value"). React never
// writes into it, so the engagement event flood — which re-renders this component as the feed
// grows — can never drop a character or move the caret. This is the exact trade §2 says the old
// index.html could not make: it kept typing intact only by refusing to render while someone
// typed; here rendering and typing are both uninterrupted because they do not share a value.
export function Chat({
  subscription,
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  apiOptions?: ApiOptions;
}) {
  const projection = useEngagement(subscription);
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="surface" data-surface="chat">
      <header>
        <h1>Chat</h1>
        {projection.missed > 0 && (
          <span role="status" data-testid="missed">
            disconnected — you missed {projection.missed} events
          </span>
        )}
      </header>

      {/* §7: approvals surface in chat too, because chat is the surface the operator is actually
          looking at. One request, one state — resolving here clears it everywhere. */}
      <ApprovalsPanel projection={projection} apiOptions={apiOptions} />

      {/* Keyed by identity (seq), never by position (§2.1 rule 4): an event arriving mid-list
          never recreates the rows below it. */}
      <ol data-testid="event-feed">
        {projection.events.map((e) => (
          <li key={e.seq} data-kind={e.kind}>
            #{e.seq} {e.kind}
          </li>
        ))}
      </ol>
      <div data-testid="event-count">{projection.events.length}</div>

      <form
        onSubmit={(ev) => {
          ev.preventDefault();
          // The input owns its value; the control path reads it on submit and clears it
          // imperatively, rather than binding it to state.
          if (inputRef.current) inputRef.current.value = "";
        }}
      >
        <input ref={inputRef} type="text" aria-label="message" defaultValue="" data-testid="chat-input" />
        <button type="submit">Send</button>
      </form>
    </div>
  );
}
