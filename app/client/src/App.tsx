import { useEffect, useMemo } from "react";
import { HashRouter, Routes, Route, Navigate, NavLink, useParams } from "react-router-dom";
import { EngagementSubscription } from "./subscription";
import { Chat } from "./surfaces/Chat";
import { FlowView } from "./surfaces/FlowView";
import { Placeholder } from "./surfaces/Placeholder";
import { Work } from "./surfaces/Work";

// HashRouter, not BrowserRouter: the built client is loaded from a file:// URL by the Electron
// shell and served as a static bundle by the Python server, neither of which does server-side
// route resolution. A tear-off opens the same hash route in a new window — and because no window
// owns state (§3), that new window is an equal subscriber that rebuilds its projection from a
// snapshot, so a window opened now and one opened three hours ago show identical content.

function tearOff(route: string) {
  window.open(`${window.location.pathname}#${route}`, "_blank", "width=900,height=700");
}

function SurfaceFrame({ children }: { children: React.ReactNode }) {
  const { engagementId } = useParams();
  const surfaces = ["chat", "work", "flow", "browser"];
  return (
    <div className="app">
      <nav>
        {surfaces.map((s) => (
          <NavLink key={s} to={`/${engagementId}/${s}`}>
            {s}
          </NavLink>
        ))}
        <button onClick={() => tearOff(window.location.hash.slice(1))}>Tear off ⧉</button>
      </nav>
      <main>{children}</main>
    </div>
  );
}

// One subscription per (window, engagement). Created from the route's engagementId, started on
// mount, closed on unmount.
function useRouteSubscription() {
  const { engagementId } = useParams();
  const subscription = useMemo(
    () => new EngagementSubscription({ engagementId: engagementId ?? "" }),
    [engagementId],
  );
  useEffect(() => {
    void subscription.start();
    return () => subscription.close();
  }, [subscription]);
  return subscription;
}

function ChatRoute() {
  return <SurfaceFrame><Chat subscription={useRouteSubscription()} /></SurfaceFrame>;
}
function FlowRoute() {
  return <SurfaceFrame><FlowView subscription={useRouteSubscription()} /></SurfaceFrame>;
}
function WorkRoute() {
  const { engagementId } = useParams();
  return (
    <SurfaceFrame>
      <Work subscription={useRouteSubscription()} engagementId={engagementId ?? ""} />
    </SurfaceFrame>
  );
}
function BrowserRoute() {
  return <SurfaceFrame><Placeholder name="Browser" subscription={useRouteSubscription()} /></SurfaceFrame>;
}
function WorkerRoute() {
  return <SurfaceFrame><Placeholder name="Worker" subscription={useRouteSubscription()} /></SurfaceFrame>;
}

export function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/:engagementId/chat" element={<ChatRoute />} />
        <Route path="/:engagementId/work" element={<WorkRoute />} />
        <Route path="/:engagementId/flow" element={<FlowRoute />} />
        <Route path="/:engagementId/browser" element={<BrowserRoute />} />
        <Route path="/:engagementId/worker/:workerId" element={<WorkerRoute />} />
        {/* No engagement selected yet: the skeleton points at the lab default so the dev build
            renders something; a real engagement picker is its own surface. */}
        <Route path="*" element={<Navigate to="/lab-default/chat" replace />} />
      </Routes>
    </HashRouter>
  );
}
