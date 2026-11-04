"""Which of the speech model's languages a caption is written in (2.9, AI captions).

Left to guess the language (no audio track, channel or viewer names it), Qwen3-ASR guesses per
piece, and on a short or musical one it guesses wrong: a Turkish drama's pieces came back
Chinese, Korean, Arabic and Russian now and then (2026-11-03), and sherpa-onnx returns no
language with the text. The captions process therefore tells the language from the words
(`guess`): the script first (Han, kana, Hangul, Cyrillic, Arabic, Thai, Greek, Devanagari),
then, for Latin, the letters only some languages use and their most common short words.
`Votes` locks it once the pieces agree, and from then on the model is told the language.
Only the standard library is used.
"""

from __future__ import annotations

from collections import Counter, deque
import re
from typing import Optional
import unicodedata

# A language's most common short words (lowercase), and the letters that mark it.
_WORDS = {
    "English": "the and is you to of a in that it for on with this are was be have not we they"
    " what there here have has been will just know right yes okay",
    "Turkish": "ve bir bu için ile de da çok ne var mı mi mu mü ama ben sen o gibi daha şey değil"
    " yok ya zaten evet hayır şimdi iyi kadar sonra nasıl neden bana sana onu bunu şu hep her"
    " hiç tamam abi hadi gel hoş böyle oldu olur olan diye bile çünkü",
    "German": "und der die das ist nicht ich du sie es ein eine zu mit auf den dem auch sich"
    " wir ja nein was wie aber noch schon hier jetzt gut doch mal",
    "French": "le la les et est que pas je vous il des un une en du ce qui dans pour sur"
    " oui non mais nous avec tout bien ici très suis",
    "Spanish": "que de el la y en los las un una es por con no se lo para del al más"
    " sí pero muy bien aquí ahora todo está estoy",
    "Portuguese": "que de o a e do da em um uma não os as para com por mais se na no",
    "Italian": "che di il la e un una non per è sono lo gli le del della con ma si",
    "Dutch": "de het een en van is dat niet ik je die op te zijn met voor maar",
    "Swedish": "och att det är en jag som på inte för med har de till av om",
    "Danish": "og at det er en jeg som på ikke for med har de til af den",
    "Finnish": "ja on ei se että hän mutta kun niin mitä tämä oli minä sinä",
    "Polish": "i w nie na to jest że się z do jak co ale tak już",
    "Czech": "a je to se na že v nebo jak ale co jsem tak by už",
    "Hungarian": "a az és hogy nem is egy de van meg csak ez el",
    "Romanian": "și de în la nu că este cu o un pe se mai ce",
    "Indonesian": "yang dan di ini itu tidak dengan untuk saya ada akan dari",
    "Malay": "yang dan di ini itu tidak dengan untuk saya ada akan dari boleh",
    "Vietnamese": "và của là có không một người này được cho những",
    "Filipino": "ang ng sa na mga ay at hindi ko ito siya ka",
}
_LETTERS = {
    "Turkish": "ıİğĞşŞ",
    "German": "ßẞ",
    "Polish": "ąęłńśźżĄĘŁŃŚŹŻ",
    "Czech": "ěřůĚŘŮ",
    "Hungarian": "őűŐŰ",
    "Romanian": "șțăȘȚĂ",
    "Spanish": "ñ¿¡Ñ",
    "Portuguese": "ãõÃÕ",
    "Vietnamese": "ơưđƠƯĐạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ",
    "Danish": "æøÆØ",
    "French": "œŒ",
}
_WORD_SETS = {name: set(words.split()) for name, words in _WORDS.items()}
_SPLIT = re.compile(r"[^\w']+", re.UNICODE)


def _script(text: str) -> Counter:
    counts: Counter = Counter()
    for char in text:
        code = ord(char)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            counts["Han"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["Kana"] += 1
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            counts["Hangul"] += 1
        elif 0x0400 <= code <= 0x04FF:
            counts["Cyrillic"] += 1
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            counts["Arabic"] += 1
        elif 0x0E00 <= code <= 0x0E7F:
            counts["Thai"] += 1
        elif 0x0370 <= code <= 0x03FF:
            counts["Greek"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["Devanagari"] += 1
        elif char.isalpha():
            counts["Latin"] += 1
    return counts


# The scripts each of Qwen's languages is written in (Latin for the rest).
_SCRIPTS = {
    "Ukrainian": {"Cyrillic"},  # 2.9.3: Parakeet's own (the rest of them: Latin)
    "Bulgarian": {"Cyrillic"},
    "Chinese": {"Han"},
    "Cantonese": {"Han"},
    "Japanese": {"Han", "Kana"},
    "Korean": {"Hangul", "Han"},
    "Russian": {"Cyrillic"},
    "Macedonian": {"Cyrillic"},
    "Arabic": {"Arabic"},
    "Persian": {"Arabic"},
    "Thai": {"Thai"},
    "Greek": {"Greek"},
    "Hindi": {"Devanagari"},
}


def written_in(text: str, language: str) -> bool:
    """`text` may be in `language` (one of Qwen's names): False only when most of its letters
    are in a script the language is not written in (2.9: locked to Turkish, the model wrote a
    line of Korean speech in Urdu's letters)."""
    counts = _script(text)
    letters = sum(counts.values())
    if letters < 4:
        return True
    own = _SCRIPTS.get(language, {"Latin"})
    foreign = sum(n for script, n in counts.items() if script not in own)
    return foreign < 0.6 * letters


def guess(text: str) -> Optional[str]:
    """The language `text` is written in (one of Qwen's names), or None when it says too
    little to tell."""
    counts = _script(text)
    letters = sum(counts.values())
    if letters < 4:
        return None
    script, n = counts.most_common(1)[0]
    if n < 0.6 * letters:
        return None  # a mix: no answer
    if script == "Han":
        return "Japanese" if counts["Kana"] else "Chinese"
    if script == "Kana":
        return "Japanese"
    if script == "Hangul":
        return "Korean"
    if script == "Cyrillic":
        return "Macedonian" if re.search(r"[ѓќѕљњџѐѝ]", text.lower()) else "Russian"
    if script == "Arabic":
        return "Persian" if re.search(r"[پچژگی]", text) else "Arabic"
    if script == "Thai":
        return "Thai"
    if script == "Greek":
        return "Greek"
    if script == "Devanagari":
        return "Hindi"
    return _latin(text)


def _latin(text: str) -> Optional[str]:
    scores: Counter = Counter()
    for name, marks in _LETTERS.items():
        hits = sum(text.count(m) for m in marks)
        if hits:
            scores[name] += 2 * hits
    words = [w for w in _SPLIT.split(unicodedata.normalize("NFC", text.lower())) if w]
    for word in words:
        for name, common in _WORD_SETS.items():
            if word in common:
                scores[name] += 1
    if not scores:
        return None
    ranked = scores.most_common(2)
    best, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if score < 2 or score < second * 1.5:
        return None  # too little, or two languages alike (Malay and Indonesian, say)
    return str(best)


class Votes:
    """The language the pieces keep coming back in: locked once `NEEDED` of the last `WINDOW`
    agree. Before that, `leading()` is the one most of them say (two at least)."""

    NEEDED = 3
    WINDOW = 4

    def __init__(self) -> None:
        self._recent: deque[str] = deque(maxlen=self.WINDOW)
        self.locked: Optional[str] = None

    def add(self, language: Optional[str]) -> Optional[str]:
        """Count a piece's language; the language when this one locks it, else None."""
        if self.locked is not None or language is None:
            return None
        self._recent.append(language)
        name, n = Counter(self._recent).most_common(1)[0]
        if n >= self.NEEDED:
            self.locked = name
            return name
        return None

    def leading(self) -> Optional[str]:
        if not self._recent:
            return None
        name, n = Counter(self._recent).most_common(1)[0]
        return name if n >= 2 else None
