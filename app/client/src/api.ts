// Read-model fetchers for the surfaces that draw from the engagement's structured stores rather
// than only the event stream (the Work surface's tree/notebook/findings). Every request carries
// the client's API version (§4.3) and the operator's key, the same handshake the subscription
// uses. `fetchImpl` is injectable so a surface can be tested without a server.

import { API_VERSION } from "./subscription";

export interface ApiOptions {
  baseUrl?: string;
  apiKey?: string;
  fetchImpl?: typeof fetch;
}

function query(apiKey?: string): string {
  const q = new URLSearchParams({ api_version: API_VERSION });
  if (apiKey) q.set("key", apiKey);
  return q.toString();
}

async function getJson(path: string, opts: ApiOptions): Promise<any> {
  const f = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const resp = await f(`${opts.baseUrl ?? ""}${path}?${query(opts.apiKey)}`);
  if (resp.status === 409) {
    throw new Error(`server cannot serve API ${API_VERSION}`);
  }
  if (!resp.ok) throw new Error(`${path} failed (${resp.status})`);
  return resp.json();
}

async function postJson(path: string, body: unknown, opts: ApiOptions): Promise<any> {
  const f = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const resp = await f(`${opts.baseUrl ?? ""}${path}?${query(opts.apiKey)}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`${path} failed (${resp.status})${await detailOf(resp)}`);
  return resp.json();
}

// FastAPI puts the reason a request was refused in `detail`, and the engagement validator fills it
// with every problem it found, joined — "allow_targets entry 'http://x' rejected: …;
// authorized_by is required". Dropping it left the operator with a bare 400 and a form they had
// to guess at. The status stays in the message ahead of it, because callers match on `(401)` and
// `(409)` to tell an auth failure from a version mismatch.
async function detailOf(resp: Response): Promise<string> {
  try {
    const body = await resp.json();
    const detail = (body as { detail?: unknown })?.detail;
    if (typeof detail === "string" && detail) return `: ${detail}`;
    // 422 reports a list of field errors instead of a string.
    if (Array.isArray(detail) && detail.length) {
      return `: ${detail.map((d: any) => d?.msg ?? JSON.stringify(d)).join("; ")}`;
    }
  } catch {
    /* not JSON, or already consumed — the status alone is still an answer */
  }
  return "";
}

export const api = {
  // The engagements the operator may open — the landing's list. A 401 here is the signal the key
  // is missing or wrong (the landing surfaces that), rather than a surface failing later.
  engagements: (opts: ApiOptions = {}): Promise<any[]> =>
    getJson(`/api/engagements`, opts),
  hypothesisGraph: (engagementId: string, opts: ApiOptions = {}) =>
    getJson(`/api/engagements/${engagementId}/hypothesis-graph`, opts),
  notebook: (engagementId: string, opts: ApiOptions = {}) =>
    getJson(`/api/engagements/${engagementId}/notebook`, opts),
  findings: (engagementId: string, opts: ApiOptions = {}) =>
    getJson(`/api/engagements/${engagementId}/findings`, opts),
  // Resolve an approval (§7). The endpoint is keyed by request id — the same id the
  // approval_required event carried — and commands go over REST, not the one-way event stream.
  resolveApproval: (requestId: string, approved: boolean, opts: ApiOptions = {}) =>
    postJson(`/api/approvals/${requestId}/resolve`, { approved, resolved_by: "web-ui" }, opts),
  // Start/stop an autonomous run for the engagement (§6.1 — chat is the control surface). Commands
  // go over REST; progress comes back on the engagement event stream the subscription reads.
  startRun: (engagementId: string, body: { mode?: string; user_message?: string }, opts: ApiOptions = {}) =>
    postJson(`/api/engagements/${engagementId}/pipeline/start`, body, opts),
  stopRun: (engagementId: string, opts: ApiOptions = {}) =>
    postJson(`/api/engagements/${engagementId}/pipeline/stop`, {}, opts),

  // ── Engagements ──────────────────────────────────────────────────────────────────────────────
  // The action classes an engagement may grant, each with the tools it gates and whether it is a
  // default. Fetched rather than hardcoded on purpose: three hand-kept copies of this list had
  // already drifted apart, and because the broker refuses on a class an engagement does not
  // grant, a stale copy shows up as a tool that silently cannot run.
  actionClasses: (opts: ApiOptions = {}): Promise<any[]> =>
    getJson(`/api/action-classes`, opts),
  // Scope entries are validated server-side only. The rules live in agent/engagement/intake.py,
  // which delegates to the broker's own scope parser so that what intake accepts is exactly what
  // the broker will enforce; a second opinion written in TypeScript would be a third one overall,
  // and the one that rejected `*.example.com` last time.
  createEngagement: (
    body: {
      engagement_id: string;
      description?: string;
      allow_targets: string[];
      deny_targets?: string[];
      allowed_action_classes?: string[];
      authorized_by: string;
      valid_hours?: number;
    },
    opts: ApiOptions = {},
  ) => postJson(`/api/engagements`, body, opts),

  // ── Chat sessions ────────────────────────────────────────────────────────────────────────────
  // A chat session is where a conversation lives; it belongs to an engagement (Session.engagement_id)
  // and carries the message history. Distinct from the engagement's own event log: see
  // conversationStream.ts on why the two streams are not interchangeable.
  sessions: (opts: ApiOptions = {}): Promise<any[]> =>
    getJson(`/api/sessions`, opts),
  createSession: (
    body: { engagement_id: string; thinking?: boolean; use_security_tools?: boolean },
    opts: ApiOptions = {},
  ) => postJson(`/api/sessions`, body, opts),
  // Commands go over REST; the turn comes back on the session event stream. 409 means a turn is
  // already in flight — reported as such rather than as a generic failure, because it is the one
  // error the operator can act on (wait, or stop the run).
  sendMessage: async (sessionId: string, content: string, opts: ApiOptions = {}) => {
    const f = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
    const resp = await f(`${opts.baseUrl ?? ""}/api/sessions/${sessionId}/messages?${query(opts.apiKey)}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (resp.status === 409) throw new Error("a turn is already running in this session");
    if (!resp.ok) throw new Error(`send failed (${resp.status})`);
    return resp.json();
  },

  // The persistent per-engagement live browser (§6.4). start/stop/navigate are commands; the screen
  // itself is pulled a frame at a time by browserFrame — a long-poll-lite over REST, not the event
  // stream (a JPEG is not a projection slice). start/stop control only the screencast; the browser
  // and its on-disk profile outlive a stop (see agent/browser/live.py).
  browserStart: (engagementId: string, opts: ApiOptions = {}) =>
    postJson(`/api/engagements/${engagementId}/browser/start`, {}, opts),
  browserStop: (engagementId: string, opts: ApiOptions = {}) =>
    postJson(`/api/engagements/${engagementId}/browser/stop`, {}, opts),
  browserNavigate: (engagementId: string, url: string, opts: ApiOptions = {}) =>
    postJson(`/api/engagements/${engagementId}/browser/navigate`, { url }, opts),
  // The newest frame, or null when the server has nothing newer than `since` (HTTP 204). Returns
  // the JPEG blob and the sequence number from X-Frame-Seq, which the caller passes back as `since`
  // next time — so a frame is fetched once and the poll idles (204) while the screen is still.
  browserFrame: async (
    engagementId: string, since: number, opts: ApiOptions = {},
  ): Promise<{ blob: Blob; seq: number } | null> => {
    const f = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
    const q = new URLSearchParams({ api_version: API_VERSION, since: String(since) });
    if (opts.apiKey) q.set("key", opts.apiKey);
    const resp = await f(`${opts.baseUrl ?? ""}/api/engagements/${engagementId}/browser/frame?${q.toString()}`);
    if (resp.status === 204) return null;
    if (!resp.ok) throw new Error(`browser frame failed (${resp.status})`);
    const seq = Number(resp.headers.get("X-Frame-Seq") ?? 0);
    return { blob: await resp.blob(), seq };
  },
};
