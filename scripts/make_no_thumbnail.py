"""Generate source/image/assets/no-thumbnail.jpg (the default thumbnail).

Run once from the project root on Windows (uses Segoe UI for Vietnamese text):
    uv run python scripts/make_no_thumbnail.py
The output is committed, so the app never needs this script at runtime.
"""

import shutil
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "source" / "image" / "assets" / "no-thumbnail.jpg"
FONT = "C\\:/Windows/Fonts/segoeui.ttf"  # ':' escaped for the ffmpeg filter syntax
SYMBOL_FONT = "C\\:/Windows/Fonts/seguisym.ttf"  # Segoe UI has no ▶ glyph

# 320x180 dark slate background, a lighter "screen" box, a centred play mark and a caption.
FILTER = ",".join([
    "drawbox=x=110:y=40:w=100:h=64:color=0x3a4150:t=fill",
    f"drawtext=fontfile='{SYMBOL_FONT}':text='▶':fontsize=30:fontcolor=0x9aa3b2:x=(w-tw)/2:y=55",
    f"drawtext=fontfile='{FONT}':text='Không có ảnh xem trước':fontsize=17:fontcolor=0xc7cdd6:"
    "x=(w-tw)/2:y=124",
])


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "color=c=0x262b33:s=320x180",
           "-vf", FILTER, "-frames:v", "1", "-q:v", "3", str(OUT)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return 1
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
