"""Casting (2.8): the devices libVLC finds to cast to (Chromecasts), for the cast menu.

libVLC 3 finds renderers with its renderer discoverers (Bonjour on macOS, microdns elsewhere:
whatever `libvlc_renderer_discoverer_list_get` names) and tells of each on a thread of its
own. A found item is valid only during that event, so it is held at once and handed to the GUI
thread through a queued signal; it is released when it goes, or when casting stops for good. A
player casts to an item with `libvlc_media_player_set_renderer` (`PlaybackSession.cast_to`):
the computer then streams what plays to the device, converting it when the device cannot play
its format.

Discovery starts the first time the cast menu opens (`CastDevices.start`), not with QiTV.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Callable, Optional, Protocol

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CastDevice:
    name: str  # as the device calls itself ("Living Room TV"); also its id in the menu
    kind: str  # libVLC's type: "chromecast"
    handle: Any  # the held libVLC renderer item


def renderer_text(value: Any) -> str:
    """A name or type libVLC gives (bytes in python-vlc) as text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class Discovery(Protocol):
    def start(
        self, found: Callable[[str, str, Any], None], lost: Callable[[str], None]
    ) -> bool: ...

    def stop(self) -> None: ...

    def release(self, handle: Any) -> None: ...


class LibVlcDiscovery:
    """libVLC's renderer discoverers on `instance` (`found`/`lost` run on libVLC's threads)."""

    def __init__(self, instance: Any) -> None:
        self._instance = instance
        self._discoverers: list[Any] = []
        self._found: Optional[Callable[[str, str, Any], None]] = None
        self._lost: Optional[Callable[[str], None]] = None

    def start(self, found: Callable[[str, str, Any], None], lost: Callable[[str], None]) -> bool:
        import vlc

        self._found, self._lost = found, lost
        services = vlc.ctypes.POINTER(vlc.ctypes.POINTER(vlc.RdDescription))()
        count = vlc.libvlc_renderer_discoverer_list_get(self._instance, vlc.ctypes.byref(services))
        names = [renderer_text(services[i].contents.name) for i in range(count)]
        if count:
            vlc.libvlc_renderer_discoverer_list_release(services, count)
        for name in names:
            discoverer = self._instance.renderer_discoverer_new(name)
            if discoverer is None:
                continue
            events = discoverer.event_manager()
            events.event_attach(vlc.EventType.RendererDiscovererItemAdded, self._on_added)
            events.event_attach(vlc.EventType.RendererDiscovererItemDeleted, self._on_deleted)
            if discoverer.start() == 0:
                self._discoverers.append(discoverer)
            else:
                discoverer.release()
        logger.info("Looking for devices to cast to (%d discoverers).", len(self._discoverers))
        return bool(self._discoverers)

    @staticmethod
    def _item(event: Any) -> Any:
        import vlc

        pointer = event.u.media  # the union's first member: the renderer item
        return vlc._Constructor(vlc.Renderer, pointer) if pointer else None

    def _on_added(self, event: Any, *_args: Any) -> None:
        import vlc

        try:
            item = self._item(event)
            if item is None or self._found is None:
                return
            held = vlc.libvlc_renderer_item_hold(item)  # valid past this event
            self._found(renderer_text(item.name()), renderer_text(item.type()), held)
        except Exception as e:  # never into libVLC's thread
            logger.warning("A cast device could not be read: %s", type(e).__name__)

    def _on_deleted(self, event: Any, *_args: Any) -> None:
        try:
            item = self._item(event)
            if item is not None and self._lost is not None:
                self._lost(renderer_text(item.name()))
        except Exception as e:
            logger.warning("A cast device's end could not be read: %s", type(e).__name__)

    def stop(self) -> None:
        for discoverer in self._discoverers:
            try:
                discoverer.stop()
                discoverer.release()
            except Exception as e:
                logger.debug("A discoverer did not stop: %s", type(e).__name__)
        self._discoverers = []

    def release(self, handle: Any) -> None:
        release(handle)


def hold(handle: Any) -> Any:
    """`handle` (a libVLC renderer item) held once more: valid until `release`d, whatever the
    discovery does with its own reference (a device found again gets a new item)."""
    import vlc

    try:
        vlc.libvlc_renderer_item_hold(handle)
    except Exception as e:
        logger.debug("A cast device was not held: %s", type(e).__name__)
    return handle


def release(handle: Any) -> None:
    import vlc

    try:
        vlc.libvlc_renderer_item_release(handle)
    except Exception as e:
        logger.debug("A cast device was not released: %s", type(e).__name__)


class CastDevices(QObject):
    """The devices found, by name, on the GUI thread (`changed` when the list changes)."""

    changed = Signal()
    _found = Signal(str, str, object)  # from a discovery thread
    _lost = Signal(str)

    def __init__(self, discovery: Discovery, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._discovery = discovery
        self._devices: dict[str, CastDevice] = {}
        self._started = False
        self._searching = False
        self._found.connect(self._on_found)
        self._lost.connect(self._on_lost)

    def start(self) -> None:
        """Look for devices (once; they keep coming and going while QiTV runs)."""
        if self._started:
            return
        self._started = True
        try:
            self._searching = self._discovery.start(self._found.emit, self._lost.emit)
        except Exception as e:  # no renderer modules: casting offers nothing
            logger.warning("Casting is not available: %s", type(e).__name__)
            self._searching = False

    def searching(self) -> bool:
        return self._searching

    def devices(self) -> list[CastDevice]:
        return sorted(self._devices.values(), key=lambda d: d.name.casefold())

    def find(self, name: str) -> Optional[CastDevice]:
        return self._devices.get(name)

    def stop(self) -> None:
        """QiTV closes: no more looking, every held item released."""
        if self._started:
            self._discovery.stop()
        for device in self._devices.values():
            self._discovery.release(device.handle)
        self._devices = {}
        self._searching = False

    def _on_found(self, name: str, kind: str, handle: Any) -> None:
        old = self._devices.get(name)
        self._devices[name] = CastDevice(name, kind, handle)
        if old is not None:
            self._discovery.release(old.handle)
        self.changed.emit()

    def _on_lost(self, name: str) -> None:
        device = self._devices.pop(name, None)
        if device is None:
            return
        self._discovery.release(device.handle)
        self.changed.emit()
