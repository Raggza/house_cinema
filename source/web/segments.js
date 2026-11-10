// House Cinema — play a converted video (plan step 5.1).
// The hub sends converted videos as HLS: a playlist (.m3u8) of 6-second fMP4 pieces.
// - Safari (iPhone, iPad, Mac) plays HLS itself: the playlist URL is simply the <video> src.
// - Other browsers (Chrome, Edge, Firefox) play it through MediaSource: SegmentPlayer below
//   downloads the pieces around the playing position and appends them. The browser's own
//   controls keep working (seek bar for the whole video, fullscreen, subtitles).
// No external library: the hub must work without Internet.

const LOOKAHEAD_S = 30;   // keep this much ahead of the playing position
const KEEP_BEHIND_S = 60; // drop what is older than this (memory)
const KEEP_AHEAD_MAX_S = 180;
const POLL_MS = 2000;     // a playlist that still grows (remux in progress) is read again
const TOLERANCE_S = 0.3;

// Must match CODEC_STRINGS in source/modules/transcode/plan.py.
const VIDEO = { h264: "avc1.640028", hevc: "hvc1.1.6.L120.90", av1: "av01.0.08M.08", vp9: "vp09.00.40.08" };
const AUDIO = { aac: "mp4a.40.2", mp3: "mp4a.6B", ac3: "ac-3", eac3: "ec-3", opus: "opus", flac: "flac" };

/** "mse" | "native" | null: how this browser can play HLS. */
export function hlsSupport(video) {
  if (window.MediaSource && typeof window.MediaSource.isTypeSupported === "function") return "mse";
  if (video.canPlayType("application/vnd.apple.mpegurl")) return "native";
  return null;
}

/** What this browser decodes in MP4, as codec names the hub understands. */
export function decodableCodecs(video, mode) {
  const ok = mode === "mse" ? (t) => window.MediaSource.isTypeSupported(t) : (t) => !!video.canPlayType(t);
  const pick = (table) => Object.keys(table).filter((k) => ok(`video/mp4; codecs="${table[k]}"`));
  return { video: pick(VIDEO), audio: pick(AUDIO) };
}

export class LoaderError extends Error {
  constructor(kind, detail = "") { super(detail || kind); this.kind = kind; this.detail = detail; }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function parsePlaylist(text, base) {
  const segments = [];
  let init = null, length = 0, t = 0;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (line.startsWith("#EXT-X-MAP:")) {
      const m = /URI="([^"]+)"/.exec(line);
      if (m) init = new URL(m[1], base).href;
    } else if (line.startsWith("#EXTINF:")) {
      length = parseFloat(line.slice(8)) || 0;
    } else if (line && !line.startsWith("#")) {
      segments.push({ url: new URL(line, base).href, start: t, end: t + length });
      t += length;
    }
  }
  return { init, segments, ended: text.includes("#EXT-X-ENDLIST"), duration: t };
}

export class SegmentPlayer {
  /**
   * @param {HTMLVideoElement} video
   * @param {{url: string, mime: string, duration?: number,
   *          reopen?: () => Promise<string>, onError?: (e: LoaderError) => void}} options
   *   reopen: asked for a new playlist URL when the hub forgot this one (404).
   */
  constructor(video, options) {
    this.video = video;
    this.url = options.url;
    this.mime = options.mime;
    this.duration = options.duration || 0;
    this.reopen = options.reopen;
    this.onError = options.onError || (() => {});
    this.stopped = false;
    this.seekGen = 0;
    this.done = new Set();   // pieces appended that the buffer may not show exactly
    this.reopens = 0;
    this.lastPoll = 0;
    this.wake = null;
    this.ctrl = null;
    this.kick = () => { if (this.wake) { const w = this.wake; this.wake = null; w(); } };
    this.onSeeking = () => { this.seekGen++; if (this.ctrl) this.ctrl.abort(); this.kick(); };
  }

  async start() {
    this.ms = new MediaSource();
    this.objectUrl = URL.createObjectURL(this.ms);
    const opened = new Promise((r) => this.ms.addEventListener("sourceopen", r, { once: true }));
    this.video.src = this.objectUrl;
    await opened;
    await this.readPlaylist();
    this.ms.duration = this.duration || this.list.duration;
    this.sb = this.ms.addSourceBuffer(this.mime);
    await this.append(await this.get(this.list.init));
    for (const ev of ["seeking"]) this.video.addEventListener(ev, this.onSeeking);
    for (const ev of ["timeupdate", "waiting", "play"]) this.video.addEventListener(ev, this.kick);
    this.loop();
  }

  stop() {
    this.stopped = true;
    if (this.ctrl) this.ctrl.abort();
    this.video.removeEventListener("seeking", this.onSeeking);
    for (const ev of ["timeupdate", "waiting", "play"]) this.video.removeEventListener(ev, this.kick);
    this.kick();
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
  }

  // ---------------------------------------------------------------- network

  async get(url, { text = false } = {}) {
    for (let attempt = 0; ; attempt++) {
      if (this.stopped) throw new LoaderError("stopped");
      this.ctrl = new AbortController();
      let r;
      try {
        r = await fetch(url, { signal: this.ctrl.signal, cache: "no-store" });
      } catch (err) {
        if (err.name === "AbortError") throw err;
        if (attempt >= 5) throw new LoaderError("offline");
        await sleep(1000);
        continue;
      }
      if (r.ok) return text ? r.text() : r.arrayBuffer();
      const body = await r.json().catch(() => ({}));
      if (r.status === 503 && body.active != null) throw new LoaderError("busy", body.detail);
      if (r.status === 503 && attempt < 20) { await sleep(1500); continue; }  // still being made
      if (r.status === 404) throw new LoaderError("gone");
      throw new LoaderError("failed", body.detail || `HTTP ${r.status}`);
    }
  }

  async readPlaylist() {
    const text = await this.get(this.url, { text: true });
    const list = parsePlaylist(text, new URL(this.url, location.href).href);
    if (!list.init || !list.segments.length) throw new LoaderError("failed", "Danh sách đoạn video trống.");
    this.list = list;
    this.lastPoll = Date.now();
  }

  /** The hub forgot this conversion (it was stopped after a long pause): start it again. */
  async renew() {
    if (!this.reopen || this.reopens >= 3) throw new LoaderError("gone");
    this.reopens++;
    this.url = await this.reopen();
    await this.readPlaylist();
  }

  // ---------------------------------------------------------------- buffer

  op(run) {
    return new Promise((resolve, reject) => {
      const sb = this.sb;
      const end = () => { sb.removeEventListener("error", fail); resolve(); };
      const fail = () => { sb.removeEventListener("updateend", end); reject(new LoaderError("failed", "Trình duyệt không nhận đoạn video.")); };
      sb.addEventListener("updateend", end, { once: true });
      sb.addEventListener("error", fail, { once: true });
      try { run(); } catch (err) {
        sb.removeEventListener("updateend", end);
        sb.removeEventListener("error", fail);
        reject(err);
      }
    });
  }

  async append(data) {
    try {
      await this.op(() => this.sb.appendBuffer(data));
    } catch (err) {
      if (err.name !== "QuotaExceededError") throw err;
      const t = this.video.currentTime;  // memory full: keep only around the position
      if (t > 5) await this.op(() => this.sb.remove(0, t - 5));
      await this.op(() => this.sb.remove(t + LOOKAHEAD_S, Infinity));
      this.done.clear();
      await this.op(() => this.sb.appendBuffer(data));
    }
  }

  async evict(t) {
    const b = this.sb.buffered;
    if (!b.length) return;
    const from = Math.max(0, t - KEEP_BEHIND_S), to = t + KEEP_AHEAD_MAX_S;
    if (b.start(0) < from - 1) await this.op(() => this.sb.remove(0, from));
    if (b.length && b.end(b.length - 1) > to + 1) await this.op(() => this.sb.remove(to, Infinity));
    for (const i of [...this.done]) {
      const s = this.list.segments[i];
      if (!s || s.end < from || s.start > to) this.done.delete(i);
    }
  }

  covered(i) {
    const s = this.list.segments[i];
    const b = this.video.buffered;
    for (let k = 0; k < b.length; k++) {
      if (b.start(k) <= s.start + TOLERANCE_S && b.end(k) >= s.end - TOLERANCE_S) return true;
    }
    return this.done.has(i);
  }

  bufferedEnd(t) {
    const b = this.video.buffered;
    for (let k = 0; k < b.length; k++) {
      if (b.start(k) - 0.5 <= t && t <= b.end(k)) return b.end(k);
    }
    return t;
  }

  /** Jump over a small hole at the playing position (pieces may start a few ms late). */
  nudge(t) {
    const b = this.video.buffered;
    for (let k = 0; k < b.length; k++) {
      if (b.start(k) > t && b.start(k) - t < 1) { this.video.currentTime = b.start(k) + 0.01; return; }
    }
  }

  // ---------------------------------------------------------------- main loop

  async loop() {
    while (!this.stopped) {
      let busy = false;
      try {
        busy = await this.step();
      } catch (err) {
        if (this.stopped) return;
        if (err.name === "AbortError") continue;  // a seek cancelled the download
        if (err.kind === "gone") {
          try { await this.renew(); continue; } catch (e) { err = e.kind ? e : err; }
        }
        this.onError(err instanceof LoaderError ? err : new LoaderError("failed", String(err.message || err)));
        return;
      }
      if (!busy) await new Promise((r) => { this.wake = r; setTimeout(r, 500); });
    }
  }

  async step() {
    if (!this.list.ended && Date.now() - this.lastPoll > POLL_MS) await this.readPlaylist();
    const t = this.video.currentTime;
    if (this.video.readyState < 3) this.nudge(t);
    const end = this.bufferedEnd(t);
    if (end - t >= LOOKAHEAD_S) return false;
    const segs = this.list.segments;
    let i = segs.findIndex((s) => s.end > end + 0.05);
    while (i >= 0 && i < segs.length && this.covered(i)) i++;
    if (i < 0 || i >= segs.length) {
      if (this.list.ended && this.ms.readyState === "open" && !this.sb.updating && end >= this.list.duration - 1) {
        try { this.ms.endOfStream(); } catch { /* already ended */ }
      }
      return false;
    }
    const gen = this.seekGen;
    const data = await this.get(segs[i].url);
    if (gen !== this.seekGen || this.stopped) return true;  // seeked meanwhile: what is needed changed
    await this.evict(t);
    await this.append(data);
    this.done.add(i);
    return true;
  }
}
