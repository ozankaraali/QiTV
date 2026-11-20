"""File → Open Link… and the channel list's Open a link (plan 4): one stream's link plays.

The card (LinkCard: the Settings form card over the dimmed picture, with a muted note under its
found line) has one field with Paste. As a link is typed or pasted, the line under the field
says what it is (services.link_kind.classify):

- a live stream or a film: Play hands it over, `play(url, kind)` (the network player plays it
  as a channel or as a film, and keeps it in Network streams, as the note says);
- an Xtream stream's link (/live/U/P/1.ts) is one stream too; its other way is "Add the whole
  account (host)", which hands the account to Add provider;
- a whole provider (an account's playlist or API link, a playlist's address, a portal): the
  line names its kind and host (never its login), the note says it is no one stream, and
  "Add as a provider…" hands it to Add provider, filled in, `add_provider(link)`;
- a link only its first bytes tell (an .m3u8, any other web address): no line yet; Play reads
  them first, at most 64 KB, on a worker (QiTV's user agent, Settings' TLS check). A list of
  channels turns the card into the provider offer; anything else plays with the kind they say,
  and so does a link that refused them or never answered (as LIVE: VLC tries it, and the
  player says why it failed).

While the bytes are read the card says "Checking…" and only Cancel works. Each read has its
request id and only the latest one answers: a cancelled card, or one opened again, drops a
late answer. Text that is no link: Play says so. The link is never logged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainter
from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from services.i18n import N_, translate
from services.jobs import cancel_requested
from services.lazy_import import lazy_module
from services.link_kind import (
    FILM,
    LIVE,
    PROBE,
    PROBE_BYTES,
    Kind,
    Provider,
    address,
    classify,
    probe_text,
    xtream_account,
)
from services.provider_api import CLIENT_HEADERS
from services.provider_link import M3U, STB, XTREAM, ProviderLink
from services.redact import safe_exc
from ui.overlays.base import announce
from ui.overlays.painting import Units
from ui.overlays.settings_card import (
    CANCEL,
    CANCEL_TEXT,
    FOUND,
    MIDDLE,
    PANEL,
    TITLE,
    Button,
    Field,
    FormCard,
)
from ui.theme import PALETTE, qcolor

logger = logging.getLogger(__name__)
requests = lazy_module("requests")

TITLE_TEXT = N_("Open a link")  # i18n: the title of File → Open Link…'s card (plan 4)
# i18n: under the link in File → Open Link…'s card: it is one live channel's stream
LIVE_FOUND = N_("✓ A live stream")
# i18n: under the link in File → Open Link…'s card: it is a film's file (or a recording's)
FILM_FOUND = N_("✓ A film")
# i18n: under a link: a whole Xtream account (host: its server's name), its login in the link
XTREAM_FOUND = N_("✓ Xtream at {host}, with its login")
M3U_FOUND = N_("✓ M3U playlist at {host}")  # i18n: under a link: a playlist's address
STB_FOUND = N_("✓ STB portal at {host}")  # i18n: under a link: an STB portal's address
# i18n: a muted line in File → Open Link…'s card: Network streams lists the links that played
KEPT_TEXT = N_("Once it plays, it's kept in Network streams.")
# i18n: a muted line in File → Open Link…'s card, under a provider's link
WHOLE_TEXT = N_("This is a whole provider, not one stream.")
PLAY_TEXT = N_("Play")  # i18n: File → Open Link…'s button: the link plays (verb)
# i18n: File → Open Link…'s button for a provider's link: Add provider opens, filled in
ADD_AS_PROVIDER_TEXT = N_("Add as a provider…")
# i18n: the other way under an Xtream stream's link: its whole account becomes a provider
ACCOUNT_TEXT = N_("Add the whole account ({host})")
# i18n: File → Open Link…'s card, in the warning colour, after Play: the text is no link
NOT_A_LINK_TEXT = N_("This isn't a link QiTV can open")

LINK, PLAY, PROVIDER, ACCOUNT = "link", "play", "provider", "account"  # the card's keys
NOTE = ("note", "")  # the note's place on the card
_Rects = dict[tuple[str, str], QRectF]  # where things are on the card, by (zone, key)
_FOUND = {LIVE: LIVE_FOUND, FILM: FILM_FOUND}
_PROVIDER_FOUND = {XTREAM: XTREAM_FOUND, M3U: M3U_FOUND, STB: STB_FOUND}
_NOTE_GAP_U = 0.8  # the mockup's, under the found line
_NOTE_ALPHA = 0.55
_WRAP = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)
_PROBE_TIMEOUT = (5, 10)  # connecting, then each read (seconds)
_PROBE_DEADLINE_S = 20.0  # the whole read at most: what came by then is what it says
_CHUNK = 16 * 1024


def provider_found(link: ProviderLink) -> str:
    """What a whole provider's link was found to be, at its host (never its login)."""
    return translate(_PROVIDER_FOUND.get(link.type, M3U_FOUND), host=link.name)


def read_start(url: str, verify_ssl: bool) -> str:
    """The first PROBE_BYTES of what `url` answers, as text (blocking: run it on a worker),
    asked with QiTV's user agent and `verify_ssl` (Settings' TLS check). Only that much is
    read, however long the answer (a live stream never ends), and the connection is closed
    after it; a slow answer gives what came within the deadline, a closing window what came
    so far. An HTTP error raises (requests.HTTPError), as a connection that fails does."""
    deadline = time.monotonic() + _PROBE_DEADLINE_S
    response = requests.get(
        url, headers=CLIENT_HEADERS, timeout=_PROBE_TIMEOUT, verify=verify_ssl, stream=True
    )
    try:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_content(_CHUNK):
            body += chunk
            if len(body) >= PROBE_BYTES or cancel_requested() or time.monotonic() > deadline:
                break
    finally:
        response.close()
    return bytes(body[:PROBE_BYTES]).decode("utf-8", errors="replace")


class LinkProbeJob(QObject):
    """One link's first bytes, read on the runner's thread: their text, plain data."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(self, url: str, verify_ssl: bool) -> None:
        super().__init__()
        self._url = url
        self._verify_ssl = verify_ssl

    def run(self) -> None:
        try:
            text = read_start(self._url, self._verify_ssl)
        except (requests.RequestException, OSError, ValueError) as e:
            # requests' errors name the link, its token and all: the type and host only.
            logger.info("A link's first bytes did not come: %s", safe_exc(e))
            self.error.emit(type(e).__name__)
            return
        self.finished.emit(text)


class LinkCard(FormCard):
    """The form card with a muted note under its found line (the mockup's): where a link that
    played is kept, or that the link is a whole provider."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._note = ""

    def set_form(
        self,
        title: str,
        fields: list[Field],
        found: str,
        buttons: list[Button],
        alternates: list[Button],
        note: str = "",
    ) -> None:
        """FormCard.set_form, with the note under the found line ("": none)."""
        self._note = note
        super().set_form(title, fields, found, buttons, alternates)

    def note(self) -> str:
        return self._note

    def _place(
        self, un: Units, fonts: dict[str, QFont], width: float, squeeze: float
    ) -> tuple[_Rects, float]:
        """The form's places, the note put under the found line (else under the first field)
        and everything under it moved down by its height."""
        rects, footer = super()._place(un, fonts, width, squeeze)
        if not self._note:
            return rects, footer
        under = rects.get(FOUND)
        if under is None and self._fields:
            under = rects.get(("field", self._fields[0].key))
        if under is None:
            under = rects[TITLE]
        left, right = under.left(), rects[TITLE].right()
        top = under.bottom() + un.px(_NOTE_GAP_U) * squeeze
        height = self._wrapped_height(fonts["alt"], self._note, max(1.0, right - left))
        edge, shift = under.bottom(), top + height - under.bottom()
        for name, rect in list(rects.items()):
            if name not in (MIDDLE, PANEL) and rect.top() >= edge - 0.5:
                rects[name] = rect.translated(0, shift)
        rects[NOTE] = QRectF(left, top, max(1.0, right - left), height)
        for name in (MIDDLE, PANEL):
            rect = rects[name]
            rects[name] = QRectF(rect.left(), rect.top(), rect.width(), rect.height() + shift)
        return rects, footer + shift

    def _paint_middle(self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: _Rects) -> None:
        super()._paint_middle(p, un, fonts, rects)
        note = rects.get(NOTE)
        if note is not None and self._note:
            p.setFont(fonts["alt"])
            p.setPen(qcolor(PALETTE.text, _NOTE_ALPHA))
            p.drawText(note, _WRAP, self._note)


class OpenLink(QObject):
    """The Open Link card's side: what it shows for the link in it, what its buttons do, and
    the read of a link's first bytes. `jobs` runs the read (the window's JobRunner); `play`,
    `add_provider` and `verify_ssl` are the window's (late-bound by the caller)."""

    def __init__(
        self,
        card: LinkCard,
        jobs: Any,
        *,
        play: Callable[[str, str], None],
        add_provider: Callable[[ProviderLink], None],
        verify_ssl: Callable[[], bool],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.card = card
        self._jobs = jobs
        self._play = play
        self._add_provider = add_provider
        self._verify_ssl = verify_ssl
        self._asked = 0  # the last read's request id
        self._waiting: Optional[int] = None  # the read on its way (its request id)
        self._probed: Optional[tuple[str, Kind]] = None  # what a link's first bytes said
        card.changed.connect(self._on_changed)
        card.pressed.connect(self._on_pressed)
        card.cancelled.connect(self._forget)

    def open(self, text: str = "") -> None:
        """The card afresh over the picture, `text` in its field ("" for File → Open Link…,
        a dropped link's), the keys in the field. A read still on its way is dropped."""
        self._forget()
        self.card.set_busy(False)
        self.card.hide()
        self._show(text)

    def shown(self) -> bool:
        return not self.card.isHidden()

    def _forget(self) -> None:
        """The card went (or shows afresh): a read on its way answers nothing."""
        self._waiting = None
        self._probed = None

    def _kind(self, url: str) -> Kind:
        if self._probed is not None and self._probed[0] == url:
            return self._probed[1]
        return classify(url)

    def _show(self, text: str) -> None:
        """The card for the link in `text`: its found line, its note, Play or Add as a
        provider…, and an Xtream stream's whole account as the other way."""
        url = address(text)
        kind = self._kind(url)
        alternates: list[Button] = []
        if isinstance(kind, Provider):
            found = provider_found(kind.link)
            note = translate(WHOLE_TEXT)
            accent = Button(PROVIDER, translate(ADD_AS_PROVIDER_TEXT), accent=True)
        else:
            found = translate(_FOUND[kind]) if kind in _FOUND else ""
            note = translate(KEPT_TEXT)
            accent = Button(PLAY, translate(PLAY_TEXT), accent=True)
            account = xtream_account(url)
            if account is not None:
                alternates.append(Button(ACCOUNT, translate(ACCOUNT_TEXT, host=account.name)))
        self.card.set_form(
            translate(TITLE_TEXT),
            [Field(LINK, "", text, paste=True)],
            found,
            [Button(CANCEL, translate(CANCEL_TEXT)), accent],
            alternates,
            note=note,
        )

    def _on_changed(self, key: str, text: str) -> None:
        if key == LINK:
            self._show(text)

    def _on_pressed(self, key: str) -> None:
        url = address(self.card.values().get(LINK, ""))
        kind = self._kind(url)
        if key == PROVIDER and isinstance(kind, Provider):
            self._hand_over(kind.link)
        elif key == ACCOUNT:
            account = xtream_account(url)
            if account is not None:
                self._hand_over(account)
        elif key == PLAY:
            if kind in (LIVE, FILM):
                self._start(url, str(kind))
            elif kind == PROBE:
                self._probe(url)
            elif not isinstance(kind, Provider):
                text = translate(NOT_A_LINK_TEXT)
                self.card.set_found(text, False)
                announce(self.card, text, assertive=True)

    def _hand_over(self, link: ProviderLink) -> None:
        self._forget()
        self.card.hide()
        self._add_provider(link)

    def _start(self, url: str, kind: str) -> None:
        self._forget()
        self.card.hide()
        self._play(url, kind)

    # ---- a link's first bytes ---------------------------------------------------------------

    def _probe(self, url: str) -> None:
        """Read `url`'s first bytes on a worker; the card waits ("Checking…")."""
        self._asked += 1
        self._waiting = self._asked
        self.card.set_busy(True)
        job = LinkProbeJob(url, bool(self._verify_ssl()))
        self._jobs.run_worker(job, self._on_read, self._on_read_failed, context=(self._asked, url))

    def _answers(self, request: int) -> bool:
        """Whether the read `request` still has someone to answer: the latest, the card up."""
        return (
            isValid(self)
            and isValid(self.card)
            and request == self._waiting
            and not self.card.isHidden()
        )

    def _on_read(self, context: tuple[int, str], text: object) -> None:
        request, url = context
        if not self._answers(request):
            return  # cancelled, or the card opened again since
        self._waiting = None
        self.card.set_busy(False)
        kind = probe_text(str(text), url)
        if not isinstance(kind, Provider):
            self._start(url, str(kind))
            return
        self._probed = (url, kind)
        self._show(self.card.values().get(LINK, url))
        announce(self.card, self.card.found()[0])

    def _on_read_failed(self, context: tuple[int, str], _message: str) -> None:
        request, url = context
        if not self._answers(request):
            return
        self._waiting = None
        self.card.set_busy(False)
        self._start(url, LIVE)  # VLC tries it; the player says why it failed
