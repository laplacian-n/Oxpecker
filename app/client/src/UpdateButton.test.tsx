// The update button renders only where there is an installed app to update, and turns whatever
// verdict the shell hands back into readable text — including the stuck/failed paths, which are the
// ones that leave a user staring at "Checking…" if they regress.
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { UpdateButton } from "./UpdateButton";
import type { UpdateVerdict } from "./shell";

afterEach(() => {
  cleanup();
  delete (window as any).oxpecker;
});

function withShell(checkForUpdates?: () => Promise<UpdateVerdict>) {
  (window as any).oxpecker = checkForUpdates ? { checkForUpdates } : {};
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

test("renders nothing in a browser tab (no window.oxpecker at all)", () => {
  const { queryByTestId } = render(<UpdateButton />);
  expect(queryByTestId("update-button")).toBeNull();
});

test("renders nothing on an old shell that has no checkForUpdates method", () => {
  withShell(); // window.oxpecker exists (isolation bridge) but no update method
  const { queryByTestId } = render(<UpdateButton />);
  expect(queryByTestId("update-button")).toBeNull();
});

test("renders the button when the shell exposes checkForUpdates", () => {
  withShell(vi.fn());
  const { getByTestId } = render(<UpdateButton />);
  expect(getByTestId("update-check").textContent).toBe("Check for updates");
});

test("shows Checking… while the check is in flight, then the verdict", async () => {
  const d = deferred<UpdateVerdict>();
  withShell(() => d.promise);
  const { getByTestId } = render(<UpdateButton />);

  fireEvent.click(getByTestId("update-check"));
  expect(getByTestId("update-check").textContent).toBe("Checking…");
  expect(getByTestId("update-check")).toBeDisabled();

  d.resolve({ status: "available", version: "0.1.9", message: "Update available: 0.1.9." });
  await waitFor(() => expect(getByTestId("update-status").textContent).toMatch(/0\.1\.9/));
  expect(getByTestId("update-check").textContent).toBe("Check for updates");
  expect(getByTestId("update-status").className).toMatch(/update-available/);
});

test("reports an up-to-date verdict", async () => {
  withShell(async () => ({ status: "current", message: "You're on the latest version (0.1.8)." }));
  const { getByTestId } = render(<UpdateButton />);
  fireEvent.click(getByTestId("update-check"));
  await waitFor(() => expect(getByTestId("update-status").textContent).toMatch(/latest version/));
});

test("reports the unsupported verdict (unpackaged shell)", async () => {
  withShell(async () => ({ status: "unsupported", message: "Update checks are only available in the installed app." }));
  const { getByTestId } = render(<UpdateButton />);
  fireEvent.click(getByTestId("update-check"));
  await waitFor(() => expect(getByTestId("update-status").textContent).toMatch(/installed app/));
});

test("a thrown bridge error lands as a readable error, not a stuck button", async () => {
  withShell(() => Promise.reject(new Error("ipc channel closed")));
  const { getByTestId } = render(<UpdateButton />);
  fireEvent.click(getByTestId("update-check"));
  await waitFor(() => expect(getByTestId("update-status").textContent).toMatch(/ipc channel closed/));
  expect(getByTestId("update-check").textContent).toBe("Check for updates"); // recovered, clickable
  expect(getByTestId("update-status").className).toMatch(/update-error/);
});

test("a second click while a check is in flight does not fire a second check", async () => {
  const d = deferred<UpdateVerdict>();
  const check = vi.fn(() => d.promise);
  withShell(check);
  const { getByTestId } = render(<UpdateButton />);
  fireEvent.click(getByTestId("update-check"));
  fireEvent.click(getByTestId("update-check")); // disabled + guarded
  expect(check).toHaveBeenCalledTimes(1);
  d.resolve({ status: "current", message: "latest" });
  await waitFor(() => expect(getByTestId("update-status")).toBeInTheDocument());
});
