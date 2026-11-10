"""Build docs/testing/device-matrix.md from the reports sent by /diagnostics (plan step 4.4).

    uv run python scripts/device_matrix.py                 # data/device-reports → the matrix
    uv run python scripts/device_matrix.py --reports DIR --out FILE

Only the part between the AUTO markers is rewritten: notes and screenshot links written by
hand elsewhere in the file are kept. The newest report of each device name is used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from source.modules.server.diagnostics import SAMPLES  # noqa: E402

START, END = "<!-- AUTO:START (scripts/device_matrix.py) -->", "<!-- AUTO:END -->"
FORMAT_LABELS = {s.key: s.label for s in SAMPLES}
MUST_PLAY = {s.key for s in SAMPLES if s.must_play}

TEMPLATE = f"""# Ma trận kiểm thử thiết bị (bước 4.4)

Cách thêm một thiết bị: chạy hub, mở `http://<IP-LAN>:8000/diagnostics` trên thiết bị đó, bấm
**Bắt đầu kiểm tra**, trả lời các câu hỏi, bấm **Gửi kết quả về hub**; sau đó trên máy chủ chạy
`uv run python scripts/device_matrix.py`. Báo cáo mới nằm trong `data/device-reports/`; muốn giữ
cùng tài liệu thì chép sang `docs/testing/reports/`. Ảnh chụp màn hình (lúc đang phát có phụ đề,
và sau khi tua) đặt trong `docs/testing/img/` và dán link vào mục "Ảnh chụp" bên dưới.
Phím remote của Smart TV (bước 5.4): trên TV mở cùng trang, dùng mục **Kiểm tra phím remote**, rồi
**Gửi kết quả phím về hub**; kết quả hiện ở bảng "Phím remote".

Ký hiệu: ✔ đạt · ✘ không đạt · 🔈 chỉ có tiếng, không có hình · 🔇 có hình, không có tiếng ·
– chưa thử / không áp dụng ·
*(tự động)* = chạy không người, chưa có câu trả lời bằng mắt.

{START}
{END}

## Ảnh chụp

| Thiết bị | Đang phát có phụ đề | Sau khi tua |
|---|---|---|

## Ghi chú thủ công
"""


def mark(value: bool | None) -> str:
    return "✔" if value is True else "✘" if value is False else "–"


def _shown(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def is_key_report(data: dict) -> bool:
    """A remote-key test (plan step 5.4) only: it must not replace the device's video results."""
    return bool(data.get("keys")) and not data.get("checks") and not data.get("formats")


def load_all(folders: list[Path]) -> tuple[list[dict], list[dict]]:
    """(video test reports, remote-key reports): the newest of each kind per device name."""
    files = [f for folder in folders if folder.is_dir() for f in folder.glob("*.json")]
    newest: dict[str, dict] = {}
    newest_keys: dict[str, dict] = {}
    for f in sorted(files, key=lambda p: p.name):  # names start with the time: later wins
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"skipped unreadable {f.name}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"skipped unreadable {f.name}", file=sys.stderr)
            continue
        data["_file"] = _shown(f)
        name = str(data.get("device", f.stem)).strip().casefold()
        (newest_keys if is_key_report(data) else newest)[name] = data
    order = lambda r: str(r.get("device", "")).casefold()  # noqa: E731
    return sorted(newest.values(), key=order), sorted(newest_keys.values(), key=order)


def load_reports(folders: list[Path]) -> list[dict]:
    return load_all(folders)[0]


def cell_check(r: dict, key: str) -> str:
    c = r.get("checks", {}).get(key)
    return mark(c.get("ok")) if c else "–"


def subtitles_cell(r: dict) -> str:
    data = cell_check(r, "subtitles")
    seen = r.get("confirmations", {}).get("subtitle_visible")
    return data if seen is None else f"{data} (thấy: {mark(seen)})"


def fullscreen_cell(r: dict) -> str:
    seen = r.get("confirmations", {}).get("fullscreen")
    if seen is not None:
        return mark(seen)
    api = r.get("checks", {}).get("fullscreen_api", {}).get("ok")
    return f"API {mark(api)}" if api is not None else "–"


def format_cell(r: dict, key: str) -> str:
    f = next((x for x in r.get("formats", []) if x.get("key") == key), None)
    if not f or f.get("plays") is None:
        return "–"
    if f["plays"] and f.get("picture") is False:
        return "🔈"
    if f["plays"] and f.get("sound") is False:
        return "🔇"
    if f["plays"] and f.get("picture") and f.get("sound") is None:
        return "✔ (tiếng?)"  # Safari: picture yes, but the browser cannot tell whether sound was decoded
    return mark(bool(f["plays"]))


REMOTE_BUTTONS = [("up", "Lên"), ("down", "Xuống"), ("left", "Trái"), ("right", "Phải"), ("ok", "OK"),
                  ("back", "Back"), ("play", "Play"), ("pause", "Pause"), ("stop", "Stop"), ("rewind", "Tua lùi"),
                  ("fast_forward", "Tua nhanh")]


def key_cell(r: dict, button: str) -> str:
    k = next((x for x in r.get("keys", []) if x.get("button") == button), None)
    if not k or k.get("received") is None:
        return "–"
    if k["received"] is False:
        return "✘"
    via = k.get("via")
    if via == "popstate":
        return "lịch sử (Back của trình duyệt)"
    if via == "click":
        return "click (con trỏ)"
    name = k.get("key") or k.get("code") or ""
    text = f"`{name}` ({k.get('key_code', 0)})" if name else f"keyCode {k.get('key_code', 0)}"
    return (text + (" chỉ keyup" if via == "keyup" else "")).replace("|", "\\|")


def build_keys(key_reports: list[dict]) -> list[str]:
    """Remote-key table (plan step 5.4): what each TV browser receives for each button."""
    head = ["Thiết bị", "Ngày", *[label for _, label in REMOTE_BUTTONS]]
    lines = ["", "### Phím remote (bước 5.4)", "",
             "Ô ghi `key` (keyCode) mà trang nhận được · ✘ bấm mà trang không nhận được · – bỏ qua.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in key_reports:
        row = [str(r.get("device", "?")), (r.get("received_at") or "")[:10],
               *[key_cell(r, b) for b, _ in REMOTE_BUTTONS]]
        lines.append("| " + " | ".join(row) + " |")
    if not key_reports:
        lines.append("| *(chưa có báo cáo phím nào)* |" + " |" * (len(head) - 1))
    return lines


def build(reports: list[dict], key_reports: list[dict] | None = None) -> str:
    key_reports = key_reports or []
    keys = [s.key for s in SAMPLES]
    head = ["Thiết bị", "Ngày", "Phát MP4", "Tua", "Phụ đề", "Toàn màn hình", "Có tiếng", "Nhớ vị trí", "Range",
            *[FORMAT_LABELS[k] for k in keys]]
    lines = [f"Cập nhật từ {len(reports)} báo cáo thiết bị.", "",
             "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in reports:
        name = r.get("device", "?") + (" *(tự động)*" if r.get("auto") else "")
        row = [name, (r.get("received_at") or "")[:10], cell_check(r, "play"), cell_check(r, "seek"),
               subtitles_cell(r), fullscreen_cell(r), mark(r.get("confirmations", {}).get("sound")),
               cell_check(r, "resume_storage"), cell_check(r, "range"), *[format_cell(r, k) for k in keys]]
        lines.append("| " + " | ".join(row) + " |")
    if not reports:
        lines.append("| *(chưa có báo cáo nào)* |" + " |" * (len(head) - 1))
    if any("(tiếng?)" in line for line in lines):
        lines += ["", "*✔ (tiếng?)*: có hình, nhưng trình duyệt (Safari) không cho trang biết có giải mã được tiếng "
                      "không. Cột \"Có tiếng\" là câu trả lời của người thử cho video MP4 H.264 + AAC."]

    lines += ["", "### Giới hạn đã biết (tự suy ra từ kết quả)", ""]
    problems = []
    for r in reports:
        dev = r.get("device", "?")
        for key in ("play", "seek", "range", "subtitles", "resume_storage"):
            if r.get("checks", {}).get(key, {}).get("ok") is False:
                detail = r["checks"][key].get("detail", "")
                problems.append(f"- **{dev}**: `{key}` không đạt{f' ({detail})' if detail else ''}.")
        for f in r.get("formats", []):
            if f.get("plays") is None:
                continue
            label = FORMAT_LABELS.get(f["key"], f["key"])
            must = " — **định dạng bắt buộc của plan**" if f["key"] in MUST_PLAY else ""
            if not f["plays"]:
                problems.append(f"- **{dev}**: không phát được {label} ({f.get('detail', '')}){must}.")
            elif f.get("picture") is False:
                problems.append(f"- **{dev}**: {label} chỉ có tiếng, không có hình{must}.")
            elif f.get("sound") is False:
                problems.append(f"- **{dev}**: {label} có hình nhưng không có tiếng{must}.")
    lines += problems or ["- Chưa thấy giới hạn nào trong các báo cáo hiện có."]
    lines += build_keys(key_reports)

    lines += ["", "### Báo cáo gốc", ""]
    lines += [f"- {r.get('device', '?')}{' (phím remote)' if is_key_report(r) else ''}: `{r['_file']}` — "
              f"{r.get('user_agent', '')}" for r in [*reports, *key_reports]] or ["- (không có)"]
    notes = [f"- **{r.get('device', '?')}**: {r['notes']}" for r in [*reports, *key_reports] if r.get("notes")]
    if notes:
        lines += ["", "### Ghi chú của người kiểm tra", "", *notes]
    return "\n".join(lines)


def write_matrix(reports: list[dict], out: Path, key_reports: list[dict] | None = None) -> None:
    text = out.read_text(encoding="utf-8") if out.exists() else TEMPLATE
    if START not in text or END not in text:
        text = text.rstrip() + "\n\n" + START + "\n" + END + "\n"
    before, rest = text.split(START, 1)
    _, after = rest.split(END, 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{before}{START}\n{build(reports, key_reports)}\n{END}{after}", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, action="append",
                        help="Folder of reports (repeatable). Default: data/device-reports (new, sent by "
                             "devices) and docs/testing/reports (kept with the docs).")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "testing" / "device-matrix.md")
    args = parser.parse_args()
    folders = args.reports or [PROJECT_ROOT / "data" / "device-reports", PROJECT_ROOT / "docs" / "testing" / "reports"]
    reports, key_reports = load_all(folders)
    write_matrix(reports, args.out, key_reports)
    print(f"{args.out}: {len(reports)} device(s), {len(key_reports)} remote-key report(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
