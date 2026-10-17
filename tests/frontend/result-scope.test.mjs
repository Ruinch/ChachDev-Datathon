import { test } from "node:test";
import assert from "node:assert/strict";
import { describeAnalysisScope } from "../../frontend/result-scope.js";

test("a completed result identifies its saved procedures rather than the live catalog", () => {
  const scope = { full_video_checked: true, checked_rule_ids: ["SAVED-A", "SAVED-B"], enabled_rule_ids: ["SAVED-A", "SAVED-B"], limitations: [] };
  const before = JSON.stringify(scope);
  const text = describeAnalysisScope(scope);
  assert.match(text, /SAVED-A/);
  assert.match(text, /SAVED-B/);
  assert.match(text, /сохранён/);
  assert.doesNotMatch(text, /по активным правилам/);
  assert.equal(JSON.stringify(scope), before);
});

test("a partial result separates checked procedures from the saved task configuration", () => {
  const text = describeAnalysisScope({
    full_video_checked: false, checked_rule_ids: ["CHECKED-A"],
    enabled_rule_ids: ["CHECKED-A", "PENDING-B"], limitations: ["Клиентская зона не видна"],
  });
  assert.match(text, /Проверка записи частичная/);
  assert.match(text, /Проверенные процедуры: CHECKED-A\./);
  assert.match(text, /Набор процедур этой задачи: CHECKED-A, PENDING-B\./);
  assert.match(text, /Клиентская зона не видна/);
});

test("an old result without a saved scope does not claim full coverage", () => {
  assert.equal(describeAnalysisScope(undefined), "Сведения об охвате этой проверки не сохранены.");
  const text = describeAnalysisScope({});
  assert.match(text, /Полнота проверки не указана/);
  assert.match(text, /Перечень проверенных процедур не сохранён/);
});
