"""The cards that add a provider and change one's address and login (Settings › Providers).

The Add card ("Add a provider") has one field for any link or address, with Paste. What it was
found to be shows under it (services.provider_link), and only the fields its kind needs follow:

- an Xtream link: its server, the username and password taken out of the link into their own
  fields (the link's field is not secret);
- a portal's: its address, the MAC its link names, a serial and a device ID;
- a playlist's: a web address, or a file on this computer ("M3U file…" picks one);
- an address that is no link: an Xtream server once a login is typed, a playlist without one,
  or the kind picked below it (STB portal, Jellyfin, Emby; picked again, the guess again). An
  Xtream server keeps no file's path from it (the same host's film or page pasted in: only
  its server, or its panel's path, is kept; services.provider_link.xtream_address).

A link pasted, or sent in one go (as the phone sends a word), gives its login to the fields at
once; one typed a character at a time keeps it until the check (the rest of a password would
land in the address). The name is optional: the host, made unique, as its placeholder shows.
"Check and add" asks the provider first, on a worker (ui.provider_check: an account, a
playlist's HEAD or its file, a media server's sign-in). Only one that answered is added,
selected and said ("Added … to your providers"). A failure says why and offers "Add anyway",
but for Jellyfin and Emby (only their sign-in's token is kept, never the password) and for a
playlist file QiTV can't find. "Free channels…" and "Free radio…" give way to their countries
in Settings (2.13.1), "Plex" to Plex's sign-in by a code (2.14: ui.plex_signin; Address and login
on a Plex provider signs in again so).

The Address-and-login card ("<name>: address and login") is the same card, filled in from the
provider, with "Check and save". Its link is never read as one until it changes: an older
QiTV's "M3U Stream", or a playlist from a panel's get.php, stays the kind it is. The guard
(services.provider_guard) compares the provider with what it would become before anything is
asked:

- the same server and account (a new password, a port fixed): checked, then saved, with no
  question;
- another kind: "This is another provider", with only "Add as new" (and Cancel);
- another server, or another account (a user name, a portal's MAC): "Add as new" first, or
  "Replace <name>'s address" / "Replace <name>'s login", checked, then saved. A playlist is
  its whole address: another one on the same server, or another file, is "another address",
  said by its server's or its file's name (never a path or a query, which may hold a login).

"Add as new" turns the card into the Add card and adds the provider as new: the one edited
stays as it was. A check that fails saves nothing here: nothing about who a provider is
changes until it answered. A media server signs in again here; its new access is kept, never
the password, and its lists come again.

What a card lacks to be checked (the address, a portal's MAC, an Xtream login, a media
server's user name) is said first, the keys on its field. Cancel (Esc, Back, the dimmed
picture) always works, while a check is on its way too, and its late answer is dropped;
nothing typed comes back once a card or Settings closed. The cards show over Settings, which
gives them the keys (SettingsController.show_card), and stay under a question the window asks
meanwhile, and under File → Open Link…'s card opened over them.
"""

from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from services.provider_changes import unique_name
from services.provider_check import Outcome
from services.provider_guard import OTHER_HOST, OTHER_KIND, SAME, judge
from services.provider_link import (
    M3U,
    STB,
    XTREAM,
    ProviderLink,
    parse_provider_link,
    xtream_address,
)
from services.provider_types import EMBY, JELLYFIN, PLEX, is_media_server
from ui.overlays.settings_card import (
    CANCEL,
    CANCEL_TEXT,
    Button,
    ChoiceCard,
    Field,
    FormCard,
    pick_file,
)
from ui.plex_signin import PlexSignIn
from ui.provider_check import ProviderCheck
from ui.settings_controller import SettingsController
from ui.settings_providers import (
    ADDED_TEXT,
    SAVE_FAILED_TEXT,
    ProviderRows,
    answer_text,
    kind_text,
    where,
)

# i18n: said in the Add card: what was pasted into its address is no provider link
NO_LINK_TEXT = N_("The clipboard holds no provider link.")
# The fields, by the provider keys they fill.
URL, USERNAME, PASSWORD, MAC, SERIAL, DEVICE, NAME = (
    "url",
    "username",
    "password",
    "mac",
    "serial_number",
    "device_id",
    "name",
)
CHECK, ANYWAY = "check", "anyway"  # the buttons (and CANCEL)
# The other ways; a kind's is its type (STB, JELLYFIN, EMBY; PLEX, its sign-in's).
FILE, FREE, RADIO = "file", "free", "radio"
KINDS = (STB, JELLYFIN, EMBY)
ADD_NEW, REPLACE = "new", "replace"  # the guard's answers (and CANCEL)
_ADD, _UPDATE = "add", "update"  # what an answered check is for
_PLAYLISTS = (M3U, "M3USTREAM")
_LOGIN_KINDS = (XTREAM, JELLYFIN, EMBY)
_PLAYLIST_SUFFIXES = (".m3u", ".m3u8")
_COLONS = " :：  "  # after the old window's labels ("MAC Address:")
# The window's cards over Settings' cards, lowest first: File → Open Link…'s, its questions.
_OVER_CARDS = ("open_link_card", "question_card", "quality_card", "captions_card")

ADD_TITLE = N_("Add a provider")  # i18n: the title of Settings → Providers → ＋ Add provider's card
# i18n: the title of a provider's card in Settings ({name}: the provider): its address and login
EDIT_TITLE = N_("{name}: address and login")
# i18n: the Add provider card's first field: any provider link, or a server's address
LINK_TEXT = N_("Link or address")
CHECK_ADD_TEXT = N_("Check and add")  # i18n: a button: ask the provider first, then add it
CHECK_SAVE_TEXT = N_("Check and save")  # i18n: a button: ask the provider first, then keep it
# i18n: a button once the check failed: add the provider all the same
ADD_ANYWAY_TEXT = N_("Add anyway")
M3U_FILE_TEXT = N_("M3U file…")  # i18n: the Add provider card's other way: a playlist file
FREE_TEXT = N_("Free channels…")
RADIO_TEXT = N_("Free radio…")  # i18n: the Add provider card's last other way (2.13); a list
# Under a provider's link: what QiTV found it to be.
# i18n: under a provider's link: what it is ({kind}: "M3U playlist", "STB portal"…; {host})
FOUND_TEXT = N_("✓ {kind} at {host}")
# i18n: under a provider's link: an Xtream link, its username and password now in their fields
FOUND_LOGIN_TEXT = N_("✓ Xtream at {host}, with its login")
# i18n: under a provider's link: a playlist file on this computer ({name}: the file's name)
FOUND_FILE_TEXT = N_("✓ M3U playlist file “{name}”")
# Under a provider's address that is no link QiTV knows: it is added as {kind} (Xtream with a
# username and password, M3U playlist without), unless another kind is picked under it.
# i18n: under an address that is no known link: the kind it is added as, unless another is picked
GUESS_TEXT = N_("{kind} at {host}, or pick its kind below")
# i18n: a provider card's line: a field it needs is empty ({field}: its label, as "Password")
FILL_IN_TEXT = N_("Fill in “{field}” first.")
# i18n: asked when a provider's new address is of another kind (an M3U playlist's into Xtream)
ANOTHER_KIND_TITLE = N_("This is another provider")
ANOTHER_KIND_BODY = N_(
    # i18n: {name} is the provider edited; {old_kind}/{kind}: kinds, as "Xtream"; hosts: servers
    "{name} ({old_kind}, {old_host}) and this one ({kind}, {host}) are of different kinds, so"
    " this one is a new provider."
)
# i18n: asked when a provider's new address is another server's
ANOTHER_HOST_TITLE = N_("This is another server")
ANOTHER_HOST_BODY = N_(
    # i18n: {name} is the provider edited; {old_host} its server, {host} the new address's
    "{name} is at {old_host}, and this one at {host}. Add it as a new provider, or replace"
    " {name}'s address?"
)
# i18n: asked when a playlist's new address is another on the same server, or another file
ANOTHER_ADDRESS_TITLE = N_("This is another address")
SAME_HOST_BODY = N_(
    # i18n: {name} is the playlist edited; {host} the server both addresses are on
    "{name} and this one are both on {host}, at different addresses. Add it as a new"
    " provider, or replace {name}'s address?"
)
SAME_FILE_BODY = N_(
    # i18n: {name} is the playlist edited; {file} the name both playlist files have
    "{name} and this one are both files named {file}, in different folders. Add it as a new"
    " provider, or replace {name}'s address?"
)
FILE_BODY = N_(
    # i18n: {name} is the playlist edited; {old_where} and {where}: a file's name or a server
    "{name} plays from {old_where}, and this one from {where}. Add it as a new provider, or"
    " replace {name}'s address?"
)
# i18n: asked when a provider's new login is another account on the same server
ANOTHER_ACCOUNT_TITLE = N_("This is another account")
ANOTHER_ACCOUNT_BODY = N_(
    # i18n: {name} is the provider edited; {old_user} and {user}: user names (a portal's MACs)
    "{name} signs in to {host} as {old_user}, and this one as {user}. Add it as a new"
    " provider, or replace {name}'s login?"
)
ADD_NEW_TEXT = N_("Add as new")  # i18n: a button: add the edited address as a new provider
# i18n: a button: the provider edited takes the new address ({name}: the provider)
REPLACE_ADDRESS_TEXT = N_("Replace {name}'s address")
# i18n: a button: the provider edited takes the new login ({name}: the provider)
REPLACE_LOGIN_TEXT = N_("Replace {name}'s login")
# The fields' labels (the old window's, without their colons).
_LABELS = {
    URL: LINK_TEXT,
    USERNAME: N_("Username"),
    PASSWORD: N_("Password"),
    MAC: N_("MAC Address:"),  # i18n: an STB portal's field: the device's MAC address
    SERIAL: N_("Serial Number (Optional):"),  # i18n: an STB portal's field, may stay empty
    DEVICE: N_("Device ID (Optional):"),  # i18n: an STB portal's field, may stay empty
    NAME: N_("Name"),
}


def _label(key: str) -> str:
    return translate(_LABELS[key]).rstrip(_COLONS)


def _is_path(text: str) -> bool:
    """Whether an address is a file's on this computer (not looked for here: the check does,
    on its worker)."""
    text = text.strip()
    return bool(text) and "://" not in text and os.path.isabs(os.path.expanduser(text))


def _host(text: str, port: bool = True) -> str:
    """The server an address names (with its port: `port`); "" for none (words, say)."""
    text = text.strip()
    if not text or any(c.isspace() for c in text):
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
        host, number = parts.hostname or "", parts.port
    except ValueError:
        return ""
    return f"{host}:{number}" if port and host and number else host


def _reads_as_playlist(text: str) -> bool:
    """Whether a web address reads as a playlist's: it ends in .m3u or .m3u8, or it asks an
    Xtream panel's get.php for one (`type=m3u…`, its login in it)."""
    text = text.strip()
    try:
        parts = urlsplit(text if "://" in text else f"http://{text}")
    except ValueError:
        return False
    path = parts.path.lower()
    if path.endswith(_PLAYLIST_SUFFIXES):
        return True
    kind = next((value for key, value in parse_qsl(parts.query) if key.lower() == "type"), "")
    return path.rsplit("/", 1)[-1] == "get.php" and "m3u" in kind.lower()


def _web(provider: dict) -> bool:
    """Whether a provider's address is a web one (not a file's)."""
    return "://" in str(provider.get(URL) or "")


def _accounts(old: dict, new: dict) -> tuple[str, str]:
    """The two accounts the guard told apart: their user names, else their MACs."""
    for key in (USERNAME, MAC):
        before, after = str(old.get(key) or "").strip(), str(new.get(key) or "").strip()
        if before.casefold() != after.casefold():
            return before or "—", after or "—"
    return "—", "—"


class ProviderCards(QObject):
    """Settings › Providers' Add card and each provider's Address-and-login card, made over
    Settings' card parent (the overlay host) and shown where Settings is (2.14: over the
    Library too). `window` is the TV window (its check runner, its toasts, its Free channels
    and Free radio); `settings` shows the cards and gives them the keys; `rows` the Providers
    rows, whose changes these are too (`ProviderRows.apply`)."""

    # What a card holds (`_reset`).
    _original: dict
    _alone: bool
    _values: dict[str, str]
    _link: Optional[ProviderLink]
    _line: Optional[tuple[str, bool]]
    _asked: dict
    _update: dict
    _pending: dict

    def __init__(
        self, window: Any, settings: SettingsController, drawer: QWidget, rows: ProviderRows
    ) -> None:
        super().__init__(drawer)
        self._window = window
        self._settings = settings
        self._rows = rows
        host = settings.card_parent()  # (the overlay host: Settings lends them, 2.14)
        self.form = FormCard(host)
        self.question = ChoiceCard(host)
        self._check = ProviderCheck(runner=window.jobs, parent=self)
        self.form.changed.connect(self._on_changed)
        self.form.pressed.connect(self._on_pressed)
        self.form.cancelled.connect(self._on_cancelled)
        self.question.answered.connect(self._on_guard)
        self._check.answered.connect(self._on_answered)
        settings.hidden.connect(self._close)  # (its cards cancelled already)
        # 2.14: Plex signs in with a code on a card of its own (ui.plex_signin).
        self.plex = PlexSignIn(window, settings, drawer, rows, shown=self._raise_over)
        self._reset()

    # ---- opening ------------------------------------------------------------------------------

    def add(self, link: Optional[ProviderLink] = None, opened_settings: bool = False) -> None:
        """The Add card: empty, or filled in from `link` (a dropped portal link, say: the keys
        then on what it lacks, its MAC). Asked again while it shows, with no link, it stays as
        it is, what was typed too. `opened_settings`: Settings opened for it (an Add provider
        from outside Settings; filled anew, the Add card keeps what it had); with no provider
        yet, its Cancel then closes Settings too, which gives the welcome card back
        (ProviderFlow.settings_closed)."""
        showing = not self.form.isHidden() and not self._editing
        if link is None and showing:
            self._settings.show_card(self.form)
            self._raise_questions()
            return
        alone = opened_settings or (showing and self._alone)
        self._close()
        self._alone = alone
        if link is not None:
            self._link = link
            self._values = {
                URL: link.url,
                USERNAME: link.username,
                PASSWORD: link.password,
                MAC: link.mac,
            }
        self._render()
        if link is not None:
            self._focus_first_empty()

    def edit(self, name: str) -> None:
        """Provider `name`'s Address-and-login card, filled in from it (a media server's
        password is never kept: it is typed to sign in again)."""
        try:
            provider = self._rows.actions.changes.get(name)
        except KeyError:
            return  # gone meanwhile
        self._close()
        if str(provider.get("type") or "").upper() == PLEX:
            self.plex.start(editing=name)  # 2.14: its sign-in again, by a code
            return
        self._editing, self._original = name, provider
        kind = self._stored_kind()

        def stored(key: str) -> str:
            return str(provider.get(key) or "")

        self._values = {URL: stored(URL), USERNAME: stored(USERNAME)}
        if not is_media_server(kind):
            self._values[PASSWORD] = stored(PASSWORD)
        if kind == STB:
            self._values.update({key: stored(key) for key in (MAC, SERIAL, DEVICE)})
        self._render()

    # ---- what the card holds ------------------------------------------------------------------

    def _reset(self) -> None:
        """No card: nothing kept of what was typed (a password least of all), no answer
        awaited."""
        self._editing = ""  # the provider edited ("": the Add card)
        self._alone = False  # Settings opened for the Add card (`add`)
        self._original = {}  # as it is listed
        self._values = {}  # every field's text, those hidden for now too
        self._link = None  # what the link field was read as
        self._chosen = ""  # the kind picked below the card (or kept by "Add as new")
        self._line = None  # said in place of what was found
        self._failed = False  # the last check failed, nothing changed since
        self._waiting = False  # a check's answer is awaited
        self._then = ""  # what it is for: _ADD or _UPDATE
        self._asked = {}  # the provider asked
        self._update = {}  # the fields an update writes
        self._pending = {}  # what the guard's question is about

    def _close(self) -> None:
        for card in (self.question, self.form):
            if not card.isHidden():
                card.hide()
        self._reset()

    def _on_cancelled(self) -> None:
        """The card's Cancel (Esc, Back, the dimmed picture): nothing kept. Settings opened
        for the Add card with no provider yet closes too: the welcome card again (not while
        Settings is going anyway, which cancels its cards)."""
        alone = self._alone
        self._reset()
        if alone and self._settings.visible() and not self._rows.actions.changes.names():
            self._settings.close()

    def _text(self, key: str) -> str:
        return self._values.get(key, "").strip()

    def _remember(self) -> None:
        """The fields' texts as typed, kept while they are hidden (another kind picked)."""
        if not self.form.isHidden():
            self._values.update(self.form.values())

    def _stored_kind(self) -> str:
        return str(self._original.get("type") or "").upper() if self._editing else ""

    def _has_login(self) -> bool:
        return bool(self._text(USERNAME) or self._values.get(PASSWORD))

    def _kind(self) -> str:
        """The kind the card holds: the one picked, the link's, a file's (a playlist), the
        provider's own (an address that is no link), else by its login."""
        if self._chosen:
            return self._chosen
        text = self._text(URL)
        stored = self._stored_kind()
        link = self._link
        if link is not None:
            if stored in _PLAYLISTS and (link.type == M3U or _reads_as_playlist(text)):
                return stored  # a playlist's own address (an older QiTV's M3U Stream too)
            return link.type
        if _is_path(text):
            return stored if stored in _PLAYLISTS else M3U
        if stored:
            return stored
        return XTREAM if self._has_login() else M3U

    def _guessing(self) -> bool:
        """An address that is no link, on the Add card, no kind picked: Xtream or a playlist,
        by its login."""
        text = self._text(URL)
        return not (self._editing or self._chosen or self._link or not text or _is_path(text))

    def _address(self, kind: str) -> str:
        """The address the provider keeps: a link's own (a server's without its login), a
        file's path, else the address as typed (a web one with its scheme; an Xtream one
        without a file's path at its end: services.provider_link.xtream_address)."""
        text = self._text(URL)
        link = self._link
        if _is_path(text):
            return os.path.expanduser(text)
        if link is not None and (link.type == kind or (link.type == M3U and kind in _PLAYLISTS)):
            return link.url
        if text and "://" not in text:
            text = f"http://{text}"
        return xtream_address(text) if kind == XTREAM else text

    def _login(self, kind: str) -> dict[str, str]:
        """What the kind signs in with, as the old window kept it (a media server's password
        goes to its sign-in alone)."""
        if kind == XTREAM:
            return {USERNAME: self._text(USERNAME), PASSWORD: self._values.get(PASSWORD, "")}
        if kind == STB:
            return {key: self._text(key) for key in (MAC, SERIAL, DEVICE)}
        if is_media_server(kind):
            return {USERNAME: self._text(USERNAME)}
        return {}

    def _entry(self, kind: str) -> dict:
        """A new provider from the card: its kind, address, login and name (the host's
        when none is typed)."""
        entry = {"type": kind, URL: self._address(kind), **self._login(kind)}
        entry[NAME] = self._text(NAME) or self._placeholder()
        return entry

    def _placeholder(self) -> str:
        """The name an unnamed new provider gets: its host, a playlist file's name, made
        unique ("" without either: its address then)."""
        text = self._text(URL)
        if _is_path(text):
            base = os.path.splitext(os.path.basename(os.path.expanduser(text)))[0]
        else:
            base = self._link.name if self._link is not None else _host(text, port=False)
        return unique_name(base, self._rows.actions.changes.names()) if base else ""

    def _missing(self, kind: str) -> str:
        """The first field the kind needs and lacks ("" when none): an address, a portal's MAC,
        an Xtream login, a media server's user name."""
        needed = [URL]
        if kind == STB:
            needed.append(MAC)
        elif kind == XTREAM:
            needed += [USERNAME, PASSWORD]
        elif is_media_server(kind):
            needed.append(USERNAME)
        return next((key for key in needed if not self._text(key)), "")

    def _may_add_unchecked(self, kind: str) -> bool:
        """Add anyway: never for a media server (only its sign-in's token is kept) or a
        playlist file (only one QiTV finds)."""
        return not is_media_server(kind) and not _is_path(self._text(URL))

    # ---- showing ------------------------------------------------------------------------------

    def _render(self) -> None:
        """The card as it holds now (shown when it was not, over Settings)."""
        kind = self._kind()
        found, ok = self._found(kind)
        form = self.form
        fresh = form.isHidden()
        if self._editing:
            title = translate(EDIT_TITLE, name=self._editing)
        else:
            title = translate(ADD_TITLE)
        form.set_form(
            title,
            self._fields(kind),
            found,
            self._buttons(kind),
            self._alternates(),
        )
        form.set_found(found, ok)  # a failure stays one
        if fresh:
            self._settings.show_card(form)
        self._raise_questions()

    def _raise_questions(self) -> None:
        """A question over the card stays over it: the guard's, and the window's (which keep
        the keys over Settings). So does File → Open Link…'s card opened over it (which has
        the keys before Settings' cards, TVController.handle): a check's late answer shows
        under it."""
        cards = [self.question] + [getattr(self._window, name, None) for name in _OVER_CARDS]
        for card in cards:
            if card is not None and card.isVisible():
                card.raise_()

    def _raise_over(self) -> None:
        """The window's cards that stay over Settings' (a question it asks meanwhile, File →
        Open Link…'s card) over the Plex sign-in's too."""
        for name in _OVER_CARDS:
            card = getattr(self._window, name, None)
            if card is not None and card.isVisible():
                card.raise_()

    def _fields(self, kind: str) -> list[Field]:
        def field(key: str, **kwargs: Any) -> Field:
            return Field(key, _label(key), self._values.get(key, ""), **kwargs)

        fields = [field(URL, paste=True)]
        if kind in _LOGIN_KINDS or self._guessing():
            fields += [field(USERNAME), field(PASSWORD, secret=True)]
        elif kind == STB:
            fields += [field(MAC), field(SERIAL), field(DEVICE)]
        if not self._editing:
            fields.append(field(NAME, placeholder=self._placeholder()))
        return fields

    def _found(self, kind: str) -> tuple[str, bool]:
        """The line under the link: what it was found to be (good), or what was said in its
        place (a failure, a field to fill in)."""
        if self._line is not None:
            return self._line
        text = self._text(URL)
        if not text or not (_is_path(text) or _host(text)):
            return "", True
        if _is_path(text):
            name = os.path.basename(os.path.expanduser(text))
            return translate(FOUND_FILE_TEXT, name=name), True
        host = where({URL: self._address(kind)})
        link = self._link
        if kind == XTREAM and link is not None and link.type == XTREAM:
            return translate(FOUND_LOGIN_TEXT, host=host), True
        if self._guessing():
            return translate(GUESS_TEXT, kind=kind_text(kind), host=host), True
        return translate(FOUND_TEXT, kind=kind_text(kind), host=host), True

    def _buttons(self, kind: str) -> list[Button]:
        buttons = [Button(CANCEL, translate(CANCEL_TEXT))]
        if self._editing:
            return buttons + [Button(CHECK, translate(CHECK_SAVE_TEXT), accent=True)]
        if self._failed and self._may_add_unchecked(kind):
            buttons.append(Button(ANYWAY, translate(ADD_ANYWAY_TEXT)))
        return buttons + [Button(CHECK, translate(CHECK_ADD_TEXT), accent=True)]

    def _alternates(self) -> list[Button]:
        """The Add card's other ways; a playlist's address card has its file picker."""
        file = Button(FILE, translate(M3U_FILE_TEXT))
        if self._editing:
            return [file] if self._stored_kind() in _PLAYLISTS else []
        kinds = [Button(kind, kind_text(kind)) for kind in (*KINDS, PLEX)]
        free = [Button(FREE, translate(FREE_TEXT)), Button(RADIO, translate(RADIO_TEXT))]
        return [file, *kinds, *free]

    def _focus_first_empty(self) -> None:
        """The keys on the first field the card still needs (the optional ones not)."""
        for key, text in self.form.values().items():
            if key not in (SERIAL, DEVICE, NAME) and not text.strip():
                self.form.focus_field(key)
                return

    # ---- the card's signals -------------------------------------------------------------------

    def _on_changed(self, key: str, text: str) -> None:
        previous = self._values.get(URL, "")
        self._remember()
        self._line, self._failed = None, False  # what was said was about what it held
        if key == URL:
            self._read_link(text, typed=abs(len(text) - len(previous)) == 1)
        self._render()

    def _read_link(self, text: str, typed: bool) -> None:
        """The link field changed: what it is, its login into its fields. Pasted (or come in
        one go), the login also goes out of the link's field: it is not secret."""
        link = parse_provider_link(text)
        self._link = link
        if link is None:
            if not typed and text.strip() and not _is_path(text) and not _host(text):
                self._line = (translate(NO_LINK_TEXT), False)
            return
        self._chosen = ""  # the link says its kind
        if link.username or link.password:
            self._values[USERNAME], self._values[PASSWORD] = link.username, link.password
        if link.mac:
            self._values[MAC] = link.mac
        if not typed:
            self._take_login(link)

    def _take_login(self, link: ProviderLink) -> None:
        """The link's login in its own fields only: the link's field shows its server."""
        if self._kind() == link.type and (link.username or link.password or link.mac):
            self._values[URL] = link.url

    def _on_pressed(self, key: str) -> None:
        self._remember()
        if key == CHECK:
            self._submit()
        elif key == ANYWAY:
            self._add_unchecked()
        elif key == FILE:
            self._pick_file()
        elif key == FREE:
            self._free_channels()
        elif key == RADIO:
            self._free_radio()
        elif key == PLEX:
            self._plex()
        elif key in KINDS:
            self._chosen = "" if self._chosen == key else key
            self._line, self._failed = None, False
            self._render()
            self._focus_first_empty()

    def _pick_file(self) -> None:
        """M3U file…: the system's file picker (as the old window's Load File)."""
        path = pick_file(self.form.window(), self.form.title(), "")
        if not path or self.form.isHidden():
            return  # none picked, or the card went meanwhile (the picker waits for its answer)
        self._values[URL] = path
        self._link, self._chosen, self._line, self._failed = None, "", None, False
        self._render()

    def _free_channels(self) -> None:
        """Free channels…: the card gives way to Settings' Free channels, its countries (a
        Settings the card opened for itself goes once one is chosen, as after the card)."""
        alone = self._alone
        self._close()
        self._window.show_free_channels(opened_settings=alone)

    def _plex(self) -> None:
        """Plex (2.14): the card gives way to Plex's sign-in, a code to enter at plex.tv/link
        (a Settings the card opened for itself closes with its Cancel, as with the card's)."""
        alone = self._alone
        self._close()
        self.plex.start(opened_settings=alone)

    def _free_radio(self) -> None:
        """Free radio… (2.13): likewise, its countries."""
        alone = self._alone
        self._close()
        self._window.show_free_radio(opened_settings=alone)

    # ---- checking, then keeping -------------------------------------------------------------

    def _submit(self) -> None:
        """Check and add, Check and save: what is missing is said; an edited provider that
        would become another one is asked about first (the guard)."""
        kind = self._kind()
        if self._link is not None:
            self._take_login(self._link)  # typed: out of sight now too
        missing = self._missing(kind)
        if missing:
            self._line = (translate(FILL_IN_TEXT, field=_label(missing)), False)
            self._render()
            self.form.focus_field(missing)
            return
        if not self._editing:
            self._start(self._entry(kind), _ADD)
            return
        changes = {URL: self._address(kind), **self._login(kind)}
        new = {**self._original, "type": kind, **changes}
        self._update = changes
        verdict = judge(self._original, new)
        if verdict == SAME:
            self._start(new, _UPDATE)
        else:
            self._render()  # the link's login out of its field
            self._ask(verdict, new)

    def _ask(self, verdict: str, new: dict) -> None:
        """The guard's question: another kind is only ever a new provider; another server or
        account is new, or the edited one's (Add as new first)."""
        self._pending = new
        name = self._editing
        old_host, host = where(self._original), where(new)
        cancel = Button(CANCEL, translate(CANCEL_TEXT))
        add_new = Button(ADD_NEW, translate(ADD_NEW_TEXT), accent=True)
        if verdict == OTHER_KIND:
            title = translate(ANOTHER_KIND_TITLE)
            body = translate(
                ANOTHER_KIND_BODY,
                name=name,
                old_kind=kind_text(self._original.get("type")),
                old_host=old_host,
                kind=kind_text(new.get("type")),
                host=host,
            )
            buttons = [cancel, add_new]
        elif verdict == OTHER_HOST:
            title, body = self._other_address(name, new)
            replace = Button(REPLACE, translate(REPLACE_ADDRESS_TEXT, name=name))
            buttons = [cancel, replace, add_new]
        else:
            old_user, user = _accounts(self._original, new)
            title = translate(ANOTHER_ACCOUNT_TITLE)
            body = translate(
                ANOTHER_ACCOUNT_BODY, name=name, host=host, old_user=old_user, user=user
            )
            replace = Button(REPLACE, translate(REPLACE_LOGIN_TEXT, name=name))
            buttons = [cancel, replace, add_new]
        self.question.ask(title, body, buttons, ADD_NEW)
        self._settings.show_card(self.question)
        self._raise_questions()

    def _other_address(self, name: str, new: dict) -> tuple[str, str]:
        """The question's words for another address: another server, or (a playlist is its
        whole address) another one on the same server, another file. Only servers' and files'
        names are said, never a path or a query (an address may hold its login)."""
        old_host, host = where(self._original), where(new)
        files = [not _web(provider) for provider in (self._original, new)]
        if all(files) and old_host == host:
            body = translate(SAME_FILE_BODY, name=name, file=host)
        elif any(files):
            body = translate(FILE_BODY, name=name, old_where=old_host, where=host)
        elif old_host == host:
            body = translate(SAME_HOST_BODY, name=name, host=host)
        else:
            body = translate(ANOTHER_HOST_BODY, name=name, old_host=old_host, host=host)
            return translate(ANOTHER_HOST_TITLE), body
        return translate(ANOTHER_ADDRESS_TITLE), body

    def _on_guard(self, key: str) -> None:
        new, self._pending = self._pending, {}
        if not new or key not in (REPLACE, ADD_NEW) or self.form.isHidden():
            return  # Cancel: back on the card, as it was
        if key == REPLACE:
            self._start(new, _UPDATE)
            return
        # Add as new: the Add card's way, the edited provider left as it was.
        kind = str(new.get("type") or "")
        self._editing, self._original, self._chosen = "", {}, kind
        self._start(self._entry(kind), _ADD)

    def _start(self, provider: dict, then: str) -> None:
        """Ask `provider` on a worker (a media server signs in with the password typed, which
        goes to its sign-in alone); the card waits, but for Cancel."""
        media = is_media_server(provider.get("type"))
        password = self._values.get(PASSWORD, "") if media else ""
        self._then, self._asked = then, provider
        self._line, self._failed = None, False
        self._render()
        self.form.set_busy(True)
        self._waiting = True  # before start: a runner may answer inside it
        self._check.start(provider, bool(self._window.config_manager.ssl_verify), password)

    def _on_answered(self, _request: int, outcome: Outcome) -> None:
        if not self._waiting or self.form.isHidden():
            return  # cancelled meanwhile (the card, or Settings); a later check is awaited
        self._waiting = False
        provider, self._asked = self._asked, {}
        self.form.set_busy(False)
        if not outcome.ok:
            self._line, self._failed = (answer_text(outcome), False), True
            self._render()
        elif self._then == _UPDATE:
            self._save(outcome)
        else:
            self._add(provider, outcome)

    def _add_unchecked(self) -> None:
        kind = self._kind()
        if self._failed and self._may_add_unchecked(kind):
            self._add(self._entry(kind), None)

    def _add(self, entry: dict, outcome: Optional[Outcome]) -> None:
        """Add `entry` (a media server with its sign-in's access), select it and say so; a
        list the disk refuses is said, and the card stays."""
        entry = {**entry, **(outcome.access if outcome is not None else {})}
        added: list[str] = []
        if not self._rows.apply(lambda: added.append(self._rows.actions.add(entry))):
            self._refused()
            return
        name = added[0]
        if outcome is not None:
            self._rows.checked(name, outcome)
        self._close()
        self._settings.refresh_sections()
        self._window.screens.notify(translate(ADDED_TEXT, names=name))

    def _save(self, outcome: Outcome) -> None:
        """The edited provider takes the card's address and login (a media server its new
        access, its lists again); one gone meanwhile only closes the card."""
        name = self._editing
        changes = {**self._update, **outcome.access}
        refresh = bool(outcome.access)
        try:
            saved = self._rows.apply(
                lambda: self._rows.actions.update(name, changes, refresh=refresh)
            )
        except KeyError:
            saved = True  # removed or renamed elsewhere meanwhile: nothing to change
        else:
            if saved:
                self._rows.checked(name, outcome)
        if not saved:
            self._refused()
            return
        self._close()
        self._settings.refresh_sections()

    def _refused(self) -> None:
        """The disk refused the list (said in a toast too, which the card may cover): the card
        stays as it was, and says so."""
        self._line = (translate(SAVE_FAILED_TEXT), False)
        self._render()
