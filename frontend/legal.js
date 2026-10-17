const sourceStatuses = {
  checked: "Источник проверен",
  historical: "Архивная редакция",
  unavailable: "Проверка недоступна",
};
const basisLabels = {
  national_rule: "Норма РК",
  internal_policy: "Внутренний порядок",
  interpretation: "Оценка по контексту",
};

function node(doc, tag, className, text) {
  const item = doc.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = String(text);
  return item;
}

function dateLabel(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return "не указана";
  const date = new Date(value + "T12:00:00Z");
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" }).format(date)
    : "не указана";
}

function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}

function sourceCard(doc, source) {
  const item = node(doc, "article", "legal-source");
  const verification = Object.hasOwn(sourceStatuses, source.verification_status) ? source.verification_status : "unavailable";
  const status = node(doc, "span", "legal-source-status", sourceStatuses[verification]);
  status.dataset.status = verification;
  item.append(status, node(doc, "h4", "", source.title || "Источник не указан"));
  if (source.locator) item.append(node(doc, "p", "", source.locator));
  if (source.edition_note) item.append(node(doc, "p", "", source.edition_note));
  if (source.checked_on) item.append(node(doc, "p", "field-help", "Сверено: " + dateLabel(source.checked_on)));
  const url = safeSourceUrl(source.url);
  if (url) {
    const link = node(doc, "a", "rule-link", "Открыть источник ↗");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    item.append(link);
  }
  return item;
}

function section(doc, title, values, tone) {
  if (!Array.isArray(values) || !values.length) return null;
  const item = node(doc, "section", "legal-section");
  if (tone) item.dataset.tone = tone;
  item.append(node(doc, "h4", "", title));
  const list = node(doc, "ul", "rule-conditions");
  for (const value of values) list.append(node(doc, "li", "", value));
  item.append(list);
  return item;
}

/** Text-only rendering: reference data never changes an event's saved rule. */
export function renderLegalGuide(container, guide) {
  const doc = container.ownerDocument;
  const content = [];
  const heading = node(doc, "div", "legal-heading");
  heading.append(node(doc, "p", "legal-meta", `Казахстан · Версия ${guide.version || "—"} · Сверено ${dateLabel(guide.reviewed_on)}`));
  if (guide.purpose) heading.append(node(doc, "p", "", guide.purpose));
  heading.append(node(doc, "p", "legal-reference-note", "Справка помогает сопоставить наблюдение с требованиями. Она не включает процедуры в анализ автоматически. В готовом результате сохраняется источник, указанный при его формировании."));
  content.push(heading);

  if (Array.isArray(guide.principles) && guide.principles.length) {
    const method = node(doc, "details", "legal-method");
    method.append(node(doc, "summary", "", "Как оценивать запись и выбирать редакцию"));
    const principles = node(doc, "div", "legal-principles");
    for (const principle of guide.principles) {
      const item = node(doc, "article", "");
      item.append(node(doc, "h3", "", principle.title), node(doc, "p", "", principle.text));
      principles.append(item);
    }
    method.append(principles);
    content.push(method);
  }

  const sources = new Map((Array.isArray(guide.sources) ? guide.sources : []).map(source => [source.id, source]));
  const procedures = node(doc, "div", "legal-procedures");
  for (const [index, procedure] of (Array.isArray(guide.procedures) ? guide.procedures : []).entries()) {
    const item = node(doc, "details", "legal-procedure");
    item.open = index === 0;
    const summary = node(doc, "summary", "legal-procedure-summary");
    summary.append(node(doc, "h3", "", procedure.title), node(doc, "span", "legal-basis", basisLabels[procedure.basis] || "Основание уточняется"));
    const body = node(doc, "div", "legal-procedure-body");
    if (procedure.requirement) body.append(node(doc, "p", "legal-requirement", procedure.requirement));
    const sections = node(doc, "div", "legal-sections");
    for (const [title, values, tone] of [
      ["Как соблюдать", procedure.correct, "correct"],
      ["Возможное отклонение", procedure.potential_deviation, "deviation"],
      ["Что нужно установить", procedure.required_context],
      ["Когда вывод делать нельзя", procedure.cannot_conclude],
      ["Пояснения", procedure.notes],
    ]) {
      const part = section(doc, title, values, tone);
      if (part) sections.append(part);
    }
    body.append(sections);
    if (Array.isArray(procedure.source_ids) && procedure.source_ids.length) {
      const references = node(doc, "section", "legal-sources");
      references.append(node(doc, "h4", "", "Источники и редакция"));
      for (const id of procedure.source_ids) references.append(sourceCard(doc, sources.get(id) || { title: `Источник ${id} отсутствует в справке`, verification_status: "unavailable" }));
      body.append(references);
    }
    if (Array.isArray(procedure.related_rule_ids) && procedure.related_rule_ids.length) {
      body.append(node(doc, "p", "field-help", "Идентификаторы в каталоге анализа: " + procedure.related_rule_ids.join(", ") + ". Состояние каждой процедуры указано в каталоге ниже."));
    }
    item.append(summary, body);
    procedures.append(item);
  }
  content.push(procedures);
  const referenced = new Set((guide.procedures || []).flatMap(procedure => procedure.source_ids || []));
  const additional = [...sources.values()].filter(source => !referenced.has(source.id));
  if (additional.length) {
    const references = node(doc, "section", "legal-sources");
    references.append(node(doc, "h3", "", "Дополнительные источники"));
    for (const source of additional) references.append(sourceCard(doc, source));
    content.push(references);
  }
  container.replaceChildren(...content);
}
