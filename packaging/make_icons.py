"""Write the app icons for packaging: icon.ico (Windows), icon.icns (macOS), icon.png.

    uv run --group desktop python packaging/make_icons.py [OUT_DIR]     # default: build/icons
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source.desktop.icon import draw_icon  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    big = draw_icon(1024)
    big.save(out / "icon.png")
    # Each size drawn on its own (sharper than scaling one image down).
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = [draw_icon(s) for s in sizes]
    images[-1].save(out / "icon.ico", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    big.save(out / "icon.icns")
    print(f"Icons written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
