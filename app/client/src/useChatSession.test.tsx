// The session hook. Three behaviours here are the kind that look fine in a browser and are wrong:
// auto-pick running again and stealing an explicit selection, a delete leaving the surface
// pointed at a session the server no longer has, and a slow response for a previous engagement
// winning the race after the operator switched.

import { expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useChatSession, SessionRow } from "./useChatSession";

function Harness({ engagementId, apiImpl }: { engagementId: string; apiImpl: any }) {
  const s = useChatSession(engagementId, {}, apiImpl);
  return (
    <div>
      <span data-testid="current">{s.sessionId ?? "none"}</span>
      <span data-testid="count">{s.sessions.length}</span>
      <span data-testid="error">{s.error}</span>
      {s.sessions.map((r) => (
        <button key={r.session_id} data-testid={`pick-${r.session_id}`} onClick={() => s.select(r.session_id)} />
      ))}
      <button data-testid="new" onClick={() => void s.create({ isolation_tier: "direct" })} />
      {s.sessions.map((r) => (
        <button key={`d-${r.session_id}`} data-testid={`rm-${r.session_id}`} onClick={() => void s.remove(r.session_id)} />
      ))}
    </div>
  );
}

/** A fake server holding rows, so create/delete are observable through the list the hook reads. */
function fakeApi(initial: SessionRow[]) {
  let rows = [...initial];
  let n = initial.length;
  return {
    rows: () => rows,
    sessions: vi.fn(async () => [...rows]),
    createSession: vi.fn(async (body: any) => {
      n += 1;
      const row = { session_id: `new${n}`, engagement_id: body.engagement_id, last_modified: 0, title: "", message_count: 0 };
      rows = [...rows, row];
      return { session_id: row.session_id };
    }),
    deleteSession: vi.fn(async (id: string) => {
      rows = rows.filter((r) => r.session_id !== id);
      return { deleted: true };
    }),
  };
}

const row = (id: string, ts: number): SessionRow =>
  ({ session_id: id, engagement_id: "eng1", last_modified: ts, title: id, message_count: 1 });

test("1. opens the most recently spoken-in conversation, not the oldest", async () => {
  // The server sorts by creation time, which is a different order the moment the older session is
  // the live one. The fixture therefore hands them over in the WRONG order on purpose: the first
  // version listed them already-sorted, so deleting the sort passed this test and the mutation
  // went uncaught.
  const impl = fakeApi([row("newer-but-idle", 100), row("older-but-live", 900)]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("older-but-live"));
});

test("2. an engagement with none gets one opened for it", async () => {
  const impl = fakeApi([]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("new1"));
  expect(impl.createSession).toHaveBeenCalledWith({ engagement_id: "eng1" }, {});
  // The list is refetched, or the sidebar shows nothing right after opening one.
  await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
});

test("3. only this engagement's conversations are listed", async () => {
  // Without the filter the sidebar shows every conversation in the install under whichever
  // engagement happens to be open, and selecting one silently crosses engagements.
  const impl = fakeApi([row("mine", 2), { ...row("theirs", 3), engagement_id: "other" }]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
  expect(screen.getByTestId("current")).toHaveTextContent("mine");
});

test("4. an explicit selection is not taken back by auto-pick", async () => {
  // Auto-pick must run only while nothing is selected. A version that re-ran it on every list
  // change dragged the surface back to the most recent conversation after a delete or a refresh.
  const impl = fakeApi([row("a", 300), row("b", 200)]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("a"));
  await userEvent.click(screen.getByTestId("pick-b"));
  expect(screen.getByTestId("current")).toHaveTextContent("b");
  // Force a list change: open a new conversation, which refetches.
  await userEvent.click(screen.getByTestId("new"));
  await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("3"));
  // The newly opened one is selected because the operator asked for it — and "a" is not.
  expect(screen.getByTestId("current")).toHaveTextContent("new3");
});

test("5. deleting the open conversation moves to another; deleting a different one does not", async () => {
  // Leaving the surface on a deleted session means its next snapshot fetch 404s and the transcript
  // renders empty with no explanation.
  const impl = fakeApi([row("a", 300), row("b", 200)]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("a"));
  await userEvent.click(screen.getByTestId("rm-a"));
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("b"));

  // Three rows and an explicit selection away from the newest, so "stay put" and "move to the
  // newest" are distinguishable. With only two rows, deleting the one that is not open leaves the
  // newest remaining row equal to the open one, and a version that re-picked unconditionally
  // passed — which is how it first did.
  const impl2 = fakeApi([row("x", 300), row("mid", 200), row("z", 100)]);
  render(<Harness engagementId="eng1" apiImpl={impl2} />);
  await waitFor(() => expect(screen.getAllByTestId("current")[1]).toHaveTextContent("x"));
  await userEvent.click(screen.getAllByTestId("pick-z")[0]);
  expect(screen.getAllByTestId("current")[1]).toHaveTextContent("z");
  await userEvent.click(screen.getAllByTestId("rm-mid")[0]);
  await waitFor(() => expect(impl2.deleteSession).toHaveBeenCalledWith("mid", {}));
  // Still on z. A re-pick would land on x, the newest remaining.
  expect(screen.getAllByTestId("current")[1]).toHaveTextContent("z");
});

test("6. deleting the last conversation leaves none selected rather than a stale id", async () => {
  const impl = fakeApi([row("only", 1)]);
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("only"));
  await userEvent.click(screen.getByTestId("rm-only"));
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("none"));
});

test("7. a failure is reported and does not throw out of the surface", async () => {
  // The autonomous run controls live on the same surface and must survive a session API that is
  // unreachable.
  const impl = { ...fakeApi([]), sessions: vi.fn(async () => { throw new Error("502"); }) };
  render(<Harness engagementId="eng1" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("error")).toHaveTextContent("502"));
  expect(screen.getByTestId("current")).toHaveTextContent("none");
});

test("8. no engagement means no session and no request", async () => {
  const impl = fakeApi([row("a", 1)]);
  render(<Harness engagementId="" apiImpl={impl} />);
  await waitFor(() => expect(screen.getByTestId("current")).toHaveTextContent("none"));
  expect(impl.sessions).not.toHaveBeenCalled();
});
