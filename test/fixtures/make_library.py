"""Generate the sample video library used by tests: test/fixtures/library/.

Run from the project root:
    python test/fixtures/make_library.py [--ffmpeg PATH] [--force]

Videos are 2-second ffmpeg test patterns (a few KB each), so the output is small
enough to keep in the repository. Re-running without --force keeps existing files.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "library"

SRC_VIDEO = ["-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15"]
SRC_AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]

# relative path -> ffmpeg encode args (after the inputs)
VIDEOS: dict[str, list[str]] = {
    "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4": [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
    ],
    "Phim lẻ/Tài liệu/Đại dương xanh.webm": [
        # yuv420p: without it testsrc (RGB) gives VP9 profile 1 (4:4:4), which browsers
        # such as Safari cannot play; real-world WebM is 4:2:0.
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuv420p", "-b:v", "50k", "-c:a", "libopus",
    ],
    "Phim bộ/Mùa 1/Tập 01.mkv": [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
    ],
    "Phim bộ/Mùa 1/Tập 02 HEVC.mkv": [
        "-c:v", "libx265", "-pix_fmt", "yuv420p", "-c:a", "aac", "-x265-params", "log-level=error",
    ],
    "UPPERCASE EXT/Clip ngắn.MP4": [
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
    ],
}

SRT_VI = """1
00:00:00,000 --> 00:00:01,000
Xin chào, đây là phụ đề tiếng Việt.

2
00:00:01,000 --> 00:00:02,000
Cuộc chiến cuối cùng bắt đầu!
"""

SRT_EN = """1
00:00:00,000 --> 00:00:01,000
Hello, this is an English subtitle.

2
00:00:01,000 --> 00:00:02,000
The final battle begins!
"""


TONE_MARKS = {"̀", "́", "̃", "̉", "̣"}


def encode_cp1258(text: str) -> bytes:
    """Encode like Windows-1258: base letter (ê, ơ, ă...) precomposed, tone mark combining."""
    out = bytearray()
    for ch in unicodedata.normalize("NFC", text):
        try:
            out += ch.encode("cp1258")
            continue
        except UnicodeEncodeError:
            pass
        parts = unicodedata.normalize("NFD", ch)
        base = unicodedata.normalize("NFC", "".join(c for c in parts if c not in TONE_MARKS))
        tones = "".join(c for c in parts if c in TONE_MARKS)
        out += (base + tones).encode("cp1258")
    return bytes(out)


TEXT_FILES: dict[str, bytes] = {
    "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.vi.srt": SRT_VI.encode("utf-8"),
    "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.en.srt": SRT_EN.encode("utf-8"),
    "Phim bộ/Mùa 1/Tập 01.vi.srt": encode_cp1258(SRT_VI),
    "Phim lẻ/ghi chú.txt": "Không phải video, scanner phải bỏ qua.\n".encode("utf-8"),
    # Not a real video: truncated junk with a video extension.
    "Hỏng/File hỏng.mp4": b"\x00\x00\x00\x18ftypisom" + b"\xde\xad\xbe\xef" * 64,
    # Hidden by naming convention (dot prefix) — scanner must skip it.
    ".hidden/.an.mp4": b"hidden placeholder",
}

EMPTY_DIRS = ["Thư mục rỗng"]


def make_video(ffmpeg: str, target: Path, codec_args: list[str]) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           *SRC_VIDEO, *SRC_AUDIO, *codec_args, "-shortest", str(target)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {target.name}: {result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"), help="ffmpeg executable")
    parser.add_argument("--force", action="store_true", help="regenerate existing files")
    args = parser.parse_args()
    if not args.ffmpeg:
        print("ffmpeg not found on PATH; pass --ffmpeg PATH", file=sys.stderr)
        return 1

    failures = 0
    for rel, codec_args in VIDEOS.items():
        target = ROOT / rel
        if target.exists() and not args.force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            make_video(args.ffmpeg, target, codec_args)
            print(f"video  {rel} ({target.stat().st_size} bytes)")
        except RuntimeError as exc:
            failures += 1
            print(f"FAILED {rel}: {exc}", file=sys.stderr)

    for rel, data in TEXT_FILES.items():
        target = ROOT / rel
        if target.exists() and not args.force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        print(f"file   {rel}")

    for rel in EMPTY_DIRS:
        (ROOT / rel).mkdir(parents=True, exist_ok=True)
        # Git cannot track empty folders; the scanner must ignore this marker file.
        (ROOT / rel / ".gitkeep").touch()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
