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

// ── Agent (live) side ─────────────────────────────────────────────────────────
import { waitFor } from "@testing-library/react";
import { beforeAll } from "vitest";

beforeAll(() => {
  // jsdom has no object-URL support; the live view makes one per frame.
  (URL as any).createObjectURL = (URL as any).createObjectURL || (() => "blob:mock");
  (URL as any).revokeObjectURL = (URL as any).revokeObjectURL || (() => {});
});

// A fetch stub for the browser REST calls: start/stop/navigate return ok JSON; the FIRST frame poll
// returns a JPEG with X-Frame-Seq:1, and everything after is 204 (nothing newer) so the loop idles.
function browserFetch(record?: { calls: string[] }) {
  let frames = 0;
  return (async (url: string, init?: any) => {
    const u = String(url);
    record?.calls.push(`${init?.method ?? "GET"} ${u.split("?")[0]}`);
    if (u.includes("/browser/frame")) {
      frames += 1;
      if (frames === 1) {
        return {
          ok: true, status: 200,
          headers: { get: (h: string) => (h === "X-Frame-Seq" ? "1" : null) },
          blob: async () => new Blob(["jpegbytes"], { type: "image/jpeg" }),
        };
      }
      return { ok: true, status: 204, headers: { get: () => null } };
    }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  }) as any;
}

test("switching to the agent tab shows Start and an empty screen, no frame", () => {
  const { getByTestId, queryByTestId } = render(<Browser subscription={sub()} engagementId="eng-x" />);
  fireEvent.click(getByTestId("browser-mode-agent"));
  expect(getByTestId("agent-browser-start")).toBeInTheDocument();
  expect(getByTestId("agent-browser-empty")).toBeInTheDocument();
  expect(queryByTestId("agent-browser-frame")).toBeNull();
});

test("Start streams the agent browser screen as a frame", async () => {
  const rec = { calls: [] as string[] };
  const { getByTestId } = render(
    <Browser subscription={sub()} engagementId="eng-x" apiOptions={{ fetchImpl: browserFetch(rec) }} />,
  );
  fireEvent.click(getByTestId("browser-mode-agent"));
  fireEvent.click(getByTestId("agent-browser-start"));
  await waitFor(() => expect(getByTestId("agent-browser-frame")).toBeInTheDocument());
  expect((getByTestId("agent-browser-frame") as HTMLImageElement).getAttribute("src")).toMatch(/^blob:/);
  expect(rec.calls).toContain("POST /api/engagements/eng-x/browser/start");
});

test("Navigate posts the url to the agent browser", async () => {
  const rec = { calls: [] as string[] };
  const { getByTestId } = render(
    <Browser subscription={sub()} engagementId="eng-x" apiOptions={{ fetchImpl: browserFetch(rec) }} />,
  );
  fireEvent.click(getByTestId("browser-mode-agent"));
  fireEvent.click(getByTestId("agent-browser-start"));
  await waitFor(() => expect(getByTestId("agent-browser-frame")).toBeInTheDocument());
  fireEvent.change(getByTestId("agent-browser-address"), { target: { value: "shop.example/login" } });
  fireEvent.submit(getByTestId("agent-browser-address").closest("form")!);
  await waitFor(() => expect(rec.calls).toContain("POST /api/engagements/eng-x/browser/navigate"));
});
