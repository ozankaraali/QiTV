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

from services.i18n import N_, translate
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
)
from ui.keymap import Action
from ui.remote_buttons import BUTTONS
from ui.remote_guide import PhoneGuide
from ui.remote_library import PhoneLibrary
from ui.remote_repeat import KeyRepeater
from ui.remote_snapshots import Handles, results, state

if TYPE_CHECKING:
    from services.remote.hub import RemoteHub

logger = logging.getLogger(__name__)

# i18n: a toast; {name} is a phone's name, such as "iPhone · Safari"
PAIRED_TEXT = N_("{name} can now control QiTV")
# i18n: a toast; {name} is a phone's name, such as "iPhone · Safari"
REMOVED_TEXT = N_("{name} can no longer control QiTV")
# i18n: the pairing screen when the phone remote couldn't start (no free network port)
START_FAILED_TEXT = N_("The phone remote couldn't start. Is another program using its port?")
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
            for browsing in (self._guides, self._libraries, self._channel_lists):
                gone = browsing.pop(event.sid, None)
                if gone is not None:
                    gone.deleteLater()
            self._phone_guides()
            if not self._phones:
                self._diff.stop()
        elif isinstance(event, Opened):
            self._phones.add(event.sid)
            self._diff.start()
            self._send(event.sid, self._state())
        elif isinstance(event, Get):
            self._answer(event)
        elif isinstance(event, Query):
            self._search(event.text)
        elif isinstance(event, OpenItem):
            self._open(event.sid, event.handle)
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
                self._guide(sid).groups(event.page, event.find, event.n)
            elif event.what == "guide":
                self._guide(sid).channels(event.list, event.page, event.find, event.n)
            elif event.what == "timetable":
                self._guide(sid).timetable(event.handle)
            elif event.what == "chgroups":
                self._channel_list(sid).groups(event.page, event.find, event.n)
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
            guide = PhoneGuide(self._window, lambda message: self._send(sid, message), self)
            self._guides[sid] = guide
        return guide

    def _channel_list(self, sid: str) -> PhoneGuide:
        listed = self._channel_lists.get(sid)
        if listed is None:
            listed = PhoneGuide(self._window, lambda m: self._send(sid, m), self, bare=True)
            self._channel_lists[sid] = listed
        return listed

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

    def _search(self, text: str) -> None:
        screens = self._window.screens
        if screens.library_shown():
            self._window.library_controller.remote_search(text)
        else:
            screens.open_search(text)

    def _open(self, sid: str, handle: str) -> None:
        found = self.result_handles.get(handle)
        lists = (self._guides.get(sid), self._channel_lists.get(sid), self._libraries.get(sid))
        for browsing in lists:
            if found is None and browsing is not None:
                found = browsing.handles.get(handle)
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
