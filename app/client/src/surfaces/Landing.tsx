import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiOptions } from "../api";
import { getApiKey, setApiKey, getBaseUrl, setBaseUrl, sessionOptions } from "../session";

// The landing (CLIENT_UI_DESIGN.md): where the operator authenticates and chooses an engagement.
// The classic UI had a key field and an engagement list; the new client needs the same entry point
// or it can only ever open the hard-coded lab-default keylessly. Two jobs, one screen:
//   1. hold the operator's API key (and, for a tab opened outside the backend, the server URL),
//      persisted per-viewer in localStorage via session.ts;
//   2. list the engagements that key can open, each a link into its chat surface.
// A 401 on the list is the unambiguous "key missing or wrong" signal, shown here rather than as a
// surface failing later. `apiImpl` is injectable so this is testable without a server.
export function Landing({ apiImpl = api }: { apiImpl?: typeof api }) {
  const [key, setKey] = useState(getApiKey());
  const [baseUrl, setBase] = useState(getBaseUrl());
  const [engagements, setEngagements] = useState<any[] | null>(null);
  const [error, setError] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (opts: ApiOptions) => {
    setLoading(true);
    setError("");
    try {
      const list = await apiImpl.engagements(opts);
      setEngagements(Array.isArray(list) ? list : []);
    } catch (e: any) {
      const msg = String(e?.message ?? e);
      setEngagements(null);
      setError(
        msg.includes("(401)") || msg.includes("(403)")
          ? "The server rejected that key. Check it and connect again."
          : msg.includes("(409)")
            ? "This server speaks a different API version than this client."
            : `Could not reach the server${baseUrl ? ` at ${baseUrl}` : ""}. Is it running?`,
      );
    } finally {
      setLoading(false);
    }
  }, [apiImpl, baseUrl]);

  // Try with whatever is already stored, so a returning operator lands straight on their list.
  useEffect(() => { void load(sessionOptions()); }, [load]);

  const connect = (e: React.FormEvent) => {
    e.preventDefault();
    setApiKey(key);
    setBaseUrl(baseUrl);
    void load(sessionOptions());
  };

  return (
    <div className="landing" data-testid="landing">
      <h1>Oxpecker</h1>
      <form onSubmit={connect} className="landing-connect">
        <label>
          API key
          <input
            type="password" value={key} data-testid="key-input"
            onChange={(e) => setKey(e.target.value)} placeholder="paste the server's key"
          />
        </label>
        <label>
          Server <span className="hint">(blank = this server)</span>
          <input
            type="text" value={baseUrl} data-testid="baseurl-input"
            onChange={(e) => setBase(e.target.value)} placeholder="http://host:7777"
          />
        </label>
        <button type="submit" data-testid="connect">Connect</button>
      </form>

      {loading && <p data-testid="landing-loading">Connecting…</p>}
      {error && <p className="landing-error" data-testid="landing-error">{error}</p>}

      {engagements !== null && !error && (
        engagements.length === 0 ? (
          <p data-testid="landing-empty">
            No engagements yet. Create one (scoped to your target) in the classic UI at <a href="/">/</a>,
            then it appears here.
          </p>
        ) : (
          <ul className="engagement-list" data-testid="engagement-list">
            {engagements.map((e) => (
              <li key={e.engagement_id}>
                <Link to={`/${e.engagement_id}/chat`} data-testid={`open-${e.engagement_id}`}>
                  <strong>{e.engagement_id}</strong>
                  {e.description ? <span className="eng-desc"> — {e.description}</span> : null}
                </Link>
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}
