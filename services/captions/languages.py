"""The languages AI captions can hear: Qwen3-ASR's 30, by their codes and names (no Qt).

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

# Qwen's English names, the language lock's words, in Qwen's order.
QWEN_LANGUAGES: list[str] = [english for _code, english, _own, _others in _TABLE]

_OWN = {english: own for _code, english, own, _others in _TABLE}
_CODE = {english: code for code, english, _own, _others in _TABLE}
# Every code and name, folded, to Qwen's name.
_LOOKUP = {
    key.casefold(): english
    for code, english, own, others in _TABLE
    for key in (code, english, own, *others)
}


def qwen_name(code_or_name: Optional[str]) -> Optional[str]:
    """Qwen's English name for a language given by its ISO 639 code (with a region or not),
    its English name or its own name; None when it is none of Qwen's (or none at all: "und",
    "mul", "")."""
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
