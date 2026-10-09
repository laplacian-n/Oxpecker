// §7 approvals rendering. The panel reads pending approvals from the projection and resolves them
// over REST; because it reads the shared state rather than its own, an approval_resolved event
// folds the row out — so the same decision clears every rendering. Both the full (drawer/chat)
// and compact (flow node) forms are pinned, and that clicking Approve sends the decision to the
// resolve endpoint by request id.

import { act } from "react";
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { emptyProjection, applyEvent, Projection } from "../projection";

afterEach(cleanup);

function withApprovals(...events: { kind: string; payload: Record<string, unknown> }[]): Projection {
  let p = emptyProjection();
  events.forEach((e, i) => (p = applyEvent(p, { seq: i + 1, ts: 0, kind: e.kind, payload: e.payload })));
  return p;
}

test("the full panel lists pending approvals and hides resolved ones", () => {
  const p = withApprovals(
    { kind: "approval_required", payload: { request_id: "r1", tool: "http_request", argument_digest: "abc" } },
    { kind: "approval_required", payload: { request_id: "r2", tool: "port_discovery" } },
    { kind: "approval_resolved", payload: { request_id: "r2", status: "approved" } },
  );
  const screen = render(<ApprovalsPanel projection={p} />);
  expect(screen.getByTestId("approve-r1")).toBeInTheDocument();
  expect(screen.queryByTestId("approve-r2")).toBeNull(); // resolved -> gone
});

test("the compact flow-node form shows the count of what is blocked", () => {
  const none = render(<ApprovalsPanel projection={emptyProjection()} compact />);
  expect(none.getByTestId("approvals-badge").textContent).toBe("");
  cleanup();
  const p = withApprovals({ kind: "approval_required", payload: { request_id: "r1", tool: "x" } });
  const some = render(<ApprovalsPanel projection={p} compact />);
  expect(some.getByTestId("approvals-badge").textContent).toContain("1 awaiting approval");
});

test("Approve sends the decision to the resolve endpoint keyed by request id", async () => {
  const p = withApprovals({ kind: "approval_required", payload: { request_id: "r1", tool: "http_request" } });
  const calls: string[] = [];
  const fetchImpl = vi.fn(async (url: string, init?: any) => {
    calls.push(`${url} ${init?.body}`);
    return { ok: true, status: 200, json: async () => ({ status: "approved" }) };
  }) as unknown as typeof fetch;

  const screen = render(<ApprovalsPanel projection={p} apiOptions={{ fetchImpl }} />);
  await act(async () => {
    fireEvent.click(screen.getByTestId("approve-r1"));
  });
  await waitFor(() => expect(calls.length).toBe(1));
  expect(calls[0]).toContain("/api/approvals/r1/resolve");
  expect(calls[0]).toContain('"approved":true');
});
