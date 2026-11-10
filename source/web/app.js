// House Cinema — library page (plan step 4.1).
// Plain ES module, no dependencies. All text from the server is inserted with textContent
// (file names are user data), never as HTML.

import * as PL from "/static/playlist.js";

const PAGE_SIZE = 60;
const SEARCH_DELAY_MS = 250;
const ALL_PAGE = 200;   // /api/library page size when collecting a whole folder for "Phát tất cả"

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child != null) node.append(child);
  return node;
};
const svg = (paths, cls = "") => {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("aria-hidden", "true");
  if (cls) s.setAttribute("class", cls);
  for (const d of paths) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
};
const FOLDER_ICON = ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"];
const DRIVE_ICON = ["M4 6h16v12H4z", "M4 14h16", "M17 17h.01"];

// ------------------------------------------------------------------ formatting

export function formatDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(r).padStart(2, "0")}`;
}

export function formatSize(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toLocaleString("vi-VN", { maximumFractionDigits: v < 10 && i ? 1 : 0 })} ${units[i]}`;
}

export function formatResolution(width, height) {
  if (!width || !height) return "";
  const short = Math.min(width, height);  // portrait videos are "1080p" too
  if (short >= 2160) return "4K";
  return `${short}p`;
}

// ------------------------------------------------------------------ API

class HubError extends Error {
  constructor(kind, status) {
    super(kind === "offline" ? "offline" : `HTTP ${status}`);
    this.kind = kind;  // "offline" | "http"
    this.status = status;
  }
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store", ...options });
  } catch {
    throw new HubError("offline");
  }
  if (response.status === 401) {  // the PIN cookie expired, or the PIN was changed (5.2)
    location.href = `/pin?next=${encodeURIComponent(location.pathname + location.search)}`;
  }
  if (!response.ok) throw new HubError("http", response.status);
  try {
    return await response.json();
  } catch {
    throw new HubError("http", response.status);
  }
}

// ------------------------------------------------------------------ state (in the URL)

const state = { lib: null, folder: "", q: "", sort: "path" };
let folderData = null;          // GET /api/folders, loaded once (again after a rescan)
let hubInfo = null;             // GET /api/hub: is this the server? how many library folders?
let view = { items: 0, next: null, loading: false, token: 0, query: null };

function readUrl() {
  const p = new URLSearchParams(location.search);
  state.lib = p.get("lib");
  state.folder = (p.get("folder") || "").replace(/^\/+|\/+$/g, "");
  state.q = p.get("q") || "";
  state.sort = ["path", "newest", "title", "largest"].includes(p.get("sort")) ? p.get("sort") : "path";
}

function urlFor(next) {
  const s = { ...state, ...next };
  const p = new URLSearchParams();
  if (s.q) p.set("q", s.q);
  else {
    if (s.lib) p.set("lib", s.lib);
    if (s.folder) p.set("folder", s.folder);
  }
  if (s.sort !== "path") p.set("sort", s.sort);
  const qs = p.toString();
  return qs ? `/?${qs}` : "/";
}

function go(next, { replace = false } = {}) {
  Object.assign(state, next);
  history[replace ? "replaceState" : "pushState"](null, "", urlFor({}));
  render();
}

// ------------------------------------------------------------------ messages

function showMessage({ title, text, error = false, action = null }) {
  const box = $("#message");
  box.replaceChildren(el("h2", { textContent: title }), el("p", { textContent: text }));
  if (action) {
    const b = el("button", { className: "btn primary", type: "button", textContent: action.label });
    b.addEventListener("click", action.run);
    box.append(b);
  }
  box.classList.toggle("error", error);
  box.hidden = false;
}

function hideMessage() { $("#message").hidden = true; }

function showError(err) {
  const retry = { label: "Thử lại", run: () => { folderData = null; hubInfo = null; render(); } };
  if (err instanceof HubError && err.kind === "offline") {
    showMessage({
      title: "Không kết nối được tới hub",
      text: "Kiểm tra máy chủ còn đang chạy và thiết bị này dùng cùng mạng Wi-Fi/LAN với máy chủ.",
      error: true, action: retry,
    });
  } else if (err instanceof HubError) {
    showMessage({
      title: "Hub gặp lỗi khi tải dữ liệu",
      text: `Máy chủ trả về lỗi (mã ${err.status}). Thử lại sau ít phút; nếu vẫn lỗi, xem log của hub.`,
      error: true, action: retry,
    });
  } else {  // a bug in this page, not a server problem
    console.error(err);
    showMessage({
      title: "Trang gặp lỗi",
      text: "Đã xảy ra lỗi khi hiển thị trang. Tải lại trang (F5) để thử lại.",
      error: true, action: { label: "Tải lại trang", run: () => location.reload() },
    });
  }
}

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}

// ------------------------------------------------------------------ rendering

function libraries() { return folderData ? folderData.libraries : []; }

function currentLibrary() {
  const libs = libraries();
  if (state.lib) return libs.find((l) => l.id === state.lib) || null;
  return libs.length === 1 ? libs[0] : null;
}

function parentOf(folder) {
  const i = folder.lastIndexOf("/");
  return i < 0 ? "" : folder.slice(0, i);
}

function renderCrumbs(lib) {
  const nav = $("#crumbs");
  nav.replaceChildren();
  const link = (text, next) => {
    const a = el("a", { href: urlFor(next), textContent: text });
    a.addEventListener("click", (e) => { e.preventDefault(); go(next); });
    return a;
  };
  const sep = () => el("span", { className: "sep", textContent: "/", ariaHidden: "true" });

  if (state.q) {
    nav.append(link("Thư viện", { q: "" }), sep(),
               el("span", { className: "current", textContent: `Kết quả tìm “${state.q}”` }));
    return;
  }
  const multi = libraries().length > 1;
  const parts = state.folder ? state.folder.split("/") : [];
  if (multi) {
    nav.append(lib ? link("Tất cả thư viện", { lib: null, folder: "" })
                   : el("span", { className: "current", textContent: "Tất cả thư viện" }));
  }
  if (!lib) return;
  if (multi) nav.append(sep());
  nav.append(parts.length ? link(lib.name, { lib: lib.id, folder: "" })
                          : el("span", { className: "current", textContent: lib.name }));
  parts.forEach((name, i) => {
    nav.append(sep());
    const path = parts.slice(0, i + 1).join("/");
    nav.append(i === parts.length - 1 ? el("span", { className: "current", textContent: name })
                                      : link(name, { lib: lib.id, folder: path }));
  });
}

function folderTile({ name, sub, next, off = false, icon = FOLDER_ICON }) {
  const a = el("a", { className: "folder" + (off ? " off" : ""), href: urlFor(next) },
               svg(icon), el("span", {}, el("div", { className: "name", textContent: name }),
                                         el("div", { className: "sub", textContent: sub })));
  a.addEventListener("click", (e) => { e.preventDefault(); go(next); });
  return a;
}

function renderFolders(lib) {
  const box = $("#folders");
  box.replaceChildren();
  if (state.q || !folderData) { box.hidden = true; return; }
  if (!lib) {  // several libraries: choose one
    for (const l of libraries()) {
      const top = folderData.folders.find((f) => f.library.id === l.id && f.folder === "");
      const n = top ? top.total_videos : 0;
      box.append(folderTile({ name: l.name, icon: DRIVE_ICON, off: !l.available,
                              sub: `${n} video` + (l.available ? "" : " · ổ đang ngắt"),
                              next: { lib: l.id, folder: "" } }));
    }
  } else {
    const subs = folderData.folders.filter((f) => f.library.id === lib.id && f.folder !== state.folder
                                                  && parentOf(f.folder) === state.folder && f.folder !== "");
    for (const f of subs) {
      const name = f.folder.slice(f.folder.lastIndexOf("/") + 1);
      box.append(folderTile({ name, sub: `${f.total_videos} video`, next: { lib: lib.id, folder: f.folder } }));
    }
  }
  box.hidden = box.childElementCount === 0;
}

function card(item) {
  const off = !item.available;
  const img = el("img", { src: item.thumbnail_url, alt: "", loading: "lazy", decoding: "async",
                          width: 320, height: 180 });
  const thumb = el("div", { className: "thumb" }, img);
  const duration = formatDuration(item.duration);
  if (duration) thumb.append(el("span", { className: "duration", textContent: duration }));
  if (off) thumb.append(el("span", { className: "badge off", textContent: "Ổ đang ngắt" }));
  else if (item.browser_playable === false) {
    // Many browsers play MKV/HEVC/AV1 themselves (device test 4.4); the others get a
    // version converted by the hub when the video is opened (5.1).
    thumb.append(el("span", { className: "badge", textContent: "Có thể cần chuyển đổi",
                              title: "Một số trình duyệt không phát trực tiếp được định dạng này; hub tự chuyển đổi khi mở video" }));
  }
  const meta = [formatResolution(item.width, item.height), formatSize(item.size)].filter(Boolean);
  if (state.q && item.folder) meta.unshift(item.folder.split("/").pop());
  const body = [el("h3", { className: "title", textContent: item.title, title: item.name }),
                el("p", { className: "meta", textContent: meta.join(" · ") })];
  thumb.append(el("span", { className: "pick", ariaHidden: "true" }));  // its number in the selection (6.5)
  const node = off ? el("div", { className: "card off", ariaDisabled: "true",
                                 title: "Thư mục chứa video này đang không kết nối" }, thumb, ...body)
                   : el("a", { className: "card", href: `/watch/${item.id}` }, thumb, ...body);
  node.dataset.id = item.id;
  node.dataset.title = item.title;
  return node;
}

function skeletons(n) {
  return Array.from({ length: n }, () => el("div", { className: "card skeleton", ariaHidden: "true" },
    el("div", { className: "thumb" }), el("div", { className: "title" }), el("div", { className: "meta" })));
}

function libraryQuery(offset) {
  const p = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE), sort: state.sort });
  if (state.q) p.set("q", state.q);
  else {
    const lib = currentLibrary();
    if (lib) { p.set("library", lib.id); p.set("folder", state.folder); }
  }
  return `/api/library?${p}`;
}

async function loadMore() {
  if (view.loading || view.next == null) return;
  const token = view.token;
  view.loading = true;
  $("#more").hidden = true;
  try {
    const page = await api(libraryQuery(view.next));
    if (token !== view.token) return;  // the view changed meanwhile
    $("#grid").append(...page.items.map(card));
    markSelection();
    view.items += page.items.length;
    view.next = page.next_offset;
  } catch (err) {
    if (token === view.token) toast(err.kind === "offline" ? "Mất kết nối tới hub." : "Không tải thêm được video.");
  } finally {
    if (token === view.token) {
      view.loading = false;
      $("#more").hidden = view.next == null;
    }
  }
}

async function render() {
  const token = ++view.token;
  view = { items: 0, next: null, loading: true, token, query: null };
  const grid = $("#grid");
  hideMessage();
  $("#more").hidden = true;
  $("#count").textContent = "";
  $("#play-all").hidden = true;
  $("#select-mode").hidden = true;
  grid.setAttribute("aria-busy", "true");
  grid.replaceChildren(...skeletons(8));
  $("#search").value = state.q;
  $("#sort").value = state.sort;
  document.title = state.q ? `Tìm “${state.q}” – House Cinema` : "House Cinema";
  try { sessionStorage.setItem("hub:libraryUrl", urlFor({})); } catch { /* the player's "back" link */ }

  try {
    if (!hubInfo) {
      hubInfo = await api("/api/hub");
      $("#admin-link").hidden = !hubInfo.is_server;
    }
    if (!folderData) folderData = await api("/api/folders");
    if (token !== view.token) return;
    if (hubInfo.folders === 0) {  // nothing configured yet: say where to add folders
      grid.replaceChildren();
      grid.setAttribute("aria-busy", "false");
      $("#folders").hidden = true;
      $("#crumbs").replaceChildren();
      view.loading = false;
      noFolders();
      return;
    }
    const lib = currentLibrary();
    renderCrumbs(lib);
    renderFolders(lib);

    if (!state.q && !lib) {  // several libraries and none chosen: only the library tiles
      grid.replaceChildren();
      grid.setAttribute("aria-busy", "false");
      view.loading = false;
      if (!libraries().length) emptyLibrary();
      return;
    }
    const page = await api(libraryQuery(0));
    if (token !== view.token) return;
    grid.replaceChildren(...page.items.map(card));
    grid.setAttribute("aria-busy", "false");
    markSelection();
    // "Phát tất cả": the videos of this folder only (not its sub-folders), as listed.
    $("#play-all").hidden = !(page.total > 0 && !state.q && lib);
    $("#select-mode").hidden = !(page.total > 0 || selecting);
    view.items = page.items.length;
    view.next = page.next_offset;
    view.loading = false;
    $("#more").hidden = view.next == null;

    const folderCount = $("#folders").childElementCount;
    if (page.total === 0) {
      if (state.q) {
        showMessage({ title: "Không tìm thấy video nào",
                      text: `Không có video nào khớp “${state.q}”. Thử gõ ít chữ hơn — không cần gõ dấu.` });
      } else if (!folderData.folders.length) {
        emptyLibrary();
      } else if (!folderCount) {
        showMessage({ title: "Thư mục trống", text: "Thư mục này không có video." });
      }
    } else {
      $("#count").textContent = `${page.total.toLocaleString("vi-VN")} video`;
    }
    if (lib && !lib.available && !state.q) {
      toast("Thư mục thư viện đang không kết nối (ví dụ ổ USB đã rút). Video vẫn được liệt kê nhưng chưa phát được.");
    }
  } catch (err) {
    if (token !== view.token) return;
    view.loading = false;
    grid.replaceChildren();
    grid.setAttribute("aria-busy", "false");
    $("#folders").hidden = true;
    showError(err);
  }
}

function noFolders() {
  if (hubInfo.is_server) {
    showMessage({
      title: "Chưa có thư mục thư viện",
      text: "Hãy thêm thư mục chứa phim trong trang quản trị, rồi quay lại đây và bấm “Quét lại”.",
      action: { label: "Mở trang quản trị", run: () => { location.href = "/admin"; } },
    });
  } else {
    showMessage({
      title: "Chưa có thư mục thư viện",
      text: `Thư mục phim chỉ thêm được trên máy chủ: mở http://localhost:${location.port || 80}/admin trên máy tính đang chạy hub.`,
    });
  }
}

function emptyLibrary() {
  showMessage({
    title: "Thư viện trống",
    text: "Chưa có video nào. Bấm “Quét lại” để tìm video trong các thư mục đã cấu hình.",
    action: { label: "Quét lại", run: rescan },
  });
}

// ------------------------------------------------------------------ several videos (step 6.5)
// "Phát tất cả" plays the folder shown (its own videos, not the sub-folders) in the order shown.
// "Chọn" picks videos (from any folder or search) and plays them in the order they were picked.
// The selection is kept for this tab while moving between folders.

const SEL_STORE = "hub:selection";
let selecting = false;
let selection = [];  // [{id, title}] in the order picked

function loadSelection() {
  try {
    const s = JSON.parse(sessionStorage.getItem(SEL_STORE));
    if (s && Array.isArray(s.items)) {
      selection = s.items.filter((it) => it && typeof it.id === "string").slice(0, PL.MAX_ITEMS);
      selecting = Boolean(s.on);
    }
  } catch { /* nothing kept */ }
}

function saveSelection() {
  try { sessionStorage.setItem(SEL_STORE, JSON.stringify({ on: selecting, items: selection })); } catch { /* ignore */ }
}

function setSelecting(on) {
  selecting = on;
  document.body.classList.toggle("selecting", on);
  const b = $("#select-mode");
  b.setAttribute("aria-pressed", String(on));
  b.querySelector(".label").textContent = on ? "Đang chọn" : "Chọn";
  if (!on && !selection.length) b.hidden = !$("#grid .card[data-id]");
  saveSelection();
  markSelection();
}

/** Show on each card whether (and in which place) it is picked; update the bottom bar. */
function markSelection() {
  const place = new Map(selection.map((it, i) => [it.id, i + 1]));
  for (const c of document.querySelectorAll("#grid .card[data-id]")) {
    const n = place.get(c.dataset.id);
    c.classList.toggle("picked", Boolean(n));
    const pick = c.querySelector(".pick");
    if (pick) pick.textContent = n ? String(n) : "";
    if (selecting && !c.classList.contains("off")) {
      c.setAttribute("aria-label", `${c.dataset.title}${n ? ` — đã chọn, thứ ${n}` : " — bấm để chọn"}`);
    } else {
      c.removeAttribute("aria-label");
    }
  }
  const bar = $("#selbar");
  bar.hidden = !selecting;
  document.body.classList.toggle("has-selbar", selecting);
  $("#sel-count").textContent = selection.length
    ? `Đã chọn ${selection.length} video` : "Bấm vào video để chọn (phát theo thứ tự chọn)";
  for (const id of ["sel-play", "sel-tv", "sel-clear"]) $(`#${id}`).disabled = !selection.length;
}

function togglePick(card) {
  if (card.classList.contains("off")) {
    toast("Video này nằm trên ổ đang ngắt kết nối nên chưa chọn được.");
    return;
  }
  const i = selection.findIndex((it) => it.id === card.dataset.id);
  if (i >= 0) selection.splice(i, 1);
  else if (selection.length >= PL.MAX_ITEMS) { toast(`Chỉ chọn được tối đa ${PL.MAX_ITEMS} video.`); return; }
  else selection.push({ id: card.dataset.id, title: card.dataset.title });
  saveSelection();
  markSelection();
}

/** Every video of the folder shown, in the order shown (several pages if needed). */
async function folderVideos() {
  const lib = currentLibrary();
  const items = [];
  let offset = 0;
  while (offset != null && items.length < PL.MAX_ITEMS) {
    const p = new URLSearchParams({ offset: String(offset), limit: String(ALL_PAGE), sort: state.sort,
                                    library: lib.id, folder: state.folder });
    const page = await api(`/api/library?${p}`);
    items.push(...page.items);
    offset = page.next_offset;
  }
  return { items: items.filter((i) => i.available), more: offset != null, total: items.length };
}

function folderTitle() {
  const lib = currentLibrary();
  return state.folder ? state.folder.split("/").pop() : (lib ? lib.name : "");
}

async function playAll() {
  const btn = $("#play-all");
  btn.disabled = true;
  try {
    const { items, more } = await folderVideos();
    if (!items.length) { toast("Thư mục này không có video nào phát được lúc này (ổ đang ngắt?)."); return; }
    if (more) toast(`Thư mục có hơn ${PL.MAX_ITEMS} video: chỉ phát ${PL.MAX_ITEMS} video đầu.`);
    const list = PL.makeList(items.map((i) => ({ id: i.id, title: i.title })), { title: folderTitle() });
    PL.saveList(list);
    location.href = PL.watchUrl(list, 0);
  } catch (err) {
    toast(err.kind === "offline" ? "Không kết nối được tới hub." : "Không lấy được danh sách video.");
  } finally {
    btn.disabled = false;
  }
}

function playSelection(onTv) {
  if (!selection.length) return;
  const list = PL.makeList(selection, { title: "Video đã chọn" });
  PL.saveList(list);
  selection = [];
  selecting = false;
  saveSelection();
  location.href = onTv ? `/remote?list=${list.id}` : PL.watchUrl(list, 0);
}

function setupSelection() {
  loadSelection();
  document.body.classList.toggle("selecting", selecting);
  $("#select-mode").setAttribute("aria-pressed", String(selecting));
  $("#select-mode .label").textContent = selecting ? "Đang chọn" : "Chọn";
  $("#select-mode").addEventListener("click", () => setSelecting(!selecting));
  $("#play-all").addEventListener("click", playAll);
  $("#sel-play").addEventListener("click", () => playSelection(false));
  $("#sel-tv").addEventListener("click", () => playSelection(true));
  $("#sel-clear").addEventListener("click", () => { selection = []; saveSelection(); markSelection(); });
  $("#sel-done").addEventListener("click", () => setSelecting(false));
  $("#grid").addEventListener("click", (e) => {
    if (!selecting) return;
    const c = e.target.closest(".card[data-id]");
    if (!c) return;
    e.preventDefault();  // picking, not opening the player
    togglePick(c);
  });
  markSelection();
}

// ------------------------------------------------------------------ rescan

const SCAN_POLL_MS = 1000;
let rescanning = false;
let scanHideTimer;

// What the progress row shows for a rescan status (GET /api/rescan). Step 1 lists the folders
// (videos found so far, total unknown: the bar just moves); step 2 reads details and makes
// thumbnails (done / total: a real percentage). `percent` null = unknown.
export function scanProgress(s) {
  if (s.phase === "media") {
    const total = s.progress_total, done = Math.min(s.progress_done ?? 0, total ?? 0);
    if (!total) return { label: "Bước 2/2: chuẩn bị đọc thông tin video…", count: "", percent: null };
    const percent = Math.floor((done / total) * 100);
    return { label: "Bước 2/2: đọc thông tin và tạo ảnh thu nhỏ", count: `${done}/${total} · ${percent}%`, percent };
  }
  const found = s.progress_done ?? 0;
  return { label: "Bước 1/2: tìm video trong các thư mục…", count: found ? `đã thấy ${found} video` : "", percent: null };
}

function showScan(s) {
  clearTimeout(scanHideTimer);
  const box = $("#scan"), bar = $("#scan-bar"), fill = bar.firstElementChild;
  const p = scanProgress(s);
  // The label is a live region: change it only when the step changes, not every second.
  if ($("#scan-label").textContent !== p.label) $("#scan-label").textContent = p.label;
  $("#scan-count").textContent = p.count;
  box.classList.toggle("indeterminate", p.percent == null);
  if (p.percent == null) {
    fill.style.width = "";
    bar.removeAttribute("aria-valuenow");
    bar.setAttribute("aria-valuetext", p.count || p.label);
  } else {
    fill.style.width = `${p.percent}%`;
    bar.setAttribute("aria-valuenow", String(p.percent));
    bar.setAttribute("aria-valuetext", p.count);
  }
  box.hidden = false;
}

function finishScan(text) {
  const box = $("#scan"), bar = $("#scan-bar");
  box.classList.remove("indeterminate");
  bar.firstElementChild.style.width = "100%";
  bar.setAttribute("aria-valuenow", "100");
  bar.setAttribute("aria-valuetext", text);
  $("#scan-label").textContent = text;
  $("#scan-count").textContent = "";
  box.hidden = false;
  clearTimeout(scanHideTimer);
  scanHideTimer = setTimeout(() => { box.hidden = true; }, 2000);
}

function hideScan() {
  clearTimeout(scanHideTimer);
  $("#scan").hidden = true;
}

function rescanErrorText(err) {
  return err.kind === "offline" ? "Không kết nối được tới hub."
    : err.status === 409 ? "Chưa có thư mục thư viện: hãy thêm thư mục trong trang quản trị (trên máy chủ)."
    : "Quét lại không thành công.";
}

async function rescan() {
  if (rescanning) return;
  let s;
  try {
    s = await api("/api/rescan", { method: "POST" });  // already running elsewhere: its status
  } catch (err) {
    toast(rescanErrorText(err));
    return;
  }
  await followRescan(s);
}

// Show a running rescan until it ends: started here, or by another device (seen at page load).
async function followRescan(s) {
  if (rescanning) return;
  rescanning = true;
  // The button only spins (its label stays short: a longer one wrapped the top bar on phones);
  // the progress row under the top bar says what is happening.
  const btn = $("#rescan");
  btn.disabled = true;
  btn.classList.add("spinning");
  try {
    while (s.state === "running") {
      showScan(s);
      await new Promise((r) => setTimeout(r, SCAN_POLL_MS));
      s = await api("/api/rescan");
    }
    if (s.state === "failed") {
      hideScan();
      toast("Quét lại không thành công. Xem log của hub để biết chi tiết.");
    } else {
      const c = s.counts;
      finishScan(`Đã quét xong: ${c.found} video`);
      const changes = [c.added && `+${c.added} mới`, c.removed && `−${c.removed} đã xoá`,
                       c.renamed && `${c.renamed} đổi tên`, c.changed && `${c.changed} thay đổi`].filter(Boolean);
      toast(changes.length ? `Đã quét xong: ${changes.join(", ")}.` : `Đã quét xong: ${c.found} video, không có thay đổi.`);
      folderData = null;
      hubInfo = null;
      render();
    }
  } catch (err) {
    hideScan();
    toast(rescanErrorText(err));
  } finally {
    rescanning = false;
    btn.disabled = false;
    btn.classList.remove("spinning");
  }
}

// ------------------------------------------------------------------ wiring

function init() {
  readUrl();
  if (new URLSearchParams(location.search).get("after") === "admin") {  // back from the admin page
    history.replaceState(null, "", urlFor({}));
    toast("Bấm “Quét lại” để tìm video trong các thư mục vừa thay đổi.");
  }
  let timer;
  const search = $("#search");
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const q = search.value.trim();
      if (q !== state.q) go({ q }, { replace: Boolean(state.q) && Boolean(q) });
    }, SEARCH_DELAY_MS);
  });
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { clearTimeout(timer); go({ q: search.value.trim() }); }
    if (e.key === "Escape" && search.value) { search.value = ""; clearTimeout(timer); go({ q: "" }); }
  });
  $("#sort").addEventListener("change", (e) => go({ sort: e.target.value }, { replace: true }));
  $("#rescan").addEventListener("click", rescan);
  $("#more button").addEventListener("click", loadMore);
  setupSelection();
  document.querySelector('[data-nav="home"]').addEventListener("click", (e) => {
    e.preventDefault();
    go({ lib: null, folder: "", q: "" });
  });
  window.addEventListener("popstate", () => { readUrl(); render(); });
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { rootMargin: "800px" }).observe($("#sentinel"));
  render();
  // A rescan started on another device (or before a reload) shows its progress here too.
  api("/api/rescan").then((s) => { if (s.state === "running") followRescan(s); }).catch(() => {});
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
