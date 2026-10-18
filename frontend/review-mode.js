/** Review is a separate operation; it never changes strict readiness or verdicts. */
export function serviceMode(info) {
  const experimental = info?.review?.enabled === true;
  return { experimental, ready: experimental ? info.review.ready === true : info?.ready === true };
}

export const isExperimental = result => result?.mode === "experimental_review" || result?.analysis_scope?.mode === "experimental_review";
export function resultEvents(result) {
  const events = isExperimental(result) ? result.observations : result.violations;
  return Array.isArray(events) ? events : [];
}

export function resultPresentation(result) {
  const experimental = isExperimental(result);
  const events = resultEvents(result);
  const hasEvents = events.length > 0 && (experimental || result.verdict === "violation");
  const clear = !experimental && result.verdict === "no_violation" && !events.length && result.analysis_scope?.full_video_checked === true;
  if (experimental) return {
    experimental, hasEvents, clear: false,
    title: hasEvents ? "Наблюдения для ручной проверки" : "Экспериментальный разбор завершён",
    badge: "НУЖНА РУЧНАЯ ПРОВЕРКА", badgeState: "unknown",
    summary: `Экспериментальный режим. Наблюдений: ${events.length}. Это гипотезы модели, не подтверждённые нарушения. Уверенность не откалибрована.`,
    emptyTitle: "Модель не выделила наблюдений",
    emptyDescription: "Это не означает отсутствия нарушений. Просмотрите исходное видео и ограничения охвата; при недостаточной видимости вывод остаётся неопределённым.",
  };
  if (hasEvents) return {
    experimental, hasEvents, clear, title: "Эпизоды для проверки", badge: "ТРЕБУЕТ ПРОВЕРКИ", badgeState: "violation",
    summary: `Событий для проверки: ${events.length}. ` + (result.analysis_scope?.full_video_checked === false
      ? "Часть записи или процедур оценить не удалось. Учитывайте ограничения ниже и сверьте эпизоды с исходной записью."
      : "Сверьте каждый эпизод с исходной записью и условиями нормы."),
  };
  return {
    experimental, hasEvents, clear, title: clear ? "Результат проверки" : "Результат недоступен",
    badge: clear ? "СОБЫТИЯ НЕ НАЙДЕНЫ" : "НЕТ ДАННЫХ", badgeState: clear ? "no_violation" : "unknown",
    summary: clear ? "Запись проверена по сохранённому набору процедур." : "Сервис вернул неполный ответ.",
    emptyTitle: clear ? "По сохранённым процедурам события не найдены" : "Не удалось показать результат",
    emptyDescription: clear ? "Анализатор мог пропустить событие. Вывод относится только к указанным выше сохранённым процедурам этой проверки. Другие действия требуют отдельной оценки по исходной записи." : "Повторите проверку или изучите ответ API.",
  };
}

export function historyPresentation(job) {
  if (job.status === "done" && isExperimental(job)) return { label: "Экспериментальный разбор", status: "unknown", note: "Ручная проверка обязательна" };
  const completeNegative = job.verdict === "no_violation" && job.analysis_scope?.full_video_checked === true;
  const uncertain = job.status === "done" && job.verdict !== "violation" && !completeNegative;
  return {
    label: job.status === "processing" ? "Обрабатывается" : job.status === "failed" ? "Без результата" : job.verdict === "violation" ? "Эпизоды для проверки" : completeNegative ? "События не найдены" : "Проверьте результат",
    status: uncertain ? "unknown" : job.status === "done" ? job.verdict : job.status,
    note: job.status === "done" && job.analysis_scope?.full_video_checked === false ? "Частичная проверка" : "",
  };
}
