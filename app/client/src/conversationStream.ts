// The session subscription: the conversation's one data path, built the same way as
// EngagementSubscription (snapshot over REST, then SSE, everything injectable) so both streams
// are reasoned about the same way — but they are different streams and this one has different
// guarantees, which is the part worth stating:
//
//   * No replay and no Last-Event-ID. /api/sessions/{id}/events hands each subscriber a fresh
//     queue (Session.subscribe) and sends only what is pushed from then on, so everything that
//     happened before this window connected has to come from GET /api/sessions/{id}. There is no
//     "missed" count to show, because the server does not know and cannot say.
//   * No `control` frame. Every frame is a plain `data:` line.
//   * Reconnection is the browser's. EventSource retries on its own; because there is no resume
//     cursor, a reconnect re-seeds nothing — a turn that finished during the gap is recovered on
//     the next snapshot fetch, not from the stream.
//
// One subscription per (window, session). Two windows on one session each get the full stream:
// the server fans out per subscriber (a single shared queue used to SPLIT the stream across tabs).

import {
  Conversation, SessionEvent, emptyConversation, reduceConversation, seedConversation,
} from "./conversation";
import { API_VERSION } from "./subscription";

type EventSourceLike = {
  addEventListener(type: string, listener: (ev: { data: string }) => void): void;
  close(): void;
  onerror: ((ev: unknown) => void) | null;
};
type EventSourceFactory = (url: string) => EventSourceLike;

export interface ConversationSubscriptionOptions {
  sessionId: string;
  apiKey?: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  eventSourceFactory?: EventSourceFactory;
}

export type ConversationListener = (conversation: Conversation) => void;

export class ConversationSubscription {
  private opts: ConversationSubscriptionOptions & { baseUrl: string };
  private conversation: Conversation = emptyConversation();
  private source: EventSourceLike | null = null;
  private listeners = new Set<ConversationListener>();
  private fetchImpl: typeof fetch;
  private makeSource: EventSourceFactory;

  constructor(opts: ConversationSubscriptionOptions) {
    this.opts = { baseUrl: "", ...opts };
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.makeSource =
      opts.eventSourceFactory ??
      ((url: string) => new (globalThis as any).EventSource(url) as EventSourceLike);
  }

  getConversation(): Conversation {
    return this.conversation;
  }

  subscribe(listener: ConversationListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const l of this.listeners) l(this.conversation);
  }

  private query(): string {
    const q = new URLSearchParams({ api_version: API_VERSION });
    if (this.opts.apiKey) q.set("key", this.opts.apiKey);
    return q.toString();
  }

  /**
   * Mark the operator's own message as sent: appended locally and the turn flagged running, before
   * the server has said anything.
   *
   * Optimistic on purpose. The round trip is a POST and then the first token, which on a loaded
   * model is seconds — long enough that a composer which cleared itself and showed nothing reads
   * as a dropped message, and the operator sends again (and gets a 409). The echo that comes back
   * is deduped in the fold.
   */
  markSent(content: string): void {
    this.conversation = {
      ...this.conversation,
      messages: [...this.conversation.messages, { role: "user", content }],
      running: true,
      // A new turn's answer starts empty; anything left in the tail belongs to the previous one.
      streamingText: "",
      streamingReasoning: "",
    };
    this.emit();
  }

  /** The turn never started (the POST failed): undo the optimistic running flag. */
  markSendFailed(): void {
    this.conversation = { ...this.conversation, running: false };
    this.emit();
  }

  async start(): Promise<void> {
    const base = `${this.opts.baseUrl}/api/sessions/${this.opts.sessionId}`;
    const resp = await this.fetchImpl(`${base}?${this.query()}`);
    if (!resp.ok) {
      throw new Error(
        `session ${this.opts.sessionId} refused (${resp.status}) — the conversation cannot be ` +
          `seeded, and the stream alone does not replay what came before`,
      );
    }
    this.conversation = seedConversation(await resp.json());
    this.emit();

    const source = this.makeSource(`${base}/events?${this.query()}`);
    this.source = source;
    source.addEventListener("message", (ev) => {
      let event: SessionEvent;
      try {
        event = JSON.parse(ev.data) as SessionEvent;
      } catch {
        // A malformed frame is one frame. Taking the conversation down over it would lose the
        // whole turn the operator is watching.
        return;
      }
      this.conversation = reduceConversation(this.conversation, event);
      this.emit();
    });
  }

  close(): void {
    this.source?.close();
    this.source = null;
  }
}
