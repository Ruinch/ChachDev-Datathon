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

// The deadline covers both the connection and reading the complete response body.
// Requests are never retried here: repeating a POST could create a second check.
export async function apiRequest(path, key, options = {}) {
  const { signal, timeoutMs = 15000, responseType = "json", ...request } = options;
  if (signal?.aborted) throw new DOMException("Запрос отменён.", "AbortError");
  const controller = new AbortController();
  let response;
  let timer;
  let abortRequest;
  const cancellation = new Promise((_, reject) => {
    abortRequest = () => {
      controller.abort();
      reject(new DOMException("Запрос отменён.", "AbortError"));
    };
    if (signal?.aborted) abortRequest();
    else signal?.addEventListener("abort", abortRequest, { once: true });
    timer = setTimeout(() => {
      controller.abort();
      reject(new ApiError(request.method === "POST"
        ? "Сервис не ответил вовремя. Перед повторной загрузкой проверьте журнал: запись могла быть принята."
        : "Сервис не ответил вовремя. Проверьте подключение и повторите запрос.", "request_timeout", 0));
    }, timeoutMs);
  });
  const operation = (async () => {
    response = await fetch(path, {
      ...request,
      signal: controller.signal,
      cache: "no-store",
      headers: { ...request.headers, "X-API-Key": key },
    });
    if (!response.ok) throw await responseError(response);
    return responseType === "blob" ? response.blob() : response.json();
  })();
  try {
    return await Promise.race([operation, cancellation]);
  } catch (error) {
    if (error instanceof ApiError || error.name === "AbortError") throw error;
    if (error instanceof SyntaxError) throw new ApiError("Сервис вернул нечитаемый ответ. Повторите запрос.", "invalid_response", response?.status || 0);
    throw new ApiError("Не удалось связаться с сервисом. Проверьте подключение.", "network_error", 0);
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abortRequest);
  }
}

export function getSystem(key, signal) {
  return apiRequest("/api/v1/system", key, { signal });
}

export function getJob(id, key, signal) {
  return apiRequest(`/api/v1/jobs/${encodeURIComponent(id)}`, key, { signal });
}

export function startAnalysis(file, key, signal) {
  const body = new FormData();
  body.append("file", file);
  return apiRequest("/api/v1/analyze", key, { method: "POST", body, signal, timeoutMs: 60000 });
}

export function getSource(id, key, signal) {
  return apiRequest(`/api/v1/jobs/${encodeURIComponent(id)}/video`, key, { signal, responseType: "blob", timeoutMs: 60000 });
}

export function getJobs(key, filters, signal) {
  const query = new URLSearchParams(filters);
  return apiRequest(`/api/v1/jobs?${query}`, key, { signal });
}

export function getRules(key, signal) {
  return apiRequest("/api/v1/rules", key, { signal });
}
