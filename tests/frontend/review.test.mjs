import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// Exercise the local annotator's save behavior without loading any real media.
function reviewForm() {
  const html = readFileSync(new URL("../../ml_tools/review.html", import.meta.url), "utf8");
  const elements = new Map();
  const context = vm.createContext({
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { value: "", checked: false, textContent: "" });
      return elements.get(id);
    } },
    window: { addEventListener() {} }, structuredClone,
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  vm.runInContext("current={id:'synthetic',sha256:'synthetic-hash'}; dirty=true", context);
  elements.get("verdict").value = "uncertain";
  elements.get("notes").value = "Human note to preserve";
  return { elements, context, saved: () => vm.runInContext("annotations.synthetic", context) };
}

test("an unknown camera group does not prevent saving human notes as a draft", () => {
  const form = reviewForm();
  form.elements.get("save").onclick();
  assert.equal(form.saved().notes, "Human note to preserve");
  assert.equal(form.saved().reviewed, false);
  assert.equal(form.saved().source_group, "");
});

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
