import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Exercise the local annotator's save behavior without loading any real media.
function reviewForm({ saveResponse } = {}) {
  const html = readFileSync(new URL("../../ml_tools/review.html", import.meta.url), "utf8");
  const elements = new Map();
  const requests = [];
  const context = vm.createContext({
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { value: "", checked: false, textContent: "" });
      return elements.get(id);
    } },
    window: { addEventListener() {}, ...(saveResponse ? { __LOCAL_REVIEW__: {
      endpoint: '/queue.json', save_endpoint: '/annotations', save_token: 'synthetic-capability',
    } } : {}) }, structuredClone,
    fetch: async (url, options) => {
      if (url === '/queue.json') return new Promise(() => {});
      requests.push({ url, options });
      return saveResponse();
    },
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  vm.runInContext("current={id:'synthetic',sha256:'synthetic-hash'}; dirty=true", context);
  elements.get("verdict").value = "uncertain";
  elements.get("notes").value = "Human note to preserve";
  return { elements, context, requests, saved: () => vm.runInContext("annotations.synthetic", context) };
}

test("an unknown camera group does not prevent saving human notes as a draft", () => {
  const form = reviewForm();
  form.elements.get("save").onclick();
  assert.equal(form.saved().notes, "Human note to preserve");
  assert.equal(form.saved().reviewed, false);
  assert.equal(form.saved().source_group, "");
});

test("local save sends a draft with a capability and only clears dirty after disk acknowledgement", async () => {
  const form = reviewForm({ saveResponse: async () => ({ ok: true, status: 200, json: async () => ({ revision: 1 }) }) });
  const pending = form.elements.get('save').onclick();
  assert.equal(vm.runInContext('dirty', form.context), true);
  assert.equal(form.elements.get('save').disabled, true);
  await pending;
  assert.equal(form.requests.length, 1);
  assert.equal(form.requests[0].url, '/annotations');
  assert.equal(form.requests[0].options.method, 'POST');
  assert.equal(form.requests[0].options.headers['X-Review-Token'], 'synthetic-capability');
  const payload = JSON.parse(form.requests[0].options.body);
  assert.equal(payload.annotations.synthetic.source_group, '');
  assert.equal(payload.annotations.synthetic.reviewed, false);
  assert.equal(payload.annotations.synthetic.notes, 'Human note to preserve');
  assert.equal(form.saved().notes, payload.annotations.synthetic.notes);
  assert.equal(vm.runInContext('dirty', form.context), false);
  assert.equal(form.elements.get('export').hidden, true);
  assert.match(form.elements.get('status').textContent, /Сохранено на ноутбуке/);
});

for (const status of [409, 503]) {
  test(`failed or stale disk save preserves the user's note and dirty state: ${status}`, async () => {
    const form = reviewForm({ saveResponse: async () => ({ ok: false, status }) });
    await form.elements.get('save').onclick();
    assert.equal(form.saved(), undefined);
    assert.equal(form.elements.get('notes').value, 'Human note to preserve');
    assert.equal(vm.runInContext('dirty', form.context), true);
    assert.equal(form.elements.get('save').disabled, false);
    assert.match(form.elements.get('status').textContent, /заметки остались на странице/);
  });
}

test("typing while a disk save is pending retains the unsaved newer note", async () => {
  let finish;
  const form = reviewForm({ saveResponse: () => new Promise(resolve => { finish = resolve; }) });
  const pending = form.elements.get('save').onclick();
  form.elements.get('notes').value = 'Newer local note';
  finish({ ok: true, status: 200, json: async () => ({ revision: 1 }) });
  await pending;
  assert.equal(form.saved().notes, 'Human note to preserve');
  assert.equal(form.elements.get('notes').value, 'Newer local note');
  assert.equal(vm.runInContext('dirty', form.context), true);
});

test("an event description must be added before saving so it cannot silently disappear", async () => {
  const form = reviewForm({ saveResponse: async () => ({ ok: true, json: async () => ({ revision: 1 }) }) });
  form.elements.get('observation').value = 'Synthetic event not added yet';
  await form.elements.get('save').onclick();
  assert.equal(form.requests.length, 0);
  assert.equal(form.saved(), undefined);
  assert.match(form.elements.get('status').textContent, /ещё не добавлено/);
  assert.equal(form.elements.get('observation').value, 'Synthetic event not added yet');
});

test("typing an event during an acknowledged save keeps the page dirty", async () => {
  let finish;
  const form = reviewForm({ saveResponse: () => new Promise(resolve => { finish = resolve; }) });
  const pending = form.elements.get('save').onclick();
  form.elements.get('observation').value = 'New synthetic event';
  finish({ ok: true, json: async () => ({ revision: 1 }) });
  await pending;
  assert.equal(vm.runInContext('dirty', form.context), true);
  assert.equal(form.elements.get('observation').value, 'New synthetic event');
});

test("marking source time creates a protected event draft until added or cleared", async () => {
  const form = reviewForm();
  vm.runInContext('dirty=false', form.context);
  form.elements.get('video').currentTime = 4.25;
  form.elements.get('markStart').onclick();
  assert.equal(vm.runInContext('dirty', form.context), true);
  assert.equal(form.elements.get('start').value, '4.25');
  await form.elements.get('save').onclick();
  assert.equal(form.saved(), undefined);
  form.elements.get('clearEvent').onclick();
  await form.elements.get('save').onclick();
  assert.equal(form.saved().notes, 'Human note to preserve');
});

for (const field of ['start', 'end', 'rule']) {
  test(`an edit to ${field} while saving remains unsaved after the disk acknowledgement`, async () => {
    let finish;
    const form = reviewForm({ saveResponse: () => new Promise(resolve => { finish = resolve; }) });
    const pending = form.elements.get('save').onclick();
    form.elements.get(field).value = field === 'rule' ? 'R22' : '4.25';
    form.elements.get(field).oninput();
    finish({ ok: true, json: async () => ({ revision: 1 }) });
    await pending;
    assert.equal(vm.runInContext('dirty', form.context), true);
    assert.equal(vm.runInContext('eventDraftDirty', form.context), true);
    assert.match(form.elements.get('status').textContent, /новые изменения/);
  });
}

for (const group of ["", "organizer-single-camera", "unknown-session", "unknown", "unassigned"]) {
  test(`a reviewed record cannot confirm an unverified group: ${group || "blank"}`, () => {
    const form = reviewForm();
    form.elements.get("reviewed").checked = true;
    form.elements.get("group").value = group;
    form.elements.get("save").onclick();
    assert.equal(form.saved(), undefined);
    assert.match(form.elements.get("status").textContent, /проверенная группа/);
    assert.equal(form.elements.get("notes").value, "Human note to preserve");
  });
}

test("a reviewed record with a locally assigned group can be saved", () => {
  const form = reviewForm();
  form.elements.get("reviewed").checked = true;
  form.elements.get("group").value = "session-a";
  form.elements.get("view").value = "Synthetic functional role description";
  form.elements.get("save").onclick();
  assert.equal(form.saved().reviewed, true);
  assert.equal(form.saved().source_group, "session-a");
  assert.equal(form.saved().viewpoint, "Synthetic functional role description");
});

test("HTTP autoload opens a synthetic queue and preserves drafts while manual file mode stays local", async () => {
  const queue = {
    manifest: { records: ["a", "b"].map((suffix, index) => ({
      id: `synthetic-${suffix}`, file_name: `synthetic-${suffix}.mov`,
      sha256: `synthetic-hash-${suffix}`, duration_sec: 10 + index,
      split: "dev", duplicate_count: 1, media_url: `/video/${index}`,
    })) },
    annotations: {
      "synthetic-a": { notes: "First human draft", reviewed: false, events: [] },
      "synthetic-b": { notes: "Second human draft", reviewed: false, events: [] },
    },
  };
  queue.manifest.records[0].review_guidance = {
    error_kind: 'false_positive', checklist: ['<b>Literal human question</b>'],
    model_rule_ids: ['R20-B'], focus_seconds: [8, -1, 99],
  };

  function page(autoload) {
    const elements = new Map(), requests = [], blobFiles = [];
    function element(id) {
      if (!elements.has(id)) elements.set(id, {
        value: "", checked: false, textContent: "", children: [],
        replaceChildren(...children) {
          this.children = children;
          if (id === "select") this.value = children[0]?.value || "";
        },
        removeAttribute(attribute) { delete this[attribute]; },
        load() { this.loads = (this.loads || 0) + 1; },
        pause() { this.paused = true; },
      });
      return elements.get(id);
    }
    const context = vm.createContext({
      document: { getElementById: element, createElement: () => ({}) },
      window: { addEventListener() {}, ...(autoload ? { __LOCAL_REVIEW__: { endpoint: "/queue.json" } } : {}) },
      structuredClone,
      fetch: async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => structuredClone(queue) };
      },
      URL: { createObjectURL(file) { blobFiles.push(file); return "blob:synthetic-local-video"; }, revokeObjectURL() {} },
    });
    const html = readFileSync(new URL("../../ml_tools/review.html", import.meta.url), "utf8");
    vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
    return { element, context, requests, blobFiles };
  }

  const http = page(true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(http.requests.length, 1);
  assert.equal(http.requests[0].url, "/queue.json");
  assert.equal(http.requests[0].options.credentials, "omit");
  assert.equal(http.element("select").children.length, 2);
  assert.equal(http.element("select").value, "synthetic-a");
  assert.equal(vm.runInContext("current.media_url", http.context), "/video/0");
  assert.equal(http.element("video").src, "/video/0");
  assert.equal(http.element("video").muted, true);
  assert.equal(http.element("notes").value, "First human draft");
  assert.equal(http.element('guidance').hidden, false);
  assert.match(http.element('guidanceTitle').textContent, /1 из 2: Ложная тревога/);
  assert.equal(http.element('checklist').children[0].textContent, '<b>Literal human question</b>');
  assert.equal(http.element('checklist').children[0].innerHTML, undefined);
  assert.equal(http.element('focus').children.length, 1);
  http.element('focus').children[0].onclick();
  assert.equal(http.element('video').currentTime, 3);
  assert.equal(http.element('video').paused, true);
  http.element('observation').value = 'Stale synthetic input';
  http.element('start').value = '8';
  http.element("select").value = "synthetic-b";
  http.element("select").onchange();
  assert.equal(http.element("video").src, "/video/1");
  assert.equal(http.element("notes").value, "Second human draft");
  assert.equal(http.element('guidance').hidden, true);
  assert.equal(http.element('observation').value, '');
  assert.equal(http.element('start').value, '0');
  http.element("notes").value = "Human addition after local review";
  http.element("save").onclick();
  assert.equal(vm.runInContext("annotations['synthetic-b'].notes", http.context), "Human addition after local review");
  assert.equal(vm.runInContext("annotations['synthetic-a'].notes", http.context), "First human draft");
  http.element('next').onclick();
  assert.equal(http.element('select').value, 'synthetic-a');
  assert.match(http.element('progress').textContent, /0 из 2/);

  const manual = page(false);
  assert.equal(manual.requests.length, 0);
  await manual.element("manifest").onchange({ target: { files: [{ text: async () => JSON.stringify(queue.manifest) }] } });
  assert.equal(manual.element("video").src, undefined, "manual mode must not load server media URLs");
  const localFile = { name: "synthetic-a.mov" };
  manual.element("videos").onchange({ target: { files: [localFile] } });
  assert.equal(manual.element("video").src, "blob:synthetic-local-video");
  assert.equal(manual.blobFiles[0], localFile);
  assert.equal(manual.requests.length, 0);
});
