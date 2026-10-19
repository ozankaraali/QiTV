"""The multiview in the TV shell: several live channels at once, on a grid (stage 1 of
docs/superpowers/specs/2026-10-19-multiview-design.md).

V, View → Multiview (Ctrl/⌘+4) or the grid button on the player controls shows it over live
TV: the channel playing becomes tile 1, an open picture-in-picture tile 2 (nothing opens
again), and the other tiles show "+". The layout chip on the controls switches 2, 4, 6 and 9
tiles; 6 and 9 only with hardware decoding (Settings), else 2 and 4.

One tile has the focus: it is the session manager's large slot, so the player controls, the
banner, zapping, the channel list, rewind and Go live act on it as on the big picture, and
its sound plays (the others are muted, unless the mixer made one heard). A press on a tile
focuses it; a click on the focused one opens the channel list, as on the big picture. A "+"
tile focuses and opens the channel list: the channel picked plays there. Tiles play through
the time-shift pipeline as a picture-in-picture does, in memory (a channel that cannot be
time-shifted plays directly): the focused tile's buffer is the memory per stream, the
others' a small one (`ring_sizes`), and the focus moves them (`SessionManager.set_large`).

Before the focused tile asks for a link, its provider's limit is checked (an STB portal mints
one per request): when the provider's other streams take all of it, the tile shows "No
stream to spare at <provider>" with the channel, and asks for nothing; so does a tile whose
stream the budget then refuses (`SessionManager.slotDenied`). A tile whose stream fails says
so too. A click on the card tries again; nothing queues or asks again by itself, but for a
tile without the focus whose STB channel failed: its link is made again once (the portal's
play token may have expired), as the big picture's and the PiP's are. Zapping onto a channel
another tile shows moves the focus there.

A tile without the focus plays at 1x: muted, it needs no nudge, so its stay-live only jumps
it to live when it fell far behind (`SessionManager.set_large`). Its label says LIVE, or
"−MM:SS" while it plays that far behind live.

Esc (with nothing else open), V or a double-click leaves: the focused tile is the big
picture, the tile focused before it stays as the picture-in-picture if it plays, and the
other tiles stop. Leaving keeps the grid (config `multiview`, versioned; older builds ignore
it): the next grid offers a "Last grid" chip (and View → Restore Last Multiview) that brings
its channels back into their tiles, each once its provider is ready, and its focus. A
channel dragged from the channel list onto a tile plays there (`drag_over`, `tile_at`). The
Library, films, files, the mini player and external players leave the grid first (the shell
calls `leave`). A smaller layout, and leaving, stop every player but the big picture and the
PiP, and nothing still on its way for a tile that went opens there.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint, Signal

from services.config_manager import ConfigManager
from services.live.refusals import REFUSALS
from services.live.stay_live import TARGETS
from services.provider_ids import with_provider
from services.redact import safe_message
from services.sessions import SessionManager, Slot
from services.zapping import ZapContext
from ui import item_provider
from ui.grid_frame import GridFrame
from ui.overlays.grid_tiles import DENIED, EMPTY, FAILED, OPENING, PLAYING, TileView
from ui.overlays.pip_label import CONNECTING, LIVE
from ui.overlays.transport import fmt_behind

logger = logging.getLogger(__name__)

CELLS = (2, 4, 6, 9)
SAVED_VERSION = 1  # config `multiview`'s format
SOFTWARE_CELLS = (2, 4)  # without hardware decoding (Settings)
DEFAULT_CELLS = 4
LAYOUT_NAMES = {2: "1×2", 4: "2×2", 6: "3×2", 9: "3×3"}
# The tiles' slots, in the order a grid takes free ones ("main" and "pip" are the players
# picture in picture already has; a tile's player is made when it first takes the focus or a
# channel, one at a time: a "+" tile has none).
TILE_NAMES = ("main", "pip", "t2", "t3", "t4", "t5", "t6", "t7", "t8")
NO_STREAM = "No stream to spare at {provider}"
TILE_FAILED = "Couldn't play this channel"
LIVE_ONLY = "Multiview is for live TV"
EXTERNAL_ONLY = "Multiview is off while channels play in an external player"
NO_PLAYERS = "Multiview needs more video players than this computer could open"
LEAVE_FIRST = "Leave the multiview first (V)"
TILE_TITLE = "Tile {number}"
SHIFTED_MARGIN_S = 1.0  # a label says "−MM:SS" once the lag is this far beyond the target
MIB = 1024 * 1024
DEFAULT_RAM_MIB = ConfigManager.DEFAULT_OPTION_TIMESHIFT_RAM_MIB  # a config without the key
_SHOWING = (OPENING, PLAYING)
_PLAYING_STATES = ("playing", "paused")


@dataclass
class _Card:
    """What a tile shows instead of a picture: its provider had no stream, or it failed."""

    state: str  # DENIED or FAILED
    channel: dict
    detail: str


class GridControls(QObject):
    changed = Signal()  # shown, left, focus or tiles changed: the mixer and the guides follow

    def __init__(
        self,
        *,
        sessions: SessionManager,
        frame: GridFrame,
        tiles,
        pip_frame,
        host,
        transport,
        catalog,
        budget,
        config,
        make_slot: Callable[[str], Optional[Slot]],
        focus: Callable[[str], bool],
        activate: Callable[[], None],
        open_drawer: Callable[[], None],
        retry: Callable[[dict], None],
        denied: Callable[[str], None],
        close_pip: Callable[[], None],
        vod: Callable[[], bool],
        external: Callable[[], bool],
        closed: Callable[[], bool],
        toast: Callable[..., None],
        reopen: Optional[Callable[[str, dict], None]] = None,
        restore: Optional[Callable[[str, dict, str, str], None]] = None,
        forget: Optional[Callable[[str], None]] = None,
        pending: Optional[Callable[[], Optional[dict]]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`frame` lays the surfaces out and `tiles` (the GridTiles overlay; None in tests)
        draws over them; `pip_frame`, `host` and `transport` are the window's (None-able
        in tests). `make_slot(name)` makes a tile's player (None: it could not); `focus(name)`
        gives a tile the large role (the shell's: False when it could not); `activate()` is a
        click on the big picture (the channel list); `open_drawer()` the channel list;
        `retry(channel)` tunes `channel` again on the focused tile; `denied(name)` says a
        tile's stream was refused (the shell forgets what it opened there); `close_pip()`
        drops a picture-in-picture still on its way; `vod()` and `external()` say a film
        plays or channels play in an external player; `reopen(name, channel)` makes a tile's
        link again (an STB portal's, once: `_may_retry`) and opens it in that tile;
        `restore(name, channel, provider, group)` opens a saved tile's channel there once its
        provider is ready; `forget(name)` drops what the shell asked for tile `name` (its link
        on its way, its wait for a provider): the tile left the grid; `pending()` is the
        channel whose link the focused tile waits for (None: none)."""
        super().__init__(parent)
        self.sessions = sessions
        self.frame = frame
        self._tiles_view = tiles
        self._pip_frame = pip_frame
        self._host = host
        self._transport = transport
        self.catalog = catalog
        self._budget = budget
        self.config = config
        self._make_slot = make_slot
        self._focus_slot = focus
        self._activate = activate
        self._open_drawer = open_drawer
        self._retry = retry
        self._denied = denied
        self._close_pip = close_pip
        self._vod = vod
        self._external = external
        self._closed = closed
        self._toast = toast
        self._reopen = reopen
        self._restore = restore
        self._forget = forget
        self._pending = pending
        self._restored = False  # the last grid came back since the grid showed
        self._shown = False
        self._cells = DEFAULT_CELLS
        self._names: list[str] = []  # the tiles, in order
        self._previous: Optional[str] = None  # the tile focused before the focused one
        self._cards: dict[str, _Card] = {}
        self._wanted: dict[str, tuple[dict, str]] = {}  # an open on its way: channel, provider
        self._linking: dict[str, dict] = {}  # a tile whose link is on its way: its channel
        self._retried: dict[str, dict] = {}  # each tile's channel whose link was made again
        self._states: dict[str, str] = {}  # each player's last state
        self._timelines: dict[str, dict] = {}
        self._playing: set[str] = set()  # the tiles whose surface shows (frame)
        self._focused_by_press: Optional[str] = None  # its click only focused it

    def wire(self) -> None:
        sessions = self.sessions
        sessions.slotState.connect(self._on_slot_state)
        sessions.slotDenied.connect(self._on_slot_denied)
        sessions.slotFailed.connect(self._on_slot_failed)
        sessions.largeChanged.connect(self._on_large_changed)
        sessions.slotTimeline.connect(self._on_timeline)
        for slot in sessions.slots():
            self.watch_slot(slot)
        frame = self.frame
        frame.focusRequested.connect(self._on_focus_requested)
        frame.tileClicked.connect(self._on_tile_clicked)
        frame.leaveRequested.connect(self._on_leave_requested)
        frame.tilesChanged.connect(self._paint)
        transport = self._transport
        if transport is not None:
            transport.layoutRequested.connect(self.next_layout)
            restored = getattr(transport, "restoreRequested", None)
            if restored is not None:
                restored.connect(self.restore_last)
            transport.set_grid_state(True, False)
        if self._tiles_view is not None and transport is not None:
            self._tiles_view.avoid(transport)

    def watch_slot(self, slot: Slot) -> None:
        """Follow a player's state and its live session's mode (a tile's when it is made)."""
        slot.playback.stateChanged.connect(partial(self._on_player_state, slot.name))
        slot.live.modeChanged.connect(lambda *_: self._refresh())

    # ---- state --------------------------------------------------------------------------

    def shown(self) -> bool:
        return self._shown

    def names(self) -> list[str]:
        """The tiles' slots, in the grid's order (none while it is hidden)."""
        return list(self._names) if self._shown else []

    def choices(self) -> tuple[int, ...]:
        """The layouts offered: 6 and 9 tiles only with hardware decoding."""
        hardware = bool(getattr(self.config, "hardware_decoding", False))
        return CELLS if hardware else SOFTWARE_CELLS

    def cells(self) -> int:
        return self._cells

    def state(self, name: str) -> str:
        """What tile `name` shows: EMPTY, OPENING, PLAYING, DENIED or FAILED."""
        card = self._cards.get(name)
        if card is not None:
            return card.state
        if name in self._linking:
            return OPENING  # its link is on its way
        slot = self._slot(name)
        if slot is None or slot.channel is None:
            return EMPTY
        return PLAYING if self._states.get(name) in _PLAYING_STATES else OPENING

    def tile_showing(self, channel: dict) -> Optional[str]:
        """Another tile than the focused one that plays (or opens) `channel`, else None."""
        if not self._shown:
            return None
        large = self.sessions.large().name
        for name in self._names:
            slot = self._slot(name)
            if (
                name != large
                and slot is not None
                and self.state(name) in _SHOWING
                and ZapContext.same(slot.channel, channel)
            ):
                return name
        return None

    def mixer_rows(self) -> Optional[list[tuple[str, str, str]]]:
        """The mixer's rows while the grid shows: (slot name, "Tile N", channel), the focused
        tile first, then the others that play, in the grid's order; None while hidden."""
        if not self._shown:
            return None
        large = self.sessions.large().name
        rows: list[tuple[str, str, str]] = []
        for number, name in enumerate(self._names, 1):
            if name != large and self.state(name) not in _SHOWING:
                continue
            row = (name, TILE_TITLE.format(number=number), self._title(name))
            if name == large:
                rows.insert(0, row)
            else:
                rows.append(row)
        return rows

    def channels(self) -> list[dict]:
        """The channels the tiles show (their guides are in use)."""
        found = []
        for name in self.names():
            slot = self._slot(name)
            if slot is not None and slot.channel is not None:
                found.append(slot.channel)
        return found

    # ---- showing and leaving ------------------------------------------------------------

    def toggle(self) -> None:
        """V, View → Multiview, the grid button: the grid, or back to one picture."""
        if self._shown:
            self.leave()
        else:
            self.show()

    def show(self, cells: Optional[int] = None) -> bool:
        """Show the grid over live TV (False, said why, when it cannot)."""
        if self._closed() or self._shown:
            return self._shown
        if self._vod():
            self._toast(LIVE_ONLY)
            return False
        if self._external():
            self._toast(EXTERNAL_ONLY)
            return False
        sessions = self.sessions
        self._close_pip()  # a picture-in-picture on its way never opens
        cells = cells if cells is not None else self._cells
        if cells not in self.choices():
            cells = DEFAULT_CELLS
        order = [sessions.large().name]
        small = sessions.small()
        if small is not None:
            order.append(small.name)
        names = self._fill(order, cells)
        self._cells, self._names = cells, names
        self._previous = small.name if small is not None else None
        self._cards.clear()
        self._wanted.clear()
        self._restored = False
        full, tile = self.ring_sizes()
        sessions.enter_grid(full_bytes=full, tile_bytes=tile)
        self._shown = True
        if self._pip_frame is not None:
            self._pip_frame.suspend()
        self._playing = self._showing()
        self.frame.show_grid(names, sessions.large().name, self._playing)
        if self._tiles_view is not None:
            self._tiles_view.show()
        self._follow_area()
        self._update_transport()
        self._paint()
        logger.info("Multiview: %d tiles", cells)
        self.changed.emit()
        return True

    def leave(self, *, keep_pip: bool = True) -> None:
        """Back to one picture: the focused tile is the big picture; with `keep_pip` the tile
        focused before it (else the first other that plays) stays as the picture-in-picture;
        the other tiles stop."""
        if not self._shown:
            return
        sessions = self.sessions
        large = sessions.large().name
        self._save()  # the grid as it is now, for "Last grid" next time
        keep = self._kept() if keep_pip else None
        for slot in sessions.slots():  # a tile left by a smaller layout too
            if slot.name not in (large, keep):
                self._stop(slot.name)
        if self._forget is not None:
            for name in self._names:  # no link on its way opens a tile any more
                self._forget(name)
        self._shown = False
        self._cards.clear()
        self._wanted.clear()
        self._linking.clear()
        self._retried.clear()
        self.frame.hide_grid()
        if self._tiles_view is not None:
            self._tiles_view.set_tiles([])
            self._tiles_view.hide()
        if self._pip_frame is not None:
            self._pip_frame.set_roles(large, keep or sessions.other().name)
            self._pip_frame.resume()
        sessions.leave_grid(keep)  # PiP opens on the kept tile, or stays closed
        small = self._pip_frame.small_rect() if self._pip_frame is not None else None
        if small is not None:
            if self._host is not None:
                self._host.set_hole(small)
            if self._transport is not None:
                self._transport.follow_pip(small)
        self._update_transport()
        self._names = []
        logger.info("Multiview left")
        self.changed.emit()

    def shutdown(self) -> None:
        """The window closes: the tiles stop with the grid (the large picture is the shell's)."""
        self.leave(keep_pip=False)

    def set_cells(self, cells: int) -> None:
        """Another layout: tiles beyond it stop (the focused one moves into it)."""
        if not self._shown or cells not in self.choices() or cells == len(self._names):
            return
        large = self.sessions.large().name
        if cells > len(self._names):
            names = self._fill(self._names, cells)
        else:
            names = self._names[:cells]
            if large not in names:
                names[-1] = large
            for name in self._names:
                if name not in names:  # it stops, and nothing on its way opens there
                    self._stop(name)
                    self._forget_tile(name)
        self._cells, self._names = cells, names
        if self._previous not in names:
            self._previous = None
        self._update_transport()
        self._refresh(force=True)
        self.changed.emit()

    def next_layout(self) -> None:
        """The layout chip: the next layout offered (after the last, the first)."""
        choices = self.choices()
        current = len(self._names)
        later = [cells for cells in choices if cells > current]
        self.set_cells(later[0] if later else choices[0])

    # ---- a tile's channel ---------------------------------------------------------------

    def refuse(self, channel: dict, name: Optional[str] = None) -> bool:
        """`channel` was chosen for tile `name` (the focused one by default): when its
        provider's other streams take all its streams, the tile shows the card instead,
        nothing asks for a link (an STB portal mints one per request), and True."""
        if not self._shown:
            return False
        provider = item_provider.provider_dict(self.catalog, channel)
        limit = self._budget.limit_for(
            provider, item_provider.max_connections(self.catalog, channel)
        )
        if limit is None:
            return False
        provider_name = item_provider.provider_name(self.catalog, channel)
        tile = name if name is not None else self.sessions.large().name
        others = self._taking(provider_name) - {tile}  # its own stream goes
        if limit > 0 and len(others) < limit:
            return False
        card = self._cards.get(tile)
        again = card is not None and card.state == DENIED and ZapContext.same(card.channel, channel)
        self._linking.pop(tile, None)
        self._stop(tile)
        self._deny(tile, channel, provider_name)
        if again:  # a click on the card: say it still holds
            self._toast(NO_STREAM.format(provider=provider_name or "your provider"), "error")
        return True

    def _taking(self, provider: str) -> set[str]:
        """The tiles that hold a stream of `provider`, or will: their link or their open is on
        its way (several links before any stream would each take one, and some STB portals
        let only the newest play)."""
        taking = set(self._budget.holders(provider))
        for name, channel in self._linking.items():
            if item_provider.provider_name(self.catalog, channel) == provider:
                taking.add(name)
        for name, (_channel, wanted) in self._wanted.items():
            if wanted == provider:
                taking.add(name)
        focused = self._pending() if self._pending is not None else None
        if focused is not None and item_provider.provider_name(self.catalog, focused) == provider:
            taking.add(self.sessions.large().name)
        return taking

    def open_focused(
        self,
        channel: dict,
        action: Optional[Callable[[], None]] = None,
        *,
        provider: str,
        limit: Optional[int],
        start_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Play `channel` in the focused tile within `provider`'s `limit`: through the
        time-shift pipeline with `start_kwargs` (`LiveSession.start`'s), else directly
        (`action` plays it once what the tile played is released)."""
        self.open_tile(
            self.sessions.large().name,
            channel,
            action,
            provider=provider,
            limit=limit,
            start_kwargs=start_kwargs,
        )

    def open_tile(
        self,
        name: str,
        channel: dict,
        action: Optional[Callable[[], None]] = None,
        *,
        provider: str,
        limit: Optional[int],
        start_kwargs: Optional[dict[str, Any]] = None,
    ) -> None:
        """Play `channel` in tile `name` (focused or not), as `open_focused` does."""
        self._cards.pop(name, None)
        self._linking.pop(name, None)
        if not ZapContext.same(self._retried.get(name), channel):
            self._retried.pop(name, None)  # another channel: it may be made again once
        self._wanted[name] = (channel, provider)
        self.sessions.open_slot(
            name,
            action,
            channel=channel,
            provider=provider,
            limit=limit,
            start_kwargs=start_kwargs,
        )
        self._refresh()

    def linking(self, name: str, channel: dict) -> bool:
        """Tile `name`'s link for `channel` is on its way (it opens beside the focused one):
        it says "Connecting…" with the channel's name meanwhile. False: it is not a tile of
        the grid, or its player could not be made (said); nothing should be asked for."""
        if not self._shown or name not in self._names or self._ensure_slot(name) is None:
            return False
        self._cards.pop(name, None)
        self._stop(name)
        self._linking[name] = channel
        self._refresh()
        return True

    def awaits(self, name: str, channel: dict) -> bool:
        """Tile `name` is still in the grid and waits for `channel`'s link (`linking`)."""
        return self._shown and name in self._names and self._linking.get(name) is channel

    def link_failed(self, name: str, channel: dict) -> None:
        """Tile `name`'s link never came: it says so (a click tries again)."""
        if self._linking.get(name) is not channel:
            return
        del self._linking[name]
        if self._shown and name in self._names:
            self._cards[name] = _Card(FAILED, channel, TILE_FAILED)
            self._refresh()

    def label(self, name: str) -> tuple[str, str]:
        """What tile `name`'s label (or card) says: the channel's name, and LIVE, "−MM:SS"
        behind live, "Connecting…" or the card's why."""
        card = self._cards.get(name)
        if card is not None:
            return self._title(name), card.detail
        if self.state(name) in _SHOWING:
            return self._title(name), self._detail(name)
        return self._title(name), ""

    def ring_sizes(self) -> tuple[int, int]:
        """The focused tile's time-shift buffer (the memory per stream) and the others' (the
        optional `multiview_tile_ram_mib`, 64 MiB by default, never under 48), in bytes."""
        full = int(getattr(self.config, "timeshift_ram_mib", DEFAULT_RAM_MIB)) * MIB
        low, high = ConfigManager.MULTIVIEW_TILE_RAM_RANGE
        wanted = getattr(
            self.config,
            "multiview_tile_ram_mib",
            ConfigManager.DEFAULT_OPTION_MULTIVIEW_TILE_RAM_MIB,
        )
        try:
            tile = max(low, min(high, int(wanted))) * MIB
        except TypeError, ValueError:
            tile = ConfigManager.DEFAULT_OPTION_MULTIVIEW_TILE_RAM_MIB * MIB
        return full, min(tile, full)

    def retry(self, name: str) -> None:
        """A click on a card: its channel again, in its tile (focused by then)."""
        card = self._cards.get(name)
        if card is not None and name == self.sessions.large().name:
            self._retry(card.channel)

    # ---- the frame's and the overlay's mouse --------------------------------------------

    def _on_focus_requested(self, name: str) -> None:
        if self._shown and self._focus(name):
            self._focused_by_press = name

    def _on_tile_clicked(self, name: str) -> None:
        just, self._focused_by_press = self._focused_by_press, None
        if not self._shown or name not in self._names:
            return
        if name != self.sessions.large().name and not self._focus(name):
            return
        state = self.state(name)
        if state == EMPTY:
            self._open_drawer()
        elif state in (DENIED, FAILED):
            self.retry(name)
        elif just != name:
            self._activate()  # a click on the focused tile: as on the big picture

    def _on_leave_requested(self, name: str) -> None:
        if self._shown and name in self._names:
            self._focus(name)
            self.leave()

    def _focus(self, name: str) -> bool:
        large = self.sessions.large().name
        if name == large:
            return True
        if self._ensure_slot(name) is None or not self._focus_slot(name):
            return False
        self._previous = large
        return True

    # ---- the session manager's reports --------------------------------------------------

    def _on_large_changed(self, *_args: Any) -> None:
        if self._shown:
            self._refresh(force=True)
            self.changed.emit()

    def _on_slot_state(self, name: str, open_: bool) -> None:
        if open_:
            self._cards.pop(name, None)
        else:
            self._wanted.pop(name, None)
            self._timelines.pop(name, None)
        self._refresh()

    def _on_slot_denied(self, name: str, _limit: int) -> None:
        channel, provider = self._wanted.pop(name, (None, ""))
        if channel is not None:
            self._deny(name, channel, provider)
        self._denied(name)

    def _on_slot_failed(self, name: str, message: str) -> None:
        """A tile's stream failed: it stops (its provider's stream goes back) and says so."""
        if not self._shown or name not in self._names:
            return
        slot = self._slot(name)
        wanted = self._wanted.get(name)
        channel = slot.channel if slot is not None and slot.channel else None
        if channel is None and wanted is not None:
            channel = wanted[0]
        if channel is None:
            return
        logger.info("A tile's stream failed: %s", channel.get("name") or "")
        self._stop(name)
        self._cards[name] = _Card(FAILED, channel, TILE_FAILED)
        self._refresh()
        if self._may_retry(name, channel, message):
            # Its play token, or its portal's session, may have expired (as the big
            # picture's and the PiP's): a fresh link, once per tile and channel.
            self._retried[name] = channel
            assert self._reopen is not None
            self._reopen(name, channel)

    def _may_retry(self, name: str, channel: dict, message: str) -> bool:
        """A tile without the focus (the focused one is the shell's) whose STB channel
        failed, never for the provider's limit, and once per tile and channel."""
        return (
            self._reopen is not None
            and name != self.sessions.large().name
            and message not in REFUSALS
            and not ZapContext.same(self._retried.get(name), channel)
            and item_provider.provider_type(self.catalog, channel) == "STB"
        )

    def _on_player_state(self, name: str, state: str) -> None:
        self._states[name] = state
        self._refresh()

    def _on_timeline(self, name: str, state: dict) -> None:
        self._timelines[name] = state
        if self._shown and name in self._names:
            self._paint()

    # ---- the tiles ----------------------------------------------------------------------

    def _slot(self, name: str) -> Optional[Slot]:
        try:
            return self.sessions.slot(name)
        except KeyError:
            return None

    def _fill(self, names: list[str], cells: int) -> list[str]:
        """`names`, then the next tiles' slot names up to `cells` (their players are made
        when they first take the focus or a channel: `_ensure_slot`)."""
        filled = list(names)[:cells]
        for name in TILE_NAMES:
            if len(filled) >= cells:
                break
            if name not in filled:
                filled.append(name)
        return filled

    def _ensure_slot(self, name: str) -> Optional[Slot]:
        """Tile `name`'s player, made now if it has none (None, said, when it cannot be)."""
        slot = self._slot(name)
        if slot is None:
            slot = self._make_slot(name)
            if slot is None:
                self._toast(NO_PLAYERS, "error")
        return slot

    def _kept(self) -> Optional[str]:
        """The tile that stays small when the grid goes: the one focused before, else the
        first other that plays (one whose link is still on its way has nothing to show)."""
        large = self.sessions.large().name
        candidates = [self._previous, *self._names]
        for name in candidates:
            if not name or name == large or name not in self._names or name in self._linking:
                continue
            slot = self._slot(name)
            if slot is not None and slot.channel is not None and self.state(name) in _SHOWING:
                return name
        return None

    def _forget_tile(self, name: str) -> None:
        """Tile `name` left the grid: what was on its way for it never opens."""
        for kept in (self._cards, self._linking, self._wanted, self._retried, self._timelines):
            kept.pop(name, None)
        if self._forget is not None:
            self._forget(name)

    def _stop(self, name: str) -> None:
        """Stop a tile's stream (nothing when it plays nothing)."""
        slot = self._slot(name)
        if slot is None:
            return
        tuner = slot.tuner
        idle = getattr(tuner, "idle", None)
        if slot.channel is None and (idle is None or idle()):
            return
        self.sessions.close_slot(name)

    def _deny(self, name: str, channel: dict, provider: str) -> None:
        detail = NO_STREAM.format(provider=provider or "your provider")
        self._cards[name] = _Card(DENIED, channel, detail)
        self._refresh()

    def _title(self, name: str) -> str:
        card = self._cards.get(name)
        if card is not None:
            return str(card.channel.get("name") or "")
        linking = self._linking.get(name)
        if linking is not None:
            return str(linking.get("name") or "")
        slot = self._slot(name)
        channel = slot.channel if slot is not None else None
        return str((channel or {}).get("name") or "")

    def _detail(self, name: str) -> str:
        """A playing tile's label state: LIVE, "−MM:SS" behind live, or "Connecting…"."""
        slot = self._slot(name)
        if slot is None:
            return CONNECTING
        timeline = self._timelines.get(name)
        if slot.live.mode == "pipeline" and timeline is not None:
            lag = float(timeline.get("lag", 0.0))
            target = float(timeline.get("target", TARGETS["normal"]))
            return fmt_behind(lag) if lag > target + SHIFTED_MARGIN_S else LIVE
        if slot.live.mode == "direct" or self._states.get(name) in _PLAYING_STATES:
            return LIVE
        return CONNECTING

    def _showing(self) -> set[str]:
        return {name for name in self._names if self.state(name) in _SHOWING}

    def _refresh(self, *, force: bool = False) -> None:
        """The tiles' states changed: the frame shows the surfaces that play, and the
        overlay draws the rest."""
        if not self._shown:
            return
        playing = self._showing()
        large = self.sessions.large().name
        if force or playing != self._playing or self.frame.focused() != large:
            self._playing = playing
            self.frame.set_tiles(self._names, large, playing)  # its tilesChanged paints
        else:
            self._paint()

    def _paint(self) -> None:
        """The overlay draws each tile's label, card or "+", and the focus ring."""
        if not self._shown:
            return
        self._follow_area()
        view = self._tiles_view
        if view is None:
            return
        large = self.sessions.large().name
        tiles = []
        for name, rect in self.frame.tiles():
            title, detail = self.label(name)
            tiles.append(TileView(name, rect, self.state(name), title, detail, name == large))
        view.set_tiles(tiles)

    def _follow_area(self) -> None:
        """The overlays let clicks through to the tiles over the whole page (but what they
        claim), and the controls' shades stay off the pictures."""
        area = self.frame.area()
        if self._host is not None:
            self._host.set_hole(area, cover=True)
        if self._transport is not None:
            self._transport.follow_pip(area)

    def _update_transport(self) -> None:
        if self._transport is not None:
            layout = LAYOUT_NAMES.get(len(self._names), "") if self._shown else ""
            self._transport.set_grid_state(True, self._shown, layout, self.restorable())

    # ---- the last grid ------------------------------------------------------------------

    def saved(self) -> Optional[dict[str, Any]]:
        """The last grid (config `multiview`) as this build reads it: its layout, the focused
        tile's place, and per tile (channel, provider, group) or None. None when there is
        none, it is another version's, or none of its providers is still listed."""
        saved = getattr(self.config, "multiview", None)
        if not isinstance(saved, dict) or saved.get("version") != SAVED_VERSION:
            return None
        cells, records = saved.get("cells"), saved.get("tiles")
        if cells not in CELLS or not isinstance(records, list):
            return None
        tiles = [self._channel_of(record) for record in records[:cells]]
        if not any(tiles):
            return None
        focus = saved.get("focus")
        return {"cells": cells, "focus": focus if isinstance(focus, int) else 0, "tiles": tiles}

    def _channel_of(self, record: Any) -> Optional[tuple[dict, str, str]]:
        """A saved tile's channel (stamped with its provider unless the list shows that
        one), the provider and the group; None for an empty tile or a provider gone."""
        if not isinstance(record, dict):
            return None
        item, provider = record.get("item"), record.get("provider_name")
        if not isinstance(item, dict) or not isinstance(provider, str) or not provider:
            return None
        if provider not in self.catalog.provider_names():
            return None
        if provider != self.catalog.current_provider_name():
            item = with_provider(item, provider)
        return item, provider, str(record.get("group_id") or "")

    def restorable(self) -> bool:
        """The grid shows, and the last grid has a channel no tile shows yet."""
        if not self._shown or self._restored:
            return False
        saved = self.saved()
        if saved is None:
            return False
        return any(
            entry is not None and not self._shown_anywhere(entry[0]) for entry in saved["tiles"]
        )

    def restore_last(self) -> None:
        """The "Last grid" chip (and View → Restore Last Multiview): the last grid's layout
        (as large as offered, never fewer tiles than show now), its channels in their tiles
        (or the next free ones), each once its provider is ready, and its focus."""
        saved = self.saved()
        if not self._shown or saved is None or self._restore is None:
            return
        self._restored = True
        offered = [cells for cells in self.choices() if cells <= saved["cells"]]
        cells = max(offered[-1] if offered else self.choices()[0], len(self._names))
        if cells != len(self._names):
            self.set_cells(cells)
        names = self._names
        taken = {name for name in names if self.state(name) in _SHOWING}
        placed: list[tuple[str, dict, str, str]] = []
        for index, entry in enumerate(saved["tiles"]):
            if entry is None or self._shown_anywhere(entry[0]):
                continue
            free = [name for name in names if name not in taken]
            if not free:
                break
            name = names[index] if index < len(names) and names[index] in free else free[0]
            taken.add(name)
            placed.append((name, *entry))
        focus = saved["focus"]
        if 0 <= focus < len(names) and any(name == names[focus] for name, *_ in placed):
            self._focus(names[focus])  # it opens as zapping does, the others beside it
        for name, channel, provider, group in placed:
            self._restore(name, channel, provider, group)
        self._update_transport()
        self.changed.emit()

    def _shown_anywhere(self, channel: dict) -> bool:
        for name in self._names:
            slot = self._slot(name)
            if self._linking.get(name) is not None and ZapContext.same(
                self._linking[name], channel
            ):
                return True
            if (
                slot is not None
                and self.state(name) in _SHOWING
                and ZapContext.same(slot.channel, channel)
            ):
                return True
        return False

    def _save(self) -> None:
        """Keep the grid as it is (config `multiview`, versioned; older builds ignore it):
        its layout, the focused tile's place and each tile's channel (a card's too)."""
        records: list[Optional[dict[str, Any]]] = []
        for name in self._names:
            card, slot = self._cards.get(name), self._slot(name)
            if card is not None:
                channel: Optional[dict] = card.channel
            elif name in self._linking:
                channel = self._linking[name]
            elif slot is not None and self.state(name) in _SHOWING:
                channel = slot.channel
            else:
                channel = None
            if channel is None:
                records.append(None)
                continue
            group = slot.group_key if slot is not None and slot.group_key else ""
            records.append(
                {
                    "provider_name": item_provider.provider_name(self.catalog, channel),
                    "group_id": str(group),
                    "item": dict(channel),
                }
            )
        if not any(records):
            return  # nothing to bring back: the grid before stays
        large = self.sessions.large().name
        self.config.multiview = {
            "version": SAVED_VERSION,
            "cells": len(self._names),
            "focus": self._names.index(large) if large in self._names else 0,
            "tiles": records,
        }
        try:
            self.config.save_config()
        except Exception as e:
            logger.warning("Couldn't save the multiview: %s", safe_message(e))

    # ---- a channel dragged from the channel list ----------------------------------------

    def tile_at(self, point: QPoint) -> Optional[str]:
        """The tile under `point` (global coordinates), else None."""
        if not self._shown:
            return None
        local = self.frame.map_from_global(point)
        for name, rect in self.frame.tiles():
            if rect.contains(local):
                return name
        return None

    def drag_over(self, point: QPoint, channel: dict) -> None:
        """A channel dragged from the channel list is over `point` (global): the tile under
        it is lit, and the channel's name follows the pointer."""
        view = self._tiles_view
        if view is None or not self._shown:
            return
        name = self.tile_at(point)
        view.set_drop(name, str(channel.get("name") or ""), view.mapFromGlobal(point))

    def drag_ended(self) -> None:
        if self._tiles_view is not None:
            self._tiles_view.set_drop(None, "", QPoint())
