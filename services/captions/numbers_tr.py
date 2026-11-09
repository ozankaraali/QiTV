"""Turkish captions' numbers written as Turkish subtitles write them (a dev trial: the speech
models write every number in words, "iki bin yediden", as they were trained). Digits for 11 and
up ("on dörtte" -> "14'te"), years ("iki bin yedi" -> "2007"), percentages ("yüzde beş" -> "%5"),
clock times after "saat" ("saat on beş otuzda" -> "saat 15.30'da"), days with a month ("yirmi üç
Nisan" -> "23 Nisan"), decimals ("üç virgül beş" -> "3,5"), ordinals from 11 ("on birinci" ->
"11.") and ranges ("on beş yirmi gün" -> "15-20 gün"). Words stay for 1 to 10 on their own
("üç kişi", "beşinci") and for units, always (the owner: unit shortening "1 dk" is not wanted on
TV). Some number words are everyday words too: "bir" (a), "yüz" (face), "bin" (get on), "altı"
(under), "yedi" (ate), "on" with a suffix (onu, ona: him, her). Alone they never turn into
digits; only inside a longer number ("on dört", "iki yüz", "üç bin"). A suffix keeps its
apostrophe and its own sound ("dörtte" -> "4'te" style: "14'te"). Standard library only."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional

UNITS = {
    "sıfır": 0,
    "bir": 1,
    "iki": 2,
    "üç": 3,
    "dört": 4,
    "beş": 5,
    "altı": 6,
    "yedi": 7,
    "sekiz": 8,
    "dokuz": 9,
}
TENS = {
    "on": 10,
    "yirmi": 20,
    "otuz": 30,
    "kırk": 40,
    "elli": 50,
    "altmış": 60,
    "yetmiş": 70,
    "seksen": 80,
    "doksan": 90,
}
SCALES = {"bin": 1_000, "milyon": 1_000_000, "milyar": 1_000_000_000, "trilyon": 10**12}
HUNDRED = "yüz"
# a stem before a suffix that begins with a vowel: dört -> dörd- (dördü, dörde, dördüncü)
SOFT = {"dörd": "dört"}
STEMS = sorted([*UNITS, *TENS, *SCALES, HUNDRED, *SOFT], key=len, reverse=True)
# the suffixes Turkish puts after a number (case, possessive, plural, copula, -lik, -li), and
# the ordinal one; anything else ("altın", "binlerce", "Beşiktaş") is another word
SUFFIX = re.compile(
    r"(?:y?[aeıiuü]|n?[ıiuü]n|n[ae]|[dt][ae]|[dt][ae]n|[dt][ae]ki|y?l[ae]|l[ae]r|l[ae]r[ıi]"
    r"|l[ae]r[dt][ae]|l[ae]r[dt][ae]n|l[ıiuü]k|l[ıiuü]|s[ıiuü]|s[ıiuü]n[ıiuü]|[ıiuü]n[ıiuü]"
    r"|[dt][ıiuü]r|[dt][ıiuü])"
)
ORDINAL = re.compile(r"[ıiuü]?nc[ıiuü]")
MONTHS = (
    "ocak",
    "şubat",
    "mart",
    "nisan",
    "mayıs",
    "haziran",
    "temmuz",
    "ağustos",
    "eylül",
    "ekim",
    "kasım",
    "aralık",
)
CLOCK_AFTER = frozenset(
    (
        "pazartesi",
        "salı",
        "çarşamba",
        "perşembe",
        "cuma",
        "cumartesi",
        "pazar",
        "bugün",
        "yarın",
        "sabah",
        "öğle",
        "öğlen",
        "akşam",
        "gece",
    )
)  # a clock time may follow these without "saat" ("pazartesi 21.30'da")
_LEAD = "\"'“‘(«"
_TRAIL = ".,;:!?…\"'”’)»"


def _lower(word: str) -> str:
    return word.replace("İ", "i").replace("I", "ı").lower()


@dataclass
class _Word:
    lead: str  # punctuation before the word
    body: str
    trail: str  # punctuation after it
    kind: Optional[str] = None  # unit, ten, hundred, scale (a number word), else None
    value: int = 0
    suffix: str = ""  # what follows the number in the same word
    ordinal: bool = False


def _split(token: str) -> _Word:
    start, end = 0, len(token)
    while start < end and token[start] in _LEAD:
        start += 1
    while end > start and token[end - 1] in _TRAIL:
        end -= 1
    word = _Word(token[:start], token[start:end], token[end:])
    low = _lower(word.body)
    for stem in STEMS:
        if not low.startswith(stem):
            continue
        rest = low[len(stem) :]
        name = SOFT.get(stem, stem)
        if stem in SOFT and not rest[:1] in tuple("ıiuü"):
            continue  # dörd- only before a vowel
        if rest and ORDINAL.fullmatch(rest):
            word.ordinal = True
        elif rest and not SUFFIX.fullmatch(rest):
            continue
        word.suffix = word.body[len(stem) :]
        if name in UNITS:
            word.kind, word.value = "unit", UNITS[name]
        elif name in TENS:
            word.kind, word.value = "ten", TENS[name]
        elif name == HUNDRED:
            word.kind, word.value = "hundred", 100
        else:
            word.kind, word.value = "scale", SCALES[name]
        return word
    return word


@dataclass
class _Number:
    start: int  # index of its first word
    end: int  # one past its last word
    value: int
    suffix: str
    ordinal: bool


def _number(words: list[_Word], i: int) -> Optional[_Number]:
    """The longest number that begins at words[i] (a suffix or punctuation ends one)."""
    total, group = 0, 0
    units = tens = hundred = False
    last_scale = None
    end = i
    for k in range(i, len(words)):
        w = words[k]
        if w.kind is None or (k > i and words[k - 1].trail):
            break
        if w.kind == "unit":
            if units or (w.value == 0 and k > i):
                break
            group += w.value
            units = True
        elif w.kind == "ten":
            if tens or units:
                break
            group += w.value
            tens = True
        elif w.kind == "hundred":
            if hundred or tens:
                break
            group = (group or 1) * 100
            hundred, units = True, False
        else:  # scale
            if last_scale is not None and w.value >= last_scale:
                break
            total += (group or 1) * w.value
            group, units, tens, hundred, last_scale = 0, False, False, False, w.value
        end = k + 1
        if w.suffix or w.ordinal:
            break
    if end == i:
        return None
    last = words[end - 1]
    return _Number(i, end, total + group, last.suffix, last.ordinal)


_SCALE_WORDS = ((10**12, "trilyon"), (10**9, "milyar"), (10**6, "milyon"), (1000, "bin"))


def _shown(number: _Number) -> str:
    """A number as news captions write it (the owner: the dots in "7.000" read slowly): years
    and numbers under 1,000 in digits; round thousands, millions and billions with their word
    ("7 bin", "250 bin", "1 milyon", "2,5 milyon", a suffix straight on: "7 binde"); other large
    numbers in digits, dotted only from five digits on ("1510", "12.500")."""
    v = number.value
    if not number.ordinal and not 1900 <= v <= 2100:
        for scale, word in _SCALE_WORDS:
            if v >= scale and v % scale == 0:
                return f"{v // scale} {word}{number.suffix}"
            if v >= scale >= 10**6 and v % (scale // 10) == 0:
                return f"{v // scale},{v % scale // (scale // 10)} {word}{number.suffix}"
    text = str(v) if v < 10_000 or 1900 <= v <= 2100 else f"{v:,}".replace(",", ".")
    return text + _tail(number)


def _tail(number: _Number) -> str:
    if number.ordinal:
        return "."
    return f"'{number.suffix}" if number.suffix else ""


def _ambiguous(words: list[_Word], number: _Number) -> bool:
    """A lone word that is an everyday word too, with or without a suffix: yüz (face: "yüzü"),
    bin (get on), bir (a), yetmiş (yetmek: "gücünüz yetmiş" showed as "gücünüz 70", the owner,
    2026-11-07). The others that are (on, altı, yedi) are 10 and under: words anyway."""
    if number.end - number.start != 1:
        return False
    low = _lower(words[number.start].body)
    return any(low.startswith(stem) for stem in ("bir", HUNDRED, "bin", "yetmiş"))


def written(text: str) -> str:
    """`text` with its numbers written as Turkish subtitles write them."""
    tokens = text.split(" ")
    words = [_split(t) for t in tokens]
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        low = _lower(w.body)
        # yüzde N: a percentage (any N)
        if low == "yüzde" and not w.trail and i + 1 < len(words):
            n = _number(words, i + 1)
            if n is not None:
                last = words[n.end - 1]
                out.append(f"{w.lead}%{n.value}{_tail(n)}{last.trail}")
                i = n.end
                continue
        # saat H[ M]: a clock time (any H)
        if low == "saat" and not w.trail and i + 1 < len(words):
            n = _number(words, i + 1)
            if n is not None and n.value <= 24 and not n.ordinal:
                out.append(w.lead + w.body + w.trail)
                m = None
                if not n.suffix and not words[n.end - 1].trail and n.end < len(words):
                    m = _number(words, n.end)
                    if m is not None and (m.value > 59 or m.ordinal):
                        m = None
                if m is not None:
                    last = words[m.end - 1]
                    out.append(f"{n.value}.{m.value:02d}{_tail(m)}{last.trail}")
                    i = m.end
                else:
                    last = words[n.end - 1]
                    out.append(f"{n.value}{_tail(n)}{last.trail}")
                    i = n.end
                continue
        # DAY H M: a clock time after a day or a time of day, its minutes ending the phrase
        # ("pazartesi yirmi bir otuzda" -> "pazartesi 21.30'da", not the range "21-30'da":
        # the owner's TV promo, 2026-11-09)
        if i > 0 and not words[i - 1].trail and _lower(words[i - 1].body) in CLOCK_AFTER:
            n = _number(words, i)
            if (
                n is not None
                and n.value <= 24
                and not n.ordinal
                and not n.suffix
                and not words[n.end - 1].trail
                and n.end < len(words)
            ):
                m = _number(words, n.end)
                if (
                    m is not None
                    and 10 <= m.value <= 59
                    and not m.ordinal
                    and (m.suffix or words[m.end - 1].trail or m.end == len(words))
                ):
                    last = words[m.end - 1]
                    out.append(f"{w.lead}{n.value}.{m.value:02d}{_tail(m)}{last.trail}")
                    i = m.end
                    continue
        n = _number(words, i)
        if n is None:
            out.append(w.lead + w.body + w.trail)
            i += 1
            continue
        first, last = words[n.start], words[n.end - 1]
        after = words[n.end] if n.end < len(words) else None
        month = after is not None and not last.trail and not n.suffix and not n.ordinal
        month = month and 1 <= n.value <= 31 and _lower(after.body).split("'")[0] in MONTHS
        # N virgül M: a decimal
        if (
            after is not None
            and _lower(after.body) == "virgül"
            and not last.trail
            and not n.suffix
            and not after.trail
            and n.end + 1 < len(words)
        ):
            m = _number(words, n.end + 1)
            if m is not None and not m.ordinal:
                # "seksen altı virgül beş milyon": 86,5 milyon, not 86,5000000 (the owner,
                # 2026-11-07): the scale word that ends it stays a word, its suffix on
                scale = m.end - 1
                if m.end - (n.end + 1) > 1 and words[scale].kind == "scale":
                    frac = _number(words[:scale], n.end + 1)
                    if frac is not None and frac.end == scale and not frac.suffix:
                        sw = words[scale]
                        out.append(f"{first.lead}{n.value},{frac.value} {sw.body}{sw.trail}")
                        i = m.end
                        continue
                tail = words[m.end - 1]
                out.append(f"{first.lead}{n.value},{m.value}{_tail(m)}{tail.trail}")
                i = m.end
                continue
        if month or (n.value >= 11 and not _ambiguous(words, n)):  # "bir Mayıs": 1 Mayıs
            text_n = f"{first.lead}{_shown(n)}"
            i = n.end
            # N M: a range when M follows at once and is larger ("on beş yirmi gün")
            if not last.trail and not n.suffix and not n.ordinal and i < len(words):
                m = _number(words, i)
                if m is not None and m.value > n.value and not m.ordinal:
                    tail = words[m.end - 1]
                    out.append(f"{text_n}-{_shown(m)}{tail.trail}")
                    i = m.end
                    continue
            out.append(text_n + last.trail)
            continue
        out.extend(x.lead + x.body + x.trail for x in words[n.start : n.end])
        i = n.end
    return " ".join(out)
