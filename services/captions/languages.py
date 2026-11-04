"""The languages AI captions can hear: Qwen3-ASR's 30, and since 2.9.3 the European ones only
Parakeet TDT 0.6B v3 writes, by their codes and names (no Qt).

Qwen3-ASR is told a language by its English name ("Turkish", never "tr"). A stream names an
audio track's language by an ISO 639 code: two letters (639-1: "tr"), or three (639-2's
bibliographic or terminology code: "ger" or "deu"; 639-3's for a language without two letters:
"yue", "fil"), sometimes with a region ("pt-BR", "en_US"). The viewer sees each language by its
own name ("Türkçe"), as Settings lists QiTV's own languages.

    qwen_name("tur") == qwen_name("tr-TR") == qwen_name("Türkçe") == "Turkish"
    own_name("Turkish") == "Türkçe"
"""

from __future__ import annotations

from typing import Optional

# Qwen's order: (ISO 639-1 code, else 639-3's; Qwen's English name; the language's own name;
# its other codes and names).
_TABLE: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("zh", "Chinese", "中文", ("zho", "chi", "cmn", "mandarin")),
    ("en", "English", "English", ("eng",)),
    ("yue", "Cantonese", "粵語", ("zh-yue",)),
    ("ar", "Arabic", "العربية", ("ara",)),
    ("de", "German", "Deutsch", ("deu", "ger")),
    ("fr", "French", "Français", ("fra", "fre")),
    ("es", "Spanish", "Español", ("spa", "castilian")),
    ("pt", "Portuguese", "Português", ("por",)),
    ("id", "Indonesian", "Bahasa Indonesia", ("ind",)),
    ("it", "Italian", "Italiano", ("ita",)),
    ("ko", "Korean", "한국어", ("kor",)),
    ("ru", "Russian", "Русский", ("rus",)),
    ("th", "Thai", "ไทย", ("tha",)),
    ("vi", "Vietnamese", "Tiếng Việt", ("vie",)),
    ("ja", "Japanese", "日本語", ("jpn",)),
    ("tr", "Turkish", "Türkçe", ("tur",)),
    ("hi", "Hindi", "हिन्दी", ("hin",)),
    ("ms", "Malay", "Bahasa Melayu", ("msa", "may", "zsm")),
    ("nl", "Dutch", "Nederlands", ("nld", "dut", "flemish")),
    ("sv", "Swedish", "Svenska", ("swe",)),
    ("da", "Danish", "Dansk", ("dan",)),
    ("fi", "Finnish", "Suomi", ("fin",)),
    ("pl", "Polish", "Polski", ("pol",)),
    ("cs", "Czech", "Čeština", ("ces", "cze")),
    ("fil", "Filipino", "Filipino", ("tl", "tgl", "tagalog")),
    ("fa", "Persian", "فارسی", ("fas", "per", "pes", "farsi")),
    ("el", "Greek", "Ελληνικά", ("ell", "gre")),
    ("hu", "Hungarian", "Magyar", ("hun",)),
    ("mk", "Macedonian", "Македонски", ("mkd", "mac")),
    ("ro", "Romanian", "Română", ("ron", "rum", "mo", "mol", "moldavian")),
)

# 2.9.3: the European languages Parakeet TDT 0.6B v3 writes and Qwen does not: chosen by hand
# or named by a sound track, Parakeet writes them (Qwen can neither write nor tell them).
_EUROPEAN_ONLY: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("uk", "Ukrainian", "Українська", ("ukr",)),
    ("bg", "Bulgarian", "Български", ("bul",)),
    ("hr", "Croatian", "Hrvatski", ("hrv",)),
    ("sk", "Slovak", "Slovenčina", ("slk", "slo")),
    ("sl", "Slovenian", "Slovenščina", ("slv",)),
    ("et", "Estonian", "Eesti", ("est",)),
    ("lv", "Latvian", "Latviešu", ("lav",)),
    ("lt", "Lithuanian", "Lietuvių", ("lit",)),
    ("mt", "Maltese", "Malti", ("mlt",)),
)

# Qwen's English names, the language lock's words, in Qwen's order.
QWEN_LANGUAGES: list[str] = [english for _code, english, _own, _others in _TABLE]
# Every language the captions can be set to: Qwen's, then Parakeet's own (2.9.3).
CAPTION_LANGUAGES: list[str] = [
    *QWEN_LANGUAGES,
    *(english for _code, english, _own, _others in _EUROPEAN_ONLY),
]
# 2.9.3: those Parakeet TDT 0.6B v3 writes, better and faster than Qwen (FLEURS: English 5.7 %
# of words wrong against Qwen 0.6B's 8 %, French 14 % against 28 %, Russian 3 % against 7 %).
EUROPEAN = frozenset(
    {
        "English",
        "German",
        "French",
        "Spanish",
        "Portuguese",
        "Italian",
        "Russian",
        "Dutch",
        "Swedish",
        "Danish",
        "Finnish",
        "Polish",
        "Czech",
        "Greek",
        "Hungarian",
        "Romanian",
        *(english for _code, english, _own, _others in _EUROPEAN_ONLY),
    }
)

# 2.9.3: those Cohere Transcribe writes, told the language: the best of the models tried there,
# on FLEURS and on broadcasts (French 3.6 % of words wrong against Parakeet's 13.3 %, Greek 21
# against 46, Japanese 5.2 against Qwen's 12, Vietnamese 6 against 12, Arabic 11 against 28;
# names right on the news); Parakeet keeps the European languages it doesn't know.
COHERE = frozenset(
    {
        "English",
        "French",
        "German",
        "Italian",
        "Spanish",
        "Portuguese",
        "Greek",
        "Dutch",
        "Polish",
        "Chinese",
        "Japanese",
        "Korean",
        "Vietnamese",
        "Arabic",
    }
)

_ALL = (*_TABLE, *_EUROPEAN_ONLY)
_OWN = {english: own for _code, english, own, _others in _ALL}
_CODE = {english: code for code, english, _own, _others in _ALL}
# Every code and name, folded, to its English name.
_LOOKUP = {
    key.casefold(): english
    for code, english, own, others in _ALL
    for key in (code, english, own, *others)
}


def qwen_name(code_or_name: Optional[str]) -> Optional[str]:
    """The English name (Qwen's, or for a language only Parakeet writes, its own: 2.9.3) of a
    language given by its ISO 639 code (with a region or not), its English name or its own
    name; None when the captions don't know it (or for none at all: "und", "mul", "")."""
    if not code_or_name:
        return None
    text = str(code_or_name).strip().casefold().replace("_", "-")
    found = _LOOKUP.get(text)
    if found is None and "-" in text:
        found = _LOOKUP.get(text.split("-", 1)[0])  # "pt-br": Portuguese
    return found


def own_name(qwen_name: str) -> str:
    """The language's name in itself ("Türkçe" for "Turkish"); a name that is none of Qwen's
    comes back as it is."""
    return _OWN.get(qwen_name, qwen_name)


def iso_code(qwen_name: str) -> str:
    """The language's ISO 639-1 code ("tr"), else its 639-3 one ("yue", "fil"); "" for a name
    that is none of Qwen's."""
    return _CODE.get(qwen_name, "")
