"""Language tags found in subtitle file names (``Phim.vi.srt``, ``Phim.eng.srt``,
``Phim.Vietnamese.srt``) → BCP 47 code + a label in that language."""

from __future__ import annotations

# code: (aliases, label)
_LANGUAGES: dict[str, tuple[tuple[str, ...], str]] = {
    "vi": (("vie", "vn", "vietnamese", "tieng viet", "tiếng việt"), "Tiếng Việt"),
    "en": (("eng", "english"), "English"),
    "ja": (("jpn", "jp", "japanese"), "日本語"),
    "zh": (("chi", "zho", "chinese", "chs", "cht"), "中文"),
    "ko": (("kor", "kr", "korean"), "한국어"),
    "fr": (("fre", "fra", "french"), "Français"),
    "de": (("ger", "deu", "german"), "Deutsch"),
    "es": (("spa", "spanish"), "Español"),
    "pt": (("por", "portuguese"), "Português"),
    "it": (("ita", "italian"), "Italiano"),
    "ru": (("rus", "russian"), "Русский"),
    "th": (("tha", "thai"), "ไทย"),
    "id": (("ind", "indonesian"), "Bahasa Indonesia"),
    "ms": (("may", "msa", "malay"), "Bahasa Melayu"),
    "lo": (("lao",), "ລາວ"),
    "km": (("khm", "khmer"), "ខ្មែរ"),
    "ar": (("ara", "arabic"), "العربية"),
    "hi": (("hin", "hindi"), "हिन्दी"),
}
_ALIASES = {alias: code for code, (aliases, _) in _LANGUAGES.items() for alias in (code, *aliases)}
_REGION_LABELS = {"zh-hans": "中文 (简体)", "zh-hant": "中文 (繁體)", "zh-cn": "中文 (简体)",
                  "zh-tw": "中文 (繁體)", "pt-br": "Português (Brasil)"}
FLAGS = {"forced": "forced", "sdh": "SDH", "cc": "CC", "hi": "SDH"}


def parse_language(tag: str) -> tuple[str, str] | None:
    """('vi', 'Tiếng Việt') for 'vi' / 'vie' / 'Vietnamese'; region kept ('pt-BR');
    None if ``tag`` is not a known language."""
    t = tag.strip().casefold().replace("_", "-")
    if t in _REGION_LABELS:
        base, region = t.split("-", 1)
        return f"{base}-{region.upper() if len(region) == 2 else region.capitalize()}", _REGION_LABELS[t]
    if t in _ALIASES:
        code = _ALIASES[t]
        return code, _LANGUAGES[code][1]
    base, _, region = t.partition("-")
    if base in _ALIASES and region.isalpha() and len(region) == 2:
        code = _ALIASES[base]
        return f"{code}-{region.upper()}", f"{_LANGUAGES[code][1]} ({region.upper()})"
    return None
