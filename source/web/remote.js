// House Cinema — the phone remote /remote (plan step 5.4).
// Pick a TV (QR code → /remote?screen=<id>, the list of TVs showing /tv, or the 4-letter code),
// then send it commands and show what it plays. /remote?play=<media id>&t=<s> (from the
// "Phát trên TV" button of the player page) starts that film on the TV as soon as one is chosen.
// The chosen TV is remembered in this browser. Text from the server is inserted with textContent.
// /remote?list=<id>&t=<s> (step 6.5: "Phát trên TV" with a playlist, from the library's
// selection or the player) sends the whole playlist (kept in sessionStorage by playlist.js).

import * as PL from "/static/playlist.js";

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child != null) node.append(child);
  return node;
};
const STORE = "hub:remote-screen";
const POLL_MS = 1000;
const LIST_MS = 3000;
const STATUS_TEXT = { idle: "Đang chờ phim", loading: "Đang tải…", playing: "Đang phát", paused: "Đã dừng",
                      ended: "Đã xem xong" };
const PLAY_ICON = "M8 5v14l11-7z";
const PAUSE_ICON = "M7 5h3v14H7zM14 5h3v14h-3z";

const storage = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ } },
  remove(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

let screenId = null;
let info = null;
let pendingPlay = null;   // {media_id, at}: to start once a TV is chosen and connected
let pollTimer = null;
let listTimer = null;
let dragging = false;
let commandError = null;  // last command problem, shown until the next success
let volumeTimer = null;

function formatTime(seconds) {
  if (seconds == null || !isFinite(seconds)) return "0:00";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m)) + `:${String(r).padStart(2, "0")}`;
}

function toPin() {
  location.href = `/pin?next=${encodeURIComponent(location.pathname + location.search)}`;
}

function message(lines, kind = "error") {
  const box = $("#message");
  const list = (Array.isArray(lines) ? lines : [lines]).filter(Boolean);
  box.replaceChildren(...list.flatMap((t, i) => (i ? [el("br"), t] : [t])));
  box.className = `remote-message ${kind}`;
  box.hidden = !list.length;
}

async function getJson(url) {
  let r;
  try { r = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } }); }
  catch { return { offline: true }; }
  if (r.status === 401) { toPin(); return { status: 401 }; }
  const data = await r.json().catch(() => ({}));
  return { status: r.status, ok: r.ok, data };
}

// ------------------------------------------------------------------ choosing a TV

function showPicker(text = null) {
  clearTimeout(pollTimer);
  screenId = null;
  info = null;
  $("#remote").hidden = true;
  $("#picker").hidden = false;
  $("#tv-url").textContent = `${location.origin}/tv`;
  message(text ? [text] : [], "info");
  loadList();
}

async function loadList() {
  clearTimeout(listTimer);
  if ($("#picker").hidden) return;
  const r = await getJson("/api/screens");
  if (r.offline) message("Không kết nối được tới hub. Kiểm tra Wi-Fi.");
  else if (r.ok) renderList(r.data.screens);
  listTimer = setTimeout(loadList, LIST_MS);
}

function renderList(screens) {
  const list = $("#screens");
  list.replaceChildren(...screens.map((s) => {
    const b = el("button", { className: "btn", type: "button" },
                 el("span", { textContent: `${s.name} · ${s.code}` }),
                 el("span", { className: s.online ? "" : "off", textContent: s.online ? "Đang mở" : "Mất kết nối" }));
    b.dataset.code = s.code;
    b.addEventListener("click", () => choose(s.id));
    return el("li", {}, b);
  }));
  $("#no-screens").hidden = screens.length > 0;
}

async function chooseByCode(e) {
  e.preventDefault();
  const code = $("#code").value.trim().toUpperCase();
  if (!code) return;
  const r = await getJson("/api/screens");
  if (!r.ok) { message("Không lấy được danh sách TV từ hub."); return; }
  const found = r.data.screens.find((s) => s.code === code);
  if (!found) { message(`Không có TV nào có mã ${code}. Xem lại mã trên màn hình TV.`); return; }
  choose(found.id);
}

function choose(id) {
  clearTimeout(listTimer);
  screenId = id;
  storage.set(STORE, id);
  $("#picker").hidden = true;
  $("#remote").hidden = false;
  message([]);
  poll();
}

// ------------------------------------------------------------------ state of the TV

async function poll() {
  clearTimeout(pollTimer);
  if (!screenId) return;
  const r = await getJson(`/api/screens/${encodeURIComponent(screenId)}`);
  if (r.status === 404) {
    storage.remove(STORE);
    showPicker("TV đã chọn không còn trên hub (TV đã đóng trang /tv lâu, hoặc hub đã khởi động lại). Chọn lại TV.");
    return;
  }
  if (r.offline) {
    message("Không kết nối được tới hub. Kiểm tra Wi-Fi.");
  } else if (r.ok) {
    info = r.data;
    if (pendingPlay && info.online) {
      const p = pendingPlay;
      pendingPlay = null;
      if (p.list) {
        const l = p.list;
        await send({ action: "play_list", media_ids: l.items.map((it) => it.id), index: PL.currentIndex(l),
                     at: p.at, shuffle: l.shuffle, repeat: l.repeat, title: l.title || "" });
      } else {
        await send({ action: "play_media", media_id: p.media_id, at: p.at });
      }
    }
    render();
  }
  if (!document.hidden) pollTimer = setTimeout(poll, POLL_MS);
}

function render() {
  const s = info.state;
  $("#screen-name").textContent = `${info.name} · ${info.code}`;
  document.body.dataset.status = info.online ? s.status : "offline";
  const idle = s.status === "idle" || !s.media_id;
  $("#now-title").textContent = idle ? "Chưa phát phim nào" : (s.title || "Đang tải…");
  $("#now-status").textContent = info.online ? STATUS_TEXT[s.status] || "" : "TV mất kết nối";
  const thumb = $("#thumb");
  if (!idle) {
    const src = `/thumbnails/${encodeURIComponent(s.media_id)}.jpg`;
    if (thumb.getAttribute("src") !== src) thumb.src = src;
  }
  thumb.hidden = idle;

  const d = s.duration || 0;
  const seek = $("#seek");
  seek.max = String(Math.max(0, Math.floor(d)));
  if (!dragging) {
    seek.value = String(Math.floor(s.position || 0));
    $("#pos").textContent = formatTime(s.position);
  }
  $("#dur").textContent = formatTime(d);

  const playing = s.status === "playing" || s.status === "loading";
  $("#pp-icon").setAttribute("d", playing && !idle ? PAUSE_ICON : PLAY_ICON);
  $("#pp-label").textContent = playing && !idle ? "Dừng" : "Phát";
  $("#mute").textContent = s.muted ? "Bật tiếng" : "Tắt tiếng";
  if (!volumeTimer) $("#volume").value = String(Math.round((s.volume ?? 1) * 100));

  const select = $("#subtitles");
  const wanted = ["off", ...s.subtitles.map((t) => t.key)].join("\n");
  if (select.dataset.keys !== wanted) {
    select.replaceChildren(el("option", { value: "off", textContent: "Tắt" }),
                           ...s.subtitles.map((t) => el("option", { value: t.key, textContent: t.label })));
    select.dataset.keys = wanted;
  }
  select.value = s.subtitle || "off";
  $("#sub-box").hidden = idle || !s.subtitles.length;
  for (const id of ["back10", "playpause", "fwd10", "seek", "stop", "mute", "volume"]) $(`#${id}`).disabled = idle || !info.online;

  const lines = [];
  let kind = "info";
  if (commandError) { lines.push(commandError); kind = "error"; }
  if (!info.online) {
    lines.push("TV đang mất kết nối với hub. Kiểm tra trang /tv trên TV còn mở và TV còn mạng.");
    kind = "error";
  } else {
    if (s.notice) {
      lines.push(s.notice.title, ...s.notice.lines);
      if (s.notice.kind === "error") kind = "error";
    }
    if (s.sound_blocked) lines.push("TV đang phát không tiếng vì trình duyệt trên TV chặn. Bấm vào màn hình TV một lần (bằng con trỏ) để bật tiếng.");
    if (!s.started) lines.push("Trên TV, bấm nút “Bắt đầu” một lần để vào toàn màn hình và cho phép phát có tiếng.");
  }
  if (pendingPlay && !info.online) lines.push("Phim sẽ được phát khi TV kết nối lại.");
  message(lines, kind);
  renderPlaylist(s.playlist, idle);
}

/** Where the TV is in its playlist (step 6.5), with Trước / Sau / Ngẫu nhiên / Lặp lại. */
function renderPlaylist(pl, idle) {
  const box = $("#pl-box");
  box.hidden = !pl || idle;
  if (box.hidden) return;
  $("#pl-title").textContent = pl.title ? `Danh sách phát · ${pl.title}` : "Danh sách phát";
  $("#pl-pos").textContent = `${pl.position}/${pl.length}`;
  $("#pl-next-title").textContent = pl.next_title
    ? (pl.up_next_in != null ? `Tiếp theo trong ${pl.up_next_in} giây: ${pl.next_title}` : `Tiếp theo: ${pl.next_title}`)
    : "Đây là video cuối của danh sách.";
  const atStart = pl.position <= 1 && pl.repeat === "off";
  $("#pl-prev").disabled = atStart || !info.online;
  $("#pl-next").disabled = !pl.next_title || !info.online;
  $("#pl-shuffle").setAttribute("aria-pressed", String(pl.shuffle));
  $("#pl-shuffle").disabled = !info.online;
  const repeat = $("#pl-repeat");
  repeat.setAttribute("aria-pressed", String(pl.repeat !== "off"));
  repeat.dataset.mode = pl.repeat;
  repeat.querySelector(".label").textContent = PL.REPEAT_LABEL[pl.repeat] || PL.REPEAT_LABEL.off;
  repeat.disabled = !info.online;
}

// ------------------------------------------------------------------ commands

async function send(command) {
  if (!screenId) return false;
  let r;
  try {
    r = await fetch(`/api/screens/${encodeURIComponent(screenId)}/commands`, {
      method: "POST", cache: "no-store", body: JSON.stringify(command),
      headers: { "Content-Type": "application/json", Accept: "application/json" } });
  } catch {
    commandError = "Không gửi được lệnh: mất kết nối tới hub.";
    if (info) render();
    return false;
  }
  if (r.status === 401) { toPin(); return false; }
  const data = await r.json().catch(() => ({}));
  if (r.ok) {
    commandError = null;
    setTimeout(poll, 400);  // show the TV's answer soon
    return true;
  }
  if (r.status === 404 && command.action === "play_media") commandError = "Không tìm thấy video này trong thư viện.";
  else if (r.status === 404 && command.action === "play_list") {
    commandError = "Không video nào của danh sách còn phát được (đã bị xoá, hoặc ổ đang ngắt).";
  }
  else if (r.status === 404) { storage.remove(STORE); showPicker("TV đã chọn không còn trên hub. Chọn lại TV."); return false; }
  else if (r.status === 422) commandError = "Lệnh không hợp lệ.";
  else commandError = data.detail || `Hub trả về lỗi ${r.status}.`;
  if (info) render();
  return false;
}

function setupControls() {
  $("#playpause").addEventListener("click", () => {
    const s = info && info.state;
    send({ action: s && (s.status === "playing" || s.status === "loading") ? "pause" : "play" });
  });
  $("#back10").addEventListener("click", () => send({ action: "seek_by", by: -10 }));
  $("#fwd10").addEventListener("click", () => send({ action: "seek_by", by: 10 }));
  const seek = $("#seek");
  seek.addEventListener("input", () => { dragging = true; $("#pos").textContent = formatTime(Number(seek.value)); });
  seek.addEventListener("change", () => { dragging = false; send({ action: "seek", to: Number(seek.value) }); });
  $("#subtitles").addEventListener("change", (e) => send({ action: "subtitle", key: e.target.value }));
  $("#volume").addEventListener("input", (e) => {
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => {
      volumeTimer = null;
      send({ action: "volume", level: Number(e.target.value) / 100 });
    }, 200);
  });
  $("#mute").addEventListener("click", () => send({ action: "mute", on: !(info && info.state.muted) }));
  $("#stop").addEventListener("click", () => send({ action: "stop" }));
  $("#pl-prev").addEventListener("click", () => send({ action: "previous" }));
  $("#pl-next").addEventListener("click", () => send({ action: "next" }));
  $("#pl-shuffle").addEventListener("click", () => {
    const pl = info && info.state.playlist;
    if (pl) send({ action: "playlist_mode", shuffle: !pl.shuffle });
  });
  $("#pl-repeat").addEventListener("click", () => {
    const pl = info && info.state.playlist;
    if (!pl) return;
    const modes = PL.REPEAT_MODES;
    send({ action: "playlist_mode", repeat: modes[(modes.indexOf(pl.repeat) + 1) % modes.length] });
  });
  $("#change").addEventListener("click", () => showPicker());
  $("#code-form").addEventListener("submit", chooseByCode);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && screenId) poll(); });
}

function init() {
  setupControls();
  const params = new URLSearchParams(location.search);
  const fromQr = params.get("screen");
  const play = params.get("play");
  const listId = params.get("list");
  const at = Math.max(0, Number(params.get("t")) || 0);
  if (play) pendingPlay = { media_id: play.slice(0, 200), at };
  if (listId) {
    const l = PL.loadList(listId);
    if (l) pendingPlay = { list: l, at };
    else message("Không tìm thấy danh sách phát (có thể đã mở ở tab khác). Chọn lại video trong thư viện.");
  }
  if (fromQr || play || listId) history.replaceState(null, "", location.pathname);  // a reload must not play again
  const chosen = fromQr || storage.get(STORE);
  if (chosen && /^[A-Za-z0-9_-]{16,64}$/.test(chosen)) choose(chosen);
  else showPicker(pendingPlay ? "Chọn TV để phát phim." : null);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
