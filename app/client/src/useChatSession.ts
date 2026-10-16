import { useCallback, useEffect, useState } from "react";
import { api, ApiOptions } from "./api";

// Caps the auto-open effect below at one created session per engagement for the life of the app
// load. That effect creates a session when the engagement has none; if the server's session list
// ever comes back without this engagement's sessions — a server running code old enough that the
// list row omits engagement_id (so the client filter matches nothing), a transient empty response,
// a races'd refetch — the effect would re-run on every mount and create a fresh empty session each
// time. That is the "Untitled session ×6" pile-up. Recording the engagements already auto-created
// for means a missing-from-list session is never mistaken for "none exist, make another".
const autoCreatedEngagements = new Set<string>();

// Which conversation the chat surface is showing, and the rest of this engagement's conversations.
//
// A message goes to a SESSION, not to an engagement, and a session belongs to an engagement
// (Session.engagement_id). The first slice of this surface auto-picked the engagement's most
// recent one and stopped there; this adds the list, explicit selection, opening a new one and
// removing one — the sidebar the classic UI had and /app/ did not.
//
// Auto-pick runs only while nothing is selected. A list refresh after a delete, or after the
// operator opens a new session, must not drag the surface back to "most recently spoken in"
// underneath them.
//
// Every failure resolves to an error string rather than a throw: this hook runs inside the
// surface that also carries the autonomous run controls, and those must keep working against a
// server whose session API is unreachable.

export interface SessionRow {
  session_id: string;
  engagement_id?: string;
  title?: string;
  message_count?: number;
  last_modified?: number;
}

export interface NewSessionOptions {
  isolation?: string;
  isolation_tier?: string;
  thinking?: boolean;
  system_prompt?: string;
}

export interface ChatSessionState {
  sessionId: string | null;
  sessions: SessionRow[];
  error: string;
  loading: boolean;
  select: (sessionId: string) => void;
  create: (opts?: NewSessionOptions) => Promise<void>;
  remove: (sessionId: string) => Promise<void>;
  refresh: () => Promise<void>;
}

type SessionApi = Pick<typeof api, "sessions" | "createSession" | "deleteSession">;

export function useChatSession(
  engagementId: string,
  apiOptions: ApiOptions = {},
  apiImpl: SessionApi = api,
): ChatSessionState {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const mine = useCallback(async (): Promise<SessionRow[]> => {
    const all = await apiImpl.sessions(apiOptions);
    const rows = (Array.isArray(all) ? all : []).filter(
      (s: SessionRow) => s.engagement_id === engagementId,
    );
    // Most recently spoken in first. The server sorts by creation time, which is a different
    // order the moment two sessions exist and the older one is the live one.
    rows.sort((a, b) => (b.last_modified ?? 0) - (a.last_modified ?? 0));
    return rows;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engagementId, apiImpl]);

  const refresh = useCallback(async () => {
    try {
      setSessions(await mine());
      setError("");
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
  }, [mine]);

  // Open the engagement's most recent conversation, or its first one. Runs once per engagement:
  // `live` guards the engagement switching mid-flight, where the slower response would otherwise
  // win and leave the surface on another engagement's conversation.
  useEffect(() => {
    if (!engagementId) {
      setSessionId(null);
      setSessions([]);
      setLoading(false);
      return;
    }
    let live = true;
    (async () => {
      setSessionId(null);
      setSessions([]);
      setError("");
      setLoading(true);
      try {
        const rows = await mine();
        if (!live) return;
        setSessions(rows);
        if (rows.length) {
          setSessionId(rows[0].session_id);
          return;
        }
        if (autoCreatedEngagements.has(engagementId)) {
          // Already auto-created one for this engagement this app load and the list is not showing
          // it (see autoCreatedEngagements). Do not spawn another — leave the surface without a
          // selection rather than pile up empty sessions; the operator can open one with "New".
          return;
        }
        // Mark before the await, not after: two effect runs in the same tick (a dep identity change
        // landing before the request resolves) must not both pass the guard and each create one.
        autoCreatedEngagements.add(engagementId);
        const created = await apiImpl.createSession({ engagement_id: engagementId }, apiOptions);
        if (!live) return;
        setSessionId(created.session_id);
        setSessions(await mine());
      } catch (e: any) {
        if (!live) return;
        setError(String(e?.message ?? e));
      } finally {
        if (live) setLoading(false);
      }
    })();
    return () => { live = false; };
    // apiOptions is a fresh literal on most renders; depending on it would reopen a session per
    // render. The key and base URL come from per-viewer storage and do not change while mounted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engagementId, mine, apiImpl]);

  const create = useCallback(async (opts: NewSessionOptions = {}) => {
    setError("");
    try {
      const created = await apiImpl.createSession(
        { engagement_id: engagementId, ...opts }, apiOptions,
      );
      // An explicit open also satisfies the auto-open guard: this engagement now has a session, so
      // a later remount against a list that does not show it must not auto-create a second one.
      autoCreatedEngagements.add(engagementId);
      // Selected before the list is refetched: the operator asked for this conversation, and
      // waiting on a second round trip to show it reads as the button having done nothing.
      setSessionId(created.session_id);
      setSessions(await mine());
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [engagementId, mine, apiImpl]);

  const remove = useCallback(async (id: string) => {
    setError("");
    try {
      await apiImpl.deleteSession(id, apiOptions);
      const rows = await mine();
      setSessions(rows);
      // Only move if the deleted one was open. Falling back to the newest remaining conversation
      // beats leaving the surface pointed at a session the server no longer has, which would
      // fail its next snapshot fetch with a 404 and show an empty transcript.
      setSessionId((current) => (current === id ? (rows[0]?.session_id ?? null) : current));
    } catch (e: any) {
      setError(String(e?.message ?? e));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mine, apiImpl]);

  return { sessionId, sessions, error, loading, select: setSessionId, create, remove, refresh };
}
