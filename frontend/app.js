import { WorkspaceSession } from "./session.js";
import { SourcePlayer, EvidenceViewer, formatTime, formatSize, validFrame, createJsonDownload } from "./media.js";
import { ServiceWorkspace } from "./service.js";
import { describeAnalysisScope } from "./result-scope.js";

const byId = id => document.getElementById(id);
const form = byId("analyze-form");
const keyInput = byId("api-key");
const fileInput = byId("video-file");
const dropzone = byId("dropzone");
const formError = byId("form-error");
const submitButton = byId("submit-button");
const statusEmpty = byId("status-empty");
const statusContent = byId("status-content");
const resultsPanel = byId("results-panel");
const resultsTitle = byId("results-title");
const eventIndex = byId("event-index");
const eventDetail = byId("event-detail");
const player = new SourcePlayer();
const viewer = new EvidenceViewer();
const session = new WorkspaceSession();
let currentEvents = [];
let currentResult = null;
let elapsedTimer = null;
let startedAt = null;
let resultDownloadUrl = null;
const filename = () => session.state.job?.metadata?.filename || session.state.selectedFile?.name || "";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setError(message, focusTarget = formError) {
  formError.textContent = message;
  formError.hidden = !message;
  if (message && focusTarget) focusTarget.focus();
}

function setLocked(value) {
  fileInput.disabled = value;
  keyInput.disabled = value;
  submitButton.disabled = value;
  byId("resume-button").disabled = value;
  byId("resume-job").disabled = value;
  byId("check-service").disabled = value;
  dropzone.dataset.locked = String(value);
  dropzone.setAttribute("aria-disabled", String(value));
  byId("submit-label").textContent = value ? "Идёт проверка…" : "Начать проверку";
}

function clearResult() {
  viewer.close();
  resultsPanel.hidden = true;
  byId("events-layout").hidden = true;
  byId("no-events").hidden = true;
  currentEvents = [];
  currentResult = null;
  byId("json-inspector").hidden = true;
  byId("result-json").value = "";
  if (resultDownloadUrl) URL.revokeObjectURL(resultDownloadUrl);
  resultDownloadUrl = null;
  byId("export-result").removeAttribute("href");
  eventIndex.replaceChildren();
  eventDetail.replaceChildren();
  byId("print-all-events")?.replaceChildren();
  byId("result-announcement").textContent = "";
}

function chooseFile(file) {
  if (session.selectFile(file)) setError("");
}

function readinessMessage(info) {
  const reasons = [];
  if (!info.analyzer?.configured) reasons.push("локальная модель ещё не подключена");
  if (info.analyzer?.runtime?.ready === false) reasons.push(info.analyzer.runtime.unavailable_reason?.code === "ml_dependencies_unavailable"
    ? "зависимости локального анализатора недоступны" : info.analyzer.runtime.unavailable_reason?.code === "analyzer_config_invalid"
      ? "настройки локальной модели некорректны" : "локальная модель не отвечает");
  if (!info.analyzer?.rules_ready) reasons.push("правила проверки ещё не активированы");
  if (!info.tools?.ffmpeg || !info.tools?.ffprobe) reasons.push("инструменты обработки видео недоступны");
  if (reasons.length) return "Сервис пока не готов: " + reasons.join("; ") + ". Предпросмотр видео доступен.";
  if (info.busy) return "Сервис обрабатывает другую запись. Повторите загрузку после её завершения.";
  if (!info.ready) return "Сервис пока не готов к обработке. Проверьте настройки сервера.";
  const names = info.analyzer?.rules?.map(rule => rule.event_type).filter(Boolean);
  return names?.length ? "Проверяем: " + names.join("; ") + "." : "Сервис готов. Проверяемых правил: " + (info.analyzer?.enabled_rule_ids?.length || 0) + ".";
}

async function checkSystem() {
  session.setKey(keyInput.value);
  return session.connect();
}

function failureMessage(error) {
  const messages = {
    analyzer_unavailable: "Локальный анализатор ещё не подключён. Предпросмотр исходного видео доступен.",
    analyzer_configuration_error: "Не удалось запустить локальную модель. Проверьте её конфигурацию и локальные веса.",
    analysis_not_ready: "Проверяемые процедуры ещё не активированы на сервере.",
    insufficient_evidence: "Недостаточно видимого контекста для проверки процедур. Вердикт не сформирован; просмотрите исходную запись.",
    analysis_timeout: "Проверка превысила лимит времени и остановлена. Попробуйте более короткую запись или проверьте скорость модели.",
    job_timeout: "Проверка превысила лимит времени и остановлена.",
    interrupted: "Проверка прервана перезапуском сервера. Загрузите запись повторно.",
    evidence_extraction_failed: "Не удалось извлечь доказательные кадры. Проверьте исходную запись и инструменты обработки видео.",
    rule_validation_failed: "Результат модели не соответствует каталогу правил. Нужна проверка настроек анализатора.",
    rule_validation_error: "Результат модели не соответствует каталогу правил. Нужна проверка настроек анализатора.",
    invalid_analysis_result: "Модель вернула некорректный результат. Вердикт не сформирован.",
    invalid_rule: "Результат модели ссылается на неизвестное или отключённое правило. Вердикт не сформирован.",
    worker_crashed: "Процесс анализа неожиданно остановился. Проверьте журнал сервера и доступную память.",
    worker_start_failed: "Не удалось запустить процесс анализа. Проверьте журнал сервера.",
    analysis_failed: "Локальный анализ завершился с ошибкой. Проверьте журнал сервера.",
  };
  return messages[error?.code] || error?.message || "Проверку не удалось завершить. Повторите запрос.";
}

function updateElapsed(finishedAt = null) {
  byId("elapsed-row").hidden = !startedAt;
  if (!startedAt) return;
  const end = finishedAt || Date.now();
  byId("status-elapsed").textContent = formatTime(Math.max(0, (end - startedAt) / 1000));
}

function setStatus(status, note, metadata = session.state.job?.metadata || {}) {
  const labels = {
    preparing: "Проверяем параметры…",
    uploading: "Загружаем видео…",
    processing: "Обрабатываем запись…",
    done: "Проверка завершена",
    failed: "Проверка не выполнена",
    connection: "Статус временно недоступен",
  };
  statusEmpty.hidden = true;
  statusContent.hidden = false;
  document.querySelector(".status-main").dataset.status = status;
  byId("status-indicator").dataset.status = status;
  byId("status-label").textContent = labels[status] || "Состояние неизвестно";
  byId("status-file").textContent = metadata.filename || filename() || "—";
  byId("status-job").textContent = session.state.jobId || (status === "uploading" ? "Создаётся…" : "—");
  byId("status-note").textContent = note;
  byId("retry-button").hidden = true;
  byId("copy-job").hidden = !session.state.jobId;
  const serverStart = Date.parse(metadata.created_at);
  if (Number.isFinite(serverStart)) startedAt = serverStart;
  clearInterval(elapsedTimer);
  const serverEnd = Date.parse(metadata.updated_at);
  if (status === "done" || status === "failed") updateElapsed(Number.isFinite(serverEnd) ? serverEnd : Date.now());
  else {
    updateElapsed();
    if (startedAt) elapsedTimer = window.setInterval(() => updateElapsed(), 1000);
  }
}

async function ensureSource() {
  if (player.url) return true;
  if (!session.state.jobId) return false;
  const button = byId("load-source");
  const expectedJob = session.state.jobId;
  button.disabled = true;
  button.textContent = "Загружаем исходное видео…";
  try {
    const blob = await session.source();
    if (!blob || expectedJob !== session.state.jobId) return false;
    player.setBlob(blob, filename());
    return true;
  } catch (error) {
    if (error.name !== "AbortError" && expectedJob === session.state.jobId) byId("source-note").textContent = error.message;
    return false;
  } finally {
    if (expectedJob === session.state.jobId) {
      button.disabled = false;
      button.textContent = "Загрузить исходное видео для просмотра";
    }
  }
}

function renderEventDetail(index) {
  const event = currentEvents[index];
  if (!event) return;
  for (const button of eventIndex.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.index) === index));
  }
  eventDetail.replaceChildren();
  const heading = element("div", "detail-heading");
  const copy = element("div");
  copy.append(element("span", "overline", "ЭПИЗОД " + String(index + 1).padStart(2, "0")));
  copy.append(element("h3", "", event.type || "Событие"));
  heading.append(copy, element("span", "time-chip", formatTime(event.timecode_sec)));
  eventDetail.append(heading);
  const seek = element("button", "text-button seek-button", "Смотреть с " + formatTime(event.timecode_sec) + " →");
  seek.type = "button";
  seek.addEventListener("click", async () => {
    seek.disabled = true;
    try { if (await ensureSource()) player.seek(event.timecode_sec); }
    finally { seek.disabled = false; }
  });
  eventDetail.append(seek);
  const description = element("section", "detail-block");
  description.append(element("span", "detail-label", "ОПИСАНИЕ СОБЫТИЯ"));
  description.append(element("p", "event-description", event.description || "Описание не получено."));
  eventDetail.append(description);
  const evidence = element("section", "detail-block");
  evidence.append(element("span", "detail-label", "КАДРЫ ИЗ ИСХОДНОГО ВИДЕО · НАЖМИТЕ ДЛЯ УВЕЛИЧЕНИЯ"));
  const frames = Array.isArray(event.evidence_frames) ? event.evidence_frames.filter(validFrame).slice(0, 3) : [];
  if (frames.length) {
    const grid = element("div", "evidence-grid");
    grid.dataset.count = String(frames.length);
    frames.forEach((frame, frameIndex) => {
      const figure = element("figure", "evidence-item");
      const button = element("button", "evidence-open");
      button.type = "button";
      button.setAttribute("aria-label", "Увеличить кадр на " + formatTime(frame.timestamp_sec));
      const image = document.createElement("img");
      image.width = 464;
      image.height = 848;
      image.loading = "lazy";
      image.alt = "Кадр исходного видео на " + formatTime(frame.timestamp_sec);
      image.addEventListener("error", () => {
        button.replaceWith(element("p", "evidence-empty", "Кадр не удалось показать."));
      }, { once: true });
      image.src = "data:image/jpeg;base64," + frame.image_base64;
      button.append(image);
      button.addEventListener("click", () => viewer.open(frames, frameIndex, button));
      figure.append(button, element("figcaption", "", formatTime(frame.timestamp_sec)));
      grid.append(figure);
    });
    evidence.append(grid);
  } else evidence.append(element("p", "evidence-empty", "Кадры для этого события не получены."));
  eventDetail.append(evidence);
  const rule = element("section", "detail-block");
  rule.append(element("span", "detail-label", "НОРМАТИВНОЕ ОСНОВАНИЕ"));
  const box = element("div", "rule-box");
  box.append(element("p", "rule-title", [event.rule?.title, event.rule?.clause].filter(Boolean).join(" · ") || "Источник правила не указан."));
  if (event.rule?.source_url) {
    try {
      const url = new URL(event.rule.source_url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("Unsupported protocol");
      const link = element("a", "rule-link", "Открыть источник ↗");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      box.append(link);
    } catch (_) { /* The title remains useful when a URL is malformed. */ }
  }
  rule.append(box);
  const legalNote = element("p", "legal-reference-note", "Здесь приведена нормативная ссылка, сохранённая с результатом. Редакция справки при анализе не записывалась; применимость нормы нужно сверять с датой и условиями операции.");
  const legalLink = element("a", "rule-link", "Открыть правовую справку →");
  legalLink.href = "#rules";
  rule.append(legalNote, legalLink);
  eventDetail.append(rule);
}

function renderResult(result) {
  clearResult();
  currentResult = result;
  const download = createJsonDownload(result, session.state.jobId);
  resultDownloadUrl = download.url;
  byId("export-result").href = download.url;
  byId("export-result").download = download.filename;
  resultsPanel.hidden = false;
  const events = Array.isArray(result.violations) ? result.violations : [];
  const badge = byId("verdict-badge");
  const summary = byId("results-summary");
  const noEvents = byId("no-events");
  const scope = result.analysis_scope;
  const scopeNote = byId("analysis-scope");
  scopeNote.hidden = false;
  scopeNote.dataset.complete = String(Boolean(scope?.full_video_checked));
  scopeNote.textContent = describeAnalysisScope(scope);
  if (result.verdict === "violation" && events.length) {
    currentEvents = events.slice().sort((a, b) => a.timecode_sec - b.timecode_sec);
    resultsTitle.textContent = "Найденные эпизоды";
    summary.textContent = "Событий: " + events.length + ". Выберите эпизод, чтобы проверить запись и правило.";
    badge.textContent = "ТРЕБУЕТ ПРОВЕРКИ";
    badge.dataset.verdict = "violation";
    byId("event-count").textContent = String(events.length).padStart(2, "0");
    byId("events-layout").hidden = false;
    currentEvents.forEach((event, index) => {
      const button = element("button", "event-button");
      button.type = "button";
      button.dataset.index = String(index);
      button.setAttribute("aria-pressed", "false");
      button.append(element("span", "event-button-number", String(index + 1).padStart(2, "0")));
      const copy = element("span", "event-button-copy");
      copy.append(element("span", "event-button-title", event.type || "Событие"));
      copy.append(element("span", "event-button-time", formatTime(event.timecode_sec)));
      button.append(copy);
      button.addEventListener("click", () => renderEventDetail(index));
      eventIndex.append(button);
    });
    renderEventDetail(0);
  } else {
    noEvents.hidden = false;
    const clear = result.verdict === "no_violation" && events.length === 0 && scope?.full_video_checked === true;
    if (!clear) scopeNote.hidden = true;
    resultsTitle.textContent = clear ? "Результат проверки" : "Результат недоступен";
    badge.textContent = clear ? "БЕЗ СОБЫТИЙ" : "НЕТ ДАННЫХ";
    badge.dataset.verdict = clear ? "no_violation" : "unknown";
    noEvents.dataset.state = clear ? "clear" : "unknown";
    summary.textContent = clear ? "Запись проверена по сохранённому набору процедур." : "Сервис вернул неполный ответ.";
    noEvents.querySelector("h3").textContent = clear ? "По проверяемым процедурам нарушений не обнаружено" : "Не удалось показать результат";
    noEvents.querySelector("p").textContent = clear ? "Вывод относится к указанным выше сохранённым процедурам этой проверки. Другие действия требуют отдельной оценки по исходной записи." : "Повторите проверку или изучите ответ API.";
    noEvents.querySelector(".no-events-icon").textContent = clear ? "✓" : "!";
  }
  byId("result-announcement").textContent = "Проверка завершена. " + summary.textContent;
  preparePrintReport(result);
  if (!byId("view-analysis").hidden) resultsTitle.focus();
}

async function restoreJob() {
  if (session.state.busy) return;
  session.setKey(keyInput.value);
  const id = byId("resume-job").value.trim();
  const opened = await session.openJob(id);
  if (!opened && session.state.pendingJob) {
    byId("connection-feedback").textContent = "Введите ключ доступа, чтобы открыть сохранённую проверку.";
    byId("connection-feedback").hidden = false;
    keyInput.focus();
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (session.state.busy) return;
  setError("");
  session.setKey(keyInput.value);
  try { await session.start(player.video.duration); }
  catch (error) { if (error.name !== "AbortError") setError(error.message); }
});

fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));
keyInput.addEventListener("input", () => {
  setError("");
  session.setKey(keyInput.value);
});
byId("check-service").addEventListener("click", async () => {
  try { await checkSystem(); }
  catch (error) { if (error.name !== "AbortError") keyInput.focus(); }
});
byId("forget-key").addEventListener("click", () => {
  if (!session.forget()) return;
  keyInput.value = "";
  keyInput.type = "password";
  byId("key-visibility").textContent = "Показать";
  byId("key-visibility").setAttribute("aria-label", "Показать ключ");
  byId("key-visibility").setAttribute("aria-pressed", "false");
  byId("connection-feedback").textContent = "Ключ удалён из текущей страницы. Для продолжения подключитесь заново.";
  byId("connection-feedback").hidden = false;
  keyInput.focus();
});
keyInput.addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    if (!byId("check-service").disabled) byId("check-service").click();
  }
});
byId("key-visibility").addEventListener("click", () => {
  const visible = keyInput.type === "password";
  keyInput.type = visible ? "text" : "password";
  byId("key-visibility").textContent = visible ? "Скрыть" : "Показать";
  byId("key-visibility").setAttribute("aria-label", visible ? "Скрыть ключ" : "Показать ключ");
  byId("key-visibility").setAttribute("aria-pressed", String(visible));
});
dropzone.addEventListener("dragover", event => {
  event.preventDefault();
  if (!session.state.busy) dropzone.classList.add("is-dragover");
});
dropzone.addEventListener("dragleave", event => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("is-dragover");
});
dropzone.addEventListener("drop", event => {
  event.preventDefault();
  dropzone.classList.remove("is-dragover");
  if (session.state.busy) return;
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  chooseFile(file);
});
byId("retry-button").addEventListener("click", () => session.refreshJob());
byId("load-source").addEventListener("click", ensureSource);
byId("resume-button").addEventListener("click", restoreJob);
byId("resume-job").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); restoreJob(); }
});
byId("copy-result").addEventListener("click", async () => {
  if (!currentResult) return;
  const json = JSON.stringify(currentResult, null, 2);
  try {
    await navigator.clipboard.writeText(json);
    byId("copy-result").textContent = "JSON скопирован";
    byId("result-announcement").textContent = "JSON результата скопирован.";
    window.setTimeout(() => { byId("copy-result").textContent = "Скопировать JSON"; }, 2500);
  } catch (_) {
    const panel = byId("json-inspector");
    panel.hidden = false;
    panel.open = true;
    byId("result-json").value = json;
    byId("result-json").focus();
    byId("result-json").select();
  }
});
byId("copy-job").addEventListener("click", async () => {
  const url = session.jobLink();
  try {
    await navigator.clipboard.writeText(url);
    byId("copy-job").textContent = "Ссылка скопирована";
    window.setTimeout(() => { byId("copy-job").textContent = "Скопировать ссылку на проверку"; }, 2500);
  } catch (_) {
    const input = byId("job-link");
    input.hidden = false;
    input.value = url;
    input.focus();
    input.select();
  }
});

new ServiceWorkspace(session);
session.subscribe((state, previous) => {
  setLocked(state.busy);
  byId("check-service").disabled = state.busy || state.connecting;
  byId("resume-job").value = state.pendingJob || state.jobId || "";
  byId("copy-job").hidden = !state.jobId;
  byId("upload-uncertain").hidden = !state.uploadUncertain;
  if (state.jobId !== previous?.jobId) byId("job-link").hidden = true;

  const changedSource = !previous || state.selectedFile !== previous.selectedFile || state.jobId !== previous.jobId;
  if (changedSource) {
    clearResult();
    const file = state.selectedFile;
    if (!file) fileInput.value = "";
    dropzone.dataset.selected = String(Boolean(file));
    byId("file-label").textContent = file ? file.name : "Выбрать видео";
    byId("file-hint").textContent = file ? formatSize(file.size) + " · нажмите, чтобы заменить" : "или перетащить файл сюда";
    if (file) player.setBlob(file, file.name);
    else if (state.jobId) player.setPending(filename());
    else player.clear();
  } else if (state.jobId && !player.url && state.job?.metadata?.filename !== previous?.job?.metadata?.filename) {
    player.setPending(filename());
  }
  startedAt = Number.isFinite(Date.parse(state.job?.metadata?.created_at)) ? Date.parse(state.job.metadata.created_at) : state.startedAt;
  clearInterval(elapsedTimer);
  if (state.status === "idle") {
    statusContent.hidden = true;
    statusEmpty.hidden = Boolean(state.selectedFile);
    const title = statusEmpty.querySelector("h3");
    const copy = statusEmpty.querySelector("p");
    title.textContent = state.pendingJob ? "Подключитесь, чтобы открыть проверку" : state.page === "review" ? "Проверка не выбрана" : "Добавьте запись для проверки";
    copy.textContent = state.pendingJob ? "Для доступа к сохранённому результату нужен ключ. Перейдите в раздел подключения." : state.page === "review" ? "Откройте запись в журнале или создайте новую проверку." : "После выбора файла здесь появится видео. Результат сохранится в журнале автоматически.";
  } else {
    const notes = {
      preparing: "Проверяем доступность сервиса и ограничения записи.",
      uploading: "Передаём файл сервису. Параметры видео проверит сервер.",
      loading: "Запрашиваем сохранённую задачу…",
      processing: "Сервис проверяет запись. Результат появится автоматически.",
      done: "Результат готов. Сверьте каждый эпизод с исходной записью.",
      failed: state.job?.status === "failed" ? failureMessage(state.job.error) : state.error?.message || "Проверка не выполнена.",
      connection: state.pollRetry
        ? state.pollRetry.exhausted
          ? "Автоматическое восстановление не удалось. Обновите статус вручную; новая загрузка не требуется. Проверка на сервере может продолжаться."
          : state.pollRetry.delayMs > 0
            ? `Связь прервана. Автоматическая попытка ${state.pollRetry.attempt}/${state.pollRetry.maxAttempts} через ${state.pollRetry.delayMs / 1000} с. Проверка на сервере может продолжаться.`
            : `Запрашиваем статус, попытка ${state.pollRetry.attempt}/${state.pollRetry.maxAttempts}.`
        : (state.error?.message || "Статус временно недоступен.") + " Обновите статус вручную.",
    };
    setStatus(state.status === "loading" ? "processing" : state.status, notes[state.status] || "");
    if (state.uploadUncertain) byId("status-label").textContent = "Создание задачи не подтверждено";
    byId("retry-button").hidden = state.status !== "connection";
    byId("retry-button").textContent = state.pollRetry && !state.pollRetry.exhausted ? "Обновить сейчас" : "Обновить статус";
    if (state.status === "uploading") byId("submit-label").textContent = "Загружаем видео…";
    if (state.status === "done" && state.job !== previous?.job) renderResult(state.job);
  }
  if (state.system) {
    const info = state.system;
    byId("service-note").hidden = false;
    byId("service-note").textContent = readinessMessage(info);
    const limitText = ["MOV"];
    if (info.limits?.max_upload_bytes) limitText.push("до " + formatSize(info.limits.max_upload_bytes));
    if (info.limits?.max_video_duration_sec) limitText.push("до " + formatTime(info.limits.max_video_duration_sec));
    byId("file-help").textContent = limitText.join(" · ");
  }
  if (state.error !== previous?.error) setError(state.error?.message || "", null);
  if (state.pendingJob) byId("resume-panel").open = true;
});

function preparePrintReport(result) {
  let report = byId("print-all-events");
  if (!report) {
    report = element("section", "print-only");
    report.id = "print-all-events";
    resultsPanel.append(report);
  }
  report.replaceChildren();
  report.append(element("h2", "", "Кассовый контроль · Отчёт по проверке"));
  report.append(element("p", "", "Вердикт: " + (result.verdict === "violation" ? "обнаружены события, требующие проверки" : result.verdict === "no_violation" && result.analysis_scope?.full_video_checked ? "по сохранённому набору процедур нарушений не обнаружено" : "результат требует уточнения")));
  report.append(element("p", "print-scope", describeAnalysisScope(result.analysis_scope)));
  const events = Array.isArray(result.violations) ? result.violations.slice().sort((a, b) => a.timecode_sec - b.timecode_sec) : [];
  for (const [index, event] of events.entries()) {
    const article = element("article", "print-event");
    article.append(element("h3", "", `${index + 1}. ${event.type} · ${formatTime(event.timecode_sec)}`));
    article.append(element("p", "", event.description || ""));
    const frames = element("div", "print-frames");
    for (const frame of (event.evidence_frames || []).filter(validFrame).slice(0, 3)) {
      const figure = element("figure");
      const image = element("img");
      image.src = "data:image/jpeg;base64," + frame.image_base64;
      image.alt = "Кадр на " + formatTime(frame.timestamp_sec);
      figure.append(image, element("figcaption", "", formatTime(frame.timestamp_sec)));
      frames.append(figure);
    }
    article.append(frames);
    article.append(element("p", "", [event.rule?.title, event.rule?.clause].filter(Boolean).join(" · ")));
    if (event.rule?.source_url) article.append(element("p", "print-reference", event.rule.source_url));
    report.append(article);
  }
  report.append(element("p", "", "Вывод относится к сохранённому набору процедур этой проверки. Сверьте найденные эпизоды с исходной видеозаписью. Нормативные ссылки сохранены с результатом; редакция справки при анализе не записывалась. Проверьте применимость нормы на дату операции."));
}
byId("print-result").addEventListener("click", async () => {
  if (!currentResult) return;
  const button = byId("print-result");
  button.disabled = true;
  try {
    const images = [...byId("print-all-events").querySelectorAll("img")];
    await Promise.allSettled(images.map(image => image.decode()));
    window.print();
  } finally { button.disabled = false; }
});
window.addEventListener("beforeunload", () => {
  if (resultDownloadUrl) URL.revokeObjectURL(resultDownloadUrl);
  session.dispose();
  player.clear();
});
