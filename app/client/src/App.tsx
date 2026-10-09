import { useEffect, useMemo } from "react";
import { HashRouter, Routes, Route, Navigate, NavLink, useParams } from "react-router-dom";
import { EngagementSubscription } from "./subscription";
import { useEngagement } from "./useEngagement";
import { surfacesForTier, SurfaceName } from "./tier/surfacing";
import { Chat } from "./surfaces/Chat";
import { FlowView } from "./surfaces/FlowView";
import { Work } from "./surfaces/Work";
import { WorkerRail } from "./surfaces/WorkerRail";
import { Placeholder } from "./surfaces/Placeholder";

// HashRouter, not BrowserRouter: the built client is loaded from a file:// URL by the Electron
// shell and served as a static bundle by the Python server, neither of which does server-side
// route resolution. A tear-off opens the same hash route in a new window — and because no window
// owns state (§3), that new window is an equal subscriber that rebuilds its projection from a
// snapshot, so a window opened now and one opened three hours ago show identical content.

function tearOff() {
  window.open(`${window.location.pathname}${window.location.hash}`, "_blank", "width=900,height=700");
}

// One subscription per (window, engagement). Started on mount, closed on unmount.
function useRouteSubscription(engagementId: string) {
  const subscription = useMemo(() => new EngagementSubscription({ engagementId }), [engagementId]);
  useEffect(() => {
    void subscription.start();
    return () => subscription.close();
  }, [subscription]);
  return subscription;
}

// The nav offers exactly the surfaces this engagement's tier has (§8). A surface the tier hides is
// still listed — disabled, with the reason — because a hidden surface that says why reads as a
// property of the tier, not a broken build.
function TierNav({ subscription, engagementId }: { subscription: EngagementSubscription; engagementId: string }) {
  const projection = useEngagement(subscription);
  const avail = Object.fromEntries(surfacesForTier(projection.tier).map((s) => [s.name, s]));
  const entries: { label: string; route: string; gate: SurfaceName }[] = [
    { label: "chat", route: "chat", gate: "chat" },
    { label: "work", route: "work", gate: "notebook" }, // notebook/findings keep work alive in low
    { label: "flow", route: "flow", gate: "flow" },
    { label: "worker", route: "worker/-", gate: "worker" },
    { label: "browser", route: "browser", gate: "browser" },
  ];
  return (
    <nav>
      <span className="tier-badge" data-testid="tier-badge">
        tier: {projection.tier}
      </span>
      {entries.map((e) => {
        const s = avail[e.gate];
        if (!s.available) {
          return (
            <span key={e.label} data-testid={`nav-${e.label}-hidden`} className="nav-hidden" title={s.reason}>
              {e.label} — {s.reason}
            </span>
          );
        }
        return (
          <NavLink key={e.label} to={`/${engagementId}/${e.route}`} data-testid={`nav-${e.label}`}>
            {e.label}
          </NavLink>
        );
      })}
      <button onClick={tearOff}>Tear off ⧉</button>
    </nav>
  );
}

// A surface route gated by the tier: renders the surface if the tier has it, otherwise the §8
// "say why" panel — so arriving by a stale link or a tear-off lands on an explanation, not an
// empty screen.
function Gated({
  subscription,
  gate,
  children,
}: {
  subscription: EngagementSubscription;
  gate: SurfaceName;
  children: React.ReactNode;
}) {
  const projection = useEngagement(subscription);
  const s = surfacesForTier(projection.tier).find((x) => x.name === gate)!;
  if (!s.available) {
    return (
      <div className="surface" data-surface="unavailable" data-testid="surface-unavailable">
        <p>{s.reason}</p>
      </div>
    );
  }
  return <>{children}</>;
}

function Surface({ gate, render }: { gate: SurfaceName; render: (s: EngagementSubscription) => React.ReactNode }) {
  const { engagementId = "" } = useParams();
  const subscription = useRouteSubscription(engagementId);
  return (
    <div className="app">
      <TierNav subscription={subscription} engagementId={engagementId} />
      <main>
        <Gated subscription={subscription} gate={gate}>
          {render(subscription)}
        </Gated>
      </main>
    </div>
  );
}

function WorkerSurface() {
  const { engagementId = "", workerId } = useParams();
  const subscription = useRouteSubscription(engagementId);
  return (
    <div className="app">
      <TierNav subscription={subscription} engagementId={engagementId} />
      <main>
        <Gated subscription={subscription} gate="worker">
          <WorkerRail subscription={subscription} initialWorkerId={workerId === "-" ? undefined : workerId} />
        </Gated>
      </main>
    </div>
  );
}

function WorkRouteInner({ subscription }: { subscription: EngagementSubscription }) {
  const { engagementId = "" } = useParams();
  return <Work subscription={subscription} engagementId={engagementId} />;
}

export function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/:engagementId/chat" element={<Surface gate="chat" render={(s) => <Chat subscription={s} />} />} />
        <Route path="/:engagementId/work" element={<Surface gate="notebook" render={(s) => <WorkRouteInner subscription={s} />} />} />
        <Route path="/:engagementId/flow" element={<Surface gate="flow" render={(s) => <FlowView subscription={s} />} />} />
        <Route path="/:engagementId/browser" element={<Surface gate="browser" render={(s) => <Placeholder name="Browser" subscription={s} />} />} />
        <Route path="/:engagementId/worker/:workerId" element={<WorkerSurface />} />
        <Route path="*" element={<Navigate to="/lab-default/chat" replace />} />
      </Routes>
    </HashRouter>
  );
}
