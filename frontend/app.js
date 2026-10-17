import { getSystem, getJob, startAnalysis, getSource } from "./api.js";
import { SourcePlayer, EvidenceViewer, formatTime, formatSize, validFrame, createJsonDownload } from "./media.js";
import { ServiceWorkspace } from "./service.js";

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
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

let activeJobId = null;
let activeKey = null;
let activeMetadata = {};
let activeFilename = "";
let currentEvents = [];
let currentResult = null;
let pollTimer = null;
let elapsedTimer = null;
let startedAt = null;
let runNumber = 0;
let locked = false;
let sourceRequest = null;
let resultDownloadUrl = null;
let workspace = null;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setError(message, focusTarget = formError) {
  formError.textContent = message;
  formError.hidden = !message;
  if (message) focusTarget.focus();
}

function setLocked(value) {
  locked = value;
  fileInput.disabled = value;
  keyInput.disabled = value;
  submitButton.disabled = value;
  byId("resume-button").disabled = value;
  byId("resume-job").disabled = value;
  byId("check-service").disabled = value;
  dropzone.dataset.locked = String(value);
  dropzone.setAttribute("aria-disabled", String(value));
  byId("submit-label").textContent = value ? "Идёт проверка…" : "Начать проверку";
  workspace?.updateAccess();
}

function jobLink(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("job", id);
  else url.searchParams.delete("job");
  if (id) url.hash = "review";
  return url;
}

function rememberJob(id) {
  history.replaceState(null, "", jobLink(id));
  byId("resume-job").value = id || "";
  byId("copy-job").hidden = !id;
  byId("job-link").hidden = true;
  if (id) workspace?.route();
}

function resetReview() {
  runNumber += 1;
  clearTimeout(pollTimer);
  clearInterval(elapsedTimer);
  sourceRequest?.abort();
  activeJobId = null;
  activeKey = null;
  activeMetadata = {};
  startedAt = null;
  statusContent.hidden = true;
  clearResult();
  rememberJob(null);
}

function clearResult() {
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
  if (locked) return;
  resetReview();
  activeFilename = file?.name || "";
  dropzone.dataset.selected = String(Boolean(file));
  byId("file-label").textContent = file ? file.name : "Выбрать видео";
  byId("file-hint").textContent = file ? formatSize(file.size) + " · нажмите, чтобы заменить" : "или перетащить файл сюда";
  statusEmpty.hidden = Boolean(file);
  if (file) player.setBlob(file, file.name);
  else player.clear();
  setError("");
}

function readinessMessage(info) {
  const reasons = [];
  if (!info.analyzer?.configured) reasons.push("локальная модель ещё не подключена");
  if (!info.analyzer?.rules_ready) reasons.push("правила проверки ещё не активированы");
  if (!info.tools?.ffmpeg || !info.tools?.ffprobe) reasons.push("инструменты обработки видео недоступны");
  if (reasons.length) return "Сервис пока не готов: " + reasons.join("; ") + ". Предпросмотр видео доступен.";
  if (info.busy) return "Сервис обрабатывает другую запись. Повторите загрузку после её завершения.";
  if (!info.ready) return "Сервис пока не готов к обработке. Проверьте настройки сервера.";
  const names = info.analyzer?.rules?.map(rule => rule.event_type).filter(Boolean);
  return names?.length ? "Проверяем: " + names.join("; ") + "." : "Сервис готов. Проверяемых правил: " + (info.analyzer?.enabled_rule_ids?.length || 0) + ".";
}

async function checkSystem(key) {
  if (!key) throw new Error("Введите ключ доступа.");
  const checkButton = byId("check-service");
  checkButton.disabled = true;
  byId("connection-state").textContent = "Проверяем…";
  try {
    const info = await getSystem(key);
    if (key !== keyInput.value.trim()) return info;
    byId("connection-state").textContent = info.ready ? info.busy ? "Занят" : "Готов" : "Не готов";
    byId("connection-state").dataset.ready = String(info.ready && !info.busy);
    byId("service-note").hidden = false;
    byId("service-note").textContent = readinessMessage(info);
    const limits = info.limits || {};
    const limitText = ["MOV"];
    if (limits.max_upload_bytes) limitText.push("до " + formatSize(limits.max_upload_bytes));
    if (limits.max_video_duration_sec) limitText.push("до " + formatTime(limits.max_video_duration_sec));
    byId("file-help").textContent = limitText.join(" · ");
    workspace?.setConnection(info, key);
    return info;
  } catch (error) {
    byId("connection-state").textContent = "Нет подключения";
    byId("connection-state").dataset.ready = "false";
    byId("service-note").hidden = true;
    workspace?.feedback("connection-feedback", error.message, "error");
    throw error;
  } finally {
    checkButton.disabled = locked;
  }
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

function setStatus(status, note, metadata = activeMetadata) {
  const labels = {
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
  byId("status-file").textContent = metadata.filename || activeFilename || "—";
  byId("status-job").textContent = activeJobId || (status === "uploading" ? "Создаётся…" : "—");
  byId("status-note").textContent = note;
  byId("retry-button").hidden = true;
  byId("copy-job").hidden = !activeJobId;
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
  if (!activeJobId || !activeKey) return false;
  sourceRequest?.abort();
  sourceRequest = new AbortController();
  const expectedRun = runNumber;
  const button = byId("load-source");
  button.disabled = true;
  button.textContent = "Загружаем исходное видео…";
  try {
    const blob = await getSource(activeJobId, activeKey, sourceRequest.signal);
    if (expectedRun !== runNumber) return false;
    player.setBlob(blob, activeMetadata.filename || activeFilename);
    return true;
  } catch (error) {
    if (error.name !== "AbortError" && expectedRun === runNumber) byId("source-note").textContent = error.message;
    return false;
  } finally {
    button.disabled = false;
    button.textContent = "Загрузить исходное видео для просмотра";
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
  eventDetail.append(rule);
}

function renderResult(result) {
  clearResult();
  currentResult = result;
  const download = createJsonDownload(result, activeJobId);
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
  const limitations = Array.isArray(scope?.limitations) ? scope.limitations.filter(item => typeof item === "string") : [];
  scopeNote.textContent = scope?.full_video_checked
    ? "Запись полностью проверена по активным правилам. Правил: " + (Array.isArray(scope.checked_rule_ids) ? scope.checked_rule_ids.length : 0) + "."
    : "Показаны обнаруженные события. Полная проверка всех процедур не завершена." + (limitations.length ? " Ограничения: " + limitations.join("; ") : "");
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
    summary.textContent = clear ? "Запись проверена по активным правилам сервиса." : "Сервис вернул неполный ответ.";
    noEvents.querySelector("h3").textContent = clear ? "По проверяемым процедурам нарушений не обнаружено" : "Не удалось показать результат";
    noEvents.querySelector("p").textContent = clear ? "Этот вывод относится к подключённым правилам. Другие процедуры и действия требуют отдельной оценки по исходной записи." : "Повторите проверку или изучите ответ API.";
    noEvents.querySelector(".no-events-icon").textContent = clear ? "✓" : "!";
  }
  byId("result-announcement").textContent = "Проверка завершена. " + summary.textContent;
  preparePrintReport(result);
  if (!byId("view-analysis").hidden) resultsTitle.focus();
}

async function pollJob(expectedRun = runNumber, restoring = false) {
  clearTimeout(pollTimer);
  if (!activeJobId || !activeKey) return;
  const id = activeJobId;
  try {
    const job = await getJob(id, activeKey);
    if (expectedRun !== runNumber || id !== activeJobId) return;
    activeMetadata = job.metadata || {};
    activeFilename = activeMetadata.filename || activeFilename;
    if (restoring && !player.url) player.setPending(activeFilename);
    if (job.status === "processing") {
      setLocked(true);
      setStatus("processing", "Сервис проверяет запись. Результат появится автоматически.");
      pollTimer = window.setTimeout(() => pollJob(expectedRun), 2000);
    } else if (job.status === "done") {
      setLocked(false);
      setStatus("done", "Результат готов. Сверьте каждый эпизод с исходной записью.");
      renderResult(job);
      workspace?.jobUpdated();
    } else if (job.status === "failed") {
      setLocked(false);
      setStatus("failed", failureMessage(job.error));
      workspace?.jobUpdated();
    } else throw new Error("Сервис вернул неизвестное состояние задачи.");
  } catch (error) {
    if (expectedRun !== runNumber || id !== activeJobId) return;
    setLocked(false);
    setStatus("connection", error.message + " Повторите запрос статуса.");
    byId("retry-button").hidden = false;
  }
}

async function restoreJob() {
  if (locked) return;
  const id = byId("resume-job").value.trim().toLowerCase();
  const key = keyInput.value.trim();
  if (!uuidPattern.test(id)) return setError("Введите полный номер задачи в формате UUID.", byId("resume-job"));
  if (!key) {
    workspace?.navigate("connection");
    workspace?.feedback("connection-feedback", "Введите ключ доступа, чтобы открыть сохранённую проверку.");
    keyInput.focus();
    return;
  }
  resetReview();
  fileInput.value = "";
  byId("file-label").textContent = "Выбрать видео";
  byId("file-hint").textContent = "или перетащить файл сюда";
  dropzone.dataset.selected = "false";
  player.clear();
  activeFilename = "";
  activeJobId = id;
  activeKey = key;
  rememberJob(id);
  setError("");
  setLocked(true);
  setStatus("processing", "Запрашиваем сохранённую задачу…");
  await pollJob(runNumber, true);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (locked) return;
  setError("");
  const file = fileInput.files[0];
  const key = keyInput.value.trim();
  if (!file) return setError("Выберите видео в формате .mov.", fileInput);
  if (!file.name.toLowerCase().endsWith(".mov")) return setError("Поддерживаются только файлы .mov.", fileInput);
  if (file.size === 0) return setError("Файл пустой. Выберите другую запись.", fileInput);
  if (!key) {
    workspace?.navigate("connection");
    workspace?.feedback("connection-feedback", "Подключитесь к сервису, затем вернитесь к выбранной записи.");
    keyInput.focus();
    return;
  }
  setLocked(true);
  let uploadStarted = false;
  try {
    const info = await checkSystem(key);
    if (!info.ready || info.busy) throw new Error(readinessMessage(info));
    if (file.size > info.limits.max_upload_bytes) throw new Error("Файл больше " + formatSize(info.limits.max_upload_bytes) + ". Выберите запись меньшего размера.");
    const duration = player.video.duration;
    if (Number.isFinite(duration) && duration > info.limits.max_video_duration_sec) throw new Error("Запись длиннее допустимого лимита " + formatTime(info.limits.max_video_duration_sec) + ".");
    resetReview();
    activeKey = key;
    activeFilename = file.name;
    startedAt = Date.now();
    uploadStarted = true;
    setStatus("uploading", "Передаём файл сервису. Параметры видео проверит сервер.");
    byId("submit-label").textContent = "Загружаем видео…";
    const expectedRun = runNumber;
    const job = await startAnalysis(file, key);
    if (expectedRun !== runNumber) return;
    if (!uuidPattern.test(job.job_id) || job.status !== "processing") throw new Error("Сервис не создал задачу. Повторите загрузку.");
    activeJobId = job.job_id;
    activeMetadata = job.metadata || {};
    rememberJob(activeJobId);
    setStatus("processing", "Файл загружен. Сервис проверяет запись.");
    pollJob(expectedRun);
  } catch (error) {
    setLocked(false);
    if (uploadStarted) setStatus("failed", error.message);
    setError(error.message);
  }
});

fileInput.addEventListener("change", () => chooseFile(fileInput.files[0]));
keyInput.addEventListener("input", () => {
  setError("");
  workspace?.invalidate();
  byId("connection-state").textContent = "Не проверен";
  byId("service-note").hidden = true;
  byId("connection-state").removeAttribute("data-ready");
});
byId("check-service").addEventListener("click", async () => {
  try { await checkSystem(keyInput.value.trim()); }
  catch (error) { workspace?.feedback("connection-feedback", error.message, "error"); keyInput.focus(); }
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
  if (!locked) dropzone.classList.add("is-dragover");
});
dropzone.addEventListener("dragleave", event => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("is-dragover");
});
dropzone.addEventListener("drop", event => {
  event.preventDefault();
  dropzone.classList.remove("is-dragover");
  if (locked) return;
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  chooseFile(file);
});
byId("retry-button").addEventListener("click", () => {
  const key = keyInput.value.trim();
  if (!key) { workspace?.navigate("connection"); keyInput.focus(); return; }
  activeKey = key;
  pollJob();
});
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
  const url = jobLink(activeJobId).href;
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

workspace = new ServiceWorkspace({
  getKey: () => keyInput.value.trim(),
  isBusy: () => locked,
  getActiveJob: () => activeJobId,
  onOpenJob: async id => {
    byId("resume-job").value = id;
    await restoreJob();
  },
  onNewCheck: () => {
    if (!activeJobId && fileInput.files.length) return;
    fileInput.value = "";
    chooseFile(null);
    statusEmpty.querySelector("h3").textContent = "Добавьте запись для проверки";
    statusEmpty.querySelector("p").textContent = "После выбора файла здесь появится видео. Результат сохранится в журнале автоматически.";
  },
  onDisconnect: () => {
    fileInput.value = "";
    chooseFile(null);
  },
});
const savedJob = new URL(location.href).searchParams.get("job");
if (savedJob && uuidPattern.test(savedJob) && !workspace.connected) {
  statusEmpty.querySelector("h3").textContent = "Подключитесь, чтобы открыть проверку";
  const copy = statusEmpty.querySelector("p");
  copy.textContent = "Для доступа к сохранённому результату нужен ключ. ";
  const link = element("a", "", "Подключиться к сервису →");
  link.href = "#connection";
  copy.append(link);
}

function preparePrintReport(result) {
  let report = byId("print-all-events");
  if (!report) {
    report = element("section", "print-only");
    report.id = "print-all-events";
    resultsPanel.append(report);
  }
  report.replaceChildren();
  report.append(element("h2", "", "Кассовый контроль · Отчёт по проверке"));
  report.append(element("p", "", "Вердикт: " + (result.verdict === "violation" ? "обнаружены события, требующие проверки" : result.verdict === "no_violation" && result.analysis_scope?.full_video_checked ? "по активным процедурам нарушений не обнаружено" : "результат требует уточнения")));
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
  report.append(element("p", "", "Вывод относится к активным процедурам сервиса. Сверьте найденные эпизоды с исходной видеозаписью."));
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
  sourceRequest?.abort();
  player.clear();
});
