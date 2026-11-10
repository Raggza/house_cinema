"""Tests for the terminal progress line used by `scan`."""

from __future__ import annotations

import functools
import io
import logging
import os
import sys
from pathlib import Path

import pytest

from source import pipeline
from source.modules.library import ScanProgress, scan_library
from source.modules.library.report import format_progress
from source.utils.progress import ProgressLine, shorten_left, stream_is_terminal

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
VIDEO_EXTS = (".mp4", ".mkv", ".webm")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make_line(enabled=True, width=60, **kwargs):
    stream, clock = io.StringIO(), FakeClock()
    line = ProgressLine(stream, enabled=enabled, width=width, clock=clock, **kwargs)
    return line, stream, clock


def last_frame(stream: io.StringIO) -> str:
    return stream.getvalue().split("\r")[-1]


# ------------------------------------------------------------- scanner events


def test_scanner_reports_progress(tmp_path):
    for i in range(3):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / "a.mp4").write_bytes(b"x")
    events: list[ScanProgress] = []
    result = scan_library([tmp_path], VIDEO_EXTS, on_progress=events.append)

    assert events, "callback was never called"
    final = events[-1]
    assert final.videos_found == result.report.found == 3
    assert final.folders_scanned == 4  # root + 3 sub-folders
    assert final.current_folder == ""
    counts = [e.videos_found for e in events]
    assert counts == sorted(counts), "video count must never go down"
    assert {e.current_folder for e in events[:-1]} >= {str(tmp_path / "d0"), str(tmp_path)}


def test_scanner_reports_inside_large_folder(tmp_path, monkeypatch):
    from source.modules.library import scanner

    monkeypatch.setattr(scanner, "PROGRESS_EVERY_ENTRIES", 5)
    for i in range(12):
        (tmp_path / f"{i}.mp4").write_bytes(b"x")
    events: list[ScanProgress] = []
    scan_library([tmp_path], VIDEO_EXTS, on_progress=events.append)
    # start of folder, after 5 and 10 entries, end of scan
    assert [e.videos_found for e in events] == [0, 5, 10, 12]


def test_scanner_counts_errors_in_progress(tmp_path, monkeypatch):
    from source.modules.library import scanner

    (tmp_path / "locked").mkdir()
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(scanner.os, "scandir", fake_scandir)
    events: list[ScanProgress] = []
    scan_library([tmp_path], VIDEO_EXTS, on_progress=events.append)
    assert events[-1].errors == 1
    assert events[-1].folders_scanned == 2


def test_no_callback_is_fine():
    assert scan_library([LIBRARY], VIDEO_EXTS, on_progress=None).report.found == 6


# ------------------------------------------------------------------- formatting


def test_format_progress():
    p = ScanProgress(folders_scanned=86, videos_found=1240, skipped=3, errors=0, current_folder="x")
    assert format_progress(p) == "Scanning… 1240 videos · 86 folders"
    p = ScanProgress(folders_scanned=1, videos_found=0, skipped=0, errors=2, current_folder="x")
    assert format_progress(p).endswith(" · 2 errors")


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [("abcdef", 10, "abcdef"), ("abcdef", 4, "…def"), ("abcdef", 1, "…"), ("abc", 0, "")],
)
def test_shorten_left(text, width, expected):
    assert shorten_left(text, width) == expected


# ------------------------------------------------------------------ ProgressLine


def test_disabled_line_writes_nothing():
    line, stream, clock = make_line(enabled=False)
    clock.now = 10
    with line:
        line.update("Scanning…", tail="E:\\Movies")
    assert stream.getvalue() == ""


def test_start_delay_avoids_flicker_on_fast_scans():
    line, stream, clock = make_line(start_delay=0.5)
    clock.now = 0.3
    line.update("early")
    assert stream.getvalue() == ""
    clock.now = 0.6
    line.update("late")
    assert last_frame(stream) == "late"


def test_redraws_are_throttled():
    line, stream, clock = make_line(start_delay=0, min_interval=0.1)
    line.update("one")
    clock.now = 0.05
    line.update("two")
    assert last_frame(stream) == "one"
    clock.now = 0.2
    line.update("three")
    assert last_frame(stream).rstrip() == "three"


def test_line_fits_width_and_keeps_end_of_path():
    line, stream, clock = make_line(start_delay=0, width=40)
    folder = r"F:\video\ririsu\2024\Một thư mục rất dài\Tập cuối"
    line.update("Scanning… 12 videos · 3 folders", tail=folder)
    frame = last_frame(stream)
    assert len(frame) <= 40
    assert frame.startswith("Scanning… 12 videos · 3 folders")
    # 40 - 31 (text) - 3 (separator) = 6 chars left: below the 8-char minimum, so no tail
    assert "…" not in frame[len("Scanning…"):]

    line2, stream2, _ = make_line(start_delay=0, width=60)
    line2.update("Scanning… 12 videos", tail=folder)
    frame2 = last_frame(stream2)
    assert len(frame2) == 60
    assert frame2.endswith("Tập cuối")
    assert "…" in frame2


def test_shorter_frame_erases_leftovers():
    line, stream, clock = make_line(start_delay=0, min_interval=0)
    line.update("a long first frame")
    line.update("short")
    frame = last_frame(stream)
    assert frame.startswith("short")
    assert len(frame) == len("a long first frame")


def test_exit_clears_the_line():
    line, stream, clock = make_line(start_delay=0)
    with line:
        line.update("Scanning… 3 videos")
    assert stream.getvalue().endswith("\r" + " " * len("Scanning… 3 videos") + "\r")


def test_log_records_clear_the_line_first():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        clock = FakeClock()
        with ProgressLine(stream, enabled=True, start_delay=0, width=60, clock=clock) as line:
            line.update("Scanning… 1 videos")
            logging.getLogger("house_cinema.test").warning("Cannot read X")
            # the warning starts on a clean line, not glued to the progress text
            assert "\r" + " " * len("Scanning… 1 videos") + "\rCannot read X\n" in stream.getvalue()
            clock.now = 1
            line.update("Scanning… 2 videos")
            assert last_frame(stream).startswith("Scanning… 2 videos")
        assert not handler.filters, "filter must be removed on exit"
    finally:
        root.removeHandler(handler)


# --------------------------------------------------------------- detection


def test_stringio_is_not_a_terminal():
    assert stream_is_terminal(io.StringIO(), environ={}) is False


@pytest.mark.skipif(os.name != "nt", reason="Git Bash detection is Windows-only")
def test_git_bash_pipe_counts_as_terminal(tmp_path):
    read_fd, write_fd = os.pipe()
    try:
        with os.fdopen(write_fd, "w", closefd=False) as pipe_stream:
            assert stream_is_terminal(pipe_stream, environ={"MSYSTEM": "MINGW64"}) is True
            assert stream_is_terminal(pipe_stream, environ={}) is False
    finally:
        os.close(read_fd)
        os.close(write_fd)
    with open(tmp_path / "log.txt", "w") as file_stream:  # `2> log.txt` in Git Bash
        assert stream_is_terminal(file_stream, environ={"MSYSTEM": "MINGW64"}) is False


# ------------------------------------------------------------------------ CLI


def _scan(capsys, tmp_path, *extra):
    code = pipeline.main(
        ["scan", "--dry-run", "--path", str(LIBRARY), "--env-file", str(tmp_path / "none.env"), *extra]
    )
    return code, capsys.readouterr()


def test_cli_no_progress_when_not_a_terminal(capsys, tmp_path):
    code, captured = _scan(capsys, tmp_path)
    assert code == 0
    assert "Scanning…" not in captured.err
    assert "\r" not in captured.out


def test_cli_progress_forced_goes_to_stderr_only(capsys, tmp_path, monkeypatch):
    # The fixture scan is too fast to pass the start delay; drop the delay and throttle.
    monkeypatch.setattr(
        pipeline, "ProgressLine", functools.partial(ProgressLine, start_delay=0, min_interval=0)
    )
    code, captured = _scan(capsys, tmp_path, "--progress")
    assert code == 0
    assert "Scanning…" in captured.err
    # After the last frame the line is erased ("\r   \r"), and anything logged afterwards
    # (the "Library scan: ..." summary) starts on a clean line.
    after_last_frame = captured.err.rsplit("Scanning…", 1)[1]
    erased = after_last_frame.split("\r")
    assert len(erased) >= 3 and erased[1].strip() == ""
    assert erased[-1] == "" or erased[-1].startswith(("20", "Library scan"))
    assert "Scanning…" not in captured.out
    assert "6 found · 1 skipped · 0 errors" in captured.out
