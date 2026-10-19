"""The TV shell's behaviour: wires the catalog, navigation, zapping and playback to the overlays.

Link requests are serialised in LinkLanes (ui/link_lanes.py): at most one is in flight (STB
portals mint a link per request and providers limit connections). A tune that arrives
meanwhile replaces the single queued target, and only the newest target ever plays.

Live channels on http(s) play through the time-shift pipeline (`LiveSession`) when the window
provides one. `LiveTuner` opens every provider stream (pipeline, direct, movie, external player)
only once the previous one is released. The time-shift side (pipeline tunes and their budget
slot, the transport's time-shift controls, LiveSession's reports, the STB fresh-link retry) is
LiveControls' (ui/live_controls.py).

In external-player mode a zap step launches the player only once the zapping settles.

Movies and episodes play through `play_vod` (the drawer and the Library), resuming where they
stopped; Autoplay (ui/autoplay.py) offers what follows one that ended. While a movie or an
episode is current, also once it ended, the zap keys stay on it: ↑/↓ and the wheel change the
volume, and number entry, the previous channel and the guide wait for live TV.

The guide (ui/guide_controller.py) takes the keys while it is open. The drawer's context menu
(ui/context_actions.py) asks for links on LinkLanes' side lane; its "Play in VLC/MPV" releases
the in-app stream first, as an external pick does. The transport's "Open in external player"
offers the same two plays for what plays now, and a right-click in the Library offers them
with the link (`show_library_menu`); while the Library shows, the menus' messages go to its
toast. Only this controller holds the LiveTuner and the session: the others call back
play_vod, tune or the external play.

Picture in picture (spec §7, Phase 4) is PipControls' (ui/pip_controls.py) over the window's
SessionManager: each player has its own LiveTuner, and `session`, `live` and the tuner here
are always the large picture's, so zapping, the banner, the transport, seek and Go live act
on it. A swap re-points them (`_use_slot`) and trades what each picture showed (its target
and zap group); nothing is stopped or restarted. A zap onto the channel the small picture
shows swaps instead of opening it twice. A movie or an episode started from the Library while
a channel plays takes the large picture and the channel goes small without reopening (a
previous small picture closes first; when the title's provider has no second stream the
channel closes instead, and a small picture plays on), and takes it back if the title's link
or its playback fails; what follows the title (Next, autoplay, another title) keeps the
channel small. A swap drops the Next card, and a title that ended small offers it once
swapped large. Files and a title with no channel streaming close the small picture first: it
is for live TV.

A failure to open a channel or a film, or to reach a provider, is toasted at once; with
`account_checks` (services/account_check.py) the provider's account is then asked why, opening
no stream (at most once a minute per provider), and a plain reason (expired, the connection
limit, a wrong password, a refusal) replaces the toast while it still shows (`_explain`).

Provider setups (the loading and welcome hints, the last channel, the drawer on an idle player)
are ProviderFlow's (ui/provider_flow.py). Opening the window's Library (spec §5.4) keeps what
plays going in a corner of it (`enter_library_playing`; `stop_for_library` is the corner's ×, or
nothing playing); back to live TV (`resume_live`) tunes the last channel when nothing plays. The
window wires this controller before the Library's: a Library play that waited for its provider
runs from the Library's providerReady slot, after this controller settled the provider change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
import logging
from pathlib import Path
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractScrollArea

from services import external_players, favorites
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme, now_next
from services.failure_reasons import LIMIT, Reason, own_streams_text
from services.live.live_session import LiveSession
from services.live.tuning import channel_key
from services.navigation import (
    FAVORITES_GROUP,
    RECENT_GROUP,
    NavNode,
    item_key,
    item_title,
    payloads,
)
from services.next_episode import play_context
from services.provider_ids import ALL_PROVIDERS, mixed_group_id, provider_of, split_group_id
from services.redact import safe_traceback, scrub
from services.sessions import SessionManager, Slot
from services.sound_sync import SoundSync
from services.vlc_profile import classify
from services.zapping import ZapContext
from ui import item_provider
from ui.autoplay import FINISHED_TEXT, Autoplay
from ui.context_actions import ContextActions, Launcher, is_channel
from ui.grid_controls import LEAVE_FIRST, GridControls
from ui.guide_controller import GuideController
from ui.keymap import Action, as_int
from ui.link_lanes import LinkLanes, Target
from ui.live_controls import LiveControls, stream_options
from ui.live_tuner import LiveTuner, start_kwargs as tune_kwargs
from ui.mixer_controls import MixerControls
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.next_card import NextCard
from ui.overlays.painting import fmt_duration
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import BannerInfo, ZapBanner
from ui.pip_controls import PipControls
from ui.pointer import PointerInput
from ui.provider_flow import ProviderFlow
from ui.subtitle_search import OnlineSetup, OnlineSubtitles
from ui.track_controls import TrackControls

logger = logging.getLogger(__name__)

INFO_MS = 3000
ERROR_MS = 6000
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
TUNING_KEEP_MS = 3000  # under the banner's 4 s: it is shown again while a link resolves
TUNING_CAP_S = 20.0  # and while the channel opens, up to this long after the tune
EXTERNAL_SETTLE_MS = 1000  # the zap burst window: an external player opens once zapping stops
FAVORITES_SAVE_FAILED_TEXT = "Couldn't save your favorites; they will be lost when QiTV closes"
_ACTIVE_STATES = frozenset({"opening", "buffering", "playing", "paused"})
_ON_DEMAND_STOPS = frozenset({"ended", "error"})  # a movie or episode still current after them
_VOLUME_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.MetaModifier.value
)
_DRAWER_ROWS = 40  # rows of a mixed list whose providers load their guides
MULTIVIEW_TITLE = "Multiview"  # the controls' title over a focused tile with no channel
_PINNED_TITLES = {FAVORITES_GROUP: "Favorites", RECENT_GROUP: "Recent"}


@dataclass
class Overlays:
    banner: ZapBanner
    column: ChannelColumn
    drawer: Drawer
    transport: Transport
    toast: Toast
    guide: Guide
    next_card: NextCard
    welcome: WelcomeCard
    pip_label: Any = None  # the PipLabel (Phase 4), with picture in picture only
    mixer: Any = None  # the volume mixer (Phase 4)
    sound_panel: Any = None  # the Sound panel above ♫ (2.2)
    subtitle_panel: Any = None  # the Subtitles panel above its button (2.2)
    subtitle_search: Any = None  # its OpenSubtitles search page (2.2)
    library_toast: Any = None  # the Library page's toast: the menus' messages while it shows


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
        sessions: Optional[SessionManager] = None,
        pip_frame=None,
        sound_sync: Optional[SoundSync] = None,
        pick_subtitle_file: Optional[Callable[[str], str]] = None,
        account_checks=None,
        online_subtitles: Optional[OnlineSetup] = None,
        grid_frame=None,
        grid_tiles=None,
        host=None,
        make_slot: Optional[Callable[[str], Optional[Slot]]] = None,
        parent: Optional[QObject] = None,
    ):
        """With `sessions` (picture in picture), its large slot's player, live session and
        tuner stand for `session`, `live` and `budget`. `sound_sync` keeps each channel's
        sound sync (None: for this run only); `pick_subtitle_file(folder)` asks for a subtitle
        file (the Subtitles panel's "Load a file…"); with `online_subtitles` (and the overlays'
        `subtitle_search`) the panel searches OpenSubtitles too. `account_checks` (an
        AccountChecks) says why a failure happened (None: the failure's own words only).
        With `grid_frame` and `make_slot` (a tile's player, made by its slot's name) the
        multiview's grid can show: `grid_tiles` draws over it, `host` is the overlay host
        (both None-able in tests)."""
        super().__init__(parent)
        self._account_checks = account_checks
        self._toasts = 0  # toasts shown so far: a reason replaces only the one it explains
        self._toasted_at = 0.0  # when the last one showed (monotonic)
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
        self.sessions = sessions
        self._tuner: LiveTuner
        if sessions is not None:
            large = sessions.large()
            tuner = large.tuner
            if not isinstance(tuner, LiveTuner):
                raise TypeError("Each picture-in-picture slot needs a LiveTuner")
            self._tuner, self.session, live = tuner, large.playback, large.live
        else:
            self._tuner = LiveTuner(session, config, live, budget, self)
        self.live = live
        self.budget = self._tuner.budget
        self._states: dict[str, str] = {}  # each player's last state (a swap shows another)
        self._resume: Optional[Target] = None  # what stop_live() stopped, for resume_stopped()
        self._external_pending: Optional[tuple[Target, str]] = None
        self._external_settle = QTimer(self, singleShot=True)
        self._external_settle.timeout.connect(self._launch_settled)
        self._started = False
        self._wired = False
        self._closed = False
        self._guides_applied: Optional[set[str]] = None  # what the hub was last told
        self._lanes = LinkLanes(catalog, self)
        # The live channel's time-shift side: pipeline tunes, its controls and reports.
        self._live_controls = LiveControls(
            tuner=self._tuner,
            live=live,
            session=session,
            catalog=catalog,
            config=config,
            lanes=self._lanes,
            transport=overlays.transport,
            now=now,
            current=lambda: self._current,
            closed=lambda: self._closed,
            programmes=self._programmes,
            now_title=self.now_title,
            toast=self._toast,
            error=self._error,
            request=self._request,
            restore_channel=self._restore_channel,
            logo=self._logo,
            show_transport=self._show_transport,
            explain=self._explain_item,
            may_reopen=self._may_reopen,
            parent=self,
        )
        # Provider setups: the loading and welcome hints, the last channel, the Library's
        # hold on live TV.
        self._flow = ProviderFlow(
            catalog=catalog,
            nav=nav,
            config=config,
            overlays=overlays,
            closed=lambda: self._closed,
            has_media=self._has_media,
            links_busy=lambda: self._lanes.busy(),
            tune=lambda channel, key, channels: self.tune(
                channel, group_key=key, channels=channels
            ),
            open_drawer=self._open_drawer,
            live_section_loaded=self._live_section_loaded,
            error=self._error,
            drop_autoplay=lambda: self._autoplay.drop(),
            autoplay_hint=lambda: self._autoplay.hint(),
            explain=self._explain,
            parent=self,
        )
        self._current: Optional[Target] = None  # handed to the player
        self._current_url = ""  # its link (a swap records the picture made large)
        # The title the channel playing large went small for, until its link comes: its failure
        # gives the channel the large picture back.
        self._freed_for: Optional[Target] = None
        # That title never came while the small picture changed channel (no swap then): the
        # new small channel gives the large picture back once it starts.
        self._give_back_later = False
        # A title on its way that closes a channel (its provider has no stream for both) once
        # its link comes; said then (PipControls.make_room), never if the link fails.
        self._room_for: Optional[Target] = None
        self._cid: Optional[str] = None  # resume key of the VOD being played
        self._channel: Optional[dict] = None  # the channel tuned last, for INFO and the drawer
        self._digits = ""  # number entry under way
        self._state = "idle"
        self._tuning_since = 0.0  # when the banner's keep-alive started (TUNING_CAP_S)
        self._guide = GuideController(overlays.guide, parent=self)
        self._autoplay = Autoplay(
            catalog=catalog,
            config=config,
            card=overlays.next_card,
            play=self.play_vod,
            is_current=self._still_current,
            external_mode=lambda: self._external_launcher() is not None,
            notify=self._notify,
            set_hint=self._flow.wait_hint,
            show_transport=overlays.transport.poke,
            parent=self,
        )
        self._context = ContextActions(
            nav=nav,
            config=config,
            lanes=self._lanes,
            external=self.external,
            toggle_favorite=self._toggle_favorite,
            play_external=self._play_external,
            notify=self._menu_notify,
            open_pip=self._open_pip_node if sessions is not None else None,
            parent=self,
        )
        # The volume keys, mute and the wheel, on the mixer (one row per picture).
        self._mixer = MixerControls(
            mixer=overlays.mixer,
            sessions=sessions,
            session=lambda: self.session,
            config=config,
            toast=self._toast,
            names=self._picture_names,
            audio_changed=self._on_audio_changed,
            tiles=self._mixer_tiles,
            parent=self,
        )
        # Subtitles from OpenSubtitles for a film or an episode (the Subtitles panel's search).
        self._online: Optional[OnlineSubtitles] = None
        if online_subtitles is not None and overlays.subtitle_search is not None:
            self._online = OnlineSubtitles(
                page=overlays.subtitle_search,
                setup=online_subtitles,
                config=config,
                current=lambda: self._current,
                playing=self._has_media,
                provider=lambda item: item_provider.provider_name(self.catalog, item),
                load=lambda path: self._tracks.load_subtitle_file(
                    path, say=False, remember_folder=False
                ),
                toast=self._toast,
                parent=self,
            )
        # The sound's and the subtitles' tracks and sync (their panels, J/K and Shift+J/K),
        # subtitle files; a channel's sound sync is kept.
        self._tracks = TrackControls(
            sound_panel=overlays.sound_panel,
            subtitle_panel=overlays.subtitle_panel,
            pick_file=pick_subtitle_file,
            config=config,
            online=self._online,
            transport=overlays.transport,
            session=lambda: self.session,
            current=lambda: self._current,
            playing=self._has_media,
            channel_key=self._sync_key,
            store=sound_sync if sound_sync is not None else SoundSync(None),
            toast=self._toast,
            show_transport=self._show_transport,
            parent=self,
        )
        self._pip: Optional[PipControls] = None
        if sessions is not None:
            self._pip = PipControls(
                sessions=sessions,
                frame=pip_frame,
                label=overlays.pip_label,
                transport=overlays.transport,
                catalog=catalog,
                config=config,
                lanes=self._lanes,
                budget=self.budget,
                live_start=self._live_controls.live_start,
                nav=nav,
                drawer=overlays.drawer,
                zap=zap,
                all_live=self._all_live,
                large_channel=lambda: self._channel,
                vod=self.vod_current,
                external=lambda: self._external_launcher() is not None,
                closed=lambda: self._closed,
                toast=self._toast,
                swap=self.swap,
                toggle_mute=self._mixer.toggle_small_mute,
                sound=self._tracks.prepare_small,
                explain=self._explain_item,
                parent=self,
            )
            self._pip.changed.connect(self._mixer.refresh)
            self._pip.changed.connect(self._update_guides)
        # The multiview's grid (stage 1): its tiles are slots, the focused one the large.
        self._grid: Optional[GridControls] = None
        if sessions is not None and grid_frame is not None and make_slot is not None:
            self._grid = GridControls(
                sessions=sessions,
                frame=grid_frame,
                tiles=grid_tiles,
                pip_frame=pip_frame,
                host=host,
                transport=overlays.transport,
                catalog=catalog,
                budget=self.budget,
                config=config,
                make_slot=make_slot,
                focus=self.focus_tile,
                activate=self.video_clicked,
                open_drawer=lambda: self.handle(Action.OPEN_DRAWER),
                retry=self._retry_tile,
                denied=self._on_tile_denied,
                close_pip=self._drop_pip_on_its_way,
                vod=self.vod_current,
                external=lambda: self._external_launcher() is not None,
                closed=lambda: self._closed,
                toast=self._toast,
                reopen=self._reopen_tile,
                restore=self._restore_tile,
                parent=self,
            )
            self._grid.changed.connect(self._mixer.refresh)
            self._grid.changed.connect(self._update_guides)
        # The links of tiles opened beside the focused one, on the side lane (one at a time).
        self._tile_links: dict[str, Target] = {}
        # A restored grid's tiles waiting for their provider's setup: (tile, channel,
        # provider, group).
        self._waiting_tiles: list[tuple[str, dict, str, str]] = []
        self._column_wheel = PointerInput(self)
        # While a live link resolves, the banner is shown again before it fades (Minor 11).
        self._tuning = QTimer(self, interval=TUNING_KEEP_MS)
        self._tuning.timeout.connect(self._keep_tuning_banner)

    # ---- lifecycle ----------------------------------------------------------------------------

    def wire(self) -> None:
        """Connect to the catalog, overlays and session (once; start() does it otherwise)."""
        if not self._wired:
            self._wired = True
            self._wire()

    def start(self) -> None:
        if not self._started:
            self._started = True
            self.wire()
        self._update_guides()  # before any provider's setup starts a guide load
        self._flow.start()

    def _wire(self) -> None:
        catalog, zap, session = self.catalog, self.zap, self.session
        column, drawer = self.overlays.column, self.overlays.drawer
        transport = self.overlays.transport
        # Links settle per provider, on the hub's signals (every provider's, not just the
        # shown one's); the flow's own slots, on the shown provider's, run after.
        hub = getattr(catalog, "catalogs", catalog)
        hub.providerReady.connect(self._on_links_provider_ready)
        hub.providerFailed.connect(self._on_links_provider_failed)
        self._flow.wire()
        self._lanes.tuneReady.connect(self._on_tune_ready)
        self._lanes.tuneFailed.connect(self._on_tune_failed)
        self._lanes.tuneDropped.connect(self._on_tune_dropped)
        self._lanes.sideFailed.connect(self._on_side_failed)
        catalog.epgReady.connect(self._on_epg_ready)
        catalog.providerReady.connect(lambda *_: self._update_guides())
        self.nav.changed.connect(self._update_guides)
        self.overlays.guide.rowsMoved.connect(self._update_guides)
        self.overlays.guide.closed.connect(self._update_guides)
        self.overlays.drawer.closed.connect(self._update_guides)
        self.nav.notice.connect(self._on_notice)
        zap.tuneRequested.connect(self._on_zap_tune)
        zap.columnRequested.connect(self._on_column_requested)
        zap.highlightChanged.connect(self._on_highlight)
        zap.digitsChanged.connect(self._on_digits)
        zap.numberNotFound.connect(self._on_number_not_found)
        self._connect_session(session, True)
        for name, player in self._players():
            self._watch_player(name, player)
        if self.sessions is not None:
            self.sessions.largeChanged.connect(self._on_large_changed)
            self.sessions.largeChanged.connect(lambda *_: self._update_guides())
            self.sessions.pipStarted.connect(self._on_pip_started)
            self.sessions.pipStateChanged.connect(self._on_pip_state)
        column.closed.connect(zap.close_column)
        column.rowClicked.connect(self._on_column_row)
        drawer.playRequested.connect(self.play_item)
        drawer.channelFound.connect(self.play_found)
        drawer.contextRequested.connect(self._on_context)
        self._guide.tuneRequested.connect(self._on_guide_tune)
        self.overlays.guide.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.nextRequested.connect(self._on_next_requested)
        transport.playPauseRequested.connect(self._toggle_pause)
        transport.seekRequested.connect(self._seek_ms)
        transport.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.miniPlayerRequested.connect(self.window_actions.toggle_mini_player)
        transport.externalRequested.connect(self._on_external_requested)
        transport.volumeRequested.connect(self._mixer.open_main)
        # The buttons for the player's keys (the owner's mouse-only rule): they act as the keys.
        transport.guideRequested.connect(partial(self.handle, Action.GUIDE))
        transport.previousRequested.connect(partial(self.handle, Action.PREVIOUS))
        transport.infoRequested.connect(partial(self.handle, Action.INFO))
        transport.favoriteRequested.connect(self._favorite_playing)
        self._live_controls.wire()
        self._mixer.wire()
        self._tracks.wire()
        if self._pip is not None:
            self._pip.wire()
        if self._grid is not None:
            self._grid.wire()
            gridded = getattr(transport, "gridRequested", None)
            if gridded is not None:
                gridded.connect(partial(self.handle, Action.MULTIVIEW))
            # A channel dragged from the channel list onto a tile plays there.
            dragged = getattr(drawer, "channelDragged", None)
            if dragged is not None:
                drawer.set_drag_enabled(self.multiview_shown)
                dragged.connect(self._on_channel_dragged)
                drawer.channelDropped.connect(self._on_channel_dropped)
                drawer.dragEnded.connect(self._grid.drag_ended)
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
        self._lanes.close()
        self._autoplay.close()
        if self._online is not None:
            self._online.shutdown()  # a sign-in ends, never holding up the quit
        self._tuning.stop()
        self._cancel_external()
        self._flow.shutdown()
        if self._account_checks is not None:
            self._account_checks.close()  # a check under way answers no one
        self.overlays.toast.set_hint(None)  # e.g. the closing notice needs the toast
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        try:
            self._mixer.save()  # a PiP level changed while the mixer still shows
        except Exception as e:
            self._log_failure("save the picture-in-picture volume", e)
        try:
            if self._grid is not None:
                self._grid.shutdown()  # the tiles but the focused one stop
            self._close_pip()  # the small picture first, then the large one
            self._tuner.shutdown()
        except Exception as e:
            self._log_failure("stop playback", e)

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        self._flow.select_provider(name, force_refresh=force_refresh)

    def stop_live(self) -> None:
        """Stop what plays (window close, self-update): its slot goes back once released."""
        self._resume = self._current if self._has_media() else None
        self.leave_multiview(keep_pip=False)
        self._close_pip()
        self._live_controls.stop_tune()

    def resume_stopped(self) -> None:
        """Play again what stop_live() stopped (a self-update that could not launch).

        With a fresh link: a channel tunes again, a movie or episode reopens at the position
        the watch state saved.
        """
        target, self._resume = self._resume, None
        if not self._closed and target is not None and target is self._current:
            self._request(replace(target, zapped=False, retried=False, start_over=False))

    def stop_for_library(self) -> None:
        """Stop what plays, waiting for nothing: the corner player's ×, or the Library opened
        with nothing playing (spec §5.4; what plays goes on in the corner otherwise).

        A link still on its way never opens, the guide, the Next card and the other player
        overlays close, and the stream's budget slot goes back once LiveTuner's release gate
        has let go of it. Until resume_live(), tune_live() or play_vod(), a provider change
        neither tunes the last channel nor opens the drawer.
        """
        if self._closed:
            return
        self.leave_multiview(keep_pip=False)
        self._flow.enter_library()
        self._cancel_external()
        self._lanes.supersede()  # an external play waiting for its link no longer plays
        self._lanes.drop_tune()  # a link on its way never opens
        self._autoplay.drop()  # the Next card, and a wait for the next season
        self._guide.close()
        self._tuning.stop()
        if self._digits:
            self.zap.cancel_digits()
        overlays = self.overlays
        overlays.column.close()
        overlays.drawer.close_drawer()
        overlays.banner.dismiss()
        overlays.transport.dismiss()
        self._tracks.close()
        self._close_online()
        try:
            self.watch.flush()  # a movie's last position
        except Exception as e:
            self._log_failure("save the watch state", e)
        self._close_pip()  # both pictures stop
        self._live_controls.stop_tune()
        self._current, self._cid = None, None

    def playing(self) -> bool:
        """Something plays, or its stream is opening; or the channel plays small while the
        title it went small for is on its way."""
        return not self._closed and (self._has_media() or self._title_on_its_way())

    def _title_on_its_way(self) -> bool:
        sessions = self.sessions
        return self._freed_for is not None and sessions is not None and sessions.pip_open()

    def enter_library_playing(self) -> None:
        """The window opens the Library with the player in a corner (2.1): what plays keeps
        playing and nothing stops.

        The drawer, the zap column and banner, the controls, the mixer, the guide, a number being
        typed and the Next card close (the overlays are gone from the corner player). Until
        leave_library_playing(), a provider change neither tunes the last channel nor opens
        the drawer, as in the Library.
        """
        if self._closed:
            return
        self.leave_multiview()  # the focused tile plays on in the corner, as the big picture
        self._flow.enter_library()
        if self._title_on_its_way():
            # The title the channel went small for is dropped, as a link on its way is when the
            # Library opens: the channel takes the large picture back and plays on.
            self._lanes.drop_tune()
            self._give_large_back(self._freed_for)
        self._guide.close()
        if self._digits:
            self.zap.cancel_digits()
        overlays = self.overlays
        overlays.column.close()
        overlays.drawer.close_drawer()
        overlays.banner.dismiss()
        overlays.transport.dismiss()
        self._mixer.hide()
        self._tracks.close()
        self._close_online()
        self._autoplay.drop()  # the Next card, and a wait for the next season

    def leave_library_playing(self) -> None:
        """The player page is back from the corner: live TV and the drawer act as before."""
        if not self._closed:
            self._flow.leave_library()

    def main_muted(self) -> bool:
        """Whether the large picture is muted."""
        return self._mixer.main_muted()

    def resume_live(self) -> None:
        """Back to live TV from the Library (R5): the last live channel again, else the drawer
        (see ProviderFlow.resume_live)."""
        if not self._closed:
            self._flow.resume_live()

    def tune_live(self, channel: dict) -> None:
        """A live channel from the Library's search: ↑/↓ then walk every channel (group "*")."""
        self._flow.leave_library()
        self.tune(channel, group_key=ALL_GROUP_ID, channels=self._all_live())

    def play_file(self, path: str) -> None:
        """Play a local media file (File → Open File…) as a movie: no link, no history.

        It is no provider's: nothing records it, and no position is saved for it.
        """
        if self._closed:
            return
        self._flow.leave_library()
        url = Path(path).absolute().as_uri()
        title = Path(path).name
        target = Target({"name": title, "cmd": url}, "movies", title, local=True)
        self.leave_multiview(keep_pip=False)  # the multiview is for live TV
        self._close_pip()  # picture in picture is for live TV
        self._cancel_external()
        self._lanes.supersede()
        self._lanes.drop_tune()  # a channel or movie still on its way never opens over it
        self._autoplay.drop()
        self._live_controls.new_target()
        previous, self._current, self._cid = self._current, target, None

        session = self.session  # the large picture's, whatever swaps before it opens

        def open_file() -> None:
            self._tracks.prepare(session, target, previous)
            session.play(url, kind=classify(url, False), start_ms=0)
            self._reset_speed(session)

        self._tuner.open(self._guarded(target, open_file), local=True)  # no provider slot
        self.overlays.banner.dismiss()
        self.overlays.transport.set_next_available(False)
        self.overlays.transport.show_vod(title, "")

    def settings_saved(self) -> None:
        """Settings were saved: a new live latency reaches the running channel at once.

        The time-shift settings (disk, size) apply from the next tune.
        """
        for tuner in self._tuners():
            tuner.set_latency(self.config.live_latency)

    def _repaint(self) -> None:
        """Repaint what depends on settings or EPG: column rows and the open drawer."""
        self.overlays.column.refresh_now()
        self.nav.changed.emit()

    # ---- input --------------------------------------------------------------------------------

    def key_context(self) -> str:
        """The keymap context for the next key: the OpenSubtitles search, the open guide or
        the drawer takes the keys."""
        if self._online is not None and self._online.visible():
            return "subtitles"
        if self._guide.is_open():
            return "guide"
        return "drawer" if self.overlays.drawer.isVisible() else "player"

    def overlay_open(self) -> bool:
        """Whether Esc would close something first: the drawer, the guide, the Next card (or the
        wait for the next season), a number being typed, the zap column or the banner."""
        overlays = self.overlays
        return bool(
            (self._online is not None and self._online.visible())
            or self._guide.is_open()
            or self._mixer.visible()
            or self._tracks.visible()
            or overlays.drawer.isVisible()
            or self._autoplay.card_open()
            or self._autoplay.waiting()
            or self._digits
            or overlays.column.isVisible()
            or overlays.banner.isVisible()
        )

    def vod_current(self) -> bool:
        """A movie, an episode or a local file is what the player has (playing or not)."""
        current = self._current
        return current is not None and not current.live

    def vod_chosen(self) -> bool:
        """The newest choice is not a live channel: a movie, an episode or a file playing or on
        its way, or nothing (its link failed)."""
        pending = self._lanes.pending()
        if pending is not None and not self._is_stay(pending):
            return not pending.live
        current = self._current
        return current is None or not current.live

    def handle(self, action: Action, payload: str = "", key: Optional[int] = None) -> None:
        """`key` is the Qt key pressed, when it matters (the mixer takes the arrow keys)."""
        if self._closed:
            return
        if self._online is not None and self._online.handle(action, payload):
            return  # the OpenSubtitles search shows: typing, the lists, Enter and Esc
        if self._guide.is_open() and self._guide.handle(action, payload):
            return
        drawer = self.overlays.drawer
        if not drawer.isVisible() and self._tracks.handle(action, key):
            return  # a Sound or Subtitles panel shows: the arrows, Enter and Esc are its own
        if not drawer.isVisible() and self._mixer.handle(action, key):
            return  # the mixer shows: the arrows, M, Tab and Esc are its own
        if action in (Action.CH_NEXT, Action.CH_PREV):
            delta = 1 if action is Action.CH_NEXT else -1
            if self._on_demand():
                self._change_volume(-VOLUME_STEP * delta)  # ↑ is louder
            else:
                self.zap.step(delta)
        elif action in (Action.MEDIA_NEXT, Action.MEDIA_PREV):
            delta = 1 if action is Action.MEDIA_NEXT else -1
            if drawer.isVisible():
                return  # the drawer is browsing; play/pause still works
            if self._on_demand():
                self._seek(delta * SEEK_STEP_MS)
            else:
                self.zap.step(delta)
                self._show_transport()
        elif action is Action.DIGIT:
            if not self._on_demand():
                self.zap.digit(payload)
        elif action is Action.PREVIOUS:
            if not self._on_demand():
                self.zap.previous()
        elif action is Action.OK:
            if self.overlays.welcome.activate():
                pass  # no provider yet: Enter is the card's Add provider
            elif self._autoplay.card_open():
                self._autoplay.play_now()
            elif self.overlays.column.isVisible():
                self._on_column_row(self.overlays.column.current_index())  # tune it now
            else:
                self._open_drawer()
        elif action is Action.OPEN_DRAWER:
            from_column = self.overlays.column.isVisible()
            self.overlays.column.close()
            self._open_drawer(from_column=from_column)
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            back = action is Action.SEEK_BACK
            if not self._live_controls.seek_live(back):
                self._seek(-SEEK_STEP_MS if back else SEEK_STEP_MS)
        elif action is Action.PLAY_PAUSE:
            self._tuner.toggle_pause()
            self._show_transport()
        elif action is Action.INFO:
            self._info()
        elif action is Action.GO_LIVE:
            self._live_controls.go_live()
        elif action is Action.GUIDE:
            self._open_guide()
        elif action is Action.FAVORITE:
            self._favorite()
        elif action is Action.FULLSCREEN:
            self.window_actions.toggle_fullscreen()
        elif action is Action.MINI_PLAYER:
            self.window_actions.toggle_mini_player()
        elif action in (Action.PIP, Action.SWAP) and self.multiview_shown():
            self._toast(LEAVE_FIRST)  # the grid's tiles are the pictures meanwhile
        elif action is Action.PIP:
            if self._pip is not None:
                self._pip.pip_key()
        elif action is Action.SWAP:
            if self._pip is not None:
                self._pip.swap_key()
        elif action is Action.MULTIVIEW:
            self.toggle_multiview()
        elif action is Action.MUTE:
            self._mixer.mute()
        elif action in (Action.SOUND_EARLIER, Action.SOUND_LATER):
            self._tracks.nudge_sound(-1 if action is Action.SOUND_EARLIER else 1)
        elif action in (Action.SUBTITLES_EARLIER, Action.SUBTITLES_LATER):
            self._tracks.nudge_subtitles(-1 if action is Action.SUBTITLES_EARLIER else 1)
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
        """Fullscreen; on the multiview's grid, back to one picture (its first click focused
        the tile, which becomes the big picture)."""
        if self.multiview_shown():
            self.leave_multiview()
        else:
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

    def _close_online(self) -> None:
        if self._online is not None:
            self._online.close()

    def pointer_may_hide(self) -> bool:
        """The pointer may go with the player controls: a picture plays on the player page and
        no list, guide, mixer or card that the pointer clicks is open."""
        if self._closed or self._current is None or not self._has_media():
            return False
        if self._flow.in_library():
            return False
        overlays = self.overlays
        menus = (
            overlays.drawer,
            overlays.column,
            overlays.guide,
            overlays.next_card,
            overlays.welcome,
            overlays.mixer,
            overlays.sound_panel,
            overlays.subtitle_panel,
            overlays.subtitle_search,
        )
        return not any(menu is not None and menu.isVisible() for menu in menus)

    def wheel(self, steps: int, modifiers) -> None:
        """`steps` follows PointerInput: +1 is one notch down (the next channel)."""
        if not steps or self._closed:
            return
        volume = as_int(modifiers) & _VOLUME_MODIFIERS
        if volume or self.config.mouse_wheel_action == "volume" or self._on_demand():
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
        self._lanes.supersede()  # and an external play still waiting for its link
        context = self.zap.context
        picked = item_provider.pick_source(self.catalog, channel, group_key)
        if group_key != context.group_key or context.index_of(channel) < 0:
            # Numbers reach every live channel, not only the group's (TV numbers are global).
            self.zap.set_context(
                ZapContext(group_key, channels, fallback=self._all_live(), source=picked)
            )
        elif not from_zap or not context.source:
            context.source = picked  # a zap keeps the list its group was picked from
        playing = self._playing_live()
        stay = playing is not None and ZapContext.same(channel, playing.item)
        if not stay and self._shown_elsewhere(channel, group_key, channels):
            return
        if not from_zap and not stay:
            self.zap.note_request(channel)  # ↑/↓ move on from it while its link is pending
        self.overlays.column.close()
        self._channel = channel
        self._update_guides()
        self._show_banner(channel)
        if stay:
            # The channel already playing was chosen again: never restart it, and drop a
            # pending choice when it settles (the "stay" marker is the playing target).
            if self._lanes.busy():
                self._lanes.queued = playing
            self.zap.tuned(channel)
            return
        if self._grid is not None and self._grid.refuse(channel):
            # Its provider has no stream to spare for the focused tile: its card says so, and
            # no link is asked for (an STB portal mints one per request).
            self._lanes.drop_tune()
            self._live_controls.new_target()
            self._current, self._cid = None, None
            self.zap.tuned(channel)
            return
        title = str(channel.get("name") or "")
        self._request(
            Target(
                channel,
                "live",
                title,
                group_key=group_key,
                source=self.zap.context.source or picked,
                zapped=from_zap,
            )
        )
        self._tuning_since = time.monotonic()
        self._tuning.start(TUNING_KEEP_MS)

    def play_item(self, node: NavNode, siblings: Sequence[NavNode], parent: NavNode) -> None:
        """A drawer row was chosen. Only channels play from the drawer: Movies and Series
        open the Library, whose titles and episodes play through play_vod()."""
        if self._closed:
            return
        if node.layer != "item" or self.nav.section_of(node) != "live":
            logger.debug("Not playable from the drawer: %s", node)
            return
        # The whole group, not just the rows the drawer's filter shows: ↑/↓ walk the group.
        nodes = self.nav.children(parent) or siblings
        channels = payloads(nodes)
        self.tune(node.payload, group_key=parent.key, channels=channels)
        self.overlays.drawer.close_drawer()

    def play_found(self, channel: dict, group_id: str = "") -> None:
        """A channel the drawer's find found (any provider's), and the list closes. ↑/↓ then
        walk the genre it is first listed in (`group_id`, its provider's), as from that genre's
        list, when the list shows that provider (or every one); else every channel, as a
        channel found by the Library's search."""
        if self._closed:
            return
        key, channels = self._found_group(channel, group_id)
        if key is None:
            self.tune_live(channel)
        else:
            self._flow.leave_library()
            self.tune(channel, group_key=key, channels=channels)
        self.overlays.drawer.close_drawer()

    def _found_group(self, channel: dict, group_id: str) -> tuple[Optional[str], list[dict]]:
        """The list's key of a found channel's genre and its channels; (None, []) when the
        list cannot show it (another provider's while it shows one) or it is not there."""
        if not group_id:
            return None, []
        provider = provider_of(channel)
        if self.nav.all_mode():
            key = mixed_group_id(provider, group_id)
        elif provider in ("", self.catalog.current_provider_name()):
            key = group_id
        else:
            return None, []
        channels = list(self.catalog.items("live", key) or [])
        if not any(ZapContext.same(channel, c) for c in channels):
            return None, []
        return key, channels

    def play_vod(
        self,
        item: dict,
        kind: str,
        context: Optional[dict] = None,
        *,
        from_library: Optional[bool] = None,
    ) -> None:
        """Play a movie or an episode where it stopped (the drawer and the Library).

        `kind` is "movie" or "episode". `context` is the Library's play context: start_over
        plays from the start; an episode's series and season, or a movie's genre_items, give
        Next and autoplay what follows. Its link is the tune slot's, and it opens once what
        plays now (a live channel too) is released. `from_library` says the title was chosen in
        the Library: TVPages knows it before it switches pages (which ends the flow's Library
        state); None reads the flow.
        """
        if self._closed:
            return
        if from_library is None:
            from_library = self._flow.in_library()
        self.leave_multiview()  # live only: the focused tile is the big picture again
        self._flow.leave_library()
        context = play_context(context)
        episode = kind == "episode"
        series = context["series"]
        title = item_title(item, episode=episode)
        beside = self._title_beside_channel()  # what plays large is a title, a channel small
        target = Target(
            item,
            "series" if episode else "movies",
            title,
            subtitle=item_title(series) if episode and isinstance(series, dict) else "",
            context=context,
            start_over=bool(context["start_over"]),
        )
        if self._pip is not None:
            self._pip.forget_room()  # what an older title would have closed
        kept = False
        if beside and self._pip is not None:
            self._pip.keep_beside(item, title)  # Next, autoplay, another title: it plays on
        elif from_library:
            kept = self._keep_channel_small(item, title)  # it closes the small picture, or not
        else:
            self._close_pip()  # picture in picture is for live TV
        self._freed_for = target if kept else None
        self._room_for = target if self._pip is not None and self._pip.has_room() else None
        self._lanes.supersede()  # a newer choice than an external play waiting for its link
        self._request(target)

    def _title_beside_channel(self) -> bool:
        """A movie or an episode is the large picture's (playing, ended or failed) while the
        small picture shows a channel (the one kept small for a title from the Library)."""
        current, sessions = self._current, self.sessions
        return (
            current is not None
            and not current.live
            and not current.local
            and sessions is not None
            and sessions.pip_open()
        )

    def _keep_channel_small(self, item: dict, title: str) -> bool:
        """A title from the Library while a channel plays large (corner player): the channel
        goes small without reopening and the title opens in the freed large slot.

        False: the channel was not kept and the caller plays as before. With no channel
        streaming large the small picture closed (it is for live TV); when the channel's
        provider has no stream for the title, the channel closes for it and a small picture of
        another provider plays on (PipControls.keep_large_small).
        """
        pip = self._pip
        if pip is None or self.sessions is None or self._playing_live() is None:
            self._close_pip()
            return False
        if self._tuner.settling() or not self._tuner.running():
            self._close_pip()
            return False  # a channel still opening has nothing to keep
        self._cancel_external()
        self._lanes.drop_tune()  # a choice on its way for the large picture is superseded
        return pip.keep_large_small(item, title)

    def _all_live(self) -> list[dict]:
        return list(self.catalog.items("live", ALL_GROUP_ID) or [])

    def _request(self, target: Target) -> None:
        self._give_back_later = False  # the large picture has a newer choice
        self._cancel_external()
        self._flow.drop_autotune()  # a choice made before the live section loaded comes first
        self._autoplay.drop()  # a newer choice than what the Next card offers
        self._lanes.request(target)

    def _follow_queue(self, settled: Target) -> bool:
        """Act on a choice made while `settled` was in flight; False when there was none."""
        queued = self._lanes.take_queued()
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

    def _is_stay(self, target: Optional[Target]) -> bool:
        """True for the marker that keeps the playing channel (see _tune)."""
        return target is not None and target is self._current and self._has_media()

    def _on_tune_ready(self, target: Target, url: str) -> None:
        if self._closed:
            return
        queued = self._lanes.queued
        if queued is not None and not self._is_stay(queued) and queued.same_as(target):
            self._lanes.queued = None
            target = queued  # chosen again meanwhile: this link serves the newest choice
        elif self._follow_queue(target):
            return
        self._play_link(target, url)

    def _play_link(self, target: Target, url: str) -> None:
        previous = self._current
        try:
            self._deliver(target, url)
        except Exception as e:
            self._current = previous  # whatever was playing before is still the current one
            self._log_failure(f"play {target.title}", e)
            self._restore_channel()
            self._error(f"{target.title}: {e}")

    def _on_tune_failed(self, target: Target, message: str) -> None:
        if self._closed:
            return
        if message == PROVIDER_LOADING_MESSAGE:
            # A provider setup is running; providerReady asks again for the newest choice:
            # one queued meanwhile is newer than the target that just failed. Only that
            # provider's ready asks it: another provider's newer choice goes on now.
            queued = self._lanes.queued
            if queued is None:
                self._lanes.queued = target
            elif self._link_owner(queued.item) != self._link_owner(target.item):
                self._follow_queue(target)
            return
        if self._follow_queue(target):
            return  # the failed target was superseded: no error for it
        self._forget_room(target)
        self._give_large_back(target)
        self._restore_channel()
        self._error(f"{target.title}: {message}")
        self._explain_item(target.item, target.title)

    def _on_tune_dropped(self, _target: Target) -> None:
        """A tune overtaken by an external play: the channel still playing stays the channel."""
        if self._closed:
            return
        self._restore_channel()  # INFO, the guide and Ctrl+D follow what plays
        playing = self._playing_live()
        if playing is not None:
            self.zap.tuned(playing.item)  # ↑/↓ move on from it, not from the dropped one

    def _on_side_failed(self, target: Target, message: str) -> None:
        """A context menu's link failed (or what used it did), or a tile's beside the focus:
        that tile says so too."""
        if self._closed:
            return
        for name, waiting in list(self._tile_links.items()):
            if waiting is target:
                del self._tile_links[name]
                if self._grid is not None:
                    self._grid.link_failed(name, target.item)
        self._menu_error(f"{target.title}: {message}")

    def _keep_tuning_banner(self) -> None:
        """Show the banner again while the channel it announces is on its way: its link
        resolves, or (delivered) its stream waits for the old one or opens, so a slow start
        never lets the banner fade before the picture comes (capped at TUNING_CAP_S)."""
        pending = self._lanes.pending()
        channel, banner, current = self._channel, self.overlays.banner, self._current
        resolving = (
            pending is not None
            and pending.live
            and not self._is_stay(pending)
            and channel is not None
            and ZapContext.same(pending.item, channel)
        )
        opening = (
            pending is None
            and current is not None
            and current.live
            and channel is not None
            and ZapContext.same(current.item, channel)
            and (self._tuner.settling() or self._state in ("idle", "opening", "buffering"))
        )
        fresh = time.monotonic() - self._tuning_since < TUNING_CAP_S
        tuning = not self._closed and fresh and (resolving or opening)
        if not tuning:
            self._tuning.stop()  # delivered or failed: the banner fades on its own
            return
        if banner.isVisible() and channel is not None:  # never one that was dismissed
            banner.show_info(self.banner_info(channel))

    def _give_large_back(self, target: Optional[Target]) -> None:
        """`target` never came (its link or its provider failed): when the channel playing
        large went small for it, the pictures swap back and the empty one closes."""
        if target is not None and target is self._freed_for:
            self._freed_for = None
            sessions = self.sessions
            if not self.swap() and sessions is not None and sessions.changing():
                self._give_back_later = True  # once the small picture's channel starts

    def _forget_room(self, target: Optional[Target]) -> None:
        """`target` never came: the channel it would have closed plays on, unsaid."""
        if target is not None and target is self._room_for and self._pip is not None:
            self._room_for = None
            self._pip.forget_room()

    def _on_pip_started(self) -> None:
        """The small picture's stream started: a swap back that waited for its channel change
        gives it the large picture now, while that is still empty."""
        if not self._give_back_later or self._closed:
            return
        self._give_back_later = False
        if self._current is None and not self._lanes.busy() and self._tuner.idle():
            self.swap()  # the empty picture, small now, closes

    def _on_pip_state(self, open_: bool) -> None:
        if not open_:
            self._give_back_later = False

    def _restore_channel(self) -> None:
        """After a failed tune, INFO and the drawer follow the channel still playing."""
        current = self._current
        self._channel = current.item if current is not None and current.live else None

    def _deliver(self, target: Target, url: str) -> None:
        item = target.item
        self._freed_for = None  # it came: the large picture is the title's
        if target is self._room_for and self._pip is not None:
            self._room_for = None
            self._pip.make_room()  # before it opens: a closing small picture is waited for
        launcher = self._external_launcher()
        # Direct plays book the slot under the item's own provider, as pipeline tunes do.
        provider_name = item_provider.provider_name(self.catalog, item)
        self._live_controls.new_target()  # the old channel's ticks and held notice
        previous = self._current  # what the large picture played: a retry keeps its sync
        if launcher is not None:
            self.leave_multiview(keep_pip=False)  # it is outside the budget too
            self._close_pip()  # an external player is outside the budget: the PiP gives way
            if target.live and target.zapped:
                # Each zap step would start a player: only the one zapping settles on does.
                self._external_pending = (target, url)
                self._external_settle.start(EXTERNAL_SETTLE_MS)
                return
            # The in-app stream is released first: the player opens the provider again.
            self._tuner.open(self._guarded(target, lambda: self._launch(target, url)))
            return
        if target.live and self.multiview_shown():
            self._open_in_tile(target, url, previous, provider_name)
            if self.overlays.transport.isVisible():
                self._live_controls.show_live_transport(target)
        elif target.live:
            kind = classify(url, self.catalog.is_live("live"))
            if self._live_controls.through_pipeline(url, kind):
                if not self._live_controls.tune_pipeline(target, url):
                    return  # over the provider's stream limit: nothing opens
                # The old stream is stopped: the channel's sync waits for its player's open.
                self._tracks.prepare(self.session, target, previous)
                self._current, self._cid = target, None
                self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            else:
                self._current, self._cid = target, None
                session = self.session  # the large picture's, whatever swaps before it opens

                def play_direct() -> None:
                    self._tracks.prepare(session, target, previous)
                    session.play(
                        url, kind=kind, latency=self.config.live_latency, **stream_options(item)
                    )
                    self._reset_speed(session)

                self._tuner.open(
                    self._guarded(target, play_direct), direct=True, provider=provider_name
                )
            if self.overlays.transport.isVisible():
                self._live_controls.show_live_transport(target)
        else:
            cid = self.watch.content_id(provider_name, item, item_type=target.item_type)
            start = 0 if target.start_over else self.watch.resume_position(cid)
            self._current, self._cid = target, cid
            session = self.session

            def open_vod() -> None:
                self._tracks.prepare(session, target, previous)
                session.play(url, kind=classify(url, False), start_ms=start)
                self._reset_speed(session)

            self._tuner.open(self._guarded(target, open_vod), direct=True, provider=provider_name)
            if self._current is not None and self._current is not target:
                return  # its open failed at once: the channel kept small took the picture back
            if start:
                self._toast(f"Resumed at {fmt_duration(start)}")
            self.overlays.banner.dismiss()  # the transport takes the bottom of the video
            self.overlays.transport.set_next_available(self._autoplay.has_next(target))
            self.overlays.transport.show_vod(target.title, target.subtitle)
        if self._current is target:  # else opening it failed at once (see _guarded)
            self._delivered(target, url)

    def _guarded(self, target: Target, open_: Callable[[], None]) -> Callable[[], None]:
        """`open_` as an open once the old stream is released: now, or from a signal later."""

        def run() -> None:
            try:
                open_()
            except Exception as e:
                # What played before was stopped to open this: nothing is current now.
                self._current, self._cid = None, None
                self._log_failure(f"play {target.title}", e)
                if not target.live:
                    self._give_small_channel_large()
                self._restore_channel()
                self._error(f"{target.title}: {e}")

        return run

    def _launch(self, target: Target, url: str, launcher: Optional[Launcher] = None) -> None:
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
            self._menu_error(first.strip() or "The external player could not start")
            return
        self._current, self._cid = target, None
        self._delivered(target, url)

    def _delivered(self, target: Target, url: str) -> None:
        """The target is playing (or opening): confirm the zap and record it in the history."""
        self._current_url = url
        if self.sessions is not None:  # what the large picture shows (a zap onto it stays)
            large = self.sessions.large()
            large.channel = target.item if target.live else None
            large.group_key = target.group_key if target.live else None
        if target.live:
            self.zap.tuned(target.item)
        if not target.local:  # a file is no provider's: no history, as play_file
            self._record_watched(target, url)

    def _record_watched(self, target: Target, url: str) -> None:
        try:
            self.watch.record_play(
                item_provider.provider_name(self.catalog, target.item),
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

    def _reset_speed(self, session=None) -> None:
        (session or self.session).set_rate(1.0)
        if session is None or session is self.session:
            self.overlays.transport.set_speed(1.0)

    def _external_launcher(self) -> Optional[Callable[[str], tuple[bool, str]]]:
        launcher: Optional[Callable[[str], tuple[bool, str]]] = None
        if self.config.play_in_vlc:
            launcher = self.external.launch_vlc
        elif self.config.play_in_mpv:
            launcher = self.external.launch_mpv
        return launcher

    # ---- picture in picture -------------------------------------------------------------------

    def swap(self) -> bool:
        """The large and the small picture trade places (S, a click on the PiP, a zap onto its
        channel): nothing restarts. A choice still on its way for the large picture is
        dropped; while its next stream waits for its slot, nothing swaps (False)."""
        sessions = self.sessions
        if self._closed or sessions is None or not sessions.pip_open():
            return False
        if self._tuner.settling():
            logger.debug("Not swapping: the large picture's stream is opening")
            return False
        self._cancel_external()
        self._lanes.drop_tune()  # superseded: it would replace the picture swapped in
        return sessions.swap()

    def open_pip(self, channel: dict, *, group_key: str, channels: list[dict]) -> None:
        """Open `channel` small (the drawer's menu or Ctrl/⌘+P), or show it in the PiP."""
        if self._pip is not None and not self._closed:
            source = item_provider.pick_source(self.catalog, channel, group_key)
            context = ZapContext(group_key, channels, fallback=self._all_live(), source=source)
            self._pip.open_channel(channel, context)

    def _pip_takes(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """A tune of the large picture onto the PiP's channel (Review Focus 3): never a second
        stream of it. Shown small, the pictures swap; on its way there, it opens large."""
        pip = self._pip
        if pip is None:
            return False
        if pip.claims(channel):
            pip.close()  # the large picture takes it instead
            return False
        small = pip.small_showing(channel)
        if small is None:
            return False
        if self._tuner.settling():
            self._tuner.stop()  # the zap still waiting is abandoned: nothing to keep small
        pip.view(small.name).context = ZapContext(
            group_key,
            channels,
            fallback=self._all_live(),
            source=item_provider.pick_source(self.catalog, channel, group_key),
        )
        self.overlays.column.close()
        if self.swap():
            return True
        pip.close()  # it cannot swap now: the PiP closes and the channel opens large
        return False

    def _close_pip(self) -> None:
        if self._pip is not None:
            self._pip.close()

    # ---- the multiview ------------------------------------------------------------------------

    def multiview_shown(self) -> bool:
        """The multiview's grid shows: its focused tile is the large picture."""
        return self._grid is not None and self._grid.shown()

    def toggle_multiview(self) -> None:
        """V, View → Multiview, the grid button: the grid, or back to one picture."""
        if self._grid is None or self._closed:
            return
        if self._digits:
            self.zap.cancel_digits()
        self.overlays.column.close()
        self._grid.toggle()

    def leave_multiview(self, *, keep_pip: bool = True) -> None:
        """Back to one picture (nothing when the grid is not shown): the focused tile is the
        big picture, and with `keep_pip` the tile focused before it stays small. What the big
        picture shows is recorded, as a swap records it."""
        grid = self._grid
        if grid is None or not grid.shown():
            return
        grid.leave(keep_pip=keep_pip)
        current = self._current
        if current is not None and current.live and self._has_media() and self._current_url:
            self._record_watched(current, self._current_url)

    def focus_tile(self, name: str) -> bool:
        """The multiview's tile `name` takes the focus (the large role): the controls, the
        banner and zapping act on it, and its sound plays. A choice still on its way for the
        tile that had the focus is dropped, as a swap drops it."""
        sessions = self.sessions
        if self._closed or sessions is None:
            return False
        if sessions.large().name == name:
            return True
        self._cancel_external()
        self._lanes.drop_tune()
        return sessions.set_large(name)

    def _shown_elsewhere(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """A tune onto a channel another picture shows: never a second stream of it."""
        if self.multiview_shown():
            return self._grid_takes(channel, group_key, channels)
        return self._pip_takes(channel, group_key, channels)

    def _grid_takes(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """Another tile shows the channel: the focus moves there, with the zap group."""
        grid, pip = self._grid, self._pip
        name = grid.tile_showing(channel) if grid is not None else None
        if name is None or pip is None:
            return False
        pip.view(name).context = ZapContext(
            group_key,
            channels,
            fallback=self._all_live(),
            source=item_provider.pick_source(self.catalog, channel, group_key),
        )
        self.overlays.column.close()
        return self.focus_tile(name)

    def _open_in_tile(
        self, target: Target, url: str, previous: Optional[Target], provider_name: str
    ) -> None:
        """A live channel for the multiview's focused tile, within its provider's limit:
        through the time-shift pipeline in memory (the focused tile's buffer), as a PiP
        opens, or directly when it cannot be time-shifted."""
        grid, sessions = self._grid, self.sessions
        assert grid is not None and sessions is not None
        item = target.item
        kind = classify(url, self.catalog.is_live("live"))
        self._current, self._cid = target, None
        session = self.session  # the focused tile's, whatever has the focus when it opens
        limit = self._live_controls.limit(item)
        if self._live_controls.through_pipeline(url, kind):
            start = self._live_controls.live_start(target, url)
            kwargs = tune_kwargs(start, self.config, disk=False)
            size = sessions.tile_bytes(True)
            if size is not None:
                kwargs["max_bytes"] = size
            grid.open_focused(item, provider=provider_name, limit=limit, start_kwargs=kwargs)
            # The old stream is stopped: the channel's sync waits for its player's open.
            self._tracks.prepare(session, target, previous)
            self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            return

        def play_direct() -> None:
            self._tracks.prepare(session, target, previous)
            session.play(url, kind=kind, latency=self.config.live_latency, **stream_options(item))
            self._reset_speed(session)

        grid.open_focused(
            item, self._guarded(target, play_direct), provider=provider_name, limit=limit
        )

    def _on_tile_denied(self, name: str) -> None:
        """A tile's stream was refused (its card says so): when it has the focus, nothing
        plays large any more."""
        sessions = self.sessions
        if sessions is None or self._closed or sessions.large().name != name:
            return
        self._current, self._cid = None, None
        self._live_controls.new_target()

    def _retry_tile(self, channel: dict) -> None:
        """A click on a tile's card: its channel again, in the zap group when it is there."""
        context = self.zap.context
        if context.index_of(channel) >= 0:
            self.tune(channel, group_key=context.group_key, channels=context.channels)
        else:
            self.tune(channel, group_key=ALL_GROUP_ID, channels=self._all_live())

    def play_in_tile(
        self,
        name: str,
        channel: dict,
        *,
        group_key: str = ALL_GROUP_ID,
        channels: Optional[list[dict]] = None,
        retried: bool = False,
    ) -> None:
        """`channel` in the multiview's tile `name` (a restored grid, a channel dropped on a
        tile, an STB tile's fresh link): the focused tile tunes as zapping does; another
        opens beside it, its focus unchanged: its provider's limit is checked first, and its
        link asked for on the side lane (one at a time, as the PiP's)."""
        grid, sessions = self._grid, self.sessions
        if grid is None or sessions is None or self._closed or not grid.shown():
            return
        if name not in grid.names():
            return
        listed = channels if channels is not None else self._all_live()
        if name == sessions.large().name:
            self.tune(channel, group_key=group_key, channels=listed)
            return
        if grid.tile_showing(channel) not in (None, name) or grid.refuse(channel, name):
            return  # another tile shows it, or its provider has no stream to spare
        title = str(channel.get("name") or "")
        source = item_provider.pick_source(self.catalog, channel, group_key)
        target = Target(channel, "live", title, group_key=group_key, source=source, retried=retried)
        context = ZapContext(group_key, listed, fallback=self._all_live(), source=source)
        self._tile_links[name] = target
        grid.linking(name, channel)
        self._lanes.request_side(
            target, lambda url: self._on_tile_link(name, target, context, url), play=False
        )

    def _on_tile_link(self, name: str, target: Target, context: ZapContext, url: str) -> None:
        """A tile's link came: it opens there (the newest choice for that tile only)."""
        grid, sessions, pip = self._grid, self.sessions, self._pip
        if self._tile_links.get(name) is not target:
            return  # another channel was chosen for it since, or the grid went
        del self._tile_links[name]
        if grid is None or sessions is None or pip is None or not grid.awaits(name, target.item):
            return  # the grid went (or came back) meanwhile, or the tile took another
        if name == sessions.large().name:  # it took the focus meanwhile: as a tune
            self._play_link(target, url)
            return
        item = target.item
        view = pip.view(name)  # what the controls act on once it takes the focus
        view.target, view.context, view.url, view.cid = target, context, url, None
        slot = sessions.slot(name)
        slot.group_key = target.group_key
        provider_name = item_provider.provider_name(self.catalog, item)
        limit = self._live_controls.limit(item)
        kind = classify(url, self.catalog.is_live("live"))
        if self._live_controls.through_pipeline(url, kind):
            kwargs = tune_kwargs(
                self._live_controls.live_start(target, url), self.config, disk=False
            )
            size = sessions.tile_bytes(False)
            if size is not None:
                kwargs["max_bytes"] = size
            grid.open_tile(name, item, provider=provider_name, limit=limit, start_kwargs=kwargs)
        else:
            session = slot.playback

            def play_direct() -> None:
                session.play(
                    url, kind=kind, latency=self.config.live_latency, **stream_options(item)
                )

            grid.open_tile(name, item, play_direct, provider=provider_name, limit=limit)
        self._tracks.prepare_small(slot.playback, item)  # its sound sync, kept per channel

    def _may_reopen(self, target: Target) -> bool:
        """An ended direct stream opens again (a reconnect): in the multiview only while its
        tile holds its provider's slot, or takes one within the limit; else its card says so
        and nothing opens."""
        grid = self._grid
        if grid is None or not grid.shown():
            return True
        item = target.item
        provider = item_provider.provider_name(self.catalog, item)
        if self._tuner.ensure_held(provider, self._live_controls.limit(item)):
            return True
        grid.refuse(item)  # the card: no stream to spare
        self._current, self._cid = None, None
        return False

    def restore_multiview(self) -> None:
        """View → Restore Last Multiview: the grid (shown first), with the last grid's
        channels."""
        if self._grid is None or self._closed:
            return
        if not self._grid.shown():
            self.toggle_multiview()
        self._grid.restore_last()

    def _restore_tile(self, name: str, channel: dict, provider: str, group: str) -> None:
        """A restored grid's tile: its channel opens once its provider is ready (as the last
        channel at start), saying "Connecting…" meanwhile."""
        hub = getattr(self.catalog, "catalogs", None)
        if hub is not None and not hub.is_ready(provider):
            self._waiting_tiles = [entry for entry in self._waiting_tiles if entry[0] != name]
            self._waiting_tiles.append((name, channel, provider, group))
            if self._grid is not None:
                self._grid.linking(name, channel)
            hub.ensure(provider)
            return
        self._play_restored(name, channel, group)

    def _play_restored(self, name: str, channel: dict, group: str) -> None:
        """A restored tile's channel, zapping through its group when the list has it, else
        through every channel."""
        listed = list(self.catalog.items("live", group) or []) if group else []
        if not any(ZapContext.same(channel, other) for other in listed):
            group, listed = ALL_GROUP_ID, self._all_live()
        self.play_in_tile(name, channel, group_key=group, channels=listed)

    def _on_channel_dragged(self, node: NavNode, point: QPoint) -> None:
        if self._grid is not None and not self._closed:
            self._grid.drag_over(point, node.payload)

    def _on_channel_dropped(self, node: NavNode, point: QPoint) -> None:
        """A channel row let go over a tile: it plays there (the focused one as a tune; any
        other beside it, the focus staying), and the list closes."""
        grid = self._grid
        if grid is None or self._closed:
            return
        grid.drag_ended()
        name = grid.tile_at(point)
        if name is None:
            return
        group = next((n for n in reversed(self.nav.path) if n.layer == "group"), None)
        channels = payloads(self.nav.children(group) or []) if group is not None else []
        key = group.key if group is not None else ALL_GROUP_ID
        if not any(ZapContext.same(node.payload, other) for other in channels):
            key, channels = ALL_GROUP_ID, self._all_live()  # a found channel: every one
        self.overlays.drawer.close_drawer()
        self.play_in_tile(name, node.payload, group_key=key, channels=channels)

    def _reopen_tile(self, name: str, channel: dict) -> None:
        """A tile without the focus failed on an STB portal: a fresh link, once (its portal's
        session may have expired; the grid says when)."""
        logger.info("Making the link of a tile's channel again after it failed")
        item_provider.sign_in_again(self.catalog, channel)
        self.play_in_tile(name, channel, retried=True)

    def _drop_pip_on_its_way(self) -> None:
        """The grid shows: a channel still on its way to the small picture never opens."""
        if self._pip is not None and self._pip.on_its_way():
            self._pip.close()

    def _mixer_tiles(self) -> Optional[list[tuple[str, str, str]]]:
        return self._grid.mixer_rows() if self._grid is not None else None

    def _open_pip_node(self, node: NavNode) -> None:
        if self._pip is not None:
            self._pip.open_node(node)

    def _on_large_changed(self, name: str, old_name: str) -> None:
        """A swap: the large picture's player, stream and state are slot `name`'s now, and
        slot `old_name` shows what the large picture showed."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or self._closed:
            return
        new, old = sessions.slot(name), sessions.slot(old_name)
        view = pip.take_large(
            old.name, new.name, self._current, self.zap.context, self._current_url, self._cid
        )
        shown = self._current if self._current is not None and self._current.live else None
        old.channel = shown.item if shown is not None else None  # what the small one shows
        old.group_key = shown.group_key if shown is not None else None
        self._use_slot(new)
        # A movie's resume key comes back with its slot: its position is saved again.
        self._current, self._cid, self._current_url = view.target, view.cid, view.url
        self._restore_channel()
        self._state = self._states.get(new.name, "idle")
        self._tuning.stop()
        self._autoplay.drop()  # the Next card, and its wait, were the other picture's
        self._freed_for = None  # a title on its way for the large picture was dropped
        if self._digits:
            self.zap.cancel_digits()
        context = view.context
        if context is not None and context is not self.zap.context:
            self.zap.set_context(context)
        channel = self._channel
        transport = self.overlays.transport
        transport.set_paused(self._state == "paused")
        grid = self.multiview_shown()  # a tile took the focus: the grid shows it
        if channel is not None:
            self.zap.tuned(channel)
            current = self._current
            if current is not None:
                # Recent and the next start's channel: a swap's, and a tile's taking the focus.
                self._record_watched(current, view.url)
                # The transport takes the large picture's last timeline at once; the banner
                # then announces the channel (they share the bottom of the video).
                self._live_controls.show_live_transport(current)
            self._show_banner(channel)
        elif self._current is not None:  # a movie or an episode: its own controls again
            current = self._current
            self.overlays.banner.dismiss()
            transport.set_next_available(self._autoplay.has_next(current))
            transport.show_vod(current.title, current.subtitle)
            if self._state == "ended":
                self._autoplay.on_ended(current)  # it ended small: what follows, as at its end
        elif grid:  # a tile showing nothing took the focus: the grid's controls stay
            self.overlays.banner.dismiss()
            if transport.isVisible():
                self._show_grid_transport()
        self._tracks.refresh()  # an open panel shows the large picture's sound or subtitles
        if grid:
            return  # the grid follows the focus itself (its ring, the tiles' labels)
        pip.after_swap()
        if self._tuner_of(old).idle():
            pip.close()  # the old large picture was between two channels: nothing to show

    def _use_slot(self, slot: Slot) -> None:
        """Act on `slot`'s player, live session and tuner (it is large now)."""
        tuner = slot.tuner
        if not isinstance(tuner, LiveTuner):
            raise TypeError("Each picture-in-picture slot needs a LiveTuner")
        if self._wired:
            self._connect_session(self.session, False)
        self.session, self.live, self._tuner = slot.playback, slot.live, tuner
        if self._wired:
            self._connect_session(slot.playback, True)
        self._live_controls.use_slot(tuner, slot.live, slot.playback)

    def _connect_session(self, session, connect: bool) -> None:
        for signal, slot in (
            (session.stateChanged, self._on_state),
            (session.positionChanged, self._on_position),
            (session.errorOccurred, self._on_error),
            (session.ended, self._on_ended),
        ):
            if connect:
                signal.connect(slot)
            else:
                signal.disconnect(slot)

    def _players(self) -> list[tuple[str, Any]]:
        sessions = self.sessions
        if sessions is None:
            return [("main", self.session)]
        return [(slot.name, slot.playback) for slot in sessions.slots()]

    def _tuners(self) -> list[LiveTuner]:
        sessions = self.sessions
        if sessions is None:
            return [self._tuner]
        return [self._tuner_of(slot) for slot in sessions.slots()]

    def slot_added(self, slot: Slot) -> None:
        """A multiview tile's slot was made: its player is followed as the others are (once
        wired; wiring follows every slot there is)."""
        if not self._wired or self._closed:
            return
        self._watch_player(slot.name, slot.playback)
        if self._pip is not None:
            self._pip.watch_slot(slot)
        if self._grid is not None:
            self._grid.watch_slot(slot)

    def _watch_player(self, name: str, player) -> None:
        """Follow a player's state, a title's position and its errors (it may be small)."""
        player.stateChanged.connect(partial(self._states.__setitem__, name))
        player.positionChanged.connect(partial(self._on_small_position, name))
        player.errorOccurred.connect(partial(self._on_small_error, name))

    @staticmethod
    def _tuner_of(slot: Slot) -> LiveTuner:
        tuner = slot.tuner
        assert isinstance(tuner, LiveTuner)
        return tuner

    def _sync_key(self, item: dict) -> str:
        """A channel's key for its sound sync: its provider's and its own (as live_tuning's)."""
        return channel_key(item_provider.provider_name(self.catalog, item), item)

    def _toggle_pause(self) -> None:
        self._tuner.toggle_pause()

    def _seek_ms(self, ms: int) -> None:
        self.session.seek(ms)

    # ---- catalog events -----------------------------------------------------------------------

    def _link_owner(self, item: dict) -> str:
        """The provider a link request for `item` goes to (an unstamped item: the shown one)."""
        name = provider_of(item)
        if name:
            return name
        name = self.catalog.current_provider_name()
        if name == ALL_PROVIDERS:
            name = str(getattr(self.config, "selected_provider_name", "") or "")
        return name

    def _on_links_provider_ready(self, name: str) -> None:
        """Provider `name` was set up: its catalog dropped the link results requested
        before, so its request in flight is asked again (the newest choice, unless the
        playing channel was it). Other providers' requests are not touched."""
        if self._closed:
            return
        dropped = self._lanes.provider_ready(name, self._link_owner)  # a menu's links go on
        if dropped is not None:
            self._ask_again(self._lanes.queued or dropped)
        elif self._lanes.in_flight() is None:
            # A tune that failed because this provider was loading waits queued for it (never
            # asked while another provider's tune is in flight: it follows that one).
            queued = self._lanes.queued
            if queued is not None and self._link_owner(queued.item) == name:
                self._ask_again(queued)
        if self._pip is not None:
            self._pip.provider_settled(name, self._link_owner, ask_again=True)
        waiting = [entry for entry in self._waiting_tiles if entry[2] == name]
        self._waiting_tiles = [entry for entry in self._waiting_tiles if entry[2] != name]
        for tile, channel, _provider, group in waiting:  # a restored grid's: it opens now
            if self._grid is not None and self._grid.awaits(tile, channel):
                self._play_restored(tile, channel, group)

    def _on_links_provider_failed(self, name: str, message: str) -> None:
        """Provider `name` failed: the link requests it dropped never settle, so they are
        forgotten; the queued choice of another provider goes on. When the newest choice, the
        channel waiting to open small or a menu's link was `name`'s, the failure is said once
        ("{name}: {message}", unless the flow says it: the list shows `name`); INFO, ★ and the
        guide follow what plays."""
        if self._closed:
            return
        dropped, side_lost = self._lanes.provider_failed(name, self._link_owner)
        queued = self._lanes.queued
        newest = queued or dropped
        lost = (
            newest is not None
            and not self._is_stay(newest)
            and self._link_owner(newest.item) == name
        )
        if queued is not None and self._link_owner(queued.item) == name:
            self._lanes.take_queued()
        elif queued is not None and dropped is not None:
            self._ask_again(queued)
        if self._pip is not None and self._pip.provider_settled(
            name, self._link_owner, ask_again=False
        ):
            lost = True
        for tile, channel, provider, _group in list(self._waiting_tiles):
            if provider == name:  # a restored grid's tile: its provider failed
                self._waiting_tiles.remove((tile, channel, provider, _group))
                if self._grid is not None:
                    self._grid.link_failed(tile, channel)
        if not lost and not side_lost:
            return
        if lost:
            self._forget_room(newest)
            self._give_large_back(newest)
            self._restore_channel()
        if not self._flow.shows_failure(name):
            self._error(f"{name}: {message}" if name else message)
            self._explain(name, name)

    def _ask_again(self, newest: Target) -> None:
        """A provider settled the newest choice's request: it asks for its link (again), or,
        when it is the marker that keeps the playing channel (see _tune), it is dropped."""
        if self._is_stay(newest):
            self._lanes.take_queued()
        else:
            self._request(newest)

    def _live_section_loaded(self) -> None:
        context = self.zap.context
        if context.group_key == ALL_GROUP_ID and not context.channels:
            # A search's channel tuned before the live section loaded: ↑/↓ walk it now.
            channels = self._all_live()
            self.zap.set_context(
                ZapContext(ALL_GROUP_ID, channels, fallback=channels, source=context.source)
            )

    def guides_in_use(self) -> set[str]:
        """The providers whose guides are needed now: the big and small pictures', the one
        the TV page browses, and in all mode those of the channels on screen (the drawer's
        group, the open guide's rows). Others stay unloaded: accounts hold 57,000+ channels."""
        names: set[str] = set()
        items = [self._channel, self._current.item if self._current is not None else None]
        sessions = self.sessions
        if sessions is not None and (sessions.pip_open() or sessions.pip_opening()):
            items.append(sessions.other().channel)
        if self._grid is not None:
            items.extend(self._grid.channels())  # the multiview's tiles'
        names.update(provider_of(item) for item in items if item)
        source = self.catalog.current_provider_name()
        if source != ALL_PROVIDERS:
            names.add(source)
        elif self._guide.is_open():
            names.update(provider_of(c) for c in self._guide.visible_channels())
        elif self.overlays.drawer.isVisible():
            names.update(self._drawer_providers())
        names.discard("")
        return names

    def _guide_context(self) -> ZapContext:
        """What the guide lists: the zap group's channels, but in all mode the live group the
        drawer shows when it is another one (its channels may be any provider's)."""
        context = self.zap.context
        if not self.nav.all_mode():
            return context
        group = self._drawer_group()
        if group is None or group.key == context.group_key:
            return context
        channels = self.catalog.items("live", group.key)
        if not channels:
            return context
        return ZapContext(group.key, list(channels), fallback=self._all_live())

    def _drawer_group(self) -> Optional[NavNode]:
        """The live group the drawer's path ends in (None: not in a live group)."""
        group = next((n for n in self.nav.path if n.layer == "group"), None)
        if group is None or self.nav.section_of(group) != "live":
            return None
        return group

    def _drawer_providers(self) -> set[str]:
        """The providers of the live group the drawer shows: a provider's own group, or, in a
        mixed list, those of its first rows (the drawer's scroll position is not known)."""
        group = self._drawer_group()
        if group is None:
            return set()
        provider, _ = split_group_id(group.key)
        if provider:
            return {provider}
        rows = self.catalog.items("live", group.key) or []
        return {provider_of(c) for c in rows[:_DRAWER_ROWS]}

    def _update_guides(self) -> None:
        if self._closed:
            return
        names = self.guides_in_use()
        if names != self._guides_applied:
            self._guides_applied = names
            self.catalog.use_guides(names)

    def _on_epg_ready(self) -> None:
        self.guides_changed()

    def guides_changed(self) -> None:
        """A guide loaded, or its times moved (a provider's guide time offset): the rows and
        the guide read it again (the time-shift bar's ticks do at its next timeline)."""
        if not self._closed:
            self._repaint()
            self._guide.invalidate()

    def _on_notice(self, text: str) -> None:
        if not self._closed:
            self._toast(text)

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
            # The column stands alone over the video (mockup 01-shapes A): the banner, and a
            # number being typed, give way; the rest tune announces the channel again.
            if self._digits:
                self.zap.cancel_digits()
            self.overlays.banner.dismiss()
            context = self.zap.context
            column.open(
                self._group_title(context.group_key),
                context.channels,
                context.current_index,
                self.nav.shows_provider(NavNode("group", context.group_key, "")),
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
                self.zap.close_column()  # a number takes over: the banner shows it alone
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
        if state == "playing":
            self._live_controls.on_playing()  # direct play: a held "unavailable" shows now

    def _on_position(self, position_ms: int, length_ms: int) -> None:
        if self._closed or self._current is None or self._current.live:
            return
        if self._cid is not None:  # a local file has no resume key
            try:
                self.watch.update_position(self._cid, position_ms, length_ms)
            except Exception as e:
                self._log_failure("save the playback position", e)
        self.overlays.transport.update_position(position_ms, length_ms)

    def _on_small_position(self, name: str, position_ms: int, length_ms: int) -> None:
        """A movie or an episode swapped small still plays: its position is saved under its
        own resume key (and its end marks it finished), as while it was large."""
        cid = self._pip.small_resume_key(name) if self._pip is not None else None
        if self._closed or cid is None:
            return
        try:
            self.watch.update_position(cid, position_ms, length_ms)
        except Exception as e:
            self._log_failure("save the playback position", e)

    def _on_small_error(self, name: str, message: str) -> None:
        """A movie or an episode swapped small failed to play: said as the large picture says
        it (a channel's player errors are its live session's to recover or report)."""
        target = self._pip.small_title(name) if self._pip is not None else None
        if not self._closed and target is not None:
            self._error(f"{target.title}: {message}")
            self._explain_item(target.item, target.title)

    def _on_error(self, message: str) -> None:
        if self._closed or self._tuner.running():
            return  # LiveSession recovers pipeline errors and reports the rest (failed)
        current = self._current
        if current is not None and self._live_controls.may_retry(current):
            self._live_controls.retry(current)  # STB play tokens expire: a fresh link first
            return
        title = current.title if current is not None else ""
        if current is not None and not current.live:
            self._give_small_channel_large()
        self._error(f"{title}: {message}" if title else message)
        if current is not None:
            self._explain_item(current.item, title)

    def _give_small_channel_large(self) -> None:
        """The movie or the episode playing large failed: a channel shown small beside it
        (kept small for a title from the Library) takes the big picture back, and the failed
        title closes; the caller says why."""
        sessions, pip = self.sessions, self._pip
        small = sessions.small() if sessions is not None else None
        if pip is None or small is None or small.channel is None:
            return
        if self.swap():
            pip.close()  # the failed title, small now

    def _on_ended(self) -> None:
        if self._closed or self._current is None:
            return
        current = self._current
        if current.live:
            if self._live_controls.pipeline_live():
                return  # the buffer reopens
            if self._live_controls.may_retry(current):
                self._live_controls.retry(current)  # an expired STB token can end it at once
                return
            if self._live_controls.reconnect(current):
                return  # "Reconnecting…": the same stream opens again, in the same slot
            self._toast("The stream ended")
            return
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        if not self._autoplay.on_ended(current):
            self._toast(FINISHED_TEXT)

    def _on_next_requested(self) -> None:
        """The transport's Next: what follows the movie or episode plays at once."""
        current = self._current
        if not self._closed and current is not None:
            self._autoplay.next_requested(current)

    def _still_current(self, target: Target) -> bool:
        """`target` still plays and nothing newer was chosen (autoplay may follow it)."""
        return target is self._current and not self._lanes.busy()

    # ---- subtitle files -----------------------------------------------------------------------

    def accepts_subtitles(self, paths: Sequence[str]) -> bool:
        """Whether files dragged over the window would load as subtitles of what plays."""
        return not self._closed and self._tracks.accepts_drop(paths)

    def drop_subtitles(self, paths: Sequence[str]) -> bool:
        """Files dropped on the window: their subtitle files show with what plays (large)."""
        return not self._closed and self._tracks.drop(paths)

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
        """Refresh the banner and the transport's badge when a logo they show arrives."""
        if self._closed:
            return
        # The transport shows what plays (`_current`), the banner the channel last asked for
        # (`_channel`): during a zap they differ, and a logo must sit beside its own name.
        shown = self._current
        if shown is not None and shown.live and shown.item.get("logo") == url:
            if not self.overlays.transport.badge_rect().isEmpty():
                self.overlays.transport.set_logo(self._logo(shown.item))
        banner, channel = self.overlays.banner, self._channel
        if channel is None or channel.get("logo") != url:
            return
        if banner.isVisible() and banner.info is not None and banner.info.logo is None:
            banner.show_info(self.banner_info(channel))

    def _open_drawer(self, from_column: bool = False) -> None:
        if not self.catalog.provider_names():
            self._flow.welcome()
            return
        # Where the big picture's channel was picked (after a swap, the new large one's); while
        # the fast-zap column is open the user is choosing in it, so the list opens at the zap's
        # own channel and group, as before 2.1.
        shown = self._current if self._current is not None and self._current.live else None
        if from_column or self.overlays.column.isVisible():
            shown = None
        if shown is not None:
            self.nav.reveal(
                shown.source,
                "live",
                shown.group_key or self.zap.context.group_key,
                shown.item,
            )
        else:
            self.nav.reset_to("live", self.zap.context.group_key or ALL_GROUP_ID)
        channel = shown.item if shown is not None else self._channel or self.zap.context.current()
        focus = None
        if channel:
            provider = provider_of(channel)
            focus = (provider, item_key(channel)) if provider else item_key(channel)
        self.overlays.drawer.open_drawer(focus_key=focus)
        self._update_guides()  # the drawer is visible now: its group's providers are in use

    def _open_guide(self) -> None:
        """The guide over live TV: the zap group's channels, on the one tuned last."""
        if self._on_demand():
            return  # it lists live channels
        context = self._guide_context()
        if not self._guide.open(context, self._channel or context.current(), self._all_live):
            return
        if self._digits:
            self.zap.cancel_digits()
        self.overlays.column.close()
        self.overlays.banner.dismiss()
        self.overlays.transport.dismiss()

    def _on_guide_tune(self, channel: dict, group_key: str, channels: list[dict]) -> None:
        if not self._closed:
            self.tune(channel, group_key=group_key, channels=channels)

    def _escape(self) -> None:
        overlays = self.overlays
        if overlays.drawer.isVisible():
            overlays.drawer.escape()  # clears the filter first, then closes
        elif self._autoplay.cancel():
            pass  # the Next card closed on the final frame, or the next season's wait ended
        elif self._digits:
            self.zap.cancel_digits()
            overlays.banner.dismiss()
        elif overlays.column.isVisible():
            overlays.column.close()  # the zap forgets its highlight, so no rest tune follows
        elif overlays.banner.isVisible():
            overlays.banner.dismiss()
        elif self.multiview_shown():
            self.leave_multiview()  # before fullscreen: the grid is what Esc closes first
        else:
            self.window_actions.leave_fullscreen()

    def _info(self) -> None:
        if self._on_demand():
            self._show_transport()
            return
        if self.overlays.column.isVisible():
            return  # the column names the channels; the banner would lie under it
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
        elif current is not None and not current.live:
            return  # a movie or an episode that cannot seek, or that ended
        elif self.live is not None and current is not None and current.live:
            # Played directly: there is no buffer to seek in. Say why when it was the channel's
            # own choice (it crawled through the time-shift buffer; back in a day).
            self._toast(self.live.no_rewind_reason)
        else:
            self._toast("Rewind arrives with time-shift")

    def _show_transport(self) -> None:
        target = self._current
        if self.overlays.drawer.isVisible():
            return
        if (target is None or not self._has_media()) and self.multiview_shown():
            self._show_grid_transport()  # the grid's button and layout chip stay in reach
            return
        if target is None or not self._has_media():
            return
        if self._digits:
            return  # the banner shows the number being typed
        self.overlays.banner.dismiss()
        if target.live and not self.overlays.transport.isVisible():
            self._live_controls.show_live_transport(target)
        else:
            self.overlays.transport.poke()

    def _show_grid_transport(self) -> None:
        """The player controls over a grid whose focused tile plays nothing: live TV's, named
        for the multiview, so its button and layout chip stay in reach."""
        if self._digits:
            return  # the banner shows the number being typed
        self.overlays.banner.dismiss()
        self.overlays.transport.show_live(MULTIVIEW_TITLE, "")

    def _change_volume(self, delta: int) -> None:
        self._mixer.change(delta)

    def _picture_names(self) -> tuple[str, str]:
        """The large and the small picture's channel (or movie) names, for the mixer."""
        current, pip, sessions = self._current, self._pip, self.sessions
        large = current.title if current is not None else ""
        small = sessions.small() if sessions is not None else None
        target = pip.view(small.name).target if pip is not None and small is not None else None
        return large, target.title if target is not None else ""

    def _on_audio_changed(self) -> None:
        """A level or mute changed, or the pictures swapped: the PiP label's speaker and the
        transport's follow."""
        self.overlays.transport.set_muted(self._mixer.main_muted())
        if self._pip is not None:
            self._pip.refresh_label()

    # ---- favorites and context actions --------------------------------------------------------

    def _favorite(self) -> None:
        """Ctrl+D: the guide's or the drawer's highlighted channel, else the current one.

        Nothing while a movie or an episode is current (the Library keeps those).
        """
        drawer = self.overlays.drawer
        if self._guide.is_open():
            channel = self._guide.current_channel()
        elif drawer.isVisible():
            node = drawer.highlighted()
            channel = node.payload if node is not None and is_channel(self.nav, node) else None
        elif self._on_demand():
            channel = None
        else:
            channel = self._channel or self.zap.context.current()
        if channel is not None:
            self._toggle_favorite(channel)

    def _favorite_playing(self) -> None:
        """The player controls' star: the live channel playing, never a highlighted row."""
        if self._on_demand():
            return
        channel = self._channel or self.zap.context.current()
        if channel is not None:
            self._toggle_favorite(channel)

    def _toggle_favorite(self, item: dict) -> None:
        if not favorites.favorite_key(item):
            return
        added = favorites.toggle(self.config, item)
        self.nav.changed.emit()  # the pinned Favorites group follows the list
        if self._channel is not None:  # the star of the channel playing (by name, as the list)
            self.overlays.transport.set_favorite(favorites.is_favorite(self.config, self._channel))
        if not favorites.save(self.config):
            self._toast(FAVORITES_SAVE_FAILED_TEXT, "error", ERROR_MS)
            return
        self._toast("Added to favorites" if added else "Removed from favorites")

    def _on_context(self, node: NavNode, global_pos: QPoint) -> None:
        """A right-click on a drawer row: its context menu."""
        if not self._closed:
            self._context.show(node, global_pos, self.overlays.drawer)

    def show_library_menu(
        self, item: dict, section: str, global_pos: QPoint, parent: Optional[QObject] = None
    ) -> None:
        """A right-click on a Library film ("movies") or episode ("series"): Play in VLC, Play
        in MPV or Copy stream link, as the drawer's menu offers them."""
        if not self._closed:
            target = Target(item, section, item_title(item, episode=section == "series"))
            self._context.show_target(target, global_pos, parent)

    def _on_external_requested(self, anchor: QPoint) -> None:
        """The transport's "Open in external player": what plays now, in VLC or MPV."""
        target = self._current
        if not self._closed and target is not None:
            self._context.show_players(target, anchor, self.overlays.transport)

    def _play_external(self, target: Target, url: str, launcher: Launcher) -> None:
        """The context menu's external play: the newest choice, once the in-app stream is gone."""
        if self._closed:
            return
        self._cancel_external()
        self.leave_multiview(keep_pip=False)  # an external player is outside the budget
        self._close_pip()  # an external player is outside the budget: the PiP gives way
        self._autoplay.drop()
        self._lanes.drop_tune()  # e.g. an STB retry still on its way would open in-app

        def launch() -> None:
            self._launch(target, url, launcher)
            if self._current is target:
                self._restore_channel()  # INFO, the guide and Ctrl+D follow what it plays

        # Nothing plays in the app: no provider slot is held, for a stream or a file alike.
        self._tuner.open(self._guarded(target, launch))

    # ---- state helpers ------------------------------------------------------------------------

    def _has_media(self) -> bool:
        return self._state in _ACTIVE_STATES or self._tuner.opening()

    def _playing_live(self) -> Optional[Target]:
        current = self._current
        return current if current is not None and current.live and self._has_media() else None

    def _on_demand(self) -> bool:
        """A movie or an episode is current: playing, or ended on its final frame."""
        current = self._current
        if current is None or current.live:
            return False
        if self._has_media() or self._state in _ON_DEMAND_STOPS:
            return True
        return self._autoplay.card_open()

    def _toast(self, text: str, kind: str = "info", ms: int = INFO_MS) -> None:
        """The player's toast; while the Library shows, the Library's (the player's page, in
        its corner or hidden, shows no toast then): e.g. a playback error in the corner."""
        toast = self.overlays.library_toast
        if toast is None or not self._flow.in_library():
            toast = self.overlays.toast
        self._toasts += 1
        self._toasted_at = time.monotonic()
        toast.show_message(text, kind, ms)

    # ---- why a failure happened ----------------------------------------------------------------

    def _explain_item(self, item: dict, title: str = "") -> None:
        """`_explain` for the provider of `item` (a channel, a film or an episode)."""
        if self._account_checks is not None:
            self._explain(item_provider.provider_name(self.catalog, item), title)

    def _explain(self, provider: str, title: str = "") -> None:
        """The failure just toasted happened at `provider`: its account is asked why (on a
        worker, at most once a minute per provider). A plain reason replaces the toast, as
        "{title}: {reason}", while that toast still shows; an account that is fine leaves it."""
        checks = self._account_checks
        if checks is None or self._closed or not provider or provider == ALL_PROVIDERS:
            return
        try:
            entry = self.catalog.provider_entry(provider)
        except KeyError, AttributeError:
            return  # gone from the list (or a catalog that cannot say)
        if not isinstance(entry, dict) or not entry.get("name"):
            return
        answer = partial(self._on_reason, self._toasts, title, provider)
        checks.explain(entry, answer, session=self._portal_session(provider))

    def _portal_session(self, provider: str) -> Optional[dict]:
        """The signed-in session of `provider`'s portal (its headers, token among them), when
        its catalog has one: the check asks with it instead of taking a new token, which
        would cost the stream that session plays."""
        hub = getattr(self.catalog, "catalogs", None)
        try:
            if hub is None or provider not in hub.created():
                return None
            manager = hub.catalog(provider).provider_manager
        except Exception:
            return None
        headers = getattr(manager, "headers", None)
        if not getattr(manager, "token", "") or not isinstance(headers, dict) or not headers:
            return None
        return dict(headers)

    def _on_reason(self, shown: int, title: str, provider: str, reason: Optional[Reason]) -> None:
        if self._closed or reason is None or not reason.plain:
            return
        if shown != self._toasts or time.monotonic() - self._toasted_at > ERROR_MS / 1000:
            return  # another message came, or the failure's toast has gone: it is old news
        text: Optional[str] = reason.text
        own = self.budget.in_use(provider) if self.budget is not None else 0
        if reason.kind == LIMIT and own and reason.active is not None and own >= reason.active:
            # The streams in use are QiTV's own (its other picture): no other device's.
            text = own_streams_text(reason.limit)
        if text:
            self._error(f"{title}: {text}" if title else text)

    def _notify(self, text: str, kind: str) -> None:
        """A toast for the shell's helpers: errors stay up longer."""
        self._toast(text, kind, ERROR_MS if kind == "error" else INFO_MS)

    def _error(self, text: str) -> None:
        """Toast an error; addresses and query strings never reach the toast or any log."""
        clean = scrub(text)
        logger.info("Error shown: %s", clean)
        self._toast(clean, "error", ERROR_MS)

    def _menu_notify(self, text: str, kind: str) -> None:
        """A context menu's message: on the Library's toast while the Library shows (the
        player's page, with its toast, is hidden then), else on the player's."""
        self._notify(text, kind)

    def _menu_error(self, text: str) -> None:
        """An external play or a context menu's link failed: _error's text, on _menu_notify's
        toast."""
        clean = scrub(text)
        logger.info("Error shown: %s", clean)
        self._menu_notify(clean, "error")

    @staticmethod
    def _log_failure(what: str, error: Exception) -> None:
        logger.warning("Could not %s: %s (%s)", what, scrub(error), type(error).__name__)
        # The frames only: the exception's text can carry a provider URL with credentials.
        logger.debug("Could not %s:\n%s", what, safe_traceback(error))
