"""Picture in picture in the TV shell (spec §7, Phase 4): a second channel, small.

P (or the transport's Second channel) opens the channel tuned before the current one small,
or closes the small picture; the drawer's context menu and Ctrl/⌘+P open the drawer's
channel. Its link is asked for on LinkLanes' side lane, one at a time: a choice made while
one is on its way waits, replacing an older one that waits, and a link that lands after the
PiP was closed, or after the large picture took its channel, opens nothing. SessionManager
opens it in the slot that is not large, with a main tune's start arguments; its refusals
and failures are toasted here.

The PiP frame and label follow the manager: the second surface shows at its corner while
PiP is open, and the label names the small picture's channel and state ("Connecting…" until
it plays; "−MM:SS" once time-shifted beyond its target, else "LIVE"). The label's × closes
the small picture, its speaker asks for the mixer, and a click on the small picture swaps
(the shell's `swap`).

What each slot shows (its target and the zap group it came from) is kept here, so a swap
can give the shell the new large picture's (`take_large`).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from services.live.stay_live import TARGETS
from services.sessions import SessionManager, Slot, limit_message
from services.zapping import ZapContext
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, start_kwargs
from ui.overlays.pip_label import CONNECTING, LIVE
from ui.overlays.transport import fmt_behind

logger = logging.getLogger(__name__)

PIP_LIVE_ONLY = "Picture-in-picture is for live TV"
NO_PREVIOUS = "No previous channel yet"
OPEN_PIP_FIRST = "Open picture-in-picture first (P)"
ALREADY_LARGE = "This channel is already playing"
SHIFTED_MARGIN_S = 1.0  # the label says "−MM:SS" once the lag is this far beyond the target
ERROR_MS = 6000


@dataclass
class SlotView:
    """What a slot plays, for the shell: its target and the zap group it was tuned from."""

    target: Optional[Target] = None
    context: Optional[ZapContext] = None


class PipControls(QObject):
    changed = Signal()  # PiP opened, closed, swapped, or its small channel changed

    def __init__(
        self,
        *,
        sessions: SessionManager,
        frame,
        label,
        transport,
        catalog,
        config,
        lanes: LinkLanes,
        live_start: Callable[[Target, str], LiveStart],
        previous: Callable[[], tuple[Optional[dict], Optional[ZapContext]]],
        large_channel: Callable[[], Optional[dict]],
        vod: Callable[[], bool],
        closed: Callable[[], bool],
        toast: Callable[..., None],
        swap: Callable[[], bool],
        mixer: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`frame` is the PipFrame and `label` the PipLabel (None in tests without them).

        `live_start(target, url)` is a main tune's LiveStart; `previous()` the channel tuned
        before the large picture's, with its zap group; `large_channel()` what the large
        picture plays; `vod()` whether a movie or an episode is current; `swap()` the
        shell's swap; `mixer()` opens the mixer on the PiP's row.
        """
        super().__init__(parent)
        self.sessions = sessions
        self._frame = frame
        self._label = label
        self._transport = transport
        self.catalog = catalog
        self.config = config
        self._lanes = lanes
        self._live_start = live_start
        self._previous = previous
        self._large_channel = large_channel
        self._vod = vod
        self._closed = closed
        self._toast = toast
        self._swap = swap
        self._mixer = mixer
        self._views: dict[str, SlotView] = {}
        self._wanted: Optional[tuple[Target, ZapContext]] = None  # its link is on its way
        self._link: Optional[Target] = None  # the one link request in flight
        self._timelines: dict[str, dict] = {}  # each slot's last timeline since it opened

    def wire(self) -> None:
        sessions = self.sessions
        sessions.pipStateChanged.connect(self._on_pip_state)
        sessions.denied.connect(self._on_denied)
        sessions.pipFailed.connect(self._on_failed)
        sessions.slotTimeline.connect(self._on_timeline)
        for slot in (sessions.large(), sessions.other()):
            slot.live.modeChanged.connect(self._refresh_label)
        self._lanes.sideFailed.connect(self._on_link_failed)
        self._transport.pipRequested.connect(self.toggle_previous)
        self._transport.set_pip_state(True, False)
        if self._frame is not None:
            self._frame.swapRequested.connect(self._swap)
        label = self._label
        if label is not None:
            label.closeRequested.connect(self.close)
            label.mixerRequested.connect(self._mixer)

    # ---- state --------------------------------------------------------------------------------

    def busy(self) -> bool:
        """PiP is open, opening, or its link is on its way."""
        sessions = self.sessions
        return self._wanted is not None or sessions.pip_open() or sessions.pip_opening()

    def small_showing(self, channel: dict) -> Optional[Slot]:
        """The small slot when it shows `channel` (the large picture may take it by a swap)."""
        small = self.sessions.small()
        if small is not None and ZapContext.same(small.channel, channel):
            return small
        return None

    def claims(self, channel: dict) -> bool:
        """`channel` waits to open small: its link is on its way, or its stream is."""
        wanted = self._wanted
        if wanted is not None and ZapContext.same(wanted[0].item, channel):
            return True
        other = self.sessions.other()
        return self.sessions.pip_opening() and ZapContext.same(other.channel, channel)

    def view(self, name: str) -> SlotView:
        return self._views.setdefault(name, SlotView())

    def take_large(self, old: str, new: str, target: Optional[Target], context) -> SlotView:
        """A swap: keep the old large slot's target and zap group; the new large one's view."""
        self._views[old] = SlotView(target, context)
        return self.view(new)

    # ---- opening and closing ------------------------------------------------------------------

    def toggle_previous(self) -> None:
        """P: close the small picture, else open the previous channel small."""
        if self._closed():
            return
        if self.busy():
            self.close()
            return
        if self._vod():
            self._toast(PIP_LIVE_ONLY)
            return
        previous, context = self._previous()
        if previous is None:
            self._toast(NO_PREVIOUS)
            return
        self.open_channel(previous, context)

    def open_channel(self, channel: dict, context: Optional[ZapContext]) -> None:
        """Open `channel` small, or show it in the small picture instead of what it shows."""
        if self._closed():
            return
        if self._vod():
            self._toast(PIP_LIVE_ONLY)
            return
        if ZapContext.same(channel, self._large_channel()):
            self._toast(ALREADY_LARGE)
            return
        if self.small_showing(channel) is not None:
            return  # already small
        title = str(channel.get("name") or "")
        group = context.group_key if context is not None else ""
        target = Target(channel, "live", title, group_key=group)
        self._wanted = (target, context if context is not None else ZapContext())
        self._request(target)

    def close(self) -> None:
        """Close the small picture, or forget the one waiting to open."""
        self._wanted = None
        sessions = self.sessions
        if sessions.pip_open() or sessions.pip_opening():
            sessions.close_pip()

    def _request(self, target: Target) -> None:
        if self._link is not None:
            return  # the newest choice waits in _wanted for the link in flight
        self._link = target
        self._lanes.request_side(target, lambda url: self._on_link(target, url), play=False)

    def _on_link(self, target: Target, url: str) -> None:
        if target is not self._link:
            return
        self._link = None
        wanted = self._wanted
        if self._closed() or wanted is None:
            return  # closed meanwhile
        if wanted[0] is not target:
            self._request(wanted[0])  # chosen since: only the newest is opened
            return
        self._wanted = None
        if self._vod() or ZapContext.same(target.item, self._large_channel()):
            return  # a movie started, or the large picture took the channel, meanwhile
        self._open(target, wanted[1], url)

    def _on_link_failed(self, target: Target, _message: str) -> None:
        """The shell toasts the failure; a newer choice asks for its own link."""
        if target is not self._link:
            return
        self._link = None
        wanted = self._wanted
        if wanted is not None and wanted[0] is target:
            self._wanted = None
        elif wanted is not None:
            self._request(wanted[0])

    def _open(self, target: Target, context: ZapContext, url: str) -> None:
        sessions = self.sessions
        slot = sessions.other()
        self._views[slot.name] = SlotView(target, context)
        self._timelines.pop(slot.name, None)
        kwargs = start_kwargs(self._live_start(target, url), self.config)
        sessions.open_pip(
            target.item,
            provider=self.catalog.current_provider(),
            xtream_max=self.catalog.max_connections(),
            start_kwargs=kwargs,
        )
        slot.group_key = target.group_key
        if sessions.pip_open():
            self._refresh_label()  # the small picture's channel changed
            self.changed.emit()

    # ---- the manager's reports ----------------------------------------------------------------

    def _on_pip_state(self, open_: bool) -> None:
        sessions, frame, label = self.sessions, self._frame, self._label
        if open_:
            if frame is not None:
                frame.show_pip()
            if label is not None:
                self._refresh_label()
                label.show()
        else:
            closed = sessions.other()
            self._views.pop(closed.name, None)
            self._timelines.pop(closed.name, None)
            if frame is not None:
                frame.hide_pip()  # the main surface large: the picture that stayed may not be
                frame.set_large(sessions.large().name)
            if label is not None:
                label.hide()
        self._transport.set_pip_state(True, open_)
        self.changed.emit()

    def after_swap(self) -> None:
        """The roles changed: the frame and the label follow."""
        if self._frame is not None:
            self._frame.set_large(self.sessions.large().name)
        self._refresh_label()
        self.changed.emit()

    def _on_denied(self, limit: int) -> None:
        if not self._closed():
            self._toast(limit_message(limit), "error", ERROR_MS)

    def _on_failed(self, message: str) -> None:
        if not self._closed():
            self._toast(message, "error", ERROR_MS)

    def _on_timeline(self, name: str, state: dict) -> None:
        self._timelines[name] = state
        small = self.sessions.small()
        if small is not None and small.name == name:
            self._refresh_label()

    def refresh_label(self) -> None:
        self._refresh_label()

    def _refresh_label(self, *_args) -> None:
        label, small = self._label, self.sessions.small()
        if label is None or small is None:
            return
        target = self.view(small.name).target
        name = target.title if target is not None else str((small.channel or {}).get("name", ""))
        label.set_info(name, self._state_text(small))
        label.set_audible(not self.sessions.muted(small.name))

    def _state_text(self, slot: Slot) -> str:
        mode = slot.live.mode
        timeline = self._timelines.get(slot.name)
        if mode == "pipeline" and timeline is not None:
            lag = float(timeline.get("lag", 0.0))
            target = float(timeline.get("target", TARGETS["normal"]))
            return fmt_behind(lag) if lag > target + SHIFTED_MARGIN_S else LIVE
        direct = getattr(slot.tuner, "playing_direct", None)  # a channel VLC plays itself
        if mode == "direct" or (direct is not None and direct()):
            return LIVE
        return CONNECTING
