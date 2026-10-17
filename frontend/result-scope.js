/** Describe the scope saved with a result; the live catalog is not an input. */
export function describeAnalysisScope(scope) {
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return "Сведения об охвате этой проверки не сохранены.";
  const checked = strings(scope.checked_rule_ids);
  const enabled = strings(scope.enabled_rule_ids);
  const limitations = strings(scope.limitations);
  const parts = [scope.full_video_checked === true
    ? "Запись полностью проверена по сохранённому набору процедур."
    : scope.full_video_checked === false ? "Проверка записи частичная." : "Полнота проверки не указана в сохранённом результате."];
  parts.push(checked.length ? "Проверенные процедуры: " + checked.join(", ") + "." : "Перечень проверенных процедур не сохранён.");
  if (!enabled.length) parts.push("Набор процедур задачи не сохранён.");
  else if (checked.length !== enabled.length || enabled.some(id => !checked.includes(id))) parts.push("Набор процедур этой задачи: " + enabled.join(", ") + ".");
  if (limitations.length) parts.push("Ограничения: " + limitations.join("; "));
  return parts.join(" ");
}

function strings(values) {
  return Array.isArray(values) ? [...new Set(values.filter(item => typeof item === "string" && item.trim()).map(item => item.trim()))] : [];
}
