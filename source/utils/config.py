"""Load Settings from config.yml, .env, the process environment and CLI overrides.

Precedence (later wins): config.yml < .env file < process environment < CLI.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import ValidationError

from source.modules.library.folders import read_saved_paths, store_path_for
from source.utils.ffmpeg import disabled_features, find_tools
from source.utils.paths import config_dir
from source.schemas.settings import (
    DEFAULT_INDEX_PATH,
    KNOWN_ROOTS_CONTEXT,
    SAVED_PATHS_CONTEXT,
    Settings,
    resolve_index_path,
)

KnownRootsReader = Callable[[Path], Iterable[str]]
"""Given the index path, return library folders already indexed (see Settings)."""

# source/configs from source; the per-user configs folder in the packaged app.
CONFIG_DIR = config_dir()
DEFAULT_CONFIG_FILE = CONFIG_DIR / "config.yml"
DEFAULT_ENV_FILE = CONFIG_DIR / ".env"

# Environment variable -> Settings field.
ENV_KEYS: dict[str, str] = {
    "APP_ENV": "app_env",
    "LOG_LEVEL": "log_level",
    "HUB_HOST": "host",
    "HUB_PORT": "port",
    "HUB_LIBRARY_PATHS": "library_paths",
    "HUB_INDEX_PATH": "index_path",
    "HUB_CACHE_DIR": "cache_dir",
    "HUB_FFMPEG": "ffmpeg_path",
    "HUB_FFPROBE": "ffprobe_path",
    "HUB_MEDIA_WORKERS": "media_workers",
    "HUB_TRANSCODE": "transcode_enabled",
    "HUB_TRANSCODE_MAX_SESSIONS": "transcode_max_sessions",
    "HUB_TRANSCODE_ENCODER": "transcode_video_encoder",
    "HUB_TRANSCODE_MAX_HEIGHT": "transcode_max_height",
    "HUB_TRANSCODE_IDLE_SECONDS": "transcode_idle_seconds",
    "HUB_TRANSCODE_CACHE_GB": "transcode_cache_gb",
    "HUB_PIN": "access_pin",
    "HUB_LAN_ONLY": "access_lan_only",
    "HUB_PIN_MAX_ATTEMPTS": "pin_max_attempts",
    "HUB_PIN_LOCKOUT_MINUTES": "pin_lockout_minutes",
}

_SOURCE_LABELS = {
    "yml": "config.yml",
    "env": "environment / .env",
    "cli": "CLI",
    "saved": "the admin page (library-paths.json)",
}
_LIBRARY_SOURCE = {"cli": "--path (this run only)", "saved": "admin page", "config": "config.yml / .env"}


class ConfigError(Exception):
    """Configuration is missing or invalid. ``lines`` holds one readable line per problem."""

    def __init__(self, lines: Sequence[str]):
        self.lines = list(lines)
        super().__init__("; ".join(self.lines))


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError([f"config file not found: {path}"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError([f"config file is not valid YAML: {path} ({exc})"]) from None
    if not isinstance(data, dict):
        raise ConfigError([f"config file must contain a mapping at the top level: {path}"])
    return data


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name) or {}
    if not isinstance(value, Mapping):
        raise ConfigError([f"config.yml section '{name}' must be a mapping"])
    return value


def _from_yaml(data: Mapping[str, Any]) -> dict[str, Any]:
    project = _section(data, "project")
    logging_ = _section(data, "logging")
    server = _section(data, "server")
    library = _section(data, "library")
    media = _section(data, "media")
    transcode = _section(data, "transcode")
    access = _section(data, "access")
    if "pin" in access:
        raise ConfigError([f"config.yml: put the PIN in {DEFAULT_ENV_FILE} (HUB_PIN=...), not in config.yml"])
    values = {
        "access_lan_only": access.get("lan_only"),
        "pin_max_attempts": access.get("pin_max_attempts"),
        "pin_lockout_minutes": access.get("pin_lockout_minutes"),
        "pin_remember_days": access.get("pin_remember_days"),
        "transcode_enabled": transcode.get("enabled"),
        "transcode_max_sessions": transcode.get("max_sessions"),
        "transcode_video_encoder": transcode.get("video_encoder"),
        "transcode_max_height": transcode.get("max_height"),
        "transcode_idle_seconds": transcode.get("idle_seconds"),
        "transcode_cache_gb": transcode.get("cache_gb"),
        "app_name": project.get("name"),
        "version": project.get("version"),
        "log_level": logging_.get("level"),
        "host": server.get("host"),
        "port": server.get("port"),
        "library_paths": library.get("paths"),
        "video_extensions": library.get("video_extensions"),
        "index_path": library.get("index_path"),
        "cache_dir": media.get("cache_dir"),
        "ffmpeg_path": media.get("ffmpeg_path"),
        "ffprobe_path": media.get("ffprobe_path"),
        "media_workers": media.get("workers"),
    }
    return {k: v for k, v in values.items() if v is not None}


def _from_env(env: Mapping[str, str | None]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, field in ENV_KEYS.items():
        raw = env.get(key)
        if raw is None or raw.strip() == "":
            continue
        if field == "library_paths":
            values[field] = [p.strip() for p in raw.split(os.pathsep) if p.strip()]
        else:
            values[field] = raw.strip()
    return values


def _format_errors(exc: ValidationError, sources: Mapping[str, str]) -> list[str]:
    lines = []
    for err in exc.errors():
        field = str(err["loc"][0]) if err["loc"] else "settings"
        msg = err["msg"].removeprefix("Value error, ")
        source = sources.get(field)
        suffix = f" (from {_SOURCE_LABELS[source]})" if source else ""
        lines.append(f"{field}: {msg}{suffix}")
    return lines


def load_settings(
    *,
    config_file: Path | None = None,
    env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_paths: Sequence[str | Path] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    known_roots_reader: KnownRootsReader | None = None,
) -> Settings:
    """Build and validate Settings. Raises ConfigError with readable lines on failure.

    ``cli_paths`` replaces (does not extend) library paths from other sources.

    ``known_roots_reader`` (optional) returns the library folders already in the index;
    those may be missing (drive unplugged) without failing validation. Without it every
    library folder must exist.
    """
    config_file = config_file or DEFAULT_CONFIG_FILE
    env_file = env_file or DEFAULT_ENV_FILE
    environ = os.environ if environ is None else environ

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}

    def apply(values: Mapping[str, Any], source: str) -> None:
        for key, value in values.items():
            merged[key] = value
            sources[key] = source

    apply(_from_yaml(_read_yaml(config_file)), "yml")
    if env_file.is_file():
        apply(_from_env(dotenv_values(env_file)), "env")
    apply(_from_env(environ), "env")
    if cli_overrides:
        apply({k: v for k, v in cli_overrides.items() if v is not None}, "cli")

    # Library folders (step 4.2): --path, else the admin page's saved list, else config/env.
    context: dict[str, Any] = {}
    if cli_paths:
        apply({"library_paths": list(cli_paths), "library_source": "cli"}, "cli")
    else:
        try:
            index_path = resolve_index_path(merged.get("index_path") or DEFAULT_INDEX_PATH)
            saved = read_saved_paths(store_path_for(index_path))
        except (OSError, ValueError, TypeError):
            saved = None  # an invalid index_path is reported by validation below
        if saved is not None:
            apply({"library_paths": saved, "library_source": "saved"}, "saved")
            context[SAVED_PATHS_CONTEXT] = True
        else:
            merged["library_source"] = "config"

    if known_roots_reader is not None:
        try:
            index_path = resolve_index_path(merged.get("index_path") or DEFAULT_INDEX_PATH)
            context[KNOWN_ROOTS_CONTEXT] = list(known_roots_reader(index_path))
        except (OSError, ValueError, TypeError):
            pass  # an invalid index_path is reported by validation below

    try:
        return Settings.model_validate(merged, context=context)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, sources)) from None


def describe(settings: Settings) -> str:
    """Return a human-readable table of the effective configuration."""
    rows: list[tuple[str, str]] = [
        ("app", f"{settings.app_name} {settings.version} ({settings.app_env})"),
        ("listen", f"{settings.host}:{settings.port}"),
        ("log level", settings.log_level),
    ]
    for i, path in enumerate(settings.library_paths):
        note = "" if path.exists() else "  (not connected)"
        rows.append(("library" if i == 0 else "", f"{path}{note}"))
    if not settings.library_paths:
        rows.append(("library", f"(none yet: add folders at http://localhost:{settings.port}/admin "
                                "on this computer)"))
    rows.append(("folders from", _LIBRARY_SOURCE[settings.library_source]))
    rows.append(("extensions", " ".join(settings.video_extensions)))
    rows.append(("index", str(settings.index_path)))
    rows.append(("cache", str(settings.cache_dir)))
    tools = find_tools(settings)
    rows.append(("ffmpeg", tools.ffmpeg or f"NOT FOUND ({settings.ffmpeg_path or 'not on PATH'})"))
    rows.append(("ffprobe", tools.ffprobe or f"NOT FOUND ({settings.ffprobe_path or 'not next to ffmpeg, not on PATH'})"))
    for feature in disabled_features(tools):
        rows.append(("off", feature))
    rows.append(("access", ("PIN on (other devices; the PIN itself is not shown)" if settings.access_pin
                            else "no PIN") + (", LAN devices only" if settings.access_lan_only
                                              else ", any IP address (lan_only off)")))
    rows.append(("convert",(f"on: {settings.transcode_video_encoder}, at most {settings.transcode_max_sessions} "
                             f"at a time, ≤{settings.transcode_max_height}p, keeps {settings.transcode_cache_gb:g} GB "
                             "of remuxed copies") if settings.transcode_enabled else "off"))
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k.ljust(width)} | {v}" for k, v in rows)
