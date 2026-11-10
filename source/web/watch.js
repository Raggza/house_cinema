// House Cinema — player page /watch/<id> (plan step 4.3).
// Native HTML5 <video> controls (seek, fullscreen, picture-in-picture work the same on every
// phone and computer, iOS included), plus: subtitle choice, resume where you stopped (saved
// in this browser), and a clear message when the browser cannot play the file's format.
// Text from the server is always inserted with textContent.
// A video the browser cannot play is converted by the hub (step 5.1) and played through
// segments.js; only when that is impossible does the page say "Định dạng chưa hỗ trợ".
// ?t=<seconds>: start there. ?screen=1 (step 5.4): this page runs inside the TV page /tv,
// fills the screen, takes commands from it (postMessage) and reports its state back.
// ?list=<id>&p=<n> (step 6.5): play a playlist (kept in sessionStorage by playlist.js).
// The next video opens in this same page (no reload), so fullscreen stays on.

import { LoaderError, SegmentPlayer, decodableCodecs, hlsSupport } from "/static/segments.js";
import * as PL from "/static/playlist.js";

const RESUME_MIN_S = 5;        // do not "resume" at 0:03
const RESUME_END_MARGIN_S = 10; // near the end counts as finished
const SAVE_EVERY_MS = 5000;
const POS_PREFIX = "hub:pos:";
const SUB_PREF = "hub:subtitles";

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child != null) node.append(child);
  return node;
};
const video = $("#video");
let mediaId = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
const params = new URLSearchParams(location.search);
const SCREEN = params.get("screen") === "1" && window.parent !== window;
const START_AT = (() => { const t = Number(params.get("t")); return isFinite(t) && t > 0 ? t : null; })();
const UPNEXT_S = 5;           // countdown before the next video of a playlist
const MAX_SKIPS = 3;          // this many videos in a row that cannot play: the playlist stops
let startAt = START_AT;       // only for the first video of the page
let media = null;
let list = null;              // the playlist being played (step 6.5), or null
let problem = null;           // TV mode: "failed" | "busy" while such a notice shows
let started = false;
let conversion = null;     // {plan, loader, hls} while a converted version plays
const excluded = new Set(); // codecs this browser claimed but could not decode
let soundBlocked = false;   // TV mode: the browser only allowed muted playback

// ------------------------------------------------------------------ helpers

function formatTime(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m)) + `:${String(r).padStart(2, "0")}`;
}

function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toLocaleString("vi-VN", { maximumFractionDigits: v < 10 && i ? 1 : 0 })} ${units[i]}`;
}

const storage = {  // localStorage can throw (private mode, disabled): never break playback
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
  remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

class HubError extends Error {
  constructor(kind, status) { super(kind); this.kind = kind; this.status = status; }
}

async function api(path) {
  let r;
  try { r = await fetch(path, { cache: "no-store", headers: { Accept: "application/json" } }); }
  catch { throw new HubError("offline"); }
  if (r.status === 401) toPin();
  if (!r.ok) throw new HubError("http", r.status);
  return r.json();
}

function toPin() {  // the PIN cookie expired, or the PIN was changed (5.2)
  location.href = `/pin?next=${encodeURIComponent(location.pathname + location.search)}`;
}

// ------------------------------------------------------------------ messages on the stage

function showNotice({ title, lines = [], actions = [], kind = "error" }) {
  const box = $("#notice");
  box.replaceChildren(el("h2", { textContent: title }),
                      ...lines.filter(Boolean).map((t) => el("p", { textContent: t })));
  if (actions.length) {
    const row = el("div", { className: "notice-actions" });
    for (const a of actions) {
      const b = el("button", { className: "btn" + (a.primary ? " primary" : ""), type: "button", textContent: a.label });
      b.addEventListener("click", a.run);
      row.append(b);
    }
    box.append(row);
  }
  box.className = `stage-notice ${kind}`;
  box.hidden = false;
  $("#stage").classList.add("has-notice");
  reportSoon();
}

function hideNotice() {
  $("#notice").hidden = true;
  $("#stage").classList.remove("has-notice");
  problem = null;
  reportSoon();
}

/** This video cannot be played (missing, unreadable, unsupported…): in a playlist, move on to
 * the next one after a countdown; after MAX_SKIPS in a row, stop. On the TV the TV page decides
 * (it gets `problem` in the state). Not for "Hub đang bận" or a lost connection: the next video
 * would fail the same way. */
function cannotPlay() {
  problem = "failed";
  reportSoon();
  if (!list) return;
  list.fails += 1;
  PL.saveList(list);
  const next = PL.nextPos(list);
  if (list.fails >= MAX_SKIPS) {
    appendNoticeLine(`${MAX_SKIPS} video liên tiếp không phát được nên danh sách phát đã dừng.`);
    return;
  }
  if (next == null) return;
  upNext(next, `Không phát được video này. Chuyển sang: ${PL.itemAt(list, next).title}`);
}

function appendNoticeLine(text) {
  const box = $("#notice");
  if (box.hidden) return;
  const p = el("p", { className: "notice-extra", textContent: text });
  const row = box.querySelector(".notice-actions");
  row ? box.insertBefore(p, row) : box.append(p);
}

function codecLine(m) {
  const parts = [];
  if (m.video_codec) parts.push(`Hình: ${m.video_codec}${m.pix_fmt ? ` (${m.pix_fmt})` : ""}`);
  if (m.audio_codec) parts.push(`Âm thanh: ${m.audio_codec}`);
  if (m.container) parts.push(`Định dạng file: ${m.container} (${m.extension})`);
  return parts.join(" · ");
}

function vlcActions() {
  return [{ label: "Sao chép link cho VLC", run: copyLink }];
}

function showUnsupported(reason, { canTry = false, why = null } = {}) {
  video.pause();
  const lines = [
    reason,
    codecLine(media),
    media.playable_note ? `Lý do: ${media.playable_note}.` : null,
    why ? `Hub không chuyển đổi được: ${why}` : null,
    "Bạn có thể mở video bằng VLC (Media › Open Network Stream) với link bên dưới, hoặc thử trình duyệt khác.",
  ];
  const actions = [...vlcActions()];
  if (canTry) actions.unshift({ label: "Vẫn thử phát", primary: true, run: () => { cancelUpNext(); hideNotice(); startPlayer(); } });
  showNotice({ title: "Định dạng chưa hỗ trợ", lines, actions });
  cannotPlay();
}

// ------------------------------------------------------------------ support check

// Asking with the codec matters: Edge answers "maybe" to a bare "video/x-matroska" and then
// plays the sound of an HEVC file without its picture.
const VIDEO_CODECS = { h264: "avc1.640028", hevc: "hvc1.1.6.L93.B0", av1: "av01.0.05M.08",
                       vp9: "vp09.00.10.08", vp8: "vp8" };
const AUDIO_CODECS = { aac: "mp4a.40.2", mp3: "mp4a.6B", opus: "opus", vorbis: "vorbis",
                       ac3: "ac-3", eac3: "ec-3", flac: "flac" };

function mimeFor(m) {
  const c = (m.container || "").toLowerCase();
  const container = { matroska: "video/x-matroska", webm: "video/webm", mp4: "video/mp4", mov: "video/mp4" }[c];
  if (!container) return "";
  const v = VIDEO_CODECS[(m.video_codec || "").toLowerCase()];
  const a = m.audio_codec ? AUDIO_CODECS[m.audio_codec.toLowerCase()] : null;
  if ((m.video_codec && !v) || (m.audio_codec && !a)) return "";  // a codec we cannot even name
  const codecs = [v, a].filter(Boolean).join(", ");
  return codecs ? `${container}; codecs="${codecs}"` : container;
}

/** "yes" | "maybe" | "no": the hub's verdict (step 2.3) refined with this browser's own answer. */
function supportFor(m) {
  if (m.browser_playable !== false) return "yes";  // true, or unknown: just try
  const mime = mimeFor(m);
  const answer = mime ? video.canPlayType(mime) : "";
  return answer ? "maybe" : "no";  // e.g. Safari says it can play HEVC
}

// ------------------------------------------------------------------ subtitles

function setupSubtitles() {
  for (const old of video.querySelectorAll("track")) old.remove();  // the previous video's
  $("#sub-select").hidden = true;
  const tracks = media.subtitles || [];
  if (!tracks.length) return;
  const pref = storage.get(SUB_PREF);  // remembered choice: a language, or "off"
  const chosen = pref === "off" ? null
    : tracks.find((t) => t.lang === pref) || tracks.find((t) => t.lang === "vi") || (pref == null ? null : tracks[0]);
  for (const t of tracks) {
    // `default` on our choice: otherwise the browser picks a track by its own language.
    video.append(el("track", { kind: "subtitles", label: t.label, srclang: t.lang, src: t.url,
                               default: chosen === t }));
  }
  const select = $("#subtitles");
  select.replaceChildren(el("option", { value: "off", textContent: "Tắt" }),
                         ...tracks.map((t) => el("option", { value: t.key, textContent: t.label })));
  select.value = chosen ? chosen.key : "off";
  applySubtitle(select.value);
  // The browser's automatic track selection runs when the video loads: apply ours again.
  video.addEventListener("loadedmetadata", () => applySubtitle(select.value), { once: true });
  $("#sub-select").hidden = false;
}

function onSubtitleChange() {  // set once for the page; `media` is the video shown now
  const select = $("#subtitles");
  applySubtitle(select.value);
  const t = ((media && media.subtitles) || []).find((x) => x.key === select.value);
  storage.set(SUB_PREF, t ? t.lang : "off");
}

function applySubtitle(key) {
  const tracks = media.subtitles || [];
  [...video.textTracks].forEach((tt, i) => {
    tt.mode = tracks[i] && tracks[i].key === key ? "showing" : "disabled";
  });
}

function toggleSubtitles() {
  const select = $("#subtitles");
  if ($("#sub-select").hidden) return;
  const tracks = media.subtitles;
  if (select.value === "off") {
    const pref = storage.get(SUB_PREF);
    select.value = (tracks.find((t) => t.lang === pref) || tracks.find((t) => t.lang === "vi") || tracks[0]).key;
  } else {
    select.value = "off";
  }
  select.dispatchEvent(new Event("change"));
  toast(select.value === "off" ? "Đã tắt phụ đề" : `Phụ đề: ${select.selectedOptions[0].textContent}`);
}

// ------------------------------------------------------------------ resume

const posKey = () => POS_PREFIX + mediaId;
let lastSave = 0;

function savePosition(force = false) {
  if (!started || !isFinite(video.currentTime)) return;
  const now = Date.now();
  if (!force && now - lastSave < SAVE_EVERY_MS) return;
  lastSave = now;
  const d = isFinite(video.duration) ? video.duration : media.duration;
  if (d && video.currentTime >= d - RESUME_END_MARGIN_S) storage.remove(posKey());  // finished
  else if (video.currentTime >= RESUME_MIN_S) storage.set(posKey(), { t: video.currentTime, d, at: now });
}

/** Where to start: ?t= (e.g. where the phone was, step 5.4), else where this browser stopped. */
function startPoint() {
  return startAt != null ? { at: startAt, isResume: false } : { at: resumePoint(), isResume: true };
}

function resumePoint() {
  const saved = storage.get(posKey());
  if (!saved || typeof saved.t !== "number") return null;
  const d = media.duration || saved.d;
  if (saved.t < RESUME_MIN_S || (d && saved.t >= d - RESUME_END_MARGIN_S)) return null;
  return saved.t;
}

function showResume(t) {
  $("#resume-text").textContent = `Đang phát tiếp từ ${formatTime(t)}.`;
  $("#resume").hidden = false;
  setTimeout(() => { $("#resume").hidden = true; }, 10000);
}

// ------------------------------------------------------------------ player

let listening = false;
let directFailed = false;
let attachGen = 0;

/** Listeners that stay for the whole page, whatever the source (direct or converted). */
function listenOnce() {
  if (listening) return;
  listening = true;
  video.addEventListener("error", onVideoError);
  video.addEventListener("timeupdate", () => savePosition());
  video.addEventListener("pause", () => savePosition(true));
  video.addEventListener("seeked", () => savePosition(true));
  video.addEventListener("ended", () => { storage.remove(posKey()); onEnded(); });
  for (const ev of ["canplay", "playing"]) {
    video.addEventListener(ev, () => { if ($("#notice").classList.contains("info")) hideNotice(); });
  }
  video.addEventListener("playing", () => {  // it plays: the "in a row" count starts again
    if (list && list.fails) { list.fails = 0; PL.saveList(list); }
  });
  window.addEventListener("pagehide", () => { savePosition(true); stopSessionNow(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(true); });
}

/** Give the <video> its source, go to `at` once loaded, and check that a picture shows. */
function attach(setSource, at, isResume) {
  const gen = ++attachGen;
  video.addEventListener("loadedmetadata", () => {
    if (gen !== attachGen) return;
    if (at != null && at > 0 && at < video.duration) {
      video.currentTime = at;
      if (isResume) showResume(at);
    }
    if (!media.video_codec) return;
    // HEVC in a browser without HEVC: sound plays, picture stays black (no width / no frame).
    setTimeout(() => { if (gen === attachGen && video.videoWidth === 0 && !video.error) noPicture(); }, 1500);
    const from = video.currentTime;
    const onTime = () => {
      if (gen !== attachGen) { video.removeEventListener("timeupdate", onTime); return; }
      if (video.currentTime - from < 1) return;
      video.removeEventListener("timeupdate", onTime);
      const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
      const frames = q ? q.totalVideoFrames : video.webkitDecodedFrameCount;
      if (video.videoWidth === 0 || frames === 0) noPicture();
    };
    video.addEventListener("timeupdate", onTime);
  }, { once: true });
  setSource();
  tryPlay();
}

/** play(); on the TV (step 5.4), if the browser refuses sound before anyone clicked the page,
 * play muted and tell the phone. Elsewhere a refused autoplay just waits for the user. */
function tryPlay() {
  video.play().catch((err) => {
    if (SCREEN && err && err.name === "NotAllowedError" && !video.muted) {
      soundBlocked = true;
      video.muted = true;
      video.play().catch(() => {});
      reportSoon();
    }
  });
}

/** Play the file as it is. */
function startPlayer() {
  const firstTime = !started;
  started = true;
  listenOnce();
  const { at, isResume } = firstTime ? startPoint() : { at: video.currentTime, isResume: false };
  attach(() => { video.src = media.stream_url; }, at, isResume);
}

function noPicture() {
  if (conversion && conversion.plan.mode === "transcode") {
    showUnsupported("Trình duyệt này không hiện được hình của video, kể cả sau khi chuyển đổi.");
    return;
  }
  excluded.add(media.video_codec);  // the browser claimed it but cannot: convert the picture
  convert("Trình duyệt này phát được âm thanh nhưng không giải mã được hình của video.");
}

function onVideoError() {
  if (!started) return;  // the source was just cleared to open the next video of a playlist
  const code = video.error ? video.error.code : 0;
  if (code === 4 || code === 3) {  // SRC_NOT_SUPPORTED, DECODE
    if (!conversion) {
      directFailed = true;
      if (code === 3) excluded.add(media.video_codec);
      convert(code === 3 ? "Trình duyệt không giải mã được video này."
                         : "Trình duyệt này không phát được định dạng của file.");
    } else if (conversion.plan.mode === "remux") {
      excluded.add(media.video_codec);
      convert("Trình duyệt không giải mã được hình của video.");
    } else {
      showUnsupported("Trình duyệt không phát được video đã chuyển đổi.");
    }
  } else if (code === 2) {  // NETWORK
    showNotice({ title: "Mất kết nối tới hub",
                 lines: ["Video bị ngắt giữa chừng. Kiểm tra hub còn chạy và mạng Wi-Fi, rồi thử lại."],
                 actions: [{ label: "Thử lại", primary: true, run: () => location.reload() }] });
  }
}

// ------------------------------------------------------------------ conversion (step 5.1)

async function requestPlan(hls) {
  const caps = decodableCodecs(video, hls);
  const body = { video: caps.video.filter((c) => !excluded.has(c)), audio: caps.audio, avoid_direct: directFailed };
  let r;
  try {
    r = await fetch(`/api/play/${encodeURIComponent(mediaId)}`, {
      method: "POST", cache: "no-store", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", Accept: "application/json" } });
  } catch { throw new HubError("offline"); }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) toPin();
  if (!r.ok) {
    const err = new HubError(r.status === 503 ? "busy" : r.status === 409 ? "cannot" : "http", r.status);
    err.detail = data.detail;
    throw err;
  }
  return data;
}

function stopSessionNow() {
  const session = conversion && conversion.plan.session;
  if (session && navigator.sendBeacon) navigator.sendBeacon(`/api/play/sessions/${session}/stop`);
}

function endConversion() {
  if (!conversion) return;
  if (conversion.loader) conversion.loader.stop();
  stopSessionNow();
  conversion = null;
}

function showBusy(detail, why) {
  video.pause();
  showNotice({ title: "Hub đang bận",
               lines: [detail || "Hub đang chuyển đổi nhiều video cùng lúc.",
                       "Video này cần được chuyển đổi trước khi phát trên thiết bị này."],
               actions: [{ label: "Thử lại", primary: true, run: () => convert(why) }, ...vlcActions()] });
  problem = "busy";  // a playlist waits here: the next video would be refused the same way
  reportSoon();
}

function conversionFailed(err, why) {
  if (err.kind === "busy") showBusy(err.detail, why);
  else if (err.kind === "offline") {
    showNotice({ title: "Mất kết nối tới hub", lines: ["Kiểm tra hub còn chạy và mạng Wi-Fi, rồi thử lại."],
                 actions: [{ label: "Thử lại", primary: true, run: () => location.reload() }] });
  } else if (err.status === 404) {
    showNotice({ title: "Không tìm thấy video",
                 lines: ["Video có thể đã bị xoá, đổi tên, hoặc thư mục chứa nó đã bị bỏ khỏi thư viện."],
                 actions: [{ label: "Về thư viện", primary: true, run: () => { location.href = $("#back").href; } }] });
    cannotPlay();
  } else {
    showUnsupported(why, { canTry: !directFailed, why: err.detail || (err.status ? `mã lỗi ${err.status}` : err.message) });
  }
}

/** Ask the hub for a converted version and play it from the current position. */
async function convert(why) {
  const pos = video.currentTime;
  const first = startPoint();
  const at = pos > 1 ? pos : first.at;  // keep the position when switching
  const isResume = !(pos > 1) && at != null && first.isResume;
  endConversion();
  video.pause();
  const hls = hlsSupport(video);
  if (!hls) {
    showUnsupported(why, { why: "trình duyệt này không phát được video đã chuyển đổi (thiếu MediaSource và HLS)." });
    return;
  }
  showNotice({ title: "Đang chuẩn bị video…", lines: [why], kind: "info" });
  let plan;
  try {
    plan = await requestPlan(hls);
  } catch (err) {
    conversionFailed(err, why);
    return;
  }
  if (plan.mode === "direct") { hideNotice(); startPlayer(); return; }
  showNotice({ title: "Đang chuẩn bị video…", lines: [plan.reason, "Video sẽ bắt đầu sau vài giây."], kind: "info" });
  started = true;
  listenOnce();
  const current = { plan, hls, loader: null };
  conversion = current;
  attach(() => {
    if (hls === "native") { video.src = plan.url; return; }  // Safari plays HLS itself
    const loader = new SegmentPlayer(video, {
      url: plan.url, mime: plan.mime, duration: media.duration,
      reopen: async () => { current.plan = await requestPlan(hls); return current.plan.url; },
      onError: (e) => { if (conversion === current) conversionFailed(e, why); },
    });
    current.loader = loader;
    loader.start().catch((e) => {
      if (conversion === current && e.name !== "AbortError") {
        conversionFailed(e instanceof LoaderError ? e : new LoaderError("failed", e.message), why);
      }
    });
  }, at, isResume);
}

// ------------------------------------------------------------------ TV screen mode (step 5.4)
// Inside /tv: commands come from the TV page (which gets them from the phone), and the state
// goes back to it. Only messages from our own origin and our own parent are taken.

let reportTimer = null;
let lastReport = 0;

function screenState() {
  const box = $("#notice");
  const notice = box.hidden ? null : {
    title: (box.querySelector("h2") || {}).textContent || "",
    lines: [...box.querySelectorAll("p")].map((p) => p.textContent.slice(0, 500)).slice(0, 8),
    kind: box.classList.contains("info") ? "info" : "error",
  };
  const d = isFinite(video.duration) && video.duration > 0 ? video.duration : (media && media.duration) || null;
  const select = $("#subtitles");
  const tracks = (media && media.subtitles) || [];
  const status = video.ended ? "ended"
    : video.paused ? (started && video.readyState >= 1 ? "paused" : "loading")
    : video.readyState < 3 ? "loading" : "playing";
  return {
    status, media_id: mediaId.slice(0, 200), title: media ? media.title.slice(0, 300) : null,
    position: Math.max(0, video.currentTime || 0), duration: d, volume: video.volume, muted: video.muted,
    subtitles: tracks.slice(0, 30).map((t) => ({ key: t.key.slice(0, 100), label: t.label.slice(0, 100) })),
    subtitle: tracks.length ? select.value : "off", notice, sound_blocked: soundBlocked,
    problem: box.hidden ? null : problem,
  };
}

function report() {
  clearTimeout(reportTimer);
  reportTimer = null;
  lastReport = Date.now();
  try { window.parent.postMessage({ type: "hub-state", state: screenState() }, location.origin); } catch { /* ignore */ }
}

/** Report now-ish: several changes in a row are sent once. */
function reportSoon() {
  if (!SCREEN || reportTimer) return;
  reportTimer = setTimeout(report, 150);
}

function seekTo(t) {
  const end = isFinite(video.duration) ? video.duration : (media && media.duration) || Infinity;
  video.currentTime = Math.max(0, Math.min(end - 0.5, t));
}

function unmute() {
  video.muted = false;
  if (!soundBlocked) return;
  // Chrome pauses a video unmuted without a click on the page: then stay muted, keep playing.
  setTimeout(() => {
    if (video.paused && !video.ended) { video.muted = true; tryPlay(); }
    else soundBlocked = false;
    reportSoon();
  }, 300);
}

function runCommand(c) {
  switch (c && c.action) {
    case "play": tryPlay(); break;
    case "pause": video.pause(); break;
    case "seek": seekTo(Number(c.to) || 0); break;
    case "seek_by": seekTo(video.currentTime + (Number(c.by) || 0)); break;
    case "volume":
      video.volume = Math.max(0, Math.min(1, Number(c.level)));
      if (video.volume > 0 && video.muted) unmute();
      break;
    case "mute": if (c.on) video.muted = true; else unmute(); break;
    case "subtitle": {
      const select = $("#subtitles");
      if ($("#sub-select").hidden) break;
      if ([...select.options].some((o) => o.value === c.key)) {
        select.value = c.key;
        select.dispatchEvent(new Event("change"));
      }
      break;
    }
    default:
  }
  reportSoon();
}

function setupScreen() {
  document.body.classList.add("screen-mode");
  video.removeAttribute("controls");  // the phone is the remote; the tiny native bar is hard to hit with a cursor
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || e.source !== window.parent) return;
    if (e.data && e.data.type === "hub-command") runCommand(e.data.command);
    else if (e.data && e.data.type === "hub-report") report();
  });
  for (const ev of ["play", "playing", "pause", "seeked", "volumechange", "ended", "loadedmetadata", "waiting", "canplay"]) {
    video.addEventListener(ev, reportSoon);
  }
  video.addEventListener("timeupdate", () => { if (Date.now() - lastReport > 1000) reportSoon(); });
  $("#subtitles").addEventListener("change", reportSoon);
  // Sound blocked by the browser: one click on the TV (with the cursor) is the gesture it wants.
  document.addEventListener("click", () => {
    if (!soundBlocked) return;
    soundBlocked = false;
    video.muted = false;
    tryPlay();
    reportSoon();
  });
  setInterval(reportSoon, 3000);
}

// ------------------------------------------------------------------ page

/** "Phát trên TV": stop here and let the TV continue from this position (step 5.4). */
function castToTv() {
  const t = started ? video.currentTime : (startPoint().at || 0);
  video.pause();
  savePosition(true);
  cancelUpNext();
  if (list) {  // the whole playlist goes to the TV, from this video and this position
    PL.saveList(list);
    location.href = `/remote?list=${list.id}&t=${Math.floor(t || 0)}`;
    return;
  }
  location.href = `/remote?play=${encodeURIComponent(mediaId)}&t=${Math.floor(t || 0)}`;
}

function renderDetails() {
  document.title = `${media.title} – House Cinema`;
  $("#title").textContent = media.title;
  $("#where").textContent = [media.library.name, ...(media.folder ? media.folder.split("/") : [])].join(" › ");
  const res = media.width && media.height ? `${media.width}×${media.height}` : "";
  $("#meta").textContent = [formatTime(media.duration), res, formatSize(media.size),
                            [media.video_codec, media.audio_codec].filter(Boolean).join(" / ")]
    .filter(Boolean).join(" · ");
  video.poster = media.thumbnail_url;
}

async function copyLink() {
  const url = new URL(media ? media.stream_url : `/stream/${mediaId}`, location.href).href;
  try {
    await navigator.clipboard.writeText(url);
    toast("Đã sao chép link. Trong VLC: Media › Open Network Stream, rồi dán.");
  } catch {
    window.prompt("Sao chép link này rồi mở trong VLC (Media › Open Network Stream):", url);
  }
}

function setupKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    if (!started && !(list && "nNpP".includes(e.key))) return;  // N/P also work on an error notice
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    const onVideo = t === video;  // the native controls already handle keys on the video
    switch (e.key) {
      case " ": case "k": case "K":
        if (onVideo && e.key === " ") return;
        e.preventDefault();
        video.paused ? video.play().catch(() => {}) : video.pause();
        break;
      case "ArrowLeft": case "j": case "J":
        if (onVideo && e.key === "ArrowLeft") return;
        e.preventDefault();
        video.currentTime = Math.max(0, video.currentTime - 10);
        break;
      case "ArrowRight": case "l": case "L":
        if (onVideo && e.key === "ArrowRight") return;
        e.preventDefault();
        video.currentTime = Math.min(video.duration || Infinity, video.currentTime + 10);
        break;
      case "f": case "F":
        e.preventDefault();
        toggleFullscreen();
        break;
      case "m": case "M":
        video.muted = !video.muted;
        toast(video.muted ? "Đã tắt tiếng" : "Đã bật tiếng");
        break;
      case "c": case "C":
        toggleSubtitles();
        break;
      case "n": case "N":  // playlist: next / previous video
        if (list) { e.preventDefault(); goNext(); }
        break;
      case "p": case "P":
        if (list) { e.preventDefault(); goPrev(); }
        break;
      default:
    }
  });
}

function toggleFullscreen() {
  if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
  if (video.requestFullscreen) video.requestFullscreen().catch(() => {});
  else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();  // iPhone
}

async function init() {
  try {
    const back = sessionStorage.getItem("hub:libraryUrl");
    if (back && back.startsWith("/")) $("#back").href = back;
  } catch { /* ignore */ }
  $("#restart").addEventListener("click", () => {
    video.currentTime = 0;
    storage.remove(posKey());
    $("#resume").hidden = true;
    video.play().catch(() => {});
  });
  $("#copy-link").addEventListener("click", copyLink);
  $("#cast").addEventListener("click", castToTv);
  $("#subtitles").addEventListener("change", onSubtitleChange);
  setupKeys();
  if (SCREEN) setupScreen();
  else setupPlaylist();
  await openMedia(mediaId);
}

/** Show and start one video. Called once at load, then for each video of a playlist: the
 * previous one is cleaned up (its conversion stopped, its subtitles removed) first. */
async function openMedia(id) {
  const gen = ++openGen;
  if (media) {  // switching inside the page
    savePosition(true);
    endConversion();
    attachGen++;  // a pending "loadedmetadata" of the previous video must not act
    video.pause();
    video.removeAttribute("src");
    video.load();
    hideNotice();
    $("#resume").hidden = true;
    started = false;
    directFailed = false;
    excluded.clear();
    startAt = null;  // ?t= was for the first video only
  }
  mediaId = id;
  media = null;
  try {
    const m = await api(`/api/media/${encodeURIComponent(mediaId)}`);
    if (gen !== openGen) return;
    media = m;
  } catch (err) {
    if (gen !== openGen) return;
    document.title = "Không phát được – House Cinema";
    if (err.kind === "offline") {
      showNotice({ title: "Không kết nối được tới hub",
                   lines: ["Kiểm tra máy chủ còn đang chạy và thiết bị này dùng cùng mạng với máy chủ."],
                   actions: [{ label: "Thử lại", primary: true, run: () => location.reload() }] });
    } else if (err.status === 404) {
      $("#title").textContent = list ? PL.current(list).title : "";
      showNotice({ title: "Không tìm thấy video",
                   lines: ["Video có thể đã bị xoá, đổi tên, hoặc thư mục chứa nó đã bị bỏ khỏi thư viện."],
                   actions: [{ label: "Về thư viện", primary: true, run: () => { location.href = $("#back").href; } }] });
      cannotPlay();
    } else {
      showNotice({ title: "Hub gặp lỗi", lines: [`Máy chủ trả về lỗi (mã ${err.status}).`],
                   actions: [{ label: "Thử lại", primary: true, run: () => location.reload() }] });
    }
    return;
  }
  renderDetails();
  setupSubtitles();

  if (!media.available) {
    showNotice({ title: "Ổ chứa video đang ngắt kết nối",
                 lines: ["Thư mục chứa video này hiện không truy cập được (ví dụ ổ USB đã rút). Cắm lại ổ rồi thử lại."],
                 actions: [{ label: "Thử lại", primary: true, run: () => location.reload() }] });
    cannotPlay();
    return;
  }
  if (media.info_status === "failed") {
    showNotice({ title: "Không đọc được file video",
                 lines: ["File có thể bị hỏng hoặc chưa tải xong.", media.info_error ? `Chi tiết: ${media.info_error}` : null],
                 actions: [{ label: "Vẫn thử phát", run: () => { cancelUpNext(); hideNotice(); startPlayer(); } },
                           ...vlcActions()] });
    cannotPlay();
    return;
  }
  const support = supportFor(media);
  if (support === "no") {
    convert("Trình duyệt này không phát trực tiếp được video này nên hub chuyển đổi nó.");
    return;
  }
  if (support === "maybe") toast("Định dạng này có thể không phát được trên mọi thiết bị.");
  startPlayer();
}

// ------------------------------------------------------------------ playlist (step 6.5)

let openGen = 0;
let upNextTimer = null;
let upNextPos = null;

function setupPlaylist() {
  const id = params.get("list");
  if (!id) return;
  list = PL.loadList(id);
  if (!list) {  // another tab, or an old link: play this video alone
    history.replaceState(null, "", location.pathname);
    return;
  }
  const p = Number(params.get("p"));
  if (Number.isInteger(p) && p >= 0 && p < PL.size(list) && PL.itemAt(list, p).id === mediaId) list.pos = p;
  else {
    const at = list.items.findIndex((it) => it.id === mediaId);
    if (at < 0) { list = null; history.replaceState(null, "", location.pathname); return; }
    list.pos = PL.posOfIndex(list, at);
  }
  PL.saveList(list);
  $("#pl-prev").addEventListener("click", goPrev);
  $("#pl-next").addEventListener("click", goNext);
  $("#pl-shuffle").addEventListener("click", () => {
    PL.setShuffle(list, !list.shuffle);
    PL.saveList(list);
    history.replaceState(null, "", PL.watchUrl(list));
    renderPlaylist();
    toast(list.shuffle ? "Phát ngẫu nhiên: bật" : "Phát ngẫu nhiên: tắt");
  });
  $("#pl-repeat").addEventListener("click", () => {
    PL.cycleRepeat(list);
    PL.saveList(list);
    renderPlaylist();
    toast(PL.REPEAT_LABEL[list.repeat]);
  });
  $("#upnext-now").addEventListener("click", () => { if (upNextPos != null) goTo(upNextPos); });
  $("#upnext-cancel").addEventListener("click", cancelUpNext);
  $("#playlist").hidden = false;
  renderPlaylist();
}

function renderPlaylist() {
  if (!list) return;
  const n = PL.size(list);
  $("#pl-title").textContent = list.title ? `Danh sách phát · ${list.title}` : "Danh sách phát";
  $("#pl-pos").textContent = `${list.pos + 1}/${n}`;
  $("#pl-prev").disabled = PL.prevPos(list) == null;
  $("#pl-next").disabled = PL.nextPos(list) == null;
  const shuffle = $("#pl-shuffle");
  shuffle.setAttribute("aria-pressed", String(list.shuffle));
  const repeat = $("#pl-repeat");
  repeat.setAttribute("aria-pressed", String(list.repeat !== "off"));
  repeat.dataset.mode = list.repeat;
  repeat.querySelector(".label").textContent = PL.REPEAT_LABEL[list.repeat];
  const items = list.order.map((index, pos) => {
    const it = list.items[index];
    const b = el("button", { type: "button", className: "pl-item" },
                 el("span", { className: "pl-num", textContent: String(pos + 1) }),
                 el("span", { className: "pl-name", textContent: it.title || it.id }));
    if (pos === list.pos) b.setAttribute("aria-current", "true");
    b.addEventListener("click", () => goTo(pos));
    return el("li", {}, b);
  });
  $("#pl-items").replaceChildren(...items);
  const now = $("#pl-items [aria-current]");
  if (now && now.scrollIntoView) now.scrollIntoView({ block: "nearest" });
}

/** Play the video at `pos` of the playlist order, in this page. */
function goTo(pos) {
  if (!list || pos == null || pos < 0 || pos >= PL.size(list)) return;
  cancelUpNext();
  list.pos = pos;
  PL.saveList(list);
  history.replaceState(null, "", PL.watchUrl(list));
  renderPlaylist();
  openMedia(PL.current(list).id);
}

function goNext() {
  const pos = PL.nextPos(list);
  if (pos == null) { toast("Đây là video cuối của danh sách."); return; }
  goTo(pos);
}

function goPrev() {
  const pos = PL.prevPos(list);
  if (pos == null) { toast("Đây là video đầu của danh sách."); return; }
  goTo(pos);
}

function onEnded() {
  if (!list || SCREEN) return;
  if (list.repeat === "one") {  // the same video again
    video.currentTime = 0;
    tryPlay();
    return;
  }
  const next = PL.nextPos(list, { auto: true });
  if (next == null) {
    showNotice({ title: "Đã phát hết danh sách", kind: "info",
                 lines: [`${PL.size(list)} video${list.title ? ` · ${list.title}` : ""}`],
                 actions: [{ label: "Phát lại từ đầu", primary: true, run: () => goTo(0) },
                           { label: "Về thư viện", run: () => { location.href = $("#back").href; } }] });
    return;
  }
  upNext(next, `Tiếp theo: ${PL.itemAt(list, next).title}`);
}

/** "Tiếp theo: …" with a countdown; "Phát ngay" or "Huỷ". */
function upNext(pos, text) {
  cancelUpNext();
  upNextPos = pos;
  let left = UPNEXT_S;
  const show = () => { $("#upnext-text").textContent = `${text} (${left} giây)`; };
  show();
  $("#upnext").hidden = false;
  upNextTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) { const p = upNextPos; cancelUpNext(); goTo(p); return; }
    show();
  }, 1000);
}

function cancelUpNext() {
  clearInterval(upNextTimer);
  upNextTimer = null;
  upNextPos = null;
  const box = $("#upnext");
  if (box) box.hidden = true;
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
