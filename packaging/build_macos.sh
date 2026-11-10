#!/usr/bin/env bash
# Build the macOS app and disk image: dist/HouseCinema-<version>-macOS-<arch>.dmg
#
#   bash packaging/build_macos.sh                       # on a Mac (Apple Silicon or Intel)
#   HC_FFMPEG_DIR=/path/to/ffmpeg bash packaging/build_macos.sh
#
# Must run ON a Mac: PyInstaller cannot build a Mac app from Windows. The .dmg is made for
# the Mac's own architecture (arm64 = Apple Silicon, x86_64 = Intel); build on each kind,
# or use the GitHub Actions workflow (.github/workflows/release.yml).
# Needs: uv (curl -LsSf https://astral.sh/uv/install.sh | sh), Internet on the first run.
#   1. ffmpeg + ffprobe: static builds by Martin Riedl (GPL, signed and notarized),
#      kept in build/ffmpeg-macos-<arch>; or HC_FFMPEG_DIR with your own static build
#   2. icons, 3. PyInstaller -> dist/House Cinema.app, 4. hdiutil -> .dmg
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ARCH="$(uname -m)"                                   # arm64 | x86_64
case "$ARCH" in
  arm64) FF_ARCH=arm64 ;;
  x86_64) FF_ARCH=amd64 ;;
  *) echo "unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
VERSION="$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -n 1)"
echo "House Cinema $VERSION ($ARCH)"

command -v uv >/dev/null 2>&1 || { echo "uv not found: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }

# ------------------------------------------------------------------------- 1. ffmpeg
FFMPEG_DIR="${HC_FFMPEG_DIR:-$ROOT/build/ffmpeg-macos-$ARCH}"
if [ ! -x "$FFMPEG_DIR/ffmpeg" ] || [ ! -x "$FFMPEG_DIR/ffprobe" ]; then
  if [ -n "${HC_FFMPEG_DIR:-}" ]; then
    echo "HC_FFMPEG_DIR has no executable ffmpeg and ffprobe: $FFMPEG_DIR" >&2; exit 1
  fi
  echo "==> Downloading ffmpeg ($FF_ARCH)"
  mkdir -p "$FFMPEG_DIR" "$ROOT/build/download"
  for tool in ffmpeg ffprobe; do
    zip="$ROOT/build/download/$tool-macos-$FF_ARCH.zip"
    curl -fL --retry 3 -o "$zip" "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$FF_ARCH/release/$tool.zip"
    unzip -o -q "$zip" -d "$FFMPEG_DIR"
    chmod +x "$FFMPEG_DIR/$tool"
  done
  cat > "$FFMPEG_DIR/SOURCE.txt" <<EOF
ffmpeg and ffprobe in this folder: static builds by Martin Riedl (https://ffmpeg.martin-riedl.de/).
Licence: GNU GPL (the build includes GPL libraries such as x264). Source code:
https://ffmpeg.org/download.html and the build scripts at https://git.martin-riedl.de/ffmpeg/build-script
EOF
fi
"$FFMPEG_DIR/ffmpeg" -hide_banner -version | head -n 1

# ------------------------------------------------------------------------- 2. icons
echo "==> Icons"
uv run --group desktop python packaging/make_icons.py build/icons

# ------------------------------------------------------------------------- 3. PyInstaller
echo "==> PyInstaller"
HC_FFMPEG_DIR="$FFMPEG_DIR" HC_ICON_DIR="$ROOT/build/icons" \
  uv run --group desktop --group packaging pyinstaller --noconfirm --clean \
  --distpath dist --workpath build/pyinstaller packaging/house_cinema.spec
APP="dist/House Cinema.app"
test -d "$APP"

# ------------------------------------------------------------------------- 4. disk image
echo "==> Disk image"
STAGE="build/dmg"
rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="dist/HouseCinema-$VERSION-macOS-$ARCH.dmg"
rm -f "$DMG"
hdiutil create -volname "House Cinema" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
echo "Done: $DMG"
