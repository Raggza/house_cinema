"""Tests for plan step 1.1: configuration and library path validation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from source import pipeline
from source.schemas import settings as settings_module
from source.utils.config import ConfigError, describe, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
MOVIES = LIBRARY / "Phim lẻ"
SHOWS = LIBRARY / "Phim bộ"

HUB_ENV_KEYS = ("APP_ENV", "LOG_LEVEL", "HUB_HOST", "HUB_PORT", "HUB_LIBRARY_PATHS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in HUB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def write_config(tmp_path):
    def _write(text: str) -> Path:
        path = tmp_path / "config.yml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def no_env_file(tmp_path) -> Path:
    return tmp_path / "missing.env"


def load(config_file, no_env_file, **kwargs):
    # Always the test index: its folder also holds the admin page's saved folder list (4.2),
    # which must never be the real one.
    environ = {"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"], **kwargs.pop("environ", {})}
    return load_settings(config_file=config_file, env_file=no_env_file, environ=environ, **kwargs)


def _yaml_path(p: Path) -> str:
    return str(p).replace("\\", "/")


# ---------------------------------------------------------------- valid config


def test_valid_config_loads(write_config, no_env_file):
    cfg = write_config(
        f"""
server: {{host: 127.0.0.1, port: 9000}}
logging: {{level: debug}}
library:
  paths: ["{_yaml_path(MOVIES)}", "{_yaml_path(SHOWS)}"]
  video_extensions: [MP4, .mkv, "webm", .mp4]
"""
    )
    s = load(cfg, no_env_file)
    assert s.host == "127.0.0.1"
    assert s.port == 9000
    assert s.log_level == "DEBUG"
    assert s.library_paths == (MOVIES.resolve(), SHOWS.resolve())
    assert s.video_extensions == (".mp4", ".mkv", ".webm")


def test_repo_config_with_cli_path():
    """The shipped config.yml is valid once a library path is given."""
    s = load_settings(environ={}, env_file=Path("does-not-exist.env"), cli_paths=[str(LIBRARY)])
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.library_paths == (LIBRARY.resolve(),)
    assert ".mp4" in s.video_extensions


def test_single_string_path_and_dedup(write_config, no_env_file):
    cfg = write_config(f'library: {{paths: "{_yaml_path(MOVIES)}"}}')
    s = load(cfg, no_env_file, cli_paths=[MOVIES, str(MOVIES) + os.sep])
    assert s.library_paths == (MOVIES.resolve(),)


# ------------------------------------------------------------------ precedence


def test_cli_path_replaces_config_paths(write_config, no_env_file):
    cfg = write_config(f'library: {{paths: ["{_yaml_path(MOVIES)}"]}}')
    s = load(cfg, no_env_file, cli_paths=[str(SHOWS)])
    assert s.library_paths == (SHOWS.resolve(),)


def test_precedence_yaml_envfile_environ_cli(write_config, tmp_path):
    cfg = write_config(
        f'server: {{port: 8001, host: 1.1.1.1}}\nlibrary: {{paths: ["{_yaml_path(MOVIES)}"]}}'
    )
    env_file = tmp_path / ".env"
    env_file.write_text("HUB_PORT=8002\nHUB_HOST=2.2.2.2\nLOG_LEVEL=warning\n", encoding="utf-8")

    s = load_settings(config_file=cfg, env_file=env_file, environ={})
    assert (s.port, s.host, s.log_level) == (8002, "2.2.2.2", "WARNING")

    s = load_settings(config_file=cfg, env_file=env_file, environ={"HUB_PORT": "8003"})
    assert (s.port, s.host) == (8003, "2.2.2.2")

    s = load_settings(
        config_file=cfg, env_file=env_file, environ={"HUB_PORT": "8003"},
        cli_overrides={"port": 8004, "host": None},
    )
    assert (s.port, s.host) == (8004, "2.2.2.2")


def test_env_library_paths_split_by_pathsep(write_config, no_env_file):
    cfg = write_config("library: {paths: []}")
    s = load(cfg, no_env_file, environ={"HUB_LIBRARY_PATHS": f"{MOVIES}{os.pathsep} {SHOWS} "})
    assert s.library_paths == (MOVIES.resolve(), SHOWS.resolve())


def test_empty_env_values_are_ignored(write_config, no_env_file):
    cfg = write_config(f'server: {{port: 8100}}\nlibrary: {{paths: ["{_yaml_path(MOVIES)}"]}}')
    s = load(cfg, no_env_file, environ={"HUB_PORT": "", "HUB_LIBRARY_PATHS": "  "})
    assert s.port == 8100


# --------------------------------------------------------------- invalid paths


def _error_text(exc_info) -> str:
    return "\n".join(exc_info.value.lines)


def test_missing_path_rejected(write_config, no_env_file):
    cfg = write_config("{}")
    bad = LIBRARY / "khong-ton-tai"
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, cli_paths=[str(bad)])
    text = _error_text(exc)
    assert "path does not exist" in text
    assert str(bad) in text
    assert "(from CLI)" in text


def test_file_instead_of_folder_rejected(write_config, no_env_file):
    cfg = write_config("{}")
    a_file = MOVIES / "ghi chú.txt"
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, cli_paths=[str(a_file)])
    assert "path is not a folder" in _error_text(exc)
    assert str(a_file) in _error_text(exc)


def test_unreadable_folder_rejected(write_config, no_env_file, monkeypatch):
    def deny(path):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(settings_module.os, "scandir", deny)
    cfg = write_config("{}")
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, cli_paths=[str(MOVIES)])
    assert "no permission to read folder" in _error_text(exc)


def test_no_library_path_is_allowed(write_config, no_env_file):
    """Step 4.2 changed this: the hub starts without folders (they are added on the admin
    page). `scan` still refuses to run without any (see test_folders.py)."""
    s = load(write_config("library: {paths: []}"), no_env_file)
    assert s.library_paths == () and s.library_source == "config"
    assert "none yet" in describe(s) and "/admin" in describe(s)


def test_overlapping_paths_rejected(write_config, no_env_file):
    cfg = write_config("{}")
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, cli_paths=[str(LIBRARY), str(MOVIES)])
    assert "overlap" in _error_text(exc)


def test_bad_path_reported_with_env_source(write_config, no_env_file):
    cfg = write_config("{}")
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, environ={"HUB_LIBRARY_PATHS": str(LIBRARY / "nope")})
    assert "(from environment / .env)" in _error_text(exc)


# ------------------------------------------------------------ other bad values


@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("server: {port: 0}", "port"),
        ("server: {port: 70000}", "port"),
        ("server: {port: abc}", "port"),
        ("server: {host: '  '}", "host must not be empty"),
        ("logging: {level: LOUD}", "log_level"),
        ("library: {video_extensions: []}", "video_extensions must not be empty"),
        ("library: {video_extensions: ['.']}", "invalid video extension"),
    ],
)
def test_invalid_values_rejected(write_config, no_env_file, yaml_text, expected):
    cfg = write_config(yaml_text)
    with pytest.raises(ConfigError) as exc:
        load(cfg, no_env_file, cli_paths=[str(MOVIES)])
    assert expected in _error_text(exc)


def test_config_file_missing(tmp_path, no_env_file):
    with pytest.raises(ConfigError) as exc:
        load(tmp_path / "nope.yml", no_env_file, cli_paths=[str(MOVIES)])
    assert "config file not found" in _error_text(exc)


@pytest.mark.parametrize("text", ["server: [unclosed", "- just\n- a list"])
def test_config_file_malformed(write_config, no_env_file, text):
    with pytest.raises(ConfigError):
        load(write_config(text), no_env_file, cli_paths=[str(MOVIES)])


def test_settings_are_immutable(write_config, no_env_file):
    s = load(write_config("{}"), no_env_file, cli_paths=[str(MOVIES)])
    with pytest.raises(Exception):
        s.port = 1  # type: ignore[misc]


def test_describe_lists_every_library(write_config, no_env_file):
    s = load(write_config("{}"), no_env_file, cli_paths=[str(MOVIES), str(SHOWS)])
    table = describe(s)
    assert str(MOVIES.resolve()) in table
    assert str(SHOWS.resolve()) in table
    assert "0.0.0.0:8000" in table


# --------------------------------------------------------------------- CLI


def test_main_check_config_ok(capsys, no_env_file):
    code = pipeline.main(["--path", str(MOVIES), "--env-file", str(no_env_file), "--check-config"])
    out = capsys.readouterr().out
    assert code == pipeline.EXIT_OK
    assert "Configuration OK" in out
    assert "Phim lẻ" in out


def test_main_bad_path_exits_nonzero(capsys, no_env_file):
    bad = r"X:\khong-ton-tai"
    code = pipeline.main(["--path", bad, "--env-file", str(no_env_file), "--check-config"])
    err = capsys.readouterr().err
    assert code == pipeline.EXIT_CONFIG_ERROR != 0
    assert err.startswith("Config error: library_paths: path does not exist")
    assert "khong-ton-tai" in err
    assert "Traceback" not in err


def test_cli_process_exit_codes(no_env_file):
    """Run the real entry point the way a user does: python -m source.pipeline."""
    env = {k: v for k, v in os.environ.items() if k not in HUB_ENV_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "source.pipeline", "--env-file", str(no_env_file), *args],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        )

    ok = run("--path", str(MOVIES), "--check-config")
    assert ok.returncode == 0, ok.stderr
    assert "Phim lẻ" in ok.stdout

    bad = run("--path", r"X:\khong-ton-tai", "--check-config")
    assert bad.returncode == pipeline.EXIT_CONFIG_ERROR
    assert "path does not exist" in bad.stderr
    assert "Traceback" not in bad.stderr
