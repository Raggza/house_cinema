"""Shared test fixtures."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _isolated_index(tmp_path, monkeypatch):
    """Never let a test touch the real data/library.db."""
    path = tmp_path / "index" / "library.db"
    monkeypatch.setenv("HUB_INDEX_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Never let a test write thumbnails into the real cache/ folder."""
    path = tmp_path / "cache"
    monkeypatch.setenv("HUB_CACHE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """pipeline.main() reconfigures the root logger (basicConfig(force=True)) with a handler
    on the test's captured stderr, which pytest closes after the test. Restore the original
    handlers so later tests do not log into a closed stream."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
