import { test } from "node:test";
import assert from "node:assert/strict";
import { ServiceWorkspace } from "../../frontend/service.js";

const ID = "123e4567-e89b-12d3-a456-426614174000";

function statusView(t, state) {
  const elements = new Map();
  const previousDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, { dataset: {}, textContent: "" });
      return elements.get(id);
    },
  } });
  t.after(() => {
    if (previousDocument) Object.defineProperty(globalThis, "document", previousDocument);
    else delete globalThis.document;
  });
  const view = Object.create(ServiceWorkspace.prototype);
  view.session = { state };
  view.lastSummary = { processing: 1 };
  view.updateAccess = () => view.updateGlobalState();
  return { view, element: id => document.getElementById(id) };
}

test("finishing the watched job invalidates a stale busy summary from the journal", t => {
  const system = { ready: true, busy: true };
  const previous = { connected: true, page: "review", jobId: ID, job: { status: "processing" }, busy: true, system };
  const state = { ...previous, busy: false, job: { status: "done" } };
  const { view, element } = statusView(t, state);
  ServiceWorkspace.prototype.render.call(view, state, previous);
  assert.equal(element("global-service-state").dataset.state, "ready");
  assert.equal(view.lastSummary, null);
});

test("opening a historical result does not discard a journal summary about another active job", t => {
  const system = { ready: true, busy: true };
  const previous = { connected: true, page: "review", jobId: ID, job: null, busy: true, system };
  const state = { ...previous, busy: false, job: { status: "done" } };
  const { view, element } = statusView(t, state);
  ServiceWorkspace.prototype.render.call(view, state, previous);
  assert.equal(element("global-service-state").dataset.state, "busy");
  assert.equal(view.lastSummary.processing, 1);
});

test("the connected experimental service is available without a green verified-analysis claim", t => {
  const system = { ready: false, busy: false, review: { enabled: true, ready: true } };
  const state = { connected: true, page: "review", jobId: ID, job: { status: "done" }, busy: false, system };
  const { view, element } = statusView(t, state);
  view.lastSummary = { processing: 0 };
  view.updateGlobalState();
  assert.match(element("global-service-state").textContent, /Экспериментальный разбор/);
  assert.equal(element("global-service-state").dataset.state, "warning");
});
