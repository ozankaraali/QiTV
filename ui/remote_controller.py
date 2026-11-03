"""The phone remote on the GUI thread: presses become key presses or actions, holds repeat here.

The remote's network thread (services/remote/hub.py) hands its events to `bridge.post`, a signal
of an object made on this thread, so they arrive here queued. A press goes through
TVWindow.remote_key (the keyboard's own path, resolved in the TV's context) or, for buttons
whose meaning must not change with what's open, straight to an action: volume and mute act
where the TV is, channel and playback buttons first leave the Library.
"""

from __future__ import annotations

from datetime import date, datetime
import logging
import threading
from typing import TYPE_CHECKING, Any, Optional, cast

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal

from services import favorites
from services.browsed_catalog import BrowsedCatalog
from services.find import Preferences, preferences_for
from services.i18n import N_, translate
from services.media_server import phone_reachable, server_resume
from services.navigation import item_title
from services.provider_api import xtream_hls
from services.provider_ids import ALL_PROVIDERS, provider_of
from services.provider_types import is_media_server
from services.redact import safe_message
from services.remote.films import is_matroska
from services.remote.net import home_ipv4
from services.remote.protocol import (
    BUTTON_META,
    MAX_ASK,
    MAX_DEVICE,
    STORE_VERSION,
    Answered,
    CaptionsLanguage,
    CaptionsSwitch,
    CastTo,
    Closed,
    CloseSearch,
    Code,
    Get,
    Held,
    Opened,
    OpenItem,
    Paired,
    Position,
    Press,
    Query,
    RecAct,
    RefreshLists,
    Schedule,
    Status,
    StoreSaved,
    Timer,
    ToTv,
    TrackPick,
    WatchHere,
)
from services.search import Hit
from services.vlc_profile import user_agent_for
from ui.find_session import FindSession
from ui.keymap import Action
from ui.library.search_session import SearchSession
from ui.link_lanes import Target
from ui.record_timer_dialog import TimerChoice
from ui.remote_buttons import BUTTONS
from ui.remote_guide import PhoneGuide
from ui.remote_library import PhoneLibrary
from ui.remote_repeat import KeyRepeater
from ui.remote_snapshots import TITLE_CHARS, Handles, results, results_of, state, tracks

if TYPE_CHECKING:
    from services.remote.hub import RemoteHub

logger = logging.getLogger(__name__)

# i18n: a toast; {name} is a phone's name, such as "iPhone · Safari"
PAIRED_TEXT = N_("{name} can now control QiTV")
# i18n: a toast; {name} is a phone's name, such as "iPhone · Safari"
REMOVED_TEXT = N_("{name} can no longer control QiTV")
# i18n: a toast: the TV's stream went to a phone's own player; {name} is the phone's name
PLAYING_ON_TEXT = N_("Playing on {name}")
PLAYING_ON_PHONE_TEXT = N_("Playing on the phone")  # i18n: a toast, the phone's name unknown
# i18n: a message when a phone that asked for the stream disconnected before it was sent
PHONE_GONE_S = 90.0  # 2.8.12: a watching phone away this long gives its stream back
PHONE_GONE_TEXT = N_("The phone left before the stream reached it")
# i18n: the pairing screen when the phone remote couldn't start; "Help → Show the Log File" is
# the Help menu's "Show the Log File…"
START_FAILED_TEXT = N_(
    "The phone remote couldn't start. The log says why: Help → Show the Log File."
)


class _PhoneChoice:
    """A phone's provider choice for its lists: held here, never saved (the settings keep the
    TV's), starting where the TV's list is. Every other setting reads the settings."""

    def __init__(self, config: Any) -> None:
        self.__dict__["_config"] = config
        self.phone_provider_name = str(getattr(config, "selected_provider_name", "") or "")
        self.phone_all_providers = bool(getattr(config, "tv_all_providers", False))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_config"], name)

    def save_config(self) -> None:
        """Nothing to save: the choice is the phone's, while QiTV runs."""


_SWITCHES = ("enabled", "relay", "relay_url")  # Settings' own: the hub never changes them

_WINDOW_ACTIONS = (Action.SEARCH, Action.FULLSCREEN)
DIFF_MS = 500  # what's on is compared this often while a phone is connected
AFTER_PRESS_MS = 60  # and soon after each press, so the phone sees what it did
SEEK_DRIFT_MS = 3000  # a film's position this far from where it should be is a seek


class RemoteBridge(QObject):
    """Made on the GUI thread; the hub's thread posts, this thread receives (queued)."""

    arrived = Signal(object)

    def post(self, event: object) -> None:
        self.arrived.emit(event)


class RemoteController(QObject):
    codeChanged = Signal(str)  # the pairing QR's address; "" when pairing is closed
    devicesChanged = Signal(list)  # [{"id", "name", "last_used"}]
    statusChanged = Signal(dict)  # {"home_url", "relay"}

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._window = window
        self.bridge = RemoteBridge(self)
        self.bridge.arrived.connect(self._on_event)
        self.hub: Optional[RemoteHub] = None
        self._stopping: Optional[threading.Thread] = None  # the last hub closing (shutdown)
        self._repeater = KeyRepeater(self._press)
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)
        self._names: dict[str, str] = {}
        self.status: dict = {"home_url": "", "relay": "off"}
        self._guides: dict[str, PhoneGuide] = {}  # each phone's own Guide, by session
        self._libraries: dict[str, PhoneLibrary] = {}  # and its own Library
        self._channel_lists: dict[str, PhoneGuide] = {}  # and the Remote tab's channel list
        self._devices: dict[str, str] = {}  # each phone's device id, by session
        self._relayed: set[str] = set()  # phones through the relay (no film comes that way)
        # Each phone's catalog for its lists (its own provider choice), by device.
        self._phone_catalogs: dict[str, BrowsedCatalog] = {}
        self._channel_finder: Optional[FindSession] = None  # the phones' finds, shared
        self._title_searcher: Optional[SearchSession] = None  # and their Library's
        # What each phone searches for on its own (the TV's picture stays), by session; those
        # not answered yet wait their turn at the shared search.
        self._quiet: dict[str, str] = {}
        self._quiet_waiting: set[str] = set()
        self._find_source = ""  # the provider the phone's list that found last shows
        # What a phone plays in its own player, and the group it was picked in (for the TV), by
        # device: a phone that slept comes back as another session.
        self._watching: dict[str, tuple[Target, Optional[tuple[str, list]]]] = {}
        # The number of the "play" each phone was handed last, by device: its position reports
        # carry it back, so one made for the title before a zap is not the new one's.
        self._plays: dict[str, int] = {}
        self._links: dict[str, str] = {}  # 2.8: the link each device was handed last
        self._play_number = 0
        self.result_handles = Handles(limit=500)  # the search's
        self._removing: dict[str, str] = {}  # device id → name, until the hub confirms
        self._phones: set[str] = set()
        self._last_state: Optional[dict] = None
        self._last_at = 0
        # 2.8.12: a phone watching whose page closed: its stream is given back once it stayed
        # away PHONE_GONE_S (a locked screen or a reload comes back sooner).
        self._gone_timers: dict[str, QTimer] = {}
        self._diff = QTimer(self)
        self._diff.setInterval(DIFF_MS)
        self._diff.timeout.connect(self._push_state)
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.timeout.connect(self._push_state)
        window.library_controller.searchChanged.connect(self._push_results)
        # 2.6: QiTV's question for a phone that asked (its Watch with no stream free): the
        # number of the one each phone was asked, which its answer carries back.
        self._asks: dict[str, int] = {}
        self._ask_number = 0
        window.controller.phoneAsked.connect(self._on_phone_asked)
        window.controller.phoneSettled.connect(self._on_phone_settled)
        # An outside player took over (a phone's Watch, Play in VLC): the other phones let go.
        self._handed_to: Optional[str] = None  # the device a Watch was just handed to
        window.controller.handedOn.connect(self._on_handed_on)
        cast_devices = getattr(window, "cast_devices", None)  # 2.8: the phones' Cast to…
        if cast_devices is not None:
            cast_devices.changed.connect(self._push_cast)
        # 2.9: the large picture's sound and subtitles on the phones' sheets. The phones that
        # have one open hear each change; `of` numbers what plays.
        self._track_phones: set[str] = set()
        self._last_tracks: Optional[dict] = None
        self._tracks_target: object = None  # what played large when `of` was last numbered
        self._tracks_of = 0
        # 2.9: AI captions' download question on the phones that turned them on: its number
        # on each (the TV's card asks too: either answer goes for both).
        self._caption_asks: dict[str, int] = {}
        captions = getattr(window, "captions", None)
        if captions is not None:
            captions.changed.connect(self._on_captions_changed)

    # ---- lifecycle -------------------------------------------------------------------------

    def _config(self) -> dict:
        data = self._window.config_manager.remote
        return data if isinstance(data, dict) else {}

    def apply_config(self) -> None:
        """Start, update or stop the remote to match the saved settings."""
        data = self._config()
        self._names_from(data)
        if data.get("version") not in (None, STORE_VERSION):
            logger.warning("Phone remote settings of a newer QiTV: the remote stays off")
            self.shutdown()
            return
        if data.get("enabled") is not True:
            self.shutdown()
            return
        if self.hub is None:
            from services.remote.hub import RemoteHub, web_dir

            self.hub = RemoteHub(self.bridge.post, web_dir())
            self.hub.start(data)
        else:
            self.hub.apply(data)

    def turn_on(self) -> None:
        """Phone Remote… while it's off: on, as Settings would turn it (the relay as it was)."""
        data = dict(self._config())
        if data.get("version") not in (None, STORE_VERSION):
            return
        data.update({"version": STORE_VERSION, "enabled": True})
        data.setdefault("relay", True)
        self._window.config_manager.remote = data
        try:
            self._window.config_manager.save_config()
        except Exception as error:
            logger.warning("Couldn't save the phone remote's switch: %s", safe_message(error))
        self.apply_config()

    def shutdown(self) -> None:
        self._timer.stop()
        self._diff.stop()
        self._phones.clear()
        self._track_phones.clear()
        self._last_tracks = None
        self._caption_asks.clear()  # the TV's card still asks
        if self.hub is not None:
            # Not waited for: a phone being sent a film held the window up to 4 s (Settings,
            # Quit). Quitting waits for it later (`wait_stopped`).
            self._stopping = self.hub.stop_soon()
            self.hub = None

    def wait_stopped(self, timeout: float) -> None:
        """Quitting: the remote's close (`shutdown`) waited for, `timeout` seconds at most."""
        stopping, self._stopping = self._stopping, None
        if stopping is not None:
            stopping.join(timeout)

    def set_pairing(self, open_: bool) -> None:
        if self.hub is not None:
            self.hub.set_pairing(open_)

    def remove_device(self, device_id: str) -> None:
        name = self._names.get(device_id, "")
        if self.hub is not None:
            self._removing[device_id] = name  # the toast once the hub has removed it
            self.hub.remove_device(device_id)
            return
        data = dict(self._config())  # the remote is off: unpair it in the saved settings
        devices = [d for d in data.get("devices", []) if isinstance(d, dict)]
        data["devices"] = [d for d in devices if d.get("id") != device_id]
        self._persist(data)
        if name:
            self._window.screens.notify(translate(REMOVED_TEXT, name=name))

    # ---- presses ---------------------------------------------------------------------------

    def fire(self, button: str) -> None:
        """Do what `button` does on the TV, once."""
        b = BUTTONS[button]
        window = self._window
        screens = window.screens
        if b.kind == "key":
            window.remote_key(b.key, b.text)
        elif b.kind == "screen":
            if b.payload == "tv":
                if screens.library_shown():
                    screens.back_to_tv()
                # The TV's guide or channel list open: closed as Back closes them, the picture
                # shows (at most both, one Esc each).
                for _ in range(2):
                    if window.controller.key_context() not in ("guide", "drawer"):
                        break
                    window.controller.handle(Action.ESCAPE)
            else:
                screens.open_library(b.payload)
        elif b.action in _WINDOW_ACTIONS:
            window.remote_action(b.action, b.payload)
        elif button == "pip" and window.controller.pip_shown():
            window.controller.close_pip()  # the phone's button says Close while one shows
        elif b.action is not None:
            if BUTTON_META[button].route == "player" and screens.library_shown():
                screens.back_to_tv()
            window.controller.handle(b.action, b.payload)

    def _press(self, button: str) -> None:
        try:
            self.fire(button)
        except Exception as error:  # a phone's press must never take QiTV down
            logger.error("Phone remote: %s failed: %s", button, safe_message(error))
        if self._phones:
            self._soon.start(AFTER_PRESS_MS)

    def _now(self) -> int:
        return int(self._clock.elapsed())

    def _schedule(self) -> None:
        due = self._repeater.next_due()
        if due is None:
            self._timer.stop()
        else:
            self._timer.start(max(0, due - self._now()))

    def _tick(self) -> None:
        self._repeater.advance(self._now())
        self._schedule()

    # ---- events from the hub ---------------------------------------------------------------

    def _on_event(self, event: object) -> None:
        if isinstance(event, Press):
            if event.down:
                self._repeater.down(event.sid, event.button, self._now())
            else:
                self._repeater.up(event.sid, event.button, event.steps, self._now())
            self._schedule()
        elif isinstance(event, Held):
            self._repeater.held(event.sid, self._now())
        elif isinstance(event, Closed):
            self._watch_gone(event.sid)
            if self._asks.pop(event.sid, None) is not None:
                self._window.controller.answer_phone(False)  # gone: the recording stays
            self._caption_asks.pop(event.sid, None)  # 2.9: the TV's card asks on
            self._track_phones.discard(event.sid)
            if not self._track_phones:
                self._last_tracks = None
            self._repeater.drop(event.sid)
            self._schedule()
            self._phones.discard(event.sid)
            self._devices.pop(event.sid, None)
            self._relayed.discard(event.sid)
            self._quiet.pop(event.sid, None)
            self._quiet_waiting.discard(event.sid)
            for browsing in (self._guides, self._libraries, self._channel_lists):
                gone = browsing.pop(event.sid, None)
                if gone is not None:
                    gone.deleteLater()
            self._release_finds()
            self._phone_guides()
            if not self._phones:
                self._diff.stop()
        elif isinstance(event, Opened):
            self._phones.add(event.sid)
            self._devices[event.sid] = event.device_id
            away = self._gone_timers.pop(event.device_id, None)
            if away is not None:
                away.stop()  # back in time: it still watches
                away.deleteLater()
            if event.door == "relay":
                self._relayed.add(event.sid)
            self._diff.start()
            self._send(event.sid, self._state())
        elif isinstance(event, Get):
            self._answer(event)
        elif isinstance(event, Query):
            self._search(event.sid, event.text, event.tv)
        elif isinstance(event, OpenItem):
            self._open(event.sid, event.handle)
        elif isinstance(event, WatchHere):
            self._watch_here(event.sid, event.handle)
        elif isinstance(event, ToTv):
            self._to_tv(event.sid)
        elif isinstance(event, RefreshLists):  # 2.8: as the Library's ↻
            self._window.screens.library_controller.refresh_lists()
        elif isinstance(event, CloseSearch):  # the phone's Done: the TV's search closes too
            if self._window.screens.library_shown():
                self._window.screens.library_controller.close_search()
        elif isinstance(event, Position):
            self._position(event.sid, event.position_ms, event.length_ms, event.play)
        elif isinstance(event, Answered):
            self._on_answered(event.sid, event.n, event.yes)
        elif isinstance(event, RecAct):
            controller = self._window.controller
            if event.act == "back":
                controller.go_back_to_recording(event.slot)
            else:
                controller.stop_background_recording(event.slot)
        elif isinstance(event, CastTo):
            self._cast_to(event.to)
        elif isinstance(event, TrackPick):  # 2.9: the Sound or Subtitles sheet's pick
            self._pick_track(event)
        elif isinstance(event, CaptionsSwitch):
            self._switch_captions(event)
        elif isinstance(event, CaptionsLanguage):
            self._caption_language(event)
        elif isinstance(event, Timer):
            self._timer_from_phone(event)
        elif isinstance(event, Schedule):
            self._schedule_from_phone(event.sid, event.handle, event.do)
        elif isinstance(event, StoreSaved):
            self._persist(event.data)
        elif isinstance(event, Paired):
            self._window.screens.notify(translate(PAIRED_TEXT, name=event.name))
            overlay = getattr(self._window, "pair_overlay", None)
            if overlay is not None:
                overlay.dismiss()  # done: one phone per showing
        elif isinstance(event, Code):
            self.codeChanged.emit(event.url)
        elif isinstance(event, Status):
            self.status = {"home_url": event.home_url, "relay": event.relay}
            self.statusChanged.emit(dict(self.status))
            overlay = getattr(self._window, "pair_overlay", None)
            if overlay is not None:
                failed = event.relay == "error" and not event.home_url
                overlay.set_error(translate(START_FAILED_TEXT) if failed else "")

    # ---- the second screen -----------------------------------------------------------------

    def _send(self, sid: str, message: dict) -> None:
        if self.hub is not None:
            self.hub.send(sid, message)

    def _state(self) -> dict:
        snapshot = state(self._window)
        self._last_state, self._last_at = snapshot, self._now()
        return snapshot

    def _push_state(self) -> None:
        """Tell the phones what's on when it changed (a film's clock runs on the phone)."""
        if not self._phones or self.hub is None:
            return
        self._stop_phones()
        self._push_tracks()
        last, last_at = self._last_state, self._last_at
        snapshot = state(self._window)
        if last is not None and not _changed(last, snapshot, self._now() - last_at):
            return
        self._last_state, self._last_at = snapshot, self._now()
        self.hub.broadcast(snapshot)

    def _push_results(self) -> None:
        if self._phones and self.hub is not None:
            self.hub.broadcast(results(self._window, self.result_handles))

    def _answer(self, event: Get) -> None:
        sid, section = event.sid, event.section or "movies"
        try:
            if event.what == "groups":
                self._guide(sid).groups(event.page, event.find, event.n, event.source)
            elif event.what == "sources":
                self._guide(sid).sources(event.n)
            elif event.what == "chsources":
                self._channel_list(sid).sources(event.n)
            elif event.what == "guide":
                self._guide(sid).channels(event.list, event.page, event.find, event.n)
            elif event.what == "timetable":
                self._guide(sid).timetable(event.handle)
            elif event.what == "chgroups":
                self._channel_list(sid).groups(event.page, event.find, event.n, event.source)
            elif event.what == "channels":
                self._channel_list(sid).channels(event.list, event.page, event.find, event.n)
            elif event.what == "genres":
                self._library(sid).genres(section, event.page, event.find, event.n)
            elif event.what == "cast":  # 2.8: the devices QiTV can cast to
                self._send(sid, self._cast_list())
            elif event.what == "tracks":  # 2.9: its Subtitles or Sound sheet opened
                self._track_phones.add(sid)
                self._push_tracks(asked=sid)
            elif event.what == "tracks_closed":  # and closed: no more of its changes
                self._track_phones.discard(sid)
                if not self._track_phones:
                    self._last_tracks = None
            else:
                self._library(sid).titles(section, event.genre, event.page, event.find, event.n)
        except Exception as error:  # a phone's question must never take QiTV down
            logger.error("Phone remote: %s failed: %s", event.what, safe_message(error))
        self._release_finds()
        if event.what in ("groups", "guide", "timetable"):
            self._phone_guides()

    def _phone_guides(self) -> None:
        """The guides of the channels the phones list load, as those the TV shows do."""
        names: set[str] = set()
        for guide in self._guides.values():
            names |= guide.providers
        try:
            self._window.controller.use_phone_guides(names)
        except Exception as error:
            logger.error("Phone remote: the guides in use: %s", safe_message(error))

    def _guide(self, sid: str) -> PhoneGuide:
        guide = self._guides.get(sid)
        if guide is None:
            send = lambda message: self._send(sid, message)  # noqa: E731
            guide = PhoneGuide(
                self._window,
                send,
                self,
                catalog=self._phone_catalog(sid),
                finder=self._finder(),
                prefer=self._prefer,
                guides_moved=self._phone_guides,
            )
            self._guides[sid] = guide
        return guide

    def _channel_list(self, sid: str) -> PhoneGuide:
        listed = self._channel_lists.get(sid)
        if listed is None:
            send = lambda m: self._send(sid, m)  # noqa: E731
            listed = PhoneGuide(
                self._window,
                send,
                self,
                bare=True,
                catalog=self._phone_catalog(sid),
                finder=self._finder(),
                prefer=self._prefer,
            )
            self._channel_lists[sid] = listed
        return listed

    def _phone_catalog(self, sid: str) -> BrowsedCatalog:
        """The catalog a phone's lists browse: its own provider choice (All providers, or one),
        kept while QiTV runs, so a phone that slept comes back where it was."""
        device = self._device(sid)
        catalog = self._phone_catalogs.get(device)
        if catalog is None:
            window = self._window
            catalog = BrowsedCatalog(
                window.catalogs,
                _PhoneChoice(window.config_manager),
                source_key="phone_provider_name",
                all_key="phone_all_providers",
                parent=self,
            )
            self._phone_catalogs[device] = catalog
        return catalog

    def rename_providers(self, renames: Any) -> None:
        """A renamed provider (`services.provider_rename.Renames`): a phone that showed it
        goes on showing it under its new name."""
        for catalog in self._phone_catalogs.values():
            choice = catalog.config
            name = getattr(choice, "phone_provider_name", "")
            if name in renames.names:
                choice.phone_provider_name = renames.names[name]
        self._find_source = renames.of(self._find_source)

    def _finder(self) -> FindSession:
        """The desktop channel list's find, for every phone: genres and channels at every
        provider from their saved lists (a worker reads them; nothing signs in), ranked as on
        the desktop."""
        if self._channel_finder is None:
            window = self._window
            finder = FindSession(
                window.provider_manager,
                window.jobs,
                loaded=window.catalog.loaded_contents,
                signature=window._find_signature,
                preferences=self._find_preferences,
                parent=self,
            )
            window.catalogs.contentChanged.connect(finder.content_changed)
            window.catalogs.providersChanged.connect(finder.content_changed)
            self._channel_finder = finder
        return self._channel_finder

    def _searcher(self) -> SearchSession:
        """The desktop Library's search, for every phone: every provider's films and series
        from their saved lists (a worker reads them), as the Library's search finds them."""
        if self._title_searcher is None:
            window = self._window
            searcher = SearchSession(
                window.provider_manager,
                window.jobs,
                window.screens.library_model,
                window.watch,
                self,
                loaded=getattr(window.library_catalog, "loaded_contents", None),
            )
            window.catalogs.contentChanged.connect(self._titles_changed)
            window.catalogs.providersChanged.connect(self._titles_changed)
            searcher.changed.connect(self._push_quiet_results)
            self._title_searcher = searcher
        return self._title_searcher

    def _titles_changed(self, *_args: object) -> None:
        """A provider's lists changed: the search reads them again, a find on show runs anew."""
        searcher = self._title_searcher
        if searcher is not None:
            searcher.drop_snapshot()
            if searcher.query.strip():
                searcher.rerun()

    def _release_finds(self) -> None:
        """No phone finds anything: the shared find and search let their text go (and their
        index after a while), as the desktop's find does once it ends, and a list change no
        longer runs them again."""
        lists = list(self._guides.values()) + list(self._channel_lists.values())
        finder = self._channel_finder
        if finder is not None and finder.query and not any(g.finding() for g in lists):
            finder.clear()
        searcher = self._title_searcher
        searched = bool(self._quiet) or any(
            library.finding() for library in self._libraries.values()
        )
        if searcher is not None and searcher.query and not searched:
            searcher.clear()

    def _prefer(self, source: str) -> None:
        self._find_source = source

    def _find_preferences(self) -> Preferences:
        """What the phones' finds rank first: the provider the phone's list shows (as the TV's
        own find ranks the TV's), the providers and channels watched lately, the stars."""
        window, config = self._window, self._window.config_manager
        names = window.catalog.provider_names()
        shown = self._find_source if self._find_source != ALL_PROVIDERS else ""
        browsed = shown or window.catalog.current_provider_name()
        return preferences_for(names, browsed, config.watch_history, favorites.matcher(config))

    def _library(self, sid: str) -> PhoneLibrary:
        library = self._libraries.get(sid)
        if library is None:
            window = self._window
            library = PhoneLibrary(
                window.library_catalog,
                window.config_manager,
                window.watch,
                lambda message: self._send(sid, message),
                self,
                cache=window.screens.library_model.cache,  # the TV's: a sorted list serves both
                searcher=self._searcher(),
                recordings=window.screens.recordings,  # the TV's Recordings shelf (2.8)
            )
            self._libraries[sid] = library
        return library

    def _watch_here(self, sid: str, handle: str = "") -> None:
        """What the TV plays, or the channel `handle` of the phone's lists, in the phone's own
        player. QiTV lets go of its own stream first: one screen at a time."""
        controller = self._window.controller
        chosen: Optional[Target] = None
        group: Optional[tuple[str, list]] = None
        if handle:
            found = self._find_handle(sid, handle) or ("", None)
            kind, payload = found
            if kind == "channel":
                channel, group_key, channels = cast(tuple, payload)
                name = str(channel.get("name") or "")
                chosen = Target(channel, "live", name, group_key=group_key)
                group = (group_key, channels) if group_key else None
            elif kind == "title" and cast(tuple, payload)[1] in ("movie", "episode"):
                item, item_kind, _genre_items = cast(tuple, payload)
                chosen = controller.title_target(item, item_kind)
            elif kind == "title" and cast(tuple, payload)[1] == "recording":
                item = cast(tuple, payload)[0]  # 2.8: from QiTV's home door
                chosen = Target(item, "movies", item_title(item), local=True)
            elif kind == "hit" and cast(Hit, payload).section in ("live", "movies"):
                hit = cast(Hit, payload)  # a search result: a channel, or a film
                if hit.section == "live":
                    chosen = Target(hit.item, "live", str(hit.item.get("name") or hit.title))
                else:
                    chosen = controller.title_target(hit.item, "movie")
            else:  # a series opens on the TV; anything else plays nowhere but there
                self._send(sid, {"t": "play", "failed": True})
                return

        def launch(target: Target, url: str) -> tuple[bool, str]:
            if sid not in self._phones:
                return False, PHONE_GONE_TEXT
            first = self._device(sid) not in self._watching
            self._watching[self._device(sid)] = (target, group)
            self._play_number = (self._play_number + 1) % MAX_ASK
            self._plays[self._device(sid)] = self._play_number
            self._links[self._device(sid)] = url
            message: dict = {"t": "play", "name": target.title[:TITLE_CHARS]}
            message["p"] = self._play_number  # what its position reports carry back
            if target.live:
                message["num"] = str(controller.channel_number(target.item) or "")
            else:  # a film, an episode or a recording goes on where it stopped (as on the TV)
                key = self._resume_key(target)
                watch = self._window.watch
                if target.start_over:
                    start = None
                elif key:  # 2.8: else a media server's own, unless QiTV saw it finished
                    start = watch.resume_from(key, target.item)
                else:
                    start = server_resume(target.item)
                if start:
                    message["start"] = start // 1000
            if target.local:
                self._recording_to(sid, str(target.item.get("path") or ""), message)
            elif not target.live and is_matroska(target.item, url):
                self._film_to(sid, target.item, url, message)
            else:
                if self.hub is not None:
                    self.hub.forget_film(self._device(sid))  # a film opening comes no more
                self._send(sid, {**message, "url": self._phone_link(target.item, url)})
            if first:  # a zap on the phone after that says nothing more
                name = self._names.get(self._devices.get(sid, ""), "")
                text = PLAYING_ON_TEXT if name else PLAYING_ON_PHONE_TEXT
                self._window.screens.notify(translate(text, name=name))
            self._handed_to = self._device(sid)  # it plays: the others let go (handedOn)
            return True, ""

        try:
            handed = controller.play_elsewhere(launch, chosen, ask_on=sid, recordings=True)
        except Exception as error:  # a phone's ask must never take QiTV down
            logger.error("Phone remote: playing on the phone failed: %s", safe_message(error))
            handed = False
        if not handed:
            self._send(sid, {"t": "play", "failed": True})

    def _on_phone_asked(self, sid: str, text: str, yes: str, no: str) -> None:
        """QiTV asks phone `sid` (2.6: stop a recording for its Watch?)."""
        if sid not in self._phones:
            self._window.controller.answer_phone(False)  # gone meanwhile: nothing stops
            return
        self._ask_number = (self._ask_number + 1) % MAX_ASK
        self._asks[sid] = self._ask_number
        self._send(sid, {"t": "ask", "id": self._ask_number, "text": text, "yes": yes, "no": no})

    def _on_phone_settled(self, sid: str) -> None:
        """Phone `sid`'s question went otherwise (a newer one, the TV's or another phone's):
        its sheet closes, and nothing opens there (the phone says so, not "Opening…")."""
        number = self._asks.pop(sid, None)
        if number is not None:
            self._send(sid, {"t": "unask", "id": number})
            self._send(sid, {"t": "play", "failed": True, "why": "elsewhere"})

    def _on_answered(self, sid: str, number: int, yes: bool) -> None:
        if self._caption_asks.get(sid) == number:  # 2.9: AI captions' download question
            self._answer_captions(sid, yes)
            return
        if self._asks.get(sid) != number:
            return  # a question no longer asked
        del self._asks[sid]
        self._window.controller.answer_phone(yes)
        if not yes:  # Keep recording: nothing opens on the phone
            self._send(sid, {"t": "play", "failed": True, "why": "recording"})

    def _film_to(self, sid: str, item: dict, url: str, message: dict) -> None:
        """An MKV film (Safari plays none) goes to the phone as HLS from QiTV's home door
        (services/remote/films.py), which reads it from its provider as the phone plays it.
        The relay carries no film: a phone through it is told to be on QiTV's home network
        (one through the relay at home didn't reach the home door: a guest network, a VPN)."""
        if sid in self._relayed or self.hub is None:
            self._send(sid, {"t": "play", "failed": True, "why": "relay"})
            return
        catalog, config = self._window.catalog, self._window.config_manager
        try:
            entry = catalog.provider_entry(provider_of(item) or catalog.current_provider_name())
        except KeyError:
            entry = {}
        verify = bool(entry.get("ssl_verify", config.ssl_verify))
        headers = {"User-Agent": user_agent_for(item, str(entry.get("type") or ""))}
        self.hub.open_film(sid, self._device(sid), url, headers, verify, message)

    def _recording_to(self, sid: str, path: str, message: dict) -> None:
        """2.8: a recording goes to the phone as HLS from QiTV's home door, read from the disk
        (services/remote/recorded.py); as a film, never through the relay."""
        if sid in self._relayed or self.hub is None:
            self._send(sid, {"t": "play", "failed": True, "why": "rec_relay"})
            return
        window = self._window

        def writing() -> bool:  # asked from the home door's thread: reads only
            paths = getattr(window, "writing_paths", None)
            return bool(paths is not None and path in paths())

        self.hub.open_recording(sid, self._device(sid), path, message, writing=writing)

    def _resume_key(self, target: Target) -> Optional[str]:
        """Where the position of what a phone plays is kept, as on the TV: a recording's id
        (2.8), a title's content id; None for a channel."""
        if target.local:
            return str(target.item.get("id") or "") or None
        return cast(Optional[str], self._window.controller.resume_key(target))

    def _position(self, sid: str, position_ms: int, length_ms: int, play: int) -> None:
        """How far a phone got in the film or episode it plays: kept as the TV keeps its own,
        so the TV (and the Library's Continue) goes on from there, and told to its media server
        (2.8). Only a report of the play handed last: one made before a zap (on its way
        meanwhile) is another title's. A page from before 2.8 says no number (`play` -1): its
        reports are kept, as 2.7 kept them."""
        watched = self._watching.get(self._device(sid))
        if watched is None or play not in (-1, self._plays.get(self._device(sid))):
            return
        key = self._resume_key(watched[0])
        if key:
            self._window.watch.update_position(key, position_ms, length_ms)
        link = self._links.get(self._device(sid))
        if link and not watched[0].live:
            self._window.controller.phone_position(link, position_ms)

    def _to_tv(self, sid: str) -> None:
        """The phone hands back what it played: the TV plays it."""
        watched = self._watching.pop(self._device(sid), None)
        if watched is None:
            return
        if self.hub is not None:
            self.hub.forget_film(self._device(sid))  # nor reads (nor still opens) it for the phone
        target, group = watched
        window = self._window
        if target.live:
            window.screens.tune_live(target.item, group)
            return
        if window.screens.library_shown():
            window.screens.back_to_tv()
        if target.local:  # a recording (2.8)
            window.controller.play_recording(target.item)
            return
        window.controller.play_vod(target.item, target.item_type, target.context)

    def _stop_phones(self) -> None:
        """The TV plays again: phones watching on their own let go (one screen at a time)."""
        if self._watching and self._window.controller.showing_here():
            for sid in self._phones:
                if self._device(sid) in self._watching:
                    self._send(sid, {"t": "stop"})
            self._watching.clear()
            if self.hub is not None:
                self.hub.drop_films()  # nor reads a film for them any more

    def _on_handed_on(self) -> None:
        """An outside player took over what the TV played: a phone's Watch (the one just
        handed it plays on), or Play in VLC. Every other phone watching lets go, as when the
        TV plays again: one screen at a time, and the stream QiTV holds for an outside player
        is the newest one's."""
        keep, self._handed_to = self._handed_to, None
        gone = [device for device in self._watching if device != keep]
        if not gone:
            return
        for sid in self._phones:
            if self._device(sid) in gone:
                self._send(sid, {"t": "stop"})
        for device in gone:
            del self._watching[device]
            if self.hub is not None:
                self.hub.forget_film(device)  # nor reads a film for it any more

    def _watch_gone(self, sid: str) -> None:
        """Session `sid` closed: if its device watched and has no other session open, its
        stream goes back after PHONE_GONE_S unless it comes back (`Opened`)."""
        device = self._device(sid)
        if device not in self._watching or device in self._gone_timers:
            return
        if any(other != sid and self._device(other) == device for other in self._phones):
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda device=device: self._watch_ended(device))
        self._gone_timers[device] = timer
        timer.start(int(PHONE_GONE_S * 1000))

    def _watch_ended(self, device: str) -> None:
        timer = self._gone_timers.pop(device, None)
        if timer is not None:
            timer.deleteLater()
        if self._watching.pop(device, None) is None:
            return
        if self.hub is not None:
            self.hub.forget_film(device)
        if not self._watching:
            self._window.controller.phone_stopped()

    def _device(self, sid: str) -> str:
        return self._devices.get(sid) or sid

    def _phone_link(self, item: dict, url: str) -> str:
        """`url` as a phone's browser plays it: an Xtream channel as HLS (Safari plays no
        MPEG-TS); any other as it is."""
        catalog = self._window.catalog
        try:
            entry = catalog.provider_entry(provider_of(item) or catalog.current_provider_name())
        except KeyError:
            return url
        if is_media_server(entry.get("type")):
            # 2.8: a server on this computer, as the phone reaches it (home's address).
            return phone_reachable(url, home_ipv4())
        if str(entry.get("type") or "").upper() != "XTREAM":
            return url
        return xtream_hls(url, str(entry.get("username") or ""), str(entry.get("password") or ""))

    def _push_quiet_results(self) -> None:
        """What a phone's own search found (the desktop's search, every provider's), for each
        phone whose text it answers; then the next phone waiting has its text searched for."""
        searcher = self._title_searcher
        if self.hub is None or searcher is None or searcher.searching:
            return  # what is on its way answers it
        for sid, text in self._quiet.items():
            if text == searcher.query:
                self._quiet_waiting.discard(sid)
                self._send(sid, results_of(searcher.hits or [], text, self.result_handles))
        waiting = [text for sid, text in self._quiet.items() if sid in self._quiet_waiting]
        if waiting:
            searcher.set_query(waiting[0])  # one search at a time: the next phone's turn

    def _search(self, sid: str, text: str, tv: bool = True) -> None:
        if not tv:  # the phone's own search: the TV's picture stays
            if text.strip():
                self._quiet[sid] = text
                self._quiet_waiting.add(sid)
                self._searcher().set_query(text)
            else:
                self._quiet.pop(sid, None)
                self._quiet_waiting.discard(sid)
                self._send(sid, results_of([], text, self.result_handles))
                self._release_finds()
            return
        screens = self._window.screens
        if screens.library_shown():
            self._window.library_controller.remote_search(text)
        else:
            screens.open_search(text)

    # ---- the large picture's sound and subtitles (2.9) --------------------------------------

    def _tracks_number(self) -> int:
        """The number of what plays large: another title or channel (a zap, a swap) is another
        number, so a pick made in the list of the one before is not taken for it."""
        current = self._window.controller.now_playing()
        if current is not self._tracks_target:
            self._tracks_target = current
            self._tracks_of = (self._tracks_of + 1) % MAX_ASK
        return self._tracks_of

    def _push_tracks(self, asked: str = "") -> None:
        """The phones' Subtitles and Sound sheets follow what changed (a track, a zap, AI
        captions' state): looked at every DIFF_MS while one has asked, as the desktop's panels
        follow libVLC. `asked`: the phone that just asked, which hears the list unchanged too."""
        if not self._track_phones or self.hub is None:
            return
        try:
            message = tracks(self._window, self._tracks_number())
        except Exception as error:  # a phone's sheet must never take QiTV down
            logger.error("Phone remote: the tracks failed: %s", safe_message(error))
            return
        if message != self._last_tracks:
            self._last_tracks = message
            for sid in self._track_phones:
                self._send(sid, message)
        elif asked:
            self._send(asked, message)

    def _picked(self, of: int) -> Any:
        """The TrackControls a pick from the list `of` acts on (None: that list is of what
        played before, or nothing does)."""
        controls = self._window.controller.track_controls()
        return controls if controls is not None and of == self._tracks_number() else None

    def _pick_track(self, event: TrackPick) -> None:
        controls = self._picked(event.of)
        try:
            if controls is not None and event.kind == "sound":
                controls.choose_sound(event.track)
            elif controls is not None:
                controls.choose_subtitles(event.track)  # AI captions go off, as on the TV
        except Exception as error:  # a phone's pick must never take QiTV down
            logger.error("Phone remote: choosing a track failed: %s", safe_message(error))
        self._track_phones.add(event.sid)
        self._push_tracks(asked=event.sid)

    def _switch_captions(self, event: CaptionsSwitch) -> None:
        """AI captions on or off; their first time, the download's question shows on the TV
        and on this phone (`_ask_captions`)."""
        controls = self._picked(event.of)
        captions = getattr(self._window, "captions", None)
        try:
            if controls is not None:
                controls.set_captions(event.on)
                if event.on and captions is not None and captions.asking():
                    self._ask_captions(event.sid, captions)
        except Exception as error:  # a phone's pick must never take QiTV down
            logger.error("Phone remote: AI captions failed: %s", safe_message(error))
        self._track_phones.add(event.sid)
        self._push_tracks(asked=event.sid)

    def _caption_language(self, event: CaptionsLanguage) -> None:
        controls = self._picked(event.of)
        try:
            if controls is not None:
                controls.choose_caption_language(event.language)
        except Exception as error:  # a phone's pick must never take QiTV down
            logger.error("Phone remote: AI captions' language failed: %s", safe_message(error))
        self._track_phones.add(event.sid)
        self._push_tracks(asked=event.sid)

    def _ask_captions(self, sid: str, captions: Any) -> None:
        """AI captions' download question on the phone that turned them on, as QiTV's other
        questions ask there; the TV's card asks the same meanwhile."""
        text, yes, no = captions.question()
        self._ask_number = (self._ask_number + 1) % MAX_ASK
        self._caption_asks[sid] = self._ask_number
        self._send(sid, {"t": "ask", "id": self._ask_number, "text": text, "yes": yes, "no": no})

    def _answer_captions(self, sid: str, yes: bool) -> None:
        """A phone answered AI captions' download question: the TV's card goes, and so does
        the question on any other phone that turned them on."""
        self._unask_captions(but=sid)
        captions = getattr(self._window, "captions", None)
        try:
            if captions is not None:
                captions.answer(yes)
        except Exception as error:  # a phone's answer must never take QiTV down
            logger.error("Phone remote: AI captions' answer failed: %s", safe_message(error))

    def _unask_captions(self, but: str = "") -> None:
        asks, self._caption_asks = self._caption_asks, {}
        for sid, number in asks.items():
            if sid != but:
                self._send(sid, {"t": "unask", "id": number})

    def _on_captions_changed(self) -> None:
        """AI captions' state changed: a question answered on the TV (or left unanswered there)
        closes on the phones, and their sheets follow ("Downloading 40%", the language)."""
        captions = getattr(self._window, "captions", None)
        if self._caption_asks and (captions is None or not captions.asking()):
            self._unask_captions()
        self._push_tracks()

    # ---- casting and scheduled recordings (2.8) ---------------------------------------------

    def _cast_list(self) -> dict:
        """The devices QiTV can cast to (looked for from the first ask), and the one cast to."""
        controls = self._window.controller.cast_controls()
        if controls is None:
            return {"t": "cast", "devices": [], "on": ""}
        devices = [
            {"name": name[:MAX_DEVICE], "on": checked}
            for _text, name, checked in controls.menu_entries()
            if name
        ]
        message = {"t": "cast", "devices": devices, "on": controls.device() or ""}
        if not controls.available():
            message["off"] = True  # this computer's VLC cannot cast: the phone says so
        return message

    def _push_cast(self) -> None:
        if self._phones and self.hub is not None:
            self.hub.broadcast(self._cast_list())

    def _cast_to(self, to: str) -> None:
        controls = self._window.controller.cast_controls()
        if controls is None:
            return
        try:
            if to:
                controls.cast_to(to)
            else:
                controls.stop()
        except Exception as error:  # a phone's ask must never take QiTV down
            logger.error("Phone remote: casting failed: %s", safe_message(error))
        self._push_cast()

    def _schedule_from_phone(self, sid: str, handle: str, do: str) -> None:
        """A timetable row's chip: its programme recorded at its time, or cancelled; the row
        shows it at once (the timetable is sent again)."""
        found = self._find_handle(sid, handle)
        if found is None or found[0] != "sched":
            return
        channel, programme = cast(tuple, found[1])
        window = self._window
        try:
            if do == "cancel":
                window.cancel_programme(channel, programme)
            else:
                window.schedule_programme(channel, programme, do == "series")
        except Exception as error:
            logger.error("Phone remote: scheduling failed: %s", safe_message(error))
        guide = self._guides.get(sid)
        if guide is not None:
            guide.refresh_timetable()

    def _timer_from_phone(self, event: Timer) -> None:
        """2.8: "Record at a time" on a timetable's channel, as the desktop's dialog does; the
        phone is told what was scheduled (or that the time is over), its timetable sent again
        (a one-time one may be a programme it lists)."""
        found = self._find_handle(event.sid, event.handle)
        reply: dict = {"t": "timer", "id": event.handle}
        text = ""
        try:
            if found is not None and found[0] == "channel":  # else a list no longer shown
                channel = cast(tuple, found[1])[0]
                day = date.fromisoformat(event.day) if event.day else date.today()
                title = event.title.strip()
                choice = TimerChoice(title, day, event.begin, event.minutes, event.days)
                if not choice.days and choice.stop() <= datetime.now().astimezone():
                    reply["failed"] = "passed"
                else:
                    text = self._window.schedule_timer(channel, choice)
        except Exception as error:  # a phone's ask must never take QiTV down
            logger.error("Phone remote: scheduling failed: %s", safe_message(error))
        if text:
            reply["text"] = text
        elif "failed" not in reply:
            reply["failed"] = "error"
        self._send(event.sid, reply)
        guide = self._guides.get(event.sid)
        if guide is not None:
            guide.refresh_timetable()

    def _find_handle(self, sid: str, handle: str) -> Optional[tuple[str, object]]:
        found = self.result_handles.get(handle)
        lists = (self._guides.get(sid), self._channel_lists.get(sid), self._libraries.get(sid))
        for browsing in lists:
            if found is None and browsing is not None:
                found = browsing.handles.get(handle)
        return found

    def _open(self, sid: str, handle: str) -> None:
        found = self._find_handle(sid, handle)
        if found is None:
            return
        kind, payload = found
        window = self._window
        try:
            if kind == "channel":
                channel, group_key, channels = cast(tuple, payload)
                group = (group_key, channels) if group_key else None
                window.screens.tune_live(channel, group)
            elif kind == "catchup":
                window.screens.watch_from_start(*cast(tuple, payload))
            elif kind == "hit":
                window.library_controller.activate_hit(payload)
            elif kind == "title":
                item, item_kind, genre_items = cast(tuple, payload)
                if item_kind == "series":  # its seasons show in the Library
                    window.screens.open_library("series")
                window.library_controller.activate_title(item, item_kind, genre_items)
        except Exception as error:
            logger.error("Phone remote: opening a %s failed: %s", kind, safe_message(error))
        if self._phones:
            self._soon.start(AFTER_PRESS_MS)

    def _persist(self, data: dict) -> None:
        config = self._window.config_manager
        current = self._config()
        data = dict(data)
        for switch in _SWITCHES:  # a save queued behind a Settings change must not undo it
            if switch in current:
                data[switch] = current[switch]
        config.remote = data
        try:
            config.save_config()
        except Exception as error:
            logger.warning("Couldn't save the phone remote's pairing: %s", safe_message(error))
        self._names_from(data)

    def _names_from(self, data: dict) -> None:
        devices = [d for d in data.get("devices", []) if isinstance(d, dict)]
        present = {str(d.get("id")) for d in devices}
        for device_id, name in list(self._removing.items()):
            if device_id not in present:
                del self._removing[device_id]
                if name:
                    self._window.screens.notify(translate(REMOVED_TEXT, name=name))
        self._names = {str(d.get("id")): str(d.get("name", "")) for d in devices}
        self.devicesChanged.emit(
            [
                {"id": d.get("id"), "name": d.get("name"), "last_used": d.get("last_used")}
                for d in devices
            ]
        )


def _changed(last: dict, now: dict, elapsed_ms: int) -> bool:
    """Whether a phone should hear `now`: anything but a playing film's clock moving on."""
    a, b = dict(last), dict(now)
    a_now, b_now = dict(a.pop("now") or {}), dict(b.pop("now") or {})
    if a != b:
        return True
    a_pos, b_pos = a_now.pop("pos_ms", None), b_now.pop("pos_ms", None)
    if a_now != b_now:
        return True
    if a_pos is None or b_pos is None:
        return bool(a_pos != b_pos)
    expected = a_pos + (elapsed_ms if a_now.get("playing") else 0)
    return bool(abs(b_pos - expected) > SEEK_DRIFT_MS)
