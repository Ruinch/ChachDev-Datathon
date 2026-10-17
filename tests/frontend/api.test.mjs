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

test("every ambiguous POST failure is marked uncertain and is sent only once", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => { throw new TypeError("offline"); });
  for (const implementation of [
    async () => { throw new TypeError("offline"); },
    async () => new Response("invalid JSON", { status: 202 }),
    async () => new Response("gateway", { status: 502 }),
    async () => new Response("proxy unavailable", { status: 503 }),
  ]) {
    fetch.mock.mockImplementation(implementation);
    const before = fetch.mock.callCount();
    await assert.rejects(apiRequest("/upload", "secret", { method: "POST" }), error => error.outcomeUnknown === true && /журнал/.test(error.message));
    assert.equal(fetch.mock.callCount(), before + 1);
  }
  fetch.mock.mockImplementation(() => new Promise(() => {}));
  await assert.rejects(apiRequest("/upload", "secret", { method: "POST", timeoutMs: 10 }), { outcomeUnknown: true, code: "request_timeout" });
});

test("definite POST rejections and safe GET failures are not upload uncertainties", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response("{}", { status: 422 }));
  for (const status of [400, 401, 413, 415, 422, 429, 503, 507]) {
    fetch.mock.mockImplementation(async () => new Response(JSON.stringify({ detail: { code: status === 503 ? "analyzer_unavailable" : status === 507 ? "storage_unavailable" : "rejected" } }), { status }));
    await assert.rejects(apiRequest("/upload", "secret", { method: "POST" }), error => error.outcomeUnknown === false);
  }
  fetch.mock.mockImplementation(async () => { throw new TypeError("offline"); });
  await assert.rejects(getSystem("secret"), error => error.outcomeUnknown === false);
});

test("a stalled rejection body cannot turn a received HTTP rejection into uncertain acceptance", async t => {
  t.mock.method(globalThis, "fetch", async () => ({ ok: false, status: 422, json: () => new Promise(() => {}) }));
  await assert.rejects(apiRequest("/upload", "secret", { method: "POST", timeoutMs: 10 }), error => error.status === 422 && error.outcomeUnknown === false && !/могла быть принята/.test(error.message));
});

test("model readiness refusals explain the cause without claiming the upload was accepted", async t => {
  const fetch = t.mock.method(globalThis, "fetch", async () => new Response("{}", { status: 503 }));
  for (const code of ["local_model_unavailable", "ml_dependencies_unavailable", "analyzer_config_invalid"]) {
    fetch.mock.mockImplementation(async () => new Response(JSON.stringify({ detail: { code, message: "Internal message" } }), { status: 503 }));
    await assert.rejects(apiRequest("/api/v1/analyze", "secret", { method: "POST" }), error =>
      error.code === code && error.outcomeUnknown === false && /модел|анализатор/.test(error.message) && !/Internal/.test(error.message));
  }
});
