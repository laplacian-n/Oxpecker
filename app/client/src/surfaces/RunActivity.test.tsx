// The run narration. The mapping from an engagement event to a readable line is the whole point,
// so it is asserted directly (narrate), plus the two rendering rules that keep it useful: noise is
// dropped, and an unknown kind is shown rather than silently swallowed.
import { expect, test } from "vitest";
import { render, screen } from "@testing-library/react";
import { RunActivity, narrate } from "./RunActivity";
import { EngagementEvent } from "../projection";

const ev = (kind: string, payload: Record<string, unknown> = {}, seq = 1): EngagementEvent =>
  ({ seq, kind, payload } as EngagementEvent);

test("named events become a readable, specific line", () => {
  expect(narrate(ev("phase_entered", { phase: "RECON" }))!.text).toBe("Entered RECON");
  expect(narrate(ev("worker_spawned", { worker_id: "w0", task: "verify missing CSP" }))!.text)
    .toBe("Spawned worker w0: verify missing CSP");
  expect(narrate(ev("worker_finished", { worker_id: "w0", detail: "no CSP header" }))!.text)
    .toBe("Worker w0 finished: no CSP header");
  expect(narrate(ev("tool_call_started", { worker: "w0", tool: "http_recon" }))!.text)
    .toBe("w0 → http_recon");
  expect(narrate(ev("finding_recorded", { detail: "Missing HSTS" }))!.text).toBe("Missing HSTS");
  expect(narrate(ev("run_complete", { detail: "closed out" }))!.text).toBe("closed out");
});

test("noise is dropped, so the narration is a commentary and not the raw log", () => {
  for (const k of ["tool_call_finished", "graph_node_changed", "model_call", "note_added"]) {
    expect(narrate(ev(k))).toBeNull();
  }
});

test("an unknown kind is still shown, humanised, rather than vanishing", () => {
  const line = narrate(ev("some_new_event", { detail: "hi" }));
  expect(line).not.toBeNull();
  expect(line!.text).toBe("Some new event — hi");
});

test("the list renders the narrated lines and skips the dropped ones", () => {
  const events = [
    ev("phase_entered", { phase: "RECON" }, 1),
    ev("tool_call_finished", { tool: "x" }, 2), // dropped
    ev("worker_spawned", { worker_id: "w0" }, 3),
  ];
  render(<RunActivity events={events} />);
  const rows = screen.getByTestId("run-activity").querySelectorAll(".activity-row");
  expect(rows).toHaveLength(2);
  expect(screen.getByTestId("run-activity")).toHaveTextContent("Entered RECON");
  expect(screen.getByTestId("run-activity")).toHaveTextContent("Spawned worker w0");
});

test("no events renders nothing (a plain chat has no run narration)", () => {
  const { container } = render(<RunActivity events={[]} />);
  expect(container.querySelector(".run-activity")).toBeNull();
});
