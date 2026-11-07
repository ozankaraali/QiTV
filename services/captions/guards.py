"""What a speech model writes that nobody said (2.9, AI captions), kept off the screen.

Measured on broadcasts (2026-11-02, Qwen3-ASR and Whisper on a drama with music and a news
channel): over music or silence the models write a phrase again and again until their token
cap ("Sen öyle." 40 times), the words of their own prompt ("system."), or the credits their
training subtitles ended with ("Altyazı M.K.", "İzlediğiniz için teşekkür ederim.", "Thanks
for watching"). People repeat themselves for real ("Hayır hayır hayır!"), so `clean()` shows a
repetition as said; only a loop no one speaks drops the whole caption (the Turkish Moonshine trial,
2026-11-07: over sung music it wrote "iki bin on yedide" again and again, shown as "2017'de"; the
owner saw "1606 124" when nobody said a number);
`token_cap()` bounds what a piece of speech can produce. With no language given, the 1.7B
(2026-11-03, the community export) writes the one it heard first ("language Turkish<asr_text>
Gedällle halkoyunları…"): only the words after it are the caption.
"""

from __future__ import annotations

import re

# About three words a second at most in fast speech, a word a token or two: the cap leaves
# room for that and stops a loop early.
TOKENS_PER_S = 6.0
TOKENS_BASE = 8
# 2.9.3: in these scripts Qwen's tokens are a few letters each, and the cap cut its captions
# short (FLEURS, QiTV's pieces: Hindi 57 % of words wrong with the cap, 34 % without it;
# Arabic and Greek too; English, Russian, Turkish and Japanese unchanged). While the language
# isn't known yet, as much room as for Russian or Korean.
_DENSE = {"Hindi": 3.0, "Greek": 3.0, "Arabic": 3.0, "Persian": 3.0, "Thai": 3.0}
_UNKNOWN = 2.0

# The model's own heading: the language it heard, then its text marker (once, at the start).
_HEADING = re.compile(r"^\s*(?:language\s+[^<]{0,40})?<asr_text>", re.IGNORECASE)
_ROLE_WORDS = re.compile(r"\s*(system|user|assistant)\s*[.!]?\s*", re.IGNORECASE)
# Credits subtitle files end with, in the languages they came up in; matched anywhere.
_FALSE_CAPTIONS = re.compile(
    r"altyaz[ıi]\s*m\.?\s*k\.?|izlediğiniz için teşekkür|thanks for watching|"
    r"thank you for watching|subtitles by|untertitel (?:im auftrag|von)|"
    r"sous-titrage|sous-titres réalisés|subtítulos (?:realizados|por)|amara\.org",
    re.IGNORECASE,
)
# A loop nobody speaks, its words compared without case or punctuation: one word ten or more times
# in a row, two words five or more, three words four or more, four to eight words three or more
# ("Gol! Gol! Gol! Gol! Gol!" is a commentator and shows as said; "iki bin on yedide, iki bin on
# yedide, iki bin on yedide!" over a song is the model, 2026-11-07).
_LOOP_TIMES = {1: 10, 2: 5, 3: 4}  # four to eight words: 3
_WORD = re.compile(r"[^\w]+")


def _looped(text: str) -> bool:
    words = [w for w in (_WORD.sub("", t.lower()) for t in text.split()) if w]
    for size in range(1, 9):
        need = _LOOP_TIMES.get(size, 3)
        for start in range(0, len(words) - size * need + 1):
            phrase = words[start : start + size]
            times, at = 1, start + size
            while words[at : at + size] == phrase:
                times, at = times + 1, at + size
            if times >= need:
                return True
    return False


def token_cap(seconds: float, language: str | None = "") -> int:
    """The most tokens a piece of `seconds` of speech in `language` (Qwen3-ASR's English name;
    None: not known yet) may produce."""
    rate = _UNKNOWN if language is None else _DENSE.get(language, 1.0)
    return int(TOKENS_BASE + TOKENS_PER_S * rate * max(0.0, seconds))


def clean(text: str) -> str:
    """`text` without what nobody said; "" when nothing is left."""
    text = " ".join(_HEADING.sub("", text).replace("<asr_text>", " ").split())
    if not any(c.isalnum() for c in text) or _ROLE_WORDS.fullmatch(text):
        return ""  # nothing said (a lone "." over music), or the prompt's own words
    if _FALSE_CAPTIONS.search(text):
        return ""
    if _looped(text):
        return ""  # the model looping over music or noise: nothing of it was said
    return text.strip()
