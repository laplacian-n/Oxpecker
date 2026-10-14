import { useRef, useState } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";

// Browser — the agent's own browser (CLIENT_UI_DESIGN.md §6.4): a tab strip, an address bar, and a
// page. The line that must stay sharp, because both happen in the same window:
//
//   * a person types a URL / signs in  -> NOT brokered. A human action in the human's own hands;
//     this is how you "hand the agent this account."
//   * the agent navigates / clicks / submits -> ALWAYS brokered, scope-checked and audited
//     server-side.
//
// This renders the human side. The operator browses an in-scope target in the page frame and signs
// in where the agent will later act. The page is an <iframe> here so the surface works both in the
// served client and in the desktop shell; a frame-busting site (X-Frame-Options / CSP) cannot be
// embedded, so a one-click "open in a new window" is offered for those. The agent's own
// navigations, with their own cookie jar per engagement (an Electron BrowserView partition) and
// the broker on every step, are the follow-up this shell is the frame for.

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

export function Browser({
  subscription,
  engagementId = "",
}: {
  subscription: EngagementSubscription;
  engagementId?: string;
}) {
  useEngagement(subscription); // keep the surface a live subscriber (tear-off/resume parity)
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
    </div>
  );
}
