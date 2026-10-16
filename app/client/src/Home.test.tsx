// The front door's routing. The operator wanted the app to open into a chat, not onto the picker,
// so Home decides where "/" lands. The four cases below are the ones that change what they see:
// a remembered engagement, a default when none is remembered, the picker when there is no key, and
// the picker when the install has no engagements yet.
import { expect, test, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const sessionMock = vi.hoisted(() => ({ key: "k", last: "" }));
vi.mock("./session", () => ({
  getApiKey: () => sessionMock.key,
  getLastEngagement: () => sessionMock.last,
  setLastEngagement: () => {},
  sessionOptions: () => ({}),
}));
const apiMock = vi.hoisted(() => ({ engagements: vi.fn() }));
vi.mock("./api", () => ({ api: apiMock }));

import { Home } from "./App";

function renderHome() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/engagements" element={<div data-testid="picker" />} />
        <Route path="/:id/chat" element={<div data-testid="chat">{location.hash}</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  sessionMock.key = "k";
  sessionMock.last = "";
  apiMock.engagements.mockReset();
});

test("opens the last engagement the operator had open", async () => {
  sessionMock.last = "laplacian-test-1";
  apiMock.engagements.mockResolvedValue([{ engagement_id: "lab-default" }, { engagement_id: "laplacian-test-1" }]);
  const { container } = renderHome();
  await waitFor(() => expect(screen.getByTestId("chat")).toBeInTheDocument());
  // Routed to that engagement's chat, not the picker.
  expect(container.querySelector('[data-testid="picker"]')).toBeNull();
});

test("falls back to lab-default when nothing is remembered", async () => {
  apiMock.engagements.mockResolvedValue([{ engagement_id: "other" }, { engagement_id: "lab-default" }]);
  renderHome();
  await waitFor(() => expect(screen.getByTestId("chat")).toBeInTheDocument());
});

test("no key sends the operator to the picker to connect", async () => {
  sessionMock.key = "";
  renderHome();
  await waitFor(() => expect(screen.getByTestId("picker")).toBeInTheDocument());
  expect(apiMock.engagements).not.toHaveBeenCalled();
});

test("no engagements yet sends them to the picker to create one", async () => {
  apiMock.engagements.mockResolvedValue([]);
  renderHome();
  await waitFor(() => expect(screen.getByTestId("picker")).toBeInTheDocument());
});

test("an unreachable or rejecting server falls back to the picker, not a blank screen", async () => {
  apiMock.engagements.mockRejectedValue(new Error("(401)"));
  renderHome();
  await waitFor(() => expect(screen.getByTestId("picker")).toBeInTheDocument());
});
