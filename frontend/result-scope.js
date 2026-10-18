/** Describe the scope saved with a result; the live catalog is not an input. */
export function describeAnalysisScope(scope) {
  if (!scope || typeof scope !== "object" || Array.isArray(scope)) return "Сведения об охвате этой проверки не сохранены.";
  const checked = strings(scope.checked_rule_ids);
  const enabled = strings(scope.enabled_rule_ids);
  const limitations = strings(scope.limitations);
  if (scope.mode === "experimental_review") {
    const requested = strings(scope.requested_rule_ids);
    const parts = ["Экспериментальный разбор: выборка кадров не гарантирует полного охвата записи или отсутствия нарушений."];
    parts.push(scope.sampled_analysis_complete === true ? "Проход по запланированной выборке завершён." : "Часть выборки или условий оценить не удалось.");
    if (requested.length) parts.push("Запрошенные кандидатные процедуры: " + requested.join(", ") + ".");
    if (checked.length) parts.push("Процедуры, рассмотренные моделью: " + checked.join(", ") + ". Это не подтверждение применимости нормы.");
    if (limitations.length) parts.push("Ограничения: " + limitations.join("; "));
    return parts.join(" ");
  }
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
