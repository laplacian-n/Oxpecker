import { useEffect, useRef } from "react";
import { ConversationSubscription } from "../conversationStream";
import { useConversation } from "../useConversation";
import { api, ApiOptions } from "../api";

// The conversation itself: what the operator types and what the agent says back, streamed.
//
// This is the capability the React client did not have (A2). Chat.tsx could start and stop an
// autonomous run and show a raw event feed; it could not hold a conversation, so "where are my
// features" sent every operator back to the classic UI at /.
//
// Two rules from CLIENT_UI_DESIGN.md §2.1 decide the shape of this component, and both exist
// because a token stream re-renders it on every delta:
//
//   * The composer is UNCONTROLLED (rule 3). A controlled textarea whose value lived in state
//     would be re-rendered mid-keystroke by the delta flood, which drops characters and moves the
//     caret — the operator cannot type while the agent is answering. The textarea owns its own
//     value and is read on send.
//   * Rows are keyed by identity, not position (rule 4). The message list is append-only, so the
//     index IS stable identity here — a row's key never changes meaning, because nothing is ever
//     inserted above it. Said out loud because the rule reads as "never use the index", and the
//     reason behind it (a row recreated when something lands above it) does not apply to a
//     transcript. The streaming tail is deliberately NOT a row in this list; see conversation.ts.

export function Conversation({
  subscription,
  sessionId,
  apiOptions = {},
}: {
  subscription: ConversationSubscription;
  sessionId: string;
  apiOptions?: ApiOptions;
}) {
  const c = useConversation(subscription);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const tailRef = useRef<HTMLDivElement>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);

  // Follow the stream. Keyed on the lengths rather than on the conversation object so this fires
  // as tokens land, and `block: "nearest"` so it does not yank the page when the operator has
  // scrolled up to read something.
  useEffect(() => {
    // Optional-called, not assumed: scrollIntoView is absent in jsdom and in some embedded
    // webviews, and following the transcript is a courtesy. A component that threw here would
    // take the whole conversation down to avoid scrolling it.
    tailRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [c.messages.length, c.streamingText.length, c.streamingReasoning.length]);

  const send = async () => {
    const el = inputRef.current;
    const content = el?.value.trim() ?? "";
    if (!content) return;
    if (errorRef.current) errorRef.current.textContent = "";
    // Clear and show the message immediately. The POST plus the first token is seconds on a
    // loaded model; a composer that emptied itself and showed nothing reads as a dropped message,
    // and the operator sends again — straight into the server's 409.
    if (el) el.value = "";
    subscription.markSent(content);
    try {
      await api.sendMessage(sessionId, content, apiOptions);
    } catch (e: any) {
      subscription.markSendFailed();
      if (errorRef.current) errorRef.current.textContent = String(e?.message ?? e);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter is a newline — what every chat client does, and what the classic
    // UI did. IME composition must never send: a Thai or Japanese candidate is confirmed with
    // Enter, and sending on that would ship a half-composed word.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <section className="conversation" data-testid="conversation">
      <div className="conversation-status">
        {c.stage && <span className="tier-badge" data-testid="conv-stage">{c.stage}</span>}
        {c.rag && (
          <span className="tier-badge" data-testid="conv-rag" title="answer grounded in the local corpus">
            knowledge base · {c.rag.count}
          </span>
        )}
        {/* Only drawn once the server has reported a window. Before that `max` is 0 and a bar
            would be measured against a denominator nobody sent. */}
        {c.context && c.context.max > 0 && (
          <span className="tier-badge" data-testid="conv-context">
            context {c.context.promptTokens} / {c.context.max}
          </span>
        )}
      </div>

      <div className="transcript-scroll">
        {c.messages.length === 0 && !c.streamingText && !c.streamingReasoning && (
          <p className="conversation-empty" data-testid="conversation-empty">
            Ask anything, or describe the target. Your message goes to the agent.
          </p>
        )}
        <ol className="transcript" data-testid="transcript">
          {c.messages.map((m, i) => (
            <li key={m.message_id ?? i} data-role={m.role} data-testid={`msg-${i}`}>
              {m.reasoning_content && (
                <details className="reasoning">
                  <summary>thinking</summary>
                  <pre>{m.reasoning_content}</pre>
                </details>
              )}
              <div className="msg-body">{m.content}</div>
              {m.tool_name && <span className="tool-tag">{m.tool_name}</span>}
            </li>
          ))}
        </ol>

        {/* The live tail: the turn in flight. Reasoning first, because that is the order it
            arrives in and the order it is useful in. */}
        {(c.streamingReasoning || c.streamingText) && (
          <div className="live-tail" data-testid="live-tail">
            {c.streamingReasoning && (
              <details className="reasoning" open data-testid="live-reasoning">
                <summary>thinking…</summary>
                <pre>{c.streamingReasoning}</pre>
              </details>
            )}
            {c.streamingText && <div className="msg-body" data-testid="live-text">{c.streamingText}</div>}
          </div>
        )}

        {c.notices.length > 0 && (
          <ul className="conversation-notices" data-testid="conv-notices">
            {c.notices.map((n, i) => <li key={i}>{n}</li>)}
          </ul>
        )}

        <div ref={tailRef} />
      </div>

      <div className="composer" data-testid="composer">
        <textarea
          ref={inputRef} rows={2} defaultValue="" data-testid="message-input"
          onKeyDown={onKeyDown}
          placeholder={c.running ? "the agent is answering — your message queues after it" : "Message the agent…"}
        />
        {/* Not disabled while running. The server answers 409 and the operator is told, which is
            better than a dead button on a turn that has silently finished: `running` is derived
            from events, and a dropped stream would freeze the composer with no way back. */}
        <button onClick={() => void send()} data-testid="send-message">
          {c.running ? "Send (busy)" : "Send"}
        </button>
      </div>
      <p className="landing-error" ref={errorRef} data-testid="send-error" />
    </section>
  );
}
