"""Typing finds a setting (Settings in the drawer, plan 3): what the find looks through and what
it finds (pure: no widgets).

It looks through every row of every section and of each layer a row opens, however deep, by
the row's title and its help as they show (translated; a provider's name as given), folded as
the channel list's find folds them (services.find.words: case and accents aside, "ı" as "i",
"ş" as "s"). Never by what a row holds: a text row's text, a secret, a stepper's value, its
note or its sub line are not looked at. A row that opens a card has no rows to look through
(its card is made when it opens); a section still in the Settings window has none either.

A row matches when every word typed is in its title or its help, whole or as a part of a word.
Those with every word in the title come first, then those the help matched; each in Settings'
order.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from services.find import words
from services.i18n import translate
from services.settings_model import Row, Section
from ui.overlays.drawer_look import SEPARATOR
from ui.overlays.settings_layout import Item, title_of


@dataclass(frozen=True)
class Target:
    """A row the find can go to: its section (an index of the sections), the rows opened as
    layers on the way to it (an index each, from the section's rows) and its index among its
    layer's rows; its title as shown and where it is ("Providers › Demo TV"); and the folded
    words of its title and its help, a space around each ("  live latency  ")."""

    section: int
    opened: tuple[int, ...]
    index: int
    row: Row
    title: str
    where: str
    title_words: str
    help_words: str


def _shown_name(section: Section) -> str:
    return section.text if section.literal else translate(section.text)


def _spaced(text: str) -> str:
    return f" {' '.join(words(text))} "


def _targets(at: int, opened: tuple[int, ...], rows: Sequence[Row], where: str) -> Iterator[Target]:
    for index, row in enumerate(rows):
        title = title_of(Item(row.key, row.text, row=row))
        help_text = translate(row.help) if row.help else ""
        yield Target(at, opened, index, row, title, where, _spaced(title), _spaced(help_text))
        layer = row.section
        if layer is not None:
            inner = SEPARATOR.join((where, _shown_name(layer)))
            yield from _targets(at, opened + (index,), layer.rows, inner)


def find_index(sections: Sequence[Section]) -> list[Target]:
    """Every row the find looks through, in Settings' order: each section's rows, a row that
    opens a layer followed by the layer's."""
    found: list[Target] = []
    for at, section in enumerate(sections):
        found.extend(_targets(at, (), section.rows, _shown_name(section)))
    return found


def find(targets: Sequence[Target], text: str) -> list[Target]:
    """The rows of `targets` (find_index) that have every word of `text` in their title or
    their help: those whose title has them all first. None for a text without a word."""
    typed = words(text)
    if not typed:
        return []
    titled, helped = [], []
    for target in targets:
        if all(word in target.title_words for word in typed):
            titled.append(target)
        elif all(word in target.title_words or word in target.help_words for word in typed):
            helped.append(target)
    return titled + helped
