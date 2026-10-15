// The engagement form writes an authorisation record the broker enforces, so the tests that
// matter are about what it sends and what it refuses to invent.
//
// Each was run first against a broken version: pre-ticking every class (test 1), pre-ticking a
// guessed default set when the fetch fails (test 7), sending untrimmed lines and blanks (test 3),
// sending a valid_hours the operator never typed (test 4), swallowing the server's reason or
// closing the form on a refusal (test 5).

import { afterEach, expect, test, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EngagementForm } from "./EngagementForm";
import { api } from "../api";

const CLASSES = [
  { action_class: "passive_recon", tools: ["http_recon"], default: true, note: "read responses" },
  { action_class: "browser_recon", tools: ["browser_click", "browser_navigate"], default: true, note: "drive the live browser" },
  { action_class: "knowledge_fetch", tools: ["knowledge_fetch"], default: false, note: "fetch a URL from the open internet" },
  { action_class: "http_recon_insecure", tools: [], default: false, note: "skip TLS verification" },
];

function stub(over: Partial<{ actionClasses: any; createEngagement: any }> = {}) {
  return {
    actionClasses: vi.fn(async () => CLASSES),
    createEngagement: vi.fn(async (body: any) => ({ engagement_id: body.engagement_id })),
    ...over,
  };
}

async function open(impl = stub(), onCreated = vi.fn()) {
  render(<EngagementForm apiImpl={impl as any} onCreated={onCreated} />);
  await userEvent.click(screen.getByTestId("new-engagement"));
  await waitFor(() => expect(screen.getByTestId("class-passive_recon")).toBeInTheDocument());
  return { impl, onCreated };
}

afterEach(() => vi.restoreAllMocks());

test("1. only the server's defaults start ticked", async () => {
  // The whole reason the list is fetched. Ticking everything would grant real internet egress
  // (knowledge_fetch) and disable TLS verification (http_recon_insecure) on every engagement an
  // operator created by pressing Create without reading.
  await open();
  expect(screen.getByTestId("class-passive_recon")).toBeChecked();
  expect(screen.getByTestId("class-browser_recon")).toBeChecked();
  expect(screen.getByTestId("class-knowledge_fetch")).not.toBeChecked();
  expect(screen.getByTestId("class-http_recon_insecure")).not.toBeChecked();
});

test("2. a class that is off still says what it is and why", async () => {
  // Before this form, knowledge_fetch had no presence in any UI at all: it was absent from both
  // hardcoded lists, so the only way to learn it existed was a broker refusal at call time.
  await open();
  const fieldset = screen.getByTestId("eng-classes");
  expect(fieldset).toHaveTextContent("knowledge_fetch");
  expect(fieldset).toHaveTextContent("fetch a URL from the open internet");
  expect(fieldset).toHaveTextContent("skip TLS verification");
});

test("3. targets go one per line, trimmed, with blank lines dropped", async () => {
  const { impl } = await open();
  await userEvent.type(screen.getByTestId("eng-id"), "acme-staging");
  await userEvent.type(screen.getByTestId("eng-allow"), "  staging.acme.com  \n\n10.0.0.0/24\n");
  await userEvent.type(screen.getByTestId("eng-deny"), "admin.acme.com\n");
  await userEvent.type(screen.getByTestId("eng-by"), "  the client  ");
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(impl.createEngagement).toHaveBeenCalled());
  const body = impl.createEngagement.mock.calls[0][0];
  expect(body.allow_targets).toEqual(["staging.acme.com", "10.0.0.0/24"]);
  expect(body.deny_targets).toEqual(["admin.acme.com"]);
  // An untrimmed authorised-by lands in the audit record and in roe.json.
  expect(body.authorized_by).toBe("the client");
  expect(body.allowed_action_classes.sort()).toEqual(["browser_recon", "passive_recon"]);
});

test("4. valid_hours is sent only when typed, so the server's default stands otherwise", async () => {
  // Two mounts, not two submits: a successful create closes the form, which is the behaviour
  // test 8 pins. The first version of this test submitted twice and failed on its own setup.
  const fill = async () => {
    await userEvent.type(screen.getByTestId("eng-id"), "a");
    await userEvent.type(screen.getByTestId("eng-allow"), "x.example.com");
    await userEvent.type(screen.getByTestId("eng-by"), "me");
  };

  const blank = stub();
  const { unmount } = render(<EngagementForm apiImpl={blank as any} onCreated={vi.fn()} />);
  await userEvent.click(screen.getByTestId("new-engagement"));
  await waitFor(() => expect(screen.getByTestId("class-passive_recon")).toBeInTheDocument());
  await fill();
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(blank.createEngagement).toHaveBeenCalled());
  expect(blank.createEngagement.mock.calls[0][0]).not.toHaveProperty("valid_hours");
  unmount();

  const typed = stub();
  render(<EngagementForm apiImpl={typed as any} onCreated={vi.fn()} />);
  await userEvent.click(screen.getByTestId("new-engagement"));
  await waitFor(() => expect(screen.getByTestId("class-passive_recon")).toBeInTheDocument());
  await fill();
  await userEvent.type(screen.getByTestId("eng-hours"), "8");
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(typed.createEngagement).toHaveBeenCalled());
  expect(typed.createEngagement.mock.calls[0][0].valid_hours).toBe(8);
});

test("5. the server's refusal is shown verbatim and the form stays open", async () => {
  // The validator reports every problem at once. Replacing that with a generic failure would send
  // the operator back to guessing which field it meant — and closing the form would lose what
  // they had typed.
  const impl = stub({
    createEngagement: vi.fn(async () => {
      throw new Error("/api/engagements failed (400): allow_targets must have at least one entry; authorized_by is required");
    }),
  });
  await open(impl);
  await userEvent.type(screen.getByTestId("eng-id"), "a");
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(screen.getByTestId("eng-error")).toHaveTextContent("at least one entry"));
  expect(screen.getByTestId("eng-error")).toHaveTextContent("authorized_by is required");
  expect(screen.getByTestId("engagement-form")).toBeInTheDocument();
});

test("6. granting a non-default class sends it", async () => {
  const { impl } = await open();
  await userEvent.type(screen.getByTestId("eng-id"), "a");
  await userEvent.type(screen.getByTestId("eng-allow"), "x.example.com");
  await userEvent.type(screen.getByTestId("eng-by"), "me");
  await userEvent.click(screen.getByTestId("class-knowledge_fetch"));
  await userEvent.click(screen.getByTestId("class-passive_recon")); // and take one away
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(impl.createEngagement).toHaveBeenCalled());
  expect(impl.createEngagement.mock.calls[0][0].allowed_action_classes.sort())
    .toEqual(["browser_recon", "knowledge_fetch"]);
});

test("7. if the class list cannot be loaded, nothing is pre-ticked and it says so", async () => {
  // Falling back to a list compiled into this file is exactly the drift the endpoint exists to
  // end: it would quietly grant whatever this client last believed the defaults were.
  const impl = stub({ actionClasses: vi.fn(async () => { throw new Error("404"); }) });
  render(<EngagementForm apiImpl={impl as any} onCreated={vi.fn()} />);
  await userEvent.click(screen.getByTestId("new-engagement"));
  await waitFor(() => expect(screen.getByTestId("eng-error")).toHaveTextContent("action classes"));
  expect(screen.queryByTestId("class-passive_recon")).toBeNull();
});

test("8. a created engagement is handed back by the id the server returned", async () => {
  // The server is free to normalise it; the caller reloads its list and may navigate there, so it
  // has to be the server's answer and not what was typed.
  const impl = stub({ createEngagement: vi.fn(async () => ({ engagement_id: "acme-staging" })) });
  const onCreated = vi.fn();
  await open(impl, onCreated);
  await userEvent.type(screen.getByTestId("eng-id"), "ACME Staging");
  await userEvent.type(screen.getByTestId("eng-allow"), "x.example.com");
  await userEvent.type(screen.getByTestId("eng-by"), "me");
  await userEvent.click(screen.getByTestId("create-engagement"));
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith("acme-staging"));
  expect(screen.queryByTestId("engagement-form")).toBeNull();
});

test("9. the real api surfaces the server's `detail`, keeping the status for callers that match on it", async () => {
  // Landing tells 401 from 409 by matching "(401)" in the message, so the detail is appended
  // rather than replacing it. Goes through the real postJson, which is the code under test.
  const fetchImpl = (async () => ({
    ok: false, status: 400,
    json: async () => ({ detail: "allow_targets entry 'http://x' rejected: scheme not allowed" }),
  })) as unknown as typeof fetch;
  await expect(
    api.createEngagement({ engagement_id: "a", allow_targets: ["http://x"], authorized_by: "me" }, { fetchImpl }),
  ).rejects.toThrow(/\(400\).*scheme not allowed/);
});
