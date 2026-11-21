"""The languages MiLMMT-46 translates between (2.10, caption translation), spelled as its model
card spells them, and how QiTV's names for languages map onto them: a caption language (Qwen's
English names, services.captions.languages) to the model's name for the source, and an app
language code to the target the setting starts at."""

from __future__ import annotations

import re
from typing import Optional

from services.captions import languages

MT_LANGUAGES: tuple[str, ...] = (
    "Arabic",
    "Azerbaijani",
    "Bulgarian",
    "Bengali",
    "Catalan",
    "Czech",
    "Danish",
    "German",
    "Greek",
    "English",
    "Spanish",
    "Persian",
    "Finnish",
    "French",
    "Hebrew",
    "Hindi",
    "Croatian",
    "Hungarian",
    "Indonesian",
    "Italian",
    "Japanese",
    "Kazakh",
    "Khmer",
    "Korean",
    "Lao",
    "Malay",
    "Burmese",
    "Norwegian",
    "Dutch",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Slovak",
    "Slovenian",
    "Swedish",
    "Tamil",
    "Thai",
    "Tagalog",
    "Turkish",
    "Urdu",
    "Uzbek",
    "Vietnamese",
    "Cantonese",
    "Chinese (Simplified)",
    "Chinese (Traditional)",
)

# 2.13.1: the targets whose third person says whether it is a man or a woman (he/she, or a verb
# that agrees: Hindi's and Urdu's), which a line alone often can't tell: each line is translated
# with the line before and its translation ("Turkish: s1 s / English: t1"; 2026-11-21: the right
# pronoun in 16-17 of 19 TV scenes against 7 alone). Vietnamese's everyday "anh ấy"/"cô ấy" and
# Tamil's he/she both need it; Chinese writes 他/她 but says them alike, and is left alone
GENDERED = frozenset(
    {
        "Arabic",
        "Bulgarian",
        "Catalan",
        "Czech",
        "Danish",
        "German",
        "Greek",
        "English",
        "Spanish",
        "French",
        "Hebrew",
        "Hindi",
        "Croatian",
        "Italian",
        "Norwegian",
        "Dutch",
        "Polish",
        "Portuguese",
        "Romanian",
        "Russian",
        "Slovak",
        "Slovenian",
        "Swedish",
        "Tamil",
        "Urdu",
        "Vietnamese",
    }
)
# ... and the rest: one third person for both (Turkish "o", Finnish "hän", Persian "او", ...)
NOT_GENDERED = frozenset(
    {
        "Azerbaijani",
        "Bengali",
        "Persian",
        "Finnish",
        "Hungarian",
        "Indonesian",
        "Japanese",
        "Kazakh",
        "Khmer",
        "Korean",
        "Lao",
        "Malay",
        "Burmese",
        "Thai",
        "Tagalog",
        "Turkish",
        "Uzbek",
        "Cantonese",
        "Chinese (Simplified)",
        "Chinese (Traditional)",
    }
)


def needs_context(target: str) -> bool:
    """A line translated into `target` (MiLMMT's name) goes with the line before (`GENDERED`)."""
    return target in GENDERED


# scripts where a character carries about a word (Thai, Lao, Burmese, Khmer, Japanese's kana,
# Chinese, Korean): services.captions.translate's length cap counts each as 3
DENSE = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff]"
)


def words_in(text: str) -> int:
    """About how many words `text` has: by its spaces, or two characters a word of a script
    written without them (a Japanese or Chinese clause is one "word" by its spaces)."""
    return max(len(text.split()), len(DENSE.findall(text)) // 2)


# QiTV's caption languages (Qwen's names) that the model names differently
_SOURCE = {"Chinese": "Chinese (Simplified)", "Filipino": "Tagalog"}

# the app's language codes (services.i18n.available_languages) the model has a language for;
# the rest (Estonian, Swahili, Latvian, ...) start the setting at Off
_APP = {
    "en": "English",
    "az": "Azerbaijani",
    "id": "Indonesian",
    "ms": "Malay",
    "ca": "Catalan",
    "da": "Danish",
    "de": "German",
    "es": "Spanish",
    "fil": "Tagalog",
    "fr": "French",
    "hr": "Croatian",
    "it": "Italian",
    "hu": "Hungarian",
    "nl": "Dutch",
    "nb": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "fi": "Finnish",
    "sv": "Swedish",
    "vi": "Vietnamese",
    "tr": "Turkish",
    "cs": "Czech",
    "el": "Greek",
    "bg": "Bulgarian",
    "ru": "Russian",
    "he": "Hebrew",
    "ur": "Urdu",
    "ar": "Arabic",
    "fa": "Persian",
    "hi": "Hindi",
    "bn": "Bengali",
    "th": "Thai",
    "ja": "Japanese",
    "zh": "Chinese (Simplified)",
    "zh_TW": "Chinese (Traditional)",
    "ko": "Korean",
}

# names in themselves, for those the captions' own table (Qwen's and Parakeet's) lacks
_OWN = {
    "Azerbaijani": "Azərbaycanca",
    "Bengali": "বাংলা",
    "Catalan": "Català",
    "Persian": "فارسی",
    "Hebrew": "עברית",
    "Croatian": "Hrvatski",
    "Kazakh": "Қазақша",
    "Khmer": "ខ្មែរ",
    "Lao": "ລາວ",
    "Burmese": "မြန်မာ",
    "Norwegian": "Norsk",
    "Tamil": "தமிழ்",
    "Tagalog": "Tagalog",
    "Urdu": "اردو",
    "Uzbek": "Oʻzbekcha",
    "Cantonese": "粵語",
    "Chinese (Simplified)": "简体中文",
    "Chinese (Traditional)": "繁體中文",
}


def source_for(caption_language: str) -> Optional[str]:
    """The model's name for a caption language (Qwen's English name); None where it has none."""
    name = _SOURCE.get(caption_language, caption_language)
    return name if name in MT_LANGUAGES else None


def app_default(app_code: str) -> Optional[str]:
    """The model's language for an app language code, where it has one."""
    return _APP.get(app_code)


def own_name(mt_name: str) -> str:
    """The language's name in itself ("Türkçe" for "Turkish")."""
    if mt_name in _OWN:
        return _OWN[mt_name]
    return languages.own_name(mt_name)
