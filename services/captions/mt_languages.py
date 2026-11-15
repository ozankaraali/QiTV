"""The languages MiLMMT-46 translates between (2.10, caption translation), spelled as its model
card spells them, and how QiTV's names for languages map onto them: a caption language (Qwen's
English names, services.captions.languages) to the model's name for the source, and an app
language code to the target the setting starts at."""

from __future__ import annotations

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
