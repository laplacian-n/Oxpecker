// The Browser surface (§6.4): the human side of the agent's browser — address bar, tabs, page
// frame, and the sharp human-vs-agent broker line. Pinned for structure and navigation; the live
// agent-navigation + BrowserView partition is the desktop follow-up.
import { render, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { Browser } from "./Browser";
import { EngagementSubscription } from "../subscription";

afterEach(cleanup);

function sub(): EngagementSubscription {
  return new EngagementSubscription({
    engagementId: "eng-x",
    fetchImpl: (async () => ({ ok: true, json: async () => ({ at_seq: 0, latest_seq: 0 }) })) as any,
    eventSourceFactory: () => ({ addEventListener() {}, close() {}, onerror: null }),
  });
}

test("typing a URL loads it in the page frame, adding the scheme", () => {
  const { getByTestId } = render(<Browser subscription={sub()} engagementId="eng-x" />);
  fireEvent.change(getByTestId("browser-address"), { target: { value: "target.example/login" } });
  fireEvent.click(getByTestId("browser-go"));
  const frame = getByTestId("browser-frame") as HTMLIFrameElement;
  expect(frame.getAttribute("src")).toBe("https://target.example/login");
});

test("states the human-vs-agent broker line", () => {
  const { getByTestId } = render(<Browser subscription={sub()} engagementId="eng-x" />);
  expect(getByTestId("browser-note").textContent).toMatch(/not brokered/i);
  expect(getByTestId("browser-note").textContent).toMatch(/agent'?s later navigation always is/i);
});

test("a new tab opens empty and can be switched to", () => {
  const { getByTestId, getAllByRole } = render(<Browser subscription={sub()} engagementId="eng-x" />);
  fireEvent.click(getByTestId("browser-new-tab"));
  expect(getAllByRole("tab").length).toBe(2);
  expect(getByTestId("browser-empty")).toBeInTheDocument();
});

test("starts with an empty state, no frame", () => {
  const { getByTestId, queryByTestId } = render(<Browser subscription={sub()} engagementId="eng-x" />);
  expect(getByTestId("browser-empty")).toBeInTheDocument();
  expect(queryByTestId("browser-frame")).toBeNull();
});
