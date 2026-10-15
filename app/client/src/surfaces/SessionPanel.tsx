import { useEffect, useRef, useState } from "react";
import { api, ApiOptions } from "../api";
import { ChatSessionState } from "../useChatSession";

// The session sidebar: which conversation is open, the others in this engagement, opening a new
// one and removing one — plus the three per-session controls the classic UI had and /app/ did not
// (thinking, steer, and an honest readout of where tool I/O actually ran).
//
// Isolation is two separate questions the UI has to keep apart, because conflating them is how an
// operator ends up believing a session is confined when it is not:
//
//   * MODE — where the tool I/O runs. "cloud" is this server, inside the tier below;
//     "this_computer" hands it to the desktop app and runs it from the operator's own network
//     position. Scope is enforced on the server either way.
//   * TIER — which sandbox confines it when it runs here. The host may not be able to provide the
//     one asked for, so the server reports the REQUEST and the OUTCOME separately
//     (isolation_tier vs effective_isolation_tier) and exec-verifies the request
//     (isolation_verified). All three are shown; a UI that showed only the request would claim
//     isolation that never happened.
//
// The tier list is fetched from /api/isolation rather than written here: create_session validates
// against sandbox/availability.TIERS and answers 400 on an unknown name, so a hardcoded copy is a
// picker offering something the server refuses. Unavailable tiers are listed, disabled, with the
// probe's own reason — the §8 "say why" treatment, because a tier this host cannot provide is a
// property of the host and not a broken build.

interface Tier { tier: string; available: boolean; reason: string; provides: string }
interface Mode { mode: string; note: string }
interface IsolationInfo { tiers: Tier[]; modes: Mode[]; default_tier: string; platform: string }

export interface SessionDetail {
  thinking?: boolean;
  isolation_mode?: string;
  isolation_tier?: string;
  effective_isolation_tier?: string | null;
  isolation_verified?: { ok?: boolean; reason?: string } | null;
}

export function SessionPanel({
  state,
  detail,
  onDetailChanged,
  apiOptions = {},
  apiImpl = api,
}: {
  state: ChatSessionState;
  /** From GET /api/sessions/{id}; absent until it has been fetched. */
  detail?: SessionDetail;
  onDetailChanged?: () => void;
  apiOptions?: ApiOptions;
  apiImpl?: Pick<typeof api, "isolation" | "setThinking" | "steer">;
}) {
  const [opening, setOpening] = useState(false);
  const [iso, setIso] = useState<IsolationInfo | null>(null);
  const [isoError, setIsoError] = useState("");
  const [mode, setMode] = useState("cloud");
  const [tier, setTier] = useState("");
  const [thinkNew, setThinkNew] = useState(false);
  const [busy, setBusy] = useState("");
  const steerRef = useRef<HTMLInputElement>(null);
  const [steerError, setSteerError] = useState("");

  useEffect(() => {
    if (!opening || iso) return;
    let live = true;
    (async () => {
      try {
        const info = (await apiImpl.isolation(apiOptions)) as IsolationInfo;
        if (!live) return;
        setIso(info);
        setTier(info.default_tier);
      } catch (e: any) {
        if (!live) return;
        // No list, no guess. A tier name invented here is one the server may reject outright, and
        // the operator would read the 400 as the session being refused rather than the picker
        // being wrong.
        setIso({ tiers: [], modes: [], default_tier: "", platform: "" });
        setIsoError(`Could not load the isolation options: ${String(e?.message ?? e)}`);
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [opening, iso, apiImpl]);

  const open = async () => {
    setBusy("new");
    await state.create({ isolation: mode, isolation_tier: tier, thinking: thinkNew });
    setBusy("");
    setOpening(false);
  };

  const toggleThinking = async () => {
    if (!state.sessionId) return;
    setBusy("thinking");
    try {
      await apiImpl.setThinking(state.sessionId, !detail?.thinking, apiOptions);
      onDetailChanged?.();
    } finally {
      setBusy("");
    }
  };

  const sendSteer = async () => {
    const message = steerRef.current?.value.trim() ?? "";
    if (!message || !state.sessionId) return;
    setSteerError("");
    setBusy("steer");
    try {
      await apiImpl.steer(state.sessionId, message, apiOptions);
      if (steerRef.current) steerRef.current.value = "";
    } catch (e: any) {
      setSteerError(String(e?.message ?? e));
    } finally {
      setBusy("");
    }
  };

  return (
    <aside className="session-panel" data-testid="session-panel">
      <div className="session-panel-head">
        <strong>Conversations</strong>
        <button onClick={() => setOpening((v) => !v)} data-testid="toggle-new-session">
          {opening ? "Cancel" : "New"}
        </button>
      </div>

      {opening && (
        <div className="new-session" data-testid="new-session-form">
          <label>
            Runs on
            <select value={mode} onChange={(e) => setMode(e.target.value)} data-testid="iso-mode">
              {(iso?.modes ?? []).map((m) => (
                <option key={m.mode} value={m.mode}>{m.mode}</option>
              ))}
            </select>
          </label>
          {/* Only meaningful for "cloud": on this_computer the desktop app runs the I/O and this
              server's sandbox tiers do not apply to it. */}
          {mode === "cloud" && (
            <label>
              Sandbox
              <select value={tier} onChange={(e) => setTier(e.target.value)} data-testid="iso-tier">
                {(iso?.tiers ?? []).map((t) => (
                  <option key={t.tier} value={t.tier} disabled={!t.available}
                          title={t.available ? t.provides : t.reason}>
                    {t.tier}{t.available ? "" : " — unavailable"}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="inline">
            <input type="checkbox" checked={thinkNew} data-testid="new-thinking"
                   onChange={(e) => setThinkNew(e.target.checked)} />
            <span>stream the model's reasoning</span>
          </label>
          <button onClick={() => void open()} disabled={busy === "new" || !!isoError}
                  data-testid="open-session">
            {busy === "new" ? "Opening…" : "Open conversation"}
          </button>
          {isoError && <p className="landing-error" data-testid="iso-error">{isoError}</p>}
        </div>
      )}

      <ul className="session-list" data-testid="session-list">
        {state.sessions.map((s) => (
          <li key={s.session_id} data-current={s.session_id === state.sessionId ? "yes" : "no"}>
            <button className="session-open" onClick={() => state.select(s.session_id)}
                    data-testid={`select-${s.session_id}`}>
              {s.title || "Untitled"}
              <span className="hint"> · {s.message_count ?? 0}</span>
            </button>
            <button className="session-del" title="delete this conversation"
                    onClick={() => void state.remove(s.session_id)}
                    data-testid={`delete-${s.session_id}`}>✕</button>
          </li>
        ))}
      </ul>
      {state.sessions.length === 0 && !state.loading && (
        <p className="hint" data-testid="no-sessions">No conversations yet.</p>
      )}
      {state.error && <p className="landing-error" data-testid="session-error">{state.error}</p>}

      {state.sessionId && (
        <div className="session-controls" data-testid="session-controls">
          <label className="inline">
            <input type="checkbox" checked={!!detail?.thinking} data-testid="thinking-toggle"
                   disabled={busy === "thinking"} onChange={() => void toggleThinking()} />
            <span>show reasoning</span>
          </label>

          {/* Three facts, never collapsed into one. `isolation_tier` is what this session asked
              for; `effective_isolation_tier` is what the last run_command actually executed under
              (null when none has, or when one was refused because the tier was unavailable); and
              `isolation_verified` is the exec-verified answer for the request, which is the only
              one that has really run bwrap. */}
          {detail?.isolation_mode && (
            <p className="iso-readout" data-testid="iso-readout">
              <span className="tier-badge">runs on: {detail.isolation_mode}</span>
              {detail.isolation_mode === "cloud" && (
                <>
                  <span className="tier-badge">requested: {detail.isolation_tier}</span>
                  <span className="tier-badge" data-effective={detail.effective_isolation_tier ?? "none"}>
                    last ran under: {detail.effective_isolation_tier ?? "nothing yet"}
                  </span>
                  {detail.isolation_verified && (
                    <span className="tier-badge" title={detail.isolation_verified.reason ?? ""}>
                      verified: {detail.isolation_verified.ok ? "yes" : "no"}
                    </span>
                  )}
                </>
              )}
            </p>
          )}

          <div className="steer" data-testid="steer">
            <input ref={steerRef} data-testid="steer-input" placeholder="Steer the run…"
                   onKeyDown={(e) => {
                     if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); void sendSteer(); }
                   }} />
            <button onClick={() => void sendSteer()} disabled={busy === "steer"} data-testid="steer-send">
              Steer
            </button>
          </div>
          {/* A steer is not a message: queued into a turn in flight, or it starts one so the note
              is acted on rather than waiting for whatever the operator types next. */}
          {steerError && <p className="landing-error" data-testid="steer-error">{steerError}</p>}
        </div>
      )}
    </aside>
  );
}
