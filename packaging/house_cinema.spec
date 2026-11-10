# PyInstaller spec for the House Cinema desktop app (Windows and macOS).
# Do not run directly: use packaging/build_windows.ps1 or packaging/build_macos.sh, which
# fetch ffmpeg and make the icons first. Environment variables read here:
#   HC_FFMPEG_DIR  folder with ffmpeg + ffprobe (and their licence files) to ship
#   HC_ICON_DIR    folder with icon.ico / icon.icns (default: build/icons)
# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
ICON_DIR = Path(os.environ.get("HC_ICON_DIR") or ROOT / "build" / "icons")
FFMPEG_DIR = os.environ.get("HC_FFMPEG_DIR")

# Read-only resources, at the same place relative to the code as in the source tree.
datas = [
    (str(ROOT / "source" / "web"), "source/web"),
    (str(ROOT / "source" / "configs" / "config.yml"), "source/configs"),
    (str(ROOT / "source" / "configs" / ".env.example"), "source/configs"),
    (str(ROOT / "source" / "image" / "assets"), "source/image/assets"),
]

# ffmpeg / ffprobe go to <bundle>/ffmpeg/ (see source/utils/paths.py: bundled_tool).
binaries = []
if FFMPEG_DIR:
    folder = Path(FFMPEG_DIR)
    exe = ".exe" if IS_WINDOWS else ""
    for name in ("ffmpeg", "ffprobe"):
        path = folder / f"{name}{exe}"
        if not path.is_file():
            raise SystemExit(f"HC_FFMPEG_DIR has no {path.name}: {folder}")
        binaries.append((str(path), "ffmpeg"))
    for extra in folder.iterdir():  # licence / readme files of the ffmpeg build
        if extra.is_file() and extra.suffix.lower() in ("", ".txt", ".md") and extra.stem.lower() not in ("ffmpeg", "ffprobe"):
            datas.append((str(extra), "ffmpeg"))
else:
    print("WARNING: HC_FFMPEG_DIR not set: the app is built WITHOUT ffmpeg", file=sys.stderr)

hiddenimports = (
    collect_submodules("source")
    + collect_submodules("uvicorn")
    + ["pystray._darwin" if IS_MAC else "pystray._win32" if IS_WINDOWS else "pystray._xorg"]
)

a = Analysis(
    [str(ROOT / "packaging" / "house_cinema_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pytest", "playwright", "httpx", "setuptools", "pip"] + (["tkinter"] if IS_MAC else []),
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = None
if IS_WINDOWS and (ICON_DIR / "icon.ico").is_file():
    icon = str(ICON_DIR / "icon.ico")
elif IS_MAC and (ICON_DIR / "icon.icns").is_file():
    icon = str(ICON_DIR / "icon.icns")

version_info = None
if IS_WINDOWS:
    # Name and version shown by Windows (file properties, firewall prompt, Task Manager).
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                     VarFileInfo, VarStruct, VSVersionInfo)

    numbers = [int("".join(c for c in part if c.isdigit()) or 0) for part in VERSION.split(".")[:4]]
    numbers = tuple(numbers + [0] * (4 - len(numbers)))
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "House Cinema"),
                StringStruct("FileDescription", "House Cinema"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "HouseCinema"),
                StringStruct("OriginalFilename", "HouseCinema.exe"),
                StringStruct("ProductName", "House Cinema"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HouseCinema",
    console=False,  # no terminal: the tray icon shows the console window on demand
    icon=icon,
    version=version_info,
    upx=False,  # UPX-packed files are flagged by antivirus more often
)

coll = COLLECT(exe, a.binaries, a.datas, name="HouseCinema", upx=False)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="House Cinema.app",
        icon=icon,
        bundle_identifier="io.github.house-cinema",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "House Cinema",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "LSUIElement": True,  # menu-bar app: no Dock icon
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSLocalNetworkUsageDescription":
                "House Cinema phát video cho điện thoại, máy tính và TV trong cùng mạng.",
        },
    )
