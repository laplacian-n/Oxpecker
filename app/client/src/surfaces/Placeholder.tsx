import { EngagementSubscription } from "../subscription";
import { useEngagement } from "../useEngagement";

// Work / Browser / Worker surfaces are steps 4–7. The skeleton gives each a real route and a real
// subscriber (so tear-off parity and resume are exercised uniformly across every surface), and a
// stated reason where it is empty — a surface that says why it is bare reads as a property of the
// build stage, not as a broken screen (the §8 "hidden surfaces say why" principle applied to a
// not-yet-built one).
export function Placeholder({ name, subscription }: { name: string; subscription: EngagementSubscription }) {
  const projection = useEngagement(subscription);
  return (
    <div className="surface" data-surface={name.toLowerCase()}>
      <header>
        <h1>{name}</h1>
        <span data-testid="event-count">{projection.events.length}</span>
      </header>
      <p>This surface is not built yet (CLIENT_UI_DESIGN.md §12). It subscribes to the engagement
        stream like every other, so its tear-off and resume behave identically once it has content.</p>
    </div>
  );
}
