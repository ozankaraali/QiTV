"""Accent- and case-insensitive text folding shared by filters and search."""

import unicodedata


def fold(text: str) -> str:
    """Casefold, strip accents and map dotless i, so "ist" matches "İstanbul" and "cafe" "Café"."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.replace("ı", "i")
