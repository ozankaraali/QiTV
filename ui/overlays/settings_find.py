"""Typing finds a setting (Settings in the drawer, plan 3): what the find looks through and what
it finds (pure: no widgets).

It looks through every section by its title, and every row of every section and of each
layer a row opens, however deep, by the row's title and its help as they show (translated; a
provider's name as given), folded as the channel list's find folds them (services.find.words:
case and accents aside, "ı" as "i", "ş" as "s"). Never by what a row holds: a text row's text,
a secret, a stepper's value, its note or its sub line are not looked at. A row that opens a
card has no rows to look through (its card is made when it opens).

A row matches when every word typed is in its title or its help, whole or as a part of a word;
a section, when every word is in its title. Those with every word in the title come first, then
those the help matched; each in Settings' order (a section before its rows). A find typed inside
a layer lists that layer's rows first (2.13.1), and a long list of names (a `found_inside`
layer: Free channels' countries) is looked through only by a find typed inside it: a country
typed in Free radio is Free radio's, and never floods a find typed elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Optional

from services.find import words
from services.i18n import translate
from services.settings_model import OPENS, Row, Section
from ui.overlays.drawer_look import SEPARATOR
from ui.overlays.settings_layout import Item, title_of

# A section the find found: its row's key is this, then the section's key (never a row's key).
SECTION_FOUND = "section:"


@dataclass(frozen=True)
class Target:
    """A row the find can go to: its section (an index of the sections), the rows opened as
    layers on the way to it (an index each, from the section's rows) and its index among its
    layer's rows (-1: the section itself, `row` standing for it); its title as shown and where
    it is ("Providers › Demo TV"; "" for a section); and the folded words of its title and its
    help, a space around each ("  live latency  "). `inside`: its layer is found only from
    inside it (Section.found_inside)."""

    section: int
    opened: tuple[int, ...]
    index: int
    row: Row
    title: str
    where: str
    title_words: str
    help_words: str
    inside: bool = False


def _shown_name(section: Section) -> str:
    return section.text if section.literal else translate(section.text)


def _spaced(text: str) -> str:
    return f" {' '.join(words(text))} "


def _targets(
    at: int, opened: tuple[int, ...], rows: Sequence[Row], where: str, inside: bool = False
) -> Iterator[Target]:
    for index, row in enumerate(rows):
        title = title_of(Item(row.key, row.text, row=row))
        help_text = translate(row.help) if row.help else ""
        yield Target(
            at, opened, index, row, title, where, _spaced(title), _spaced(help_text), inside
        )
        layer = row.section
        if layer is not None:
            inner = SEPARATOR.join((where, _shown_name(layer)))
            yield from _targets(at, opened + (index,), layer.rows, inner, layer.found_inside)


def find_index(sections: Sequence[Section]) -> list[Target]:
    """Every section and row the find looks through, in Settings' order: each section, then its
    rows, a row that opens a layer followed by the layer's."""
    found: list[Target] = []
    for at, section in enumerate(sections):
        name = _shown_name(section)
        itself = Row(SECTION_FOUND + section.key, name, kind=OPENS, literal=True)
        found.append(Target(at, (), -1, itself, name, "", _spaced(name), _spaced("")))
        found.extend(_targets(at, (), section.rows, name))
    return found


def find(
    targets: Sequence[Target], text: str, here: Optional[tuple[int, tuple[int, ...]]] = None
) -> list[Target]:
    """The rows of `targets` (find_index) that have every word of `text` in their title or
    their help: those whose title has them all first. None for a text without a word. `here`
    is the layer the find was typed in ((section, opened), as a Target's; None: a section):
    its rows come first, and only there are a `found_inside` layer's rows looked through."""
    typed = words(text)
    if not typed:
        return []
    titled, helped = [], []
    for target in targets:
        if target.inside and (target.section, target.opened) != here:
            continue
        if all(word in target.title_words for word in typed):
            titled.append(target)
        elif all(word in target.title_words or word in target.help_words for word in typed):
            helped.append(target)
    found = titled + helped
    if here is None:
        return found
    ours = [target for target in found if (target.section, target.opened) == here]
    return ours + [target for target in found if (target.section, target.opened) != here]
