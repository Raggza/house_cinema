// House Cinema — PIN page (plan step 5.2). On success the hub sets a cookie and we go back to
// the page that was asked for (?next=, a path of this hub only).

const $ = (sel) => document.querySelector(sel);

function nextUrl() {
  const n = new URLSearchParams(location.search).get("next") || "/";
  return n.startsWith("/") && !n.startsWith("//") && !n.includes("\\") ? n : "/";
}

let unlockTimer;
function say(text, kind = "error") {
  const m = $("#pin-message");
  m.textContent = text;
  m.className = `pin-message ${kind}`;
  m.hidden = false;
}

function lock(seconds, text) {
  const input = $("#pin"), button = $("#pin-submit");
  input.disabled = button.disabled = true;
  say(text);
  clearTimeout(unlockTimer);
  unlockTimer = setTimeout(() => {
    input.disabled = button.disabled = false;
    say("Bạn có thể thử lại.", "info");
    input.focus();
  }, seconds * 1000);
}

async function submit(event) {
  event.preventDefault();
  const input = $("#pin");
  const pin = input.value.trim();
  if (!pin) return;
  $("#pin-submit").disabled = true;
  let r, data = {};
  try {
    r = await fetch("/api/pin", { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ pin }), cache: "no-store" });
    data = await r.json().catch(() => ({}));
  } catch {
    $("#pin-submit").disabled = false;
    say("Không kết nối được tới hub. Kiểm tra máy chủ còn chạy và cùng mạng Wi-Fi.");
    return;
  }
  $("#pin-submit").disabled = false;
  if (r.ok) { location.replace(nextUrl()); return; }
  input.value = "";
  input.focus();
  if (r.status === 429) { lock(data.retry_after || 60, data.detail || "Nhập sai quá nhiều lần. Thử lại sau."); return; }
  if (r.status === 401) {
    const left = data.attempts_left;
    say(left != null ? `PIN không đúng. Còn ${left} lần thử.` : "PIN không đúng.");
    return;
  }
  say(`Hub gặp lỗi (mã ${r.status}).`);
}

$("#pin-form").addEventListener("submit", submit);
