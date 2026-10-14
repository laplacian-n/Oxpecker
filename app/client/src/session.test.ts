// The operator session (session.ts): the key/baseUrl the client authenticates with, kept
// per-viewer in localStorage. Pinned here because every /api call and SSE stream reads it — and
// because it must degrade to keyless rather than throw when storage is unavailable.
import { beforeEach, expect, test, vi } from "vitest";
import {
  getApiKey, setApiKey, getBaseUrl, setBaseUrl, sessionOptions, bootstrapSessionFromUrl,
} from "./session";

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

test("a key in the URL is adopted and scrubbed from the address", () => {
  const loc = { search: "?key=s3cret&server=http://box:7777", pathname: "/app/", hash: "#/x/chat" };
  let replaced = "";
  const history = { replaceState: (_d: unknown, _t: string, u: string) => { replaced = u; } };
  bootstrapSessionFromUrl(loc, history);
  expect(getApiKey()).toBe("s3cret");
  expect(getBaseUrl()).toBe("http://box:7777");
  expect(replaced).toBe("/app/#/x/chat"); // key/server removed, route hash kept
});

test("no key in the URL leaves an existing session untouched and rewrites nothing", () => {
  setApiKey("already");
  let replaced: string | null = null;
  bootstrapSessionFromUrl(
    { search: "?foo=bar", pathname: "/app/", hash: "" },
    { replaceState: (_d, _t, u) => { replaced = u; } },
  );
  expect(getApiKey()).toBe("already");
  expect(replaced).toBeNull(); // nothing to scrub, so the URL is not rewritten
});

test("a throwing localStorage degrades to keyless rather than crashing", () => {
  const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(getApiKey()).toBe("");
  expect(sessionOptions()).toEqual({});
  spy.mockRestore();
});
