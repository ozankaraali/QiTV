"""Picture in picture in the TV shell (spec §7): a second channel, small.

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
keeps the small picture beside it (`keep_beside`). When the title's provider has no second
stream for a channel, that channel closes for it only once the title's link comes
(`make_room`: "Closed … to play …"); if the link fails, nothing closes.

The PiP frame and label follow the manager: the second surface shows at its corner while
PiP is open, and the label names the small picture's channel and state ("Connecting…" until
it plays; "−MM:SS" once time-shifted beyond its target, else "LIVE"); a movie or an episode
swapped small shows its title alone, and "Finished" once it ended there (swapped large
again, the Next card offers what follows, as at its end). The label's × closes the small
picture, its speaker mutes or unmutes it, and its ⇄, like a click on the small picture,
swaps (the shell's `swap`).

A channel the small picture plays directly (its live session fell back) whose stream ends
opens again in the same player, so in its budget slot, as the large picture's does
(`ui.live_controls`): after RECONNECT_MS, twice per channel (twice more once it played for
HEALTHY_S), then "The stream ended". A swap during the back-off hands the reopen to the
picture that has the player then (`take_reopen`, `adopt_reopen`, and LiveControls' own).

What each slot shows (its target, the zap group it came from and, for a movie or an episode,
its resume key) is kept here, so a swap can give the shell the new large picture's
(`take_large`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.catalog import ALL_GROUP_ID
from services.i18n import N_, tr, tr_n, translate
from services.live.live_session import LOADING, RECONNECTING, UNAVAILABLE
from services.live.refusals import STREAM_LIMIT
from services.live.stay_live import TARGETS
from services.navigation import payloads
from services.sessions import SessionManager, Slot, limit_message, records
from services.vlc_profile import PlayKind
from services.zapping import ZapContext
from ui import item_provider, live_controls
from ui.autoplay import FINISHED_TEXT
from ui.context_actions import is_channel
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, start_kwargs
from ui.overlays.pip_label import CONNECTING, LIVE, REC
from ui.overlays.transport import fmt_behind

logger = logging.getLogger(__name__)

# The toasts (kept in English; shown translated).
PIP_LIVE_ONLY = N_("Picture-in-picture is for live TV")  # i18n: a toast
PIP_EXTERNAL = N_(
    "Picture-in-picture is off while channels play in an external player"
)  # i18n: a toast
# i18n: a toast: there is no channel watched before this one to open small
NO_PREVIOUS = N_("No previous channel yet")
OPEN_PIP_FIRST = N_("Open picture-in-picture first (P)")  # i18n: a toast; P is the key
ALREADY_LARGE = N_("This channel is already playing")  # i18n: a toast
# A swap asked for while the large picture's channel opens.
SWITCHING = N_("Switching…")  # i18n: a toast: the two pictures are about to swap
SWITCHING_MS = 1500
SHIFTED_MARGIN_S = 1.0  # the label says "−MM:SS" once the lag is this far beyond the target
ERROR_MS = 6000
NOTICE_MS = 3000  # a small picture's stream notice, after its channel's name
# Not said for the small picture: its label says "Connecting…" while it loads, and the large
# picture keeps "no time-shift" for a rewind too.
_QUIET_NOTICES = (LOADING, UNAVAILABLE)


@dataclass
class _Room:
    """What a title on its way closes once its link comes, and the toast that says so."""

    note: str  # "Closed {channel} to play {title}: {provider} allows {limit} stream(s)"
    close_small: bool  # the small picture closes then (else the large channel, as it opens)


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
        sound: Optional[Callable[[Any, dict], None]] = None,
        explain: Callable[[dict, str], None] = lambda item, title: None,
        needs_stream: Callable[[dict, str, Callable[[], None]], bool] = lambda *_: False,
        parent: Optional[QObject] = None,
    ) -> None:
        """`frame` is the PipFrame and `label` the PipLabel (None in tests without them).

        `budget` is the ConnectionBudget; `live_start(target, url)` a main tune's LiveStart;
        `nav`, `drawer` and `zap` the shell's (P on the drawer's channel, the previous
        channel); `all_live()` every live channel; `large_channel()` what the large picture
        plays; `vod()` whether a movie or an episode is current; `external()` whether channels
        play in an external player (it holds the provider's stream); `swap()` the shell's swap
        (False when it has to wait); `toggle_mute()` mutes or unmutes the small picture;
        `sound(player, channel)` gives a channel opening small its sound sync (2.2).
        `needs_stream(channel, purpose, then)` asks before a recording is stopped for the
        stream a channel opening over `purpose`'s needs (2.6): True, asked (`then` follows).
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
        self._sound = sound
        self._explain = explain  # why a failure happened, at the item's provider
        self._needs_stream = needs_stream
        self._views: dict[str, SlotView] = {}
        self._wanted: Optional[tuple[Target, ZapContext]] = None  # its link is on its way
        self._link: Optional[Target] = None  # the one link request in flight
        self._timelines: dict[str, dict] = {}  # each slot's last timeline since it opened
        self._closed_view: Optional[SlotView] = None  # what the small picture showed last
        self._ended: set[str] = set()  # the slots whose player's title ended
        self._room: Optional[_Room] = None  # what a title on its way closes when it comes
        # Per slot: the channel it showed small and the notices said for it (once each).
        self._said: dict[str, tuple[Optional[dict], set[str]]] = {}
        # An ended direct stream of the small picture: reopens per slot since its channel
        # opened, each player's last "playing" (monotonic), and the reopen after its back-off.
        self._reopens: dict[str, int] = {}
        self._playing_since: dict[str, float] = {}
        self._reopen_due: Optional[tuple[str, Target, str, PlayKind]] = None
        self._reopen_timer = QTimer(self, singleShot=True)
        self._reopen_timer.timeout.connect(self._reopen)

    def wire(self) -> None:
        sessions = self.sessions
        sessions.pipStateChanged.connect(self._on_pip_state)
        sessions.denied.connect(self._on_denied)
        sessions.pipFailed.connect(self._on_failed)
        sessions.slotTimeline.connect(self._on_timeline)
        sessions.setAside.connect(self._on_set_aside)
        sessions.shownSmall.connect(lambda _name: self.follow_roles())
        for slot in sessions.slots():
            self.watch_slot(slot)
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

    def watch_slot(self, slot: Slot) -> None:
        """Follow a slot's player and live session (each slot's at wiring, a tile's when the
        multiview adds it): the label shows the state of whichever is small."""
        slot.live.modeChanged.connect(self._refresh_label)
        slot.live.recordingChanged.connect(self._refresh_label)  # REC (2.2)
        slot.live.notice.connect(partial(self._on_notice, slot.name))
        slot.playback.stateChanged.connect(partial(self._on_player_state, slot.name))

    # ---- state --------------------------------------------------------------------------------

    def busy(self) -> bool:
        """PiP is open, opening, or its link is on its way."""
        sessions = self.sessions
        return self._wanted is not None or sessions.pip_open() or sessions.pip_opening()

    def on_its_way(self) -> bool:
        """A channel is on its way to the small picture: its link, its opening or its change."""
        sessions = self.sessions
        return self._wanted is not None or sessions.pip_opening() or sessions.changing()

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

    def forget_view(self, name: str) -> None:
        """Slot `name` (a multiview tile) closed: what it showed is forgotten."""
        self._views.pop(name, None)
        self._timelines.pop(name, None)

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
            self._toast(translate(OPEN_PIP_FIRST))
        else:
            self.user_swap()

    def user_swap(self) -> bool:
        """S, a click on the PiP or its ⇄: a swap that has to wait says so."""
        swapped = self._swap()
        if not swapped and self.sessions.pip_open():
            self._toast(translate(SWITCHING), "info", SWITCHING_MS)
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
            self._toast(translate(NO_PREVIOUS))
            return
        self.open_channel(previous, context)

    def open_channel(self, channel: dict, context: Optional[ZapContext]) -> None:
        """Open `channel` small, or show it in the small picture instead of what it shows."""
        if self._closed():
            return
        if self._refused():
            return
        if ZapContext.same(channel, self._large_channel()):
            self._toast(translate(ALREADY_LARGE))
            return
        if self.small_showing(channel) is not None:
            return  # already small
        background = self.sessions.background_showing(channel)
        if background is not None:
            self._show_background(background.name, context)
            return
        purpose = str(getattr(self.sessions.other().tuner, "purpose", ""))
        if self._needs_stream(channel, purpose, lambda: self.open_channel(channel, context)):
            return  # no stream is free while a recording holds one: asked
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
            self._toast(translate(PIP_LIVE_ONLY))
            return True
        if self._external():
            self._toast(translate(PIP_EXTERNAL))
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
        closes for it as the title opens (said then: `make_room`) and a small picture,
        another provider's, plays on.
        """
        self._room = None
        channel = self._large_channel()
        if channel is None or self._external():
            self.close()
            return False
        note = self._no_second_stream(channel, item, title)
        if note:
            self._room = _Room(note, close_small=False)
            return False
        self.close()
        return self.sessions.demote_large()

    def keep_beside(self, item: dict, title: str) -> None:
        """A movie or an episode takes the place of the one playing large (Next, autoplay,
        another from the Library): the small picture plays on beside it, unless its channel is
        of the title's provider, which allows fewer than two streams; then it closes for the
        title once the title's link comes (`make_room`)."""
        self._room = None
        small = self.sessions.small()
        channel = small.channel if small is not None else None
        if channel is None:
            self.close()
            return
        note = self._no_second_stream(channel, item, title)
        if note:
            self._room = _Room(note, close_small=True)

    def has_room(self) -> bool:
        """The title chosen last closes a channel once its link comes."""
        return self._room is not None

    def make_room(self) -> None:
        """The title's link came: the channel it has no stream for closes (the small picture
        now; the large one as the title opens), and the toast says so."""
        room, self._room = self._room, None
        if room is None:
            return
        if room.close_small:
            self.close()
        self._toast(room.note)

    def forget_room(self) -> None:
        """The title never came (or another was chosen): nothing closes for it."""
        self._room = None

    def _no_second_stream(self, channel: dict, item: dict, title: str) -> str:
        """When `channel` is of the title's provider, which allows fewer than two streams,
        the toast that says it closes for the title; else ""."""
        provider = item_provider.provider_dict(self.catalog, item)
        name = str(provider.get("name", ""))
        limit = self._budget.limit_for(provider, item_provider.max_connections(self.catalog, item))
        if (
            limit is None
            or limit >= 2
            or item_provider.provider_name(self.catalog, channel) != name
        ):
            return ""
        return tr_n(
            "Closed {channel} to play {title}: {provider} allows {n} stream",
            "Closed {channel} to play {title}: {provider} allows {n} streams",
            limit,
            channel=channel.get("name") or "",
            title=title,
            provider=name,
        )  # i18n: a toast; {channel} closed for the film or episode {title}

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
        own = self.sessions.other().name  # the slot it would open in: its own go back
        others = [p for p in self._budget.holders(name) if p != own]
        if limit > 0 and len(others) < limit:
            return False
        self._toast(limit_message(limit), "error", ERROR_MS)
        return True

    def close(self, *, keep_recording: bool = True) -> None:
        """Close the small picture, or forget the one waiting to open. 2.6: a small picture
        that records records on in the background, unless not `keep_recording` (the quit)."""
        if self._room is not None and self._room.close_small:
            self._room = None  # closed already: nothing to close, or say, for the title
        self._wanted = None
        self._link = None  # a late link for it is ignored (it is no longer `_link`)
        self._cancel_reopen()
        sessions = self.sessions
        if keep_recording:
            self._set_small_aside()
        if sessions.pip_open() or sessions.pip_opening():
            sessions.close_pip()

    # ---- 2.6: recordings in the background ----------------------------------------------------

    def follow_roles(self) -> None:
        """A player went to the background or came back: the frame shows the large and the
        small player (a background one's surface hides) and the label follows."""
        if self._frame is not None:
            sessions = self.sessions
            self._frame.set_roles(sessions.large().name, sessions.other().name)
        self._refresh_label()
        self.changed.emit()

    def _set_small_aside(self) -> None:
        """The open small picture records: it records on in the background (another player
        is the small picture's)."""
        small = self.sessions.small()
        if small is not None and records(small):
            self.sessions.set_aside(small.name)

    def _show_background(self, name: str, context: Optional[ZapContext]) -> None:
        """The channel background player `name` records shows small: no stream opens."""
        self._wanted = None
        self._link = None
        self._cancel_reopen()
        self._set_small_aside()
        if context is not None:
            self.view(name).context = context
        if self.sessions.show_small(name):
            self.sessions.slot(name).live.bring_back()

    def _on_set_aside(self, name: str, fresh: str) -> None:
        """Slot `name` records on in the background: when `fresh` took the small role, the
        frame and the label follow (the large role is the shell's to follow)."""
        if fresh != self.sessions.other().name:
            return
        self.forget_view(fresh)  # it shows nothing yet; `name`'s view stays for its return
        self.sessions.slot(name).live.set_aside()
        self.follow_roles()

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
        # A movie started, or the large picture took the channel, meanwhile: nothing opens.
        # A retried channel was small beside a movie (nothing else opens small then): again.
        if (self._vod() and not target.retried) or ZapContext.same(
            target.item, self._large_channel()
        ):
            return
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
        self._set_small_aside()  # 2.6: a small picture that records records on
        slot = sessions.other()
        self._views[slot.name] = SlotView(target, context, url)
        self._closed_view = None
        self._timelines.pop(slot.name, None)
        self._said.pop(slot.name, None)  # its new channel's notices are said again
        self._reopens.pop(slot.name, None)
        self._cancel_reopen()
        # The small picture keeps its time-shift in memory: one disk ring at most.
        kwargs = start_kwargs(self._live_start(target, url), self.config, disk=False)
        sessions.open_pip(
            target.item,
            provider=item_provider.provider_dict(self.catalog, target.item),
            xtream_max=item_provider.max_connections(self.catalog, target.item),
            start_kwargs=kwargs,
        )
        if self._sound is not None:  # its old stream stopped: the new one opens with it
            self._sound(slot.playback, target.item)
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
        self._toast(translate(message), "error", ERROR_MS)  # one kept in English shows translated
        target = view.target if view is not None else None
        if target is not None:
            self._explain(target.item, target.title)  # why, at its provider

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
        """A player's state: a title that ended small says "Finished" on the label; a channel
        played directly that ended small opens again."""
        if state == "playing":
            self._playing_since[name] = time.monotonic()
        elif state == "ended":
            self._reconnect_small(name)
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

    def _on_notice(self, name: str, text: str) -> None:
        """The small picture's stream notice ("Reconnecting…", "Plays without rewind…"):
        toasted after its channel's name, each text once per channel shown small. The large
        picture's are LiveControls'."""
        small = self.sessions.small()
        if self._closed() or small is None or small.name != name or text in _QUIET_NOTICES:
            return
        said = self._said.get(name)
        if said is None or said[0] is not small.channel:
            said = self._said[name] = (small.channel, set())
        if text in said[1]:
            return
        said[1].add(text)
        self._say(name, translate(text))  # LiveSession's notices are English (N_) texts

    def _say(self, name: str, message: str) -> None:
        """Toast `message` (shown text) after the name of slot `name`'s channel."""
        small = self.sessions.small()
        target = self.view(name).target
        channel = small.channel if small is not None else None
        title = target.title if target is not None else str((channel or {}).get("name", ""))
        shown = tr("{name}: {message}", name=title, message=message) if title else message
        self._toast(shown, "info", NOTICE_MS)

    def _reconnect_small(self, name: str) -> None:
        """Slot `name`'s player ended: when it is the small picture's channel, played directly,
        its stream opens again in the same player after the back-off (its budget slot is
        kept: its live session still holds it); past the reopens, it says the stream ended."""
        small = self.sessions.small()
        if self._closed() or small is None or small.name != name:
            return
        if self._reopen_due is not None:
            return  # already on its way
        target = self.view(name).target
        playback = small.playback
        url, kind = getattr(playback, "url", None), getattr(playback, "kind", None)
        if target is None or not target.live or not url:
            return
        if kind is None or kind in live_controls.NOT_REOPENED:
            return  # the time-shift buffer reopens itself
        count = self._reopens.get(name, 0)
        since = self._playing_since.pop(name, None)
        if since is not None and time.monotonic() - since >= live_controls.HEALTHY_S:
            count = 0  # it played well since it last opened: the back-off anew
        if count >= len(live_controls.RECONNECT_MS):
            self._say(name, tr("The stream ended"))
            return
        self._reopens[name] = count + 1
        self._reopen_due = (name, target, url, kind)
        self._reopen_timer.start(live_controls.RECONNECT_MS[count])
        self._say(name, translate(RECONNECTING))

    def _reopen(self) -> None:
        due, self._reopen_due = self._reopen_due, None
        if due is None or self._closed():
            return
        name, target, url, kind = due
        small = self.sessions.small()
        if small is None or small.name != name or self.view(name).target is not target:
            return  # closed or another channel small meanwhile (a swap hands it over)
        playback = small.playback
        if getattr(playback, "url", None) != url:
            return
        logger.info("Opening %s small again after its stream ended", target.title)
        playback.play(
            url,
            kind=kind,
            latency=self.config.live_latency,
            **live_controls.reopen_options(playback, target.item),
        )

    def _cancel_reopen(self) -> None:
        self._reopen_due = None
        self._reopen_timer.stop()

    def take_reopen(self, name: str) -> Optional[live_controls.Reopen]:
        """A swap made slot `name` large: its ended stream on its way to open again goes with
        it (to LiveControls; None: none waits). It no longer opens here."""
        due = self._reopen_due
        if due is None or due[0] != name:
            return None
        remaining = max(0, self._reopen_timer.remainingTime())
        self._cancel_reopen()
        _name, target, url, kind = due
        return live_controls.Reopen(target, url, kind, remaining, self._reopens.get(name, 0))

    def adopt_reopen(self, name: str, reopen: live_controls.Reopen) -> None:
        """A swap made slot `name` small while its ended stream was on its way to open again
        (large): it opens here once the rest of its back-off is over."""
        self._reopens[name] = reopen.count
        self._reopen_due = (name, reopen.target, reopen.url, reopen.kind)
        self._reopen_timer.start(reopen.remaining_ms)

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
        label.set_level(self.sessions.level(small.name))

    def _state_text(self, slot: Slot, *, live: bool = True) -> str:
        if not live:  # a movie or an episode swapped small plays on: its title alone
            return FINISHED_TEXT if slot.name in self._ended else ""
        if slot.live.recording():
            return REC  # its recording goes on small (2.2)
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
