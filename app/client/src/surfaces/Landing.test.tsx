// The landing: the client's entry point (key + engagement picker). Pinned because without it the
// client could only open the hard-coded lab-default keylessly — the "client not complete" gap.
import { render, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Landing } from "./Landing";
import { getApiKey } from "../session";

afterEach(cleanup);
beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
});

function renderLanding(apiImpl: any) {
  return render(
    <MemoryRouter>
      <Landing apiImpl={apiImpl} />
    </MemoryRouter>,
  );
}

test("lists the engagements the key can open, each a link into its chat", async () => {
  const api = { engagements: vi.fn().mockResolvedValue([
    { engagement_id: "co-lap", description: "the live target" },
    { engagement_id: "lab-default", description: "" },
  ]) };
  const { getByTestId } = renderLanding(api);
  await waitFor(() => getByTestId("engagement-list"));
  const link = getByTestId("open-co-lap") as HTMLAnchorElement;
  expect(link.getAttribute("href")).toContain("/co-lap/chat");
  expect(getByTestId("open-lab-default")).toBeInTheDocument();
});

test("a 401 is shown as a key problem, not a dead screen", async () => {
  const api = { engagements: vi.fn().mockRejectedValue(new Error("/api/engagements failed (401)")) };
  const { getByTestId } = renderLanding(api);
  await waitFor(() => getByTestId("landing-error"));
  expect(getByTestId("landing-error").textContent).toMatch(/rejected that key/i);
});

test("an empty list says so rather than looking broken", async () => {
  const api = { engagements: vi.fn().mockResolvedValue([]) };
  const { getByTestId } = renderLanding(api);
  await waitFor(() => getByTestId("landing-empty"));
});

test("Connect persists the typed key and refetches with it", async () => {
  const api = { engagements: vi.fn().mockResolvedValue([]) };
  const { getByTestId } = renderLanding(api);
  await waitFor(() => getByTestId("landing-empty"));
  fireEvent.change(getByTestId("key-input"), { target: { value: "s3cret" } });
  fireEvent.click(getByTestId("connect"));
  await waitFor(() => expect(getApiKey()).toBe("s3cret"));
  // the refetch used the stored key
  expect(api.engagements).toHaveBeenLastCalledWith(expect.objectContaining({ apiKey: "s3cret" }));
});
