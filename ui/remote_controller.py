"""The phone remote on the GUI thread: presses become key presses or actions, holds repeat here.

The remote's network thread (services/remote/hub.py) hands its events to `bridge.post`, a signal
of an object made on this thread, so they arrive here queued. A press goes through
TVWindow.remote_key (the keyboard's own path, resolved in the TV's context) or, for buttons
whose meaning must not change with what's open, straight to an action: volume and mute act
where the TV is, channel and playback buttons first leave the Library.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional, cast

from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Signal

from services.browsed_catalog import BrowsedCatalog
from services.i18n import N_, translate
from services.provider_api import xtream_hls
from services.provider_ids import provider_of
from services.redact import safe_message
from services.remote.protocol import (
    BUTTON_META,
    STORE_VERSION,
    Closed,
    Code,
    Get,
    Held,
    Opened,
    OpenItem,
    Paired,
    Press,
    Query,
    Status,
    StoreSaved,
    ToTv,
    WatchHere,
)
from ui.keymap import Action
from ui.link_lanes import Target
from ui.remote_buttons import BUTTONS
from ui.remote_guide import PhoneGuide
from ui.remote_library import PhoneLibrary
from ui.remote_repeat import KeyRepeater
from ui.remote_snapshots import TITLE_CHARS, Handles, results, state

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
PHONE_GONE_TEXT = N_("The phone left before the stream reached it")
# i18n: the pairing screen when the phone remote couldn't start (no free network port)
START_FAILED_TEXT = N_("The phone remote couldn't start. Is another program using its port?")


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
        # Each phone's catalog for its lists (its own provider choice), by device.
        self._phone_catalogs: dict[str, BrowsedCatalog] = {}
        # What a phone plays in its own player, and the group it was picked in (for the TV), by
        # device: a phone that slept comes back as another session.
        self._watching: dict[str, tuple[Target, Optional[tuple[str, list]]]] = {}
        self.result_handles = Handles(limit=500)  # the search's
        self._removing: dict[str, str] = {}  # device id → name, until the hub confirms
        self._phones: set[str] = set()
        self._last_state: Optional[dict] = None
        self._last_at = 0
        self._diff = QTimer(self)
        self._diff.setInterval(DIFF_MS)
        self._diff.timeout.connect(self._push_state)
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.timeout.connect(self._push_state)
        window.library_controller.searchChanged.connect(self._push_results)

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
        if self.hub is not None:
            self.hub.stop()
            self.hub = None

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
            self._repeater.drop(event.sid)
            self._schedule()
            self._phones.discard(event.sid)
            self._devices.pop(event.sid, None)
            for browsing in (self._guides, self._libraries, self._channel_lists):
                gone = browsing.pop(event.sid, None)
                if gone is not None:
                    gone.deleteLater()
            self._phone_guides()
            if not self._phones:
                self._diff.stop()
        elif isinstance(event, Opened):
            self._phones.add(event.sid)
            self._devices[event.sid] = event.device_id
            self._diff.start()
            self._send(event.sid, self._state())
        elif isinstance(event, Get):
            self._answer(event)
        elif isinstance(event, Query):
            self._search(event.text)
        elif isinstance(event, OpenItem):
            self._open(event.sid, event.handle)
        elif isinstance(event, WatchHere):
            self._watch_here(event.sid, event.handle)
        elif isinstance(event, ToTv):
            self._to_tv(event.sid)
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
            else:
                self._library(sid).titles(section, event.genre, event.page, event.find, event.n)
        except Exception as error:  # a phone's question must never take QiTV down
            logger.error("Phone remote: %s failed: %s", event.what, safe_message(error))
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
            guide = PhoneGuide(self._window, send, self, catalog=self._phone_catalog(sid))
            self._guides[sid] = guide
        return guide

    def _channel_list(self, sid: str) -> PhoneGuide:
        listed = self._channel_lists.get(sid)
        if listed is None:
            send = lambda m: self._send(sid, m)  # noqa: E731
            catalog = self._phone_catalog(sid)
            listed = PhoneGuide(self._window, send, self, bare=True, catalog=catalog)
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
            else:  # a series opens on the TV; anything else plays nowhere but there
                self._send(sid, {"t": "play", "failed": True})
                return

        def launch(target: Target, url: str) -> tuple[bool, str]:
            if sid not in self._phones:
                return False, PHONE_GONE_TEXT
            first = self._device(sid) not in self._watching
            self._watching[self._device(sid)] = (target, group)
            message = {"t": "play", "url": self._phone_link(target.item, url)}
            message["name"] = target.title[:TITLE_CHARS]
            if target.live:
                message["num"] = str(controller.channel_number(target.item) or "")
            self._send(sid, message)
            if first:  # a zap on the phone after that says nothing more
                name = self._names.get(self._devices.get(sid, ""), "")
                text = PLAYING_ON_TEXT if name else PLAYING_ON_PHONE_TEXT
                self._window.screens.notify(translate(text, name=name))
            return True, ""

        if not controller.play_elsewhere(launch, chosen):
            self._send(sid, {"t": "play", "failed": True})

    def _to_tv(self, sid: str) -> None:
        """The phone hands back what it played: the TV plays it."""
        watched = self._watching.pop(self._device(sid), None)
        if watched is None:
            return
        target, group = watched
        window = self._window
        if target.live:
            window.screens.tune_live(target.item, group)
            return
        if window.screens.library_shown():
            window.screens.back_to_tv()
        window.controller.play_vod(target.item, target.item_type, target.context)

    def _stop_phones(self) -> None:
        """The TV plays again: phones watching on their own let go (one screen at a time)."""
        if self._watching and self._window.controller.showing_here():
            for sid in self._phones:
                if self._device(sid) in self._watching:
                    self._send(sid, {"t": "stop"})
            self._watching.clear()

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
        if str(entry.get("type") or "").upper() != "XTREAM":
            return url
        return xtream_hls(url, str(entry.get("username") or ""), str(entry.get("password") or ""))

    def _search(self, text: str) -> None:
        screens = self._window.screens
        if screens.library_shown():
            self._window.library_controller.remote_search(text)
        else:
            screens.open_search(text)

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
