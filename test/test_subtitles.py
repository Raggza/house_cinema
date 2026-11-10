"""Tests for plan step 3.3: subtitles (.srt/.vtt next to the video → WebVTT, UTF-8)."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from source import pipeline
from source.modules.library import LibraryIndex, scan_library
from source.modules.server import create_app
from source.modules.subtitles import SubtitleError, find_subtitles, load_as_vtt
from source.modules.subtitles.convert import SubtitleFormatError, normalize_vtt, srt_to_vtt
from source.modules.subtitles.encoding import SubtitleEncodingError, decode_subtitle
from source.modules.subtitles.languages import parse_language
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
sys.path.insert(0, str(PROJECT_ROOT / "test" / "fixtures"))
from make_library import SRT_VI, encode_cp1258  # noqa: E402

VIDEO_EXTS = (".mp4", ".mkv", ".webm")
IS_WINDOWS = sys.platform == "win32"

# Longer, realistic texts for encoding detection.
VI_TEXT = ("1\n00:00:01,000 --> 00:00:04,000\nNgười ta nói rằng mỗi chuyến đi đều bắt đầu từ một bước chân.\n\n"
           "2\n00:00:04,500 --> 00:00:07,000\nAnh có chắc là chúng ta không đi lạc chứ? Đường này lạ quá.\n")
JA_TEXT = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは、今日はいい天気ですね。\n\n2\n00:00:03,500 --> 00:00:05,000\n東京へ行きましょう！\n"
ZH_TEXT = "1\n00:00:01,000 --> 00:00:03,000\n你好，这是中文字幕。我们明天见。\n"
KO_TEXT = "1\n00:00:01,000 --> 00:00:03,000\n안녕하세요, 이것은 한국어 자막입니다.\n"
FR_TEXT = "1\n00:00:01,000 --> 00:00:03,000\nC'est déjà l'été, ça va très bien à côté.\n"
RU_TEXT = "1\n00:00:01,000 --> 00:00:03,000\nПривет, это русские субтитры.\n"
ZH_TW_TEXT = "1\n00:00:01,000 --> 00:00:03,000\n你好，這是繁體中文字幕。我們明天見。\n"
VI_LONG = VI_TEXT * 20


# ===================================================================== SRT → VTT


def cues(vtt: str) -> list[list[str]]:
    blocks = vtt.split("\n\n")
    assert blocks[0] == "WEBVTT"
    return [b.strip("\n").split("\n") for b in blocks[1:] if b.strip()]


def test_basic_conversion():
    out = srt_to_vtt(SRT_VI)
    assert out.vtt.startswith("WEBVTT\n\n")
    assert cues(out.vtt) == [["00:00:00.000 --> 00:00:01.000", "Xin chào, đây là phụ đề tiếng Việt."],
                             ["00:00:01.000 --> 00:00:02.000", "Cuộc chiến cuối cùng bắt đầu!"]]
    assert (out.cues, out.skipped_blocks) == (2, 0)


def test_messy_real_world_srt():
    srt = ("﻿1\r\n00:00:01,5 --> 00:00:02,25  X1:100 X2:200 Y1:10 Y2:20\r\n<font color=\"#ffff00\">Vàng</font>\r\n"
           "\r\n\r\n\r\n"
           "00:00:03.000 --> 00:00:04.000\r\n{\\an8}Không có số thứ tự\r\n"
           "3\r\n01:02,000 --> 01:03,000\r\nKhông có giờ & <i>nghiêng</i> <b>đậm</b> < 5 > 3 -->\r\n"
           "4\r\n100:00:00,000 --> 100:00:01,000\r\nRất dài\r\n\r\n"
           "rác không phải phụ đề\r\nvẫn là rác\r\n\r\n"
           "6\r\n00:00:09,000 --> 00:00:08,000\r\nKết thúc trước khi bắt đầu\r\n")
    out = srt_to_vtt(srt)
    assert cues(out.vtt) == [
        ["00:00:01.500 --> 00:00:02.250", "Vàng"],
        ["00:00:03.000 --> 00:00:04.000", "Không có số thứ tự"],
        ["00:01:02.000 --> 00:01:03.000", "Không có giờ &amp; <i>nghiêng</i> <b>đậm</b> &lt; 5 &gt; 3 -&gt;"],
        ["100:00:00.000 --> 100:00:01.000", "Rất dài"],
    ]
    assert (out.cues, out.skipped_blocks) == (4, 2)


def test_multiline_cue_and_entities_kept():
    out = srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\n- Dòng một\n- Dòng hai &amp; ba\n")
    assert cues(out.vtt) == [["00:00:01.000 --> 00:00:02.000", "- Dòng một", "- Dòng hai &amp; ba"]]


@pytest.mark.parametrize("text", ["", "\n\n", "chỉ là văn bản\nkhông có thời gian\n"])
def test_no_cues_is_an_error(text):
    with pytest.raises(SubtitleFormatError, match="no subtitle cue"):
        srt_to_vtt(text)


def test_vtt_passthrough_and_fake_vtt():
    real = "WEBVTT - phụ đề\r\n\r\n00:01.000 --> 00:02.000\r\nXin chào\r\n"
    assert normalize_vtt(real).vtt == "WEBVTT - phụ đề\n\n00:01.000 --> 00:02.000\nXin chào\n"
    fake = normalize_vtt("1\n00:00:01,000 --> 00:00:02,000\nThực ra là SRT\n")
    assert cues(fake.vtt) == [["00:00:01.000 --> 00:00:02.000", "Thực ra là SRT"]]


# ===================================================================== encodings


def _bom_utf8(text: str) -> bytes:
    return b"\xef\xbb\xbf" + text.encode("utf-8")


@pytest.mark.parametrize(("text", "encode", "encoding"), [
    (VI_TEXT, lambda t: t.encode("utf-8"), "utf-8"),
    (VI_TEXT, _bom_utf8, "utf-8-sig"),
    (VI_TEXT, lambda t: t.encode("utf-16"), "utf-16"),  # with BOM
    (VI_TEXT, encode_cp1258, "cp1258"),
    (SRT_VI, encode_cp1258, "cp1258"),
    (VI_LONG, encode_cp1258, "cp1258"),
    (JA_TEXT, lambda t: t.encode("cp932"), "cp932"),
    (JA_TEXT * 30, lambda t: t.encode("cp932"), "cp932"),
    (ZH_TEXT, lambda t: t.encode("gb18030"), "gb18030"),
    (ZH_TEXT * 30, lambda t: t.encode("gb18030"), "gb18030"),
    (ZH_TW_TEXT, lambda t: t.encode("big5"), "big5"),
    (KO_TEXT, lambda t: t.encode("cp949"), "cp949"),
    (KO_TEXT * 30, lambda t: t.encode("cp949"), "cp949"),
    (FR_TEXT, lambda t: t.encode("cp1252"), "cp1252"),
    (RU_TEXT, lambda t: t.encode("cp1251"), "cp1251"),
], ids=["utf8", "utf8-bom", "utf16-bom", "cp1258", "cp1258-fixture", "cp1258-long", "cp932",
        "cp932-long", "gb18030", "gb18030-long", "big5", "cp949", "cp949-long", "cp1252", "cp1251"])
def test_decode(text, encode, encoding):
    decoded = decode_subtitle(encode(text))
    assert decoded.encoding == encoding
    assert decoded.text == unicodedata.normalize("NFC", text)


def test_cp1258_text_is_nfc_like_utf8():
    """Windows-1258 stores 'ố' as 'ô' + combining acute; after decoding it must be the same
    characters as the UTF-8 file, or search and display would differ."""
    assert decode_subtitle(encode_cp1258(VI_TEXT)).text == decode_subtitle(VI_TEXT.encode()).text


def test_undecodable_is_a_clear_error():
    junk = bytes(range(128, 256)) * 4
    with pytest.raises(SubtitleEncodingError, match="cannot tell the text encoding.*UTF-8"):
        decode_subtitle(junk)


def test_broken_bom_is_a_clear_error():
    with pytest.raises(SubtitleEncodingError, match="utf-8-sig"):
        decode_subtitle(b"\xef\xbb\xbf\xff\xfe broken")


# ===================================================================== languages / discovery


@pytest.mark.parametrize(("tag", "expected"), [
    ("vi", ("vi", "Tiếng Việt")), ("VIE", ("vi", "Tiếng Việt")), ("Vietnamese", ("vi", "Tiếng Việt")),
    ("eng", ("en", "English")), ("jpn", ("ja", "日本語")), ("pt-BR", ("pt-BR", "Português (Brasil)")),
    ("pt_br", ("pt-BR", "Português (Brasil)")), ("en-US", ("en-US", "English (US)")),
    ("zh-Hans", ("zh-Hans", "中文 (简体)")), ("director", None), ("x1", None),
])
def test_parse_language(tag, expected):
    assert parse_language(tag) == expected


def touch(path: Path, data: bytes = b"1\n00:00:00,000 --> 00:00:01,000\nx\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_find_subtitles_naming_rules(tmp_path):
    video = touch(tmp_path / "Phim hay.mp4", b"video")
    touch(tmp_path / "Phim hay.part2.mp4", b"another video")
    for name in ["Phim hay.srt", "Phim hay.vi.srt", "Phim hay.vi.vtt", "PHIM HAY.ENG.SRT",
                 "Phim hay.en.forced.srt", "Phim hay.pt-BR.srt", "Phim hay.director.srt",
                 "Phim hay.part2.srt",  # belongs to Phim hay.part2.mp4
                 "Phim hay 2.srt", "Phim.srt", "Phim hay.vi.txt", "Phim hay.vi.ass"]:
        touch(tmp_path / name)
    tracks = {t.key: (t.lang, t.label, t.format, t.file_name)
              for t in find_subtitles(video, VIDEO_EXTS)}
    assert tracks == {
        "en": ("en", "English", "srt", "PHIM HAY.ENG.SRT"),
        "en-forced": ("en", "English (forced)", "srt", "Phim hay.en.forced.srt"),
        "pt-br": ("pt-BR", "Português (Brasil)", "srt", "Phim hay.pt-BR.srt"),
        "vi": ("vi", "Tiếng Việt", "vtt", "Phim hay.vi.vtt"),  # VTT first for the same language
        "vi-2": ("vi", "Tiếng Việt", "srt", "Phim hay.vi.srt"),
        "und": ("und", "Phụ đề", "srt", "Phim hay.srt"),
        "und-director": ("und", "director", "srt", "Phim hay.director.srt"),
    }
    assert [t.key for t in find_subtitles(video, VIDEO_EXTS)][-2:] == ["und", "und-director"]


def test_find_subtitles_nfd_and_hidden(tmp_path):
    video = touch(tmp_path / unicodedata.normalize("NFD", "Tập 01.mkv"), b"video")
    touch(tmp_path / "Tập 01.vi.srt")  # NFC name for an NFD video name
    hidden = touch(tmp_path / "Tập 01.en.srt")
    if IS_WINDOWS:
        import ctypes

        assert ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    else:
        hidden.rename(tmp_path / ".Tập 01.en.srt")
    assert [t.key for t in find_subtitles(video, VIDEO_EXTS)] == ["vi"]


def test_subtitle_symlink_is_ignored(tmp_path):
    video = touch(tmp_path / "lib" / "Phim.mp4", b"video")
    outside = touch(tmp_path / "outside.srt")
    try:
        os.symlink(outside, tmp_path / "lib" / "Phim.vi.srt")
    except OSError:
        pytest.skip("no privilege to create file symlinks (Windows without Developer Mode)")
    assert find_subtitles(video, VIDEO_EXTS) == []


def test_missing_folder_gives_no_tracks(tmp_path):
    assert find_subtitles(tmp_path / "gone" / "Phim.mp4", VIDEO_EXTS) == []


def test_load_as_vtt_errors(tmp_path):
    with pytest.raises(SubtitleError, match="cannot read"):
        load_as_vtt(tmp_path / "missing.srt", "srt")
    with pytest.raises(SubtitleError, match="no subtitle cue"):
        load_as_vtt(touch(tmp_path / "a.srt", "không phải phụ đề".encode()), "srt")
    big = tmp_path / "big.srt"
    with open(big, "wb") as f:
        f.truncate(11 * 1024 * 1024)
    with pytest.raises(SubtitleError, match="too large"):
        load_as_vtt(big, "srt")


# ===================================================================== HTTP


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "lib"
    shutil.copytree(LIBRARY, root)
    touch(root / "Phim lẻ" / "Tài liệu" / "Lỗi.mp4", b"video")
    touch(root / "Phim lẻ" / "Tài liệu" / "Lỗi.vi.srt", bytes(range(128, 256)) * 4)
    return root


@pytest.fixture
def ids(lib, _isolated_index) -> dict[str, str]:
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], VIDEO_EXTS))
        return {i.relative_path: i.id for i in index.items()}


def settings_for(lib, index_path, tmp_path, **env):
    return load_settings(environ={"HUB_INDEX_PATH": str(index_path), **env},
                         env_file=tmp_path / "none.env", cli_paths=[str(lib)])


@pytest.fixture
def client(lib, ids, _isolated_index, tmp_path):
    with TestClient(create_app(settings_for(lib, _isolated_index, tmp_path))) as c:
        yield c


MOVIE = "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"
EPISODE = "Phim bộ/Mùa 1/Tập 01.mkv"


def test_track_list(client, ids):
    r = client.get(f"/subtitles/{ids[MOVIE]}")
    assert r.status_code == 200
    assert r.json() == [
        {"key": "en", "lang": "en", "label": "English", "format": "srt",
         "url": f"/subtitles/{ids[MOVIE]}/en.vtt"},
        {"key": "vi", "lang": "vi", "label": "Tiếng Việt", "format": "srt",
         "url": f"/subtitles/{ids[MOVIE]}/vi.vtt"},
    ]
    assert "file_name" not in r.text and ".srt" not in r.text  # no file names/paths leak


def test_vtt_from_utf8_srt(client, ids):
    r = client.get(f"/subtitles/{ids[MOVIE]}/vi.vtt")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/vtt; charset=utf-8"
    text = r.content.decode("utf-8")
    assert text.split("\n")[0] == "WEBVTT"
    assert "00:00:00.000 --> 00:00:01.000\nXin chào, đây là phụ đề tiếng Việt." in text


def test_vtt_from_cp1258_srt_has_correct_vietnamese(client, ids, caplog):
    caplog.set_level("INFO", logger="house_cinema")
    r = client.get(f"/subtitles/{ids[EPISODE]}/vi.vtt")
    assert r.status_code == 200
    text = r.content.decode("utf-8")  # strict: must be valid UTF-8
    assert "Xin chào, đây là phụ đề tiếng Việt." in text
    assert "Cuộc chiến cuối cùng bắt đầu!" in text
    assert unicodedata.is_normalized("NFC", text)
    assert any("decoded as cp1258" in r.getMessage() for r in caplog.records)


def test_video_without_subtitles_still_plays(client, ids):
    webm = ids["Phim lẻ/Tài liệu/Đại dương xanh.webm"]
    assert client.get(f"/subtitles/{webm}").json() == []
    r = client.get(f"/stream/{webm}", headers={"Range": "bytes=0-99"})
    assert (r.status_code, len(r.content)) == (206, 100)


def test_unreadable_subtitle_is_a_clear_422(client, ids, caplog):
    media = ids["Phim lẻ/Tài liệu/Lỗi.mp4"]
    assert [t["key"] for t in client.get(f"/subtitles/{media}").json()] == ["vi"]
    r = client.get(f"/subtitles/{media}/vi.vtt")
    assert r.status_code == 422
    assert "cannot tell the text encoding" in r.text and "UTF-8" in r.text
    assert "lib" not in r.text and "Lỗi" not in r.text  # no path in the answer
    assert any("cannot be served" in rec.getMessage() for rec in caplog.records)
    # The video itself is not affected.
    assert client.get(f"/stream/{media}").status_code == 200


@pytest.mark.parametrize("url", ["/subtitles/0123456789abcdef", "/subtitles/0123456789abcdef/vi.vtt"])
def test_unknown_video_is_404(client, url):
    assert client.get(url).status_code == 404


def test_unknown_track_is_404(client, ids):
    assert client.get(f"/subtitles/{ids[MOVIE]}/fr.vtt").status_code == 404
    assert client.get(f"/subtitles/{ids[MOVIE]}/vi.srt").status_code == 404


def test_subtitle_added_later_shows_up_without_rescan(client, ids, lib):
    webm = ids["Phim lẻ/Tài liệu/Đại dương xanh.webm"]
    touch(lib / "Phim lẻ" / "Tài liệu" / "Đại dương xanh.ja.srt", JA_TEXT.encode("cp932"))
    assert [t["key"] for t in client.get(f"/subtitles/{webm}").json()] == ["ja"]
    assert "東京へ行きましょう！" in client.get(f"/subtitles/{webm}/ja.vtt").content.decode()


@pytest.mark.parametrize("url", ["/subtitles/..%2F..%2Fwindows%2Fwin.ini",
                                 "/subtitles/0123456789abcdef/..%2F..%2Fwin.ini.vtt",
                                 "/subtitles/C:%5CWindows/vi.vtt"])
def test_traversal_is_blocked(client, caplog, url):
    caplog.set_level("INFO", logger="house_cinema")
    assert client.get(url).status_code == 404
    assert any(r.levelname == "WARNING" and "Blocked" in r.getMessage() for r in caplog.records)


def test_bad_id_on_subtitles_is_logged_as_probe(client, caplog):
    caplog.set_level("INFO", logger="house_cinema")
    assert client.get("/subtitles/zzzz/vi.vtt").status_code == 404
    assert any("Blocked /subtitles request" in r.getMessage() for r in caplog.records)


def test_demo_page_only_outside_production(lib, ids, _isolated_index, tmp_path):
    with TestClient(create_app(settings_for(lib, _isolated_index, tmp_path))) as c:
        r = c.get("/demo/subtitles")
        assert r.status_code == 200 and "<track" not in r.text and "createElement(\"track\")" in r.text
    prod = settings_for(lib, _isolated_index, tmp_path, APP_ENV="production")
    with TestClient(create_app(prod)) as c:
        assert c.get("/demo/subtitles").status_code == 404


def test_cli_info_lists_subtitles(capsys, lib, ids, tmp_path):
    code = pipeline.main(["info", ids[MOVIE], "--path", str(lib), "--env-file", str(tmp_path / "none.env")])
    out = capsys.readouterr().out
    assert code == 0
    assert "subtitles  | English [en] — Cuộc Chiến Cuối Cùng.en.srt" in out
    assert "Tiếng Việt [vi] — Cuộc Chiến Cuối Cùng.vi.srt" in out
    code = pipeline.main(["info", ids["Phim lẻ/Tài liệu/Đại dương xanh.webm"], "--path", str(lib),
                          "--env-file", str(tmp_path / "none.env")])
    assert "subtitles  | none" in capsys.readouterr().out
