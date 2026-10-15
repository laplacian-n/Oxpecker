import { useEffect, useRef, useState } from "react";
import { API_VERSION, EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";
import { ApiOptions, api } from "../api";

// Browser — the agent's own browser (CLIENT_UI_DESIGN.md §6.4). Two sides share the surface, and
// the line between them must stay sharp:
//
//   * YOU browse (the "You browse" tab): a person types a URL / signs in -> NOT brokered. A human
//     action in the human's own hands; this is how you "hand the agent this account." Rendered in
//     an <iframe>; a frame-busting site (X-Frame-Options / CSP) gets a one-click open-in-window.
//   * THE AGENT browses (the "Agent (live)" tab): the persistent per-engagement browser on the
//     server (agent/browser/live.py), whose screen is streamed here as JPEG frames. Its navigation
//     is brokered, scope-checked and audited server-side. start/stop control the screencast only;
//     the browser and its on-disk profile outlive a stop.

interface Tab { id: number; title: string; url: string; src: string }

let _nextTab = 1;
function newTab(url = ""): Tab {
  return { id: _nextTab++, title: url || "New tab", url, src: "" };
}

function normalizeUrl(u: string): string {
  u = u.trim();
  if (!u) return "";
  return /^https?:\/\//i.test(u) ? u : `https://${u}`;
}

// Input the operator sends to the agent browser's screen. Coordinates are in FRAME pixels (the
// screencast is at most 1280px wide), never the <img>'s rendered CSS pixels.
type AgentInput =
  | { action: "click"; x: number; y: number; button: "left" | "middle" | "right" }
  | { action: "key"; key: string }
  | { action: "type"; text: string }
  | { action: "scroll"; deltaY: number };

// Mirrors api.ts's postJson (kept local so this surface stays a single-file change).
async function postInput(engagementId: string, body: AgentInput, opts: ApiOptions): Promise<void> {
  const f = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const q = new URLSearchParams({ api_version: API_VERSION });
  if (opts.apiKey) q.set("key", opts.apiKey);
  const resp = await f(`${opts.baseUrl ?? ""}/api/engagements/${engagementId}/browser/input?${q.toString()}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`browser/input failed (${resp.status})`);
}

// The live screen, isolated in its own component so a new frame re-renders ONLY the <img> — never
// the whole surface. The operator was burned by the classic UI's full re-render flicker; here the
// poll loop lives in an effect and the only state it touches is the current frame's object URL.
function LiveAgentBrowser({ engagementId, apiOptions }: { engagementId: string; apiOptions: ApiOptions }) {
  const [src, setSrc] = useState<string>("");
  const [status, setStatus] = useState<"stopped" | "starting" | "live" | "error">("stopped");
  const [err, setErr] = useState<string>("");
  const seqRef = useRef(0);
  const urlRef = useRef<string>("");
  const runningRef = useRef(false);
  const addrRef = useRef<HTMLInputElement>(null);

  function revoke() {
    if (urlRef.current) { try { URL.revokeObjectURL(urlRef.current); } catch (_) { /* jsdom */ } urlRef.current = ""; }
  }

  async function poll() {
    while (runningRef.current) {
      try {
        const frame = await api.browserFrame(engagementId, seqRef.current, apiOptions);
        if (frame && runningRef.current) {
          seqRef.current = frame.seq;              // 204s leave seq unchanged, so the poll idles
          const url = URL.createObjectURL(frame.blob);
          revoke();
          urlRef.current = url;
          setSrc(url);                              // re-renders only this component's <img>
          if (status !== "live") setStatus("live");
        }
      } catch (_) { /* transient (server busy / restarting) — keep polling */ }
      await new Promise((r) => setTimeout(r, 150));
    }
  }

  async function start() {
    setErr("");
    setStatus("starting");
    try {
      await api.browserStart(engagementId, apiOptions);
    } catch (e) {
      setStatus("error");
      setErr(e instanceof Error ? e.message : String(e));
      return;
    }
    runningRef.current = true;
    void poll();
  }

  async function stop() {
    runningRef.current = false;
    try { await api.browserStop(engagementId, apiOptions); } catch (_) { /* best effort */ }
    setStatus("stopped");
  }

  async function navigate() {
    const url = normalizeUrl(addrRef.current?.value ?? "");
    if (!url) return;
    setErr("");
    try {
      await api.browserNavigate(engagementId, url, apiOptions);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  // Fire-and-forget: the screen never waits on an input round-trip. A dropped input is not something the
  // operator can act on; the next frame shows whether it landed.
  const send = (body: AgentInput) => { void postInput(engagementId, body, apiOptions).catch(() => {}); };

  // Stop the loop and free the last frame's object URL when the surface unmounts or is torn off.
  useEffect(() => () => { runningRef.current = false; revoke(); }, []);

  const running = status === "live" || status === "starting";
  return (
    <div className="agent-browser" data-testid="agent-browser" data-status={status}>
      <div className="agent-browser-bar">
        {!running ? (
          <button data-testid="agent-browser-start" onClick={start}>Start</button>
        ) : (
          <button data-testid="agent-browser-stop" onClick={stop}>Stop</button>
        )}
        <form style={{ display: "flex", flex: 1, gap: 6 }}
              onSubmit={(e) => { e.preventDefault(); navigate(); }}>
          <input ref={addrRef} data-testid="agent-browser-address" aria-label="agent address"
                 placeholder="https://in-scope-target/ — the agent navigates here (brokered)"
                 style={{ flex: 1 }} />
          <button type="submit" data-testid="agent-browser-go" disabled={!running}>Navigate</button>
        </form>
        <span data-testid="agent-browser-status" style={{ color: "var(--muted)", fontSize: 12 }}>{status}</span>
      </div>
      {err && <p className="agent-browser-err" data-testid="agent-browser-err" style={{ color: "var(--accent)", fontSize: 12 }}>{err}</p>}
      <div className="agent-browser-screen">
        {src ? (
          <>
            <p className="agent-input-hint" data-testid="agent-input-hint"
               style={{ color: "var(--muted)", fontSize: 12, margin: "4px 0" }}>
              Click and type on the screen to drive the browser as yourself (not brokered).
            </p>
            <img src={src} alt="agent browser screen" data-testid="agent-browser-frame" tabIndex={0}
                 style={{ display: "block", maxWidth: "100%", background: "#000" }}
                 onClick={(e) => {
                   // Map the click from CSS pixels to frame pixels via the rendered box.
                   const img = e.currentTarget;
                   const r = img.getBoundingClientRect();
                   if (r.width === 0 || r.height === 0 || img.naturalWidth === 0) return;
                   const x = Math.round((e.clientX - r.left) * (img.naturalWidth / r.width));
                   const y = Math.round((e.clientY - r.top) * (img.naturalHeight / r.height));
                   send({ action: "click", x, y, button: "left" });
                 }}
                 onKeyDown={(e) => {
                   // Only while focused (onKeyDown on the img). A single character types; anything else
                   // is a Playwright key name (Enter, Backspace, ArrowDown, ...).
                   e.preventDefault();
                   send(e.key.length === 1 ? { action: "type", text: e.key } : { action: "key", key: e.key });
                 }}
                 onWheel={(e) => {
                   e.preventDefault();
                   if (e.deltaY) send({ action: "scroll", deltaY: Math.round(e.deltaY) });
                 }} />
          </>
        ) : (
          <div className="browser-empty" data-testid="agent-browser-empty">
            {status === "stopped"
              ? "Start the agent browser to stream its screen. It is the persistent per-engagement browser; its profile and sign-ins outlive a stop."
              : "Waiting for the first frame…"}
          </div>
        )}
      </div>
    </div>
  );
}

export function Browser({
  subscription,
  engagementId = "",
  apiOptions = {},
}: {
  subscription: EngagementSubscription;
  engagementId?: string;
  apiOptions?: ApiOptions;
}) {
  useEngagement(subscription); // keep the surface a live subscriber (tear-off/resume parity)
  const [mode, setMode] = useState<"human" | "agent">("human");
  const [tabs, setTabs] = useState<Tab[]>([newTab()]);
  const [activeId, setActiveId] = useState<number>(tabs[0].id);
  const addrRef = useRef<HTMLInputElement>(null);
  const active = tabs.find((t) => t.id === activeId) ?? tabs[0];

  const patch = (id: number, p: Partial<Tab>) =>
    setTabs((ts) => ts.map((t) => (t.id === id ? { ...t, ...p } : t)));

  const go = () => {
    const url = normalizeUrl(addrRef.current?.value ?? "");
    if (!url) return;
    patch(active.id, { url, src: url, title: url });
  };

  const addTab = () => {
    const t = newTab();
    setTabs((ts) => [...ts, t]);
    setActiveId(t.id);
  };

  const closeTab = (id: number) => {
    setTabs((ts) => {
      const rest = ts.filter((t) => t.id !== id);
      const next = rest.length ? rest : [newTab()];
      if (id === activeId) setActiveId(next[0].id);
      return next;
    });
  };

  return (
    <div className="surface browser" data-surface="browser">
      <div className="browser-mode" data-testid="browser-mode">
        <button type="button" aria-pressed={mode === "human"} data-testid="browser-mode-human"
                onClick={() => setMode("human")}>You browse</button>
        <button type="button" aria-pressed={mode === "agent"} data-testid="browser-mode-agent"
                onClick={() => setMode("agent")}>Agent (live)</button>
      </div>

      {mode === "agent" ? (
        <LiveAgentBrowser engagementId={engagementId} apiOptions={apiOptions} />
      ) : (
        <>
          <div className="browser-tabs" role="tablist" data-testid="browser-tabs">
            {tabs.map((t) => (
              <span key={t.id} role="tab" aria-selected={t.id === activeId}
                    data-testid={`browser-tab-${t.id}`} className="browser-tab"
                    onClick={() => setActiveId(t.id)}>
                {t.title.length > 24 ? t.title.slice(0, 24) + "…" : t.title}
                <button className="tab-close" aria-label="close tab"
                        onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}>×</button>
              </span>
            ))}
            <button className="tab-new" aria-label="new tab" data-testid="browser-new-tab" onClick={addTab}>+</button>
          </div>

          <form className="browser-address" onSubmit={(e) => { e.preventDefault(); go(); }}>
            <input ref={addrRef} key={active.id} defaultValue={active.url} data-testid="browser-address"
                   aria-label="address" placeholder="https://in-scope-target/ — sign in to hand the agent this account" />
            <button type="submit" data-testid="browser-go">Go</button>
          </form>

          <p className="browser-note" data-testid="browser-note">
            You are browsing. Signing in here hands the agent this session — your navigation is not
            brokered; the agent's later navigation always is (scope-checked and audited server-side).
          </p>

          <div className="browser-page">
            {active.src ? (
              <>
                <iframe title="page" src={active.src} data-testid="browser-frame" />
                <div className="browser-fallback">
                  Some sites refuse to embed. If the page is blank,{" "}
                  <a href={active.src} target="_blank" rel="noreferrer" data-testid="browser-open-external">
                    open it in a new window
                  </a>.
                </div>
              </>
            ) : (
              <div className="browser-empty" data-testid="browser-empty">
                Enter an in-scope URL to browse. Cookie isolation per engagement ({engagementId}) and
                the agent's own brokered navigation arrive with the desktop BrowserView.
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
