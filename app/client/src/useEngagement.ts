import { useSyncExternalStore } from "react";
import { EngagementSubscription } from "./subscription";
import { Projection } from "./projection";

// Bind a component to a subscription's projection. `useSyncExternalStore` is exactly the right
// primitive here: the subscription is the external store, the projection is its immutable
// snapshot, and React handles consistent reads under a flood of updates. A component re-renders
// when the projection reference changes — and never writes into it, because no window owns state
// (§3); the subscription is the single source and the component is one of its equal subscribers.
export function useEngagement(subscription: EngagementSubscription): Projection {
  return useSyncExternalStore(
    (onChange) => subscription.subscribe(onChange),
    () => subscription.getProjection(),
  );
}
