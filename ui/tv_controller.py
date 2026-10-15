"""The TV shell's behaviour: wires the catalog, navigation, zapping and playback to the overlays.

Link requests are serialised: at most one is in flight (STB portals mint a link per request and
providers limit connections). A tune that arrives meanwhile replaces the single queued target,
and only the newest target ever plays.

Live channels on http(s) play through the time-shift pipeline (`LiveSession`) when the window
provides one. `LiveTuner` opens every provider stream (pipeline, direct, movie, external player)
only once the previous one is released, and gives pipeline tunes a slot of the provider's
connection budget (spec §6.9). Pipeline playback errors are LiveSession's to recover; it reports
what it cannot as `failed`. An STB channel that fails gets a fresh link (its play token may have
expired) once per tune before the error shows; a refusal for the provider's connection limit
says so.

In external-player mode a zap step launches the player only once the zapping settles.

Movies and episodes play through `play_vod` (the drawer and the Library), resuming where they
stopped. When one ends, autoplay offers the next episode of its series or the next title of
its genre on the Next card; a next season that is not loaded yet is requested first. The
transport's Next plays it at once. External players never autoplay (as in v1.13.8).

The drawer's context menu requests links beside the tune: one at a time, never in the tune's
slot, and a link request opens no stream. Its "Play in VLC/MPV" releases the in-app stream
first, as an external pick does.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
import re
from typing import Callable, Optional, Union
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea, QMenu, QWidget

from services import external_players
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme, now_next
from services.live.live_session import UNAVAILABLE, LiveSession
from services.live.source import REFUSALS, STREAM_REFUSED
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode, episode_key, item_key
from services.next_episode import NeedEpisodes, NextUp, next_episode, next_movie
from services.redact import safe_traceback
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind, classify
from services.zapping import ZapContext
from ui.keymap import Action, as_int
from ui.live_tuner import LiveStart, LiveTuner
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.next_card import NextCard
from ui.overlays.painting import fmt_duration
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import BannerInfo, ZapBanner
from ui.pointer import PointerInput
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

WELCOME_TEXT = "Add a provider in Settings to start watching"
INFO_MS = 3000
ERROR_MS = 6000
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
LIVE_SEEK_S = 10.0  # ←/→ while time-shifting (spec §6.7)
TUNING_KEEP_MS = 3000  # under the banner's 4 s: it is shown again while a link resolves
EXTERNAL_SETTLE_MS = 1000  # the zap burst window: an external player opens once zapping stops
LIMITED_TEXT = "Your provider limits concurrent streams"  # when its limit is not known
REFUSED_TEXT = "The provider refused the stream — it may"  # a plain 403: maybe the limit
TICK_PROGRAMMES = 6  # programme starts marked on the time-shift bar
FINISHED_TEXT = "Finished"
NOTHING_NEXT_TEXT = "Nothing to play next"
NEXT_SEASON_FAILED_TEXT = "Couldn't load the next season"
FAVORITES_SAVE_FAILED_TEXT = "Couldn't save your favorites; they will be lost when QiTV closes"
# The Library's play context (LibraryController._context): what Next and autoplay follow.
_PLAY_CONTEXT = {
    "start_over": False,
    "series": None,
    "season": None,
    "episodes": None,
    "genre_items": None,
}
_PIPELINE_SCHEMES = ("http", "https")  # what the pipeline's source can read
_ACTIVE_STATES = frozenset({"opening", "buffering", "playing", "paused"})
_VOLUME_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.MetaModifier.value
)
_PINNED_TITLES = {FAVORITES_GROUP: "Favorites", RECENT_GROUP: "Recent"}
# While the guide is open these close it first and then act as usual (they tune or open the
# drawer); the media keys do nothing, as with the drawer open.
_GUIDE_CLOSERS = (
    Action.OK,
    Action.OPEN_DRAWER,
    Action.PREVIOUS,
    Action.DIGIT,
    Action.CH_NEXT,
    Action.CH_PREV,
)
_GUIDE_IGNORED = (Action.MEDIA_NEXT, Action.MEDIA_PREV)
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE)
_QUERY = re.compile(r"\S*\?\S*=\S*")  # a query string carries "=": "Who?Me" stays


def scrub(text: object) -> str:
    """Drop addresses and query strings from a message: they can carry credentials."""
    cleaned = _QUERY.sub("…", _ADDRESS.sub("…", str(text)))
    return " ".join(cleaned.split())


def _title(item: Optional[dict], *, episode: bool = False) -> str:
    """A title's name as the drawer shows it ("" without a title)."""
    if not isinstance(item, dict):
        return ""
    name = (item.get("ename") if episode else None) or item.get("name") or item.get("title")
    return str(name or "Untitled")


def _css(value: str, alpha: Optional[float] = None) -> str:
    color = qcolor(value, alpha)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.2f})"


def _menu_style() -> str:
    """The context menu in the shell's colours, with the shared focus style."""
    line = _css(PALETTE.text, 0.12)
    return (
        f"QMenu {{ background: {_css(PALETTE.panel_raised)}; color: {_css(PALETTE.text)};"
        f" border: 1px solid {line}; padding: 4px; }}"
        " QMenu::item { padding: 6px 22px; border: 1.5px solid transparent; border-radius: 6px; }"
        f" QMenu::item:selected {{ background: {_css(PALETTE.accent, FOCUS_FILL_ALPHA)};"
        f" border-color: {_css(PALETTE.accent)}; }}"
        f" QMenu::separator {{ height: 1px; background: {line}; margin: 4px 8px; }}"
    )


@dataclass
class Overlays:
    banner: ZapBanner
    column: ChannelColumn
    drawer: Drawer
    transport: Transport
    toast: Toast
    guide: Guide
    next_card: NextCard


@dataclass
class _Target:
    """Something to play once its link resolves."""

    item: dict
    section: str  # "live", "movies" or "series" (an episode)
    title: str
    group_key: str = ""
    subtitle: str = ""
    zapped: bool = False  # chosen by a zap step (↑/↓, digits, previous), not picked
    retried: bool = False  # its link was made again after a playback failure (STB tokens)
    context: Optional[dict] = None  # a movie's or episode's play context (see play_vod)
    start_over: bool = False  # from the start, not where it stopped

    @property
    def live(self) -> bool:
        return self.section == "live"

    @property
    def item_type(self) -> str:
        return {"live": "channel", "series": "episode"}.get(self.section, "movie")

    def same_as(self, other: _Target) -> bool:
        if self.section != other.section or not ZapContext.same(self.item, other.item):
            return False
        # STB episodes share the season's cmd and id; the episode number tells them apart.
        return self.section != "series" or episode_key(self.item) == episode_key(other.item)


@dataclass
class _LinkJob:
    """A context menu's link request: `then` takes the link once it is ready."""

    target: _Target
    then: Callable[[str], None] = field(repr=False)


@dataclass
class _Awaiting:
    """Autoplay waits for a season's episodes before it can name what follows `target`."""

    target: _Target
    play_now: bool  # the transport's Next: play it at once, without the card
    series_id: str
    season_id: str


class TVController(QObject):
    def __init__(
        self,
        *,
        config,
        catalog,
        nav,
        zap,
        session,
        watch,
        overlays: Overlays,
        window_actions,
        external=None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        logos=None,
        live: Optional[LiveSession] = None,
        budget: Optional[ConnectionBudget] = None,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.config = config
        self.catalog = catalog
        self.nav = nav
        self.zap = zap
        self.session = session
        self.watch = watch
        self.overlays = overlays
        self.window_actions = window_actions
        self.external = external if external is not None else external_players
        self._now = now
        self._logos = logos
        # Opens every stream; `live` (the time-shift pipeline) None plays every live channel
        # directly (no libVLC, tests).
        self._tuner = LiveTuner(session, config, live, budget, self)
        self.live = live
        self.budget = self._tuner.budget
        self._ticks: list[tuple[float, str]] = []
        self._unavailable_noticed: set[tuple[str, str]] = set()  # (provider, channel) this run
        self._held_notice: Optional[tuple[str, str]] = None  # shown once the channel plays
        self._resume: Optional[_Target] = None  # what stop_live() stopped, for resume_live()
        self._external_pending: Optional[tuple[_Target, str]] = None
        self._external_settle = QTimer(self, singleShot=True)
        self._external_settle.timeout.connect(self._launch_settled)
        self._started = False
        self._closed = False
        self._inflight: Optional[tuple[int, _Target]] = None
        self._queued: Optional[_Target] = None
        self._current: Optional[_Target] = None  # handed to the player
        self._cid: Optional[str] = None  # resume key of the VOD being played
        self._channel: Optional[dict] = None  # the channel tuned last, for INFO and the drawer
        self._digits = ""  # number entry under way
        self._state = "idle"
        self._provider: Optional[str] = None
        self._await_live = False  # after providerReady: the first live section decides
        self._loading = False  # the "Loading <provider>…" hint is up
        self._autotune: Optional[tuple[dict, str]] = None
        self._audio_id: Optional[int] = None
        self._subtitle_id: Optional[int] = None
        self._guide_channels: tuple[str, list[dict]] = ("", [])  # what the guide opened on
        self._next: Optional[tuple[dict, str, dict]] = None  # what the Next card offers
        self._awaiting: Optional[_Awaiting] = None
        self._side: Optional[tuple[int, _LinkJob]] = None  # the context menu's link request
        self._side_queue: deque[_LinkJob] = deque()
        self._column_wheel = PointerInput(self)
        # While a live link resolves, the banner is shown again before it fades (Minor 11).
        self._tuning = QTimer(self, interval=TUNING_KEEP_MS)
        self._tuning.timeout.connect(self._keep_tuning_banner)

    # ---- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._wire()
        if not self.catalog.provider_names():
            # ProviderManager cannot set up without providers; the setup would only fail.
            self._welcome()
            return
        self._show_loading(self._setup_name(None))
        self.catalog.select_provider()

    def _wire(self) -> None:
        catalog, zap, session = self.catalog, self.zap, self.session
        column, drawer = self.overlays.column, self.overlays.drawer
        transport = self.overlays.transport
        catalog.providerReady.connect(self._on_provider_ready)
        catalog.providerFailed.connect(self._on_provider_failed)
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        catalog.linkReady.connect(self._on_link_ready)
        catalog.linkFailed.connect(self._on_link_failed)
        catalog.epgReady.connect(self._on_epg_ready)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.episodesFailed.connect(self._on_episodes_failed)
        self.nav.notice.connect(self._on_notice)
        self.nav.providerRequested.connect(self._on_provider_requested)
        zap.tuneRequested.connect(self._on_zap_tune)
        zap.columnRequested.connect(self._on_column_requested)
        zap.highlightChanged.connect(self._on_highlight)
        zap.digitsChanged.connect(self._on_digits)
        zap.numberNotFound.connect(self._on_number_not_found)
        session.stateChanged.connect(self._on_state)
        session.positionChanged.connect(self._on_position)
        session.errorOccurred.connect(self._on_error)
        session.ended.connect(self._on_ended)
        column.closed.connect(zap.close_column)
        column.rowClicked.connect(self._on_column_row)
        drawer.playRequested.connect(self.play_item)
        drawer.contextRequested.connect(self._on_context)
        self.overlays.guide.tuneRequested.connect(self._on_guide_tune)
        self.overlays.next_card.playNow.connect(self._on_play_now)
        self.overlays.next_card.cancelled.connect(self._on_next_cancelled)
        transport.nextRequested.connect(self._on_next_requested)
        transport.playPauseRequested.connect(self._tuner.toggle_pause)
        transport.seekRequested.connect(session.seek)
        transport.seekRelativeRequested.connect(self._seek_relative_ms)
        transport.speedRequested.connect(self._set_speed)
        transport.goLiveRequested.connect(self._go_live)
        transport.programmeStartRequested.connect(self._programme_start)
        transport.seekToRequested.connect(self._seek_to)
        transport.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.audioRequested.connect(self._cycle_audio)
        transport.subtitleRequested.connect(self._cycle_subtitles)
        self._tuner.startFailed.connect(self._on_live_start_failed)
        if self.live is not None:
            live = self.live
            live.modeChanged.connect(self._on_live_mode)
            live.timelineChanged.connect(self._on_live_timeline)
            live.notice.connect(self._on_live_notice)
            live.failed.connect(self._on_live_failed)
        if self._logos is not None:
            self._logos.logoReady.connect(self._on_logo_ready)
        # The column's list would scroll on its own; wheel over it zaps instead.
        self._column_wheel.wheelSteps.connect(self._on_column_wheel)
        column.installEventFilter(self)
        for area in column.findChildren(QAbstractScrollArea):
            area.installEventFilter(self)
            area.viewport().installEventFilter(self)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._drop_links()
        self._drop_next()
        self._tuning.stop()
        self._cancel_external()
        self._autotune = None
        self._loading = False
        self.overlays.toast.set_hint(None)  # e.g. the closing notice needs the toast
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        try:
            self._tuner.shutdown()
        except Exception as e:
            self._log_failure("stop playback", e)

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if not self.catalog.provider_names():
            self._welcome()
            return
        self._show_loading(self._setup_name(name))
        # Pending links are settled when the provider is ready (see _on_provider_ready).
        self.catalog.select_provider(name, force_refresh=force_refresh)

    def refresh_overlays(self) -> None:
        """After a settings change: repaint the rows and reload the guide."""
        self._repaint()
        self.catalog.refresh_epg()

    def stop_live(self) -> None:
        """Stop what plays (window close, self-update): its slot goes back once released."""
        self._resume = self._current if self._has_media() else None
        self._stop_tune()

    def resume_live(self) -> None:
        """Play again what stop_live() stopped (a self-update that could not launch).

        With a fresh link: a channel tunes again, a movie or episode reopens at the position
        the watch state saved.
        """
        target, self._resume = self._resume, None
        if not self._closed and target is not None and target is self._current:
            self._request(replace(target, zapped=False, retried=False, start_over=False))

    def settings_saved(self) -> None:
        """Settings were saved: a new live latency reaches the running channel at once.

        The time-shift settings (disk, size) apply from the next tune.
        """
        self._tuner.set_latency(self.config.live_latency)

    def _repaint(self) -> None:
        """Repaint what depends on settings or EPG: column rows and the open drawer."""
        self.overlays.column.refresh_now()
        self.nav.changed.emit()

    # ---- input --------------------------------------------------------------------------------

    def key_context(self) -> str:
        """The keymap context for the next key: the open guide or drawer takes the keys."""
        if self.overlays.guide.isVisible():
            return "guide"
        return "drawer" if self.overlays.drawer.isVisible() else "player"

    def handle(self, action: Action, payload: str = "") -> None:
        if self._closed:
            return
        if self.overlays.guide.isVisible() and self._guide_key(action):
            return
        drawer = self.overlays.drawer
        if action in (Action.CH_NEXT, Action.CH_PREV):
            delta = 1 if action is Action.CH_NEXT else -1
            if self._playing_vod():
                self._change_volume(-VOLUME_STEP * delta)  # ↑ is louder
            else:
                self.zap.step(delta)
        elif action in (Action.MEDIA_NEXT, Action.MEDIA_PREV):
            delta = 1 if action is Action.MEDIA_NEXT else -1
            if drawer.isVisible():
                return  # the drawer is browsing; play/pause still works
            if self._playing_vod():
                self._seek(delta * SEEK_STEP_MS)
            else:
                self.zap.step(delta)
                self._show_transport()
        elif action is Action.DIGIT:
            if not self._playing_vod():
                self.zap.digit(payload)
        elif action is Action.PREVIOUS:
            self.zap.previous()
        elif action is Action.OK:
            if self.overlays.next_card.isVisible():
                self.overlays.next_card.activate()  # Play now
            elif self.overlays.column.isVisible():
                self._on_column_row(self.overlays.column.current_index())  # tune it now
            else:
                self._open_drawer()
        elif action is Action.OPEN_DRAWER:
            self.overlays.column.close()
            self._open_drawer()
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            back = action is Action.SEEK_BACK
            if self._pipeline_live() and self.live is not None:
                self.live.seek_relative(-LIVE_SEEK_S if back else LIVE_SEEK_S)
                self._show_transport()
            else:
                self._seek(-SEEK_STEP_MS if back else SEEK_STEP_MS)
        elif action is Action.PLAY_PAUSE:
            self._tuner.toggle_pause()
            self._show_transport()
        elif action is Action.INFO:
            self._info()
        elif action is Action.GO_LIVE:
            self._go_live()
        elif action is Action.GUIDE:
            self._open_guide()
        elif action is Action.FAVORITE:
            self._favorite()
        elif action is Action.FULLSCREEN:
            self.window_actions.toggle_fullscreen()
        elif action is Action.MINI_PLAYER:
            self.window_actions.toggle_mini_player()
        elif action is Action.MUTE:
            muted = self.session.toggle_mute()
            self._toast("Muted" if muted else f"Volume {self.session.volume()}%")
        elif action in (Action.VOL_UP, Action.VOL_DOWN):
            self._change_volume(VOLUME_STEP if action is Action.VOL_UP else -VOLUME_STEP)
        elif action is Action.ESCAPE:
            self._escape()
        elif action in (Action.NAV_UP, Action.NAV_DOWN):
            drawer.move_by(-1 if action is Action.NAV_UP else 1)
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            drawer.page(-1 if action is Action.NAV_PAGE_UP else 1)
        elif action is Action.NAV_LEFT:
            drawer.left()
        elif action is Action.NAV_RIGHT:
            drawer.right()
        elif action is Action.NAV_ACTIVATE:
            drawer.activate()
        elif action is Action.NAV_TEXT:
            drawer.type_text(payload)
        elif action is Action.NAV_BACKSPACE:
            drawer.backspace()

    def video_clicked(self) -> None:
        if self.overlays.drawer.isVisible():
            self.overlays.drawer.close_drawer()
        else:
            self.handle(Action.OPEN_DRAWER)

    def video_double_clicked(self) -> None:
        self.window_actions.toggle_fullscreen()

    def mouse_back(self) -> None:
        """The mouse's Back button: a drawer layer up (spec §5.3); closed, it opens."""
        self._mouse_layer(Action.NAV_LEFT)

    def mouse_forward(self) -> None:
        """The mouse's Forward button: a drawer layer down; closed, it opens."""
        self._mouse_layer(Action.NAV_RIGHT)

    def _mouse_layer(self, action: Action) -> None:
        if self._closed:
            return
        if self.overlays.drawer.isVisible():
            self.handle(action)
        else:
            self.handle(Action.OPEN_DRAWER)

    def mouse_moved(self) -> None:
        self._show_transport()

    def wheel(self, steps: int, modifiers) -> None:
        """`steps` follows PointerInput: +1 is one notch down (the next channel)."""
        if not steps or self._closed:
            return
        volume = as_int(modifiers) & _VOLUME_MODIFIERS
        if volume or self.config.mouse_wheel_action == "volume" or self._playing_vod():
            self._change_volume(-VOLUME_STEP * steps)
        else:
            self._zap_steps(steps)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and self.overlays.column.isVisible():
            self._column_wheel.wheel(event.angleDelta().y(), event.modifiers())
            event.accept()
            return True
        return False

    def _on_column_wheel(self, steps: int, modifiers) -> None:
        if self._closed:
            return
        if as_int(modifiers) & _VOLUME_MODIFIERS:
            self._change_volume(-VOLUME_STEP * steps)
        else:
            self._zap_steps(steps)  # zap.step re-highlights, which keeps the column open

    def _zap_steps(self, steps: int) -> None:
        for _ in range(abs(steps)):
            self.zap.step(1 if steps > 0 else -1)

    # ---- tuning and playing -------------------------------------------------------------------

    def tune(self, channel: dict, *, group_key: str, channels: list[dict]) -> None:
        """Tune a channel the shell chose (drawer, autotune, column click)."""
        self._tune(channel, group_key, channels, from_zap=False)

    def _tune(self, channel: dict, group_key: str, channels: list[dict], *, from_zap: bool) -> None:
        if self._closed:
            return
        self._cancel_external()  # a newer choice: the settling external launch is dropped
        context = self.zap.context
        if group_key != context.group_key or context.index_of(channel) < 0:
            # Numbers reach every live channel, not only the group's (TV numbers are global).
            self.zap.set_context(ZapContext(group_key, channels, fallback=self._all_live()))
        playing = self._playing_live()
        stay = playing is not None and ZapContext.same(channel, playing.item)
        if not from_zap and not stay:
            self.zap.note_request(channel)  # ↑/↓ move on from it while its link is pending
        self.overlays.column.close()
        self._channel = channel
        self._show_banner(channel)
        if stay:
            # The channel already playing was chosen again: never restart it, and drop a
            # pending choice when it settles (the "stay" marker is the playing target).
            if self._inflight is not None or self._queued is not None:
                self._queued = playing
            self.zap.tuned(channel)
            return
        title = str(channel.get("name") or "")
        self._request(_Target(channel, "live", title, group_key=group_key, zapped=from_zap))
        self._tuning.start(TUNING_KEEP_MS)

    def play_item(self, node: NavNode, siblings: list[NavNode], parent: NavNode) -> None:
        if self._closed:
            return
        section = self.nav.section_of(node)
        drawer = self.overlays.drawer
        if node.layer == "item" and section == "live":
            # The whole group, not just the rows the drawer's filter shows: ↑/↓ walk the group.
            nodes = self.nav.children(parent) or siblings
            channels = [n.payload for n in nodes if isinstance(n.payload, dict)]
            self.tune(node.payload, group_key=parent.key, channels=channels)
            drawer.close_drawer()
        elif node.layer == "episode" or (node.layer == "item" and section == "movies"):
            drawer.close_drawer()
            # The whole season or group, not the filtered rows: Next follows them.
            nodes = self.nav.children(parent) or siblings
            items = [n.payload for n in nodes if isinstance(n.payload, dict)]
            if node.layer == "episode":
                path = reversed(self.nav.path)
                series = next((n.payload for n in path if n.layer == "item"), None)
                season = parent.payload if parent.layer == "season" else None
                context = {"series": series, "season": season, "episodes": items}
                self.play_vod(node.payload, "episode", context)
            else:
                self.play_vod(node.payload, "movie", {"genre_items": items})
        else:
            logger.debug("Not playable: %s", node)

    def play_vod(self, item: dict, kind: str, context: Optional[dict] = None) -> None:
        """Play a movie or an episode where it stopped (the drawer and the Library).

        `kind` is "movie" or "episode". `context` is the Library's play context: start_over
        plays from the start; an episode's series and season, or a movie's genre_items, give
        Next and autoplay what follows. Its link is the tune slot's, and it opens once what
        plays now (a live channel too) is released.
        """
        if self._closed:
            return
        context = {**_PLAY_CONTEXT, **(context or {})}
        episode = kind == "episode"
        self._request(
            _Target(
                item,
                "series" if episode else "movies",
                _title(item, episode=episode),
                subtitle=_title(context["series"]) if episode else "",
                context=context,
                start_over=bool(context["start_over"]),
            )
        )

    def _all_live(self) -> list[dict]:
        return list(self.catalog.items("live", ALL_GROUP_ID) or [])

    def _request(self, target: _Target) -> None:
        self._cancel_external()
        self._drop_next()  # a newer choice than what the Next card offers
        if self._inflight is not None:
            self._queued = target  # replaces any older queued target
            return
        self._queued = None  # the target sent now is the newest choice
        request_id = self.catalog.request_link(
            target.item, section=target.section, is_episode=target.section == "series"
        )
        self._inflight = (request_id, target)

    def _drop_links(self) -> None:
        """Forget every pending link (a provider setup drops their results anyway)."""
        self._inflight = None
        self._queued = None
        self._side = None
        self._side_queue.clear()

    def _take(self, request_id: int) -> Optional[_Target]:
        """The in-flight target when `request_id` is the request we are waiting for."""
        if self._closed or self._inflight is None or self._inflight[0] != request_id:
            logger.debug("Ignoring the result of link request %s", request_id)
            return None
        target = self._inflight[1]
        self._inflight = None
        return target

    def _follow_queue(self, settled: _Target) -> bool:
        """Act on a choice made while `settled` was in flight; False when there was none."""
        queued, self._queued = self._queued, None
        if queued is None:
            return False
        if self._is_stay(queued):
            logger.debug("Dropping the link for %s: staying on %s", settled.title, queued.title)
        else:
            logger.debug(
                "Dropping the link for %s: %s was chosen since", settled.title, queued.title
            )
            self._request(queued)
        return True

    def _is_stay(self, target: Optional[_Target]) -> bool:
        """True for the marker that keeps the playing channel (see _tune)."""
        return target is not None and target is self._current and self._has_media()

    def _on_link_ready(self, request_id: int, url: str) -> None:
        job = self._take_side(request_id)
        if job is not None:
            try:
                job.then(url)
            except Exception as e:
                self._log_failure(f"use the link of {job.target.title}", e)
                self._error(f"{job.target.title}: {e}")
            self._next_side_link()
            return
        target = self._take(request_id)
        if target is None:
            return
        queued = self._queued
        if queued is not None and not self._is_stay(queued) and queued.same_as(target):
            self._queued = None
            target = queued  # chosen again meanwhile: this link serves the newest choice
        elif self._follow_queue(target):
            return
        self._play_link(target, url)

    def _play_link(self, target: _Target, url: str) -> None:
        previous = self._current
        try:
            self._deliver(target, url)
        except Exception as e:
            self._current = previous  # whatever was playing before is still the current one
            self._log_failure(f"play {target.title}", e)
            self._restore_channel()
            self._error(f"{target.title}: {e}")

    def _on_link_failed(self, request_id: int, message: str) -> None:
        job = self._take_side(request_id)
        if job is not None:
            self._error(f"{job.target.title}: {message}")
            self._next_side_link()
            return
        target = self._take(request_id)
        if target is None:
            return
        if message == PROVIDER_LOADING_MESSAGE:
            # A provider setup is running; providerReady asks again for the newest choice:
            # one queued meanwhile is newer than the target that just failed.
            if self._queued is None:
                self._queued = target
            return
        if self._follow_queue(target):
            return  # the failed target was superseded: no error for it
        self._restore_channel()
        self._error(f"{target.title}: {message}")

    def _keep_tuning_banner(self) -> None:
        """Show the banner again while the link of the channel it announces resolves."""
        pending = self._queued or (self._inflight[1] if self._inflight is not None else None)
        channel, banner = self._channel, self.overlays.banner
        tuning = (
            not self._closed
            and pending is not None
            and pending.live
            and not self._is_stay(pending)
            and channel is not None
            and ZapContext.same(pending.item, channel)
        )
        if not tuning:
            self._tuning.stop()  # delivered or failed: the banner fades on its own
            return
        if banner.isVisible() and channel is not None:  # never one that was dismissed
            banner.show_info(self.banner_info(channel))

    def _restore_channel(self) -> None:
        """After a failed tune, INFO and the drawer follow the channel still playing."""
        current = self._current
        self._channel = current.item if current is not None and current.live else None

    def _deliver(self, target: _Target, url: str) -> None:
        item = target.item
        launcher = self._external_launcher()
        self._audio_id = self._subtitle_id = None
        self._held_notice = None
        self._ticks = []  # the old channel's programme ticks
        if launcher is not None:
            if target.live and target.zapped:
                # Each zap step would start a player: only the one zapping settles on does.
                self._external_pending = (target, url)
                self._external_settle.start(EXTERNAL_SETTLE_MS)
                return
            # The in-app stream is released first: the player opens the provider again.
            self._tuner.open(self._guarded(target, lambda: self._launch(target, url)))
            return
        if target.live:
            kind = classify(url, self.catalog.is_live("live"))
            if self._through_pipeline(url, kind):
                if not self._tune_pipeline(target, url):
                    return  # over the provider's stream limit: nothing opens
                self._current, self._cid = target, None
                self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            else:
                self._current, self._cid = target, None

                def play_direct() -> None:
                    self.session.play(
                        url,
                        kind=kind,
                        latency=self.config.live_latency,
                        user_agent=item.get("user_agent"),
                    )
                    self._reset_speed()

                self._tuner.open(self._guarded(target, play_direct), direct=True)
            if self.overlays.transport.isVisible():
                self._show_live_transport(target)
        else:
            provider = self.catalog.current_provider_name()
            cid = self.watch.content_id(provider, item, item_type=target.item_type)
            start = 0 if target.start_over else self.watch.resume_position(cid)
            self._current, self._cid = target, cid

            def open_vod() -> None:
                self.session.play(url, kind=classify(url, False), start_ms=start)
                self._reset_speed()

            self._tuner.open(self._guarded(target, open_vod), direct=True)
            if start:
                self._toast(f"Resumed at {fmt_duration(start)}")
            self.overlays.banner.dismiss()  # the transport takes the bottom of the video
            self.overlays.transport.set_next_available(self._has_next(target))
            self.overlays.transport.show_vod(target.title, target.subtitle)
        if self._current is target:  # else opening it failed at once (see _guarded)
            self._delivered(target, url)

    def _guarded(self, target: _Target, open_: Callable[[], None]) -> Callable[[], None]:
        """`open_` as an open once the old stream is released: now, or from a signal later."""

        def run() -> None:
            try:
                open_()
            except Exception as e:
                # What played before was stopped to open this: nothing is current now.
                self._current, self._cid = None, None
                self._log_failure(f"play {target.title}", e)
                self._restore_channel()
                self._error(f"{target.title}: {e}")

        return run

    def _launch(
        self,
        target: _Target,
        url: str,
        launcher: Optional[Callable[[str], tuple[bool, str]]] = None,
    ) -> None:
        """Hand the link to the external player (the in-app stream is released by now).

        `launcher` is the context menu's choice; by default, the settings' player.
        """
        launcher = launcher or self._external_launcher()
        if launcher is None:  # the setting changed meanwhile: play it here
            self._play_link(replace(target, zapped=False), url)
            return
        ok, message = launcher(url)
        if not ok:
            first, _, rest = (message or "").strip().partition("\n")
            if rest.strip():
                logger.info("External player: %s", scrub(rest))
            self._restore_channel()
            self._error(first.strip() or "The external player could not start")
            return
        self._current, self._cid = target, None
        self._delivered(target, url)

    def _delivered(self, target: _Target, url: str) -> None:
        """The target is playing (or opening): confirm the zap and record it in the history."""
        if target.live:
            self.zap.tuned(target.item)
        try:
            self.watch.record_play(
                self.catalog.current_provider_name(),
                target.item,
                item_type=target.item_type,
                link=url,
                group_id=target.group_key if target.live else None,
            )
        except Exception as e:
            self._log_failure(f"record {target.title} in the watch history", e)

    def _launch_settled(self) -> None:
        """The zapping settled in external-player mode: launch the channel it ended on."""
        pending, self._external_pending = self._external_pending, None
        if pending is not None and not self._closed:
            target, url = pending
            self._play_link(replace(target, zapped=False), url)

    def _cancel_external(self) -> None:
        self._external_settle.stop()
        self._external_pending = None

    # ---- live pipeline ------------------------------------------------------------------------

    def _through_pipeline(self, url: str, kind: PlayKind) -> bool:
        """Whether a live link plays through the time-shift pipeline.

        Its source reads http(s) only; container files listed as channels (M3U) play as files.
        """
        if self.live is None or kind is PlayKind.VOD_FILE:
            return False
        return urlsplit(url).scheme.lower() in _PIPELINE_SCHEMES

    def _limit(self) -> Optional[int]:
        """The provider's simultaneous streams: None when unlimited or unknown."""
        return self.budget.limit_for(
            self.catalog.current_provider(), self.catalog.max_connections()
        )

    def _limit_text(self, limit: Optional[int]) -> str:
        return f"Your provider allows {limit} streams at a time" if limit else LIMITED_TEXT

    def _tune_pipeline(self, target: _Target, url: str) -> bool:
        """Ask for a budget slot for `target` (spec §6.9); it starts once the old one is released.

        False when the provider's limit is taken by other streams (Phase 4: picture in
        picture): the toast says so and the running channel goes on.
        """
        name = self.catalog.current_provider_name()
        limit = self._limit()
        if self._tuner.refused(name, limit):
            logger.info("Not opening %s: the provider allows %s streams", target.title, limit)
            self._restore_channel()
            self._toast(self._limit_text(limit), "error", ERROR_MS)
            return False
        provider = self.catalog.current_provider()
        user_agent = target.item.get("user_agent") or DEFAULT_USER_AGENT
        verify_ssl = bool(provider.get("ssl_verify", self.config.ssl_verify))
        self._tuner.tune(LiveStart(url, user_agent, verify_ssl), name, limit)
        return True

    def _pipeline_live(self) -> bool:
        """The current channel plays from the time-shift buffer."""
        current = self._current
        return current is not None and current.live and self._tuner.pipeline()

    def _on_live_start_failed(self, message: str) -> None:
        if not self._closed:
            title = self._current.title if self._current is not None else ""
            self._error(f"{title}: {message}" if title else message)

    def _on_live_mode(self, _mode: str) -> None:
        current = self._current
        if self._closed or current is None or not current.live:
            return
        if self.overlays.transport.isVisible():
            self._show_live_transport(current)  # the time-shift bar, or plain live

    def _on_live_timeline(self, state: dict) -> None:
        current = self._current
        if self._closed or not self._tuner.running() or current is None or not current.live:
            return
        transport = self.overlays.transport
        if transport.isVisible():
            self._ticks = self._programme_ticks(current.item, state)
        transport.update_timeline(state, self._ticks)

    def _programme_ticks(self, channel: dict, state: dict) -> list[tuple[float, str]]:
        """Programme starts in timeline seconds: the live edge is now (as the transport)."""
        now = self._now()
        edge = float(state["live_edge"])
        since = now - timedelta(seconds=max(0.0, edge - float(state["start"])))
        return [
            (edge - (now - p.start).total_seconds(), p.title)
            for p in self._programmes(channel, since, TICK_PROGRAMMES)
        ]

    def _on_live_notice(self, text: str) -> None:
        if self._closed:
            return
        if text == UNAVAILABLE:  # once per channel and run
            current = self._current
            item = current.item if current is not None else {}
            channel = str(item.get("cmd") or item.get("id") or item.get("name") or "")
            key = (self.catalog.current_provider_name(), channel)
            if key in self._unavailable_noticed:
                return
            if self._may_retry(current):
                # An expired STB token fails the pipeline too: say it only once the direct
                # play works, so a link made again does not use up the notice.
                self._held_notice = key
                return
            self._unavailable_noticed.add(key)
        self._toast(text)

    def _show_held_notice(self) -> None:
        key, self._held_notice = self._held_notice, None
        if key is not None and key not in self._unavailable_noticed:
            self._unavailable_noticed.add(key)
            self._toast(UNAVAILABLE)

    def _may_retry(self, current: Optional[_Target]) -> bool:
        """An STB channel's link may be made again once per tune (its play token expires)."""
        return (
            current is not None
            and current.live
            and not current.retried
            and self.catalog.provider_type() == "STB"
            and self._inflight is None
            and self._queued is None
        )

    def _retry(self, current: _Target) -> None:
        logger.info("Making the link of %s again after a playback failure", current.title)
        self._held_notice = None
        self._stop_tune()  # it failed: its slot goes back, also if the new link does not come
        self._request(replace(current, retried=True))

    def _stop_tune(self) -> None:
        self._tuner.stop()
        self._ticks = []

    def _on_live_failed(self, message: str) -> None:
        """Playback of the live channel failed for good (LiveSession recovered what it could)."""
        if self._closed:
            return
        current = self._current
        if current is not None and self._may_retry(current):
            self._retry(current)  # STB play tokens expire: a fresh link before the error
            return
        title = current.title if current is not None else ""
        if message in REFUSALS:
            self._stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            return
        self._error(f"{title}: {message}" if title else message)

    def _refusal_text(self, message: str) -> str:
        limit = self._limit()
        if message != STREAM_REFUSED:
            return self._limit_text(limit)
        if limit:
            return f"{REFUSED_TEXT} allow only {limit} streams at a time"
        return f"{REFUSED_TEXT} limit concurrent streams"

    def _seek_relative_ms(self, delta_ms: int) -> None:
        if self._pipeline_live() and self.live is not None:
            self.live.seek_relative(delta_ms / 1000.0)
        else:
            self.session.seek_relative(delta_ms)

    def _seek_to(self, seconds: float) -> None:
        if self._pipeline_live() and self.live is not None:
            self.live.seek_to(seconds)

    def _set_speed(self, rate: float) -> None:
        if self._pipeline_live() and self.live is not None:
            self.live.set_speed(rate)
        else:
            self.session.set_rate(rate)

    def _go_live(self) -> None:
        current = self._current
        if current is not None and current.live and self._tuner.go_live():
            self._show_transport()

    def _programme_start(self) -> None:
        """⏮: the start of the programme being watched if buffered, else the oldest point."""
        current, state = self._current, self._tuner.timeline
        if not self._pipeline_live() or self.live is None or current is None or state is None:
            return
        now = self._now()
        edge, oldest = float(state["live_edge"]), float(state["start"])
        watching = now - timedelta(seconds=max(0.0, edge - float(state["position"])))
        target = oldest
        for programme in self._programmes(current.item, watching, 1):
            start = edge - (now - programme.start).total_seconds()
            if programme.is_on(watching) and start >= oldest:
                target = start
        self.live.seek_to(target)
        self._show_transport()

    def _reset_speed(self) -> None:
        self.session.set_rate(1.0)
        self.overlays.transport.set_speed(1.0)

    def _external_launcher(self) -> Optional[Callable[[str], tuple[bool, str]]]:
        launcher: Optional[Callable[[str], tuple[bool, str]]] = None
        if self.config.play_in_vlc:
            launcher = self.external.launch_vlc
        elif self.config.play_in_mpv:
            launcher = self.external.launch_mpv
        return launcher

    # ---- catalog events -----------------------------------------------------------------------

    def _on_provider_ready(self, name: str) -> None:
        if self._closed:
            return
        # A provider setup makes the catalog drop link results requested before it, so a
        # request still in flight may never settle: ask again for the newest target, unless
        # the provider changed and its channels belong to the old one.
        pending = self._queued or (self._inflight[1] if self._inflight is not None else None)
        if self._is_stay(pending):
            pending = None  # the playing channel was the newest choice: nothing to ask again
        self._drop_links()
        if self._provider is not None and name != self._provider:
            self.zap.reset()
            self._channel = None
            pending = None
            self._drop_next()
            self.overlays.guide.close_guide()  # its channels are the old provider's
        self._provider = name
        if pending is not None:
            self._request(pending)
        record = self.config.last_live_channel
        item: Optional[dict] = None
        group = ALL_GROUP_ID
        if (
            isinstance(record, dict)
            and record.get("provider_name") == name
            and isinstance(record.get("item"), dict)
        ):
            item, group = record["item"], str(record.get("group_id") or ALL_GROUP_ID)
        idle = pending is None and not self._has_media()
        wanted = item is not None and self.config.start_with_last_channel and idle
        self._autotune = (item, group) if wanted and item is not None else None
        self._await_live = True
        if item is not None:
            self.nav.reset_to("live", group)
        else:
            self.nav.reset_to("live")

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed:
            return
        self._drop_links()
        self._end_loading()
        self._error(f"{name}: {message}" if name else message)
        # The sources, on the failed one, with a row to connect again: never a black screen.
        if self._has_media() and not self.overlays.drawer.isVisible():
            return  # the saved catalog still plays; the drawer offers the retry when opened
        self.nav.focus_source(name or self.catalog.current_provider_name())
        self.overlays.drawer.open_drawer(focus_side="left")

    def _on_provider_requested(self, name: str) -> None:
        """A source chosen in the drawer started a setup."""
        if not self._closed:
            self._show_loading(name)

    def _on_section_ready(self, section: str) -> None:
        if self._closed or section != "live" or not self._await_live:
            return
        self._await_live = False
        self._end_loading()
        autotune, self._autotune = self._autotune, None
        if autotune is not None and self._tune_last(*autotune):
            return
        self._open_drawer_if_idle()

    def _on_section_failed(self, section: str, message: str) -> None:
        if self._closed or section != "live" or not self._await_live:
            return
        if message == PROVIDER_LOADING_MESSAGE:
            return  # a setup is running; its providerReady arms this again
        if self.catalog.groups("live"):
            # NavModel keeps showing the saved catalog and posts a notice (toasted).
            self._on_section_ready(section)
            return
        self._await_live = False
        self._autotune = None
        self._end_loading()
        self._error(f"Live TV: {message}")
        self._open_drawer_if_idle()  # it shows the retry row

    def _on_epg_ready(self) -> None:
        if not self._closed:
            self._repaint()
            if self.overlays.guide.isVisible():
                self.overlays.guide.invalidate()

    def _on_notice(self, text: str) -> None:
        if not self._closed:
            self._toast(text)

    def _open_drawer_if_idle(self) -> None:
        drawer = self.overlays.drawer
        busy = self._has_media() or self._inflight is not None or self._queued is not None
        if not busy and not drawer.isVisible():
            drawer.open_drawer()

    def _tune_last(self, item: dict, group: str) -> bool:
        for key in dict.fromkeys((group, ALL_GROUP_ID)):
            channels = self._group_channels(key)
            found = next((c for c in channels if ZapContext.same(c, item)), None)
            if found is not None:
                self.tune(found, group_key=key, channels=channels)
                return True
        logger.info("The last channel %r is no longer listed", item.get("name"))
        return False

    def _group_channels(self, group_key: str) -> list[dict]:
        if group_key in _PINNED_TITLES:
            nodes = self.nav.children(NavNode("group", group_key, group_key)) or []
            return [n.payload for n in nodes if isinstance(n.payload, dict)]
        return list(self.catalog.items("live", group_key) or [])

    def _group_title(self, group_key: str) -> str:
        if group_key in _PINNED_TITLES:
            return _PINNED_TITLES[group_key]
        for group in self.catalog.groups("live"):
            if group.id == group_key:
                return str(group.title)
        return "All channels" if group_key == ALL_GROUP_ID else group_key

    # ---- zap events ---------------------------------------------------------------------------

    def _on_zap_tune(self, channel: dict) -> None:
        context = self.zap.context
        self._tune(channel, context.group_key, context.channels, from_zap=True)

    def _on_column_requested(self, open_: bool) -> None:
        if self._closed:
            return  # zap timers may fire while the window is torn down
        column = self.overlays.column
        if open_:
            context = self.zap.context
            column.open(
                self._group_title(context.group_key), context.channels, context.current_index
            )
        else:
            column.close()

    def _on_highlight(self, channel: dict, index: int) -> None:
        if not self._closed:
            self.overlays.column.highlight(index)
            if self._external_pending is not None:
                self._external_settle.start(EXTERNAL_SETTLE_MS)  # still zapping

    def _on_digits(self, text: str) -> None:
        self._digits = text
        if not self._closed:
            if text:
                self.overlays.transport.dismiss()
            self.overlays.banner.show_digits(text)

    def _on_number_not_found(self, number: str) -> None:
        if not self._closed:
            self._toast(f"No channel {number}")

    def _on_column_row(self, row: int) -> None:
        context = self.zap.context
        if 0 <= row < len(context.channels):
            self.tune(context.channels[row], group_key=context.group_key, channels=context.channels)
        else:
            self.overlays.column.close()

    # ---- session events -----------------------------------------------------------------------

    def _on_state(self, state: str) -> None:
        if self._closed:
            return
        self._state = state
        if state in ("paused", "playing"):
            self.overlays.transport.set_paused(state == "paused")
        if state == "playing" and self._held_notice is not None:
            self._show_held_notice()  # the channel plays, directly: time-shift is not there

    def _on_position(self, position_ms: int, length_ms: int) -> None:
        if self._closed or self._cid is None or self._current is None or self._current.live:
            return
        try:
            self.watch.update_position(self._cid, position_ms, length_ms)
        except Exception as e:
            self._log_failure("save the playback position", e)
        self.overlays.transport.update_position(position_ms, length_ms)

    def _on_error(self, message: str) -> None:
        if self._closed or self._tuner.running():
            return  # LiveSession recovers pipeline errors and reports the rest (failed)
        title = self._current.title if self._current is not None else ""
        self._error(f"{title}: {message}" if title else message)

    def _on_ended(self) -> None:
        if self._closed or self._current is None:
            return
        current = self._current
        if current.live:
            if self._pipeline_live():
                return  # the buffer reopens
            if self._may_retry(current):
                self._retry(current)  # an expired STB token can end a direct stream at once
                return
            self._toast("The stream ended")
            return
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        if self._autoplays(current):
            self._advance(current, play_now=False)
        else:
            self._toast(FINISHED_TEXT)

    # ---- autoplay and Next --------------------------------------------------------------------

    @staticmethod
    def _has_next(target: _Target) -> bool:
        """Whether Next can follow `target`: an episode of a known season, a genre's movie."""
        context = target.context or {}
        if target.section == "series":
            return isinstance(context.get("series"), dict) and isinstance(
                context.get("season"), dict
            )
        return target.section == "movies" and bool(context.get("genre_items"))

    def _autoplays(self, target: _Target) -> bool:
        """Autoplay follows a movie or episode that ended, as the settings allow."""
        if self._external_launcher() is not None or not self._has_next(target):
            return False  # external players never autoplay (as in v1.13.8)
        if self._inflight is not None or self._queued is not None:
            return False  # something else was chosen and is on its way
        if target.section == "series":
            return bool(self.config.auto_play_episodes)
        return bool(self.config.auto_play_movies)

    def _following(self, target: _Target) -> Union[NextUp, NeedEpisodes, None]:
        """What follows `target`: the next episode (maybe in a season still to load) or title."""
        context = target.context or {}
        if target.section == "series":
            series, season = context.get("series"), context.get("season")
            if not isinstance(series, dict) or not isinstance(season, dict):
                return None
            return next_episode(
                self.catalog.seasons(series) or [season],
                lambda s: self.catalog.episodes(series, s),
                season,
                target.item,
                season_key=item_key,
                episode_key=episode_key,
            )
        following = next_movie(list(context.get("genre_items") or []), target.item, key=item_key)
        return None if following is None else NextUp(None, following)

    def _advance(self, target: _Target, *, play_now: bool, loaded: str = "") -> None:
        """Offer (or with `play_now` play) what follows `target`.

        A next season that is not loaded yet is requested first; `loaded` is the season whose
        episodes just arrived, so a season that still has none does not loop.
        """
        result = self._following(target)
        if isinstance(result, NeedEpisodes):
            series = (target.context or {}).get("series") or {}
            season_id = str(result.season.get("id"))
            if season_id == loaded:  # loaded, yet the catalog still has no episodes for it
                logger.info("Season %s has no episodes after loading them", season_id)
                self._toast(NEXT_SEASON_FAILED_TEXT, "error", ERROR_MS)
                return
            self._awaiting = _Awaiting(target, play_now, str(series.get("id")), season_id)
            self.catalog.request_episodes(series, result.season)
            return
        if result is None:
            self._toast(NOTHING_NEXT_TEXT if play_now else FINISHED_TEXT)
            return
        context = {**(target.context or {}), "start_over": False}
        kind = "episode" if target.section == "series" else "movie"
        if kind == "episode":
            context["season"] = result.season
            context["episodes"] = self.catalog.episodes(context["series"], result.season)
        if play_now:
            self.play_vod(result.item, kind, context)
            return
        self._next = (result.item, kind, context)
        subtitle = self._next_subtitle(result, kind)
        self.overlays.next_card.show_next(
            _title(result.item, episode=kind == "episode"), subtitle, kind=kind
        )

    def _next_subtitle(self, result: NextUp, kind: str) -> str:
        """The Next card's second line: season and episode ("S2 · E1"), or a movie's genre."""
        item = result.item
        if kind == "movie":
            category = str(item.get("category_id") or item.get("tv_genre_id") or "")
            for group in self.catalog.groups("movies") if category else []:
                if group.id == category:
                    return str(group.title)
            return str(item.get("genre") or item.get("genres_str") or "")
        season = (result.season or {}).get("number") or item.get("season")
        number = item.get("number") or item.get("episode_num") or item.get("series")
        parts = [f"{tag}{value}" for tag, value in (("S", season), ("E", number)) if value]
        return " · ".join(parts)

    def _on_episodes_ready(self, series_id: str, season_id: str) -> None:
        awaiting = self._awaiting
        if self._closed or awaiting is None:
            return
        if (awaiting.series_id, awaiting.season_id) != (series_id, season_id):
            return
        self._awaiting = None
        if self._still_current(awaiting.target):
            self._advance(awaiting.target, play_now=awaiting.play_now, loaded=season_id)

    def _on_episodes_failed(self, series_id: str, season_id: str, _message: str) -> None:
        awaiting = self._awaiting
        if self._closed or awaiting is None:
            return
        if (awaiting.series_id, awaiting.season_id) != (series_id, season_id):
            return
        self._awaiting = None
        if self._still_current(awaiting.target):
            self._toast(NEXT_SEASON_FAILED_TEXT, "error", ERROR_MS)

    def _still_current(self, target: _Target) -> bool:
        """Nothing else was chosen since `target` played."""
        return target is self._current and self._inflight is None and self._queued is None

    def _on_play_now(self) -> None:
        offered, self._next = self._next, None
        if offered is not None and not self._closed:
            self.play_vod(*offered)

    def _on_next_cancelled(self) -> None:
        if self._closed or self._next is None:
            return  # dropped by a newer choice (see _drop_next)
        self._next = None
        self.overlays.transport.poke()  # the final frame stays, with the transport

    def _on_next_requested(self) -> None:
        """The transport's Next: what follows plays at once, without the card."""
        current = self._current
        if self._closed or current is None or not self._has_next(current):
            return
        self._drop_next()
        self._advance(current, play_now=True)

    def _drop_next(self) -> None:
        """Forget the offer and a wait for episodes; the card closes without a reply."""
        self._awaiting = None
        if self._next is not None:
            self._next = None
            self.overlays.next_card.escape()

    # ---- tracks -------------------------------------------------------------------------------

    def _cycle_audio(self) -> None:
        tracks = [t for t in self.session.audio_tracks() if t[0] != -1]  # mute disables audio
        if not tracks:
            self._toast("No audio tracks")
            return
        current = self._audio_id if self._audio_id is not None else tracks[0][0]
        track_id, name = self._next_track(tracks, current)
        self.session.set_audio_track(track_id)
        self._audio_id = track_id
        self._toast(f"Audio: {name}")

    def _cycle_subtitles(self) -> None:
        tracks = self.session.subtitle_tracks()
        if not tracks:
            self._toast("No subtitles")
            return
        current = self._subtitle_id if self._subtitle_id is not None else -1
        track_id, name = self._next_track(tracks, current)
        self.session.set_subtitle_track(track_id)
        self._subtitle_id = track_id
        self._toast(f"Subtitles: {'Off' if track_id == -1 else name}")

    @staticmethod
    def _next_track(tracks: list[tuple[int, str]], current: int) -> tuple[int, str]:
        ids = [t[0] for t in tracks]
        index = ids.index(current) if current in ids else -1
        return tracks[(index + 1) % len(tracks)]

    # ---- overlays -----------------------------------------------------------------------------

    def now_title(self, channel: dict) -> Optional[tuple[str, float]]:
        now = self._now()
        programmes = self._programmes(channel, now, 1)
        if programmes and programmes[0].is_on(now):
            return programmes[0].title, programmes[0].progress(now)
        return None

    def banner_info(self, channel: dict) -> BannerInfo:
        now = self._now()
        programmes = self._programmes(channel, now, 4)
        current, upcoming = now_next(programmes, now)
        later = [p for p in programmes if upcoming is not None and p.start > upcoming.start]
        return BannerInfo(
            number=self._number(channel),
            name=str(channel.get("name") or ""),
            logo=self._logo(channel),
            now=current,
            next=upcoming,
            upcoming=later,
            is_live=True,
            clock=now,
        )

    def _programmes(self, channel: dict, now: datetime, limit: int) -> list[Programme]:
        try:
            return list(self.catalog.programmes(channel, now, limit=limit))
        except Exception as e:
            self._log_failure(f"look up the guide for {channel.get('name')!r}", e)
            return []

    def _number(self, channel: dict) -> str:
        number = ZapContext.number_of(channel)
        if number:
            return number
        context = self.zap.context
        index = context.index_of(channel)
        # As ZapContext.by_number: list positions stand in only when nothing is numbered.
        if index >= 0 and not any(ZapContext.number_of(c) for c in context.channels):
            return str(index + 1)
        return ""

    def _logo(self, channel: dict) -> Optional[QPixmap]:
        url = channel.get("logo") or ""
        if not url or self._logos is None:
            return None
        pixmap: Optional[QPixmap] = self._logos.pixmap(url)
        if pixmap is None:
            self._logos.request([url])
        return pixmap

    def _on_logo_ready(self, url: str) -> None:
        """Refresh a visible banner when its channel's logo arrives."""
        banner, channel = self.overlays.banner, self._channel
        if self._closed or channel is None or channel.get("logo") != url:
            return
        if banner.isVisible() and banner.info is not None and banner.info.logo is None:
            banner.show_info(self.banner_info(channel))

    def _open_drawer(self) -> None:
        if not self.catalog.provider_names():
            self._welcome()
            return
        self.nav.reset_to("live", self.zap.context.group_key or ALL_GROUP_ID)
        channel = self._channel or self.zap.context.current()
        self.overlays.drawer.open_drawer(focus_key=item_key(channel) if channel else None)

    def _open_guide(self) -> None:
        """The guide over live TV: the zap group's channels, on the one tuned last."""
        if self._playing_vod():
            return  # it lists live channels
        context = self.zap.context
        group, channels = context.group_key, list(context.channels)
        if not channels:
            group, channels = ALL_GROUP_ID, self._all_live()
        if not channels:
            return
        channel = self._channel or context.current()
        index = next((i for i, c in enumerate(channels) if ZapContext.same(c, channel)), 0)
        self._guide_channels = (group, channels)
        if self._digits:
            self.zap.cancel_digits()
        self.overlays.column.close()
        self.overlays.banner.dismiss()
        self.overlays.transport.dismiss()
        self.overlays.guide.open_guide(channels, index)

    def _guide_key(self, action: Action) -> bool:
        """Route a key to the open guide; False when the usual handling should follow."""
        guide = self.overlays.guide
        moves: dict[Action, tuple[Callable[[int], None], int]] = {
            Action.NAV_UP: (guide.move_by, -1),
            Action.NAV_DOWN: (guide.move_by, 1),
            Action.NAV_PAGE_UP: (guide.page, -1),
            Action.NAV_PAGE_DOWN: (guide.page, 1),
            Action.NAV_LEFT: (guide.shift, -1),
            Action.NAV_RIGHT: (guide.shift, 1),
        }
        if action in moves:
            move, delta = moves[action]
            move(delta)
        elif action is Action.NAV_ACTIVATE:
            guide.activate()
        elif action is Action.ESCAPE:
            guide.escape()
        elif action is Action.GUIDE:
            guide.close_guide()
        elif action in _GUIDE_CLOSERS:
            guide.close_guide()
            return False
        elif action not in _GUIDE_IGNORED:
            return False  # playback, volume, fullscreen and favorites work over the guide
        return True

    def _on_guide_tune(self, channel: dict) -> None:
        if self._closed:
            return
        group, channels = self._guide_channels
        self.overlays.guide.close_guide()
        self.tune(channel, group_key=group, channels=channels)

    def _escape(self) -> None:
        overlays = self.overlays
        if overlays.drawer.isVisible():
            overlays.drawer.escape()  # clears the filter first, then closes
        elif overlays.next_card.isVisible():
            overlays.next_card.escape()  # stays on the final frame
        elif self._digits:
            self.zap.cancel_digits()
            overlays.banner.dismiss()
        elif overlays.column.isVisible():
            overlays.column.close()  # the zap forgets its highlight, so no rest tune follows
        elif overlays.banner.isVisible():
            overlays.banner.dismiss()
        else:
            self.window_actions.leave_fullscreen()

    def _info(self) -> None:
        if self._playing_vod():
            self._show_transport()
            return
        channel = self._channel or self.zap.context.current()
        if channel is not None:
            self._show_banner(channel)

    def _show_banner(self, channel: dict) -> None:
        """Announce `channel`; the banner and the transport share the bottom of the video."""
        self.overlays.transport.dismiss()
        self.overlays.banner.show_info(self.banner_info(channel))

    def _seek(self, delta_ms: int) -> None:
        current = self._current
        if self.session.is_seekable():
            self.session.seek_relative(delta_ms)
            self._show_transport()
        elif self._tuner.opening():
            return  # it still opens: the time-shift may yet come
        elif self.live is not None and current is not None and current.live:
            self._toast(UNAVAILABLE)  # played directly: there is no buffer to seek in
        else:
            self._toast("Rewind arrives with time-shift")

    def _show_transport(self) -> None:
        target = self._current
        if target is None or not self._has_media() or self.overlays.drawer.isVisible():
            return
        if self._digits:
            return  # the banner shows the number being typed
        self.overlays.banner.dismiss()
        if target.live and not self.overlays.transport.isVisible():
            self._show_live_transport(target)
        else:
            self.overlays.transport.poke()

    def _show_live_transport(self, target: _Target) -> None:
        now = self.now_title(target.item)
        subtitle = now[0] if now else ""
        transport = self.overlays.transport
        if not self._pipeline_live():
            transport.show_live(target.title, subtitle)
            return
        transport.show_timeshift(target.title, subtitle)
        timeline = self._tuner.timeline
        if timeline is not None:
            self._ticks = self._programme_ticks(target.item, timeline)
            transport.update_timeline(timeline, self._ticks)

    def _change_volume(self, delta: int) -> None:
        volume = max(0, min(100, self.session.volume() + delta))
        self.session.set_volume(volume)
        self._toast(f"Volume {volume}%")

    # ---- favorites and context actions --------------------------------------------------------

    def _is_live_item(self, node: Optional[NavNode]) -> bool:
        return (
            node is not None
            and node.layer == "item"
            and isinstance(node.payload, dict)
            and self.nav.section_of(node) == "live"
        )

    def _favorite(self) -> None:
        """Ctrl+D: the drawer's highlighted channel while it is open, else the current one."""
        drawer = self.overlays.drawer
        if drawer.isVisible():
            node = drawer.highlighted()
            if node is not None and self._is_live_item(node):
                self._toggle_favorite(node.payload)
            return
        channel = None if self._playing_vod() else self._channel or self.zap.context.current()
        if channel is not None:
            self._toggle_favorite(channel)

    def _toggle_favorite(self, item: dict) -> None:
        """Favorites are channel names, as NavModel and the Library keep them."""
        name = item.get("name")
        if not name:
            return
        favorites = list(self.config.favorites or [])
        added = name not in favorites
        self.config.favorites = [*favorites, name] if added else [f for f in favorites if f != name]
        self.nav.changed.emit()  # the pinned Favorites group follows the list
        try:
            self.config.save_config()
        except Exception as e:
            self._log_failure("save the favorites", e)
            self._toast(FAVORITES_SAVE_FAILED_TEXT, "error", ERROR_MS)
            return
        self._toast("Added to favorites" if added else "Removed from favorites")

    def _on_context(self, node: NavNode, global_pos: QPoint) -> None:
        """A right-click on a drawer row: its context menu, built now."""
        if self._closed:
            return
        menu = self._context_menu(node)
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(global_pos)

    def _context_menu(self, node: NavNode) -> Optional[QMenu]:
        live = self._is_live_item(node)
        playable = isinstance(node.payload, dict) and self.nav.is_playable(node)
        if not live and not playable:
            return None  # e.g. a series: nothing to play or keep
        drawer = self.overlays.drawer
        menu = QMenu(drawer if isinstance(drawer, QWidget) else None)
        menu.setStyleSheet(_menu_style())
        item: dict = node.payload
        if live:
            kept = item.get("name") in (self.config.favorites or [])
            text = "Remove from favorites" if kept else "Add to favorites"
            menu.addAction(text).triggered.connect(lambda _=False: self._toggle_favorite(item))
        if playable:
            if live:
                menu.addSeparator()
            # Its section and group as the drawer shows them now, not when the link lands.
            group = next((n.key for n in reversed(self.nav.path) if n.layer == "group"), "")
            section = self.nav.section_of(node) or "live"
            target = _Target(item, section, node.title, group_key=group)
            for text, launcher in (
                ("Play in VLC", self.external.launch_vlc),
                ("Play in MPV", self.external.launch_mpv),
            ):
                menu.addAction(text).triggered.connect(
                    lambda _=False, run=launcher: self._link_for(
                        target, lambda url: self._play_external(target, url, run)
                    )
                )
            menu.addSeparator()
            menu.addAction("Copy stream link").triggered.connect(
                lambda _=False: self._link_for(target, self._copy_link)
            )
        return menu

    def _link_for(self, target: _Target, then: Callable[[str], None]) -> None:
        """Request `target`'s link beside the tune's; requests go one at a time."""
        if not self._closed:
            self._side_queue.append(_LinkJob(target, then))
            self._next_side_link()

    def _next_side_link(self) -> None:
        if self._closed or self._side is not None or not self._side_queue:
            return
        job = self._side_queue.popleft()
        section = job.target.section
        request_id = self.catalog.request_link(
            job.target.item, section=section, is_episode=section == "series"
        )
        self._side = (request_id, job)

    def _take_side(self, request_id: int) -> Optional[_LinkJob]:
        side = self._side
        if self._closed or side is None or side[0] != request_id:
            return None
        self._side = None
        return side[1]

    def _copy_link(self, url: str) -> None:
        QGuiApplication.clipboard().setText(url)
        self._toast("Link copied")

    def _play_external(
        self, target: _Target, url: str, launcher: Callable[[str], tuple[bool, str]]
    ) -> None:
        """The context menu's external play: the newest choice, once the in-app stream is gone."""
        if self._closed:
            return
        self._cancel_external()
        self._drop_next()
        self._inflight = self._queued = None  # a tune still on its way would open in-app
        self._tuner.open(self._guarded(target, lambda: self._launch(target, url, launcher)))

    # ---- state helpers ------------------------------------------------------------------------

    def _has_media(self) -> bool:
        return self._state in _ACTIVE_STATES or self._tuner.opening()

    def _playing_live(self) -> Optional[_Target]:
        current = self._current
        return current if current is not None and current.live and self._has_media() else None

    def _playing_vod(self) -> bool:
        return self._current is not None and not self._current.live and self._has_media()

    def _toast(self, text: str, kind: str = "info", ms: int = INFO_MS) -> None:
        self.overlays.toast.show_message(text, kind, ms)

    def _error(self, text: str) -> None:
        """Toast an error; addresses and query strings never reach the toast or any log."""
        clean = scrub(text)
        logger.info("Error shown: %s", clean)
        self._toast(clean, "error", ERROR_MS)

    @staticmethod
    def _log_failure(what: str, error: Exception) -> None:
        logger.warning("Could not %s: %s (%s)", what, scrub(error), type(error).__name__)
        # The frames only: the exception's text can carry a provider URL with credentials.
        logger.debug("Could not %s:\n%s", what, safe_traceback(error))

    def _welcome(self) -> None:
        """Until a provider exists (Phase 3 brings the welcome card)."""
        self._loading = False
        self.overlays.toast.set_hint(WELCOME_TEXT)

    def _setup_name(self, name: Optional[str]) -> str:
        """The provider a setup for `name` loads, as ProviderManager picks it."""
        names = self.catalog.provider_names()
        wanted = name if name is not None else getattr(self.config, "selected_provider_name", None)
        if wanted in names:
            return str(wanted)
        return names[0] if names else ""

    def _show_loading(self, name: str) -> None:
        """From a setup's start until the first live section or a failure (Review Focus 5)."""
        self._loading = True
        self.overlays.toast.set_hint(f"Loading {name}…" if name else "Loading…")

    def _end_loading(self) -> None:
        if self._loading:
            self._loading = False
            self.overlays.toast.set_hint(None)
