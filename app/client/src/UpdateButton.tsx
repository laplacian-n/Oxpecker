import { useState } from "react";
import { updateChecker, UpdateVerdict } from "./shell";

// A "Check for updates" button, shown only where there is something to check: the desktop shell
// that exposes window.oxpecker.checkForUpdates. In a browser tab, or on an old shell with no
// updater, updateChecker() is null and this renders nothing — a tab cannot update the installed
// app, so there is no action and nothing to explain (contrast the tier-hidden surfaces, which say
// why they are absent).
//
// The verdict text comes straight from the shell (electron/main.js checkForUpdatesNow), which is
// the one place that knows whether the app is packaged, whether a newer version exists, and what
// went wrong — the button only renders what it is handed.
export function UpdateButton() {
  const check = updateChecker();
  const [checking, setChecking] = useState(false);
  const [verdict, setVerdict] = useState<UpdateVerdict | null>(null);

  if (!check) return null;

  const onClick = async () => {
    if (checking) return; // a second click while the HTTP check is in flight is a no-op
    setChecking(true);
    setVerdict(null);
    try {
      setVerdict(await check());
    } catch (e: any) {
      // The shell is meant to resolve a verdict rather than throw, but a bridge error must still
      // land as a readable result, not a button stuck on "Checking…".
      setVerdict({ status: "error", message: `Update check failed: ${String(e?.message ?? e)}` });
    } finally {
      setChecking(false);
    }
  };

  return (
    <span className="update-button" data-testid="update-button">
      <button type="button" onClick={onClick} disabled={checking} data-testid="update-check">
        {checking ? "Checking…" : "Check for updates"}
      </button>
      {verdict && (
        <span
          className={`update-status update-${verdict.status}`}
          data-testid="update-status"
          role="status"
        >
          {verdict.message}
        </span>
      )}
    </span>
  );
}
