// The desktop shell bridge, as the client sees it.
//
// The Electron shell exposes a tiny `window.oxpecker` object (electron/preload.js). The client
// runs in three places that each expose a DIFFERENT amount of it:
//   * a browser tab served by the Python backend — no `window.oxpecker` at all;
//   * an OLD desktop shell (0.1.7) — `window.oxpecker` exists for "this computer" isolation but has
//     NO update methods;
//   * the current desktop shell — `window.oxpecker.checkForUpdates` / `.appVersion` present.
//
// So capability is detected per-METHOD, never per-platform: "is `window.oxpecker` here" is not the
// question, "is THIS call here" is. The machine most in need of an update is the one running the
// old shell, and it is precisely the one whose `window.oxpecker` exists but lacks the method.

export type UpdateStatus = "available" | "current" | "unsupported" | "error";

export interface UpdateVerdict {
  status: UpdateStatus;
  version?: string;
  message: string;
}

interface OxpeckerShell {
  // Present only in the current desktop shell.
  checkForUpdates?: () => Promise<UpdateVerdict>;
  appVersion?: () => Promise<string>;
  // The "this computer" isolation bridge (older shells have these too).
  startClientExecutor?: (serverBase: string, apiKey: string) => Promise<unknown>;
  stopClientExecutor?: () => Promise<unknown>;
}

declare global {
  interface Window {
    oxpecker?: OxpeckerShell;
  }
}

// The update-check call IF the running shell actually provides it, else null. A browser tab and an
// old shell both return null — there is nothing to check, nothing to render. Callers render nothing
// on null rather than a disabled button (a tab cannot replace an installer, so there is no action
// to explain — unlike a tier-hidden surface, which says why).
export function updateChecker(): (() => Promise<UpdateVerdict>) | null {
  const shell = window.oxpecker;
  const fn = shell?.checkForUpdates;
  return typeof fn === "function" ? () => fn.call(shell) : null;
}
