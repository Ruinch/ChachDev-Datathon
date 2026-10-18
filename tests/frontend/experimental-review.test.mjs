import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { serviceMode, isExperimental, resultEvents, resultPresentation, historyPresentation } from "../../frontend/review-mode.js";
import { describeAnalysisScope } from "../../frontend/result-scope.js";

test("review readiness never promotes the strict analyzer and requires an explicit enabled flag", () => {
  const info = { ready: false, review: { enabled: true, ready: true } };
  assert.deepEqual(serviceMode(info), { experimental: true, ready: true });
  assert.equal(info.ready, false);
  assert.equal(serviceMode({ ready: false, review: { ready: true } }).ready, false);
  assert.equal(serviceMode({ ready: true, review: { enabled: true, ready: false } }).ready, false);
});

test("experimental results remain observations even if a malformed response includes a verdict", () => {
  const result = { mode: "experimental_review", verdict: "violation", observations: [{ type: "Synthetic observation" }], violations: [{ type: "Wrong collection" }] };
  assert.equal(resultEvents(result)[0].type, "Synthetic observation");
  const view = resultPresentation(result);
  assert.equal(view.experimental, true);
  assert.equal(view.clear, false);
  assert.match(view.title, /Наблюдения/);
  assert.match(view.summary, /Уверенность не откалибрована/);
});

test("an empty review with full sampled coverage never claims absence of violations", () => {
  const result = { mode: "experimental_review", observations: [], analysis_scope: { full_video_checked: false, sampled_analysis_complete: true } };
  const view = resultPresentation(result);
  assert.equal(view.clear, false);
  assert.match(view.emptyDescription, /не означает отсутствия нарушений/);
  assert.match(view.badge, /РУЧН/);
  assert.doesNotMatch(view.summary, /Запись проверена/);
});

test("history preserves experimental identification for zero and multiple observations", () => {
  for (const event_count of [0, 2]) {
    const view = historyPresentation({ status: "done", verdict: null, event_count, analysis_scope: { mode: "experimental_review" } });
    assert.equal(view.status, "unknown");
    assert.match(view.label, /Экспериментальный разбор/);
    assert.match(view.note, /Ручная проверка/);
  }
});

test("review scope distinguishes requested candidate procedures from validated coverage", () => {
  const text = describeAnalysisScope({ mode: "experimental_review", full_video_checked: false, sampled_analysis_complete: true,
    requested_rule_ids: ["CANDIDATE-A", "CANDIDATE-B"], checked_rule_ids: ["CANDIDATE-A"], enabled_rule_ids: [], limitations: ["Synthetic visibility limit"] });
  assert.match(text, /Экспериментальный разбор/);
  assert.match(text, /CANDIDATE-A, CANDIDATE-B/);
  assert.match(text, /не гарантирует полного охвата/);
  assert.match(text, /Synthetic visibility limit/);
  assert.doesNotMatch(text, /Запись полностью проверена|Набор процедур задачи не сохранён/);
});

test("the existing strict negative result retains its scope requirement", () => {
  assert.equal(resultPresentation({ verdict: "no_violation", violations: [], analysis_scope: { full_video_checked: true } }).clear, true);
  assert.equal(resultPresentation({ verdict: "no_violation", violations: [], analysis_scope: { full_video_checked: false } }).clear, false);
});

// Exercise the actual DOM renderers with synthetic data, without a browser or media.
function renderedResult(result) {
  class Element {
    constructor(tag = "div", className = "", text = "") {
      this.tagName = tag; this.className = className; this.text = text;
      this.children = []; this.dataset = {}; this.attributes = {}; this.hidden = false;
    }
    set textContent(value) { this.text = value; this.children = []; }
    get textContent() { return this.text + this.children.map(child => child.textContent).join(" "); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.text = ""; this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener() {}
    focus() {}
    querySelectorAll(selector) { return this.children.flatMap(child => [...((selector.startsWith(".") ? child.className === selector.slice(1) : child.tagName === selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) {
      const item = this.querySelectorAll(selector)[0];
      if (item) return item;
      const child = new Element(selector.startsWith(".") ? "span" : selector, selector.startsWith(".") ? selector.slice(1) : "");
      this.append(child); return child;
    }
  }
  const elements = new Map();
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const context = vm.createContext({
    byId, element: (tag, className, text) => new Element(tag, className, text),
    document: { createElement: tag => new Element(tag) }, URL,
    currentEvents: [], currentResult: null, resultDownloadUrl: null,
    resultsPanel: byId("results-panel"), resultsTitle: byId("results-title"),
    eventIndex: byId("event-index"), eventDetail: byId("event-detail"),
    clearResult() { byId("no-events").hidden = true; byId("events-layout").hidden = true; },
    createJsonDownload: () => ({ url: "blob:synthetic", filename: "review.json" }),
    session: { state: { jobId: "synthetic" } }, viewer: { open() {} },
    validFrame: () => true, formatTime: value => `${value}s`,
    describeAnalysisScope, resultEvents, resultPresentation, isExperimental,
  });
  const source = readFileSync(new URL("../../frontend/app.js", import.meta.url), "utf8");
  vm.runInContext(source.slice(source.indexOf("function renderEventDetail("), source.indexOf("async function restoreJob(")), context);
  vm.runInContext(source.slice(source.indexOf("function preparePrintReport("), source.indexOf('byId("print-result").addEventListener')), context);
  context.result = result;
  vm.runInContext("renderResult(result)", context);
  return byId;
}

test("rendered review cards and print preserve candidate references, evidence and uncertainty", () => {
  const byId = renderedResult({ status: "done", mode: "experimental_review", review_required: true,
    observations: [{ type: "Synthetic observation", description: "Synthetic hypothesis", timecode_sec: 3,
      evidence_frames: [{ timestamp_sec: 3, image_base64: "synthetic-jpeg" }],
      candidate_rule: { title: "Synthetic candidate", clause: "Clause X", source_url: "https://example.org/candidate" }, confidence_status: "uncalibrated" }],
    analysis_scope: { mode: "experimental_review", sampled_analysis_complete: true, requested_rule_ids: ["CANDIDATE"], checked_rule_ids: ["CANDIDATE"], limitations: ["Synthetic limit"] },
  });
  assert.equal(byId("events-layout").hidden, false);
  assert.equal(byId("event-index").querySelectorAll("button").length, 1);
  assert.match(byId("event-detail").textContent, /ГИПОТЕЗА МОДЕЛИ|Уверенность не откалибрована/);
  assert.match(byId("event-detail").textContent, /Synthetic candidate · Clause X/);
  assert.equal(byId("event-detail").querySelectorAll("img")[0].src, "data:image/jpeg;base64,synthetic-jpeg");
  assert.equal(byId("event-detail").querySelectorAll("a")[0].href, "https://example.org/candidate");
  assert.match(byId("print-all-events").textContent, /Экспериментальный разбор/);
  assert.match(byId("print-all-events").textContent, /применимость не подтверждена/);
  assert.match(byId("print-all-events").textContent, /уверенность не откалибрована/);
});

test("rendered empty review keeps visible limitations and asks for human inspection", () => {
  const byId = renderedResult({ status: "done", mode: "experimental_review", observations: [], review_required: true,
    analysis_scope: { mode: "experimental_review", sampled_analysis_complete: false, limitations: ["Synthetic insufficient visibility"] } });
  assert.equal(byId("analysis-scope").hidden, false);
  assert.match(byId("analysis-scope").textContent, /Synthetic insufficient visibility/);
  assert.match(byId("no-events").textContent, /не означает отсутствия нарушений/);
  assert.equal(byId("no-events").dataset.state, "unknown");
  assert.match(byId("verdict-badge").textContent, /РУЧНАЯ ПРОВЕРКА/);
});
