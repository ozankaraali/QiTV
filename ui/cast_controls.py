"""Casting (2.8): the player bar's cast button and its menu, and what a cast does.

The button opens a menu of the devices found on the network (`services.casting`; looking for
them starts the first time it opens), and, while casting, "Stop casting". Casting leaves the
multiview and closes the picture in picture (one picture goes to the device), then opens what
plays again with the device as its output: a channel from its time-shift buffer where it was, a
film where it is. A pill says "Playing on <device>", with Stop casting. The cast stays on for
whatever plays next until it is stopped, or until the device goes away (said in a toast).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint
from PySide6.QtWidgets import QMenu, QWidget

from services.casting import CastDevices
from services.i18n import N_, translate

logger = logging.getLogger(__name__)

# i18n: the cast menu while no device was found yet (a TV that can be cast to, a Chromecast)
LOOKING = N_("Looking for devices…")
# i18n: the cast menu: this computer's VLC has no casting (its renderer modules are missing)
NO_CASTING = N_("Casting isn't available on this computer")
# i18n: the cast menu's entry that sends the picture back to this computer
STOP_CASTING = N_("Stop casting")
# i18n: a pill while casting ({name}: the device, such as "Living Room TV")
PLAYING_ON = N_("Playing on {name}")
# i18n: a toast: the device being cast to left the network (or turned off)
DEVICE_GONE = N_("{name} is no longer available. Playing here again.")


class CastControls(QObject):
    def __init__(
        self,
        *,
        devices: CastDevices,
        session: Callable[[], Any],
        live: Callable[[], Any],
        current: Callable[[], Any],
        one_picture: Callable[[], None],
        transport: Any,
        notice: Any,
        toast: Callable[..., None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`session()` is the large picture's player, `live()` its LiveSession, `current()`
        what it plays (a Target, None: nothing); `one_picture()` leaves the multiview and
        closes the picture in picture; `notice` is a CastNotice (None-able)."""
        super().__init__(parent)
        self._devices = devices
        self._session = session
        self._live = live
        self._current = current
        self._one_picture = one_picture
        self._transport = transport
        self._notice = notice
        self._toast = toast
        self._device: Optional[str] = None  # the device cast to
        devices.changed.connect(self._on_devices_changed)
        if transport is not None:
            transport.set_cast_state(True, False)
        if notice is not None and hasattr(notice, "keepWatching"):
            notice.keepWatching.connect(self.stop)  # its Stop casting

    def casting(self) -> bool:
        return self._device is not None

    def available(self) -> bool:
        """Casting works here: VLC looks for devices (or has found some)."""
        return self._devices.searching() or bool(self._devices.devices())

    def device(self) -> Optional[str]:
        return self._device

    # ---- the menu ---------------------------------------------------------------------------

    def menu_entries(self) -> list[tuple[str, Optional[str], bool]]:
        """(text, device name or "" for Stop casting or None for a note, checked). Without
        casting in VLC (no renderer modules) it says so, and the cast button goes."""
        self._devices.start()
        entries: list[tuple[str, Optional[str], bool]] = [
            (device.name, device.name, device.name == self._device)
            for device in self._devices.devices()
        ]
        if not self.available():
            if self._transport is not None:
                self._transport.set_cast_state(False, False)
            return [(translate(NO_CASTING), None, False)]
        if not entries:
            entries.append((translate(LOOKING), None, False))
        if self._device is not None:
            entries.append((translate(STOP_CASTING), "", False))
        return entries

    def show_menu(self, global_pos: QPoint, parent: Optional[QWidget] = None) -> None:
        menu = QMenu(parent)
        for text, name, checked in self.menu_entries():
            action = menu.addAction(text)
            if name is None:
                action.setEnabled(False)
            elif name == "":
                action.triggered.connect(self.stop)
            else:
                action.setCheckable(True)
                action.setChecked(checked)
                action.triggered.connect(lambda _on=False, n=name: self.cast_to(n))
        menu.popup(global_pos)

    # ---- casting ----------------------------------------------------------------------------

    def cast_to(self, name: str) -> None:
        """Send what plays (and what plays next) to the device `name`."""
        device = self._devices.find(name)
        if device is None or name == self._device:
            return
        self._one_picture()
        self._device = name
        logger.info("Casting to a %s device.", device.kind or "cast")
        self._session().cast_to(device.handle)
        self._reopen()
        if self._transport is not None:
            self._transport.set_cast_state(True, True)
        if self._notice is not None:
            self._notice.show_notice(translate(PLAYING_ON, name=name))

    def stop(self) -> None:
        """What plays comes back to this computer's picture."""
        if self._device is None:
            return
        self._device = None
        logger.info("Casting stopped.")
        self._session().cast_to(None)
        self._reopen()
        if self._transport is not None:
            self._transport.set_cast_state(True, False)
        if self._notice is not None:
            self._notice.hide()

    def shutdown(self) -> None:
        """QiTV closes: discovery ends and the devices are let go."""
        self._devices.stop()

    def _reopen(self) -> None:
        """What plays opens again with its new output, from where it is."""
        target = self._current()
        if target is None:
            return  # nothing plays: the next play goes there
        session = self._session()
        live = self._live() if target.live else None
        if live is not None and live.reopen_player():
            return  # a channel from its time-shift buffer, where it was
        session.reopen(None if target.live else session.time_ms())

    def _on_devices_changed(self) -> None:
        name = self._device
        if name is not None and self._devices.find(name) is None:
            self.stop()
            self._toast(translate(DEVICE_GONE, name=name), "error")
