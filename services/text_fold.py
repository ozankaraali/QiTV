"""Accent- and case-insensitive text folding shared by filters and search."""

import unicodedata


class _KeepBases(dict):
    """A str.translate table that drops combining marks: each code point is looked up once.

    str.translate calls __missing__ only for a code point it has not seen; the answer is kept,
    so a title is then translated by dictionary lookups alone (the per-character Python loop
    this replaces cost most of a non-ASCII fold).
    """

    def __missing__(self, code: int) -> int | None:
        kept = None if unicodedata.combining(chr(code)) else code
        self[code] = kept
        return kept


_KEEP_BASES = _KeepBases()


def fold(text: str) -> str:
    """Casefold, strip accents and map dotless i, so "ist" matches "İstanbul" and "cafe" "Café"."""
    if text.isascii():  # no accents and no dotless i: casefold is the whole job
        return text.casefold()
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return decomposed.translate(_KEEP_BASES).replace("ı", "i")
