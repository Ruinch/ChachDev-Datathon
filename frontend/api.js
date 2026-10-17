export class ApiError extends Error {
  constructor(message, code, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

const messages = {
  invalid_api_key: "Неверный ключ доступа. Проверьте ключ и повторите запрос.",
  invalid_media: "Файл не удалось прочитать как видео. Выберите исходную запись .mov.",
  video_too_long: "Длительность записи превышает лимит сервиса.",
  video_duration_exceeded: "Длительность записи превышает лимит сервиса.",
  upload_too_large: "Файл превышает допустимый размер.",
  unsupported_format: "Поддерживаются только файлы .mov.",
  empty_upload: "Файл пустой. Выберите другую запись.",
  empty_video: "Файл пустой. Выберите другую запись.",
  upload_timeout: "Загрузка превысила лимит времени. Проверьте соединение или выберите файл меньшего размера.",
  server_busy: "Сервис уже обрабатывает запись. Повторите загрузку после её завершения.",
  unsupported_file_type: "Поддерживаются только файлы .mov.",
  analysis_not_ready: "Правила проверки ещё не активированы.",
  invalid_rule_catalog: "Каталог правил недоступен или содержит ошибку. Проверьте конфигурацию сервера.",
  video_tools_unavailable: "На сервере недоступны инструменты обработки видео.",
  video_probe_timeout: "Не удалось проверить параметры видео за допустимое время. Выберите другую запись.",
  source_unavailable: "Исходная запись больше не хранится на сервере.",
  service_busy: "Сервис уже обрабатывает запись. Повторите загрузку после её завершения.",
  busy: "Сервис уже обрабатывает запись. Повторите загрузку после её завершения.",
  analyzer_unavailable: "Локальный анализатор ещё не подключён.",
  analyzer_config_invalid: "Не удалось загрузить настройки анализатора. Проверьте конфигурацию сервера.",
  rules_unavailable: "Правила проверки ещё не активированы.",
  ffmpeg_unavailable: "На сервере недоступен инструмент обработки видео.",
  ffprobe_unavailable: "На сервере недоступна проверка параметров видео.",
  job_not_found: "Задача не найдена. Проверьте номер или загрузите запись заново.",
};

async function responseError(response) {
  let detail;
  try {
    const body = await response.json();
    detail = body.detail || body.error;
  } catch (_) {
    // A proxy may return HTML instead of the API error schema.
  }
  const code = typeof detail === "object" ? detail?.code : undefined;
  let message = messages[code];
  if (!message && response.status === 401) message = messages.invalid_api_key;
  if (!message && response.status === 404) message = messages.job_not_found;
  if (!message && response.status === 429) message = messages.service_busy;
  if (!message && response.status === 413) message = messages.upload_too_large;
  if (!message && response.status === 415) message = messages.unsupported_format;
  if (!message) message = typeof detail?.message === "string" ? detail.message : typeof detail === "string" ? detail : `Сервис вернул ошибку ${response.status}. Повторите запрос.`;
  return new ApiError(message, code, response.status);
}

export async function apiRequest(path, key, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      cache: "no-store",
      headers: { ...options.headers, "X-API-Key": key },
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Не удалось связаться с сервисом. Проверьте подключение.", "network_error", 0);
  }
  if (!response.ok) throw await responseError(response);
  return response;
}

export async function getSystem(key) {
  return (await apiRequest("/api/v1/system", key)).json();
}

export async function getJob(id, key) {
  return (await apiRequest(`/api/v1/jobs/${encodeURIComponent(id)}`, key)).json();
}

export async function startAnalysis(file, key) {
  const body = new FormData();
  body.append("file", file);
  return (await apiRequest("/api/v1/analyze", key, { method: "POST", body })).json();
}

export async function getSource(id, key, signal) {
  return (await apiRequest(`/api/v1/jobs/${encodeURIComponent(id)}/video`, key, { signal })).blob();
}

export async function getJobs(key, filters, signal) {
  const query = new URLSearchParams(filters);
  return (await apiRequest(`/api/v1/jobs?${query}`, key, { signal })).json();
}

export async function getRules(key, signal) {
  return (await apiRequest("/api/v1/rules", key, { signal })).json();
}
