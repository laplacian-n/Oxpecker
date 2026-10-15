// "This computer" isolation only works while something pulls the server's delegated jobs. The
// React client never did; only static/index.html did. These pin that it does now, and that it
// stops — both halves matter, because an executor left running keeps executing jobs for a
// conversation the operator has closed.
//
// Run first against a broken version: no stop on "cloud" (test 2), no stop on undefined (test 3),
// the mode captured once instead of followed (test 4), and a platform check instead of a method
// check (test 5).

import { afterEach, expect, test, vi } from "vitest";
import { render } from "@testing-library/react";
import { useClientExecutor } from "./useClientExecutor";
import { setApiKey, setBaseUrl } from "./session";

function Probe({ mode }: { mode?: string }) {
  useClientExecutor(mode);
  return null;
}

function shell() {
  const start = vi.fn(async () => undefined);
  const stop = vi.fn(async () => undefined);
  window.oxpecker = { startClientExecutor: start, stopClientExecutor: stop };
  return { start, stop };
}

afterEach(() => {
  delete window.oxpecker;
  try { localStorage.clear(); } catch { /* blocked storage — nothing to clear */ }
});

test("1. a this_computer session starts the executor with the server and key", async () => {
  // Read at the moment the mode changes, not captured earlier: the operator can paste a key after
  // the surface mounted.
  setBaseUrl("http://192.168.1.50:7777");
  setApiKey("k-123");
  const { start, stop } = shell();
  render(<Probe mode="this_computer" />);
  expect(start).toHaveBeenCalledWith("http://192.168.1.50:7777", "k-123");
  expect(stop).not.toHaveBeenCalled();
});

test("2. a cloud session stops it", async () => {
  const { start, stop } = shell();
  render(<Probe mode="cloud" />);
  expect(stop).toHaveBeenCalled();
  expect(start).not.toHaveBeenCalled();
});

test("3. no session (or detail not yet fetched) also stops it", async () => {
  // undefined is not "leave it as it was": an executor left pulling for a session that is gone
  // would keep running that conversation's jobs.
  const { start, stop } = shell();
  render(<Probe mode={undefined} />);
  expect(stop).toHaveBeenCalled();
  expect(start).not.toHaveBeenCalled();
});

test("4. switching conversations switches the executor with them", async () => {
  const { start, stop } = shell();
  const { rerender } = render(<Probe mode="cloud" />);
  expect(stop).toHaveBeenCalledTimes(1);
  rerender(<Probe mode="this_computer" />);
  expect(start).toHaveBeenCalledTimes(1);
  rerender(<Probe mode="cloud" />);
  expect(stop).toHaveBeenCalledTimes(2);
});

test("5. a browser tab, and a shell too old to have the bridge, do nothing instead of throwing", () => {
  // No bridge at all: a plain browser tab has nothing to delegate to, and the server reports
  // executor_connected: false on the tool result, which is the honest answer.
  expect(() => render(<Probe mode="this_computer" />)).not.toThrow();
  // The bridge present but without these methods — an installed shell from before they existed.
  // A platform check ("is this Electron") instead of a method check would call undefined here.
  window.oxpecker = { checkForUpdates: async () => ({ status: "current", version: "1", message: "" }) };
  expect(() => render(<Probe mode="this_computer" />)).not.toThrow();
});

test("6. the same mode twice does not restart it", () => {
  // The executor long-polls; tearing it down and starting it again on an unrelated re-render
  // would drop a job it was already waiting on.
  const { start } = shell();
  const { rerender } = render(<Probe mode="this_computer" />);
  rerender(<Probe mode="this_computer" />);
  expect(start).toHaveBeenCalledTimes(1);
});
