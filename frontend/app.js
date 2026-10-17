const form = document.querySelector("#analyze-form");
const keyInput = document.querySelector("#api-key");
const keyVisibility = document.querySelector("#key-visibility");
const fileInput = document.querySelector("#video-file");
const dropzone = document.querySelector("#dropzone");
const fileLabel = document.querySelector("#file-label");
const fileHint = document.querySelector("#file-hint");
const formError = document.querySelector("#form-error");
const submitButton = document.querySelector("#submit-button");
const submitLabel = document.querySelector("#submit-label");
const statusEmpty = document.querySelector("#status-empty");
const statusContent = document.querySelector("#status-content");
const statusMain = document.querySelector(".status-main");
const statusIndicator = document.querySelector("#status-indicator");
const statusLabel = document.querySelector("#status-label");
const statusFile = document.querySelector("#status-file");
const statusJob = document.querySelector("#status-job");
const statusNote = document.querySelector("#status-note");
const retryButton = document.querySelector("#retry-button");
const resultsPanel = document.querySelector("#results-panel");
const resultsTitle = document.querySelector("#results-title");
const resultsSummary = document.querySelector("#results-summary");
const verdictBadge = document.querySelector("#verdict-badge");
const noEvents = document.querySelector("#no-events");
const eventsLayout = document.querySelector("#events-layout");
const eventCount = document.querySelector("#event-count");
const eventIndex = document.querySelector("#event-index");
const eventDetail = document.querySelector("#event-detail");
const resultAnnouncement = document.querySelector("#result-announcement");

const MAX_FILE_BYTES = 128 * 1024 * 1024;
const fileSizeFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });
let activeJobId = null;
let activeKey = null;
let pollTimer = null;
let runNumber = 0;
let currentEvents = [];

function setError(message, focusTarget = formError) {
  formError.textContent = message;
  formError.hidden = !message;
  if (message) focusTarget.focus();
}

function formatTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  const tenths = Math.round(value * 10);
  const minutes = Math.floor(tenths / 600);
  const remaining = ((tenths % 600) / 10).toFixed(1).padStart(4, "0");
  return `${String(minutes).padStart(2, "0")}:${remaining}`;
}

function makeElement(tag, className, content) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  return element;
}

function showFile(file) {
  dropzone.dataset.selected = file ? "true" : "false";
  fileLabel.textContent = file ? file.name : "Выбрать видео";
  fileHint.textContent = file
    ? `${fileSizeFormatter.format(file.size / 1024 / 1024)} МБ · нажмите, чтобы заменить`
    : "или перетащить файл сюда";
  setError("");
}

function clearResult() {
  resultsPanel.hidden = true;
  eventsLayout.hidden = true;
  noEvents.hidden = true;
  currentEvents = [];
  eventIndex.replaceChildren();
  eventDetail.replaceChildren();
  resultAnnouncement.textContent = "";
}

function setStatus(status, filename, jobId, note) {
  const labels = {
    uploading: "Загружаем видео…",
    processing: "Обрабатываем запись…",
    done: "Проверка завершена",
    failed: "Проверка не выполнена",
    connection: "Нет связи с сервисом",
  };
  statusEmpty.hidden = true;
  statusContent.hidden = false;
  statusMain.dataset.status = status;
  statusIndicator.dataset.status = status;
  statusLabel.textContent = labels[status] || "Состояние неизвестно";
  statusFile.textContent = filename || "—";
  statusJob.textContent = jobId || (status === "uploading" ? "Создаётся…" : "—");
  statusNote.textContent = note || "";
  retryButton.hidden = true;
}

function friendlyFailure(error) {
  if (error?.code === "analyzer_unavailable") {
    return "Локальный анализатор ещё не подключён. Загрузка и API работают; для поиска событий нужна модель.";
  }
  if (error?.code === "evidence_extraction_failed") {
    return "Не удалось извлечь кадры из записи. Проверьте исходный файл и повторите проверку.";
  }
  if (error?.code === "analysis_failed") {
    return "Локальный анализ завершился с ошибкой. Попробуйте другую запись или проверьте журнал сервера.";
  }
  return error?.message || "Не удалось обработать запись. Повторите проверку.";
}

async function readError(response) {
  if (response.status === 401) return "Неверный ключ доступа. Проверьте X-API-Key.";
  if (response.status === 413) return "Файл превышает допустимый размер. Выберите видео до 128 МБ.";
  if (response.status === 415) return "Поддерживаются только файлы .mov.";
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.error?.message === "string") return body.error.message;
  } catch (_) {
    // The service may return a non-JSON error page.
  }
  return `Сервис вернул ошибку ${response.status}. Повторите запрос.`;
}

function validFrame(frame) {
  return frame && typeof frame.image_base64 === "string"
    && frame.image_base64.length > 0
    && /^[A-Za-z0-9+/]+={0,2}$/.test(frame.image_base64);
}

function renderEventDetail(index) {
  const event = currentEvents[index];
  if (!event) return;
  for (const button of eventIndex.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.index) === index));
  }
  eventDetail.replaceChildren();

  const heading = makeElement("div", "detail-heading");
  const headingCopy = makeElement("div");
  headingCopy.append(makeElement("span", "overline", `ЭПИЗОД ${String(index + 1).padStart(2, "0")}`));
  headingCopy.append(makeElement("h3", "", event.type || "Событие"));
  heading.append(headingCopy, makeElement("span", "time-chip", formatTime(event.timecode_sec)));
  eventDetail.append(heading);

  const description = makeElement("section", "detail-block");
  description.append(makeElement("span", "detail-label", "ОПИСАНИЕ СОБЫТИЯ"));
  description.append(makeElement("p", "event-description", event.description || "Описание не получено."));
  eventDetail.append(description);

  const evidence = makeElement("section", "detail-block");
  evidence.append(makeElement("span", "detail-label", "КАДРЫ ИЗ ИСХОДНОГО ВИДЕО"));
  const frames = Array.isArray(event.evidence_frames) ? event.evidence_frames.filter(validFrame).slice(0, 3) : [];
  if (frames.length) {
    const grid = makeElement("div", "evidence-grid");
    grid.dataset.count = String(frames.length);
    for (const frame of frames) {
      const figure = makeElement("figure", "evidence-item");
      const image = document.createElement("img");
      image.width = 464;
      image.height = 848;
      image.loading = "lazy";
      image.alt = `Кадр исходного видео на ${formatTime(frame.timestamp_sec)}`;
      image.addEventListener("error", () => {
        image.replaceWith(makeElement("span", "evidence-empty", "Кадр не удалось показать."));
      }, { once: true });
      image.src = `data:image/jpeg;base64,${frame.image_base64}`;
      figure.append(image, makeElement("figcaption", "", formatTime(frame.timestamp_sec)));
      grid.append(figure);
    }
    evidence.append(grid);
  } else {
    evidence.append(makeElement("p", "evidence-empty", "Кадры для этого события не получены."));
  }
  eventDetail.append(evidence);

  const rule = makeElement("section", "detail-block");
  rule.append(makeElement("span", "detail-label", "НОРМАТИВНОЕ ОСНОВАНИЕ"));
  const ruleBox = makeElement("div", "rule-box");
  const ruleTitle = [event.rule?.title, event.rule?.clause].filter(Boolean).join(" · ");
  ruleBox.append(makeElement("p", "rule-title", ruleTitle || "Источник правила не указан."));
  if (event.rule?.source_url) {
    try {
      const url = new URL(event.rule.source_url);
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("Unsupported protocol");
      const link = makeElement("a", "rule-link", "Открыть источник ↗");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      ruleBox.append(link);
    } catch (_) {
      // Keep the rule title visible even when its URL is invalid.
    }
  }
  rule.append(ruleBox);
  eventDetail.append(rule);
}

function renderResult(result) {
  clearResult();
  resultsPanel.hidden = false;
  const events = Array.isArray(result?.violations) ? result.violations : [];

  if (result?.verdict === "violation" && events.length) {
    currentEvents = events;
    resultsTitle.textContent = "Найденные эпизоды";
    resultsSummary.textContent = `Событий: ${events.length}. Выберите эпизод, чтобы изучить кадры и правило.`;
    verdictBadge.textContent = "ТРЕБУЕТ ПРОВЕРКИ";
    verdictBadge.dataset.verdict = "violation";
    eventCount.textContent = String(events.length).padStart(2, "0");
    eventsLayout.hidden = false;
    events.forEach((event, index) => {
      const button = makeElement("button", "event-button");
      button.type = "button";
      button.dataset.index = String(index);
      button.setAttribute("aria-pressed", "false");
      button.append(makeElement("span", "event-button-number", String(index + 1).padStart(2, "0")));
      const copy = makeElement("span", "event-button-copy");
      copy.append(makeElement("span", "event-button-title", event.type || "Событие"));
      copy.append(makeElement("span", "event-button-time", formatTime(event.timecode_sec)));
      button.append(copy);
      button.addEventListener("click", () => renderEventDetail(index));
      eventIndex.append(button);
    });
    renderEventDetail(0);
    resultAnnouncement.textContent = `Проверка завершена. Найдено событий: ${events.length}.`;
  } else if (result?.verdict === "no_violation" && events.length === 0) {
    resultsTitle.textContent = "Результат проверки";
    resultsSummary.textContent = "Проверка по подключённым правилам завершена.";
    verdictBadge.textContent = "БЕЗ СОБЫТИЙ";
    verdictBadge.dataset.verdict = "no_violation";
    noEvents.hidden = false;
    noEvents.querySelector("h3").textContent = "События не обнаружены";
    noEvents.querySelector("p").textContent = "Проверка по подключённым правилам завершена. Просмотрите исходную запись, если нужна ручная оценка.";
    noEvents.querySelector(".no-events-icon").textContent = "✓";
    resultAnnouncement.textContent = "Проверка завершена. События не обнаружены.";
  } else {
    resultsTitle.textContent = "Результат недоступен";
    resultsSummary.textContent = "Сервис вернул неполный ответ.";
    verdictBadge.textContent = "НЕТ ДАННЫХ";
    verdictBadge.dataset.verdict = "unknown";
    noEvents.hidden = false;
    noEvents.querySelector("h3").textContent = "Не удалось показать результат";
    noEvents.querySelector("p").textContent = "Повторите проверку или изучите ответ API.";
    noEvents.querySelector(".no-events-icon").textContent = "!";
    resultAnnouncement.textContent = "Проверка завершена, но результат неполный.";
  }
  resultsTitle.focus();
}

async function pollJob(expectedRun = runNumber) {
  clearTimeout(pollTimer);
  if (!activeJobId || !activeKey) return;
  const jobId = activeJobId;
  const key = activeKey;
  try {
    const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
      headers: { "X-API-Key": key },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(await readError(response));
    const job = await response.json();
    if (expectedRun !== runNumber || jobId !== activeJobId) return;
    if (job.status === "processing") {
      setStatus("processing", statusFile.textContent, jobId, "Сервис проверяет запись. Результат появится автоматически.");
      pollTimer = window.setTimeout(() => pollJob(expectedRun), 2000);
    } else if (job.status === "done") {
      setStatus("done", statusFile.textContent, jobId, "Данные проверки готовы к просмотру.");
      renderResult(job);
    } else if (job.status === "failed") {
      setStatus("failed", statusFile.textContent, jobId, friendlyFailure(job.error));
    } else {
      throw new Error("Сервис вернул неизвестное состояние задачи.");
    }
  } catch (error) {
    if (expectedRun !== runNumber || jobId !== activeJobId) return;
    setStatus("connection", statusFile.textContent, jobId, `${error.message} Проверьте подключение и запросите статус снова.`);
    retryButton.hidden = false;
  }
}

fileInput.addEventListener("change", () => showFile(fileInput.files[0]));
keyInput.addEventListener("input", () => setError(""));
keyVisibility.addEventListener("click", () => {
  const visible = keyInput.type === "password";
  keyInput.type = visible ? "text" : "password";
  keyVisibility.textContent = visible ? "Скрыть" : "Показать";
  keyVisibility.setAttribute("aria-label", visible ? "Скрыть ключ" : "Показать ключ");
  keyVisibility.setAttribute("aria-pressed", String(visible));
});

dropzone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropzone.classList.add("is-dragover");
});
dropzone.addEventListener("dragleave", (event) => {
  if (!dropzone.contains(event.relatedTarget)) dropzone.classList.remove("is-dragover");
});
dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("is-dragover");
  const file = event.dataTransfer?.files?.[0];
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  showFile(file);
});

retryButton.addEventListener("click", () => pollJob());

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  const file = fileInput.files[0];
  const key = keyInput.value.trim();
  if (!file) return setError("Выберите видео в формате .mov.", fileInput);
  if (!file.name.toLowerCase().endsWith(".mov")) return setError("Поддерживаются только файлы .mov.", fileInput);
  if (file.size > MAX_FILE_BYTES) return setError("Файл больше 128 МБ. Выберите запись меньшего размера.", fileInput);
  if (file.size === 0) return setError("Файл пустой. Выберите другую запись.", fileInput);
  if (!key) return setError("Введите ключ доступа.", keyInput);

  clearTimeout(pollTimer);
  runNumber += 1;
  const expectedRun = runNumber;
  activeJobId = null;
  activeKey = key;
  clearResult();
  setStatus("uploading", file.name, null, "Передаём файл на локальный сервис.");
  submitButton.disabled = true;
  submitLabel.textContent = "Загружаем видео…";

  try {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch("/api/v1/analyze", {
      method: "POST",
      headers: { "X-API-Key": key },
      body,
    });
    if (!response.ok) throw new Error(await readError(response));
    const job = await response.json();
    if (!job.job_id || job.status !== "processing") throw new Error("Сервис не создал задачу. Повторите загрузку.");
    if (expectedRun !== runNumber) return;
    activeJobId = job.job_id;
    setStatus("processing", file.name, job.job_id, "Файл загружен. Сервис проверяет запись.");
    pollJob(expectedRun);
  } catch (error) {
    if (expectedRun !== runNumber) return;
    const message = error.message || "Не удалось загрузить файл. Повторите попытку.";
    setStatus("failed", file.name, null, message);
    setError(message);
  } finally {
    if (expectedRun === runNumber) {
      submitButton.disabled = false;
      submitLabel.textContent = "Начать проверку";
    }
  }
});
