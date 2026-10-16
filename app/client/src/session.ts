// The operator's session: the API key the client authenticates every /api call and SSE stream
// with (CLIENT_UI_DESIGN.md — the same key the classic UI's key field held). Kept per-viewer in
// localStorage, which is exactly what browser storage is for (a per-viewer convenience, not shared
// or durable server state), so every read and write is wrapped: a private window, cleared site
// data or a thumbnail capture can make localStorage throw or come back empty, and the client must
// still run (keyless — correct against a server with no key file).
//
// baseUrl is kept alongside it for the same reason: a browser tab opened straight at the built
// bundle (not served by the backend) can point itself at a remote server. Default "" is
// same-origin, which is how the Electron shell and the backend-served bundle both run.

const KEY_STORAGE = "oxpecker.apiKey";
const BASEURL_STORAGE = "oxpecker.baseUrl";
const LAST_ENGAGEMENT_STORAGE = "oxpecker.lastEngagement";

function read(name: string): string {
  try {
    return localStorage.getItem(name) ?? "";
  } catch {
    return "";
  }
}

function write(name: string, value: string): void {
  try {
    if (value) localStorage.setItem(name, value);
    else localStorage.removeItem(name);
  } catch {
    /* storage unavailable (private window, blocked) — the value just does not persist */
  }
}

export function getApiKey(): string {
  return read(KEY_STORAGE);
}

export function setApiKey(key: string): void {
  write(KEY_STORAGE, key.trim());
}

export function getBaseUrl(): string {
  return read(BASEURL_STORAGE);
}

export function setBaseUrl(url: string): void {
  write(BASEURL_STORAGE, url.replace(/\/$/, ""));
}

// The engagement the operator last had open, so the app reopens straight into it instead of the
// picker. Per-viewer like the key — a convenience, not state the server owns. The picker (now at
// /engagements) stays one click away for switching or creating; this only decides where "/" lands.
export function getLastEngagement(): string {
  return read(LAST_ENGAGEMENT_STORAGE);
}

export function setLastEngagement(engagementId: string): void {
  write(LAST_ENGAGEMENT_STORAGE, engagementId);
}

// The options every subscription and api call takes, built from the stored session. One place, so
// a surface never has to remember to thread the key — it reads the session and passes this.
export function sessionOptions(): { apiKey?: string; baseUrl?: string } {
  const apiKey = getApiKey();
  const baseUrl = getBaseUrl();
  return { ...(apiKey ? { apiKey } : {}), ...(baseUrl ? { baseUrl } : {}) };
}

// Adopt a key (and optional server) handed in the URL — `?key=...&server=...` — then scrub them
// from the address so the secret does not linger in the bar or in history. This is what lets the
// app open straight in: the Electron shell (or a bookmarked link) loads /app/?key=<key>, the
// client stores it, and the operator never pastes a key. A key already in localStorage is left
// untouched when the URL carries none, so a normal reload keeps the session.
export function bootstrapSessionFromUrl(
  loc: { search: string; pathname: string; hash: string } = window.location,
  history: { replaceState: (d: unknown, t: string, u: string) => void } = window.history,
): void {
  try {
    const params = new URLSearchParams(loc.search);
    const key = params.get("key");
    const server = params.get("server");
    if (!key && !server) return;
    if (key) setApiKey(key);
    if (server) setBaseUrl(server);
    params.delete("key");
    params.delete("server");
    const qs = params.toString();
    history.replaceState(null, "", `${loc.pathname}${qs ? `?${qs}` : ""}${loc.hash}`);
  } catch {
    /* no window, or storage/history unavailable — the landing's key field is the fallback */
  }
}
