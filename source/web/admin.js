// House Cinema — admin page (plan step 4.2): library folders. Only served to the server
// computer itself (the hub answers 403 to other devices). Paths are inserted as text only.

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children) if (child != null) node.append(child);
  return node;
};

let state = null;  // last GET /api/admin/libraries

class HubError extends Error {
  constructor(kind, status, body) { super(kind); this.kind = kind; this.status = status; this.body = body; }
}

async function api(path, options = {}) {
  let r;
  try {
    r = await fetch(path, { cache: "no-store", headers: { Accept: "application/json",
                                                          "Content-Type": "application/json" }, ...options });
  } catch {
    throw new HubError("offline");
  }
  let body = null;
  try { body = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) throw new HubError("http", r.status, body);
  return body;
}

let toastTimer;
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

function showNotice(text, kind = "info") {
  const n = $("#notice");
  n.textContent = text;
  n.className = `notice ${kind}`;
  n.hidden = !text;
}

function showErrors(problems) {
  const box = $("#errors");
  box.replaceChildren();
  if (!problems || !problems.length) { box.hidden = true; return; }
  const list = el("ul");
  for (const p of problems) list.append(el("li", { textContent: p.message }));
  box.append(list);
  box.hidden = false;
}

function explain(err) {
  if (err.kind === "offline") return "Không kết nối được tới hub. Kiểm tra hub còn đang chạy.";
  if (err.status === 403) return "Trang quản trị chỉ dùng được trên máy chủ.";
  if (err.status === 409) return "Hub đang chạy với --path: danh sách thư mục không sửa được ở đây.";
  return (err.body && err.body.detail) || `Hub gặp lỗi (mã ${err.status}).`;
}

function render() {
  const list = $("#folders");
  list.replaceChildren();
  list.setAttribute("aria-busy", "false");
  const editable = state.editable;
  if (!state.folders.length) {
    list.append(el("li", { className: "empty", textContent: "Chưa có thư mục nào. Bấm Thêm bên dưới để chọn thư mục chứa phim." }));
  }
  for (const f of state.folders) {
    const status = f.problem
      ? el("span", { className: "state bad", textContent: f.exists ? f.problem : "Không tìm thấy (ổ có thể đang rút)" })
      : el("span", { className: "state ok", textContent: "Sẵn sàng" });
    const count = el("span", { className: "count-badge", textContent: f.videos ? `${f.videos} video` : "chưa quét" });
    const remove = el("button", { className: "btn", type: "button", textContent: "Bỏ", disabled: !editable,
                                  title: "Bỏ thư mục này khỏi thư viện (không xoá file trên ổ)" });
    remove.addEventListener("click", () => removeFolder(f));
    list.append(el("li", { className: "folder-row" },
      el("div", { className: "folder-info" }, el("code", { className: "path", textContent: f.path }),
         el("div", { className: "meta" }, status, count)),
      remove));
  }
  $("#add").hidden = !editable;
  if (state.source === "cli") {
    showNotice("Hub đang chạy với tham số --path nên danh sách này chỉ xem được. Chạy lại hub không có --path để sửa ở đây.", "warn");
  } else if (state.source === "config" && state.folders.length) {
    showNotice("Danh sách đang lấy từ config.yml. Lần lưu đầu tiên ở trang này sẽ thay thế nó.", "info");
  } else {
    showNotice("");
  }
  $("#store").textContent = editable ? `Danh sách được lưu tại: ${state.store_file}` : "";
}

async function load() {
  try {
    state = await api("/api/admin/libraries");
    render();
  } catch (err) {
    $("#folders").setAttribute("aria-busy", "false");
    $("#add").hidden = true;
    showNotice(explain(err), "error");
  }
}

async function save(paths, { successText }) {
  showErrors(null);
  try {
    state = await api("/api/admin/libraries", { method: "PUT", body: JSON.stringify({ paths }) });
    render();
    $("#saved").hidden = false;
    toast(successText);
    return true;
  } catch (err) {
    if (err.status === 422 && err.body && err.body.problems) showErrors(err.body.problems);
    else showErrors([{ message: explain(err) }]);
    return false;
  }
}

async function removeFolder(folder) {
  const videos = folder.videos
    ? `${folder.videos} video của thư mục này sẽ không còn hiện trong thư viện, và lần quét tới sẽ xoá chúng khỏi chỉ mục (kể cả ảnh thu nhỏ).`
    : "Thư mục này chưa có video nào trong thư viện.";
  const ok = window.confirm(`Bỏ thư mục khỏi thư viện?\n\n${folder.path}\n\n${videos}\nFile phim trên ổ đĩa không bị xoá.`);
  if (!ok) return;
  await save(state.folders.map((f) => f.path).filter((p) => p !== folder.path),
             { successText: "Đã bỏ thư mục." });
}

// ------------------------------------------------------------------ connect (step 5.3)

const KIND = { ethernet: "Ethernet", wifi: "Wi-Fi", vpn: "VPN", virtual: "mạng ảo", other: "mạng" };
const label = (a) => {
  const kind = KIND[a.kind] || a.kind;
  return a.name && a.name !== kind ? `${kind} · ${a.name}` : kind;
};

async function loadNetwork() {
  const box = $("#connect");
  let net;
  try {
    net = await api("/api/admin/network");
  } catch (err) {
    box.setAttribute("aria-busy", "false");
    $("#urls").replaceChildren(el("li", { textContent: explain(err) }));
    return;
  }
  box.setAttribute("aria-busy", "false");
  $("#urls").replaceChildren(...net.addresses.map((a) => el("li", {},
    el("a", { href: a.url, textContent: a.url, className: "url" }),
    el("span", { className: "hint small", textContent: ` ${label(a)}` }))));
  if (net.qr_svg) { $("#qr").src = net.qr_svg; $("#qr").hidden = false; }
  const warn = $("#net-warnings");
  warn.replaceChildren(...net.warnings.map((w) => el("p", { textContent: w })));
  warn.hidden = !net.warnings.length;
  if (net.others.length) {
    $("#others").replaceChildren(...net.others.map((a) => el("li", {
      textContent: `${a.ip} · ${label(a)}${a.up ? "" : " (không kết nối)"}` })));
    $("#net-others").hidden = false;
  }
}

// ------------------------------------------------------------------ ffmpeg (step 6.1)

async function loadTools() {
  let tools;
  try { tools = await api("/api/admin/tools"); } catch { return; }
  if (!tools.disabled.length) return;
  const missing = [tools.ffmpeg ? null : "ffmpeg", tools.ffprobe ? null : "ffprobe"].filter(Boolean).join(" và ");
  $("#tools-title").textContent = `Thiếu ${missing}`;
  $("#tools-missing").textContent = "Hub vẫn chạy, nhưng các tính năng sau bị tắt:";
  $("#tools-off").replaceChildren(...tools.disabled.map((f) => el("li", { textContent: f })));
  $("#tools-hint").textContent = tools.hint;
  $("#tools").hidden = false;
}

// ------------------------------------------------------------------ folder picker

// The browser never tells a page the full path of a folder picked in it, so the hub (which runs
// on this same computer: this page is server-only) opens the system's own picker and answers
// with the path. Returns the chosen folder, or null (cancelled, or the picker could not open).
async function pickFolder() {
  const where = /Mac/i.test(navigator.userAgent) ? "Finder" : "File Explorer";
  const status = $("#picking");
  status.textContent = `Đang mở hộp chọn thư mục (${where}) trên máy này… Chọn thư mục rồi bấm “Chọn”. `
    + "Nếu không thấy, hãy tìm cửa sổ đó trên thanh tác vụ.";
  status.hidden = false;
  showErrors(null);
  try {
    const picked = await api("/api/admin/pick-folder", { method: "POST" });
    return picked.cancelled ? null : picked.path;
  } catch (err) {
    showErrors([{ message: explainPicker(err) }]);
    return null;
  } finally {
    status.hidden = true;
  }
}

function explainPicker(err) {
  if (err.kind === "offline") return explain(err);
  if (err.status === 409 || err.status === 501) return err.body?.detail || explain(err);
  return explain(err);
}

async function addFolder(path) {
  return save([...state.folders.map((f) => f.path), path], { successText: "Đã thêm thư mục." });
}

function init() {
  loadTools();
  loadNetwork();
  $("#add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#path");
    const button = e.submitter || $("#add button");
    button.disabled = true;
    try {
      const typed = input.value.trim();
      if (typed) {  // a pasted path (also for network folders, or a server without a desktop)
        if (await addFolder(typed)) input.value = "";
      } else {
        const picked = await pickFolder();
        if (picked) await addFolder(picked);
      }
    } finally {
      button.disabled = false;
      input.focus();
    }
  });
  load();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
else init();
