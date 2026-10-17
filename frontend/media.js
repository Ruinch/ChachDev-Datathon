const numberFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 });

export function formatTime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "—";
  const tenths = Math.round(value * 10);
  return `${String(Math.floor(tenths / 600)).padStart(2, "0")}:${((tenths % 600) / 10).toFixed(1).padStart(4, "0")}`;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${numberFormatter.format(bytes / 1024)} КБ`;
  return `${numberFormatter.format(bytes / 1024 / 1024)} МБ`;
}

export function validFrame(frame) {
  return frame && typeof frame.image_base64 === "string"
    && frame.image_base64.length > 0 && /^[A-Za-z0-9+/]+={0,2}$/.test(frame.image_base64);
}

export class SourcePlayer {
  constructor() {
    this.panel = document.querySelector("#source-panel");
    this.video = document.querySelector("#source-video");
    this.stage = document.querySelector("#video-stage");
    this.body = document.querySelector("#source-body");
    this.name = document.querySelector("#source-name");
    this.note = document.querySelector("#source-note");
    this.download = document.querySelector("#download-video");
    this.loadButton = document.querySelector("#load-source");
    this.toggle = document.querySelector("#toggle-video");
    this.url = null;
    this.filename = "";
    this.toggle.addEventListener("click", () => this.collapse(!this.body.hidden));
    this.video.addEventListener("loadedmetadata", () => {
      this.note.textContent = `Длительность ${formatTime(this.video.duration)} · просмотр в браузере`;
    });
    this.video.addEventListener("error", () => {
      this.note.textContent = "Браузер не воспроизводит кодек этой записи. Скачайте оригинал или используйте доказательные кадры.";
    });
  }

  clear() {
    this.video.pause();
    this.video.removeAttribute("src");
    this.video.load();
    if (this.url) URL.revokeObjectURL(this.url);
    this.url = null;
    this.download.removeAttribute("href");
    this.download.hidden = true;
    this.stage.hidden = true;
    this.panel.hidden = true;
    this.loadButton.hidden = true;
    this.note.textContent = "";
  }

  setBlob(blob, filename) {
    this.clear();
    this.filename = filename || "input.mov";
    this.url = URL.createObjectURL(blob);
    this.name.textContent = this.filename;
    this.video.src = this.url;
    this.video.muted = true;
    this.download.href = this.url;
    this.download.download = this.filename;
    this.download.hidden = false;
    this.stage.hidden = false;
    this.panel.hidden = false;
    this.note.textContent = "Предпросмотр исходной записи. Воспроизведение начнётся по нажатию.";
    this.collapse(false);
  }

  setPending(filename) {
    this.clear();
    this.filename = filename || "input.mov";
    this.name.textContent = this.filename;
    this.panel.hidden = false;
    this.loadButton.hidden = false;
    this.note.textContent = "Для просмотра сохранённой записи загрузите её с сервера. Понадобится тот же ключ доступа.";
    this.collapse(false);
  }

  collapse(collapsed) {
    this.body.hidden = collapsed;
    this.toggle.setAttribute("aria-expanded", String(!collapsed));
    this.toggle.textContent = collapsed ? "Показать видео" : "Свернуть";
  }

  seek(seconds) {
    if (!this.url) return false;
    const move = () => {
      const target = Math.max(0, Number(seconds) || 0);
      this.video.currentTime = Number.isFinite(this.video.duration) ? Math.min(target, Math.max(0, this.video.duration - .05)) : target;
      this.note.textContent = `Эпизод на ${formatTime(target)}. Нажмите воспроизведение, чтобы проверить последовательность действий.`;
    };
    if (this.video.readyState >= 1) move();
    else this.video.addEventListener("loadedmetadata", move, { once: true });
    this.collapse(false);
    this.panel.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    this.video.focus({ preventScroll: true });
    return true;
  }
}

export class EvidenceViewer {
  constructor() {
    this.dialog = document.querySelector("#evidence-dialog");
    this.image = document.querySelector("#dialog-image");
    this.title = document.querySelector("#evidence-dialog-title");
    this.counter = document.querySelector("#frame-count");
    this.previous = document.querySelector("#previous-frame");
    this.next = document.querySelector("#next-frame");
    this.download = document.querySelector("#download-frame");
    this.error = document.querySelector("#dialog-error");
    this.frames = [];
    this.index = 0;
    document.querySelector("#close-evidence").addEventListener("click", () => this.dialog.close());
    this.previous.addEventListener("click", () => this.show(this.index - 1));
    this.next.addEventListener("click", () => this.show(this.index + 1));
    this.dialog.addEventListener("click", event => { if (event.target === this.dialog) this.dialog.close(); });
    this.dialog.addEventListener("keydown", event => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        this.show(this.index + (event.key === "ArrowLeft" ? -1 : 1));
      }
    });
    this.dialog.addEventListener("close", () => {
      document.body.classList.remove("dialog-open");
      this.image.removeAttribute("src");
      this.frames = [];
      if (this.trigger?.isConnected) this.trigger.focus();
    });
    this.image.addEventListener("error", () => {
      this.image.hidden = true;
      this.error.hidden = false;
    });
  }

  open(frames, index, trigger) {
    this.frames = frames;
    this.trigger = trigger;
    this.show(index);
    document.body.classList.add("dialog-open");
    this.dialog.showModal();
  }

  show(index) {
    if (index < 0 || index >= this.frames.length) return;
    this.index = index;
    const frame = this.frames[index];
    const src = `data:image/jpeg;base64,${frame.image_base64}`;
    const time = formatTime(frame.timestamp_sec);
    this.image.hidden = false;
    this.error.hidden = true;
    this.image.alt = `Кадр исходной записи на ${time}`;
    this.image.src = src;
    this.title.textContent = time;
    this.counter.textContent = `${index + 1} / ${this.frames.length}`;
    this.previous.disabled = index === 0;
    this.next.disabled = index === this.frames.length - 1;
    this.download.href = src;
    this.download.download = `frame-${time.replace(/[:.]/g, "-")}.jpg`;
  }
}

export function createJsonDownload(data, jobId) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" }));
  return { url, filename: `analysis-${jobId || "result"}.json` };
}
