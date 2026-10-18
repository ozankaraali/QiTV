"""Picture in picture in the TV shell (spec §7, Phase 4): a second channel, small.

P (or the transport's Second channel) opens the channel tuned before the current one small,
or closes the small picture; the drawer's context menu and Ctrl/⌘+P open the drawer's
channel. S (or a click on the small picture) swaps; one that has to wait for the large
picture's channel to open says "Switching…". Its link is asked for on LinkLanes' side lane,
one at a time: a choice made while one is on its way waits, replacing an older one that
waits, and a link that lands after the PiP was closed, or after the large picture took its
channel, opens nothing. SessionManager opens it in the slot that is not large, with a main
tune's start arguments; its refusals and failures are toasted here, except that an STB
channel failing small gets a fresh link once (its portal signed in again when the page
browses another provider), as the large picture's does, and opens small again.

A movie or an episode from the Library takes the large picture and keeps its channel small
(`keep_large_small`); what takes the title's place there (Next, autoplay, another title)
keeps the small picture beside it (`keep_beside`).

The PiP frame and label follow the manager: the second surface shows at its corner while
PiP is open, and the label names the small picture's channel and state ("Connecting…" until
it plays; "−MM:SS" once time-shifted beyond its target, else "LIVE"); a movie or an episode
swapped small shows its title alone, and "Finished" once it ended there (swapped large
again, the Next card offers what follows, as at its end). The label's × closes the small
picture, its speaker mutes or unmutes it, and its ⇄, like a click on the small picture,
swaps (the shell's `swap`).

What each slot shows (its target, the zap group it came from and, for a movie or an episode,
its resume key) is kept here, so a swap can give the shell the new large picture's
(`take_large`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, Signal

from services.catalog import ALL_GROUP_ID
from services.live.refusals import STREAM_LIMIT
from services.live.stay_live import TARGETS
from services.navigation import payloads
from services.sessions import PURPOSE, SessionManager, Slot, limit_message
from services.zapping import ZapContext
from ui import item_provider
from ui.autoplay import FINISHED_TEXT
from ui.context_actions import is_channel
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, start_kwargs
from ui.overlays.pip_label import CONNECTING, LIVE
from ui.overlays.transport import fmt_behind

logger = logging.getLogger(__name__)

PIP_LIVE_ONLY = "Picture-in-picture is for live TV"
PIP_EXTERNAL = "Picture-in-picture is off while channels play in an external player"
NO_PREVIOUS = "No previous channel yet"
OPEN_PIP_FIRST = "Open picture-in-picture first (P)"
ALREADY_LARGE = "This channel is already playing"
SWITCHING = "Switching…"  # a swap asked for while the large picture's channel opens
SWITCHING_MS = 1500
SHIFTED_MARGIN_S = 1.0  # the label says "−MM:SS" once the lag is this far beyond the target
ERROR_MS = 6000


@dataclass
class SlotView:
    """What a slot plays, for the shell: its target and the zap group it was tuned from."""

    target: Optional[Target] = None
    context: Optional[ZapContext] = None
    url: str = ""  # its link: the watch history records it when it becomes large
    cid: Optional[str] = None  # a movie's or an episode's resume key (it stays with the slot)


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
        budget,
        live_start: Callable[[Target, str], LiveStart],
        nav,
        drawer,
        zap,
        all_live: Callable[[], list[dict]],
        large_channel: Callable[[], Optional[dict]],
        vod: Callable[[], bool],
        external: Callable[[], bool],
        closed: Callable[[], bool],
        toast: Callable[..., None],
        swap: Callable[[], bool],
        toggle_mute: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`frame` is the PipFrame and `label` the PipLabel (None in tests without them).

        `budget` is the ConnectionBudget; `live_start(target, url)` a main tune's LiveStart;
        `nav`, `drawer` and `zap` the shell's (P on the drawer's channel, the previous
        channel); `all_live()` every live channel; `large_channel()` what the large picture
        plays; `vod()` whether a movie or an episode is current; `external()` whether channels
        play in an external player (it holds the provider's stream); `swap()` the shell's swap
        (False when it has to wait); `toggle_mute()` mutes or unmutes the small picture.
        """
        super().__init__(parent)
        self.sessions = sessions
        self._frame = frame
        self._label = label
        self._transport = transport
        self.catalog = catalog
        self.config = config
        self._lanes = lanes
        self._budget = budget
        self._live_start = live_start
        self._nav = nav
        self._drawer = drawer
        self._zap = zap
        self._all_live = all_live
        self._large_channel = large_channel
        self._vod = vod
        self._external = external
        self._closed = closed
        self._toast = toast
        self._swap = swap
        self._toggle_mute = toggle_mute
        self._views: dict[str, SlotView] = {}
        self._wanted: Optional[tuple[Target, ZapContext]] = None  # its link is on its way
        self._link: Optional[Target] = None  # the one link request in flight
        self._timelines: dict[str, dict] = {}  # each slot's last timeline since it opened
        self._closed_view: Optional[SlotView] = None  # what the small picture showed last
        self._ended: set[str] = set()  # the slots whose player's title ended

    def wire(self) -> None:
        sessions = self.sessions
        sessions.pipStateChanged.connect(self._on_pip_state)
        sessions.denied.connect(self._on_denied)
        sessions.pipFailed.connect(self._on_failed)
        sessions.slotTimeline.connect(self._on_timeline)
        for slot in (sessions.large(), sessions.other()):
            slot.live.modeChanged.connect(self._refresh_label)
            slot.playback.stateChanged.connect(partial(self._on_player_state, slot.name))
        self._lanes.sideFailed.connect(self._on_link_failed)
        self._transport.pipRequested.connect(self.toggle_previous)
        self._transport.set_pip_state(True, False)
        if self._frame is not None:
            self._frame.swapRequested.connect(self.user_swap)
        label = self._label
        if label is not None:
            label.closeRequested.connect(self.close)
            label.muteRequested.connect(self._toggle_mute)
            label.swapRequested.connect(self.user_swap)

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

    def take_large(
        self,
        old: str,
        new: str,
        target: Optional[Target],
        context,
        url: str,
        cid: Optional[str] = None,
    ) -> SlotView:
        """A swap: keep the old large slot's target, zap group, link and resume key; the new
        large one's."""
        self._views[old] = SlotView(target, context, url, cid)
        return self.view(new)

    def small_title(self, name: str) -> Optional[Target]:
        """The movie or the episode slot `name` plays small (None: it is large, or plays a
        channel)."""
        small = self.sessions.small()
        target = self.view(name).target if small is not None and small.name == name else None
        return target if target is not None and not target.live else None

    def small_resume_key(self, name: str) -> Optional[str]:
        """The resume key of the movie or episode slot `name` plays small (None: it is large,
        or plays a channel)."""
        small = self.sessions.small()
        if small is None or small.name != name:
            return None
        return self.view(name).cid

    # ---- keys, clicks and the menu ------------------------------------------------------------

    def pip_key(self) -> None:
        """P: the drawer's highlighted channel small (Ctrl/⌘+P there), else the previous
        channel, or close the small picture."""
        drawer = self._drawer
        node = drawer.highlighted() if drawer.isVisible() else None
        if node is not None and is_channel(self._nav, node):
            self.open_node(node)
            drawer.close_drawer()
        else:
            self.toggle_previous()

    def swap_key(self) -> None:
        """S: swap, or say how to open picture in picture."""
        if not self.sessions.pip_open():
            self._toast(OPEN_PIP_FIRST)
        else:
            self.user_swap()

    def user_swap(self) -> bool:
        """S, a click on the PiP or its ⇄: a swap that has to wait says so."""
        swapped = self._swap()
        if not swapped and self.sessions.pip_open():
            self._toast(SWITCHING, "info", SWITCHING_MS)
        return swapped

    def open_node(self, node) -> None:
        """A drawer row's channel small, with its group for ↑/↓ once it is swapped in."""
        nav = self._nav
        group = next((n for n in reversed(nav.path) if n.layer == "group"), None)
        nodes = nav.children(group) if group is not None else []
        channels = payloads(nodes) or [node.payload]
        key = group.key if group is not None else ALL_GROUP_ID
        source = item_provider.pick_source(self.catalog, node.payload, key)
        self.open_channel(
            node.payload, ZapContext(key, channels, fallback=self._all_live(), source=source)
        )

    def _previous_channel(self) -> tuple[Optional[dict], Optional[ZapContext]]:
        """The channel tuned before the large picture's, with the zap group to walk."""
        zap = self._zap
        channel = zap.previous_channel()
        if channel is None:
            return None, None
        context = zap.context
        if context.index_of(channel) < 0:
            channels = self._all_live()
            source = item_provider.pick_source(self.catalog, channel, ALL_GROUP_ID)
            context = ZapContext(ALL_GROUP_ID, channels, fallback=channels, source=source)
        return channel, ZapContext(
            context.group_key, context.channels, fallback=context.fallback, source=context.source
        )

    # ---- opening and closing ------------------------------------------------------------------

    def toggle_previous(self) -> None:
        """P: close the small picture, else open the previous channel small."""
        if self._closed():
            return
        if self.busy():
            self.close()
            return
        if self._refused():
            return
        previous, context = self._previous_channel()
        if previous is None:
            self._toast(NO_PREVIOUS)
            return
        self.open_channel(previous, context)

    def open_channel(self, channel: dict, context: Optional[ZapContext]) -> None:
        """Open `channel` small, or show it in the small picture instead of what it shows."""
        if self._closed():
            return
        if self._refused():
            return
        if ZapContext.same(channel, self._large_channel()):
            self._toast(ALREADY_LARGE)
            return
        if self.small_showing(channel) is not None:
            return  # already small
        if self._limit_reached(channel):
            return
        title = str(channel.get("name") or "")
        group = context.group_key if context is not None else ""
        source = (context.source if context is not None else "") or item_provider.pick_source(
            self.catalog, channel, group
        )
        target = Target(channel, "live", title, group_key=group, source=source)
        self._wanted = (target, context if context is not None else ZapContext())
        self._request(target)

    def _refused(self) -> bool:
        """A movie plays, or channels play in an external player: no PiP (toasted)."""
        if self._vod():
            self._toast(PIP_LIVE_ONLY)
            return True
        if self._external():
            self._toast(PIP_EXTERNAL)
            return True
        return False

    def provider_settled(self, name: str, owner: Callable[[dict], str], *, ask_again: bool) -> bool:
        """Provider `name`'s setup dropped its link requests: none comes. The channel waiting
        to open asks anew when it is `name`'s and `ask_again` (a setup that finished), else it
        is forgotten when it is `name`'s (a failure: True, the shell says why); another
        provider's request stays."""
        link = self._link
        wanted = self._wanted
        if wanted is None:
            if link is not None and owner(link.item) == name:
                self._link = None
            return False
        if link is not None:
            if owner(link.item) != name:
                return False  # another provider's request is still coming
            self._link = None
        elif owner(wanted[0].item) != name:
            return False
        if owner(wanted[0].item) == name and not ask_again:
            self._wanted = None
            return True
        if not self._closed():
            self._request(wanted[0])
        return False

    def keep_large_small(self, item: dict, title: str) -> bool:
        """A movie or an episode from the Library takes the large picture: the channel that
        was large moves to the small one without reopening, and a previous small picture
        closes first (its stream is released before the title opens, as any open waits).

        False when the channel is not kept and the caller plays as before: no channel plays
        large or the channels play in an external player (the small picture closes: it is for
        live TV), or the title's provider has no second stream, in which case the channel
        closes for it (toasted) and a small picture, another provider's, plays on.
        """
        channel = self._large_channel()
        if channel is None or self._external():
            self.close()
            return False
        if self._no_second_stream(channel, item, title):
            return False
        self.close()
        return self.sessions.demote_large()

    def keep_beside(self, item: dict, title: str) -> None:
        """A movie or an episode takes the place of the one playing large (Next, autoplay,
        another from the Library): the small picture plays on beside it, unless its channel is
        of the title's provider, which allows fewer than two streams; then it closes for the
        title (said)."""
        small = self.sessions.small()
        channel = small.channel if small is not None else None
        if channel is None or self._no_second_stream(channel, item, title):
            self.close()

    def _no_second_stream(self, channel: dict, item: dict, title: str) -> bool:
        """`channel` is of the title's provider, which allows fewer than two streams: the
        channel closes for the title (said here)."""
        provider = item_provider.provider_dict(self.catalog, item)
        name = str(provider.get("name", ""))
        limit = self._budget.limit_for(provider, item_provider.max_connections(self.catalog, item))
        if (
            limit is None
            or limit >= 2
            or item_provider.provider_name(self.catalog, channel) != name
        ):
            return False
        plural = "s" if limit != 1 else ""
        self._toast(
            f"Closed {channel.get('name') or ''} to play {title}: "
            f"{name} allows {limit} stream{plural}"
        )
        return True

    def _limit_reached(self, channel: dict) -> bool:
        """The limit of the channel's own provider leaves no stream for a small picture: say
        so before asking for a link (an STB portal mints one per request, and allows one)."""
        provider = item_provider.provider_dict(self.catalog, channel)
        limit = self._budget.limit_for(
            provider, item_provider.max_connections(self.catalog, channel)
        )
        if limit is None:
            return False
        name = str(provider.get("name", ""))
        others = [p for p in self._budget.holders(name) if p != PURPOSE]  # its own go back
        if limit > 0 and len(others) < limit:
            return False
        self._toast(limit_message(limit), "error", ERROR_MS)
        return True

    def close(self) -> None:
        """Close the small picture, or forget the one waiting to open."""
        self._wanted = None
        self._link = None  # a late link for it is ignored (it is no longer `_link`)
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
        self._views[slot.name] = SlotView(target, context, url)
        self._closed_view = None
        self._timelines.pop(slot.name, None)
        # The small picture keeps its time-shift in memory: one disk ring at most (M5 ruling).
        kwargs = start_kwargs(self._live_start(target, url), self.config, disk=False)
        sessions.open_pip(
            target.item,
            provider=item_provider.provider_dict(self.catalog, target.item),
            xtream_max=item_provider.max_connections(self.catalog, target.item),
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
            self._closed_view = self._views.pop(closed.name, None)  # a failure may retry it
            self._timelines.pop(closed.name, None)
            if frame is not None:
                frame.set_large(sessions.large().name)  # the picture that stayed (as it was)
                frame.hide_pip()
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
        """The small picture closed for a failure: an STB channel's link is made again once
        (its play token, or its portal's session, may have expired), else it says why."""
        if self._closed():
            return
        view = self._views.get(self.sessions.other().name) or self._closed_view
        self._closed_view = None
        if view is not None and self._may_retry(view.target):
            self._retry(view)
            return
        self._toast(message, "error", ERROR_MS)

    def _may_retry(self, target: Optional[Target]) -> bool:
        """As the large picture (LiveControls.may_retry): once per channel opened small, never
        for the provider's limit, nor when another channel was chosen small since."""
        failure = self.sessions.failure  # "": its start raised, or the budget refused
        return (
            target is not None
            and target.live
            and not target.retried
            and bool(failure)
            and failure != STREAM_LIMIT
            and self._wanted is None
            and self._link is None
            and item_provider.provider_type(self.catalog, target.item) == "STB"
        )

    def _retry(self, view: SlotView) -> None:
        target = view.target
        assert target is not None
        logger.info("Making the link of %s again after the small picture failed", target.title)
        item_provider.sign_in_again(self.catalog, target.item)
        again = replace(target, retried=True)
        self._wanted = (again, view.context if view.context is not None else ZapContext())
        self._request(again)

    def _on_player_state(self, name: str, state: str) -> None:
        """A player's state: a title that ended small says "Finished" on the label."""
        ended = state == "ended"
        if ended == (name in self._ended):
            return
        if ended:
            self._ended.add(name)
        else:
            self._ended.discard(name)
        small = self.sessions.small()
        if small is not None and small.name == name:
            self._refresh_label()

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
        label.set_info(name, self._state_text(small, live=target is None or target.live))
        label.set_audible(not self.sessions.muted(small.name))

    def _state_text(self, slot: Slot, *, live: bool = True) -> str:
        if not live:  # a movie or an episode swapped small plays on: its title alone
            return FINISHED_TEXT if slot.name in self._ended else ""
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
