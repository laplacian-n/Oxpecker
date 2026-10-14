// The operator session (session.ts): the key/baseUrl the client authenticates with, kept
// per-viewer in localStorage. Pinned here because every /api call and SSE stream reads it — and
// because it must degrade to keyless rather than throw when storage is unavailable.
import { beforeEach, expect, test, vi } from "vitest";
import { getApiKey, setApiKey, getBaseUrl, setBaseUrl, sessionOptions } from "./session";

beforeEach(() => {
  try { localStorage.clear(); } catch { /* ignore */ }
});

test("a set key round-trips and is trimmed", () => {
  setApiKey("  s3cret  ");
  expect(getApiKey()).toBe("s3cret");
});

test("setting an empty key clears it", () => {
  setApiKey("s3cret");
  setApiKey("");
  expect(getApiKey()).toBe("");
});

test("baseUrl round-trips with the trailing slash stripped", () => {
  setBaseUrl("http://host:7777/");
  expect(getBaseUrl()).toBe("http://host:7777");
});

test("sessionOptions omits what is not set", () => {
  expect(sessionOptions()).toEqual({});
  setApiKey("k");
  expect(sessionOptions()).toEqual({ apiKey: "k" });
  setBaseUrl("http://h");
  expect(sessionOptions()).toEqual({ apiKey: "k", baseUrl: "http://h" });
});

test("a throwing localStorage degrades to keyless rather than crashing", () => {
  const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(getApiKey()).toBe("");
  expect(sessionOptions()).toEqual({});
  spy.mockRestore();
});
