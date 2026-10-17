import { getJobs, getRules } from "./api.js";
import { formatSize, formatTime } from "./media.js";

const byId = id => document.getElementById(id);
const labels = { checks: "Журнал проверок", new: "Новая проверка", review: "Разбор записи", rules: "Процедуры проверки", connection: "Подключение" };
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }).format(date) : "—";
}

function safeLink(reference) {
  try {
    const url = new URL(reference?.source_url);
    if (!["https:", "http:"].includes(url.protocol)) return null;
    const link = node("a", "rule-link", "Открыть нормативный источник ↗");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (_) { return null; }
}

export class ServiceWorkspace {
  constructor({ getKey, onOpenJob, onNewCheck, onDisconnect, isBusy, getActiveJob }) {
    Object.assign(this, { getKey, onOpenJob, onNewCheck, onDisconnect, isBusy, getActiveJob });
    this.verifiedKey = "";
    this.system = null;
    this.page = null;
    this.offset = 0;
    this.limit = 20;
    const savedJob = new URL(location.href).searchParams.get("job");
    this.pendingJob = savedJob && uuidPattern.test(savedJob) ? savedJob : null;
    this.historyRequest = null;
    this.rulesRequest = null;
    this.historyTimer = null;
    this.searchTimer = null;
    this.lastSummary = null;
    this.historyVersion = 0;
    this.rulesVersion = 0;
    window.addEventListener("hashchange", () => this.route());
    window.addEventListener("popstate", () => this.route());
    document.addEventListener("visibilitychange", () => {
      clearTimeout(this.historyTimer);
      if (!document.hidden && this.page === "checks" && this.connected) this.loadHistory();
    });
    byId("history-search").addEventListener("input", () => {
      clearTimeout(this.searchTimer);
      this.searchTimer = window.setTimeout(() => { this.offset = 0; this.loadHistory(); }, 250);
    });
    for (const id of ["history-status", "history-verdict"]) byId(id).addEventListener("change", () => { this.offset = 0; this.loadHistory(); });
    byId("history-refresh").addEventListener("click", () => this.loadHistory());
    byId("history-prev").addEventListener("click", () => { this.offset = Math.max(0, this.offset - this.limit); this.loadHistory(); });
    byId("history-next").addEventListener("click", () => { this.offset += this.limit; this.loadHistory(); });
    byId("forget-key").addEventListener("click", () => {
      if (this.isBusy()) return;
      byId("api-key").value = "";
      byId("api-key").type = "password";
      byId("key-visibility").textContent = "Показать";
      byId("key-visibility").setAttribute("aria-label", "Показать ключ");
      byId("key-visibility").setAttribute("aria-pressed", "false");
      this.onDisconnect();
      this.invalidate();
      this.feedback("connection-feedback", "Ключ удалён из текущей страницы. Для продолжения подключитесь заново.");
      byId("api-key").focus();
    });
    this.invalidate();
    this.route();
  }

  get connected() { return Boolean(this.verifiedKey && this.verifiedKey === this.getKey()); }

  feedback(id, text, tone = "") {
    const item = byId(id);
    item.textContent = text;
    item.hidden = !text;
    item.dataset.tone = tone;
  }

  invalidate() {
    this.verifiedKey = "";
    this.system = null;
    this.historyVersion += 1;
    this.rulesVersion += 1;
    this.historyRequest?.abort();
    this.rulesRequest?.abort();
    clearTimeout(this.historyTimer);
    this.lastSummary = null;
    byId("history-body").replaceChildren();
    byId("rules-list").replaceChildren();
    byId("system-details").replaceChildren(node("p", "muted-copy", "Состояние и лимиты появятся после подключения к сервису."));
    for (const id of ["stat-total", "stat-processing", "stat-violation", "stat-done"]) byId(id).textContent = "—";
    byId("history-range").textContent = "";
    byId("rules-summary").textContent = "";
    byId("rules-summary").hidden = true;
    byId("history-empty").hidden = true;
    this.feedback("history-feedback", "");
    this.feedback("rules-feedback", "");
    this.feedback("connection-feedback", "");
    byId("connection-state").textContent = "Не подключён";
    byId("check-service").textContent = "Подключиться";
    byId("service-note").hidden = true;
    byId("history-refresh").disabled = true;
    byId("history-prev").disabled = true;
    byId("history-next").disabled = true;
    byId("global-service-state").textContent = "Нет подключения";
    byId("global-service-state").dataset.state = "offline";
    this.updateAccess();
  }

  updateAccess() {
    byId("history-locked").hidden = this.connected;
    byId("rules-locked").hidden = this.connected;
    byId("history-body").closest(".table-scroll").hidden = !this.connected;
    byId("history-range").closest(".table-pagination").hidden = !this.connected;
    byId("intake-access-note").hidden = this.connected;
    for (const id of ["history-search", "history-status", "history-verdict"]) byId(id).disabled = !this.connected;
    byId("forget-key").disabled = this.isBusy() || !this.getKey();
    this.updateGlobalState();
  }

  updateGlobalState() {
    if (!this.connected || !this.system) return;
    const busy = this.isBusy() || Boolean(this.lastSummary?.processing ?? this.system.busy);
    const state = byId("global-service-state");
    state.textContent = this.system.ready ? busy ? "Идёт обработка" : "Готов к проверке" : "Анализ недоступен";
    state.dataset.state = this.system.ready ? busy ? "busy" : "ready" : "warning";
  }

  setConnection(info, key) {
    if (key !== this.getKey()) return;
    this.verifiedKey = key;
    this.system = info;
    byId("check-service").textContent = "Обновить состояние";
    const state = byId("global-service-state");
    state.textContent = info.ready ? info.busy ? "Идёт обработка" : "Готов к проверке" : "Анализ недоступен";
    state.dataset.state = info.ready ? info.busy ? "busy" : "ready" : "warning";
    byId("history-refresh").disabled = false;
    this.feedback("connection-feedback", "Доступ подтверждён. Журнал и сохранённые результаты доступны.", "success");
    const details = byId("system-details");
    details.replaceChildren();
    const grid = node("dl", "system-grid");
    const values = [
      ["Анализатор", info.analyzer?.configured ? "Подключён" : "Не подключён"],
      ["Активные процедуры", String(info.analyzer?.enabled_rule_ids?.length || 0)],
      ["Обработка видео", info.tools?.ffmpeg && info.tools?.ffprobe ? "Доступна" : "Недоступна"],
      ["Размер записи", formatSize(info.limits?.max_upload_bytes)],
      ["Длительность записи", formatTime(info.limits?.max_video_duration_sec)],
      ["Лимит анализа", formatTime(info.limits?.job_timeout_sec)],
    ];
    for (const [label, value] of values) {
      const item = node("div", "system-item");
      item.append(node("dt", "system-label", label), node("dd", "system-value", value));
      grid.append(item);
    }
    details.append(grid);
    this.updateAccess();
    if (this.pendingJob) {
      const id = this.pendingJob;
      this.pendingJob = null;
      this.openJob(id);
    } else if (this.page === "checks") this.loadHistory();
    else if (this.page === "rules") this.loadRules();
  }

  route() {
    const hash = location.hash.slice(1);
    if (hash === "main-content" && this.page) {
      byId("main-content").focus();
      return;
    }
    const savedCandidate = new URL(location.href).searchParams.get("job");
    const saved = savedCandidate && uuidPattern.test(savedCandidate) ? savedCandidate : null;
    const next = labels[hash] ? hash : saved ? "review" : "checks";
    const previous = this.page;
    if (next === "new" && previous !== "new" && this.isBusy()) {
      this.navigate("review", this.getActiveJob());
      byId("result-announcement").textContent = "Дождитесь завершения текущей проверки. Журнал остаётся доступен.";
      return;
    }
    this.page = next;
    if (next === "checks" || next === "rules") {
      this.pendingJob = null;
      if (savedCandidate) {
        const cleanUrl = new URL(location.href);
        cleanUrl.searchParams.delete("job");
        history.replaceState(null, "", cleanUrl);
      }
    }
    clearTimeout(this.historyTimer);
    for (const item of document.querySelectorAll("[data-view]")) item.hidden = item.dataset.view !== (next === "new" || next === "review" ? "analysis" : next);
    for (const item of document.querySelectorAll("[data-nav]")) {
      if (item.dataset.nav === (next === "review" ? "checks" : next)) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    byId("page-location").textContent = labels[next];
    document.title = labels[next] + " · Кассовый контроль";
    const analysisHeading = byId("view-analysis").querySelector("h1");
    analysisHeading.textContent = next === "review" ? "Разбор видеозаписи" : "Новая проверка";
    byId("view-analysis").querySelector(".page-description").textContent = next === "review"
      ? "Сверьте найденные события с исходной записью, доказательными кадрами и нормативными источниками."
      : "Загрузите запись кассовой операции. Результат включает события, таймкоды, исходные кадры и нормативные источники.";
    byId("view-analysis").dataset.mode = next === "review" ? "review" : "new";
    if (next !== "new" && next !== "review") byId("source-video").pause();
    if (previous !== next) {
      const heading = document.querySelector('[data-view]:not([hidden]) h1');
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "instant" });
    }
    this.updateAccess();
    if (next === "new" && previous !== "new") {
      this.pendingJob = null;
      this.onNewCheck();
    }
    if (next === "review" && saved && saved !== this.getActiveJob()) {
      if (this.connected) this.openJob(saved);
      else {
        this.pendingJob = saved;
        byId("resume-job").value = saved;
        byId("resume-panel").open = true;
      }
    }
    if (next === "review" && !saved && !this.getActiveJob()) {
      const empty = byId("status-empty");
      empty.querySelector("h3").textContent = "Проверка не выбрана";
      const text = empty.querySelector("p");
      text.textContent = "Откройте сохранённую запись в журнале или создайте новую проверку. ";
      const link = node("a", "", "Перейти к журналу →");
      link.href = "#checks";
      text.append(link);
    }
    if (next === "checks") this.loadHistory();
    if (next === "rules") this.loadRules();
  }

  navigate(page, id = null) {
    const url = new URL(location.href);
    url.hash = page;
    if (id) url.searchParams.set("job", id);
    else if (page === "new") url.searchParams.delete("job");
    if (url.href !== location.href) history.pushState(null, "", url);
    this.route();
  }

  async openJob(id) {
    if (!uuidPattern.test(id)) return;
    if (!this.connected) {
      this.pendingJob = id;
      this.navigate("connection");
      return;
    }
    if (this.isBusy() && id !== this.getActiveJob()) {
      this.navigate("review", this.getActiveJob());
      byId("result-announcement").textContent = "Дождитесь завершения текущей проверки.";
      return;
    }
    const url = new URL(location.href);
    url.hash = "review";
    url.searchParams.set("job", id);
    history.replaceState(null, "", url);
    // The callback owns the current job, so render after it establishes state.
    if (id !== this.getActiveJob()) await this.onOpenJob(id);
    this.route();
  }

  async loadHistory() {
    clearTimeout(this.historyTimer);
    if (!this.connected || this.page !== "checks") return;
    this.historyRequest?.abort();
    this.historyRequest = new AbortController();
    const version = ++this.historyVersion;
    byId("history-refresh").disabled = true;
    byId("history-body").closest("table").setAttribute("aria-busy", "true");
    this.feedback("history-feedback", "Загружаем журнал…");
    try {
      const result = await getJobs(this.getKey(), {
        limit: String(this.limit), offset: String(this.offset), q: byId("history-search").value.trim(),
        status: byId("history-status").value, verdict: byId("history-verdict").value,
      }, this.historyRequest.signal);
      if (version !== this.historyVersion || !this.connected) return;
      if (!result.items.length && result.total > 0 && this.offset >= result.total) {
        this.offset = Math.floor((result.total - 1) / this.limit) * this.limit;
        return this.loadHistory();
      }
      this.lastSummary = result.summary;
      this.updateGlobalState();
      byId("stat-total").textContent = String(result.summary.total);
      byId("stat-processing").textContent = String(result.summary.processing);
      byId("stat-violation").textContent = String(result.summary.violation);
      byId("stat-done").textContent = String(result.summary.done);
      const rows = [];
      for (const job of result.items) {
        const row = node("tr");
        const file = node("td");
        file.append(node("span", "history-file", job.metadata?.filename || "Без названия"));
        const subtitle = [job.metadata?.duration_sec ? formatTime(job.metadata.duration_sec) : null, job.metadata?.size_bytes ? formatSize(job.metadata.size_bytes) : null, job.job_id.slice(0, 8)].filter(Boolean).join(" · ");
        file.append(node("span", "history-meta", subtitle));
        const date = node("td");
        date.append(node("span", "history-meta", dateLabel(job.metadata?.created_at)));
        const state = node("td");
        const completeNegative = job.verdict === "no_violation" && job.analysis_scope?.full_video_checked === true;
        const uncertainResult = job.status === "done" && job.verdict !== "violation" && !completeNegative;
        const label = job.status === "processing" ? "Обрабатывается" : job.status === "failed" ? "Ошибка обработки" : job.verdict === "violation" ? "Найдены события" : completeNegative ? "Без событий" : "Проверьте результат";
        const badge = node("span", "history-status", label);
        badge.dataset.status = uncertainResult ? "unknown" : job.status === "done" ? job.verdict : job.status;
        state.append(badge);
        if (job.status === "done" && job.analysis_scope?.full_video_checked === false) state.append(node("span", "history-meta", "Частичная проверка"));
        const events = node("td", "history-events", job.status === "done" ? String(job.event_count) : "—");
        const action = node("td");
        const link = node("a", "history-open", "Открыть");
        link.href = `/?job=${encodeURIComponent(job.job_id)}#review`;
        link.setAttribute("aria-label", "Открыть проверку " + (job.metadata?.filename || job.job_id));
        link.addEventListener("click", event => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          this.openJob(job.job_id);
        });
        action.append(link);
        row.append(file, date, state, events, action);
        rows.push(row);
      }
      byId("history-body").replaceChildren(...rows);
      byId("history-empty").hidden = result.total !== 0;
      const empty = byId("history-empty");
      empty.querySelector("h2, h3").textContent = result.summary.total === 0 ? "Проверок пока нет" : "Ничего не найдено";
      empty.querySelector("p").textContent = result.summary.total === 0 ? "Загрузите первую запись. Её состояние и результат сохранятся в журнале." : "Измените запрос или сбросьте фильтры.";
      byId("history-range").textContent = result.total ? `${this.offset + 1}–${this.offset + result.items.length} из ${result.total}` : "0 записей";
      byId("history-prev").disabled = this.offset === 0;
      byId("history-next").disabled = this.offset + this.limit >= result.total;
      this.feedback("history-feedback", result.total ? `Обновлено в ${new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}` : "");
      if (result.summary.processing && !document.hidden) this.historyTimer = window.setTimeout(() => this.loadHistory(), 8000);
    } catch (error) {
      if (error.name === "AbortError" || version !== this.historyVersion) return;
      if (error.status === 401) this.invalidate();
      this.feedback("history-feedback", error.message, "error");
    } finally {
      if (version === this.historyVersion) {
        byId("history-refresh").disabled = !this.connected;
        byId("history-body").closest("table").removeAttribute("aria-busy");
      }
    }
  }

  async loadRules() {
    if (!this.connected || this.page !== "rules") return;
    this.rulesRequest?.abort();
    this.rulesRequest = new AbortController();
    const version = ++this.rulesVersion;
    this.feedback("rules-feedback", "Загружаем процедуры…");
    try {
      const result = await getRules(this.getKey(), this.rulesRequest.signal);
      if (version !== this.rulesVersion || !this.connected) return;
      byId("rules-summary").textContent = `Всего процедур: ${result.total}. Активных: ${result.enabled_count}. Отключённые процедуры не участвуют в анализе.`;
      byId("rules-summary").hidden = false;
      const cards = result.items.map(rule => {
        const card = node("article", "rule-card");
        const heading = node("div", "rule-card-heading");
        const title = node("div");
        title.append(node("span", "rule-id mono", rule.id), node("h2", "", rule.event_type));
        const state = node("span", "rule-state", rule.enabled ? "Активна" : rule.verification === "verified" ? "Отключена" : "На проверке");
        state.dataset.enabled = String(rule.enabled);
        heading.append(title, state);
        card.append(heading);
        card.append(node("p", "rule-reference", [rule.reference?.title, rule.reference?.clause].filter(Boolean).join(" · ")));
        const link = safeLink(rule.reference);
        if (link) card.append(link);
        for (const [label, values] of [["Что должно быть видно", rule.observable_signals], ["Условия применимости", rule.required_context], ["Ограничения проверки", rule.limitations]]) {
          if (!Array.isArray(values) || !values.length) continue;
          card.append(node("h3", "", label));
          const list = node("ul", "rule-conditions");
          for (const value of values) list.append(node("li", "", value));
          card.append(list);
        }
        if (rule.verification !== "verified") card.append(node("p", "field-help", "Источник и условия применения ожидают подтверждения. Эта процедура не используется в результатах анализа."));
        if (rule.verified_on) card.append(node("p", "field-help", "Проверена: " + rule.verified_on));
        return card;
      });
      byId("rules-list").replaceChildren(...cards);
      this.feedback("rules-feedback", result.total ? "" : "В каталоге пока нет процедур.");
    } catch (error) {
      if (error.name === "AbortError" || version !== this.rulesVersion) return;
      if (error.status === 401) this.invalidate();
      this.feedback("rules-feedback", error.message, "error");
    }
  }

  jobUpdated() {
    this.updateAccess();
    if (this.page === "checks") this.loadHistory();
  }
}
