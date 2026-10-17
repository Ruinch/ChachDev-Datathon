import { test } from "node:test";
import assert from "node:assert/strict";
import { renderLegalGuide } from "../../frontend/legal.js";
import { ServiceWorkspace } from "../../frontend/service.js";
import { getLegalGuide } from "../../frontend/api.js";

// The view uses text nodes and native details. This small document records its
// public output without adding a browser library to production dependencies.
class Element {
  constructor(doc, tag = "div") {
    this.ownerDocument = doc;
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = new Map();
    this.hidden = false;
    this.value = "";
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || "") + this.children.map(child => child.textContent).join(" "); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes.set(name, value); }
  removeAttribute(name) { this.attributes.delete(name); }
  all(tag) { return this.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...child.all(tag)]); }
}

function documentStub() {
  const ids = new Map();
  return {
    createElement(tag) { return new Element(this, tag); },
    getElementById(id) {
      if (!ids.has(id)) ids.set(id, new Element(this));
      return ids.get(id);
    },
  };
}

function guide() {
  return {
    version: "2026-10", reviewed_on: "2026-10-18", jurisdiction: "KZ", result_binding: "reference_only",
    purpose: "Сопоставление с источниками", principles: [{ title: "Контекст", text: "Нужна полная операция." }],
    sources: [{ id: "s1", title: "Название документа", url: "https://example.org/rule", locator: "Пункт 20", edition_note: "Редакция с изменениями", checked_on: "2026-10-18", verification_status: "checked" }],
    procedures: [{ id: "p1", title: "Проверяемая процедура", basis: "national_rule", requirement: "Точное требование", correct: ["Правильный порядок"], potential_deviation: ["Возможное отклонение"], required_context: ["Видна граница операции"], cannot_conclude: ["Клиент за пределами кадра"], notes: ["Примечание"], source_ids: ["s1"], related_rule_ids: ["R-1"] }],
  };
}

test("legal guidance keeps correct actions, context, limits, edition and source together", () => {
  const doc = documentStub();
  const container = doc.createElement("div");
  const data = guide();
  data.procedures.push({ id: "p2", title: "Внутренняя процедура", basis: "internal_policy", source_ids: [], correct: ["Инструкция банка"] });
  const before = JSON.stringify(data);
  renderLegalGuide(container, data);
  const details = container.all("details").filter(item => item.className === "legal-procedure");
  assert.equal(details.length, 2);
  assert.equal(details[0].open, true);
  assert.equal(details[1].open, false);
  for (const text of ["Правильный порядок", "Возможное отклонение", "Видна граница операции", "Клиент за пределами кадра", "Пункт 20", "Редакция с изменениями", "Источник проверен", "Внутренний порядок", "18 октября 2026", "R-1"]) assert.ok(container.textContent.includes(text), text);
  assert.match(container.textContent, /не включает процедуры в анализ автоматически/);
  const link = details[0].all("a")[0];
  assert.equal(link.href, "https://example.org/rule");
  assert.equal(link.rel, "noopener noreferrer");
  assert.equal(JSON.stringify(data), before, "rendering must not mutate or activate rules");
});

test("source status, missing reference, and untrusted markup cannot become a legal assertion or executable link", () => {
  const doc = documentStub();
  const container = doc.createElement("div");
  const data = guide();
  data.sources[0].url = "javascript:alert(1)";
  data.sources[0].verification_status = "unavailable";
  data.procedures[0].title = "<img src=x onerror=alert(1)>";
  data.procedures[0].source_ids.push("missing");
  data.sources.push({ id: "old", title: "Прошлая редакция", url: "https://example.org/history", verification_status: "historical" });
  renderLegalGuide(container, data);
  assert.match(container.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(container.all("img").length, 0);
  assert.equal(container.all("a").length, 1);
  assert.match(container.textContent, /Источник missing отсутствует/);
  assert.match(container.textContent, /Проверка недоступна/);
  assert.match(container.textContent, /Архивная редакция/);
  assert.doesNotMatch(container.textContent, /Источник проверен/);
});

function workspace(t, session) {
  const doc = documentStub();
  const previous = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { value: doc, configurable: true });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, "document", previous);
    else delete globalThis.document;
  });
  const view = { session, connected: true, page: "rules", legalVersion: 0, rulesVersion: 0, feedback(id, text) { doc.getElementById(id).textContent = text; } };
  return { view, doc };
}

test("a failed guide does not hide the independently loaded procedure catalog and its verification note", async t => {
  const { view, doc } = workspace(t, {
    legalGuide: async () => { throw new Error("Справка недоступна"); },
    rules: async () => ({ total: 1, enabled_count: 0, items: [{ id: "R-1", event_type: "Процедура", enabled: false, verification: "candidate", verification_note: "Требуется внутренняя инструкция" }] }),
  });
  await Promise.all([ServiceWorkspace.prototype.loadLegalGuide.call(view), ServiceWorkspace.prototype.loadRules.call(view)]);
  assert.match(doc.getElementById("legal-feedback").textContent, /Повторите загрузку/);
  assert.match(doc.getElementById("rules-list").textContent, /Требуется внутренняя инструкция/);
  assert.equal(doc.getElementById("rules-feedback").textContent, "");
  assert.equal(doc.getElementById("legal-refresh").disabled, false);
});

test("a failed analyzer catalog does not prevent opening the reference guide", async t => {
  const { view, doc } = workspace(t, {
    legalGuide: async () => guide(),
    rules: async () => { throw new Error("Каталог недоступен"); },
  });
  await Promise.all([ServiceWorkspace.prototype.loadLegalGuide.call(view), ServiceWorkspace.prototype.loadRules.call(view)]);
  assert.match(doc.getElementById("rules-feedback").textContent, /Каталог недоступен/);
  assert.match(doc.getElementById("legal-content").textContent, /Точное требование/);
  assert.equal(doc.getElementById("legal-feedback").textContent, "");
  assert.equal(doc.getElementById("rules-refresh").disabled, false);
});

test("the legal guide uses the protected no-cache API without placing the key in its URL", async t => {
  const expected = guide();
  t.mock.method(globalThis, "fetch", async (path, options) => {
    assert.equal(path, "/api/v1/legal-guide");
    assert.equal(options.headers["X-API-Key"], "private-key");
    assert.equal(options.cache, "no-store");
    return { ok: true, status: 200, json: async () => expected };
  });
  assert.deepEqual(await getLegalGuide("private-key"), expected);
});
