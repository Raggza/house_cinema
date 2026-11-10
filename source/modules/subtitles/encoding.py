"""Decode a subtitle file whose encoding is not declared.

Order:
1. A byte order mark (UTF-8, UTF-16 LE/BE) decides.
2. Valid UTF-8 is UTF-8 (other encodings practically never form valid UTF-8 by chance).
3. Otherwise each legacy encoding that fits the subtitles people actually have is tried
   (Vietnamese Windows-1258 first, then Japanese, Chinese, Korean, Western, Cyrillic), and
   the one whose non-ASCII characters look most like real text in that language wins.
   Plain "does it decode" is not enough: Windows-1252 decodes any byte, and Chinese GB18030
   decodes most byte pairs.
4. If nothing looks right, SubtitleEncodingError: better a clear error than garbled text.

Windows-1258 writes Vietnamese tones as separate combining marks ("a" + U+0301); the text
is NFC-normalized so it becomes "á" like everywhere else.
"""

from __future__ import annotations

import codecs
import re
import unicodedata
from dataclasses import dataclass

_VIETNAMESE = set(unicodedata.normalize(
    "NFC",
    "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
))
_VIETNAMESE |= {c.upper() for c in _VIETNAMESE}


def _vietnamese_only(c: str) -> bool:
    """Letters no Western language uses: đ, horn (ơ ư), breve (ă), hook above, dot below,
    or two marks at once (ế, ặ, ữ…)."""
    if c in "đĐ":
        return True
    marks = [m for m in unicodedata.normalize("NFD", c) if unicodedata.combining(m)]
    return len(marks) >= 2 or any(m in "̛̣̆̉" for m in marks)


_VIETNAMESE_ONLY = {c for c in _VIETNAMESE if _vietnamese_only(c)}
_WESTERN = set("àáâãäåæçèéêëìíîïñòóôõöøùúûüýÿßÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÑÒÓÔÕÖØÙÚÛÜÝœŒ¿¡«»€°–—‘’“”…·")
_TYPOGRAPHY = set("‘’“”–—…·•€°«»♪♫ ")

# Order matters on ties: Korean bytes are also valid (common) Chinese GB18030 characters, so
# Korean is tried first; Chinese read as Korean scores low anyway (Hanja, see _score).
LEGACY_ENCODINGS = ("cp1258", "cp932", "cp949", "gb18030", "big5", "cp1252", "cp1251")
MIN_SCORE = 0.8


class SubtitleEncodingError(ValueError):
    pass


@dataclass(frozen=True)
class Decoded:
    text: str
    encoding: str


def _is_cjk(c: str) -> bool:
    o = ord(c)
    return (0x3040 <= o <= 0x30FF  # hiragana, katakana
            or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF  # CJK ideographs
            or 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF  # hangul
            or 0x3000 <= o <= 0x303F  # CJK punctuation 、。「」
            or 0xFF01 <= o <= 0xFF5E)  # fullwidth ASCII ！？（）


def _is_hangul(c: str) -> bool:
    return "가" <= c <= "힣" or "ᄀ" <= c <= "ᇿ"


def _is_han(c: str) -> bool:
    return "一" <= c <= "鿿" or "㐀" <= c <= "䶿"


def _in_gb2312(c: str) -> bool:
    """In GB2312 level 1 (the 3755 most used Chinese characters, lead bytes B0–D7)."""
    try:
        return c.encode("gb2312")[0] <= 0xD7
    except UnicodeEncodeError:
        return False


def _in_euc_kr(c: str) -> bool:
    try:
        c.encode("euc_kr")
        return True
    except UnicodeEncodeError:
        return False


def _frequent_big5(c: str) -> bool:
    """Big5 puts the ~5400 frequent characters first (lead bytes A4–C6)."""
    try:
        return 0xA1 <= c.encode("big5")[0] <= 0xC6
    except UnicodeEncodeError:
        return False


_CJK_SPACE = re.compile(r"[぀-ヿ㐀-鿿가-힣][,.!?…]? +[぀-ヿ㐀-鿿가-힣]")


def _score(text: str, encoding: str) -> float:
    """Share (0..1) of the non-ASCII characters that are plausible for ``encoding``.

    The wrong legacy encoding usually still "decodes"; what gives it away is the result:
    Cyrillic letters glued to Latin ones (French read as Windows-1251), Hangul without
    spaces between words (Chinese/Japanese read as Korean), rare Chinese characters in a row
    (Korean or Big5 read as GB18030), and so on.
    """
    chars = [(i, c) for i, c in enumerate(text) if ord(c) > 127]
    if not chars:
        return 1.0
    n = len(chars)

    def next_to_latin(i: int) -> bool:
        return any(0 <= j < len(text) and text[j].isascii() and text[j].isalpha() for j in (i - 1, i + 1))

    if encoding == "cp1258":
        good = sum(c in _VIETNAMESE or c in _TYPOGRAPHY for _, c in chars)
        # Without any letter only Vietnamese has (ơ, ư, đ, ả, ẽ, ọ…), this is likely Western text.
        if not any(c in _VIETNAMESE_ONLY for _, c in chars):
            good *= 0.5
    elif encoding == "cp1252":
        good = sum(c in _WESTERN or c in _TYPOGRAPHY for _, c in chars) * 0.95  # after the specific ones
    elif encoding == "cp1251":
        good = sum(("Ѐ" <= c <= "ӿ" and not next_to_latin(i)) or c in _TYPOGRAPHY for i, c in chars)
    elif encoding == "cp932":
        good = sum(_is_cjk(c) or c in _TYPOGRAPHY for _, c in chars)
        if not any("぀" <= c <= "ヿ" for _, c in chars):
            good *= 0.9  # Japanese text without any kana is unlikely
    elif encoding in ("gb18030", "big5"):
        common = _in_gb2312 if encoding == "gb18030" else _frequent_big5
        good = sum((_is_han(c) and common(c)) or (_is_cjk(c) and not _is_han(c) and not _is_hangul(c))
                   or c in _TYPOGRAPHY for _, c in chars)
        if any("぀" <= c <= "ヿ" for _, c in chars):
            good *= 0.9  # kana: Japanese is the better guess
    elif encoding == "cp949":
        # Modern Korean subtitles are Hangul (from the common EUC-KR set), practically never
        # Hanja: Chinese bytes read as Korean give Hanja, Japanese bytes give rare syllables.
        good = sum((_is_hangul(c) and _in_euc_kr(c)) or c in _TYPOGRAPHY
                   or (_is_cjk(c) and not _is_han(c) and not _is_hangul(c) and not "぀" <= c <= "ヿ")
                   for _, c in chars)
        hangul = sum(_is_hangul(c) for _, c in chars)
        # Korean separates words with spaces; Chinese/Japanese bytes read as Korean do not.
        if hangul >= 8 and len(_CJK_SPACE.findall(text)) * 10 < hangul:
            good *= 0.7
    else:
        good = 0
    return good / n


def decode_subtitle(data: bytes) -> Decoded:
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                     (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                return Decoded(unicodedata.normalize("NFC", data.decode(enc)), enc)
            except UnicodeDecodeError as exc:
                raise SubtitleEncodingError(f"starts like {enc} but is not valid {enc}: {exc.reason}") from None
    try:
        return Decoded(unicodedata.normalize("NFC", data.decode("utf-8")), "utf-8")
    except UnicodeDecodeError:
        pass

    best: tuple[float, str, str] | None = None
    for enc in LEGACY_ENCODINGS:
        try:
            text = unicodedata.normalize("NFC", data.decode(enc))
        except UnicodeDecodeError:
            continue
        score = _score(text, enc)
        if best is None or score > best[0]:  # ties: earlier (more specific) encoding wins
            best = (score, enc, text)
    if best is None or best[0] < MIN_SCORE:
        tried = f"best guess {best[1]} ({best[0]:.0%} plausible)" if best else "no encoding fits"
        raise SubtitleEncodingError(
            f"cannot tell the text encoding (not UTF-8; {tried}). Save the file as UTF-8 "
            "(e.g. Notepad > Save as > Encoding: UTF-8).")
    return Decoded(best[2], best[1])
