// House Cinema — playlists (plan step 6.5), shared by the player page, the TV page and the
// library page. A playlist is a list of videos plus the order they play in: the list order,
// or a shuffled one (the video playing stays first). Repeat: "off", "all" (back to the first
// after the last) or "one" (the same video again).
// The player keeps the playlist in sessionStorage (one per browser tab, never in the URL: a
// long list would not fit); the URL only says which list and where in it (?list=…&p=…).

export const MAX_ITEMS = 500;
export const REPEAT_MODES = ["off", "all", "one"];
export const REPEAT_LABEL = { off: "Không lặp", all: "Lặp danh sách", one: "Lặp 1 video" };
const STORE = "hub:playlist";

function token() {
  const a = new Uint8Array(8);
  (globalThis.crypto || window.crypto).getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function identity(n) { return Array.from({ length: n }, (_, i) => i); }

/** A random order of 0..n-1 that starts with `first` (Fisher–Yates). */
export function shuffledOrder(n, first, rand = Math.random) {
  const rest = identity(n).filter((i) => i !== first);
  for (let i = rest.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [rest[i], rest[j]] = [rest[j], rest[i]];
  }
  return first == null || first < 0 ? rest : [first, ...rest];
}

/** items: [{id, title}] in play order (the order chosen). `index`: the item to start with. */
export function makeList(items, { index = 0, title = "", shuffle = false, repeat = "off", id = null } = {}) {
  const kept = [];
  const seen = new Set();
  for (const it of items || []) {
    if (!it || typeof it.id !== "string" || !it.id || seen.has(it.id)) continue;
    seen.add(it.id);
    kept.push({ id: it.id.slice(0, 200), title: String(it.title || "").slice(0, 300) });
    if (kept.length >= MAX_ITEMS) break;
  }
  const n = kept.length;
  const start = Math.min(Math.max(0, Number(index) || 0), Math.max(0, n - 1));
  const list = { id: id || token(), title: String(title || "").slice(0, 200), items: kept,
                 shuffle: false, repeat: REPEAT_MODES.includes(repeat) ? repeat : "off",
                 order: identity(n), pos: start, fails: 0 };
  if (shuffle) setShuffle(list, true);
  return list;
}

export function size(list) { return list ? list.items.length : 0; }
export function currentIndex(list) { return list.order[list.pos]; }
export function current(list) { return list.items[currentIndex(list)]; }
export function itemAt(list, pos) { return pos == null ? null : list.items[list.order[pos]]; }

export function setShuffle(list, on, rand = Math.random) {
  const cur = currentIndex(list);
  list.shuffle = Boolean(on);
  list.order = list.shuffle ? shuffledOrder(list.items.length, cur, rand) : identity(list.items.length);
  list.pos = Math.max(0, list.order.indexOf(cur));
}

export function setRepeat(list, mode) {
  if (REPEAT_MODES.includes(mode)) list.repeat = mode;
}

export function cycleRepeat(list) {
  setRepeat(list, REPEAT_MODES[(REPEAT_MODES.indexOf(list.repeat) + 1) % REPEAT_MODES.length]);
  return list.repeat;
}

/** Where to go after the current video. `auto`: it ended by itself ("Lặp 1 video" replays it;
 * a "Sau" press moves on). null = the end of the list. */
export function nextPos(list, { auto = false } = {}) {
  if (!size(list)) return null;
  if (auto && list.repeat === "one") return list.pos;
  if (list.pos + 1 < list.order.length) return list.pos + 1;
  return list.repeat === "off" ? null : 0;
}

export function prevPos(list) {
  if (!size(list)) return null;
  if (list.pos > 0) return list.pos - 1;
  return list.repeat === "off" ? null : list.order.length - 1;
}

/** Jump to the item at `index` of the list (a click in the list panel). */
export function posOfIndex(list, index) { return list.order.indexOf(index); }

// ------------------------------------------------------------------ storage (one tab)

export function saveList(list) {
  try { sessionStorage.setItem(STORE, JSON.stringify(list)); } catch { /* private mode: still plays */ }
}

export function loadList(id) {
  let list = null;
  try { list = JSON.parse(sessionStorage.getItem(STORE)); } catch { return null; }
  if (!list || list.id !== id || !Array.isArray(list.items) || !list.items.length) return null;
  const n = list.items.length;
  const okOrder = Array.isArray(list.order) && list.order.length === n
    && [...list.order].sort((a, b) => a - b).every((v, i) => v === i);
  if (!okOrder) list.order = identity(n);
  if (!Number.isInteger(list.pos) || list.pos < 0 || list.pos >= n) list.pos = 0;
  if (!REPEAT_MODES.includes(list.repeat)) list.repeat = "off";
  list.fails = Number(list.fails) || 0;
  return list;
}

export function watchUrl(list, pos = list.pos) {
  const it = itemAt(list, pos);
  return `/watch/${encodeURIComponent(it.id)}?list=${list.id}&p=${pos}`;
}
