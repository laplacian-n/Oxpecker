import { useEffect } from "react";
import { getApiKey, getBaseUrl } from "./session";

// "This computer" isolation: tell the desktop shell to start pulling this server's delegated tool
// jobs, and to stop when the open session no longer wants them.
//
// This is not optional plumbing. A session created with isolation "this_computer" has its already
// authorised tool I/O handed to the desktop app (dev_server._delegate_to_client), which blocks on
// _CLIENT_JOBS.submit until an executor answers. If nothing is pulling, every such tool call
// fails with "no executor connected" — the server offers the work and no one takes it.
//
// Only static/index.html did this (syncClientExecutor), so the React client could display a
// this_computer session perfectly and never make one work. That gap is why this exists before the
// default UI moves to /app/: flipping the default without it would have broken the feature
// silently, in the one configuration whose whole point is running from the operator's own network
// position.
//
// window.oxpecker exists only inside the Electron shell (electron/preload.js). In a browser tab
// there is nothing to delegate to, so this is a no-op and the server reports
// "executor_connected: false" on the tool result, which is the honest answer.

export function useClientExecutor(isolationMode: string | undefined): void {
  useEffect(() => {
    const shell = typeof window === "undefined" ? undefined : window.oxpecker;
    if (!shell?.startClientExecutor || !shell?.stopClientExecutor) return;
    // The key and base URL come from per-viewer storage, read at the moment the mode changes
    // rather than captured earlier: the operator can paste a key after the surface mounted.
    if (isolationMode === "this_computer") {
      void shell.startClientExecutor(getBaseUrl() || window.location.origin, getApiKey());
    } else {
      // Includes "cloud" AND undefined (no session open, or its detail has not arrived). Stopping
      // on undefined is deliberate: an executor left pulling for a session that is gone would
      // keep running jobs from a conversation the operator has closed.
      void shell.stopClientExecutor();
    }
  }, [isolationMode]);
}
