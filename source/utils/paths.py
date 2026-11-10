"""Where the hub keeps its files, from the source folder or from the packaged app.

- From source (``run.cmd``, ``python -m source.pipeline``): everything stays in the project
  folder, as before: ``data/``, ``cache/``, ``source/configs/``.
- Packaged app (PyInstaller, ``sys.frozen``): the program folder may be read-only
  (``C:\\Program Files``, a signed macOS ``.app``), so what the hub writes and what the user
  edits lives in a per-user folder:
    Windows  %LOCALAPPDATA%\\HouseCinema
    macOS    ~/Library/Application Support/HouseCinema
    Linux    $XDG_DATA_HOME/house-cinema (~/.local/share/house-cinema)
  The environment variable HOUSE_CINEMA_HOME overrides this folder in both cases.

Read-only resources (web pages, default config, images) stay next to the code: PyInstaller
puts them at the same relative place under ``sys._MEIPASS``, so ``Path(__file__)`` finds them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_FOLDER_NAME = "HouseCinema"
HOME_ENV = "HOUSE_CINEMA_HOME"

FROZEN = bool(getattr(sys, "frozen", False))
"""True in the packaged app (PyInstaller)."""

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""The source checkout; inside the packaged app, the folder holding the bundled ``source``."""

BUNDLED_CONFIG_DIR = PROJECT_ROOT / "source" / "configs"
"""config.yml and .env.example shipped with the code (read-only in the packaged app)."""


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_FOLDER_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "house-cinema"


def data_home() -> Path:
    """Base of relative data paths (index_path, cache_dir): the project folder from source,
    the per-user folder in the packaged app; HOUSE_CINEMA_HOME overrides both."""
    override = os.environ.get(HOME_ENV, "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser().resolve()
    return _user_data_dir() if FROZEN else PROJECT_ROOT


def config_dir() -> Path:
    """Folder of the config.yml and .env the hub reads by default."""
    override = os.environ.get(HOME_ENV, "").strip()
    if FROZEN or override:
        return data_home() / "configs"
    return BUNDLED_CONFIG_DIR


def ensure_user_config() -> Path:
    """Packaged app / HOUSE_CINEMA_HOME: on first start, copy the shipped config.yml and
    .env.example to the user's config folder so they can be edited. Existing files are
    never overwritten. Returns the config folder. From source this changes nothing."""
    folder = config_dir()
    if folder == BUNDLED_CONFIG_DIR:
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("config.yml", ".env.example"):
        target, source = folder / name, BUNDLED_CONFIG_DIR / name
        if not target.exists() and source.is_file():
            target.write_bytes(source.read_bytes())
    return folder


def bundled_tool(name: str) -> str | None:
    """ffmpeg / ffprobe shipped inside the packaged app (``<bundle>/ffmpeg/``), else None."""
    if not FROZEN:
        return None
    exe = f"{name}.exe" if os.name == "nt" else name
    path = PROJECT_ROOT / "ffmpeg" / exe
    return str(path) if path.is_file() else None
