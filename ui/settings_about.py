"""Settings' Keys and About QiTV (2.13.1: the Help menu's two windows before): their rows, and
what the links among them do.

Keys lists ui/keymap.py's SHORTCUTS table, grouped as the README's key tables are
(scripts/readme_keys.py): a row for each screen, under its title, opening a layer of lines to
read (Section.browse: the keys scroll it, nothing in it acts), one for each entry: what the keys
do, and the keys as this computer names them (⌘ and ↑ on a Mac, "0–9" for a range).

About QiTV shows QiTV's logo, its version and what it is, the credits (what QiTV uses and works
with), TMDB's notice with TMDB's logo under it (TMDB's terms ask for both in About, the logo less
prominent than QiTV's own), then the links: Credits and licenses (THIRD-PARTY-NOTICES.md), the
project's page and Report an issue, each opened in the browser. The texts are the old window's.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QKeySequence

from services import system_open
from services.config_manager import get_app_version
from services.i18n import N_, translate
from services.settings_model import ACTION, INFO, Row, Section
from ui.keymap import SHORTCUT_TITLES, SHORTCUTS

KEYS_PREFIX = "keys/"
ABOUT_PREFIX = "about/"
NOTICES_ROW = ABOUT_PREFIX + "notices"
HOME_ROW = ABOUT_PREFIX + "home"
ISSUES_ROW = ABOUT_PREFIX + "issues"

HOME_URL = "https://github.com/ozankaraali/QiTV"
ISSUES_URL = f"{HOME_URL}/issues"
NOTICES_URL = f"{HOME_URL}/blob/main/THIRD-PARTY-NOTICES.md"
# i18n: under QiTV's name and version in Settings › About QiTV: what QiTV is
TAGLINE = N_("A TV-first IPTV player for STB portals, Xtream and M3U playlists.")
CREDITS_TEXT = N_(
    # i18n: About QiTV's credits; the names stay as they are
    "Plays and casts with VLC, built with Qt and Python. Subtitles come from OpenSubtitles,"
    " free channels from iptv-org, and film and series details from TMDB. Works with Jellyfin,"
    " Emby and Plex servers and with Chromecast; Chromecast is a trademark of Google LLC, and"
    " QiTV is not affiliated with Jellyfin, Emby, Plex or Google."
)
# i18n: a link in About QiTV, after its credits: THIRD-PARTY-NOTICES.md, in the browser
NOTICES_LABEL = N_("Credits and licenses")
REPORT_TEXT = N_("Report an issue")  # i18n: a link in About QiTV, to the project's issues
# 2.8: TMDB's terms ask for this notice (their template, with the product's name) and their
# logo (assets/tmdb.png) in About, the logo less prominent than QiTV's own; they set no size.
# i18n: TMDB's terms ask for this sentence: translate it faithfully
TMDB_NOTICE = N_(
    "QiTV uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by"
    " TMDB."
)
TMDB_LOGO_FILE = "tmdb.png"
LOGO_U = 5.0  # QiTV's logo (64 px at 1280, as the old window's)
TMDB_LOGO_U = 0.7  # TMDB's (9 px at 1280, as the old window's), far less prominent
_LINKS = {NOTICES_ROW: NOTICES_URL, HOME_ROW: HOME_URL, ISSUES_ROW: ISSUES_URL}


def key_label(keys: str) -> str:
    """How a SHORTCUTS entry's keys read here: native names (⌘, ↑ on macOS), "0–9" ranges."""
    labels = []
    for part in keys.split(", "):
        first, ellipsis, last = part.partition("…")
        if ellipsis and len(first) == 1 and len(last) == 1:
            labels.append(f"{first}–{last}")
        else:
            labels.append(QKeySequence(part).toString(QKeySequence.SequenceFormat.NativeText))
    return " / ".join(labels)


def keys_rows() -> tuple[Row, ...]:
    """Keys' rows: each screen's, in the table's order, opening its keys to read."""
    rows = []
    for context, entries in SHORTCUTS.items():
        title = SHORTCUT_TITLES[context]
        lines = tuple(
            Row(f"{KEYS_PREFIX}{context}/{n}", what, kind=INFO, note=key_label(keys), whole=True)
            for n, (keys, what) in enumerate(entries)
        )
        layer = Section(f"{KEYS_PREFIX}{context}", title, rows=lines, browse=True)
        rows.append(Row(f"{KEYS_PREFIX}{context}", title, section=layer))
    return tuple(rows)


def about_rows() -> tuple[Row, ...]:
    """About QiTV's rows, as the old window showed them: the texts above, the links under them
    (the keys reach the links; the lines above the first show with it)."""
    return (
        Row(ABOUT_PREFIX + "logo", "", kind=INFO, literal=True, image="qitv", image_u=LOGO_U),
        Row(
            ABOUT_PREFIX + "version",
            "QiTV",
            kind=INFO,
            literal=True,
            note=get_app_version(),
            sub=translate(TAGLINE),
        ),
        Row(ABOUT_PREFIX + "credits", CREDITS_TEXT, kind=INFO, whole=True),
        Row(ABOUT_PREFIX + "tmdb", TMDB_NOTICE, kind=INFO, whole=True),
        Row(
            ABOUT_PREFIX + "tmdb_logo",
            "",
            kind=INFO,
            literal=True,
            image=TMDB_LOGO_FILE,
            image_u=TMDB_LOGO_U,
        ),
        Row(NOTICES_ROW, NOTICES_LABEL, kind=ACTION, icon="external"),
        Row(HOME_ROW, HOME_URL, kind=ACTION, literal=True, icon="external"),
        Row(ISSUES_ROW, REPORT_TEXT, kind=ACTION, icon="external"),
    )


class AboutRows:
    """Keys' and About's rows' side, for the Settings controller (`keys/…`, `about/…`): no value
    of their own (their notes show), nothing to step or edit, and About's links open in the
    browser."""

    def __init__(self, settings: Any) -> None:
        settings.register(KEYS_PREFIX, self)
        settings.register(ABOUT_PREFIX, self)

    def keys(self) -> tuple[Row, ...]:
        return keys_rows()

    def about(self) -> tuple[Row, ...]:
        return about_rows()

    def value(self, row: Row) -> Any:
        return ""

    def step(self, row: Row, delta: int) -> None:
        """No row of theirs steps."""

    def edited(self, row: Row, text: str) -> None:
        """No row of theirs is a text."""

    def chosen(self, row: Row) -> None:
        url = _LINKS.get(row.key)
        if url:
            system_open.open_url(url)
