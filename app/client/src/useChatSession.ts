import { useEffect, useState } from "react";
import { api, ApiOptions } from "./api";

// Which conversation this engagement's chat surface is showing.
//
// A message cannot be sent to an engagement — it goes to a SESSION, and a session belongs to an
// engagement (Session.engagement_id). The React client had no concept of one, so this is the
// smallest thing that makes the chat usable: reuse the engagement's most recent conversation, or
// open the first one. A session list, with new/delete and a picker, is the next slice; what this
// must not do is silently drop the operator into a different engagement's history.
//
// Every failure resolves to an error string rather than a throw. This hook runs inside the chat
// surface, which also carries the autonomous run controls — those must keep working against a
// server whose session API is unreachable, so a failure here degrades the conversation only.

export interface ChatSessionState {
  sessionId: string | null;
  error: string;
  loading: boolean;
}

export function useChatSession(
  engagementId: string,
  apiOptions: ApiOptions = {},
  apiImpl: Pick<typeof api, "sessions" | "createSession"> = api,
): ChatSessionState {
  const [state, setState] = useState<ChatSessionState>({ sessionId: null, error: "", loading: true });

  useEffect(() => {
    if (!engagementId) {
      setState({ sessionId: null, error: "", loading: false });
      return;
    }
    // Guards the engagement switching while a request is in flight: without it the slower
    // response wins and the surface ends up showing another engagement's conversation.
    let live = true;
    (async () => {
      setState({ sessionId: null, error: "", loading: true });
      try {
        const all = await apiImpl.sessions(apiOptions);
        if (!live) return;
        const mine = (Array.isArray(all) ? all : []).filter((s) => s.engagement_id === engagementId);
        // Most recently touched first. The server already sorts by creation time, but the
        // conversation the operator wants is the one most recently SPOKEN in, which is a
        // different order the moment two sessions exist.
        mine.sort((a, b) => (b.last_modified ?? 0) - (a.last_modified ?? 0));
        if (mine.length) {
          setState({ sessionId: mine[0].session_id, error: "", loading: false });
          return;
        }
        const created = await apiImpl.createSession({ engagement_id: engagementId }, apiOptions);
        if (!live) return;
        setState({ sessionId: created.session_id, error: "", loading: false });
      } catch (e: any) {
        if (!live) return;
        setState({ sessionId: null, error: String(e?.message ?? e), loading: false });
      }
    })();
    return () => { live = false; };
    // apiOptions is a fresh object literal on most renders; depending on it would re-run this on
    // every parent render and open a session per render. The key and base URL come from
    // per-viewer storage and do not change while the surface is mounted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engagementId, apiImpl]);

  return state;
}
