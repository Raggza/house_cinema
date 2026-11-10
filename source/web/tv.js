// House Cinema — the TV screen /tv (plan step 5.4).
// The TV browsers tried (BrowseHere, TV Bro) drive a mouse cursor with the remote and never
// pass the keys to the page, so the TV only shows: a phone (/remote) is the remote.
//  1. Register as a screen (POST /api/screens), keeping id + key in this browser: a reload,
//     or a restart of the hub, gives the same screen back, so phones need not pick it again.
//  2. Show the QR code and the short code; wait for commands on an EventSource.
//  3. "Bắt đầu", pressed once with the cursor, makes the page fullscreen and lets it play with
//     sound (browsers allow both only after a click). Films then change inside this page,
//     which stays fullscreen.
//  4. A film plays in the player page of steps 4.3/5.1 (/watch/<id>?screen=1) in an iframe:
//     conversion and subtitles work as everywhere. Its state comes back by postMessage and is
//     sent to the hub (PUT /state), where phones read it.
// Text from the server is always inserted with textContent.
//  5. A playlist (step 6.5, command play_list) is kept here: when a video ends, the next one
//     starts after a short "Tiếp theo" countdown; a video that cannot play is skipped (at most
//     MAX_SKIPS in a row); "Hub đang bận" stops there. The phone sees where the TV is in it.

import * as PL from "/static/playlist.js";

const $ = (sel) => document.querySelector(sel);
const STORE = "hub:tv-screen";
const RETRY_MS = 3000;
const SEND_EVERY_MS = 500;
const UPNEXT_S = 5;
const MAX_SKIPS = 3;

const storage = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
};

const frame = $("#frame");
let screen = null;      // {id, key, code, name, remote_url, qr_svg}
let events = null;
let started = false;
let current = null;     // media id playing, or null (idle)
let playerState = null; // last state from the player iframe
let lastTitle = null;
let sending = false;
let sendAgain = false;
let lastSent = 0;
let sendTimer = null;
let registerTimer = null;
let list = null;          // the playlist playing (PL.makeList), or null
let upNextTimer = null;
let upNextLeft = null;    // seconds before the next video, while counting down
let upNextPos = null;     // …and which one
let skipping = false;     // the current video's "cannot play" was already counted

function guessName() {
  const asked = new URLSearchParams(location.search).get("name");
  if (asked && asked.trim()) return asked.trim().slice(0, 60);
  const ua = navigator.userAgent;
  return /BRAVIA/i.test(ua) ? "Sony TV" : /Tizen/.test(ua) ? "Samsung TV" : /Web0S|webOS/.test(ua) ? "LG TV"
    : /Android.*\b(TV|AFT\w*)\b|GoogleTV|Android TV/i.test(ua) ? "Android TV" : "TV";
}

function status(text, bad = false) {
  const s = $("#status");
  s.textContent = text;
  s.className = "tv-status" + (bad ? " bad" : "");
  badge();
}

/** The small box over a film: lost connection, or sound blocked. */
function badge() {
  const b = $("#badge");
  let text = "";
  if (current && $("#status").classList.contains("bad")) text = $("#status").textContent;
  else if (current && upNextLeft != null && list) {
    const next = PL.itemAt(list, upNextPos);
    text = `Tiếp theo: ${next ? next.title : ""} (${upNextLeft} giây)`;
  } else if (current && playerState && playerState.sound_blocked) text = "Bấm vào màn hình TV một lần để bật tiếng.";
  b.textContent = text;
  b.hidden = !text;
}

// ------------------------------------------------------------------ hub connection

async function register() {
  clearTimeout(registerTimer);
  const saved = storage.get(STORE) || {};
  const body = { name: guessName() };
  if (saved.id && saved.key) Object.assign(body, { id: saved.id, key: saved.key });
  let r;
  try {
    r = await fetch("/api/screens", { method: "POST", cache: "no-store", body: JSON.stringify(body),
                                     headers: { "Content-Type": "application/json", Accept: "application/json" } });
  } catch {
    status("Không kết nối được tới hub. Đang thử lại…", true);
    registerTimer = setTimeout(register, RETRY_MS);
    return;
  }
  if (r.status === 401) { location.href = `/pin?next=${encodeURIComponent("/tv" + location.search)}`; return; }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    status(`${data.detail || `Hub trả về lỗi ${r.status}.`} Đang thử lại…`, true);
    registerTimer = setTimeout(register, RETRY_MS * 3);
    return;
  }
  screen = data;
  storage.set(STORE, { id: screen.id, key: screen.key });
  $("#code").textContent = screen.code;
  $("#url").textContent = screen.remote_url;
  $("#qr").src = screen.qr_svg;
  $("#qr").hidden = false;
  $("#headline").textContent = `Màn hình TV: ${screen.name}`;
  document.body.dataset.code = screen.code;
  connect();
}

function connect() {
  if (events) events.close();
  const es = new EventSource(`/api/screens/${encodeURIComponent(screen.id)}/events?key=${encodeURIComponent(screen.key)}`);
  events = es;
  es.addEventListener("hello", () => {
    status(current ? "Đã kết nối." : "Đã kết nối. Chọn phim trên điện thoại rồi bấm “Phát trên TV”.");
    document.body.dataset.connected = "1";
    sendState(true);
  });
  es.addEventListener("command", (e) => {
    let command;
    try { command = JSON.parse(e.data); } catch { return; }
    handle(command);
  });
  es.onerror = () => {
    if (events !== es) return;
    delete document.body.dataset.connected;
    status("Mất kết nối tới hub. Đang kết nối lại…", true);
    if (es.readyState === EventSource.CLOSED) {  // refused (hub restarted, PIN expired): register again
      events = null;
      registerTimer = setTimeout(register, RETRY_MS);
    }
  };
}

// ------------------------------------------------------------------ state for the phones

function idleState() {
  return { status: "idle", position: 0, volume: 1, muted: false, subtitles: [], subtitle: "off", started };
}

function stateNow() {
  if (!current) return idleState();
  const s = playerState && playerState.media_id === current ? playerState
    : { status: "loading", media_id: current, position: 0, volume: 1, muted: false, subtitles: [], subtitle: "off" };
  return { ...s, started, playlist: playlistState() };
}

function playlistState() {
  if (!list) return null;
  const next = PL.itemAt(list, PL.nextPos(list));
  return { title: list.title || null, position: list.pos + 1, length: PL.size(list), shuffle: list.shuffle,
           repeat: list.repeat, next_title: next ? next.title.slice(0, 300) : null, up_next_in: upNextLeft };
}

// ------------------------------------------------------------------ playlist (step 6.5)

function playAt(pos, at = 0) {
  cancelUpNext();
  list.pos = pos;
  skipping = false;
  play(PL.current(list).id, at, { keepList: true });
}

function upNext(pos) {
  cancelUpNext();
  upNextPos = pos;
  upNextLeft = UPNEXT_S;
  badge();
  sendState(true);
  upNextTimer = setInterval(() => {
    upNextLeft -= 1;
    if (upNextLeft <= 0) { playAt(upNextPos); return; }
    badge();
    sendState();
  }, 1000);
}

function cancelUpNext() {
  clearInterval(upNextTimer);
  upNextTimer = null;
  upNextLeft = null;
  upNextPos = null;
  badge();
}

/** The player says the video ended, or cannot be played. */
function playlistEvent(state) {
  if (!list) return false;
  if (state.status === "ended" && upNextLeft == null) {
    if (list.repeat === "one") { toPlayer({ action: "seek", to: 0 }); toPlayer({ action: "play" }); return true; }
    const next = PL.nextPos(list, { auto: true });
    if (next == null) {
      const n = PL.size(list);
      list = null;
      stop(`Đã phát hết danh sách (${n} video).`);
      return true;
    }
    upNext(next);
    return true;
  }
  if (state.problem === "failed" && !skipping && upNextLeft == null) {
    skipping = true;  // counted once for this video
    list.fails += 1;
    const next = PL.nextPos(list);
    if (list.fails >= MAX_SKIPS || next == null) {
      status(list.fails >= MAX_SKIPS ? `${MAX_SKIPS} video liên tiếp không phát được nên danh sách phát đã dừng.`
                                     : "Video cuối của danh sách không phát được.", true);
      return false;
    }
    upNext(next);
    return false;
  }
  if (state.status === "playing") list.fails = 0;
  return false;
}

/** PUT the state, at most every SEND_EVERY_MS, always the newest one. */
function sendState(now = false) {
  if (!screen) return;
  if (sending) { sendAgain = true; return; }
  const wait = now ? 0 : Math.max(0, SEND_EVERY_MS - (Date.now() - lastSent));
  if (wait > 0) {
    if (!sendTimer) sendTimer = setTimeout(() => { sendTimer = null; sendState(true); }, wait);
    return;
  }
  sending = true;
  lastSent = Date.now();
  fetch(`/api/screens/${encodeURIComponent(screen.id)}/state?key=${encodeURIComponent(screen.key)}`, {
    method: "PUT", cache: "no-store", body: JSON.stringify(stateNow()),
    headers: { "Content-Type": "application/json" },
  }).then((r) => {
    if (r.status === 404 || r.status === 403) register();  // the hub forgot this screen
  }).catch(() => { /* the event stream reports a lost connection */ }).finally(() => {
    sending = false;
    if (sendAgain) { sendAgain = false; sendState(); }
  });
}

// ------------------------------------------------------------------ commands

function toPlayer(command) {
  if (!current || !frame.contentWindow) return;
  frame.contentWindow.postMessage({ type: "hub-command", command }, location.origin);
}

function play(mediaId, at, { keepList = false } = {}) {
  if (!keepList) { list = null; cancelUpNext(); }
  current = mediaId;
  playerState = null;
  const t = Number(at) > 0 ? `&t=${Math.floor(Number(at))}` : "";
  frame.src = `/watch/${encodeURIComponent(mediaId)}?screen=1${t}`;
  frame.hidden = false;
  $("#idle").hidden = true;
  $("#start-box").hidden = true;
  status("Đã kết nối.");
  sendState(true);
}

function stop(text = null) {
  cancelUpNext();
  current = null;
  playerState = null;
  frame.src = "about:blank";  // the player page closes: its conversion stops (step 5.1)
  frame.hidden = true;
  $("#idle").hidden = false;
  $("#start-box").hidden = started && Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  $("#headline").textContent = text || `Màn hình TV: ${screen ? screen.name : ""}`;
  badge();
  sendState(true);
}

function handle(command) {
  switch (command.action) {
    case "play_media": play(command.media_id, command.at); break;
    case "play_list": {
      const items = (command.items || []).map((it) => ({ id: it.media_id, title: it.title }));
      if (!items.length) break;
      list = PL.makeList(items, { index: command.index, title: command.title, shuffle: command.shuffle,
                                  repeat: command.repeat });
      playAt(list.pos, command.at);
      break;
    }
    case "next":
    case "previous": {
      if (!list) break;
      const pos = command.action === "next" ? (upNextPos ?? PL.nextPos(list)) : PL.prevPos(list);
      if (pos != null) playAt(pos);
      break;
    }
    case "playlist_mode":
      if (!list) break;
      if (typeof command.shuffle === "boolean" && command.shuffle !== list.shuffle) PL.setShuffle(list, command.shuffle);
      if (command.repeat) PL.setRepeat(list, command.repeat);
      sendState(true);
      break;
    case "stop": list = null; stop(); break;
    default: toPlayer(command);
  }
}

window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || e.source !== frame.contentWindow || !current) return;
  if (!e.data || e.data.type !== "hub-state" || !e.data.state) return;
  playerState = e.data.state;
  if (playerState.title) lastTitle = playerState.title;
  badge();
  if (playlistEvent(playerState)) return;
  if (playerState.status === "ended" && !list) { stop(lastTitle ? `Đã xem xong: ${lastTitle}` : null); return; }
  sendState();
});

// ------------------------------------------------------------------ "Bắt đầu"

function goFullscreen() {
  const el = document.documentElement;
  const ask = el.requestFullscreen || el.webkitRequestFullscreen;
  if (!ask) return;
  try {
    const p = ask.call(el);
    if (p && p.catch) p.catch(() => { /* the browser said no: the page still fills the window */ });
  } catch { /* ignore */ }
}

function start() {
  started = true;
  document.body.dataset.started = "1";
  goFullscreen();
  $("#start").textContent = "Toàn màn hình";
  $("#start-box").hidden = true;
  sendState(true);
}

function onFullscreenChange() {
  const full = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  if (!current) $("#start-box").hidden = full && started;
}

function init() {
  $("#start").addEventListener("click", start);
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
  setInterval(() => sendState(), 10000);  // keeps the phones' view fresh even when nothing changes
  register();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
