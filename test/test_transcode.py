"""Tests for plan step 5.1: remux / transcode of videos a browser cannot play.

Sample videos (made once per run with ffmpeg, a few seconds):
- 60 s HEVC 10-bit + AAC in MKV (must be transcoded for a browser without HEVC),
- 60 s H.264 + AAC in MKV (remux: picture copied),
- 60 s H.264 + AC-3 in MP4 (remux, sound converted to AAC),
- 10 min tiny H.264 (to see ffmpeg stopped when far ahead of the viewer).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.library import LibraryIndex, scan_library
from source.modules.library.media_info import process_media
from source.modules.server import bind_socket, create_app
from source.modules.transcode import CannotConvert, ClientCaps, decide
from source.modules.transcode.sessions import AHEAD_SEGMENTS, segment_count, vod_playlist
from source.schemas.media import MediaInfo
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(TOOLS.missing != [], reason="ffmpeg/ffprobe needed")
EXTS = (".mp4", ".mkv", ".webm")
BROWSER = {"video": ["h264"], "audio": ["aac", "mp3"]}  # a browser without HEVC / AC-3

HEVC = "Phim/HEVC 10-bit.mkv"
MKV = "Phim/H264.mkv"
AC3 = "Phim/Âm thanh AC3.mp4"
LONG = "Phim/Dài 10 phút.mp4"


def info(**kw) -> MediaInfo:
    base = dict(status="ok", duration=60.0, container="matroska", video_codec="h264", pix_fmt="yuv420p",
                audio_codec="aac", signature="1:1")
    base.update(kw)
    return MediaInfo(**base)


def caps(video=("h264",), audio=("aac",)) -> ClientCaps:
    return ClientCaps(video=frozenset(video), audio=frozenset(audio))


# ======================================================================= decisions


def test_mkv_with_h264_is_remuxed_keeping_the_sound():
    d = decide(info(), caps())
    assert (d.mode, d.copy_video, d.copy_audio) == ("remux", True, True)
    assert d.mime == 'video/mp4; codecs="avc1.640028,mp4a.40.2"' and "MP4" in d.reason


def test_ac3_sound_is_converted_only_when_the_browser_lacks_it():
    d = decide(info(container="mp4", audio_codec="ac3"), caps())
    assert (d.mode, d.copy_video, d.copy_audio) == ("remux", True, False)
    assert "AC3 sang AAC" in d.reason and "mp4a.40.2" in d.mime
    assert decide(info(container="mp4", audio_codec="ac3"), caps(audio=("aac", "ac3"))).copy_audio is True


@pytest.mark.parametrize("kw", [dict(video_codec="hevc", pix_fmt="yuv420p10le"), dict(video_codec="av1"),
                                dict(video_codec="mpeg4"), dict(pix_fmt="yuv420p10le")])
def test_pictures_the_browser_cannot_decode_are_transcoded(kw):
    d = decide(info(**kw), caps())
    assert (d.mode, d.copy_video, d.copy_audio) == ("transcode", False, False)
    assert d.mime == 'video/mp4; codecs="avc1.640028,mp4a.40.2"' and "H.264" in d.reason


def test_hevc_is_copied_for_a_browser_that_decodes_it():
    d = decide(info(video_codec="hevc"), caps(video=("h264", "hevc")))
    assert d.mode == "remux" and "hvc1" in d.mime


@pytest.mark.parametrize("kw,text", [(dict(video_codec=None), "không có hình"), (dict(status="failed"), "chưa đọc được"),
                                     (dict(video_codec="hevc", duration=None), "thời lượng")])
def test_cannot_convert(kw, text):
    with pytest.raises(CannotConvert, match=text):
        decide(info(**kw), caps())


def test_playlist_covers_the_whole_video_in_6_second_pieces():
    text = vod_playlist(60.2)
    # A 0.2 s tail is too short to be a piece of its own: the last piece takes it.
    assert text.count("#EXTINF:6.000000,") == 9 and "#EXTINF:6.200000,\nseg00009.m4s" in text
    assert "seg00010" not in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST") and '#EXT-X-MAP:URI="init.mp4"' in text
    assert segment_count(61) == 11 and segment_count(2.0) == 1
    assert "#EXTINF:1.000000," in vod_playlist(61.0)


# ======================================================================= hub with sample videos


@pytest.fixture(scope="module")
def library(tmp_path_factory) -> Path:
    if TOOLS.missing:
        pytest.skip("ffmpeg/ffprobe needed")
    root = tmp_path_factory.mktemp("tc") / "Thư viện"
    (root / "Phim").mkdir(parents=True)
    src = ["-f", "lavfi", "-i", "testsrc2=duration=60:size=320x180:rate=25",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=60"]
    jobs = {
        HEVC: ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
               "-pix_fmt", "yuv420p10le", "-c:a", "aac"],
        MKV: ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac"],
        AC3: ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "ac3"],
    }
    for rel, codec in jobs.items():
        subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", *src, *codec, "-shortest", str(root / rel)],
                       check=True, timeout=120)
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=duration=600:size=160x90:rate=10",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(root / LONG)],
                   check=True, timeout=120)
    return root


@pytest.fixture(scope="module")
def indexed(library, tmp_path_factory):
    """(index path, {relative path: id}) — scanned once for the module."""
    index = tmp_path_factory.mktemp("idx") / "library.db"
    with LibraryIndex(index) as idx:
        idx.apply_scan(scan_library([library], EXTS, known_files=idx.known_files()))
        process_media(idx, TOOLS, index.parent / "thumbs-cache", workers=2)
        ids = {i.relative_path: i.id for i in idx.items()}
    return index, ids


def hub_settings(library, index, cache, tmp, **env):
    return load_settings(environ={"HUB_INDEX_PATH": str(index), "HUB_CACHE_DIR": str(cache), **env},
                         env_file=tmp / "none.env", cli_paths=[str(library)])


@pytest.fixture
def hub(library, indexed, tmp_path):
    """A TestClient on a fresh cache; ``hub(**env)`` for other settings."""
    index, ids = indexed
    opened = []

    def make(**env):
        app = create_app(hub_settings(library, index, tmp_path / "cache", tmp_path, **env))
        client = TestClient(app, client=("127.0.0.1", 5000))
        client.__enter__()
        opened.append(client)
        return client, app.state.transcoder, ids

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def probe(*files: Path) -> dict:
    joined = files[0].parent / "joined.mp4"
    joined.write_bytes(b"".join(f.read_bytes() for f in files))
    out = subprocess.run([TOOLS.ffprobe, "-v", "error", "-show_entries", "stream=codec_name,start_time",
                          "-of", "json", str(joined)], capture_output=True, text=True, check=True).stdout
    return {s["codec_name"]: float(s["start_time"]) for s in json.loads(out)["streams"]}


def save(tmp: Path, name: str, content: bytes) -> Path:
    path = tmp / name
    path.write_bytes(content)
    return path


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_until(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


# ======================================================================= transcode


@needs_ffmpeg
def test_hevc_is_transcoded_piece_by_piece_and_seeks(hub, tmp_path):
    client, tc, ids = hub()
    r = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["mode"] == "transcode" and plan["session"] and plan["url"] == f"/hls/s/{plan['session']}/index.m3u8"
    base = plan["url"].rsplit("/", 1)[0]
    playlist = client.get(plan["url"])
    assert playlist.headers["content-type"] == "application/vnd.apple.mpegurl"
    assert playlist.text.count("#EXTINF") == 10 and "#EXT-X-ENDLIST" in playlist.text

    first = client.get(f"{base}/seg00000.m4s")
    assert first.status_code == 200
    init = client.get(f"{base}/init.mp4")
    assert init.status_code == 200
    streams = probe(save(tmp_path, "i.mp4", init.content), save(tmp_path, "s0.m4s", first.content))
    # From 0, ffmpeg shifts the picture by its B-frame delay (0.08 s); the player skips it.
    assert set(streams) == {"h264", "aac"} and streams["h264"] == pytest.approx(0, abs=0.1)
    pid_before = tc.running_pids()

    # A seek to 0:42: ffmpeg restarts there instead of converting everything before it.
    started = time.monotonic()
    piece = client.get(f"{base}/seg00007.m4s")
    assert piece.status_code == 200 and time.monotonic() - started < 10
    streams = probe(save(tmp_path, "i.mp4", init.content), save(tmp_path, "s7.m4s", piece.content))
    # On the 6-second grid, with the real time in the piece itself (any init file works).
    assert streams["h264"] == pytest.approx(42, abs=0.1)
    assert tc.running_pids() != pid_before or not pid_before
    assert client.get(f"{base}/seg00099.m4s").status_code == 404  # past the end


@needs_ffmpeg
def test_ffmpeg_stops_when_the_viewer_leaves(hub):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    client.get(plan["url"].replace("index.m3u8", "seg00000.m4s"))
    pids = tc.running_pids()
    assert pids and all(pid_alive(p) for p in pids)
    assert client.post(f"/api/play/sessions/{plan['session']}/stop").status_code == 204
    assert tc.active_count() == 0 and tc.running_pids() == []
    assert wait_until(lambda: not any(pid_alive(p) for p in pids))
    assert list((tc.root / ids[HEVC] / "sessions").glob("*")) == []  # its pieces are deleted
    # A piece request still on its way when the viewer left: no ffmpeg again (step 6.5).
    assert client.get(plan["url"].replace("index.m3u8", "seg00001.m4s")).status_code == 404
    assert tc.active_count() == 0 and tc.running_pids() == []


@needs_ffmpeg
def test_mp4_that_failed_in_the_browser_is_remuxed(hub):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[LONG]}", json={**BROWSER, "avoid_direct": True}).json()
    assert plan["mode"] == "remux"  # not transcoded: the picture is fine


@needs_ffmpeg
def test_idle_session_is_stopped_and_picked_up_again(hub):
    client, tc, ids = hub(HUB_TRANSCODE_IDLE_SECONDS="5")
    plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    base = plan["url"].rsplit("/", 1)[0]
    assert client.get(f"{base}/seg00002.m4s").status_code == 200
    tc.reap(now=time.monotonic() + 6)  # 6 s without any request (closed tab, long pause)
    assert tc.active_count() == 0 and tc.running_pids() == []
    # The paused tab resumes: the same session works again (a new ffmpeg where it is).
    assert client.get(f"{base}/seg00004.m4s").status_code == 200
    assert tc.active_count() == 1


@needs_ffmpeg
def test_ffmpeg_stops_when_far_ahead_and_restarts_on_demand(hub):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[LONG]}", json={"video": [], "audio": []}).json()
    assert plan["mode"] == "transcode"
    base = plan["url"].rsplit("/", 1)[0]
    assert client.get(f"{base}/seg00000.m4s").status_code == 200
    folder = next((tc.root / ids[LONG] / "sessions").iterdir())
    assert wait_until(lambda: len(list(folder.glob("seg*.m4s"))) > AHEAD_SEGMENTS + 2, timeout=60)
    tc.reap()
    assert tc.running_pids() == []  # stopped: no need to convert 2 min ahead of the viewer
    session = tc._sessions[plan["session"]]
    made = session.produced
    assert AHEAD_SEGMENTS < made < session.segments - 5
    nxt = made + 2  # the viewer gets there: ffmpeg starts again, from that piece
    assert client.get(f"{base}/seg{nxt:05d}.m4s").status_code == 200
    assert session.enc_start == nxt and session.error is None


@needs_ffmpeg
def test_too_many_conversions_get_a_clear_busy_answer(hub):
    client, tc, ids = hub(HUB_TRANSCODE_MAX_SESSIONS="1")
    first = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    r = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER)
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    body = r.json()
    assert body["detail"].startswith("Hub đang bận") and (body["active"], body["limit"]) == (1, 1)
    client.post(f"/api/play/sessions/{first['session']}/stop")
    assert client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).status_code == 200  # a free slot again


@needs_ffmpeg
def test_status_for_the_admin(hub):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    client.get(plan["url"].replace("index.m3u8", "seg00000.m4s"))
    data = client.get("/api/admin/transcode").json()
    assert data["active"] == 1 and data["limit"] == 2
    row = data["items"][0]
    assert row["kind"] == "transcode" and row["title"] == "HEVC 10-bit" and row["client"] == "127.0.0.1"


# ======================================================================= remux


@needs_ffmpeg
def test_mkv_is_remuxed_once_and_kept(hub, tmp_path, library, indexed):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[MKV]}", json=BROWSER).json()
    assert plan["mode"] == "remux" and plan["url"] == f"/hls/r/{ids[MKV]}/copy/index.m3u8"
    assert wait_until(lambda: "#EXT-X-ENDLIST" in client.get(plan["url"]).text, timeout=30)
    text = client.get(plan["url"]).text
    assert text.count("#EXTINF") >= 9
    base = plan["url"].rsplit("/", 1)[0]
    streams = probe(save(tmp_path, "i.mp4", client.get(f"{base}/init.mp4").content),
                    save(tmp_path, "s.m4s", client.get(f"{base}/seg00000.m4s").content))
    assert set(streams) == {"h264", "aac"}
    assert wait_until(lambda: tc.running_pids() == [])

    # Another viewer, or after a restart of the hub: served from the cache, no ffmpeg.
    client2, tc2, _ = hub()
    assert client2.post(f"/api/play/{ids[MKV]}", json=BROWSER).json()["url"] == plan["url"]
    assert "#EXT-X-ENDLIST" in client2.get(plan["url"]).text and tc2.running_pids() == []


@needs_ffmpeg
def test_ac3_sound_becomes_aac(hub, tmp_path):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[AC3]}", json=BROWSER).json()
    assert plan["mode"] == "remux" and "/aac/" in plan["url"] and "AC3 sang AAC" in plan["reason"]
    base = plan["url"].rsplit("/", 1)[0]
    client.get(plan["url"])
    streams = probe(save(tmp_path, "i.mp4", client.get(f"{base}/init.mp4").content),
                    save(tmp_path, "s.m4s", client.get(f"{base}/seg00000.m4s").content))
    assert set(streams) == {"h264", "aac"}
    # A browser that plays AC-3 (Edge) plays the file as it is.
    assert client.post(f"/api/play/{ids[AC3]}", json={"video": ["h264"], "audio": ["aac", "ac3"]}).json()["mode"] == "direct"


@needs_ffmpeg
def test_mp4_that_plays_needs_no_conversion(hub):
    client, tc, ids = hub()
    r = client.post(f"/api/play/{ids[LONG]}", json=BROWSER).json()
    assert r == {"mode": "direct", "url": f"/stream/{ids[LONG]}", "mime": None, "session": None, "reason": ""}
    assert tc.running_pids() == []


@needs_ffmpeg
def test_unfinished_remux_is_dropped_and_cache_is_capped(hub):
    client, tc, ids = hub(HUB_TRANSCODE_CACHE_GB="0", HUB_TRANSCODE_IDLE_SECONDS="5")
    plan = client.post(f"/api/play/{ids[MKV]}", json=BROWSER).json()
    assert wait_until(lambda: "#EXT-X-ENDLIST" in client.get(plan["url"]).text, timeout=30)
    folder = tc.root / ids[MKV] / "remux-copy"
    assert folder.is_dir()
    tc.reap(now=time.monotonic() + 100)  # not watched any more, and no cache allowed
    assert not folder.exists()

    leftover = tc.root / ids[AC3] / "remux-aac"  # a remux cut short when the hub stopped
    leftover.mkdir(parents=True)
    (leftover / "meta.json").write_text('{"done": false}')
    hub()  # the next start of the hub cleans it up
    assert not leftover.exists()


@needs_ffmpeg
def test_a_piece_that_cannot_be_opened_yet_is_waited_for(hub, monkeypatch):
    """Right after ffmpeg renames a piece into place, Windows can refuse to open it for a
    moment: the hub waits instead of sending a 200 with a broken body (step 6.2 soak)."""
    from source.modules.server import app as app_module

    real, looks = app_module._can_open, []

    def held_at_first(path):
        looks.append(path.name)
        return len(looks) > 2 and real(path)

    monkeypatch.setattr(app_module, "_can_open", held_at_first)
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    seg = client.get(plan["url"].replace("index.m3u8", "seg00000.m4s"))
    assert seg.status_code == 200 and len(seg.content) > 1000
    assert looks.count("seg00000.m4s") >= 3


def test_reap_sees_a_remux_that_ends_while_it_looks(tmp_path):
    """ffmpeg exits between two looks at it: the job must still be marked done or dropped
    (it was neither, which made the test above flaky under load; found in step 6.2)."""
    from source.modules.transcode.sessions import RemuxJob, TranscodeManager

    class EndsNow:  # running at the first look, gone at the next
        pid, returncode = 1, 0

        def __init__(self):
            self.looks = iter([True])

        def running(self):
            return next(self.looks, False)

        def stop(self, why=""):
            pass

        close = stop

        def error_text(self):
            return ""

    settings = load_settings(env_file=tmp_path / "none.env", cli_paths=[str(PROJECT_ROOT / "test" / "fixtures" / "library")])
    tc = TranscodeManager(settings, FFTools(None, None))
    folder = tc.root / "m1" / "remux-copy"
    folder.mkdir(parents=True)
    job = RemuxJob(media_id="m1", variant="copy", folder=folder, signature="s", encoder=EndsNow())
    tc._remux[("m1", "copy")] = job
    tc.reap(now=time.monotonic() + settings.transcode_idle_seconds + 10)
    dropped = ("m1", "copy") not in tc._remux and not folder.exists()
    assert dropped or job.done


def test_final_playlist_left_in_tmp_by_ffmpeg_is_used(tmp_path):
    """On Windows ffmpeg's last rename of index.m3u8.tmp fails while the hub reads the
    playlist, silently: the hub finishes the job itself (step 6.2)."""
    from source.modules.transcode.sessions import RemuxJob, TranscodeManager

    class Finished:
        pid, returncode = 1, 0

        def running(self):
            return False

        def close(self):
            pass

    settings = load_settings(env_file=tmp_path / "none.env", cli_paths=[str(PROJECT_ROOT / "test" / "fixtures" / "library")])
    tc = TranscodeManager(settings, FFTools(None, None))
    folder = tc.root / "m1" / "remux-copy"
    folder.mkdir(parents=True)
    (folder / "index.m3u8").write_text("#EXTM3U\n#EXTINF:6.0,\nseg00000.m4s\n")
    (folder / "index.m3u8.tmp").write_text("#EXTM3U\n#EXTINF:6.0,\nseg00000.m4s\n#EXTINF:6.0,\nseg00001.m4s\n#EXT-X-ENDLIST\n")
    tc._remux[("m1", "copy")] = RemuxJob(media_id="m1", variant="copy", folder=folder, signature="s", encoder=Finished())
    playlist = tc.remux_file("m1", "copy", "index.m3u8")
    assert b"#EXT-X-ENDLIST" in playlist and b"seg00001" in playlist
    assert not (folder / "index.m3u8.tmp").exists() and tc._remux[("m1", "copy")].done

    # A remux cached by an older hub with the same problem is repaired when loaded.
    cached = tc.root / "m2" / "remux-aac"
    cached.mkdir(parents=True)
    (cached / "meta.json").write_text('{"done": true, "signature": "s"}')
    (cached / "index.m3u8.tmp").write_text("#EXTM3U\n#EXTINF:6.0,\nseg00000.m4s\n#EXT-X-ENDLIST\n")
    assert b"#EXT-X-ENDLIST" in tc.remux_file("m2", "aac", "index.m3u8")


def test_folder_delete_is_retried_when_windows_is_not_done(tmp_path, monkeypatch, caplog):
    """WinError 145 "directory is not empty" right after ffmpeg stops: try again (step 6.2)."""
    from source.modules.transcode import sessions

    folder = tmp_path / "remux-copy"
    folder.mkdir()
    real, calls = shutil.rmtree, []

    def flaky(path, onexc=None):
        calls.append(path)
        if len(calls) < 3:  # the first two tries leave the folder, as Windows does
            onexc(os.rmdir, str(path), OSError(145, "The directory is not empty"))
            return
        real(path)

    monkeypatch.setattr(sessions.shutil, "rmtree", flaky)
    assert sessions._rmtree(folder) is None and not folder.exists() and len(calls) == 3

    # Still in use after the short retry (a piece being sent, the antivirus): the folder is
    # kept aside and tried again by the housekeeping tick, until it goes.
    caplog.set_level("INFO", logger="house_cinema.transcode")
    settings = load_settings(env_file=tmp_path / "none.env", cli_paths=[str(PROJECT_ROOT / "test" / "fixtures" / "library")])
    tc = sessions.TranscodeManager(settings, FFTools(None, None))
    stuck = tc.root / "m1" / "remux-copy"
    stuck.mkdir(parents=True)
    in_use = [True]

    def busy_rmtree(path, onexc=None):
        if in_use[0]:
            onexc(os.rmdir, str(path), OSError(145, "The directory is not empty"))
        else:
            real(path)

    monkeypatch.setattr(sessions.shutil, "rmtree", busy_rmtree)
    monkeypatch.setattr(sessions.time, "sleep", lambda s: None)
    assert sessions._rmtree(stuck) is not None
    tc._delete(stuck)
    assert stuck in tc._trash and "trying again later" in caplog.text
    tc.reap()
    assert stuck.exists() and stuck in tc._trash  # still in use
    in_use[0] = False
    tc.reap()
    assert not stuck.exists() and not tc._trash

    # A folder used again (same remux started anew) leaves the retry list: never deleted later.
    again = tc.root / "m2" / "sessions" / "abc"
    tc._trash.add(again)
    tc._prepare(sessions.TranscodeSession(sid="abc", media_id="m2", title="", source=again, duration=12.0,
                                          has_audio=False, folder=again, client="", segments=2))
    assert again not in tc._trash and again.is_dir()


# ======================================================================= refusals and safety


@needs_ffmpeg
@pytest.mark.parametrize("env,text", [({"HUB_TRANSCODE": "false"}, "đang tắt"), ({"HUB_FFMPEG": ""}, None)])
def test_conversion_unavailable(hub, env, text, monkeypatch):
    if text is None:  # no ffmpeg on this computer
        monkeypatch.setattr("source.modules.server.app.find_tools", lambda s: FFTools(None, TOOLS.ffprobe))
        text = "chưa cài ffmpeg"
    client, tc, ids = hub(**env)
    r = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER)
    assert r.status_code == 409 and text in r.json()["detail"]


@needs_ffmpeg
def test_only_hub_files_are_served(hub):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
    sid = plan["session"]
    for url in (f"/hls/s/{sid}/ffmpeg.log", f"/hls/s/{sid}/run-0.m3u8", f"/hls/s/{sid}/..%2F..%2Fmeta.json",
                f"/hls/s/{'0' * 16}/index.m3u8", "/hls/s/zz/index.m3u8",
                f"/hls/r/{ids[MKV]}/copy/ffmpeg.log", f"/hls/r/{ids[MKV]}/other/index.m3u8",
                f"/hls/r/{'1' * 16}/copy/index.m3u8"):
        assert client.get(url).status_code == 404, url
    assert client.post("/api/play/0123456789abcdef", json=BROWSER).status_code == 404
    assert client.post(f"/api/play/{ids[HEVC]}", json={"video": ["x264; rm"]}).status_code == 422


@needs_ffmpeg
def test_remux_of_a_video_no_longer_in_the_library_is_not_served(hub, library, indexed, tmp_path):
    client, tc, ids = hub()
    plan = client.post(f"/api/play/{ids[MKV]}", json=BROWSER).json()
    assert wait_until(lambda: "#EXT-X-ENDLIST" in client.get(plan["url"]).text, timeout=30)
    other = tmp_path / "Khác"
    other.mkdir()
    index, _ = indexed
    app = create_app(load_settings(environ={"HUB_INDEX_PATH": str(index), "HUB_CACHE_DIR": str(tmp_path / "cache")},
                                   env_file=tmp_path / "none.env", cli_paths=[str(other)]))
    with TestClient(app) as c:  # same cache, but that library folder is not shared any more
        assert c.get(plan["url"]).status_code == 404


@needs_ffmpeg
def test_hub_stop_ends_every_ffmpeg(library, indexed, tmp_path):
    index, ids = indexed
    app = create_app(hub_settings(library, index, tmp_path / "cache", tmp_path))
    with TestClient(app) as client:
        plan = client.post(f"/api/play/{ids[HEVC]}", json=BROWSER).json()
        client.get(plan["url"].replace("index.m3u8", "seg00000.m4s"))
        pids = app.state.transcoder.running_pids()
        assert pids
    assert wait_until(lambda: not any(pid_alive(p) for p in pids))


@needs_ffmpeg
@pytest.mark.skipif(os.name != "nt", reason="Job Object is Windows only")
def test_ffmpeg_dies_with_a_crashed_hub(library, indexed, tmp_path):
    """Kill -9 of the hub process: Windows ends its ffmpeg too (Job Object)."""
    index, ids = indexed
    script = tmp_path / "hub.py"
    script.write_text(f"""
import sys, time
sys.path.insert(0, {str(PROJECT_ROOT)!r})
from pathlib import Path
from source.modules.transcode.process import Encoder
e = Encoder([{TOOLS.ffmpeg!r}, "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=duration=600", "-f", "null", "-"],
            Path({str(tmp_path / 'ff.log')!r}), label="test")
print(e.pid, flush=True)
time.sleep(600)
""", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        pid = int(proc.stdout.readline())
        assert pid_alive(pid)
    finally:
        proc.kill()  # like a crash: no cleanup code runs
        proc.wait(timeout=10)
    assert wait_until(lambda: not pid_alive(pid), timeout=10)


# ======================================================================= in the browser (Edge)


@contextlib.contextmanager
def live_hub(library, index, tmp, **env):
    app = create_app(hub_settings(library, index, tmp / "cache", tmp, **env))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", app.state.transcoder
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        except Exception:
            pytest.skip("Microsoft Edge is needed (H.264)")
        yield b
        b.close()


# Pretend to be a browser without MKV / HEVC / AC-3 (like Firefox or an older phone).
LIMITED = """(() => {
  const no = (t) => /matroska|hvc1|hev1|ac-3|ec-3/.test(String(t));
  const cpt = HTMLMediaElement.prototype.canPlayType;
  HTMLMediaElement.prototype.canPlayType = function (t) { return no(t) ? "" : cpt.call(this, t); };
  const its = MediaSource.isTypeSupported.bind(MediaSource);
  MediaSource.isTypeSupported = (t) => !no(t) && its(t);
})();"""


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg
    ctx.close()
    assert errors == [], errors


def plays_with_picture(page, at_least: float) -> None:
    page.wait_for_function(f"""() => {{
        const v = document.querySelector('#video');
        const q = v.getVideoPlaybackQuality();
        return v.currentTime > {at_least} && v.videoWidth > 0 && q.totalVideoFrames > 0 && !v.paused;
    }}""", timeout=60000)


@needs_ffmpeg
@pytest.mark.parametrize("rel,limited,mode", [(HEVC, True, "transcode"), (MKV, True, "remux"), (AC3, True, "remux")])
def test_converted_video_plays_and_seeks_in_the_browser(page, library, indexed, tmp_path, rel, limited, mode):
    index, ids = indexed
    plans = []
    page.on("response", lambda r: "/api/play/" in r.url and r.request.method == "POST" and plans.append(r.json()))
    if limited:
        page.add_init_script(LIMITED)
    with live_hub(library, index, tmp_path) as (base, tc):
        page.goto(f"{base}/watch/{ids[rel]}")
        plays_with_picture(page, 0.5)
        assert plans and plans[-1]["mode"] == mode
        page.wait_for_selector("#notice", state="hidden")
        page.evaluate("document.querySelector('#video').currentTime = 40")
        page.wait_for_function("document.querySelector('#video').currentTime >= 40", timeout=30000)
        plays_with_picture(page, 40.5)
        assert page.evaluate("document.querySelector('#video').duration") == pytest.approx(60, abs=1)
        sound = page.evaluate("document.querySelector('#video').webkitAudioDecodedByteCount")
        assert sound is None or sound > 0  # AC-3 → AAC: there is sound now


@needs_ffmpeg
def test_hevc_with_sound_only_switches_to_conversion(page, library, indexed, tmp_path):
    """Edge says it can play HEVC but shows no picture (no HEVC extension): the page notices
    and asks the hub for an H.264 version, without the user doing anything."""
    index, ids = indexed
    with live_hub(library, index, tmp_path) as (base, tc):
        page.goto(f"{base}/watch/{ids[HEVC]}")
        plays_with_picture(page, 0.5)


@needs_ffmpeg
def test_closing_the_tab_stops_ffmpeg(browser, library, indexed, tmp_path):
    index, ids = indexed
    with live_hub(library, index, tmp_path) as (base, tc):
        ctx = browser.new_context()
        page = ctx.new_page()
        page.add_init_script(LIMITED)
        page.goto(f"{base}/watch/{ids[HEVC]}")
        plays_with_picture(page, 0.5)
        pids = tc.running_pids()
        assert tc.active_count() == 1
        page.close()  # pagehide → the session is ended at once
        assert wait_until(lambda: tc.active_count() == 0, timeout=10)
        assert wait_until(lambda: not any(pid_alive(p) for p in pids))
        ctx.close()


@needs_ffmpeg
def test_busy_hub_says_so(page, library, indexed, tmp_path):
    index, ids = indexed
    page.add_init_script(LIMITED)
    with live_hub(library, index, tmp_path, HUB_TRANSCODE_MAX_SESSIONS="1") as (base, tc):
        import urllib.request
        req = urllib.request.Request(f"{base}/api/play/{ids[HEVC]}", data=json.dumps(BROWSER).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req).read()  # another viewer takes the only slot
        page.goto(f"{base}/watch/{ids[HEVC]}")
        notice = page.locator("#notice")
        notice.locator("h2", has_text="Hub đang bận").wait_for(timeout=15000)
        assert "tối đa 1" in notice.inner_text()
        assert notice.get_by_role("button", name="Thử lại").is_visible()
        assert notice.get_by_role("button", name="Sao chép link cho VLC").is_visible()


@needs_ffmpeg
def test_resume_on_a_converted_video(page, library, indexed, tmp_path):
    index, ids = indexed
    page.add_init_script(LIMITED)
    with live_hub(library, index, tmp_path) as (base, tc):
        page.goto(f"{base}/watch/{ids[HEVC]}")
        plays_with_picture(page, 0.5)
        page.evaluate("document.querySelector('#video').currentTime = 30")
        plays_with_picture(page, 30.5)
        page.evaluate("document.querySelector('#video').pause()")
        page.goto("about:blank")
        page.goto(f"{base}/watch/{ids[HEVC]}")
        plays_with_picture(page, 30)
        assert page.evaluate("document.querySelector('#video').currentTime") < 40
        assert re.search(r"Đang phát tiếp từ 0:3[01]", page.locator("#resume").inner_text())
