// House Cinema — device test (plan step 4.4). Plays a sample of each format, seeks, checks
// subtitles, asks the tester what only a person can see, and sends the report to the hub.
// ?auto=1&device=<name>: no questions, sends by itself (for unattended browsers).
// Also the remote-key test for smart TVs (plan step 5.4), at the end of this file.

const $ = (sel) => document.querySelector(sel);
const params = new URLSearchParams(location.search);
const AUTO = params.get("auto") === "1";
const video = $("#video");
const report = { device: "", user_agent: navigator.userAgent.slice(0, 600), auto: AUTO,
                 screen: `${screen.width}×${screen.height}@${window.devicePixelRatio || 1}`,
                 touch: "ontouchstart" in window || navigator.maxTouchPoints > 0,
                 checks: {}, formats: [], confirmations: {}, notes: "" };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function guessDevice() {
  const ua = navigator.userAgent;
  // Safari on iPad (and on iPhone with "Request Desktop Website") says "Macintosh": a Mac has
  // no touch screen, so touch + a Mac user agent is an iPhone/iPad; the screen width tells which.
  const appleTouch = /Macintosh/.test(ua) && navigator.maxTouchPoints > 0;
  const os = /iPhone/.test(ua) ? "iPhone" : /iPad|Macintosh.*Mobile/.test(ua) ? "iPad"
    : appleTouch ? (Math.min(screen.width, screen.height) <= 500 ? "iPhone" : "iPad")
    : /BRAVIA/i.test(ua) ? "Sony TV" : /Tizen/.test(ua) ? "Samsung TV" : /Web0S|webOS/.test(ua) ? "LG TV"
    : /Android.*\b(TV|AFT\w*)\b|GoogleTV|Android TV/i.test(ua) ? "Android TV"
    : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows" : /Mac OS X/.test(ua) ? "Mac"
    : /Linux/.test(ua) ? "Linux" : "Thiết bị";
  // "; wv)": an Android WebView inside an app (e.g. the TV browsers BrowseHere, TV Bro), not Chrome.
  const m = /; wv\)/.test(ua) && ua.match(/Chrome\/(\d+)/) ? ["WebView", ua.match(/Chrome\/(\d+)/)[1]]
    : ua.match(/Edg\/(\d+)/) ? ["Edge", ua.match(/Edg\/(\d+)/)[1]]
    : ua.match(/(?:Firefox|FxiOS)\/(\d+)/) ? ["Firefox", ua.match(/(?:Firefox|FxiOS)\/(\d+)/)[1]]
    : ua.match(/SamsungBrowser\/(\d+)/) ? ["Samsung Internet", ua.match(/SamsungBrowser\/(\d+)/)[1]]
    : ua.match(/(?:Chrome|CriOS)\/(\d+)/) ? ["Chrome", ua.match(/(?:Chrome|CriOS)\/(\d+)/)[1]]
    : ua.match(/Version\/(\d+).*Safari/) ? ["Safari", ua.match(/Version\/(\d+)/)[1]] : ["Trình duyệt", ""];
  return `${os} – ${m[0]} ${m[1]}`.trim();
}

// ------------------------------------------------------------------ UI

function step(text) {
  const li = document.createElement("li");
  li.textContent = text;
  li.className = "running";
  $("#steps").append(li);
  return {
    done(ok, detail = "") {
      li.className = ok === true ? "ok" : ok === false ? "bad" : "na";
      li.textContent = `${text}: ${ok === true ? "đạt" : ok === false ? "không đạt" : "không áp dụng"}${detail ? ` (${detail})` : ""}`;
    },
  };
}

function check(key, ok, detail = "") {
  report.checks[key] = { ok, detail: String(detail).slice(0, 500) };
}

function ask(key, text, extra = null) {
  if (AUTO) { report.confirmations[key] = null; return Promise.resolve(null); }
  return new Promise((resolve) => {
    $("#question-text").textContent = text;
    const extraBtn = $("#extra");
    extraBtn.hidden = !extra;
    if (extra) { extraBtn.textContent = extra.label; extraBtn.onclick = extra.run; }
    $("#question").hidden = false;
    $("#question").scrollIntoView({ block: "nearest" });
    const finish = (value) => {
      $("#question").hidden = true;
      report.confirmations[key] = value;
      resolve(value);
    };
    $("#yes").onclick = () => finish(true);
    $("#no").onclick = () => finish(false);
    $("#skip").onclick = () => finish(null);
  });
}

function waitFor(predicate, ms, events = ["timeupdate", "seeked", "loadeddata", "playing", "error"]) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; events.forEach((e) => video.removeEventListener(e, test)); clearInterval(poll); clearTimeout(timer); resolve(v); };
    const test = () => { if (predicate()) finish(true); else if (video.error) finish(false); };
    events.forEach((e) => video.addEventListener(e, test));
    const poll = setInterval(test, 200);
    const timer = setTimeout(() => finish(predicate()), ms);
    test();
  });
}

function errorText() {
  const e = video.error;
  if (!e) return "";
  return ({ 1: "đã huỷ", 2: "lỗi mạng", 3: "không giải mã được", 4: "định dạng không hỗ trợ" })[e.code] || `lỗi ${e.code}`;
}

/** Was the sound track decoded? Each engine has its own (non-standard) way to tell. */
function hasSound() {
  if (typeof video.webkitAudioDecodedByteCount === "number") return video.webkitAudioDecodedByteCount > 0;  // Chrome, Edge, Safari
  if (typeof video.mozHasAudio === "boolean") return video.mozHasAudio;  // Firefox
  return null;
}

function resetVideo() {
  video.pause();
  [...video.querySelectorAll("track")].forEach((t) => t.remove());
  video.removeAttribute("src");
  video.load();
}

// ------------------------------------------------------------------ the test

async function run() {
  report.device = ($("#device").value.trim() || guessDevice()).slice(0, 80);
  try { localStorage.setItem("hub:diag-device", report.device); } catch { /* ignore */ }
  $("#start").disabled = true;
  $("#run").hidden = false;

  let list;
  const s0 = step("Tải danh sách video mẫu");
  try {
    const r = await fetch("/api/diagnostics/samples", { cache: "no-store" });
    if (!r.ok) throw new Error(`mã ${r.status}`);
    list = await r.json();
    s0.done(true, `${list.samples.length} định dạng`);
  } catch (e) {
    s0.done(false, String(e.message || e));
    check("samples", false, String(e.message || e));
    return finish();
  }
  const main = list.samples.find((x) => x.must_play);

  // Range requests (seeking depends on them; iOS refuses to play without them).
  const s1 = step("Server hỗ trợ tải từng đoạn (Range)");
  try {
    const r = await fetch(main.url, { headers: { Range: "bytes=0-99" }, cache: "no-store" });
    const body = await r.arrayBuffer();
    const ok = r.status === 206 && body.byteLength === 100 && /^bytes 0-99\//.test(r.headers.get("Content-Range") || "");
    check("range", ok, `HTTP ${r.status}, ${body.byteLength} byte`);
    s1.done(ok);
  } catch (e) { check("range", false, String(e)); s1.done(false); }

  const s2 = step("Lưu được vị trí đang xem (localStorage)");
  try { localStorage.setItem("hub:diag", "1"); localStorage.removeItem("hub:diag"); check("resume_storage", true); s2.done(true); }
  catch (e) { check("resume_storage", false, String(e)); s2.done(false, "không lưu được: không nhớ được vị trí xem"); }

  // Play, seek, subtitles on the MP4 H.264 sample (the plan's must-play file).
  resetVideo();
  const track = document.createElement("track");
  Object.assign(track, { kind: "subtitles", srclang: "vi", label: "Tiếng Việt", src: list.subtitles_url, default: true });
  video.append(track);
  video.textTracks[0].mode = "showing";
  video.src = main.url;
  const s3 = step(`Phát ${main.label}`);
  video.play().catch(() => {});
  const plays = await waitFor(() => video.currentTime > 1, 15000);
  check("play", plays, plays ? `${video.videoWidth}×${video.videoHeight}` : errorText() || "không chạy");
  s3.done(plays, plays ? "" : errorText());

  const s4 = step("Tua tới giây 30");
  let seekOk = false;
  if (plays) {
    video.currentTime = 30;
    seekOk = await waitFor(() => video.currentTime >= 30 && video.currentTime < 34 && !video.seeking, 15000)
      && await waitFor(() => video.currentTime > 30.6, 8000);
  }
  check("seek", plays ? seekOk : null, plays ? `dừng ở ${video.currentTime.toFixed(1)} s` : "chưa phát được");
  s4.done(plays ? seekOk : null);

  const s5 = step("Phụ đề hiện đúng thời điểm");
  let cueOk = null;
  if (seekOk) {
    cueOk = await waitFor(() => {
      const t = video.textTracks[0];
      return Boolean(t && t.activeCues && t.activeCues.length && /giây thứ 30/.test(t.activeCues[0].text));
    }, 6000);
  }
  check("subtitles", cueOk, cueOk ? "câu ở giây 30 đang hiển thị" : "");
  s5.done(cueOk);
  video.pause();
  if (seekOk) {
    await ask("subtitle_visible", "Trên video có dòng chữ “Phụ đề ở giây thứ 30: tua tới đây thành công” không?");
    await ask("fullscreen", "Bấm “Thử toàn màn hình”. Video có hiện toàn màn hình (kèm phụ đề) không? Thoát toàn màn hình rồi trả lời.",
              { label: "Thử toàn màn hình", run: enterFullscreen });
    await ask("sound", "Bấm “Bật tiếng”: bạn có nghe tiếng bíp đều không?",
              { label: "Bật tiếng", run: () => { video.muted = false; video.volume = 0.5; video.play().catch(() => {}); } });
    video.muted = true;
    video.pause();
  }
  const fsApi = Boolean(video.requestFullscreen || video.webkitEnterFullscreen || video.webkitRequestFullscreen);
  check("fullscreen_api", fsApi, fsApi ? "" : "trình duyệt không có API toàn màn hình");

  // Every format: what the browser says, and what really happens.
  for (const s of list.samples) {
    const st = step(`Định dạng ${s.label}`);
    resetVideo();
    const answer = video.canPlayType(s.mime);
    video.src = s.url;
    video.play().catch(() => {});
    const ok = await waitFor(() => video.currentTime > 0.3, 8000);
    await sleep(500);
    const picture = ok ? video.videoWidth > 0 : null;
    const sound = ok ? hasSound() : null;  // null: this browser cannot tell
    const detail = !ok ? (errorText() || "không chạy")
      : !picture ? "chỉ có tiếng, không có hình"
      : sound === false ? `${video.videoWidth}×${video.videoHeight}, không có tiếng`
      : `${video.videoWidth}×${video.videoHeight}`;
    report.formats.push({ key: s.key, can_play_type: answer, plays: ok, picture, sound, detail });
    st.done(ok && picture !== false && sound !== false, `${detail}; canPlayType: “${answer || "không"}”`);
  }
  resetVideo();
  for (const [key, text] of Object.entries(list.missing || {})) {
    report.formats.push({ key, can_play_type: "", plays: null, picture: null, detail: `không tạo được mẫu: ${text}`.slice(0, 300) });
  }
  return finish();
}

function enterFullscreen() {
  const ok = () => { report.checks.fullscreen_event = { ok: true, detail: "đã vào toàn màn hình" }; };
  document.addEventListener("fullscreenchange", () => { if (document.fullscreenElement) ok(); }, { once: true });
  video.addEventListener("webkitbeginfullscreen", ok, { once: true });
  if (video.requestFullscreen) video.requestFullscreen().catch((e) => { report.checks.fullscreen_event = { ok: false, detail: String(e).slice(0, 200) }; });
  else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
}

function finish() {
  const rows = $("#results");
  rows.replaceChildren();
  const label = { range: "Range (tua)", resume_storage: "Nhớ vị trí xem", play: "Phát MP4 H.264", seek: "Tua",
                  subtitles: "Phụ đề (đúng thời điểm)", fullscreen_api: "API toàn màn hình", samples: "Video mẫu" };
  const add = (name, ok, note) => {
    const tr = document.createElement("tr");
    for (const [i, text] of [name, ok === true ? "✔ đạt" : ok === false ? "✘ không" : "–", note || ""].entries()) {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 1) td.className = ok === true ? "ok" : ok === false ? "bad" : "";
      tr.append(td);
    }
    rows.append(tr);
  };
  for (const [k, v] of Object.entries(report.checks)) add(label[k] || k, v.ok, v.detail);
  for (const [k, v] of Object.entries(report.confirmations)) {
    add({ subtitle_visible: "Bạn thấy phụ đề", fullscreen: "Toàn màn hình", sound: "Có tiếng" }[k] || k, v, "người kiểm tra trả lời");
  }
  for (const f of report.formats) add(f.key, f.plays === null ? null : f.plays && f.picture !== false, f.detail);
  $("#summary").hidden = false;
  if (AUTO) send();
  else $("#summary").scrollIntoView({ block: "start" });
}

async function send() {
  report.notes = ($("#notes").value || "").slice(0, 1000);
  $("#send").disabled = true;
  try {
    const r = await fetch("/api/diagnostics/reports", { method: "POST", headers: { "Content-Type": "application/json" },
                                                        body: JSON.stringify(report) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `mã ${r.status}`);
    $("#sent").textContent = `Đã gửi. Cảm ơn bạn! (${body.saved})`;
    document.body.dataset.sent = body.saved;
  } catch (e) {
    $("#sent").textContent = `Không gửi được: ${e.message || e}. Thử lại.`;
    $("#send").disabled = false;
  }
}

// ------------------------------------------------------------------ remote-key test (plan step 5.4)
// Asks for one remote button at a time and records what the browser really receives: a key
// event (key/code/keyCode), only a keyup, a step back in the history (Back handled by the
// browser itself), or a click (TV browsers in cursor mode). Nothing is guessed from the brand.
// ?keywait=<seconds>: time per button (default 12).

const REMOTE_BUTTONS = [
  ["up", "▲ Lên"], ["down", "▼ Xuống"], ["left", "◀ Trái"], ["right", "▶ Phải"], ["ok", "OK (nút giữa)"],
  ["back", "Back (quay lại)"], ["play", "▶ Play"], ["pause", "❚❚ Pause"], ["stop", "■ Stop"],
  ["rewind", "◀◀ Tua lùi"], ["fast_forward", "▶▶ Tua nhanh"],
];
const REMOTE_HINTS = {
  ok: "Nếu trên màn hình có con trỏ, bấm OK ở chỗ trống (không vào nút trên trang).",
  back: "Trang đã chuẩn bị sẵn: bấm Back sẽ không rời khỏi trang này.",
  play: "Remote chỉ có một nút Play/Pause thì bấm nút đó.",
  pause: "Remote chỉ có một nút Play/Pause thì bấm nút đó lần nữa.",
};
const STEP_SECONDS = Math.min(60, Math.max(1, Number(params.get("keywait")) || 12));
const GUARD_MS = 400;   // after a button is taken: ignore stray events (key repeat, the start click)
const LOG_MAX = 60;
const remote = { running: false, index: 0, results: [], log: [], started: 0, timer: null, deadline: 0,
                 guardUntil: 0, down: new Set(), cleaning: false };

const nowMs = () => performance.now();

function remoteStart() {
  Object.assign(remote, { running: true, index: 0, results: [], log: [], started: nowMs(),
                          guardUntil: nowMs() + GUARD_MS, down: new Set() });
  $("#remote-log").replaceChildren();
  $("#remote-last").textContent = "";
  $("#remote-sent").textContent = "";
  $("#remote-send").disabled = false;
  $("#remote-summary").hidden = true;
  $("#remote-run").hidden = false;
  $("#remote-start").disabled = true;
  delete document.body.dataset.remoteSent;
  // An extra history entry: Back pressed during the test lands here (popstate) instead of leaving.
  try { history.pushState({ hubRemoteTest: true }, "", location.href); } catch { /* ignore */ }
  $("#remote").scrollIntoView({ block: "start" });
  askButton();
}

function askButton() {
  clearInterval(remote.timer);
  if (remote.index >= REMOTE_BUTTONS.length) { remoteFinish(); return; }
  const [id, label] = REMOTE_BUTTONS[remote.index];
  $("#remote-step").textContent = `Nút ${remote.index + 1}/${REMOTE_BUTTONS.length}${REMOTE_HINTS[id] ? ` · ${REMOTE_HINTS[id]}` : ""}`;
  $("#remote-prompt").textContent = `Bấm nút: ${label}`;
  remote.deadline = nowMs() + STEP_SECONDS * 1000;
  const tick = () => {
    const left = Math.ceil((remote.deadline - nowMs()) / 1000);
    if (left <= 0) { record({ received: false }); return; }
    $("#remote-countdown").textContent = `Còn ${left} giây. Không bấm gì thì ghi là "không nhận được".`;
  };
  tick();
  remote.timer = setInterval(tick, 250);
}

function record(result) {
  if (!remote.running) return;
  const [button, label] = REMOTE_BUTTONS[remote.index];
  const entry = { button, received: result.received, via: result.via || null,
                  key: String(result.key || "").slice(0, 40), code: String(result.code || "").slice(0, 40),
                  key_code: Math.max(0, Math.min(100000, Number(result.key_code) || 0)) };
  remote.results.push(entry);
  $("#remote-last").textContent = `${label}: ${resultText(entry)}${entry.received ? ` (${viaText(entry)})` : ""}`;
  remote.guardUntil = nowMs() + GUARD_MS;
  remote.index += 1;
  askButton();
}

function accept(via, e = {}) {
  if (!remote.running || nowMs() < remote.guardUntil) return;
  record({ received: true, via, key: e.key, code: e.code, key_code: e.keyCode });
}

function logEvent(type, e = {}) {
  const entry = { type, key: String(e.key || "").slice(0, 40), code: String(e.code || "").slice(0, 40),
                  key_code: Math.max(0, Math.min(100000, Number(e.keyCode) || 0)),
                  ms: Math.max(0, Math.min(3_600_000, Math.round(nowMs() - remote.started))) };
  remote.log.push(entry);
  if (remote.log.length > LOG_MAX) remote.log.shift();
  const li = document.createElement("li");
  li.textContent = `+${(entry.ms / 1000).toFixed(2)}s ${type}`
    + (type === "keydown" || type === "keyup" ? ` key="${entry.key}" code="${entry.code}" keyCode=${entry.key_code}` : "");
  const list = $("#remote-log");
  list.append(li);
  while (list.children.length > LOG_MAX) list.firstElementChild.remove();
}

function onRemoteKey(e) {
  if (!remote.running) return;
  logEvent(e.type, e);
  e.preventDefault();  // no scrolling, no button activation, no browser shortcut while testing
  e.stopPropagation();
  const id = e.code || e.key || String(e.keyCode);
  if (e.type === "keydown") {
    if (e.repeat) return;
    remote.down.add(id);
    accept("keydown", e);
  } else if (!remote.down.delete(id)) {
    accept("keyup", e);  // a keyup with no keydown before it: some TV keys only send this
  }
}

function onRemotePopState() {
  if (remote.cleaning) { remote.cleaning = false; return; }
  if (!remote.running) return;
  logEvent("popstate");
  try { history.pushState({ hubRemoteTest: true }, "", location.href); } catch { /* ignore */ }  // re-arm
  accept("popstate");
}

function onRemoteClick(e) {
  if (!remote.running || (e.target instanceof Element && e.target.closest("#remote-controls"))) return;
  logEvent("click");
  accept("click");
}

function remoteFinish(stopped = false) {
  if (!remote.running) return;
  clearInterval(remote.timer);
  if (stopped) {
    for (const [button] of REMOTE_BUTTONS.slice(remote.results.length)) {
      remote.results.push({ button, received: null, via: null, key: "", code: "", key_code: 0 });
    }
  }
  remote.running = false;
  if (history.state && history.state.hubRemoteTest) { remote.cleaning = true; history.back(); }
  $("#remote-run").hidden = true;
  $("#remote-start").disabled = false;
  const rows = $("#remote-results");
  rows.replaceChildren();
  const labels = Object.fromEntries(REMOTE_BUTTONS);
  for (const r of remote.results) {
    const tr = document.createElement("tr");
    for (const [i, text] of [labels[r.button], resultText(r), r.received ? viaText(r) : ""].entries()) {
      const td = document.createElement("td");
      td.textContent = text;
      if (i === 1) td.className = r.received === true ? "ok" : r.received === false ? "bad" : "";
      tr.append(td);
    }
    rows.append(tr);
  }
  $("#remote-summary").hidden = false;
  $("#remote-summary").scrollIntoView({ block: "start" });
}

function resultText(r) {
  return r.received === true ? "✔ nhận được" : r.received === false ? "✘ không nhận được" : "– bỏ qua";
}

function viaText(r) {
  if (r.via === "popstate") return "trình duyệt tự quay lại trang trước (lịch sử)";
  if (r.via === "click") return "một cú click (chế độ con trỏ)";
  return `key "${r.key}" · code "${r.code}" · keyCode ${r.key_code}${r.via === "keyup" ? " · chỉ có keyup" : ""}`;
}

async function remoteSend() {
  const payload = {
    device: ($("#device").value.trim() || guessDevice()).slice(0, 80), user_agent: report.user_agent, auto: false,
    screen: report.screen, touch: report.touch, checks: {}, formats: [], confirmations: {},
    keys: remote.results, key_log: remote.log, notes: ($("#remote-notes").value || "").slice(0, 1000),
  };
  try { localStorage.setItem("hub:diag-device", payload.device); } catch { /* ignore */ }
  $("#remote-send").disabled = true;
  try {
    const r = await fetch("/api/diagnostics/reports", { method: "POST", headers: { "Content-Type": "application/json" },
                                                        body: JSON.stringify(payload) });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.detail || `mã ${r.status}`);
    $("#remote-sent").textContent = `Đã gửi. Cảm ơn bạn! (${body.saved})`;
    document.body.dataset.remoteSent = body.saved;
  } catch (e) {
    $("#remote-sent").textContent = `Không gửi được: ${e.message || e}. Thử lại.`;
    $("#remote-send").disabled = false;
  }
}

function setupRemoteTest() {
  document.addEventListener("keydown", onRemoteKey, true);
  document.addEventListener("keyup", onRemoteKey, true);
  document.addEventListener("click", onRemoteClick, true);
  window.addEventListener("popstate", onRemotePopState);
  $("#remote-start").addEventListener("click", remoteStart);
  $("#remote-again").addEventListener("click", remoteStart);
  $("#remote-skip").addEventListener("click", () => record({ received: null }));
  $("#remote-stop").addEventListener("click", () => remoteFinish(true));
  $("#remote-send").addEventListener("click", remoteSend);
}

function init() {
  let saved = null;
  try { saved = localStorage.getItem("hub:diag-device"); } catch { /* ignore */ }
  $("#device").value = params.get("device") || saved || guessDevice();
  $("#start").addEventListener("click", run);
  $("#send").addEventListener("click", send);
  setupRemoteTest();
  if (AUTO) run();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
