"""What a speech model writes that nobody said (2.9, AI captions), kept off the screen.

Measured on broadcasts (2026-11-02, Qwen3-ASR and Whisper on a drama with music and a news
channel): over music or silence the models write a phrase again and again until their token
cap ("Sen öyle." 40 times), the words of their own prompt ("system."), or the credits their
training subtitles ended with ("Altyazı M.K.", "İzlediğiniz için teşekkür ederim.", "Thanks
for watching"). `clean()` keeps a phrase repeated in a row once and drops the rest;
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
# A phrase of 1 to 8 words (with its punctuation) said three or more times in a row.
_REPEATS = re.compile(r"((?:\S+\s+){0,7}?\S+?[.!?,…]?)(?:\s+\1){2,}", re.IGNORECASE)


def token_cap(seconds: float) -> int:
    """The most tokens a piece of `seconds` of speech may produce."""
    return int(TOKENS_BASE + TOKENS_PER_S * max(0.0, seconds))


def clean(text: str) -> str:
    """`text` without what nobody said; "" when nothing is left."""
    text = " ".join(_HEADING.sub("", text).replace("<asr_text>", " ").split())
    if not any(c.isalnum() for c in text) or _ROLE_WORDS.fullmatch(text):
        return ""  # nothing said (a lone "." over music), or the prompt's own words
    if _FALSE_CAPTIONS.search(text):
        return ""
    previous = None
    while previous != text:  # a loop of loops
        previous = text
        text = _REPEATS.sub(r"\1", text)
    return text.strip()
