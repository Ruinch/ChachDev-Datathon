const form = document.querySelector("#analyze-form");
const keyInput = document.querySelector("#api-key");
const fileInput = document.querySelector("#video-file");
const fileLabel = document.querySelector("#file-label");
const formError = document.querySelector("#form-error");
const submitButton = document.querySelector("#submit-button");
const statusEmpty = document.querySelector("#status-empty");
const statusContent = document.querySelector("#status-content");
const statusIndicator = document.querySelector("#status-indicator");
const statusLabel = document.querySelector("#status-label");
const statusFile = document.querySelector("#status-file");
const statusJob = document.querySelector("#status-job");
const statusNote = document.querySelector("#status-note");
const retryButton = document.querySelector("#retry-button");
const resultsPanel = document.querySelector("#results-panel");
const resultsSummary = document.querySelector("#results-summary");
const eventsList = document.querySelector("#events-list");

let activeJobId = null;
let activeKey = null;
let pollTimer = null;
const labels = {
  processing: "Обрабатывается",
  done: "Завершено",
  failed: "Не удалось обработать",
};

function setError(message) {
  formError.textContent = message;
  formError.hidden = !message;
}

function formatTime(seconds) {
  const tenths = Math.max(0, Math.round((Number(seconds) || 0) * 10));
  return `${String(Math.floor(tenths / 600)).padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
}

async function readError(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (typeof body.error?.message === "string") return body.error.message;
  } catch (_) {
    // Keep a useful message when the server does not return JSON.
  }
  return `Ошибка запроса (${response.status}).`;
}

function setStatus(job) {
  statusEmpty.hidden = true;
  statusContent.hidden = false;
  statusIndicator.dataset.status = job.status;
  statusLabel.textContent = labels[job.status] || "Состояние неизвестно";
  statusFile.textContent = job.filename || fileInput.files[0]?.name || "—";
  statusJob.textContent = job.job_id || activeJobId || "—";
  retryButton.hidden = true;

  if (job.status === "processing") statusNote.textContent = "Идёт проверка записи. Результат появится здесь автоматически.";
  if (job.status === "done") statusNote.textContent = "Проверка завершена.";
  if (job.status === "failed") statusNote.textContent = job.error?.message || "Обработка завершилась с ошибкой.";
}

function addText(parent, tag, className, value) {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  parent.append(element);
  return element;
}

function renderEvent(event) {
  const card = document.createElement("article");
  card.className = "event-card";
  const top = addText(card, "div", "event-topline", "");
  addText(top, "h3", "event-type", event.type || "Событие");
  addText(top, "span", "time-chip", formatTime(event.timecode_sec));
  addText(card, "p", "event-description", event.description || "Описание отсутствует.");

  if (event.rule) {
    const rule = addText(card, "p", "rule-line", "Источник: ");
    const title = [event.rule.title, event.rule.clause].filter(Boolean).join(", ");
    if (event.rule.source_url) {
      try {
        const url = new URL(event.rule.source_url);
        if (!["http:", "https:"].includes(url.protocol)) throw new Error("bad protocol");
        const link = addText(rule, "a", "", title || "Открыть правило");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } catch (_) {
        addText(rule, "span", "", title || "Ссылка недоступна");
      }
    } else {
      addText(rule, "span", "", title || "Источник не указан");
    }
  }

  const evidence = Array.isArray(event.evidence_frames) ? event.evidence_frames.slice(0, 3) : [];
  if (evidence.length) {
    const grid = addText(card, "div", "evidence-grid", "");
    for (const frame of evidence) {
      const figure = addText(grid, "figure", "evidence-item", "");
      const image = document.createElement("img");
      image.alt = `Кадр исходного видео на ${formatTime(frame.timestamp_sec)}`;
      image.loading = "lazy";
      figure.append(image);
      addText(figure, "figcaption", "", formatTime(frame.timestamp_sec));
      if (typeof frame.image_base64 === "string" && /^[A-Za-z0-9+/=]+$/.test(frame.image_base64)) {
        image.src = `data:image/jpeg;base64,${frame.image_base64}`;
      }
    }
  }
  eventsList.append(card);
}

function renderResult(result) {
  eventsList.replaceChildren();
  resultsPanel.hidden = false;
  const events = Array.isArray(result?.violations) ? result.violations : [];
  if (result?.verdict === "no_violation") {
    resultsSummary.textContent = "На проверенных правилах нарушения не обнаружены.";
  } else if (result?.verdict === "violation") {
    resultsSummary.textContent = `Найдено событий: ${events.length}. Проверьте каждый эпизод по исходному видео.`;
  } else {
    resultsSummary.textContent = "Сервис не вернул достоверный результат.";
  }
  for (const event of events) renderEvent(event);
}

async function pollJob() {
  if (!activeJobId || !activeKey) return;
  clearTimeout(pollTimer);
  const jobId = activeJobId;
  const key = activeKey;
  try {
    const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
      headers: { "X-API-Key": key },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(await readError(response));
    const job = await response.json();
    if (jobId !== activeJobId) return;
    setStatus(job);
    if (job.status === "done") renderResult(job);
    if (job.status === "processing") {
      pollTimer = window.setTimeout(pollJob, 2000);
    }
  } catch (error) {
    if (jobId !== activeJobId) return;
    statusNote.textContent = `Не удалось получить статус: ${error.message}`;
    retryButton.hidden = false;
  }
}

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileLabel.textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} МБ` : "Файл .mov";
  setError("");
});

retryButton.addEventListener("click", pollJob);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError("");
  const file = fileInput.files[0];
  const key = keyInput.value.trim();
  if (!key) return setError("Введите ключ доступа.");
  if (!file) return setError("Выберите видео .mov.");
  if (!file.name.toLowerCase().endsWith(".mov")) return setError("Поддерживаются файлы .mov.");

  clearTimeout(pollTimer);
  activeJobId = null;
  activeKey = key;
  resultsPanel.hidden = true;
  submitButton.disabled = true;
  submitButton.firstElementChild.textContent = "Загружается…";

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
    activeJobId = job.job_id;
    setStatus({ ...job, filename: file.name });
    pollJob();
  } catch (error) {
    setError(error.message || "Не удалось загрузить файл.");
  } finally {
    submitButton.disabled = false;
    submitButton.firstElementChild.textContent = "Начать проверку";
  }
});
