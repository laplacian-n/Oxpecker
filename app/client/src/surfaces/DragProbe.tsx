// DragProbe — the §2.1 rule-2 guard for "a drag is never dropped under load". It stands in for
// the flow canvas in tests: jsdom cannot drive React Flow's pointer interaction, and React Flow
// keeps the drag transform out of React state by design (§5), so this minimal node — position in a
// ref, written straight to the DOM — is a faithful, conservative proxy for the same rule.
import { useLayoutEffect, useRef } from "react";
import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";

// Flow — the architecture in motion (CLIENT_UI_DESIGN.md §6.3). The real surface is React Flow
// (step 6); this skeleton carries one draggable node, enough to assert §2.1 rule 2 against: the
// node's position is held in a ref and written straight to the DOM transform, NOT through
// application state. The engagement event flood re-renders this component (the feed grows), but a
// re-render re-applies the ref's position imperatively instead of fighting the drag — so the
// pointer is never dropped mid-gesture and the node ends where it was released, while events
// stream the whole time. When React Flow lands this test re-targets its canvas; the rule it
// guards is the same.
export function DragProbe({ subscription }: { subscription: EngagementSubscription }) {
  const projection = useEngagement(subscription);
  const nodeRef = useRef<HTMLDivElement>(null);
  const pos = useRef({ x: 20, y: 20 });
  const dragging = useRef<{ dx: number; dy: number } | null>(null);

  // Apply the ref position to the DOM on every render (including flood-driven ones), so a
  // re-render never resets an in-flight drag.
  useLayoutEffect(() => {
    if (nodeRef.current) {
      nodeRef.current.style.transform = `translate(${pos.current.x}px, ${pos.current.y}px)`;
    }
  });

  return (
    <div className="surface" data-surface="flow">
      <header>
        <h1>Flow</h1>
        <span data-testid="event-count">{projection.events.length}</span>
      </header>
      <div
        className="canvas"
        style={{ position: "relative", width: 400, height: 300 }}
        onPointerMove={(e) => {
          if (!dragging.current) return;
          pos.current = { x: e.clientX - dragging.current.dx, y: e.clientY - dragging.current.dy };
          // Straight to the DOM (ref), not to state — this is the gesture, which fires far faster
          // than ~10/s and must not go through application state (§2.1 rule 2).
          if (nodeRef.current) {
            nodeRef.current.style.transform = `translate(${pos.current.x}px, ${pos.current.y}px)`;
          }
        }}
        onPointerUp={() => {
          dragging.current = null;
        }}
      >
        <div
          ref={nodeRef}
          data-testid="flow-node"
          style={{ position: "absolute", width: 80, height: 40, touchAction: "none" }}
          onPointerDown={(e) => {
            dragging.current = { dx: e.clientX - pos.current.x, dy: e.clientY - pos.current.y };
            (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
          }}
        >
          node
        </div>
      </div>
    </div>
  );
}
