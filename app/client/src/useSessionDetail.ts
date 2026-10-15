import { useCallback, useEffect, useState } from "react";
import { api, ApiOptions } from "./api";
import { SessionDetail } from "./surfaces/SessionPanel";

// A session's settings and isolation facts, which the event stream does not carry: thinking,
// which mode it runs in, the tier it asked for, the tier its last command actually ran under, and
// whether that request was exec-verified.
//
// Refetched on demand rather than watched. These change when the operator changes them (thinking)
// or when a command runs (effective tier), and neither is worth a second stream — the caller
// refreshes after the action it took, which is also the only moment the value can have moved from
// this window's point of view.
export function useSessionDetail(
  sessionId: string | null,
  apiOptions: ApiOptions = {},
  apiImpl: Pick<typeof api, "session"> = api,
): { detail: SessionDetail | undefined; refresh: () => void } {
  const [detail, setDetail] = useState<SessionDetail | undefined>(undefined);
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    if (!sessionId) { setDetail(undefined); return; }
    let live = true;
    (async () => {
      try {
        const d = await apiImpl.session(sessionId, apiOptions);
        if (live) setDetail(d);
      } catch {
        // The panel renders what it has; a failed settings fetch must not take down the
        // conversation beside it, which has its own data path.
        if (live) setDetail(undefined);
      }
    })();
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, epoch, apiImpl]);

  return { detail, refresh: useCallback(() => setEpoch((e) => e + 1), []) };
}
