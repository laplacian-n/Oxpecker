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

// The options every subscription and api call takes, built from the stored session. One place, so
// a surface never has to remember to thread the key — it reads the session and passes this.
export function sessionOptions(): { apiKey?: string; baseUrl?: string } {
  const apiKey = getApiKey();
  const baseUrl = getBaseUrl();
  return { ...(apiKey ? { apiKey } : {}), ...(baseUrl ? { baseUrl } : {}) };
}
