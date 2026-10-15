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
  if (!resp.ok) throw new Error(`${path} failed (${resp.status})`);
  return resp.json();
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
