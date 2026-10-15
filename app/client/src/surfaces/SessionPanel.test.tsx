// The session sidebar. The behaviours worth pinning are the ones where being slightly wrong is
// invisible: auto-pick stealing an explicit selection, a delete leaving the surface on a session
// the server no longer has, a tier picker offering something the server will refuse, and an
// isolation readout that implies a sandbox nothing enforced.
//
// Each was run first against a broken version — see the comment on each test.

import { afterEach, expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SessionPanel, SessionDetail } from "./SessionPanel";
import { ChatSessionState } from "../useChatSession";

const ISO = {
  platform: "Linux",
  default_tier: "bubblewrap",
  modes: [{ mode: "cloud", note: "" }, { mode: "this_computer", note: "" }],
  tiers: [
    { tier: "direct", available: true, reason: "", provides: "no kernel isolation" },
    { tier: "bubblewrap", available: true, reason: "", provides: "namespaces + rlimits" },
    { tier: "wsl2", available: false, reason: "this host is Linux — use bubblewrap", provides: "" },
  ],
};

function state(over: Partial<ChatSessionState> = {}): ChatSessionState {
  return {
    sessionId: "s1",
    sessions: [
      { session_id: "s1", title: "scan the host", message_count: 4 },
      { session_id: "s2", title: "read the docs", message_count: 2 },
    ],
    error: "", loading: false,
    select: vi.fn(), create: vi.fn(async () => {}), remove: vi.fn(async () => {}),
    refresh: vi.fn(async () => {}),
    ...over,
  };
}

const DEFAULT_PROMPT = "You are the assistant. @@TOOL_LIST@@";

const stub = (over = {}) => ({
  isolation: vi.fn(async () => ISO),
  setThinking: vi.fn(async () => ({ thinking: true })),
  steer: vi.fn(async () => ({ queued: true })),
  defaultSystemPrompt: vi.fn(async () => ({ prompt: DEFAULT_PROMPT })),
  setStage: vi.fn(async (_id: string, stage: string) => ({ stage })),
  ...over,
});

afterEach(() => vi.restoreAllMocks());

test("1. the list marks which conversation is open", async () => {
  render(<SessionPanel state={state()} apiImpl={stub() as any} />);
  const items = screen.getByTestId("session-list").children;
  expect(items[0]).toHaveAttribute("data-current", "yes");
  expect(items[1]).toHaveAttribute("data-current", "no");
});

test("2. selecting and deleting go through the hook, by id", async () => {
  const s = state();
  render(<SessionPanel state={s} apiImpl={stub() as any} />);
  await userEvent.click(screen.getByTestId("select-s2"));
  expect(s.select).toHaveBeenCalledWith("s2");
  await userEvent.click(screen.getByTestId("delete-s1"));
  expect(s.remove).toHaveBeenCalledWith("s1");
});

test("3. the tier picker is the server's list, and an unavailable tier is shown disabled", async () => {
  // Hardcoding the tiers would offer one create_session rejects with a 400, which the operator
  // reads as the session being refused rather than the picker being wrong. Hiding an unavailable
  // tier instead of disabling it loses the probe's reason, which is the only thing that explains
  // why this host cannot provide it.
  render(<SessionPanel state={state()} apiImpl={stub() as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("iso-tier")).toBeInTheDocument());
  const options = [...screen.getByTestId("iso-tier").querySelectorAll("option")];
  expect(options.map((o) => o.value)).toEqual(["direct", "bubblewrap", "wsl2"]);
  expect(options[2]).toBeDisabled();
  expect(options[2]).toHaveAttribute("title", "this host is Linux — use bubblewrap");
  // The server's own default, not this file's idea of one.
  expect(screen.getByTestId("iso-tier")).toHaveValue("bubblewrap");
});

test("4. the sandbox picker disappears for this_computer, where this server's tiers do not apply", async () => {
  render(<SessionPanel state={state()} apiImpl={stub() as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("iso-tier")).toBeInTheDocument());
  await userEvent.selectOptions(screen.getByTestId("iso-mode"), "this_computer");
  expect(screen.queryByTestId("iso-tier")).toBeNull();
});

test("5. opening a conversation passes the chosen mode, tier and thinking", async () => {
  const s = state();
  render(<SessionPanel state={s} apiImpl={stub() as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("iso-tier")).toBeInTheDocument());
  await userEvent.selectOptions(screen.getByTestId("iso-tier"), "direct");
  await userEvent.click(screen.getByTestId("new-thinking"));
  await userEvent.click(screen.getByTestId("open-session"));
  await waitFor(() => expect(s.create).toHaveBeenCalledWith({
    isolation: "cloud", isolation_tier: "direct", thinking: true,
  }));
});

test("6. if the isolation options cannot be loaded, nothing is guessed and Open is blocked", async () => {
  const impl = stub({ isolation: vi.fn(async () => { throw new Error("404"); }) });
  const s = state();
  render(<SessionPanel state={s} apiImpl={impl as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("iso-error")).toHaveTextContent("isolation options"));
  expect(screen.getByTestId("open-session")).toBeDisabled();
  expect(s.create).not.toHaveBeenCalled();
});

test("7. the isolation readout keeps the request, the outcome and the verification apart", async () => {
  // A UI that showed only isolation_tier would claim a sandbox that may never have run: the
  // request and the outcome differ whenever a tier was unavailable and the command was refused.
  const detail: SessionDetail = {
    isolation_mode: "cloud", isolation_tier: "bubblewrap",
    effective_isolation_tier: "direct",
    isolation_verified: { ok: false, reason: "bwrap could not create a user namespace" },
  };
  render(<SessionPanel state={state()} detail={detail} apiImpl={stub() as any} />);
  const out = screen.getByTestId("iso-readout");
  expect(out).toHaveTextContent("requested: bubblewrap");
  expect(out).toHaveTextContent("last ran under: direct");
  expect(out).toHaveTextContent("verified: no");
});

test("8. nothing having run yet is said, not implied to be the requested tier", async () => {
  const detail: SessionDetail = {
    isolation_mode: "cloud", isolation_tier: "bubblewrap", effective_isolation_tier: null,
  };
  render(<SessionPanel state={state()} detail={detail} apiImpl={stub() as any} />);
  expect(screen.getByTestId("iso-readout")).toHaveTextContent("last ran under: nothing yet");
});

test("9. the thinking toggle reflects the session and refreshes it after flipping", async () => {
  // Thinking lives on the server, so two windows on one session agree about it. A local-only
  // checkbox would drift from what the server will actually stream.
  const impl = stub();
  const onDetailChanged = vi.fn();
  render(<SessionPanel state={state()} detail={{ thinking: true }} apiImpl={impl as any}
                       onDetailChanged={onDetailChanged} />);
  expect(screen.getByTestId("thinking-toggle")).toBeChecked();
  await userEvent.click(screen.getByTestId("thinking-toggle"));
  await waitFor(() => expect(impl.setThinking).toHaveBeenCalledWith("s1", false, {}));
  expect(onDetailChanged).toHaveBeenCalled();
});

test("10. a steer is sent, clears the box, and a failure says so without clearing it", async () => {
  const impl = stub();
  render(<SessionPanel state={state()} detail={{ thinking: false }} apiImpl={impl as any} />);
  const box = screen.getByTestId("steer-input") as HTMLInputElement;
  await userEvent.type(box, "check the admin panel too");
  await userEvent.click(screen.getByTestId("steer-send"));
  await waitFor(() => expect(impl.steer).toHaveBeenCalledWith("s1", "check the admin panel too", {}));
  expect(box.value).toBe("");

  const failing = stub({ steer: vi.fn(async () => { throw new Error("session not found (404)"); }) });
  render(<SessionPanel state={state()} detail={{ thinking: false }} apiImpl={failing as any} />);
  const boxes = screen.getAllByTestId("steer-input") as HTMLInputElement[];
  await userEvent.type(boxes[1], "again");
  await userEvent.click(screen.getAllByTestId("steer-send")[1]);
  await waitFor(() => expect(screen.getByTestId("steer-error")).toHaveTextContent("404"));
  expect(boxes[1].value).toBe("again");
});

test("11. an empty steer is not sent", async () => {
  const impl = stub();
  render(<SessionPanel state={state()} detail={{}} apiImpl={impl as any} />);
  await userEvent.type(screen.getByTestId("steer-input"), "   ");
  await userEvent.click(screen.getByTestId("steer-send"));
  expect(impl.steer).not.toHaveBeenCalled();
});

test("12. with no session open there are no per-session controls to mislead with", async () => {
  render(<SessionPanel state={state({ sessionId: null, sessions: [] })} apiImpl={stub() as any} />);
  expect(screen.queryByTestId("session-controls")).toBeNull();
  expect(screen.getByTestId("no-sessions")).toBeInTheDocument();
});

test("13. the new-session prompt starts from the server default, and an untouched default is not sent", async () => {
  // Sending the default back would freeze a copy into the session, so a later change to the
  // default would never reach it. Only an actual edit is sent.
  const impl = stub();
  const s = state();
  render(<SessionPanel state={s} apiImpl={impl as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("new-system-prompt")).toHaveValue(DEFAULT_PROMPT));
  expect(impl.defaultSystemPrompt).toHaveBeenCalledWith({});
  await waitFor(() => expect(screen.getByTestId("iso-tier")).toBeInTheDocument());
  await userEvent.click(screen.getByTestId("open-session"));
  await waitFor(() => expect(s.create).toHaveBeenCalled());
  const sent = (s.create as any).mock.calls[0][0];
  expect(sent).not.toHaveProperty("system_prompt");
});

test("14. an edited prompt is passed to create; a blank one sends nothing", async () => {
  const s = state();
  render(<SessionPanel state={s} apiImpl={stub() as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  const box = await screen.findByTestId("new-system-prompt");
  await waitFor(() => expect(box).toHaveValue(DEFAULT_PROMPT));
  await userEvent.clear(box);
  await userEvent.type(box, "  Only report what you verified.  ");
  await userEvent.click(screen.getByTestId("open-session"));
  await waitFor(() => expect(s.create).toHaveBeenLastCalledWith(expect.objectContaining({
    system_prompt: "Only report what you verified.",
  })));

  // Opening closes the form once create settles, so wait for that before reopening it; the edited
  // text is still there.
  await waitFor(() => expect(screen.queryByTestId("new-session-form")).toBeNull());
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(screen.getByTestId("new-system-prompt")).toHaveValue("  Only report what you verified.  "));
  await userEvent.clear(screen.getByTestId("new-system-prompt"));
  await userEvent.click(screen.getByTestId("open-session"));
  await waitFor(() => expect(s.create).toHaveBeenCalledTimes(2));
  expect((s.create as any).mock.calls[1][0]).not.toHaveProperty("system_prompt");
});

test("15. if the default prompt cannot be loaded the box is blank and Open still works", async () => {
  const impl = stub({ defaultSystemPrompt: vi.fn(async () => { throw new Error("500"); }) });
  const s = state();
  render(<SessionPanel state={s} apiImpl={impl as any} />);
  await userEvent.click(screen.getByTestId("toggle-new-session"));
  await waitFor(() => expect(impl.defaultSystemPrompt).toHaveBeenCalled());
  expect(screen.getByTestId("new-system-prompt")).toHaveValue("");
  await waitFor(() => expect(screen.getByTestId("iso-tier")).toBeInTheDocument());
  await userEvent.click(screen.getByTestId("open-session"));
  await waitFor(() => expect(s.create).toHaveBeenCalled());
  expect((s.create as any).mock.calls[0][0]).not.toHaveProperty("system_prompt");
});

test("16. the stage selector sets the stage on this session and then refreshes the detail", async () => {
  const impl = stub();
  const onDetailChanged = vi.fn();
  render(<SessionPanel state={state()} detail={{ stage: "RECON" }} apiImpl={impl as any}
                       onDetailChanged={onDetailChanged} />);
  expect(screen.getByTestId("stage-select")).toHaveValue("RECON");
  expect([...screen.getByTestId("stage-select").querySelectorAll("option")].map((o) => o.value))
    .toEqual(["RECON", "ANALYSIS", "VALIDATION", "REPORT"]);
  await userEvent.selectOptions(screen.getByTestId("stage-select"), "VALIDATION");
  await waitFor(() => expect(impl.setStage).toHaveBeenCalledWith("s1", "VALIDATION", {}));
  expect(onDetailChanged).toHaveBeenCalled();
});

test("17. no stage selector until the session's detail says what stage it is in", async () => {
  render(<SessionPanel state={state()} detail={{ thinking: false }} apiImpl={stub() as any} />);
  expect(screen.queryByTestId("stage-select")).toBeNull();
});
