"""Settings › Providers › Network streams (plan 4): the links that played, kept apart from the
providers (network_streams.json: never one of them, nor counted as one).

Providers lists Network streams after the providers once a link is kept, how many under it
("4 links"). Its layer lists each link by its name (as the viewer gave it: never translated)
and, under it, what the channel list says of it ("Live", "Film · stopped at 41:12"), or why
it failed last time, in the warning colour; then ＋ Open a link…, which opens File → Open
Link…'s card over Settings. A link opens a layer of its own:

- Play: it plays as its row in the channel list plays it (as it was kept, as the stream of
  whoever's it is), and Settings closes. With no provider the channel list never opens: its
  links play from here. From Settings over the Library (2.14), on the player page;
- Name, edited in place (blank: one made of its address);
- Address, edited in place and shown as kept. Its stream with a fresh token is kept at once,
  under the same id (its name, resume point and ⟲ Recent's row follow); another stream is
  asked first, in a card: Keep as a new link (the default: this one stays as it was), or
  Replace (this link plays it from now on, its id, name, place and ⟲ Recent's row kept; where
  a film stopped is forgotten: it was another film). What is no one stream's link (a whole
  provider's, or none) is said, and nothing changes;
- Kind and Last opened, only shown, and why it did not play last time while it says so: a
  line of its own, whole, in the warning colour (the keys and the phone have no tooltip);
- Remove…, asked first in a card.

Every change is the network player's (ui.network_player): written on a worker, one write at a
time, and the channel list's Network streams follows (`changed`), as do these rows, built
again whenever the links change, here or elsewhere (one that played, read from the file).

Keys: `provider/@links` for the row and its layer, `provider/@links/<id>` for a link's layer
and `provider/@links/<id>/<field>` for its rows (its id quoted), `provider/@links/@open` for
＋ Open a link…. No provider's key starts so (ui.settings_providers escapes a leading "@").
No address is ever logged.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote, unquote

from PySide6.QtCore import QObject

from services import i18n
from services.i18n import N_, tr_n, translate
from services.link_kind import PROBE, SITE, SITE_FILM, Provider, address, classify
from services.live_sites import media_address, page_address, site_media, site_page
from services.navigation import LinkRow
from services.network_streams import FILM, LIVE, Entry, default_name, stream_id
from services.provider_ids import NETWORK_TITLE
from services.recordings_list import short_when
from services.settings_model import (
    ACTION,
    DANGER,
    INFO,
    PROVIDER_PREFIX,
    TEXT,
    Row,
    Section,
)
from ui.network_player import NetworkPlayer
from ui.open_link import NOT_A_LINK_TEXT, PLAY_TEXT, WHOLE_TEXT
from ui.overlays.drawer import LINK_FILM_TEXT, LINK_LIVE_TEXT, link_sub
from ui.overlays.settings_card import CANCEL, CANCEL_TEXT, Button, ChoiceCard
from ui.settings_controller import SettingsController
from ui.settings_providers import REMOVE_TEXT
from ui.tv_controller import ERROR_MS

logger = logging.getLogger(__name__)

LINKS = PROVIDER_PREFIX + "@links"  # Network streams' row in Providers, and its layer
OPEN_LINK = LINKS + "/@open"  # ＋ Open a link…
# A link's rows, by the last part of their keys.
PLAY, NAME, ADDRESS, KIND, OPENED, REMOVE = "play", "name", "address", "kind", "opened", "remove"
FAILED = "failed"  # why it did not play last time (an info line, while it says so)
KEEP_NEW, REPLACE = "new", "replace"  # the address question's answers (and CANCEL)

# i18n: help under Settings → Providers → Network streams (the links opened with Open a link)
LINKS_HELP = N_("The links that played from Open a link, kept apart from the providers.")
# i18n: a row of Settings → Providers → Network streams, after a ＋: File → Open Link…'s card
OPEN_LINK_TEXT = N_("Open a link…")
NEVER_CTX = "last opened"
# i18n: a link's Last opened in Settings → Providers → Network streams: it never played
NEVER_TEXT = N_("Never", ctx="last opened")
# i18n: asked when a link's new address in Settings → Network streams is another stream
ANOTHER_STREAM_TITLE = N_("This is another stream")
ANOTHER_STREAM_BODY = N_(
    # i18n: {name} is the link edited (its name in Network streams); a token is the part of a
    # link that lets it play for a while
    "It isn't {name} with a new token, but another stream. Keep it as a new link, or replace"
    " {name}'s address?"
)
# i18n: a button: the other stream becomes a link of its own in Network streams
KEEP_NEW_TEXT = N_("Keep as a new link")
# i18n: a button: the link edited plays the other stream from now on (verb)
REPLACE_TEXT = N_("Replace")
# i18n: asked before a link goes from Settings → Providers → Network streams ({name}: its name)
REMOVE_QUESTION = N_("Remove “{name}” from Network streams?")
_NOTHING = ("", "", "", "")  # nothing asked: (what, link id, new address, its kind)


def count_text(count: int) -> str:
    """How many links Network streams keeps ("4 links")."""
    # i18n: under Network streams in Settings → Providers: how many links it keeps
    return tr_n("{n} link", "{n} links", count, n=i18n.number(count))


def link_key(key: str) -> str:
    """The link kept under `key`'s row in Network streams, and its layer's key."""
    return f"{LINKS}/{quote(key, safe='')}"


def row_key(key: str, field: str) -> str:
    """The link kept under `key`'s row `field` (PLAY, NAME …)."""
    return f"{link_key(key)}/{field}"


def _parsed(key: str) -> Optional[tuple[str, str]]:
    """(link id, field) of a link's row key ("": its own row); None for the list's own rows
    (Network streams', ＋ Open a link…)."""
    if not key.startswith(LINKS + "/") or key == OPEN_LINK:
        return None
    parts = key[len(LINKS) + 1 :].split("/", 1) + [""]
    return unquote(parts[0]), parts[1]


class LinkRows(QObject):
    """Settings' Network streams (`row`, among Providers' rows) and the handler of its keys
    (`provider/@links…`, registered with `settings`). `window` is the TV window (its Open Link
    card, its toasts); `drawer` the Settings layer the rows show in; `player` the network
    player, whose links these are and which makes every change."""

    def __init__(
        self, window: Any, settings: SettingsController, drawer: Any, player: NetworkPlayer
    ) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._drawer = drawer
        self._player = player
        self._question: Optional[ChoiceCard] = None
        self._asked = _NOTHING
        settings.register(LINKS, self)
        player.changed.connect(self._on_links_changed)

    @property
    def question(self) -> Optional[ChoiceCard]:
        """The card that asks about another stream's address and before a link goes (None
        until it first asked)."""
        return self._question

    # ---- the rows ---------------------------------------------------------------------------

    def row(self) -> Optional[Row]:
        """Network streams' row in Providers, how many links under it; None without one."""
        links = self._player.rows()
        if not links:
            return None
        rows = tuple(self._link_row(link) for link in links) + (self._open_row(),)
        layer = Section(LINKS, NETWORK_TITLE, rows=rows)
        return Row(LINKS, NETWORK_TITLE, LINKS_HELP, section=layer, sub=count_text(len(links)))

    def _link_row(self, link: LinkRow) -> Row:
        """A link by its name, what it is under it (or why it failed, in the warning colour),
        opening its own rows."""
        entry = self._player.store.get(link.id)
        name = link.name or (default_name(entry.url) if entry is not None else link.id)
        sub, warn = link_sub(link)
        key = link_key(link.id)
        layer = Section(key, name, rows=self._layer(link.id, sub if warn else ""), literal=True)
        return Row(key, name, section=layer, sub=sub, warn=warn, literal=True)

    @staticmethod
    def _open_row() -> Row:
        return Row(
            OPEN_LINK,
            OPEN_LINK_TEXT,
            # i18n: help under Settings → Providers → Network streams → Open a link
            N_("Plays one stream's link; once it plays, it's kept here."),
            kind=ACTION,
            icon="plus",
        )

    @staticmethod
    def _layer(key: str, failed: str = "") -> tuple[Row, ...]:
        """A link's rows; `failed`: why it did not play last time (as its row says it), an
        info line of its own, whole, in the warning colour (the keys and the phone have no
        tooltip to read a cut one in)."""
        why: tuple[Row, ...] = ()
        if failed:
            why = (Row(row_key(key, FAILED), failed, kind=INFO, literal=True, warn=True),)
        return (
            Row(
                row_key(key, PLAY),
                PLAY_TEXT,
                # i18n: help under a link's Play in Settings → Network streams
                N_("Plays it now, and Settings closes."),
                kind=ACTION,
                icon="play",  # its main action: ▶, in the accent
            ),
            Row(
                row_key(key, NAME),
                N_("Name"),  # i18n: a link's name in Settings → Network streams, edited in place
                # i18n: help under a link's Name in Settings → Network streams
                N_("What Network streams lists it as. Enter keeps a new name, Esc the old one."),
                kind=TEXT,
            ),
            Row(
                row_key(key, ADDRESS),
                N_("Address"),  # i18n: a link's address (its URL) in Settings → Network streams
                # i18n: help under a link's Address in Settings → Network streams
                N_("A new token for its stream is kept at once; another stream is asked first."),
                kind=TEXT,
            ),
            Row(row_key(key, KIND), N_("Kind"), kind=INFO),  # i18n: a link's kind (Live, Film)
            # i18n: when a link in Settings → Network streams last played
            Row(row_key(key, OPENED), N_("Last opened"), kind=INFO),
            *why,
            Row(
                row_key(key, REMOVE),
                REMOVE_TEXT,
                # i18n: help under a link's Remove in Settings → Network streams
                N_("Takes it off Network streams. Asked first."),
                kind=DANGER,
            ),
        )

    # ---- RowHandler -------------------------------------------------------------------------

    def value(self, row: Row) -> Any:
        found = self._found(row.key)
        if found is None:
            return None
        entry, field = found
        if field == NAME:
            return entry.name
        if field == ADDRESS:
            return entry.url  # shown as kept, never logged
        if field == KIND:
            return translate(LINK_FILM_TEXT if entry.kind == FILM else LINK_LIVE_TEXT)
        if field == OPENED:
            return short_when(entry.opened) or translate(NEVER_TEXT, ctx=NEVER_CTX)
        return None

    def step(self, row: Row, delta: int) -> None:
        """(None of its rows steps.)"""

    def edited(self, row: Row, text: str) -> None:
        found = self._found(row.key)
        if found is None:
            return
        entry, field = found
        if field == NAME:
            name = text.strip() or default_name(entry.url)
            if name != entry.name:
                self._player.rename(entry.id, name)
        elif field == ADDRESS:
            self._readdress(entry, text)

    def chosen(self, row: Row) -> None:
        if row.key == OPEN_LINK:
            self._window.open_link()  # over Settings, which has the keys again once it goes
            return
        found = self._found(row.key)
        if found is None:
            return
        entry, field = found
        if field == PLAY:
            self._window.screens.leave_library_to_play()  # 2.14: never unseen behind it
            if self._player.play_kept(entry.id):
                self._settings.close()
        elif field == REMOVE:
            self._ask_remove(entry)

    # ---- the changes ------------------------------------------------------------------------

    def _on_links_changed(self) -> None:
        """The links changed (here, one that played, the file read): while Settings shows,
        the rows again, the place kept by key (it builds them anew each time it opens)."""
        if self._settings.visible():
            self._settings.refresh_sections()

    def _readdress(self, entry: Entry, text: str) -> None:
        """The link's Address edited to `text`: its stream's fresh token at once; another
        stream asked first; no stream's link said and refused (none, or the same: as it was)."""
        url = address(text)
        kind = classify(url)
        page = site_page(url) if kind == SITE else None
        media = site_media(url) if kind == SITE_FILM else None
        if page is not None:
            url, kind = page_address(page), LIVE  # a site's channel page: kept as its page
        elif media is not None:
            url, kind = media_address(media), FILM  # 2.15: a site's video or clip, a film
        if not url or url == entry.url:
            return
        if isinstance(kind, Provider):
            self._notify(translate(WHOLE_TEXT))
            return
        if kind not in (LIVE, FILM, PROBE):
            self._notify(translate(NOT_A_LINK_TEXT))
            return
        if stream_id(url) == stream_id(entry.url):
            self._player.set_url(entry.id, url)  # the stream it plays now, a fresh token
            return
        told = str(kind) if kind in (LIVE, FILM) else ""  # a PROBE keeps the link's kind
        self._asked = (ADDRESS, entry.id, url, told)
        buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(REPLACE, translate(REPLACE_TEXT)),
            Button(KEEP_NEW, translate(KEEP_NEW_TEXT), accent=True),
        ]
        body = translate(ANOTHER_STREAM_BODY, name=entry.name)
        self._ask(translate(ANOTHER_STREAM_TITLE), body, buttons, KEEP_NEW)

    def _ask_remove(self, entry: Entry) -> None:
        self._asked = (REMOVE, entry.id, "", "")
        buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(REMOVE, translate(REMOVE_TEXT), danger=True),
        ]
        self._ask(translate(REMOVE_QUESTION, name=entry.name), "", buttons, CANCEL)

    def _ask(self, title: str, body: str, buttons: list[Button], default: str) -> None:
        if self._question is None:
            self._question = ChoiceCard(self._settings.card_parent())
            self._question.answered.connect(self._on_answered)
        self._question.ask(title, body, buttons, default)
        self._settings.show_card(self._question)

    def _on_answered(self, answer: str) -> None:
        (what, key, url, kind), self._asked = self._asked, _NOTHING
        if what == ADDRESS and answer == REPLACE:
            logger.info("Network streams: a link plays another stream from now on")
            self._player.set_url(key, url, kind)
        elif what == ADDRESS and answer == KEEP_NEW:
            logger.info("Network streams: another stream kept as a new link")
            entry = self._player.store.get(key)
            self._player.keep_new(url, kind or (entry.kind if entry is not None else LIVE))
        elif what == REMOVE and answer == REMOVE:
            logger.info("Network streams: a link removed")
            self._player.remove(key)

    # ---- what they need ---------------------------------------------------------------------

    def _found(self, key: str) -> Optional[tuple[Entry, str]]:
        """The kept link a row key is of, and the row's field; None for the list's own rows
        and a link gone meanwhile."""
        parsed = _parsed(key)
        entry = self._player.store.get(parsed[0]) if parsed is not None else None
        if parsed is None or entry is None:
            return None
        return entry, parsed[1]

    def _notify(self, text: str) -> None:
        self._window.screens.notify(text, "error", ERROR_MS)
