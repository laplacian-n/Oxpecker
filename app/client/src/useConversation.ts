import { useSyncExternalStore } from "react";
import { ConversationSubscription } from "./conversationStream";
import { Conversation } from "./conversation";

// Bind a component to a session subscription, the same way useEngagement binds one to the
// engagement subscription: the subscription is the external store, the conversation is its
// immutable snapshot, and React handles consistent reads while tokens stream in. A component
// re-renders when the reference changes and never writes into it.
export function useConversation(subscription: ConversationSubscription): Conversation {
  return useSyncExternalStore(
    (onChange) => subscription.subscribe(onChange),
    () => subscription.getConversation(),
  );
}
