import { formatSize, formatTime } from "./media.js";
import { renderLegalGuide } from "./legal.js";
import { serviceMode, historyPresentation } from "./review-mode.js";

const byId = id => document.getElementById(id);
const labels = { checks: "Журнал проверок", new: "Новая проверка", review: "Разбор записи", rules: "Процедуры проверки", connection: "Подключение" };

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
  constructor(session) {
    this.session = session;
    this.offset = 0;
    this.limit = 20;
    this.historyRequest = null;
    this.rulesRequest = null;
    this.legalRequest = null;
    this.rulesLoading = false;
    this.legalLoading = false;
    this.historyTimer = null;
    this.searchTimer = null;
    this.lastSummary = null;
    this.historyVersion = 0;
    this.rulesVersion = 0;
    this.legalVersion = 0;
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
    byId("rules-refresh").addEventListener("click", () => this.loadRules());
    byId("legal-refresh").addEventListener("click", () => this.loadLegalGuide());
    this.unsubscribe = session.subscribe((state, previous) => this.render(state, previous));
  }

  get connected() { return this.session.state.connected; }
  get page() { return this.session.state.page; }

  render(state, previous) {
    if (!previous || (!state.connected && previous.connected) || (!state.system && previous.system)) this.invalidate();
    if (!previous || state.page !== previous.page) this.renderPage(state.page, previous?.page);
    if (state.system && state.system !== previous?.system) this.renderConnection(state.system);
    const finished = state.job && state.job !== previous?.job && ["done", "failed"].includes(state.job.status);
    if (finished && previous?.jobId === state.jobId && previous.job?.status === "processing") this.lastSummary = null;
    this.updateAccess();
    byId("check-service").disabled = state.busy || state.connecting;
    byId("check-service").textContent = state.connected ? "Обновить состояние" : "Подключиться";
    const mode = serviceMode(state.system);
    byId("connection-state").textContent = state.connecting ? "Проверяем…" : state.connected ? mode.ready ? state.system.busy ? "Занят" : mode.experimental ? "Экспериментальный разбор" : "Готов" : "Не готов" : "Не подключён";
    byId("connection-state").dataset.ready = String(Boolean(state.connected && mode.ready && !state.system?.busy));
    if (state.error && state.error !== previous?.error) this.feedback("connection-feedback", state.error.message, "error");
    if (!state.connected) return;
    const entered = !previous?.connected || previous.page !== state.page;
    if (state.page === "checks" && (entered || finished)) this.loadHistory();
    if (state.page === "rules" && entered) {
      this.loadRules();
      this.loadLegalGuide();
    }
  }

  feedback(id, text, tone = "") {
    const item = byId(id);
    item.textContent = text;
    item.hidden = !text;
    item.dataset.tone = tone;
  }

  invalidate() {
    this.historyVersion += 1;
    this.rulesVersion += 1;
    this.legalVersion += 1;
    this.historyRequest?.abort();
    this.rulesRequest?.abort();
    this.legalRequest?.abort();
    this.rulesLoading = false;
    this.legalLoading = false;
    clearTimeout(this.historyTimer);
    this.lastSummary = null;
    byId("history-body").replaceChildren();
    byId("rules-list").replaceChildren();
    byId("legal-content").replaceChildren();
    byId("rules-list").removeAttribute("aria-busy");
    byId("legal-content").removeAttribute("aria-busy");
    byId("system-details").replaceChildren(node("p", "muted-copy", "Состояние и лимиты появятся после подключения к сервису."));
    for (const id of ["stat-total", "stat-processing", "stat-violation", "stat-done"]) byId(id).textContent = "—";
    byId("history-range").textContent = "";
    byId("rules-summary").textContent = "";
    byId("rules-summary").hidden = true;
    byId("history-empty").hidden = true;
    this.feedback("history-feedback", "");
    this.feedback("rules-feedback", "");
    this.feedback("legal-feedback", "");
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
    byId("rules-catalog").hidden = !this.connected;
    byId("legal-guide").hidden = !this.connected;
    byId("rules-refresh").disabled = !this.connected || this.rulesLoading;
    byId("legal-refresh").disabled = !this.connected || this.legalLoading;
    byId("history-body").closest(".table-scroll").hidden = !this.connected;
    byId("history-range").closest(".table-pagination").hidden = !this.connected;
    byId("intake-access-note").hidden = this.connected;
    for (const id of ["history-search", "history-status", "history-verdict"]) byId(id).disabled = !this.connected;
    byId("forget-key").disabled = this.session.state.busy || !this.session.state.keyPresent;
    this.updateGlobalState();
  }

  updateGlobalState() {
    if (!this.connected || !this.session.state.system) return;
    const finished = ["done", "failed"].includes(this.session.state.job?.status);
    const busy = this.session.state.busy || Boolean(this.lastSummary?.processing ?? (!finished && this.session.state.system.busy));
    const state = byId("global-service-state");
    const mode = serviceMode(this.session.state.system);
    state.textContent = mode.ready ? busy ? "Идёт обработка" : mode.experimental ? "Экспериментальный разбор" : "Готов к проверке" : "Анализ недоступен";
    state.dataset.state = mode.ready ? busy ? "busy" : mode.experimental ? "warning" : "ready" : "warning";
  }

  renderConnection(info) {
    byId("check-service").textContent = "Обновить состояние";
    const state = byId("global-service-state");
    const mode = serviceMode(info);
    state.textContent = mode.ready ? info.busy ? "Идёт обработка" : mode.experimental ? "Экспериментальный разбор" : "Готов к проверке" : "Анализ недоступен";
    state.dataset.state = mode.ready ? info.busy ? "busy" : mode.experimental ? "warning" : "ready" : "warning";
    byId("history-refresh").disabled = false;
    this.feedback("connection-feedback", mode.experimental
      ? "Доступ подтверждён. Включён экспериментальный разбор: модель выдаёт наблюдения для ручной проверки, без вердикта о нарушении. Уверенность не откалибрована."
      : "Доступ подтверждён. Журнал и сохранённые результаты доступны.", mode.experimental ? "" : "success");
    const details = byId("system-details");
    details.replaceChildren();
    const grid = node("dl", "system-grid");
    const values = [
      ["Режим обработки", mode.experimental ? "Экспериментальный разбор" : "Проверка процедур"],
      ["Анализатор", info.analyzer?.configured ? "Настроен" : "Не настроен"],
      ["Активные процедуры", String(info.analyzer?.enabled_rule_ids?.length || 0)],
      ["Обработка видео", info.tools?.ffmpeg && info.tools?.ffprobe ? "Доступна" : "Недоступна"],
      ["Размер записи", formatSize(info.limits?.max_upload_bytes)],
      ["Длительность записи", formatTime(info.limits?.max_video_duration_sec)],
      ["Лимит анализа", formatTime(info.limits?.job_timeout_sec)],
    ];
    if (mode.experimental) values.push(["Кандидатные процедуры", String(info.review.rule_ids?.length || 0)], ["Подтверждённый анализ", info.ready ? "Доступен" : "Отключён до приёмки качества"]);
    if (info.analyzer?.runtime) values.splice(1, 0, ["Локальная модель", info.analyzer.runtime.ready ? "Отвечает" : info.analyzer.runtime.unavailable_reason?.code === "ml_dependencies_unavailable"
      ? "Нет зависимостей" : info.analyzer.runtime.unavailable_reason?.code === "analyzer_config_invalid" ? "Ошибка настройки" : "Не отвечает"]);
    for (const [label, value] of values) {
      const item = node("div", "system-item");
      item.append(node("dt", "system-label", label), node("dd", "system-value", value));
      grid.append(item);
    }
    details.append(grid);
    this.updateAccess();
  }

  renderPage(next, previous) {
    clearTimeout(this.historyTimer);
    this.historyRequest?.abort();
    this.rulesRequest?.abort();
    this.legalRequest?.abort();
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
      ? "Сверьте отмеченные эпизоды с исходной записью, кадрами и условиями применимой нормы."
      : "Загрузите запись кассовой операции. Результат включает события, таймкоды, исходные кадры и нормативные источники.";
    byId("view-analysis").dataset.mode = next === "review" ? "review" : "new";
    if (next !== "new" && next !== "review") byId("source-video").pause();
    if (previous && previous !== next) {
      const heading = document.querySelector('[data-view]:not([hidden]) h1');
      heading.tabIndex = -1;
      heading.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "instant" });
    }
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
      const result = await this.session.history({
        limit: String(this.limit), offset: String(this.offset), q: byId("history-search").value.trim(),
        status: byId("history-status").value, verdict: byId("history-verdict").value,
      }, this.historyRequest.signal);
      if (version !== this.historyVersion || !this.connected || this.page !== "checks") return;
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
        const presentation = historyPresentation(job);
        const badge = node("span", "history-status", presentation.label);
        badge.dataset.status = presentation.status;
        state.append(badge);
        if (presentation.note) state.append(node("span", "history-meta", presentation.note));
        const events = node("td", "history-events", job.status === "done" ? String(job.event_count) : "—");
        const action = node("td");
        const link = node("a", "history-open", "Открыть");
        link.href = `/?job=${encodeURIComponent(job.job_id)}#review`;
        link.setAttribute("aria-label", "Открыть проверку " + (job.metadata?.filename || job.job_id));
        link.addEventListener("click", event => {
          if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
          event.preventDefault();
          this.session.openJob(job.job_id);
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
    this.rulesLoading = true;
    byId("rules-refresh").disabled = true;
    byId("rules-list").setAttribute("aria-busy", "true");
    this.feedback("rules-feedback", "Загружаем процедуры…");
    try {
      const result = await this.session.rules(this.rulesRequest.signal);
      if (version !== this.rulesVersion || !this.connected || this.page !== "rules") return;
      byId("rules-summary").textContent = `Всего процедур: ${result.total}. Подтверждённых активных: ${result.enabled_count}. ` + (serviceMode(this.session.state.system).experimental
        ? "В экспериментальном разборе кандидатные процедуры используются только для гипотез, которые должен проверить человек."
        : "Отключённые процедуры не участвуют в анализе.");
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
        if (rule.verification_note) card.append(node("p", "field-help", rule.verification_note));
        else if (rule.verification !== "verified") card.append(node("p", "field-help", "Источник, наблюдаемость и качество распознавания ожидают подтверждения. Процедура может использоваться только как гипотеза в явно обозначенном экспериментальном разборе."));
        if (rule.verified_on) card.append(node("p", "field-help", "Проверена: " + rule.verified_on));
        return card;
      });
      byId("rules-list").replaceChildren(...cards);
      this.feedback("rules-feedback", result.total ? "" : "В каталоге пока нет процедур.");
    } catch (error) {
      if (error.name === "AbortError" || version !== this.rulesVersion) return;

      this.feedback("rules-feedback", error.message, "error");
    } finally {
      if (version === this.rulesVersion) {
        this.rulesLoading = false;
        byId("rules-refresh").disabled = !this.connected;
        byId("rules-list").removeAttribute("aria-busy");
      }
    }
  }

  async loadLegalGuide() {
    if (!this.connected || this.page !== "rules") return;
    this.legalRequest?.abort();
    this.legalRequest = new AbortController();
    const version = ++this.legalVersion;
    this.legalLoading = true;
    byId("legal-refresh").disabled = true;
    byId("legal-content").setAttribute("aria-busy", "true");
    this.feedback("legal-feedback", "Загружаем правовую справку…");
    try {
      const guide = await this.session.legalGuide(this.legalRequest.signal);
      if (version !== this.legalVersion || !this.connected || this.page !== "rules") return;
      renderLegalGuide(byId("legal-content"), guide);
      this.feedback("legal-feedback", "");
    } catch (error) {
      if (error.name === "AbortError" || version !== this.legalVersion) return;
      this.feedback("legal-feedback", error.message + " Повторите загрузку справки.", "error");
    } finally {
      if (version === this.legalVersion) {
        this.legalLoading = false;
        byId("legal-refresh").disabled = !this.connected;
        byId("legal-content").removeAttribute("aria-busy");
      }
    }
  }

}
