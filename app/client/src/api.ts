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
};
