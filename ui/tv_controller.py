"""The TV shell's behaviour: wires the catalog, navigation, zapping and playback to the overlays.

Link requests are serialised: at most one is in flight (STB portals mint a link per request and
providers limit connections). A tune that arrives meanwhile replaces the single queued target,
and only the newest target ever plays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractScrollArea

from services import external_players
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.epg_view import Programme, now_next
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode, episode_key, item_key
from services.vlc_profile import classify
from services.zapping import ZapContext
from ui.keymap import Action, as_int
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.painting import fmt_duration
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import BannerInfo, ZapBanner
from ui.pointer import PointerInput

logger = logging.getLogger(__name__)

WELCOME_TEXT = "Add a provider in Settings to start watching"
WELCOME_MS = 8000
INFO_MS = 3000
ERROR_MS = 6000
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
_ACTIVE_STATES = frozenset({"opening", "buffering", "playing", "paused"})
_VOLUME_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.MetaModifier.value
)
_PINNED_TITLES = {FAVORITES_GROUP: "Favorites", RECENT_GROUP: "Recent"}
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE)
_QUERY = re.compile(r"\S*\?\S+")


def scrub(text: object) -> str:
    """Drop addresses and query strings from a message: they can carry credentials."""
    cleaned = _QUERY.sub("…", _ADDRESS.sub("…", str(text)))
    return " ".join(cleaned.split())


@dataclass
class Overlays:
    banner: ZapBanner
    column: ChannelColumn
    drawer: Drawer
    transport: Transport
    toast: Toast


@dataclass
class _Target:
    """Something to play once its link resolves."""

    item: dict
    section: str  # "live", "movies" or "series" (an episode)
    title: str
    group_key: str = ""
    subtitle: str = ""

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
        self._autotune: Optional[tuple[dict, str]] = None
        self._audio_id: Optional[int] = None
        self._subtitle_id: Optional[int] = None
        self._column_wheel = PointerInput(self)

    # ---- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._wire()
        if not self.catalog.provider_names():
            # ProviderManager cannot set up without providers; the setup would only fail.
            self._welcome()
            return
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
        self.nav.notice.connect(self._on_notice)
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
        transport.playPauseRequested.connect(session.toggle_pause)
        transport.seekRequested.connect(session.seek)
        transport.seekRelativeRequested.connect(session.seek_relative)
        transport.speedRequested.connect(session.set_rate)
        transport.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.audioRequested.connect(self._cycle_audio)
        transport.subtitleRequested.connect(self._cycle_subtitles)
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
        self._autotune = None
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        try:
            self.session.stop()
        except Exception as e:
            self._log_failure("stop playback", e)

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if not self.catalog.provider_names():
            self._welcome()
            return
        # Pending links are settled when the provider is ready (see _on_provider_ready).
        self.catalog.select_provider(name, force_refresh=force_refresh)

    def refresh_overlays(self) -> None:
        """After a settings change: repaint the rows and reload the guide."""
        self._repaint()
        self.catalog.refresh_epg()

    def _repaint(self) -> None:
        """Repaint what depends on settings or EPG: column rows and the open drawer."""
        self.overlays.column.refresh_now()
        self.nav.changed.emit()

    # ---- input --------------------------------------------------------------------------------

    def handle(self, action: Action, payload: str = "") -> None:
        if self._closed:
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
        elif action is Action.OPEN_DRAWER:
            if self.overlays.column.isVisible():
                self._on_column_row(self.overlays.column.current_index())  # tune it now
            else:
                self._open_drawer()
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            self._seek(-SEEK_STEP_MS if action is Action.SEEK_BACK else SEEK_STEP_MS)
        elif action is Action.PLAY_PAUSE:
            self.session.toggle_pause()
            self._show_transport()
        elif action is Action.INFO:
            self._info()
        elif action in (Action.GO_LIVE, Action.GUIDE):
            self._toast("Coming soon")
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
        context = self.zap.context
        if group_key != context.group_key or context.index_of(channel) < 0:
            # Numbers reach every live channel, not only the group's (TV numbers are global).
            self.zap.set_context(ZapContext(group_key, channels, fallback=self._all_live()))
        if not from_zap:
            self.zap.note_request(channel)  # ↑/↓ move on from it while its link is pending
        self.overlays.column.close()
        self._channel = channel
        self.overlays.banner.show_info(self.banner_info(channel))
        title = str(channel.get("name") or "")
        self._request(_Target(channel, "live", title, group_key=group_key))

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
            episode = node.layer == "episode"
            series = ""
            if episode:
                series = next((n.title for n in reversed(self.nav.path) if n.layer == "item"), "")
            drawer.close_drawer()
            self._request(
                _Target(
                    node.payload, "series" if episode else "movies", node.title, subtitle=series
                )
            )
        else:
            logger.debug("Not playable: %s", node)

    def _all_live(self) -> list[dict]:
        return list(self.catalog.items("live", ALL_GROUP_ID) or [])

    def _request(self, target: _Target) -> None:
        if self._inflight is not None:
            self._queued = target  # replaces any older queued target
            return
        request_id = self.catalog.request_link(
            target.item, section=target.section, is_episode=target.section == "series"
        )
        self._inflight = (request_id, target)

    def _drop_links(self) -> None:
        self._inflight = None
        self._queued = None

    def _take(self, request_id: int) -> Optional[_Target]:
        """The in-flight target when `request_id` is the request we are waiting for."""
        if self._closed or self._inflight is None or self._inflight[0] != request_id:
            logger.debug("Ignoring the result of link request %s", request_id)
            return None
        target = self._inflight[1]
        self._inflight = None
        return target

    def _on_link_ready(self, request_id: int, url: str) -> None:
        target = self._take(request_id)
        if target is None:
            return
        queued, self._queued = self._queued, None
        if queued is not None:
            if not queued.same_as(target):
                logger.debug(
                    "Dropping the link for %s: %s was chosen since", target.title, queued.title
                )
                self._request(queued)
                return
            target = queued  # chosen again meanwhile: this link serves the newest choice
        try:
            self._deliver(target, url)
        except Exception as e:
            self._log_failure(f"play {target.title}", e)
            self._error(f"{target.title}: {e}")

    def _on_link_failed(self, request_id: int, message: str) -> None:
        target = self._take(request_id)
        if target is None:
            return
        if message == PROVIDER_LOADING_MESSAGE:
            # A provider setup is running; providerReady asks again for the newest target.
            if self._queued is None:
                self._queued = target
            return
        queued, self._queued = self._queued, None
        if queued is not None:
            self._request(queued)  # the failed target was superseded: no error for it
            return
        current = self._current
        self._channel = current.item if current is not None and current.live else None
        self._error(f"{target.title}: {message}")

    def _deliver(self, target: _Target, url: str) -> None:
        provider = self.catalog.current_provider_name()
        item = target.item
        launcher = self._external_launcher()
        self._audio_id = self._subtitle_id = None
        if launcher is not None:
            ok, message = launcher(url)
            if not ok:
                first, _, rest = (message or "").strip().partition("\n")
                if rest.strip():
                    logger.info("External player: %s", scrub(rest))
                self._error(first.strip() or "The external player could not start")
                return
            self._current, self._cid = target, None
            if self._has_media():
                self.session.stop()  # one stream per provider connection
        elif target.live:
            self._current, self._cid = target, None
            self.session.play(
                url,
                kind=classify(url, self.catalog.is_live("live")),
                latency=self.config.live_latency,
                user_agent=item.get("user_agent"),
            )
            self._reset_speed()
            if self.overlays.transport.isVisible():
                self._show_live_transport(target)
        else:
            cid = self.watch.content_id(provider, item, item_type=target.item_type)
            start = self.watch.resume_position(cid)
            self._current, self._cid = target, cid
            self.session.play(url, kind=classify(url, False), start_ms=start)
            self._reset_speed()
            if start:
                self._toast(f"Resumed at {fmt_duration(start)}")
            self.overlays.transport.show_vod(target.title, target.subtitle)
        if target.live:
            self.zap.tuned(item)
        try:
            self.watch.record_play(
                provider,
                item,
                item_type=target.item_type,
                link=url,
                group_id=target.group_key if target.live else None,
            )
        except Exception as e:
            self._log_failure(f"record {target.title} in the watch history", e)

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
        self._drop_links()
        if self._provider is not None and name != self._provider:
            self.zap.reset()
            self._channel = None
            pending = None
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
        self._error(f"{name}: {message}" if name else message)
        self._open_drawer_if_idle()  # the sources stay reachable instead of a black screen

    def _on_section_ready(self, section: str) -> None:
        if self._closed or section != "live" or not self._await_live:
            return
        self._await_live = False
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
        self._error(f"Live TV: {message}")
        self._open_drawer_if_idle()  # it shows the retry row

    def _on_epg_ready(self) -> None:
        if not self._closed:
            self._repaint()

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

    def _on_digits(self, text: str) -> None:
        self._digits = text
        if not self._closed:
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

    def _on_position(self, position_ms: int, length_ms: int) -> None:
        if self._closed or self._cid is None or self._current is None or self._current.live:
            return
        try:
            self.watch.update_position(self._cid, position_ms, length_ms)
        except Exception as e:
            self._log_failure("save the playback position", e)
        self.overlays.transport.update_position(position_ms, length_ms)

    def _on_error(self, message: str) -> None:
        if self._closed:
            return
        title = self._current.title if self._current is not None else ""
        self._error(f"{title}: {message}" if title else message)

    def _on_ended(self) -> None:
        if self._closed or self._current is None:
            return
        if self._current.live:
            self._toast("The stream ended")
            return
        self._toast("Finished")
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)

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

    def _escape(self) -> None:
        overlays = self.overlays
        if overlays.drawer.isVisible():
            overlays.drawer.escape()  # clears the filter first, then closes
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
            self.overlays.banner.show_info(self.banner_info(channel))

    def _seek(self, delta_ms: int) -> None:
        if self.session.is_seekable():
            self.session.seek_relative(delta_ms)
            self._show_transport()
        else:
            self._toast("Rewind arrives with time-shift")

    def _show_transport(self) -> None:
        target = self._current
        if target is None or not self._has_media() or self.overlays.drawer.isVisible():
            return
        if target.live and not self.overlays.transport.isVisible():
            self._show_live_transport(target)
        else:
            self.overlays.transport.poke()

    def _show_live_transport(self, target: _Target) -> None:
        now = self.now_title(target.item)
        self.overlays.transport.show_live(target.title, now[0] if now else "")

    def _change_volume(self, delta: int) -> None:
        volume = max(0, min(100, self.session.volume() + delta))
        self.session.set_volume(volume)
        self._toast(f"Volume {volume}%")

    # ---- state helpers ------------------------------------------------------------------------

    def _has_media(self) -> bool:
        return self._state in _ACTIVE_STATES

    def _playing_vod(self) -> bool:
        return self._current is not None and not self._current.live and self._has_media()

    def _toast(self, text: str, kind: str = "info", ms: int = INFO_MS) -> None:
        self.overlays.toast.show_message(text, kind, ms)

    def _error(self, text: str) -> None:
        """Toast an error; addresses and query strings never reach the toast or INFO logs."""
        clean = scrub(text)
        logger.debug("Error (raw): %s", text)
        logger.info("Error shown: %s", clean)
        self._toast(clean, "error", ERROR_MS)

    @staticmethod
    def _log_failure(what: str, error: Exception) -> None:
        logger.warning("Could not %s: %s (%s)", what, scrub(error), type(error).__name__)
        logger.debug("Could not %s", what, exc_info=True)

    def _welcome(self) -> None:
        self._toast(WELCOME_TEXT, "info", WELCOME_MS)
