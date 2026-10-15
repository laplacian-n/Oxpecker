// The two session calls this file gained: the server default is read as a template, and a stage
// change is a command. Both go through the real request helpers, so the test sees the URL and the
// body that actually leave the client.

import { expect, test } from "vitest";
import { api } from "./api";

function recorder(body: unknown = {}) {
  const calls: { url: string; init?: any }[] = [];
  const fetchImpl = (async (url: string, init?: any) => {
    calls.push({ url: String(url), init });
    return { ok: true, status: 200, json: async () => body } as any;
  }) as any;
  return { calls, fetchImpl };
}

test("defaultSystemPrompt GETs the server default and returns it as the server sent it", async () => {
  const rec = recorder({ prompt: "base @@TOOL_LIST@@" });
  const out = await api.defaultSystemPrompt({ fetchImpl: rec.fetchImpl });
  expect(out).toEqual({ prompt: "base @@TOOL_LIST@@" });
  expect(rec.calls[0].url).toMatch(/^\/api\/default-system-prompt\?api_version=/);
  expect(rec.calls[0].init).toBeUndefined();
});

test("setStage POSTs the stage as a JSON body to the session's stage route", async () => {
  const rec = recorder({ stage: "REPORT" });
  const out = await api.setStage("s9", "REPORT", { fetchImpl: rec.fetchImpl });
  expect(out).toEqual({ stage: "REPORT" });
  expect(rec.calls[0].url).toMatch(/^\/api\/sessions\/s9\/stage\?/);
  expect(rec.calls[0].init.method).toBe("POST");
  expect(JSON.parse(rec.calls[0].init.body)).toEqual({ stage: "REPORT" });
});

test("createSession sends system_prompt in the body when one is given", async () => {
  const rec = recorder({ session_id: "n1" });
  await api.createSession({ engagement_id: "e1", system_prompt: "custom" }, { fetchImpl: rec.fetchImpl });
  expect(JSON.parse(rec.calls[0].init.body)).toEqual({ engagement_id: "e1", system_prompt: "custom" });
});
