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
the in-app stream first, as an external pick does. Only this controller holds the LiveTuner
and the session: the others call back play_vod, tune or the external play.

Picture in picture (spec §7, Phase 4) is PipControls' (ui/pip_controls.py) over the window's
SessionManager: each player has its own LiveTuner, and `session`, `live` and the tuner here
are always the large picture's, so zapping, the banner, the transport, seek and Go live act
on it. A swap re-points them (`_use_slot`) and trades what each picture showed (its target
and zap group); nothing is stopped or restarted. A zap onto the channel the small picture
shows swaps instead of opening it twice. Movies, episodes, files and the Library close the
small picture first: it is for live TV.

Provider setups (the loading and welcome hints, the last channel, the drawer on an idle player)
are ProviderFlow's (ui/provider_flow.py). Opening the window's Library (spec §5.4, R5) stops
what plays (`stop_for_library`); back to live TV (`resume_live`) tunes the last channel. The
window wires this controller before the Library's: a Library play that waited for its provider
runs from the Library's providerReady slot, after this controller settled the provider change.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
import logging
from pathlib import Path
import re
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QAbstractScrollArea

from services import external_players, favorites
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme, now_next
from services.live.live_session import UNAVAILABLE, LiveSession
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode, item_key, item_title
from services.next_episode import play_context
from services.redact import safe_traceback
from services.sessions import SessionManager, Slot
from services.vlc_profile import classify
from services.zapping import ZapContext
from ui.autoplay import FINISHED_TEXT, Autoplay
from ui.context_actions import ContextActions, Launcher, is_channel
from ui.guide_controller import GuideController
from ui.keymap import Action, as_int
from ui.link_lanes import LinkLanes, Target
from ui.live_controls import LiveControls
from ui.live_tuner import LiveTuner
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
from ui.pip_controls import OPEN_PIP_FIRST, PipControls
from ui.pointer import PointerInput
from ui.provider_flow import ProviderFlow

logger = logging.getLogger(__name__)

INFO_MS = 3000
ERROR_MS = 6000
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
TUNING_KEEP_MS = 3000  # under the banner's 4 s: it is shown again while a link resolves
EXTERNAL_SETTLE_MS = 1000  # the zap burst window: an external player opens once zapping stops
FAVORITES_SAVE_FAILED_TEXT = "Couldn't save your favorites; they will be lost when QiTV closes"
_ACTIVE_STATES = frozenset({"opening", "buffering", "playing", "paused"})
_ON_DEMAND_STOPS = frozenset({"ended", "error"})  # a movie or episode still current after them
_VOLUME_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.MetaModifier.value
)
_PINNED_TITLES = {FAVORITES_GROUP: "Favorites", RECENT_GROUP: "Recent"}
_ADDRESS = re.compile(r"\b[a-z][a-z0-9+.\-]*://\S*", re.IGNORECASE)
_QUERY = re.compile(r"\S*\?\S*=\S*")  # a query string carries "=": "Who?Me" stays


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
    guide: Guide
    next_card: NextCard
    welcome: WelcomeCard
    pip_label: Any = None  # the PipLabel (Phase 4), with picture in picture only
    mixer: Any = None  # the volume mixer (Phase 4)


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
        parent: Optional[QObject] = None,
    ):
        """With `sessions` (picture in picture), its large slot's player, live session and
        tuner stand for `session`, `live` and `budget`."""
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
            show_transport=self._show_transport,
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
            settle_links=self._settle_links,
            request=self._request,
            has_media=self._has_media,
            links_busy=lambda: self._lanes.busy(),
            drop_links=lambda: self._drop_links(ask_again=False),
            tune=lambda channel, key, channels: self.tune(
                channel, group_key=key, channels=channels
            ),
            open_drawer=self._open_drawer,
            live_section_loaded=self._live_section_loaded,
            error=self._error,
            drop_autoplay=lambda: self._autoplay.drop(),
            autoplay_hint=lambda: self._autoplay.hint(),
            parent=self,
        )
        self._current: Optional[Target] = None  # handed to the player
        self._current_url = ""  # its link (a swap records the picture made large)
        self._cid: Optional[str] = None  # resume key of the VOD being played
        self._channel: Optional[dict] = None  # the channel tuned last, for INFO and the drawer
        self._digits = ""  # number entry under way
        self._state = "idle"
        self._audio_id: Optional[int] = None
        self._subtitle_id: Optional[int] = None
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
            notify=self._notify,
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
            audio_changed=self._refresh_pip_label,
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
                live_start=self._live_controls.live_start,
                previous=self._previous_channel,
                large_channel=lambda: self._channel,
                vod=self.vod_current,
                closed=lambda: self._closed,
                toast=self._toast,
                swap=self.swap,
                mixer=self._mixer.open_small,
                parent=self,
            )
            self._pip.changed.connect(self._mixer.refresh)
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
        self._flow.start()

    def _wire(self) -> None:
        catalog, zap, session = self.catalog, self.zap, self.session
        column, drawer = self.overlays.column, self.overlays.drawer
        transport = self.overlays.transport
        self._flow.wire()
        self._lanes.tuneReady.connect(self._on_tune_ready)
        self._lanes.tuneFailed.connect(self._on_tune_failed)
        self._lanes.tuneDropped.connect(self._on_tune_dropped)
        self._lanes.sideFailed.connect(self._on_side_failed)
        catalog.epgReady.connect(self._on_epg_ready)
        self.nav.notice.connect(self._on_notice)
        zap.tuneRequested.connect(self._on_zap_tune)
        zap.columnRequested.connect(self._on_column_requested)
        zap.highlightChanged.connect(self._on_highlight)
        zap.digitsChanged.connect(self._on_digits)
        zap.numberNotFound.connect(self._on_number_not_found)
        self._connect_session(session, True)
        for name, player in self._players():
            player.stateChanged.connect(partial(self._states.__setitem__, name))
        if self.sessions is not None:
            self.sessions.largeChanged.connect(self._on_large_changed)
        column.closed.connect(zap.close_column)
        column.rowClicked.connect(self._on_column_row)
        drawer.playRequested.connect(self.play_item)
        drawer.contextRequested.connect(self._on_context)
        self._guide.tuneRequested.connect(self._on_guide_tune)
        transport.nextRequested.connect(self._on_next_requested)
        transport.playPauseRequested.connect(self._toggle_pause)
        transport.seekRequested.connect(self._seek_ms)
        transport.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.miniPlayerRequested.connect(self.window_actions.toggle_mini_player)
        transport.audioRequested.connect(self._cycle_audio)
        transport.subtitleRequested.connect(self._cycle_subtitles)
        self._live_controls.wire()
        self._mixer.wire()
        if self._pip is not None:
            self._pip.wire()
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
        self._tuning.stop()
        self._cancel_external()
        self._flow.shutdown()
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
            self._close_pip()  # the small picture first, then the large one
            self._tuner.shutdown()
        except Exception as e:
            self._log_failure("stop playback", e)

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        self._flow.select_provider(name, force_refresh=force_refresh)

    def stop_live(self) -> None:
        """Stop what plays (window close, self-update): its slot goes back once released."""
        self._resume = self._current if self._has_media() else None
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
        """The window opens the Library (spec §5.4, R5): stop what plays, waiting for nothing.

        A link still on its way never opens, the guide, the Next card and the other player
        overlays close, and the stream's budget slot goes back once LiveTuner's release gate
        has let go of it. Until resume_live(), tune_live() or play_vod(), a provider change
        neither tunes the last channel nor opens the drawer.
        """
        if self._closed:
            return
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
        try:
            self.watch.flush()  # a movie's last position
        except Exception as e:
            self._log_failure("save the watch state", e)
        self._close_pip()  # both pictures stop
        self._live_controls.stop_tune()
        self._current, self._cid = None, None

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
        target = Target({"name": title, "cmd": url}, "movies", title)
        self._close_pip()  # picture in picture is for live TV
        self._cancel_external()
        self._lanes.supersede()
        self._lanes.drop_tune()  # a channel or movie still on its way never opens over it
        self._autoplay.drop()
        self._audio_id = self._subtitle_id = None
        self._live_controls.new_target()
        self._current, self._cid = target, None

        session = self.session  # the large picture's, whatever swaps before it opens

        def open_file() -> None:
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
        """The keymap context for the next key: the open guide or drawer takes the keys."""
        if self._guide.is_open():
            return "guide"
        return "drawer" if self.overlays.drawer.isVisible() else "player"

    def overlay_open(self) -> bool:
        """Whether Esc would close something first: the drawer, the guide, the Next card (or the
        wait for the next season), a number being typed, the zap column or the banner."""
        overlays = self.overlays
        return bool(
            self._guide.is_open()
            or self._mixer.visible()
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

    def handle(self, action: Action, payload: str = "") -> None:
        if self._closed:
            return
        if self._guide.is_open() and self._guide.handle(action, payload):
            return
        drawer = self.overlays.drawer
        if not drawer.isVisible() and self._mixer.handle(action):
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
            self.overlays.column.close()
            self._open_drawer()
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
        elif action is Action.PIP:
            self._pip_key()
        elif action is Action.SWAP:
            self._swap_key()
        elif action is Action.MUTE:
            self._mixer.mute()
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
        if group_key != context.group_key or context.index_of(channel) < 0:
            # Numbers reach every live channel, not only the group's (TV numbers are global).
            self.zap.set_context(ZapContext(group_key, channels, fallback=self._all_live()))
        playing = self._playing_live()
        stay = playing is not None and ZapContext.same(channel, playing.item)
        if not stay and self._pip_takes(channel, group_key, channels):
            return
        if not from_zap and not stay:
            self.zap.note_request(channel)  # ↑/↓ move on from it while its link is pending
        self.overlays.column.close()
        self._channel = channel
        self._show_banner(channel)
        if stay:
            # The channel already playing was chosen again: never restart it, and drop a
            # pending choice when it settles (the "stay" marker is the playing target).
            if self._lanes.busy():
                self._lanes.queued = playing
            self.zap.tuned(channel)
            return
        title = str(channel.get("name") or "")
        self._request(Target(channel, "live", title, group_key=group_key, zapped=from_zap))
        self._tuning.start(TUNING_KEEP_MS)

    def play_item(self, node: NavNode, siblings: list[NavNode], parent: NavNode) -> None:
        """A drawer row was chosen. Only channels play from the drawer: Movies and Series
        open the Library, whose titles and episodes play through play_vod()."""
        if self._closed:
            return
        if node.layer != "item" or self.nav.section_of(node) != "live":
            logger.debug("Not playable from the drawer: %s", node)
            return
        # The whole group, not just the rows the drawer's filter shows: ↑/↓ walk the group.
        nodes = self.nav.children(parent) or siblings
        channels = [n.payload for n in nodes if isinstance(n.payload, dict)]
        self.tune(node.payload, group_key=parent.key, channels=channels)
        self.overlays.drawer.close_drawer()

    def play_vod(self, item: dict, kind: str, context: Optional[dict] = None) -> None:
        """Play a movie or an episode where it stopped (the drawer and the Library).

        `kind` is "movie" or "episode". `context` is the Library's play context: start_over
        plays from the start; an episode's series and season, or a movie's genre_items, give
        Next and autoplay what follows. Its link is the tune slot's, and it opens once what
        plays now (a live channel too) is released.
        """
        if self._closed:
            return
        self._flow.leave_library()
        context = play_context(context)
        episode = kind == "episode"
        series = context["series"]
        self._close_pip()  # picture in picture is for live TV
        self._lanes.supersede()  # a newer choice than an external play waiting for its link
        self._request(
            Target(
                item,
                "series" if episode else "movies",
                item_title(item, episode=episode),
                subtitle=item_title(series) if episode and isinstance(series, dict) else "",
                context=context,
                start_over=bool(context["start_over"]),
            )
        )

    def _all_live(self) -> list[dict]:
        return list(self.catalog.items("live", ALL_GROUP_ID) or [])

    def _request(self, target: Target) -> None:
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
            # one queued meanwhile is newer than the target that just failed.
            if self._lanes.queued is None:
                self._lanes.queued = target
            return
        if self._follow_queue(target):
            return  # the failed target was superseded: no error for it
        self._restore_channel()
        self._error(f"{target.title}: {message}")

    def _on_tune_dropped(self, _target: Target) -> None:
        """A tune overtaken by an external play: the channel still playing stays the channel."""
        if self._closed:
            return
        self._restore_channel()  # INFO, the guide and Ctrl+D follow what plays
        playing = self._playing_live()
        if playing is not None:
            self.zap.tuned(playing.item)  # ↑/↓ move on from it, not from the dropped one

    def _on_side_failed(self, target: Target, message: str) -> None:
        """A context menu's link failed (or what used it did)."""
        if not self._closed:
            self._error(f"{target.title}: {message}")

    def _keep_tuning_banner(self) -> None:
        """Show the banner again while the link of the channel it announces resolves."""
        pending = self._lanes.pending()
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

    def _deliver(self, target: Target, url: str) -> None:
        item = target.item
        launcher = self._external_launcher()
        # Direct plays book the slot under the catalog's provider, as pipeline tunes do.
        provider_name = self.catalog.current_provider_name()
        self._audio_id = self._subtitle_id = None
        self._live_controls.new_target()  # the old channel's ticks and held notice
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
            if self._live_controls.through_pipeline(url, kind):
                if not self._live_controls.tune_pipeline(target, url):
                    return  # over the provider's stream limit: nothing opens
                self._current, self._cid = target, None
                self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            else:
                self._current, self._cid = target, None
                session = self.session  # the large picture's, whatever swaps before it opens

                def play_direct() -> None:
                    session.play(
                        url,
                        kind=kind,
                        latency=self.config.live_latency,
                        user_agent=item.get("user_agent"),
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
                session.play(url, kind=classify(url, False), start_ms=start)
                self._reset_speed(session)

            self._tuner.open(self._guarded(target, open_vod), direct=True, provider=provider_name)
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
            self._error(first.strip() or "The external player could not start")
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
        self._record_watched(target, url)

    def _record_watched(self, target: Target, url: str) -> None:
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

    def _swap_key(self) -> None:
        sessions = self.sessions
        if sessions is None:
            return
        if not sessions.pip_open():
            self._toast(OPEN_PIP_FIRST)
            return
        self.swap()

    def open_pip(self, channel: dict, *, group_key: str, channels: list[dict]) -> None:
        """Open `channel` small (the drawer's menu or Ctrl/⌘+P), or show it in the PiP."""
        if self._pip is not None and not self._closed:
            context = ZapContext(group_key, channels, fallback=self._all_live())
            self._pip.open_channel(channel, context)

    def _pip_key(self) -> None:
        """P: the drawer's highlighted channel small (Ctrl/⌘+P there), else the previous one,
        or close the small picture."""
        pip, drawer = self._pip, self.overlays.drawer
        if pip is None:
            return
        node = drawer.highlighted() if drawer.isVisible() else None
        if node is not None and is_channel(self.nav, node):
            self._open_pip_node(node)
            drawer.close_drawer()
        else:
            pip.toggle_previous()

    def _open_pip_node(self, node: NavNode) -> None:
        """A drawer row's channel small, with its group for ↑/↓ once it is swapped in."""
        group = next((n for n in reversed(self.nav.path) if n.layer == "group"), None)
        nodes = self.nav.children(group) if group is not None else []
        channels = [n.payload for n in nodes if isinstance(n.payload, dict)] or [node.payload]
        key = group.key if group is not None else ALL_GROUP_ID
        self.open_pip(node.payload, group_key=key, channels=channels)

    def _previous_channel(self) -> tuple[Optional[dict], Optional[ZapContext]]:
        """The channel tuned before the large picture's, with the zap group to walk."""
        channel = self.zap.previous_channel()
        if channel is None:
            return None, None
        context = self.zap.context
        if context.index_of(channel) < 0:
            channels = self._all_live()
            context = ZapContext(ALL_GROUP_ID, channels, fallback=channels)
        return channel, ZapContext(context.group_key, context.channels, fallback=context.fallback)

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
        pip.view(small.name).context = ZapContext(group_key, channels, fallback=self._all_live())
        self.overlays.column.close()
        if self.swap():
            return True
        pip.close()  # it cannot swap now: the PiP closes and the channel opens large
        return False

    def _close_pip(self) -> None:
        if self._pip is not None:
            self._pip.close()

    def _on_large_changed(self, name: str) -> None:
        """A swap: the large picture's player, stream and state are the other slot's now."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or self._closed:
            return
        new, old = sessions.large(), sessions.other()
        view = pip.take_large(
            old.name, new.name, self._current, self.zap.context, self._current_url
        )
        shown = self._current if self._current is not None and self._current.live else None
        old.channel = shown.item if shown is not None else None  # what the small one shows
        old.group_key = shown.group_key if shown is not None else None
        self._use_slot(new)
        self._current, self._cid, self._current_url = view.target, None, view.url
        self._restore_channel()
        self._state = self._states.get(new.name, "idle")
        self._audio_id = self._subtitle_id = None
        self._tuning.stop()
        if self._digits:
            self.zap.cancel_digits()
        context = view.context
        if context is not None and context is not self.zap.context:
            self.zap.set_context(context)
        channel = self._channel
        transport = self.overlays.transport
        transport.set_paused(self._state == "paused")
        if channel is not None:
            self.zap.tuned(channel)
            current = self._current
            if current is not None:
                self._record_watched(current, view.url)  # Recent, the next start's channel
                # The transport takes the large picture's last timeline at once; the banner
                # then announces the channel (they share the bottom of the video).
                self._live_controls.show_live_transport(current)
            self._show_banner(channel)
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
        return [(slot.name, slot.playback) for slot in (sessions.large(), sessions.other())]

    def _tuners(self) -> list[LiveTuner]:
        sessions = self.sessions
        if sessions is None:
            return [self._tuner]
        return [self._tuner_of(slot) for slot in (sessions.large(), sessions.other())]

    @staticmethod
    def _tuner_of(slot: Slot) -> LiveTuner:
        tuner = slot.tuner
        assert isinstance(tuner, LiveTuner)
        return tuner

    def _toggle_pause(self) -> None:
        self._tuner.toggle_pause()

    def _seek_ms(self, ms: int) -> None:
        self.session.seek(ms)

    # ---- catalog events -----------------------------------------------------------------------

    def _settle_links(self, provider_changed: bool) -> Optional[Target]:
        """A provider became ready: the newest choice to ask again, its links dropped.

        A setup makes the catalog drop link results requested before it, so a request still
        in flight may never settle. A changed provider's channels are the old one's: nothing
        is asked again, the zapping, the Next card and the guide start over, and the small
        picture closes.
        """
        pending = self._lanes.pending()
        if self._is_stay(pending):
            pending = None  # the playing channel was the newest choice: nothing to ask again
        self._drop_links(ask_again=not provider_changed)
        if provider_changed:
            self.zap.reset()
            self._channel = None
            pending = None
            self._autoplay.drop()
            self._guide.close()  # its channels are the old provider's
            self._close_pip()  # the small picture's channel and stream are too
        return pending

    def _drop_links(self, *, ask_again: bool) -> None:
        """Forget every link request; the PiP's asks again when `ask_again`."""
        self._lanes.drop()
        if self._pip is not None:
            self._pip.links_dropped(ask_again)

    def _live_section_loaded(self) -> None:
        context = self.zap.context
        if context.group_key == ALL_GROUP_ID and not context.channels:
            # A search's channel tuned before the live section loaded: ↑/↓ walk it now.
            channels = self._all_live()
            self.zap.set_context(ZapContext(ALL_GROUP_ID, channels, fallback=channels))

    def _on_epg_ready(self) -> None:
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
            if self._live_controls.pipeline_live():
                return  # the buffer reopens
            if self._live_controls.may_retry(current):
                self._live_controls.retry(current)  # an expired STB token can end it at once
                return
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
            self._flow.welcome()
            return
        self.nav.reset_to("live", self.zap.context.group_key or ALL_GROUP_ID)
        channel = self._channel or self.zap.context.current()
        self.overlays.drawer.open_drawer(focus_key=item_key(channel) if channel else None)

    def _open_guide(self) -> None:
        """The guide over live TV: the zap group's channels, on the one tuned last."""
        if self._on_demand():
            return  # it lists live channels
        context = self.zap.context
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
        else:
            self.window_actions.leave_fullscreen()

    def _info(self) -> None:
        if self._on_demand():
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
        elif current is not None and not current.live:
            return  # a movie or an episode that cannot seek, or that ended
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
            self._live_controls.show_live_transport(target)
        else:
            self.overlays.transport.poke()

    def _change_volume(self, delta: int) -> None:
        self._mixer.change(delta)

    def _picture_names(self) -> tuple[str, str]:
        """The large and the small picture's channel (or movie) names, for the mixer."""
        current, pip, sessions = self._current, self._pip, self.sessions
        large = current.title if current is not None else ""
        small = sessions.small() if sessions is not None else None
        target = pip.view(small.name).target if pip is not None and small is not None else None
        return large, target.title if target is not None else ""

    def _refresh_pip_label(self) -> None:
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

    def _toggle_favorite(self, item: dict) -> None:
        if not favorites.favorite_key(item):
            return
        added = favorites.toggle(self.config, item)
        self.nav.changed.emit()  # the pinned Favorites group follows the list
        if not favorites.save(self.config):
            self._toast(FAVORITES_SAVE_FAILED_TEXT, "error", ERROR_MS)
            return
        self._toast("Added to favorites" if added else "Removed from favorites")

    def _on_context(self, node: NavNode, global_pos: QPoint) -> None:
        """A right-click on a drawer row: its context menu."""
        if not self._closed:
            self._context.show(node, global_pos, self.overlays.drawer)

    def _play_external(self, target: Target, url: str, launcher: Launcher) -> None:
        """The context menu's external play: the newest choice, once the in-app stream is gone."""
        if self._closed:
            return
        self._cancel_external()
        self._autoplay.drop()
        self._lanes.drop_tune()  # e.g. an STB retry still on its way would open in-app

        def launch() -> None:
            self._launch(target, url, launcher)
            if self._current is target:
                self._restore_channel()  # INFO, the guide and Ctrl+D follow what it plays

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
        self.overlays.toast.show_message(text, kind, ms)

    def _notify(self, text: str, kind: str) -> None:
        """A toast for the shell's helpers: errors stay up longer."""
        self._toast(text, kind, ERROR_MS if kind == "error" else INFO_MS)

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
