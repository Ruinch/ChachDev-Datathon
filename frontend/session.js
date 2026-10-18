import * as http from "./api.js";

const pages = new Set(["checks", "new", "review", "rules", "connection"]);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const normalizeJob = value => typeof value === "string" && uuid.test(value) ? value.toLowerCase() : null;
const aborted = () => new DOMException("Запрос отменён.", "AbortError");
const pollBackoff = [1000, 2000, 4000, 8000];
const retryableStatus = error => !error.status || error.status === 408 || error.status === 429 || error.status >= 500;

function browserNavigation() {
  return {
    read: () => location.href,
    write: (url, replace) => history[replace ? "replaceState" : "pushState"](null, "", url),
    listen: callback => {
      window.addEventListener("hashchange", callback);
      window.addEventListener("popstate", callback);
      return () => {
        window.removeEventListener("hashchange", callback);
        window.removeEventListener("popstate", callback);
      };
    },
  };
}

/**
 * Owns the page session: credentials stay in memory, async work belongs to an
 * auth generation, and only explicit navigation writes a route. DOM views never
 * need the key or the ordering rules for restore/upload/polling.
 */
export class WorkspaceSession {
  #key = "";
  #authVersion = 0;
  #jobVersion = 0;
  #listeners = new Set();
  #requests = new Set();
  #pollTimer = null;
  #pollFailures = 0;
  #connectionRequest = null;
  #jobRequest = null;
  #sourceRequest = null;
  #disposed = false;

  constructor({ api = http, navigation = browserNavigation(), scheduler = globalThis } = {}) {
    this.api = api;
    this.navigation = navigation;
    this.scheduler = scheduler;
    const url = new URL(navigation.read());
    const jobId = normalizeJob(url.searchParams.get("job"));
    const page = pages.has(url.hash.slice(1)) ? url.hash.slice(1) : jobId ? "review" : "checks";
    const pendingJob = page === "review" || page === "connection" ? jobId : null;
    if (url.searchParams.has("job") && !pendingJob) {
      url.searchParams.delete("job");
      navigation.write(url.href, true);
    }
    this.state = Object.freeze({
      page, connected: false, connecting: false, keyPresent: false, system: null,
      selectedFile: null, jobId: null, job: null, status: "idle", busy: false,
      pendingJob, error: null, pollRetry: null, uploadUncertain: false, startedAt: null, revision: 0,
    });
    this.stopNavigation = navigation.listen(() => this.#readRoute());
  }

  subscribe(listener) {
    this.#listeners.add(listener);
    listener(this.state, null);
    return () => this.#listeners.delete(listener);
  }

  #update(patch) {
    if (this.#disposed) return;
    const previous = this.state;
    this.state = Object.freeze({ ...previous, ...patch, revision: previous.revision + 1 });
    for (const listener of this.#listeners) listener(this.state, previous);
  }

  #cancelJob() {
    this.#jobVersion += 1;
    this.scheduler.clearTimeout(this.#pollTimer);
    this.#pollTimer = null;
    this.#pollFailures = 0;
    this.#jobRequest?.controller.abort();
    this.#jobRequest = null;
    this.#sourceRequest?.abort();
    this.#sourceRequest = null;
  }

  #cancelAuth() {
    this.#authVersion += 1;
    this.#connectionRequest = null;
    this.#cancelJob();
    for (const controller of this.#requests) controller.abort();
    this.#requests.clear();
  }

  setKey(value) {
    if (this.state.busy) return false;
    const key = String(value || "").trim();
    if (key === this.#key) return true;
    this.#key = key;
    this.#cancelAuth();
    this.#update({ connected: false, connecting: false, keyPresent: Boolean(key), system: null,
      jobId: null, job: null, status: "idle", error: null, pollRetry: null, startedAt: null });
    return true;
  }

  forget() {
    if (this.state.busy) return false;
    // Increment even for an empty key so late requests can never reconnect it.
    this.#key = "";
    this.#cancelAuth();
    this.#update({ connected: false, connecting: false, keyPresent: false, system: null,
      jobId: null, job: null, status: "idle", error: null, pollRetry: null, startedAt: null });
    return true;
  }

  async #request(operation, externalSignal, { upload = false } = {}) {
    if (!this.#key) throw new Error("Введите ключ доступа.");
    const version = this.#authVersion;
    const key = this.#key;
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (externalSignal?.aborted) throw aborted();
    externalSignal?.addEventListener("abort", cancel, { once: true });
    this.#requests.add(controller);
    try {
      const result = await operation(key, controller.signal);
      if (version !== this.#authVersion || controller.signal.aborted || this.#disposed) throw aborted();
      return result;
    } catch (error) {
      if (version !== this.#authVersion || controller.signal.aborted || this.#disposed) throw aborted();
      if (error.status === 401) {
        const pendingJob = this.state.jobId || this.state.pendingJob;
        // A parallel request can invalidate credentials while the POST has
        // already reached the server. Cancelling its reply cannot prove refusal.
        // A 401 received from the POST itself does prove that it was rejected.
        const uploadUncertain = this.state.uploadUncertain || (!upload && this.state.status === "uploading");
        this.#cancelAuth();
        this.#update({ connected: false, connecting: false, system: null, jobId: null,
          job: null, busy: false, status: "idle", pendingJob, pollRetry: null, uploadUncertain, startedAt: null, error });
      }
      throw error;
    } finally {
      this.#requests.delete(controller);
      externalSignal?.removeEventListener("abort", cancel);
    }
  }

  connect() {
    if (this.#connectionRequest) return this.#connectionRequest;
    if (!this.#key) return Promise.reject(new Error("Введите ключ доступа."));
    this.#update({ connecting: true, error: null });
    const version = this.#authVersion;
    const operation = this.#request((key, signal) => this.api.getSystem(key, signal))
      .then(info => {
        this.#update({ connected: true, connecting: false, system: info });
        const pending = this.state.pendingJob;
        if (pending && !this.state.busy) {
          // Consume before opening: connection updates and URL events can repeat.
          this.#update({ pendingJob: null });
          void this.openJob(pending);
        }
        return info;
      })
      .catch(error => {
        if (version === this.#authVersion && error.name !== "AbortError") this.#update({ connected: false, connecting: false, system: null, error });
        throw error;
      })
      .finally(() => { if (version === this.#authVersion) this.#connectionRequest = null; });
    this.#connectionRequest = operation;
    return operation;
  }

  jobLink(id = this.state.jobId) {
    const url = new URL(this.navigation.read());
    url.searchParams.delete("job");
    const valid = normalizeJob(id);
    if (valid) url.searchParams.set("job", valid);
    url.hash = "review";
    return url.href;
  }

  #writeRoute(page, id, replace = false) {
    const url = new URL(this.navigation.read());
    url.hash = page;
    url.searchParams.delete("job");
    if (page === "review" && id) url.searchParams.set("job", id);
    if (url.href !== this.navigation.read()) this.navigation.write(url.href, replace);
  }

  navigate(page, id = null) {
    return this.#navigate(page, id, false);
  }

  #navigate(page, id, replace) {
    if (!pages.has(page)) page = "checks";
    if (page === "new" && this.state.busy) {
      this.#writeRoute("review", this.state.jobId, true);
      this.#update({ page: "review", error: new Error("Дождитесь завершения текущей проверки. Журнал остаётся доступен.") });
      return false;
    }
    if (page === "review" && id) return this.openJob(id);
    if (page === "new" && this.state.page !== "new" && this.state.jobId) this.reset();
    this.#writeRoute(page, page === "review" ? this.state.jobId : null, replace);
    this.#update({ page, pendingJob: page === "checks" || page === "rules" || page === "new" ? null : this.state.pendingJob });
    return true;
  }

  #readRoute() {
    const url = new URL(this.navigation.read());
    const hash = url.hash.slice(1);
    if (hash === "main-content") return;
    const candidate = url.searchParams.get("job");
    const id = normalizeJob(candidate);
    const page = pages.has(hash) ? hash : id ? "review" : "checks";
    if (candidate && !id) {
      url.searchParams.delete("job");
      this.navigation.write(url.href, true);
    }
    if (page === "review" && id) {
      if (this.state.connected) void this.openJob(id);
      else this.#update({ page, pendingJob: id });
      return;
    }
    // A browser back/hash event already created its history entry. Normalize it
    // in place, otherwise Back would continuously recreate the same entry.
    this.#navigate(page, null, true);
  }

  selectFile(file) {
    if (this.state.busy) return false;
    this.#cancelJob();
    const page = this.state.page === "review" ? "new" : this.state.page;
    this.#writeRoute(page, null, true);
    this.#update({ page, selectedFile: file || null, jobId: null, job: null,
      status: "idle", pendingJob: null, error: null, pollRetry: null, uploadUncertain: false, startedAt: null });
    return true;
  }

  reset() {
    if (this.state.busy) return false;
    this.#cancelJob();
    this.#update({ selectedFile: null, jobId: null, job: null, status: "idle", error: null, pollRetry: null, uploadUncertain: false, startedAt: null });
    return true;
  }

  async openJob(candidate) {
    const id = normalizeJob(candidate);
    if (!id) {
      this.#update({ error: new Error("Введите полный номер задачи в формате UUID.") });
      return false;
    }
    if (this.state.busy && id !== this.state.jobId) {
      this.#writeRoute("review", this.state.jobId, true);
      this.#update({ page: "review", error: new Error("Дождитесь завершения текущей проверки.") });
      return false;
    }
    if (!this.state.connected) {
      this.#writeRoute("connection", null);
      this.#update({ page: "connection", pendingJob: id, error: null });
      return false;
    }
    this.#writeRoute("review", id);
    if (id === this.state.jobId) {
      this.#update({ page: "review", pendingJob: null });
      if (this.#jobRequest) return this.#jobRequest.promise;
      return this.state.status === "connection" ? this.refreshJob() : true;
    }
    this.#cancelJob();
    this.#update({ page: "review", selectedFile: null, pendingJob: null,
      jobId: id, job: null, status: "loading", busy: true, error: null, pollRetry: null, uploadUncertain: false, startedAt: null });
    return this.refreshJob();
  }

  refreshJob({ automatic = false } = {}) {
    if (!this.state.jobId) return Promise.resolve(false);
    if (this.#jobRequest) return this.#jobRequest.promise;
    if (!automatic) this.#pollFailures = 0;
    this.scheduler.clearTimeout(this.#pollTimer);
    this.#pollTimer = null;
    const id = this.state.jobId;
    const version = this.#jobVersion;
    const controller = new AbortController();
    const promise = this.#request((key, signal) => this.api.getJob(id, key, signal), controller.signal)
      .then(job => {
        if (version !== this.#jobVersion || id !== this.state.jobId) return false;
        if (!["processing", "done", "failed"].includes(job.status)) throw new Error("Сервис вернул неизвестное состояние задачи.");
        this.#pollFailures = 0;
        this.#update({ job, status: job.status, busy: job.status === "processing", error: null, pollRetry: null });
        if (job.status === "processing") this.#pollTimer = this.scheduler.setTimeout(() => this.refreshJob({ automatic: true }), 2000);
        return true;
      })
      .catch(error => {
        if (version !== this.#jobVersion || error.name === "AbortError") return false;
        // Unknown status does not prove the server stopped processing.
        const processing = this.state.job?.status === "processing" && error.status !== 404;
        let pollRetry = null;
        if (retryableStatus(error)) {
          const delayMs = pollBackoff[this.#pollFailures];
          if (delayMs !== undefined) {
            this.#pollFailures += 1;
            pollRetry = { attempt: this.#pollFailures, maxAttempts: pollBackoff.length, delayMs, exhausted: false };
            this.#pollTimer = this.scheduler.setTimeout(() => this.refreshJob({ automatic: true }), delayMs);
          } else pollRetry = { attempt: this.#pollFailures, maxAttempts: pollBackoff.length, delayMs: 0, exhausted: true };
        }
        this.#update({ status: "connection", busy: processing, error, pollRetry });
        return false;
      })
      .finally(() => { if (this.#jobRequest?.controller === controller) this.#jobRequest = null; });
    this.#jobRequest = { controller, promise };
    if (this.state.pollRetry) this.#update({ pollRetry: automatic ? { ...this.state.pollRetry, delayMs: 0 } : null });
    return promise;
  }

  async start(duration) {
    if (this.state.busy) return false;
    const file = this.state.selectedFile;
    if (!file || !file.name.toLowerCase().endsWith(".mov")) throw new Error("Выберите видео в формате .mov.");
    if (!file.size) throw new Error("Файл пустой. Выберите другую запись.");
    if (!this.#key) { this.navigate("connection"); throw new Error("Подключитесь к сервису, затем вернитесь к выбранной записи."); }
    this.#update({ busy: true, status: "preparing", error: null, pollRetry: null, uploadUncertain: false });
    const version = this.#authVersion;
    try {
      const info = await this.connect();
      if (!info.ready || info.busy) throw new Error(info.busy ? "Сервис обрабатывает другую запись. Дождитесь её завершения." : "Сервис пока не готов к анализу. Проверьте состояние подключения.");
      if (file.size > info.limits.max_upload_bytes) throw new Error("Размер записи превышает допустимый лимит сервиса.");
      if (Number.isFinite(duration) && duration > info.limits.max_video_duration_sec) throw new Error("Длительность записи превышает допустимый лимит сервиса.");
      this.#cancelJob();
      this.#update({ job: null, jobId: null, startedAt: Date.now(), status: "uploading" });
      const job = await this.#request((key, signal) => this.api.startAnalysis(file, key, signal), undefined, { upload: true });
      const id = normalizeJob(job?.job_id);
      if (!id || job.status !== "processing") throw new http.ApiError("Сервис вернул некорректный ответ создания задачи. Запись могла быть принята. Проверьте журнал перед повторной загрузкой.", "invalid_response", 202, { outcomeUnknown: true });
      this.#writeRoute("review", id);
      this.#update({ page: "review", jobId: id, job, status: "processing", pendingJob: null });
      void this.refreshJob();
      return true;
    } catch (error) {
      if (version !== this.#authVersion || error.name === "AbortError") return false;
      const status = this.state.status === "uploading" ? "failed" : "idle";
      if (error.outcomeUnknown && !/журнал/i.test(error.message)) {
        error = new http.ApiError(`${error.message} Запись могла быть принята. Перед повторной загрузкой проверьте журнал.`, error.code, error.status, { outcomeUnknown: true });
      }
      this.#update({ busy: false, status, error, uploadUncertain: Boolean(error.outcomeUnknown) });
      throw error;
    }
  }

  async source() {
    const id = this.state.jobId;
    const version = this.#jobVersion;
    if (!id) return null;
    this.#sourceRequest?.abort();
    const controller = new AbortController();
    this.#sourceRequest = controller;
    try {
      const blob = await this.#request((key, signal) => this.api.getSource(id, key, signal), controller.signal);
      if (version !== this.#jobVersion || id !== this.state.jobId) throw aborted();
      return blob;
    } finally {
      if (this.#sourceRequest === controller) this.#sourceRequest = null;
    }
  }

  history(filters, signal) { return this.#request((key, token) => this.api.getJobs(key, filters, token), signal); }
  rules(signal) { return this.#request((key, token) => this.api.getRules(key, token), signal); }
  legalGuide(signal) { return this.#request((key, token) => this.api.getLegalGuide(key, token), signal); }

  dispose() {
    this.#disposed = true;
    this.#cancelAuth();
    this.stopNavigation();
    this.#listeners.clear();
  }
}
