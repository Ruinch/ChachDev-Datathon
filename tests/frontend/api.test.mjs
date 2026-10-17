import { test } from "node:test";
import assert from "node:assert/strict";
import { apiRequest, getSystem, startAnalysis } from "../../frontend/api.js";

test("timeout covers a stalled successful response body", async t => {
  let signal;
  t.mock.method(globalThis, "fetch", async (_path, options) => {
    signal = options.signal;
    return { ok: true, status: 200, json: () => new Promise(() => {}) };
  });
  await assert.rejects(apiRequest("/slow", "secret", { timeoutMs: 10 }), error => error.code === "request_timeout" && /вовремя/.test(error.message));
  assert.equal(signal.aborted, true);
});

test("timeout covers a stalled error response body", async t => {
  t.mock.method(globalThis, "fetch", async () => ({ ok: false, status: 502, json: () => new Promise(() => {}) }));
  await assert.rejects(apiRequest("/slow", "secret", { timeoutMs: 10 }), { code: "request_timeout" });
});

test("source downloads have the same finite deadline for reading their binary body", async t => {
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, status: 200, blob: () => new Promise(() => {}) }));
  await assert.rejects(apiRequest("/video", "secret", { responseType: "blob", timeoutMs: 10 }), { code: "request_timeout" });
});

test("a timed out POST is never retried and tells the user to check history first", async t => {
  const fetch = t.mock.method(globalThis, "fetch", () => new Promise(() => {}));
  await assert.rejects(apiRequest("/upload", "secret", { method: "POST", timeoutMs: 10 }), error => error.code === "request_timeout" && /журнал/.test(error.message));
  assert.equal(fetch.mock.callCount(), 1);
});

test("caller cancellation also interrupts body reading without becoming a network failure", async t => {
  t.mock.method(globalThis, "fetch", async () => ({ ok: true, json: () => new Promise(() => {}) }));
  const controller = new AbortController();
  const request = apiRequest("/slow", "secret", { signal: controller.signal });
  controller.abort();
  await assert.rejects(request, { name: "AbortError" });
});

test("an already cancelled upload never reaches fetch", async t => {
  const fetch = t.mock.method(globalThis, "fetch", () => assert.fail("cancelled request sent"));
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(startAnalysis(new Blob(["video"]), "secret", controller.signal), { name: "AbortError" });
  assert.equal(fetch.mock.callCount(), 0);
});

test("protected requests use a header, no cache, and readable structured server errors", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async (_path, options) => {
    assert.equal(options.headers["X-API-Key"], "secret");
    assert.equal(options.cache, "no-store");
    return new Response(JSON.stringify({ detail: { code: "invalid_api_key", message: "raw" } }), { status: 401 });
  });
  await assert.rejects(getSystem("secret"), error => error.status === 401 && error.code === "invalid_api_key" && /ключ/.test(error.message));
  assert.equal(fetch.mock.calls[0].arguments[0].includes("secret"), false);
});

test("invalid JSON and network outages remain distinguishable", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response("bad JSON", { status: 200 }));
  await assert.rejects(getSystem("secret"), { code: "invalid_response" });
  fetch.mock.mockImplementation(async () => { throw new TypeError("network"); });
  await assert.rejects(getSystem("secret"), { code: "network_error" });
});
