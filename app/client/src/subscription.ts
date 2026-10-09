// The engagement subscription (CLIENT_UI_DESIGN.md §3–4): the one data path every surface draws
// from. It fetches a snapshot, then subscribes to the SSE stream from that snapshot's sequence
// number, applying each event to its projection. Reconnection and `Last-Event-ID` come from the
// browser's own `EventSource`; this wraps it with the snapshot seed, the projection fold, and the
// missed-count the client shows when it fell behind (§5.5).
//
// Everything external is injectable (the `EventSource` constructor, `fetch`), so the catch-up,
// resume and flood behaviour can be driven in a test without a running server or a real browser.

import { applyEvent, EngagementEvent, fromSnapshot, Projection, emptyProjection } from "./projection";

export const API_VERSION = "1.0.0";

type EventSourceLike = {
  addEventListener(type: string, listener: (ev: { data: string }) => void): void;
  close(): void;
  onerror: ((ev: unknown) => void) | null;
};
type EventSourceFactory = (url: string) => EventSourceLike;

export interface SubscriptionOptions {
  engagementId: string;
  apiKey?: string;
  baseUrl?: string;
  lastEventId?: number; // resume from here; omit (or 0) for a fresh subscriber reading the whole log
  fetchImpl?: typeof fetch;
  eventSourceFactory?: EventSourceFactory;
}

export type ProjectionListener = (projection: Projection) => void;

export class EngagementSubscription {
  private opts: Required<Pick<SubscriptionOptions, "engagementId" | "baseUrl">> & SubscriptionOptions;
  private projection: Projection = emptyProjection();
  private source: EventSourceLike | null = null;
  private listeners = new Set<ProjectionListener>();
  private fetchImpl: typeof fetch;
  private makeSource: EventSourceFactory;

  constructor(opts: SubscriptionOptions) {
    this.opts = { baseUrl: "", ...opts };
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.makeSource =
      opts.eventSourceFactory ??
      ((url: string) => new (globalThis as any).EventSource(url) as EventSourceLike);
  }

  getProjection(): Projection {
    return this.projection;
  }

  subscribe(listener: ProjectionListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(): void {
    for (const l of this.listeners) l(this.projection);
  }

  private query(params: Record<string, string | number | undefined>): string {
    const q = new URLSearchParams();
    q.set("api_version", API_VERSION);
    if (this.opts.apiKey) q.set("key", this.opts.apiKey);
    for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, String(v));
    return q.toString();
  }

  // Fetch the snapshot and open the stream. A server that cannot serve this API version answers
  // the snapshot request with 409 (§4.3); the caller sees the rejected promise and refuses to run
  // against it rather than half-working.
  async start(): Promise<void> {
    const at = this.opts.lastEventId;
    const snapUrl = `${this.opts.baseUrl}/api/engagements/${this.opts.engagementId}/snapshot?${this.query({ at })}`;
    const resp = await this.fetchImpl(snapUrl);
    if (!resp.ok) {
      throw new Error(`snapshot refused (${resp.status}): the client will not run against a server that cannot serve API ${API_VERSION}`);
    }
    this.projection = fromSnapshot(await resp.json());
    this.emit();

    const streamUrl = `${this.opts.baseUrl}/api/engagements/${this.opts.engagementId}/events?${this.query({ last_event_id: at })}`;
    const source = this.makeSource(streamUrl);
    this.source = source;
    source.addEventListener("control", (ev) => {
      const control = JSON.parse(ev.data);
      // The gap the server computed at connect (§5.5): how many events this window missed while
      // it was away. Shown until replay closes it.
      this.projection = { ...this.projection, missed: control.payload?.missed ?? 0 };
      this.emit();
    });
    source.addEventListener("message", (ev) => {
      const event = JSON.parse(ev.data) as EngagementEvent;
      this.projection = applyEvent(this.projection, event);
      this.emit();
    });
  }

  close(): void {
    this.source?.close();
    this.source = null;
  }
}
