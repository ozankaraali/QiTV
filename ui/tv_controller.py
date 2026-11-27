"""The TV shell's behaviour: wires the catalog, navigation, zapping and playback to the overlays.

Link requests are serialised in LinkLanes (ui/link_lanes.py): at most one is in flight (STB
portals mint a link per request and providers limit connections). A tune that arrives
meanwhile replaces the single queued target, and only the newest target ever plays.

Live channels on http(s) play through the time-shift pipeline (`LiveSession`) when the window
provides one. `LiveTuner` opens every provider stream (pipeline, direct, movie, external player)
only once the previous one is released. The time-shift side (pipeline tunes and their budget
slot, the transport's time-shift controls, LiveSession's reports, the STB fresh-link retry) is
LiveControls' (ui/live_controls.py).

In external-player mode a zap step launches the player only once the zapping settles. While a
link kept in Network streams plays, channel up/down steps to the next or previous kept link
(the network player's `step`), round as channels go.

Movies and episodes play through `play_vod` (the drawer and the Library), resuming where they
stopped; Autoplay (ui/autoplay.py) offers what follows one that ended. A file opened in QiTV
whose name is an episode has its folder listed on a thread as it opens (ui/episode_listing.py):
the same show's next episode there follows it, as a file (spec §8). While a movie or an
episode is current, also once it ended, the zap keys stay on it: ↑/↓ and the wheel change the
volume, and number entry, the previous channel and the guide wait for live TV.

The guide (ui/guide_controller.py) takes the keys while it is open. The drawer's context menu
(ui/context_actions.py) asks for links on LinkLanes' side lane; its "Play in VLC/MPV" releases
the in-app stream first, as an external pick does. The transport's "Open in external player"
offers the same two plays for what plays now, and a right-click in the Library offers them
with the link (`show_library_menu`); while the Library shows, the menus' messages go to its
toast. Only this controller holds the LiveTuner and the session: the others call back
play_vod, tune or the external play.

The Picture panel (2.3, ui/picture_controls.py) acts on the large picture as the Sound panel
does: its aspect ratio, kept per channel, the statistics, and a sleep timer that
stops playback the Library's way (`stop_for_sleep`) and leaves fullscreen.

Picture in picture (spec §7) is PipControls' (ui/pip_controls.py) over the window's
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

Sound with no picture (spec §10, Plan 4) plays on the audio screen: a song at once as it plays
(the transport as its dock), another file once its media has no video track, a live channel
once it has played NO_PICTURE_S with none (radio: live TV's controls stay). It goes when a
picture shows, at a new target (but a song after a song: no black between them) and at a stop.
A song's album (its folder's same album, ui/song_jobs.py) plays on through an AudioQueue
(ui/audio_queue.py): shuffle and repeat, the dock's and the media keys' previous and next. A
playlist of songs (play_list) is an album named after it, in its order; a mixed one plays each
entry on its own screen, a song's next entry after it with no card.

Provider setups (the loading and welcome hints, the last channel, the drawer on an idle player)
are ProviderFlow's (ui/provider_flow.py). Opening the window's Library (spec §5.4) keeps what
plays going in a corner of it (`enter_library_playing`; `stop_for_library` is the corner's ×, or
nothing playing); back to live TV (`resume_live`) tunes the last channel when nothing plays. The
window wires this controller before the Library's: a Library play that waited for its provider
runs from the Library's providerReady slot, after this controller settled the provider change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
import logging
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, Optional, cast

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea

from services import external_players, favorites, free_radio, recording, recording_schedule
from services.album_listing import unread_track
from services.budgeted_stream import HeldSlot
from services.casting import CastDevices
from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE, shown_label
from services.catchup import archive_of
from services.catchup_memory import CatchupMemory
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme, now_next
from services.failure_reasons import LIMIT, Reason, own_streams_text, shown_message
from services.film_relay import FilmRelay, relayable
from services.i18n import N_, tr, translate
from services.jobs import JobRunner
from services.live import parts as live_parts
from services.live.live_session import LiveSession
from services.live.tuning import channel_key
from services.live_sites import forget_kept_listings, media_address, site_media, site_page
from services.local_episodes import next_in_folder, parse_episode
from services.media_reporter import MediaReporter
from services.media_server import Quality
from services.navigation import (
    FAVORITES_GROUP,
    LINK_LAYER,
    RECENT_GROUP,
    NavNode,
    item_key,
    item_title,
    payloads,
    recent_channel,
)
from services.network_streams import (
    DOWN,
    FAILED,
    FILM,
    LIVE,
    TIMED_OUT,
    failure_reason,
    is_link,
    is_offline,
    is_site_failure,
    names_its_channel,
    shown_reason,
    site_failure,
)
from services.next_episode import LOCAL_NEXT, play_context
from services.open_paths import PLAYLIST_EXTENSIONS, is_music, is_song, is_video
from services.picture import SOURCE_DIRECT, ChannelPictures
from services.playback import NO_VLC_MESSAGE
from services.provider_ids import (
    ALL_PROVIDERS,
    NETWORK_PROVIDER,
    mixed_group_id,
    provider_of,
    split_group_id,
    with_provider,
)
from services.provider_types import is_media_server, is_plex
from services.redact import safe_traceback, scrub
from services.sessions import SessionManager, Slot, records
from services.song_facts import BRAND_COLOURS
from services.sound_sync import SoundSync
from services.track_memory import TrackMemory
from services.vlc_profile import DEFAULT_USER_AGENT, classify
from services.watch_state import local_key
from services.zapping import ZapContext
from ui import item_provider
from ui.audio_queue import REPEAT_MODES, AudioQueue
from ui.autoplay import FINISHED_TEXT, Autoplay
from ui.captions_texts import NOT_AVAILABLE
from ui.cast_controls import CastControls
from ui.context_actions import OPEN_PIP_TEXT, ContextActions, Launcher, is_channel
from ui.episode_listing import FolderListing
from ui.grid_controls import LEAVE_FIRST, GridControls
from ui.guide_controller import GuideController
from ui.keymap import Action, as_int
from ui.link_lanes import LinkLanes, RecordingJob, SiteJob, Target
from ui.live_controls import LiveControls, stream_options
from ui.live_tuner import LiveTuner, start_kwargs as tune_kwargs
from ui.mixer_controls import MixerControls
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import Drawer
from ui.overlays.guide import Guide
from ui.overlays.next_card import NextCard
from ui.overlays.painting import fmt_duration
from ui.overlays.popup_menu import Where
from ui.overlays.stopped_notice import STOPPED
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import BannerInfo, ZapBanner
from ui.picture_controls import PictureControls
from ui.pip_controls import PipControls
from ui.player_keys import PlayerKeys
from ui.pointer import PointerInput
from ui.provider_flow import ProviderFlow
from ui.quality_controls import CONVERSION_REFUSED, SIZE_WHILE_RECORDING, QualityControls
from ui.song_jobs import AlbumJob, CoverJob, cover_colours

# i18n: a toast: the multiview cannot open while a picture is cast to a TV
MULTIVIEW_WHILE_CASTING = N_("Stop casting to show the multiview")
from ui.record_guard import RecordGuard
from ui.remote_typing import field_of
from ui.settings_controller import MOUSE_FORWARD, SettingsController
from ui.settings_layer import SettingsLayer
from ui.stream_question import ScheduledRecording, StreamQuestion, quit_text
from ui.subtitle_search import OnlineSetup, OnlineSubtitles
from ui.track_controls import TrackControls

logger = logging.getLogger(__name__)

INFO_MS = 3000
ERROR_MS = 6000
# i18n: a toast: hiding a channel or a group needs a PIN first (Settings → Parental controls)
SET_PIN_FIRST = N_("Set a PIN first: Settings → Parental controls.")
# i18n: a toast: the channel or group is hidden while QiTV is locked; Settings shows it again
HIDDEN_TEXT = N_("Hidden while locked. Settings → Parental controls shows it again.")
# i18n: a toast after "Hide group" (2.9.1): no PIN, nothing locked
TIDIED_TEXT = N_("Hidden. Settings → Providers → Hidden groups shows it again.")
RECORDING_MS = 5000  # "Recording saved in <folder>: <name>" (a name to read)
VOLUME_STEP = 5
SEEK_STEP_MS = 10_000
TUNING_KEEP_MS = 3000  # under the banner's 4 s: it is shown again while a link resolves
TUNING_CAP_S = 20.0  # and while the channel opens, up to this long after the tune
EXTERNAL_SETTLE_MS = 1000  # the zap burst window: an external player opens once zapping stops
FAVORITES_SAVE_FAILED_TEXT = N_("Couldn't save your favorites; they will be lost when QiTV closes")
_ACTIVE_STATES = frozenset({"opening", "buffering", "playing", "paused"})
_ON_DEMAND_STOPS = frozenset({"ended", "error"})  # a movie or episode still current after them
_VOLUME_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier.value | Qt.KeyboardModifier.MetaModifier.value
)
_DRAWER_ROWS = 40  # rows of a mixed list whose providers load their guides
# The audio screen (spec §10). A live stream may list its sound before its picture: a channel is
# radio once it has played this long with no picture listed. Whether what plays has a picture is
# looked at this often while a channel plays (it may come and go), or until a file's is known.
NO_PICTURE_S = 2.0
SOUND_POLL_MS = 500
FILE_TRACKS_WAIT_S = 10.0  # a file whose tracks libVLC hasn't listed by then is left as it is
# The controls' title over a focused tile with no channel (the feature's name).
MULTIVIEW_TITLE = N_("Multiview")  # i18n: the player controls' title over the multiview's grid
# The fast-zap column's titles of the pinned groups.
_PINNED_TITLES = {
    FAVORITES_GROUP: N_("Favorites"),  # i18n: the group of your favorite channels (a title)
    RECENT_GROUP: N_("Recent"),  # i18n: the group of the channels watched lately (a title)
}


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
    pip_label: Any = None  # the PipLabel, with picture in picture only
    mixer: Any = None  # the volume mixer
    sound_panel: Any = None  # the Sound panel above ♫ (2.2)
    subtitle_panel: Any = None  # the Subtitles panel above its button (2.2)
    subtitle_search: Any = None  # its OpenSubtitles search page (2.2)
    picture_panel: Any = None  # the Picture panel above its button (2.3)
    stats: Any = None  # the statistics over the picture (2.3)
    sleep_notice: Any = None  # the sleep timer's last minute (2.3)
    layout_row: Any = None  # the multiview's layouts above its layout chip
    library_toast: Any = None  # the Library page's toast: the menus' messages while it shows
    pairing: Any = None  # the phone remote's pairing screen (2.5)
    back_notice: Any = None  # 2.6: Resume, where a channel come back to was left
    question: Any = None  # 2.6: the QuestionCard: stop a recording for a stream?
    library_question: Any = None  # its twin on the Library page
    rec_sign: Any = None  # 2.6: the REC sign of recordings in the background
    stopped_notice: Any = None  # 2.6: "Stopped", with Play again, after Stop
    library_rec_sign: Any = None  # its twin on the Library page
    cast_notice: Any = None  # 2.8: "Playing on <device>", with Stop casting
    # "The stream ended · Trying again in 0:25", with Try now (`services.revive`)
    retry_notice: Any = None
    quality_panel: Any = None  # 2.8: the ⚙ quality menu's panel above its button
    quality_question: Any = None  # 2.8: its own QuestionCard (convert on the server?)
    captions: Any = None  # 2.9: the AI captions over the picture (a CaptionsOverlay)
    audio_screen: Any = None  # a song's or a radio channel's screen (AudioScreen; None: none)
    more_menu: Any = None  # the audio dock's ⋯ menu (MoreMenu)


def _own_link(item: dict) -> Optional[str]:
    """A link of Network streams' own address (its items carry it); None for any other."""
    if not is_link(item):
        return None
    return str(item.get("cmd") or "").strip() or None


@dataclass
class _LocalList:
    """A playlist of local files that plays (TVController.play_list): its name, its files with
    their sizes (None: unknown), and the entry that plays, as its index and its target. A list
    of songs only (`songs`) is an album: its entries' Tracks, in its order (their files' names
    until their tags are read), and the queue it plays through while one of them plays."""

    name: str
    entries: tuple[str, ...]
    sizes: tuple[Optional[int], ...]
    index: int = 0
    target: Optional[Target] = None
    songs: bool = False
    tracks: list[dict] = field(default_factory=list)
    queue: Optional[AudioQueue] = None


class TVController(QObject):
    # 2.6: QiTV's question for phone `sid` (its text, the answers' words), and that it is gone.
    phoneAsked = Signal(str, str, str, str)
    phoneSettled = Signal(str)
    # An outside player (Play in VLC, a phone's Watch) took over what the large picture played:
    # the phones watching before let go (one screen at a time, as when the TV plays again).
    handedOn = Signal()
    # Plan 4: a link of Network streams played (its item, LIVE or FILM, its address), or did
    # not, or went for good (and why: an English text, `network_streams.failure_reason`).
    linkPlayed = Signal(object, str, str)
    linkFailed = Signal(object, str, str, str)
    # 2.15: a site's video or clip (its page's address) answered with its name ("title · Twitch").
    linkNamed = Signal(str, str)

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
        track_memory: Optional[TrackMemory] = None,
        channel_pictures: Optional[ChannelPictures] = None,
        catchup_memory: Optional[CatchupMemory] = None,
        pick_subtitle_file: Optional[Callable[[str], str]] = None,
        account_checks=None,
        online_subtitles: Optional[OnlineSetup] = None,
        grid_frame=None,
        grid_tiles=None,
        host=None,
        make_slot: Optional[Callable[[str], Optional[Slot]]] = None,
        cast_devices: Optional[CastDevices] = None,
        media_reporter: Optional[MediaReporter] = None,
        jobs: Optional[JobRunner] = None,
        parent: Optional[QObject] = None,
    ):
        """With `sessions` (picture in picture), its large slot's player, live session and
        tuner stand for `session`, `live` and `budget`. `sound_sync` keeps each channel's
        sound sync, `track_memory` each title's tracks (2.9.1) and `channel_pictures` its
        aspect ratio (None: for this run only),
        `catchup_memory` whether each channel's provider
        archive answered (None: alike); `pick_subtitle_file(folder)` asks for a subtitle
        file (the Subtitles panel's "Load a file…"); with `online_subtitles` (and the overlays'
        `subtitle_search`) the panel searches OpenSubtitles too. `account_checks` (an
        AccountChecks) says why a failure happened (None: the failure's own words only).
        With `grid_frame` and `make_slot` (a tile's player, made by its slot's name) the
        multiview's grid can show: `grid_tiles` draws over it, `host` is the overlay host
        (both None-able in tests). `jobs` runs what the controller asks off its thread (a
        site's channel page asked for its stream; None: a runner of its own)."""
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
        self._stopped: Optional[Target] = None  # 2.6: what Stop stopped, for play_again()
        self._page: Optional[str] = None  # the window's page ("player", "library", "corner")
        self._external_pending: Optional[tuple[Target, str]] = None
        # 2.8.12: what a phone's Watch plays holds one of its provider's streams, as the large
        # picture's own play would, until the TV plays again, another player shows large,
        # another outside player takes over or QiTV stops: the phone is QiTV's own (a scheduled
        # recording nor a picture in picture takes that stream meanwhile). A player outside
        # QiTV (VLC, mpv) holds none: QiTV can't tell what it does.
        self._phone_slot = HeldSlot(self.budget, self._tuner.purpose)
        self._external_settle = QTimer(self, singleShot=True)
        self._external_settle.timeout.connect(self._launch_settled)
        self._started = False
        self._wired = False
        self._closed = False
        # 2.8: a media server's conversion chosen in the quality menu, by provider: its titles
        # play converted until QiTV closes (a version as it is ends it).
        self._media_quality: dict[str, Any] = {}
        # 2.14: the title (provider, id) a refused conversion was said for, once (review M1).
        self._refusal_said: Optional[tuple[str, str]] = None
        self._guides_applied: Optional[set[str]] = None  # what the hub was last told
        self._phone_guides: set[str] = set()  # the providers the phone remote lists
        # Plan 4: a link of Network streams' address as it plays now (`set_links`; None: an
        # item its provider makes the link of), and the link whose first picture was heard.
        self._links: Callable[[dict], Optional[str]] = _own_link
        self._link_heard: Optional[Target] = None
        # Whose stream a link is now (`set_link_owner`; None: no link), asked as it plays.
        self._whose: Callable[[dict], Optional[str]] = lambda item: None
        # Its link kept under an id plays, by the network player (`set_link_player`).
        self._play_kept: Callable[[str], bool] = lambda key: False
        # A kept link as it plays, by its id, without playing it (`set_link_item`).
        self._link_item: Callable[[str], Optional[tuple[dict, str]]] = lambda key: None
        # Channel up/down while a kept link plays: its neighbour there (`set_link_stepper`).
        self._step_link: Callable[[dict, int], bool] = lambda item, delta: False
        # A link plays while no provider is configured: held as a file opened in QiTV is (the
        # welcome card waits) until it stops, ends or fails (_hold_for_link); and whether a
        # file's hold (one playing, or looked at) is under it, the file's to end.
        self._link_hold = False
        self._file_under_link = False
        self._lanes = LinkLanes(catalog, self)
        self._lanes.known = self._network_url  # no provider is asked for a link's address
        # A site's channel page (Kick, Twitch) is asked for its stream on `jobs` each time it
        # plays; a newer tune stops the ask the tune in flight waits for (`_request`), and
        # QiTV's closing stops them all.
        self._jobs = jobs if jobs is not None else JobRunner(self)
        self._lanes.resolver = self._resolve_site
        self._site_stops: set[threading.Event] = set()
        # 2.15: (the page, its kept listing's address) of a Twitch recording still being made
        self._kept_listing: Optional[tuple[str, str]] = None
        self._tune_stop: Optional[threading.Event] = None
        self._asking_tune = False
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
            noted=lambda path, item, title, start: self.note_recording(path, item, title, start),
            toast=self._toast,
            error=self._error,
            request=self._request,
            restore_channel=self._restore_channel,
            logo=self._logo,
            show_transport=self._show_transport,
            explain=self._explain_item,
            may_reopen=self._may_reopen,
            memory=catchup_memory,
            prepare=lambda session, target: self._prepare(session, target, None),
            back_notice=lambda: self.overlays.back_notice,
            archive_allowed=self._archive_allowed,
            beside_recording=self._archive_beside_recording,
            borrow=lambda item, provider, purpose: self.lend_scheduled(item, provider, purpose),
            gave_up=self._fail_focused_tile,
            on_sizes=self._offer_live_quality,
            retry_notice=lambda: self.overlays.retry_notice,
            link=lambda item: self._network_url(item) is not None,
            link_failed=self._on_link_failed,
            site_page=self._site_page,
            reask=self._reask_site,
            waits_for_live=self._waits_for_live,
            recording_of=self._site_recording,
            parent=self,
        )
        # Cards over the TV of their own (File → Open Link…'s, plan 4; add_card).
        self._cards: list[Any] = []
        # 2.15: the phones' Watches asked for (play_elsewhere's target), with what hears why
        # one failed (a site's channel that isn't live waits for it on the phone).
        self._elsewhere_failed: list[tuple[Target, Callable[[Target, str], Optional[bool]]]] = []
        # Provider setups: the loading and welcome hints, the last channel, the Library's
        # hold on live TV. What opens by itself (the list, the welcome card) waits while
        # Settings or a card over the TV shows: either has the keys (final review M1).
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
            drop_autoplay=self._drop_card,
            autoplay_hint=lambda: self._autoplay.hint(),
            explain=self._explain,
            settings_shown=lambda: self._settings_shown() or self._shown_card() is not None,
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
        # The video file whose first "playing" AI captions heard (_video_file_playing)
        self._video_played: Optional[Target] = None
        self._channel: Optional[dict] = None  # the channel tuned last, for INFO and the drawer
        self._digits = ""  # number entry under way
        self._state = "idle"
        self._tuning_since = 0.0  # when the banner's keep-alive started (TUNING_CAP_S)
        self._guide = GuideController(overlays.guide, parent=self)
        # A file's next episode in its folder (spec §8): the folder is listed on a thread as
        # the episode opens; only the newest listing, for the file that still plays, counts.
        self._listing = FolderListing(self)
        self._listing.listed.connect(self._on_folder_listed)
        self._listing_generation = 0
        self._listing_for: Optional[tuple[Target, str]] = None  # the file listed for, its path
        # A playlist of local files (spec §11): each entry's next is the list's, never its
        # folder's; it is followed while one of its entries is what plays.
        self._list: Optional[_LocalList] = None
        # The audio screen (spec §10, Plan 4): what it is for (`_sound`: a song, a file with no
        # picture, a radio channel; the large picture's), shown or about to be, and its kind:
        # "song" (a music file: shown at its first `playing`), "file" (another local file:
        # shown once it plays with no video track), "radio" (a channel: once it has played
        # NO_PICTURE_S with none). It goes when a picture shows, at a new target, at a stop.
        self._sound: Optional[Target] = None
        self._sound_kind = ""
        self._sound_shown = False
        self._sound_in_list = False  # a playlist's song: its list's next, not an album's
        self._sound_path = ""  # its file's path, as play_file was given it
        self._sound_played: Optional[Target] = None  # whose first `playing` the screen heard
        self._silent_since: Optional[float] = None  # a channel with no picture listed, since
        self._sound_since = 0.0  # when a file's tracks were first looked for
        self._sound_clock: Callable[[], float] = time.monotonic
        self._stage_for_words = False  # Words turned on from the screen: its Stage (no pick)
        self._radio_texts: tuple[str, str] = ("", "")  # a radio channel's lines, as shown
        # The song the screen shows (its title, artist and album) and for whom: the system's
        # media panel has them while that is still what plays (panel_facts).
        self._song_facts: Optional[tuple[Target, str, str, str]] = None
        self._sound_poll = QTimer(self, interval=SOUND_POLL_MS)
        self._sound_poll.timeout.connect(self._sound_watch)
        # A song's album (its folder's same album, read on a thread: one listing for the album,
        # its next track takes the list it has) and its cover; only the newest answer, for the
        # song that still plays, counts (as the episode listing's).
        self._queue: Optional[AudioQueue] = None
        self._album_job = AlbumJob(self)
        self._album_job.read.connect(self._on_album_read)
        self._album_generation = 0
        self._album_for: Optional[tuple[Target, str]] = None  # the song listed for, its path
        # The track a song that couldn't be played skipped to (item 4): its own failure before
        # it plays is a second in a row, and ends the album.
        self._skipped_to: Optional[Target] = None
        # A playlist of songs' tags, read by name on a thread of its own (an album opened
        # meanwhile never cuts it short); only the newest list's answer counts.
        self._list_job = AlbumJob(self)
        self._list_job.read.connect(self._on_list_read)
        self._list_generation = 0
        self._shuffle_rng = random.Random()  # a shuffled list of songs' first song
        self._cover_job = CoverJob(self)
        self._cover_job.ready.connect(self._on_cover_ready)
        self._cover_generation = 0
        self._cover_for: Optional[Target] = None
        self._autoplay = Autoplay(
            catalog=catalog,
            config=config,
            card=overlays.next_card,
            play=self._play_following,
            is_current=self._still_current,
            external_mode=lambda: self._external_launcher() is not None,
            notify=self._notify,
            set_hint=self._flow.wait_hint,
            show_transport=overlays.transport.poke,
            declined=self._release_file_hold,
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
            forget_recent=self._forget_recent,
            clear_recents=self._clear_recents,
            hide=self._hide_from_menu,  # 2.8: parental controls, once their PIN is set
            may_hide=lambda: self.parental is not None and self.parental.has_pin(),
            tidy=self._tidy_from_menu,  # 2.9.1: out of the lists, no PIN
            record_at=lambda: self.record_at,
            link=lambda item: self._network_url(item) is not None,
            site_page_of=self._site_link,
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
        # Each player's live channel and the key it opened with: the channel's sound sync
        # and shape are kept under that key while it plays there.
        self._player_keys = PlayerKeys(self._sync_key)
        # The sound's and the subtitles' tracks and sync (their panels, J/K and Shift+J/K),
        # subtitle files; a channel's sound sync is kept.
        self._tracks = TrackControls(
            sound_panel=overlays.sound_panel,
            subtitle_panel=overlays.subtitle_panel,
            pick_file=pick_subtitle_file,
            config=config,
            online=self._online,
            # 2.9: AI captions' rows, once the window has its CaptionsController.
            captions=lambda: getattr(window_actions, "captions", None),
            transport=overlays.transport,
            session=lambda: self.session,
            current=lambda: self._current,
            playing=self._has_media,
            keys=self._player_keys,
            store=sound_sync if sound_sync is not None else SoundSync(None),
            memory=track_memory,
            toast=self._toast,
            show_transport=self._show_transport,
            parent=self,
        )
        # The picture's aspect ratio (kept per channel), the statistics and the
        # sleep timer: the Picture panel (2.3).
        self._picture = PictureControls(
            panel=overlays.picture_panel,
            transport=overlays.transport,
            stats=overlays.stats,
            notice=overlays.sleep_notice,
            session=lambda: self.session,
            current=lambda: self._current,
            playing=self._has_media,
            players=lambda: [player for _name, player in self._players()],
            keys=self._player_keys,
            store=channel_pictures if channel_pictures is not None else ChannelPictures(None),
            config=config,
            toast=self._toast,
            programme_left=self._programme_left,
            live_info=self._live_info,
            stop=self.stop_for_sleep,
            show_transport=self._show_transport,
            audio_screen=lambda: self._screen() if self.sound_shown() else None,
            parent=self,
        )
        # 2.8: the ⚙ quality menu: a media server's versions and conversions.
        self._quality: Optional[QualityControls] = None
        if overlays.quality_panel is not None and overlays.quality_question is not None:
            self._quality = QualityControls(
                panel=overlays.quality_panel,
                transport=overlays.transport,
                card=overlays.quality_question,
                current=lambda: self._current,
                menu=self._media_menu,
                choose=self.change_quality,
                live_menu=self.live_menu,
                choose_live=self.change_live_quality,
                parent=self,
            )
        # Settings in the drawer (the window's, set_settings): what gives way to it, what it
        # gives back, its keys before the list's (ui/settings_layer.py).
        self.settings_layer = SettingsLayer(
            overlays=overlays,
            nav=nav,
            catalog=catalog,
            flow=self._flow,
            guide=self._guide,
            quality=self._quality,
            closed=lambda: self._closed,
            close_online=lambda: self._close_online(),
            update_guides=lambda: self._update_guides(),
            parent=self,
        )
        # 2.6: before a recording is stopped for a stream, the question.
        # 2.8: a Jellyfin or Emby server hears where its title plays (None: none told).
        self._media = media_reporter
        # 2.8: the providers scheduled recordings record from (their guides stay loaded).
        self.extra_guides: Callable[[], set[str]] = set
        # 2.9.7: the guides a page beside the TV shows (the Recordings home's rows)
        self.view_guides: Callable[[], set[str]] = set
        # 2.8: what records on schedule now (RecordingScheduler.recording_now) and its Stop.
        self.scheduled_now: Callable[[], list[tuple[str, str, float, dict]]] = list
        self.stop_scheduled: Callable[[str], None] = lambda name: None
        # 2.9.7: a scheduled recording's session to watch with no stream of its own
        # (RecordingScheduler.lend and lendable; none while QiTV records nothing on schedule).
        self.lend_scheduled: Callable[[dict, str, str], Any] = lambda item, provider, use: None
        # 2.9.7: a recording started with R, for the recordings index: (its file, the
        # channel, the programme on now, its start, unix); the window's
        self.note_recording: Callable[[str, dict, str, float], None] = lambda *_args: None
        # 2.9.7: a recording ended (R, its stream, its limit): the index keeps where
        self.note_recording_end: Callable[[str], None] = lambda _path: None
        # 2.9.7: the user stops a player's recording: a scheduled one hosted there is done
        # (the scheduler's `stopped_by_user`), not a broken stream to record again
        self.recording_user_stop: Callable[[Any], None] = lambda _live: None
        # 2.9.7: the player a scheduled recording records on (by its REC name), if one does
        self.scheduled_host: Callable[[str], Optional[str]] = lambda _name: None
        self.lendable_scheduled: Callable[[dict, str], bool] = lambda item, provider: False
        # 2.8: "Record at a time…" for a channel (the window's, once QiTV records)
        self.record_at: Optional[Callable[[dict], None]] = None
        # 2.8: parental controls (services.parental.Parental; None: none): what they hide never
        # enters the history.
        self.parental: Optional[Any] = None
        self.hidden_groups: Optional[Any] = None  # 2.9.1: the window's (services.hidden_groups)
        # 2.8: casting the large picture to a Chromecast (none without libVLC's renderers).
        self._cast: Optional[CastControls] = None
        if cast_devices is not None:
            self._cast = CastControls(
                devices=cast_devices,
                session=lambda: self.session,
                live=lambda: self.sessions.large().live if self.sessions is not None else None,
                current=lambda: self._current,
                one_picture=self._one_picture_for_cast,
                transport=overlays.transport,
                notice=overlays.cast_notice,
                toast=self._toast,
                parent=self,
            )
        self._question = StreamQuestion(
            budget=self.budget,
            recordings=self._all_recordings,  # 2.8: those on schedule too
            large=lambda: self.sessions.large() if self.sessions is not None else None,
            card=self._question_card,
            stop=self._stop_for_stream,
            closed=lambda: self._closed,
            parent=self,
        )
        self._question.asked.connect(self.phoneAsked)
        self._question.settled.connect(self.phoneSettled)
        # 2.6: a provider that keeps only the newest stream: the recording wins.
        self._guard: Optional[RecordGuard] = None
        if sessions is not None:
            self._guard = RecordGuard(
                self.budget,
                sessions.slots,
                limit_of=lambda slot: self._live_controls.limit(slot.channel or {}),
                stop=self._stop_for_guard,
                toast=lambda text: self._toast(text, "error", ERROR_MS),
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
                sound=self._prepare_small,
                explain=self._explain_item,
                needs_stream=self._ask_for_stream,
                borrow=lambda item, provider, purpose: self.lend_scheduled(item, provider, purpose),
                owned=self._owned,
                started=self._count_start,
                choose=self._choose_small,
                hidden=self._hidden_now,
                title_gone=self._small_title_gone,
                site_page=self._site_page,
                parent=self,
            )
            self._pip.changed.connect(self._mixer.refresh)
            self._pip.changed.connect(self._update_guides)
        # The multiview's grid: its tiles are slots, the focused one the large.
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
                forget=self._forget_tile,
                pending=self._focused_link,
                layouts=overlays.layout_row,
                tile_action=self._on_tile_control,
                show_controls=self._show_transport,
                confirm=self._confirm_tile,
                site_page=self._site_page,
                parent=self,
            )
            self._grid.changed.connect(self._mixer.refresh)
            self._grid.changed.connect(self._update_guides)
        # The links of tiles opened beside the focused one, on the side lane (one at a time).
        self._tile_links: dict[str, Target] = {}
        # The channel list was opened for the small picture (P with no previous channel): the
        # channel picked there opens small.
        self._choosing_small = False
        # A restored grid's tiles waiting for their provider's setup: (tile, channel,
        # provider, group).
        self._waiting_tiles: list[tuple[str, dict, str, str]] = []
        self._column_wheel = PointerInput(self)
        # While a live link resolves, the banner is shown again before it fades.
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
        self._lanes.linkDropped.connect(self._link_dropped)
        self._lanes.sideFailed.connect(self._on_side_failed)
        catalog.epgReady.connect(self._on_epg_ready)
        catalog.providerReady.connect(lambda *_: self._update_guides())
        self.nav.changed.connect(self._update_guides)
        self.overlays.guide.rowsMoved.connect(self._update_guides)
        self.overlays.guide.closed.connect(self._update_guides)
        self.overlays.drawer.closed.connect(self._update_guides)
        self.overlays.drawer.closed.connect(self._end_choosing_small)
        self.nav.notice.connect(self._on_notice)
        zap.tuneRequested.connect(self._on_zap_tune)
        zap.columnRequested.connect(self._on_column_requested)
        zap.highlightChanged.connect(self._on_highlight)
        zap.digitsChanged.connect(self._on_digits)
        zap.numberNotFound.connect(self._on_number_not_found)
        self._connect_session(session, True)
        for name, player in self._players():
            self._watch_player(name, player)
        for live in self._lives():
            self._watch_recordings(live)
        if self.sessions is not None and self._guard is not None:
            for slot in self.sessions.slots():
                self._guard.watch_slot(slot)
        if self.sessions is not None:
            self.sessions.largeChanged.connect(self._on_large_changed)
            self.sessions.largeChanged.connect(lambda *_: self._update_guides())
            self.sessions.pipStarted.connect(self._on_pip_started)
            self.sessions.pipStateChanged.connect(self._on_pip_state)
            self.sessions.slotState.connect(self._on_slot_state)
            self.sessions.setAside.connect(self._on_set_aside)
            self.sessions.broughtBack.connect(self._on_brought_back)
            self.sessions.backgroundChanged.connect(self._refresh_rec_sign)
        for sign in (self.overlays.rec_sign, self.overlays.library_rec_sign):
            if sign is not None:
                sign.goBack.connect(self.go_back_to_recording)
                sign.stop.connect(self.stop_background_recording)
        column.closed.connect(zap.close_column)
        column.rowClicked.connect(self._on_column_row)
        drawer.playRequested.connect(self.play_item)
        drawer.channelFound.connect(self.play_found)
        drawer.contextRequested.connect(self._on_context)
        set_link_item = getattr(drawer, "set_link_item", None)
        if set_link_item is not None:  # its link rows' menus and drags, as the links play
            set_link_item(lambda key: self._link_item(key))
        self._guide.tuneRequested.connect(self._on_guide_tune)
        self._guide.catchupRequested.connect(self._on_guide_catchup)
        self.overlays.guide.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        guide_menu = getattr(self.overlays.guide, "contextRequested", None)
        if guide_menu is not None:  # 2.9.3: a right-click on a channel of the guide
            guide_menu.connect(self._on_guide_context)
        transport.nextRequested.connect(self._on_next_requested)
        transport.playPauseRequested.connect(self._toggle_pause)
        transport.stopRequested.connect(self.stop)
        if self.overlays.stopped_notice is not None:
            self.overlays.stopped_notice.keepWatching.connect(self.play_again)
        transport.seekRequested.connect(self._seek_ms)
        transport.fullscreenRequested.connect(self.window_actions.toggle_fullscreen)
        transport.miniPlayerRequested.connect(self.window_actions.toggle_mini_player)
        transport.externalRequested.connect(self._on_external_requested)
        cast_requested = getattr(transport, "castRequested", None)
        if self._cast is not None and cast_requested is not None:
            cast_requested.connect(self._on_cast_requested)
        # 2.10: the speaker's mixer holds the controls up, as the track panels do: the pointer on
        # the mixer (above the controls) left the controls idle, they hid, and the mixer they
        # carried dropped and stayed (hovered)
        transport.volumeRequested.connect(self._open_mixer)
        mixer_closed = getattr(self.overlays.mixer, "closed", None)
        if mixer_closed is not None:
            mixer_closed.connect(self._on_mixer_closed)
        # The buttons for the player's keys (every key has a control on screen) act as the keys.
        transport.guideRequested.connect(partial(self.handle, Action.GUIDE))
        transport.previousRequested.connect(partial(self.handle, Action.PREVIOUS))
        transport.infoRequested.connect(partial(self.handle, Action.INFO))
        transport.favoriteRequested.connect(self._favorite_playing)
        # The audio dock's own buttons (Plan 4): the album's tracks, shuffle, repeat, the volume.
        docked: tuple[tuple[str, Callable[..., None]], ...] = (
            ("prevTrackRequested", self._previous_track),
            ("nextTrackRequested", self._next_track),
            ("shuffleRequested", self._toggle_shuffle),
            ("repeatRequested", self._cycle_repeat),
            ("volumeSetRequested", self._mixer.set_main_level),
        )
        for name, handler in docked:
            signal = getattr(transport, name, None)
            if signal is not None:
                signal.connect(handler)
        screen = self.overlays.audio_screen
        if screen is not None:
            screen.viewPicked.connect(self._on_view_picked)
            screen.trackPicked.connect(self._on_track_picked)
            screen.wordsRequested.connect(self._on_words_requested)
            screen.upNextRequested.connect(lambda: screen.set_view("album"))  # not a pick
        self._live_controls.wire()  # its record button too (2.2)
        self._mixer.wire()
        self._tracks.wire()
        self._picture.wire()
        if self._quality is not None:
            self._quality.wire()
        if self._pip is not None:
            self._pip.wire()
        self._on_audio_changed()  # the speakers show the remembered levels from the start
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
        self._forget_kept_listings()  # 2.15: a recording's kept listing goes with QiTV
        self._live_controls.stop_waiting()  # a site's channel waited for is asked no more
        for stop in self._site_stops:
            stop.set()  # a site still asked answers no one: its ask ends
        self._autoplay.close()
        self._sound_poll.stop()
        if self._online is not None:
            self._online.shutdown()  # a sign-in ends, never holding up the quit
        self._tuning.stop()
        self._picture.shutdown()  # the sleep timer ends unsaid
        if self._cast is not None:
            self._cast.shutdown()  # no more looking for devices (2.8)
        if self._media is not None:
            self._media.shutdown()  # a server's title stops where it was (2.8)
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

    def set_up_early(self) -> None:
        """The window began the start's setup as it was built: ProviderFlow.set_up_early."""
        self._flow.set_up_early()

    def hold_for_file(self, on: bool) -> None:
        """A file opened in QiTV is on its way or plays (`on`): nothing opens over it (see
        ProviderFlow.hold_for_file), nor does the Next card's episode (it would count as a
        choice made since: TVWindow.file_gives_way). The controller lets go when the file
        ends (after the Next card of its folder's next episode, unless that plays), is stopped,
        fails or gives way to another title (_before_new_target). While a link held for plays
        (_hold_for_link), a file's release from outside (a subtitle dropped, a file that
        turned out gone or gave way) keeps the hold: the link's own end lets go."""
        if on:
            self._autoplay.drop()
            self._file_under_link = self._link_hold  # a file's hold beside the link's
        elif self._link_hold:
            self._file_under_link = False  # what held is the link's alone from now on
            return
        self._flow.hold_for_file(on)

    def hold_for_read(self) -> None:
        """A start's playlist is read (Ruling P4-3): the start waits for it, without spending
        it (ProviderFlow.hold_for_read), until read_over."""
        self._flow.hold_for_read()

    def read_over(self, *, as_file: bool) -> bool:
        """The start's playlist was read. `as_file` (a list of files, one HLS stream, one with
        none of its files there, one that couldn't be read): the start is held as a film's
        (hold_for_file); else (a provider's list, a newer choice) it starts as any start. True
        when a read held the start and it is now held as a file's; False when none did."""
        if not self._flow.reading():
            return False
        if as_file:
            self.hold_for_file(True)
            return True
        self._flow.read_over()
        return False

    def when_idle(self, callback: Callable[[], None]) -> None:
        """`callback` now, or once the file held for is over."""
        self._flow.when_idle(callback)

    def stop_live(self) -> None:
        """Stop what plays (window close, self-update): its slot goes back once released.

        2.8: every recording ends first (its file saved), as Quit's question ends them, so its
        player parks as any other and its disk buffer is kept for the next helper (a
        self-update closed it)."""
        self._phone_slot.give_back()
        self._count_start()  # a stop: nothing waiting for a stream opens after it
        recording = [slot.live for slot in self._recordings()]
        large = self.live
        if self.sessions is None and large is not None and self._large_records():
            recording.append(large)
        for live in recording:
            path = live.recording_path()
            if path:  # 2.9.7: its end may come after QiTV is gone: its tile's length is now
                self.note_recording_end(path)
            live.stop_recording()
        self._resume = self._current if self._has_media() else None
        self.leave_multiview(keep_pip=False)
        if self._pip is not None:
            self._pip.close(keep_recording=False)
        self._live_controls.stop_tune()
        if self.sessions is not None:
            self.sessions.stop_background()  # 2.6: recordings stop (their files are saved)

    def resume_stopped(self) -> None:
        """Play again what stop_live() stopped (a self-update that could not launch).

        With a fresh link: a channel tunes again, a movie or episode reopens at the position
        the watch state saved.
        """
        target, self._resume = self._resume, None
        if not self._closed and target is not None and target is self._current:
            again = replace(target, zapped=False, retried=False, start_over=False)
            again = replace(again, archive_at=None, archive_until=None)  # live
            self._request(again, chosen_now=False)  # what played: no newer choice

    def stop_for_library(self) -> None:
        """Stop what plays, waiting for nothing: the corner player's ×, or the Library opened
        with nothing playing (spec §5.4; what plays goes on in the corner otherwise).

        A link still on its way never opens, the guide, the Next card and the other player
        overlays close, and the stream's budget slot goes back once LiveTuner's release gate
        has let go of it. Until resume_live(), tune_live() or play_vod(), a provider change
        neither tunes the last channel nor opens the drawer.
        """
        if self._media is not None:
            self._media.stopped()  # 2.8: a server's title stopped where it was
        if self._closed:
            return
        self.leave_multiview(keep_pip=False)
        self._flow.enter_library()
        self._stop_all()

    def stop_for_sleep(self) -> None:
        """The sleep timer ended (2.3): what plays stops as for the Library (each stream
        closed, its budget slot back once released) and fullscreen ends; nothing plays by
        itself until the viewer chooses something."""
        if self._media is not None:
            self._media.stopped()  # 2.8: a server's title stopped where it was
        if self._closed:
            return
        self.leave_multiview(keep_pip=False)
        self._flow.drop_autotune()  # a provider still setting up tunes nothing
        self.window_actions.leave_fullscreen()
        self._stop_all()

    def _stop_all(self) -> None:
        """Stop what plays: no link on its way opens, the player's overlays close, a movie's
        position is saved, both pictures stop."""
        stopped = self._current
        self._count_start()  # a stop (Stop, the Library's, the sleep timer's)
        self._cancel_external()
        self._lanes.supersede()  # an external play waiting for its link no longer plays
        self._lanes.drop_tune()  # a link on its way never opens
        if self._tune_stop is not None:
            self._tune_stop.set()  # (nor is a site asked on for it: Task 6 M6)
            self._tune_stop = None
        self._autoplay.drop()  # the Next card, and a wait for the next season
        self._guide.close()
        self._tuning.stop()
        if self._digits:
            self.zap.cancel_digits()
        overlays = self.overlays
        overlays.column.close()
        overlays.drawer.close_drawer()
        self.close_settings()
        overlays.banner.dismiss()
        overlays.transport.dismiss()
        self._tracks.close()
        self._picture.close()
        self._close_online()
        self.cancel_cards()  # Open Link…'s
        try:
            self.watch.flush()  # a movie's last position
        except Exception as e:
            self._log_failure("save the watch state", e)
        self._close_pip()  # both pictures stop
        self._set_recording_aside()  # 2.6: a recording records on in the background
        self._live_controls.stop_tune()
        self._current, self._cid = None, None
        self._end_sound()  # the song's screen and its dock (dismiss() keeps the dock)
        self._opening_over()
        if stopped is not None and stopped.local:
            # A file stopped (not one still looked at: it plays once found, held for).
            self._flow.hold_for_file(False)
        self._let_go_of_link()  # a link playing or on its way: nothing of it opens now

    def playing(self) -> bool:
        """Something plays, or its stream is opening; or the channel plays small while the
        title it went small for is on its way."""
        return not self._closed and (self._has_media() or self._title_on_its_way())

    def showing_here(self) -> bool:
        """The TV's own player has a stream (opening, playing or paused), not one handed on."""
        return not self._closed and self._state in _ACTIVE_STATES

    def _title_on_its_way(self) -> bool:
        sessions = self.sessions
        return self._freed_for is not None and sessions is not None and sessions.pip_open()

    def enter_library_playing(self) -> None:
        """The window opens the Library with the player in a corner (2.1): what plays keeps
        playing and nothing stops.

        The drawer, Settings, the zap column and banner, the controls, the mixer, the guide, a
        number being typed and the Next card close (the overlays are gone from the corner
        player). Until leave_library_playing(), a provider change neither tunes the last
        channel nor opens the drawer, as in the Library.
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
        self.close_settings()
        overlays.banner.dismiss()
        overlays.transport.dismiss()
        self._mixer.hide()
        self._tracks.close()
        self._picture.close()
        self._close_online()
        self.cancel_cards()  # Open Link…'s
        self._autoplay.drop()  # the Next card, and a wait for the next season

    def leave_library_playing(self) -> None:
        """The player page is back from the corner: live TV and the drawer act as before."""
        if not self._closed:
            self._flow.leave_library()

    def main_muted(self) -> bool:
        """Whether the large picture is muted."""
        return self._mixer.main_muted()

    def main_level(self) -> int:
        """The large picture's volume (0-100)."""
        return self._mixer.main_level()

    def now_playing(self) -> Optional[Target]:
        """What the large picture plays or opens (None: nothing), for the control port."""
        return self._current

    def resume_live(self) -> None:
        """Back to live TV from the Library: the last live channel again, else the drawer (see
        ProviderFlow.resume_live)."""
        if not self._closed:
            self._flow.resume_live()

    def tune_live(
        self, channel: dict, group_key: str = ALL_GROUP_ID, channels: Optional[list] = None
    ) -> None:
        """A live channel from the Library's search (↑/↓ then walk every channel: group "*") or
        from the phone remote's Guide (its group's channels)."""
        self._flow.leave_library()
        listed = self._all_live() if channels is None else channels
        self.tune(channel, group_key=group_key, channels=listed)

    # ---- plan 4: links of Network streams ------------------------------------------------------

    def set_links(self, resolve: Callable[[dict], Optional[str]]) -> None:
        """`resolve(item)`: a link of Network streams' address as it plays now (the network
        player's: a token replaced since; None for an item its provider makes the link of).
        Without it a link plays its own address."""
        self._links = resolve

    def set_link_owner(self, whose: Callable[[dict], Optional[str]]) -> None:
        """`whose(item)`: whose stream the link `item` is now (the network player's: the
        account its address names, else Network streams'; None for an item that is no link).
        Every way a link plays books it so, and ⟲ Recent lists it so, never as it was kept."""
        self._whose = whose
        set_owner = getattr(self.nav, "set_link_owner", None)
        if set_owner is not None:
            set_owner(whose)

    def _owned(self, item: dict) -> dict:
        """`item` stamped with whose stream it is now, when it is a link (final review C1: a
        link kept before its account was configured books under that account); any other
        item as it is."""
        owner = self._whose(item) if isinstance(item, dict) else None
        if owner is None or provider_of(item) == owner:
            return item
        return with_provider(item, owner)

    def _owned_target(self, target: Target) -> Target:
        """`target`, its link stamped with whose stream it is now; itself for anything else
        (what plays now stays what plays now)."""
        owned = target.item if target.local else self._owned(target.item)
        return target if owned is target.item else replace(target, item=owned)

    def set_link_player(self, play: Callable[[str], bool]) -> None:
        """`play(id)`: the link of Network streams kept under `id` plays (the network
        player's: it knows its kind and whose stream it is); False when none is kept so."""
        self._play_kept = play

    def set_link_item(self, item_of: Callable[[str], Optional[tuple[dict, str]]]) -> None:
        """`item_of(id)`: the link of Network streams kept under `id` as `play` would hand it
        over (its item: its address, whose stream it is; its kind, LIVE or FILM), None when
        none is kept so: what its row's menu and a tile it is dragged onto play."""
        self._link_item = item_of

    def set_link_stepper(self, step: Callable[[dict, int], bool]) -> None:
        """`step(item, delta)`: channel up/down while `item` plays, when it is a link kept in
        Network streams: the next (+1) or previous (−1) kept link plays (the network player's:
        as kept, whose stream it is); False when `item` is no kept link (the channels' zap
        steps)."""
        self._step_link = step

    def stopped_at(self, item: dict) -> Optional[int]:
        """Where the film `item` (a link of Network streams) stopped, in ms, as it resumes
        here; None: from its start."""
        key = self.resume_key(self.title_target(item, "movie"))
        return self.watch.resume_position(key) if key else None

    def forget_stopped(self, item: dict) -> None:
        """The film `item` (a link of Network streams whose address now names another
        stream) plays from its start next time: where the old one stopped is forgotten, and
        the old one, if it plays now, keeps no point under it any more."""
        key = self.resume_key(self.title_target(item, "movie"))
        if key:
            if self._cid == key:
                self._cid = None
            self.watch.forget_position(key)

    def play_link(self, item: dict, section: str, zapped: bool = False) -> None:
        """A link of Network streams plays (the network player hands it over): `section`
        "live" as a channel (in ⟲ Recent's group, where it lands), else as a film, where it
        stopped. Its address is known: the link lanes answer it, no provider is asked. With no
        provider configured it is held as a file opened in QiTV is (_hold_for_link).
        `zapped`: channel up/down stepped to it over the picture, a zap step as a channel's
        (an outside player opens only once the steps settle). A link parental controls hide
        plays nowhere while locked, as a hidden channel (its list leaves it out)."""
        if self._closed or self.hides_link(item):
            return  # (before the hold: nothing would replace it)
        self._hold_for_link()
        if section == "live" and zapped:
            self._tune(item, RECENT_GROUP, [item], from_zap=True)
        elif section == "live":
            self.tune_live(item, RECENT_GROUP, [item])
        else:
            self.play_vod(item, "movie", play_context())

    def _network_url(self, item: dict) -> Optional[str]:
        return self._links(item) or None

    def _site_page(self, item: dict) -> Optional[str]:
        """The page of the link `item` when it is a site's channel page (Kick, Twitch): what
        it is kept, copied and remembered as, never the address its site answers as it plays
        (its token lasts minutes); None for any other item."""
        url = self._network_url(item)
        return url if url is not None and site_page(url) is not None else None

    def _site_link(self, item: dict) -> Optional[str]:
        """The page of the link `item` when it is a site's channel page or (2.15) a video's or
        clip's page of one: what it is copied and remembered as (never the address its site
        answers); None for any other item. (A channel's live behaviour keys on `_site_page`.)"""
        url = self._network_url(item)
        if url is None:
            return None
        return url if site_page(url) is not None or site_media(url) is not None else None

    def _resolve_site(
        self, url: str, ok: Callable[[str], None], failed: Callable[[str], None]
    ) -> None:
        """The lanes' resolver: the site of the channel page `url` asked for the stream it
        plays now (2.15: of a video's or clip's page, for the address it plays at, its name
        said by `linkNamed`), on a worker; `ok(address)` or `failed(why)` comes on this thread
        (why in English: Network streams keeps it, `site_failure`). The tune's ask is noted,
        so a newer tune stops it (`_request`)."""
        channel = site_page(url)
        media = site_media(url) if channel is None else None
        page = channel or media
        if page is None or self._closed:
            failed("")  # (the lanes ask only for a site's page, and are closed with QiTV)
            return
        stop = threading.Event()
        self._site_stops.add(stop)
        if self._asking_tune:
            self._tune_stop = stop

        def answered(_context: object, answer: object) -> None:
            self._site_asked(stop)
            address = getattr(answer, "url", answer)
            name = getattr(answer, "name", "")
            if media is not None and str(address).startswith("file:"):
                self._kept_listing = (url, str(address))  # (2.15: goes as its film closes)
            if media is not None and name and not self._closed:
                self.linkNamed.emit(media_address(media), str(name))  # (2.15: its title)
            ok(str(address))

        def refused(_context: object, reason: object) -> None:
            self._site_asked(stop)
            failed(site_failure(str(reason), page))

        self._jobs.run_worker(SiteJob(page, stop, self._sites_dir()), answered, refused)

    def _reask_site(
        self, page: str, ok: Callable[[str], None], failed: Callable[[str], None]
    ) -> None:
        """A dropped site channel asks its site again for a stream (the lanes' resolver); like
        the tune's ask, a newer choice stops it (`_request`)."""
        self._asking_tune = True
        try:
            self._resolve_site(page, ok, failed)
        finally:
            self._asking_tune = False

    def _site_recording(self, url: str, then: Callable[[object], None]) -> Callable[[], None]:
        """2.15: the site of the live channel page `url` asked, on a worker, for its running
        broadcast's recording: `then(recording)` on this thread (a `live_sites.Recording`,
        or None: it keeps none, or was not reached). Returns what stops the ask (a newer
        tune, QiTV closing: `then` is not called after it)."""
        page = site_page(url)
        stop = threading.Event()
        if page is None or self._closed:
            return stop.set
        self._site_stops.add(stop)

        def answered(_context: object, found: object) -> None:
            self._site_stops.discard(stop)
            if not stop.is_set() and not self._closed:
                then(found)

        def failed(_context: object, _reason: object) -> None:
            self._site_stops.discard(stop)
            if not stop.is_set() and not self._closed:
                then(None)

        self._jobs.run_worker(RecordingJob(page, stop), answered, failed)
        return stop.set

    def _sites_dir(self) -> Optional[str]:
        """2.15: QiTV's own folder for a Twitch recording's kept listing (beside the config),
        emptied as the film it plays closes (`_forget_kept_listings`)."""
        folder = getattr(self.config, "get_config_dir", None)
        return str(Path(folder()) / "cache" / "sites") if callable(folder) else None

    def _forget_kept_listings(self, target: Optional[Target] = None) -> None:
        """2.15: the kept listing of a recording still being made goes as its film closes
        (another target replaces it, or QiTV closes; the same film played again keeps it), on
        a plain thread (the disk)."""
        kept = self._kept_listing
        if kept is None or (target is not None and self._site_link(target.item) == kept[0]):
            return
        self._kept_listing = None
        folder = self._sites_dir()
        if folder is not None:
            threading.Thread(
                target=forget_kept_listings, args=(folder,), name="qitv-sites-clean", daemon=True
            ).start()

    def _site_asked(self, stop: threading.Event) -> None:
        """A site's ask ended (answered, or not): nothing more stops it."""
        self._site_stops.discard(stop)
        if self._tune_stop is stop:
            self._tune_stop = None

    def _guard_view(self, section: str, item: dict) -> tuple[str, dict]:
        """`item` of `section` as parental controls know it: a link as one live link of
        Network streams by its id, whoever's stream it is now (an account added after it was
        hidden) and a film too; anything else as it is."""
        if self._network_url(item) is not None:
            return "live", with_provider(item, NETWORK_PROVIDER)
        return section, item

    def hides_link(self, item: dict) -> bool:
        """Whether parental controls hide the link `item` now (locked): it is not listed,
        never plays, and channel up/down steps past it. False for an item that is no link."""
        guard = self.parental
        if guard is None or self._network_url(item) is None:
            return False
        return bool(guard.hides_item(*self._guard_view("live", item)))

    def _protects_play(self, guard: Any, target: Target) -> bool:
        """`guard.protects_play` for what `target` plays, a link as `_guard_view` says."""
        section, item = self._guard_view(target.section, target.item)
        return bool(guard.protects_play(section, item, target.context))

    def _hold_for_link(self) -> None:
        """A link is about to play while no provider is configured: nothing opens over it (the
        welcome card waits), as over a file opened in QiTV (hold_for_file), until it stops,
        ends, fails or another title that is no link replaces it (or a link, once a provider
        exists). It spends nothing: the first provider added later still opens its list when
        ready. With a provider configured a link plays as a channel or a film does: nothing is
        held."""
        if not self.catalog.provider_names():
            self._autoplay.drop()
            if not self._link_hold:  # a file playing or looked at holds already: its hold
                self._file_under_link = self._flow.held()
            self._flow.hold_for_file(True, spends_start=False)
            self._link_hold = True

    def _link_holds(self, target: Optional[Target]) -> bool:
        """Whether `target` is a link the hold for links is for (any link, while it holds)."""
        return (
            self._link_hold
            and target is not None
            and not target.local
            and self._network_url(target.item) is not None
        )

    def _let_go_of_link(self) -> None:
        """The link held for stopped, ended or failed: what waited for it (the welcome card,
        the update question) may come now, as after a file."""
        if self._link_hold:
            self._link_hold = self._file_under_link = False
            self._flow.hold_for_file(False)

    def _let_go_unless_a_link_plays(self) -> None:
        """A link chosen while held never came (its open raised, an outside player refused
        it, the multiview had no room): the hold goes, unless a link it holds for plays on, or
        a file's hold is under it (a file plays on, or is looked at): that one stays the
        file's, and goes as a file's does."""
        current = self._current
        if self._link_holds(current):
            return
        if self._file_under_link or (current is not None and current.local):
            self._link_hold = self._file_under_link = False
            return
        self._let_go_of_link()

    def _resume_owner(self, item: dict, provider_name: str) -> str:
        """Whose resume points `item`'s are: a link's are Network streams' (its id stays
        whatever token, or account, its address names)."""
        return NETWORK_PROVIDER if self._network_url(item) is not None else provider_name

    def _link_played(self) -> None:
        """What plays large shows its first picture: a link's is heard once per target."""
        current = self._current
        if current is None or current is self._link_heard:
            return
        url = self._network_url(current.item)
        if url is None:
            return
        self._link_heard = current
        self.linkPlayed.emit(current.item, LIVE if current.live else FILM, url)

    def _on_link_failed(self, target: Target, why: str) -> None:
        """A link did not play, or went for good: said plainly (the toast before it said
        how), and the network player keeps why."""
        url = self._network_url(target.item)
        if self._closed or url is None:
            return
        self._error(self._own_words(why) or self._named(target.title, translate(why)))
        self.linkFailed.emit(target.item, LIVE if target.live else FILM, url, why)
        if self._current is None or target is self._current:
            if self._link_holds(target):
                self._let_go_of_link()  # it failed for good: as a file that failed

    @staticmethod
    def _own_words(why: str) -> Optional[str]:
        """Why a link did not play, said alone when it names its channel itself (a site's
        answer, kept in English: in the active language); None for any other (said after
        the link's name)."""
        return shown_reason(why) if names_its_channel(why) else None

    def watch_from_start(
        self, channel: dict, programme: Programme, group_key: str, channels: list
    ) -> None:
        """A past programme from the phone remote's timetable: as the guide's Watch from the
        start."""
        self._flow.leave_library()
        self._on_guide_catchup(channel, programme, group_key, channels)

    def timetable(
        self, channel: dict, limit: int, back: timedelta
    ) -> tuple[datetime, list[Programme]]:
        """Now, and `channel`'s programmes from the one on now (from up to `back` earlier when
        its archive plays), oldest first."""
        now = self._now()
        reach = self.archive_reach(channel)
        since = now - min(back, reach) if reach > timedelta(0) else now
        return now, self._programmes(channel, since, limit)

    def play_file(self, path: str, size: Optional[int] = None, *, start_over: bool = False) -> None:
        """Play a local media file (File → Open File…) as a movie: no link, no history.

        It is no provider's: nothing records it in a history. With its `size`, it goes on
        from where it stopped (its name and size are its resume key), unless `start_over`;
        without one, from the start and no position is kept. A song plays on its screen, its
        album after it (Plan 4).
        """
        target = self._open_file(path, size, start_over=start_over)
        name = Path(path).name
        if target is not None and target is self._current and is_video(path):
            if parse_episode(name) is not None:
                # An episode: its folder may name the next one (_on_folder_listed).
                self._listing_generation += 1
                self._listing_for = (target, path)
                self._listing.start(self._listing_generation, str(Path(path).parent))

    def _open_file(
        self, path: str, size: Optional[int], *, start_over: bool = False, in_list: bool = False
    ) -> Optional[Target]:
        """A local file plays (play_file's, a playlist entry's: `in_list`): resumed by its name
        and `size` (unless `start_over`); a song's key is marked as audio. Its new target (None
        while QiTV closes)."""
        name = Path(path).name
        key = local_key(name, size) if size is not None else None
        if key and is_music(path):
            self.watch.mark_audio(key)
        return self._play_local(path, name, resume_key=key, start_over=start_over, in_list=in_list)

    def play_list(
        self,
        name: str,
        entries: Sequence[str],
        sizes: Sequence[Optional[int]] = (),
        start: Optional[int] = None,
    ) -> None:
        """A playlist of local files, `name`d, plays from its first entry (spec §11), or from
        `start` (a folder's: the entry the viewer stopped part-way into, which resumes there;
        the list keeps the entries before it): each entry's next is the list's next entry
        ("local_next" in its play context, with no season or number), for the transport's Next
        and, with "Auto-play next episode" on, the Next card; no entry's folder is listed.
        `sizes` (the router read them off the GUI thread) give each its resume key.

        A list of songs only (by their extensions) is an album named after it on the audio
        screen (Plan 4, Task 9): its entries in its order, their tags read by name on a thread
        (those files only, never their folders), shuffle and repeat as an album's, no card
        between two songs; shuffled with no `start`, it starts at a song of its own picking.
        In any other list a song is a lone song on its screen, and the list's next entry plays
        after it with no card."""
        paths = tuple(str(entry) for entry in entries)
        if not paths:
            return
        known = tuple(sizes)[: len(paths)]
        listed = _LocalList(name, paths, known + (None,) * (len(paths) - len(known)))
        self._list = listed
        self._list_generation += 1  # an older list's songs, read late, reach nothing
        self._list_job.stop()  # (they stop being read between two files)
        first = 0 if start is None else min(max(start, 0), len(paths) - 1)
        if self._screen() is not None and all(is_song(p) for p in paths):
            listed.songs = True
            listed.tracks = [unread_track(p, s) for p, s in zip(paths, listed.sizes)]
            self._list_job.start_files(self._list_generation, list(zip(paths, listed.sizes)))
            if start is None and self._shuffle_setting():
                first = self._shuffle_rng.randrange(len(paths))
        self._play_entry(first)

    def _play_entry(self, index: int, *, start_over: bool = False) -> None:
        """The list's entry at `index` plays (from its start with `start_over`: Play again),
        its next entry as what follows it (a list of songs: its queue's next, with no card)."""
        listed = self._list
        if listed is None:
            return
        # A folder's listing still on its way (a file before the list) never reaches it.
        self._listing_generation += 1
        self._listing_for = None
        path = listed.entries[index]
        listed.index = index  # (a list of songs' queue opens on its row: _list_song_opens)
        target = self._open_file(path, listed.sizes[index], start_over=start_over, in_list=True)
        listed.target = target
        opens = getattr(self.window_actions, "list_entry_opens", None)
        if target is not None and target is self._current and callable(opens):
            opens(target, path)  # the subtitle files dropped for this entry load once it opens
        if target is not None and target is self._current and not listed.songs:
            self._list_follows(listed, target)

    def _list_follows(self, listed: _LocalList, target: Target) -> None:
        """`target`, the list's entry that plays, has the list's next entry as what follows it
        ("local_next"), for the transport's Next and the Next card; the last has none."""
        index = listed.index
        if index + 1 >= len(listed.entries):
            return
        following = listed.entries[index + 1]
        entry = {
            "name": Path(following).stem,
            "season": None,
            "number": None,
            "local_path": following,
            "size": listed.sizes[index + 1],
        }
        target.context = play_context({LOCAL_NEXT: entry})
        self.overlays.transport.set_next_available(self._autoplay.has_next(target))
        self._dock_follows()

    def play_recording(self, item: dict, start_over: bool = False) -> None:
        """A recording from the Library (2.8): its file plays as a movie from where it stopped
        (its id is its resume key), in no provider's history."""
        path = str(item.get("path") or "")
        if path:
            self._play_local(
                path,
                item_title(item),
                resume_key=str(item.get("id") or ""),
                start_over=start_over,
                item=item,
            )

    def _play_local(
        self,
        path: str,
        title: str,
        resume_key: Optional[str] = None,
        start_over: bool = False,
        item: Optional[dict] = None,
        in_list: bool = False,
    ) -> Optional[Target]:
        """`item`: the Library's recording (2.8), kept on what plays so that a phone can
        take it over (play_elsewhere); `in_list`: a playlist's entry. The file's new target
        (None while QiTV closes). A song shows the audio dock at once (its screen as it plays);
        anything else a film's controls until it is known to have no picture."""
        if self._closed:
            return None
        self._count_start()  # a file starts (any: the viewer's, a list's next, autoplay's)
        if not in_list:
            self._list_job.stop()  # a list of songs given up: its songs stop being read
        self._flow.leave_library()
        url = Path(path).absolute().as_uri()
        target = Target({**(item or {}), "name": title, "cmd": url}, "movies", title, local=True)
        self._before_new_target(target)
        self._forget_stopped()  # 2.6: a newer choice than what Stop stopped
        self.leave_multiview(keep_pip=False)  # the multiview is for live TV
        self._close_pip()  # picture in picture is for live TV
        self._set_recording_aside()  # 2.6: it records on in the background
        self._cancel_external()
        self._lanes.supersede()
        self._lanes.drop_tune()  # a channel or movie still on its way never opens over it
        self._autoplay.drop()
        self._live_controls.new_target()
        previous, self._current, self._cid = self._current, target, resume_key or None
        start = 0 if start_over or not resume_key else self.watch.resume_position(resume_key) or 0

        session = self.session  # the large picture's, whatever swaps before it opens

        def open_file() -> None:
            self._prepare(session, target, previous)
            session.play(url, kind=classify(url, False), start_ms=start, local=True)
            self._reset_speed(session)

        self._tuner.open(self._guarded(target, open_file), local=True)  # no provider slot
        self.overlays.banner.dismiss()
        self.overlays.transport.set_next_available(False)
        if self._current is target and self._sound_opens(target, path, in_list=in_list):
            self._show_dock(title)
        else:
            self.overlays.transport.show_vod(title, "")
        return target

    def _on_folder_listed(self, generation: int, listing: list) -> None:
        """The episode's folder, listed on its thread: the same show's next episode in it
        becomes the file's play context ("local_next"), for the Next card and the transport's
        Next. A listing that comes after another title was chosen changes nothing."""
        listed_for = self._listing_for
        if self._closed or generation != self._listing_generation or listed_for is None:
            return
        target, path = listed_for
        self._listing_for = None
        if target is not self._current:
            return
        following = next_in_folder(path, listing)
        if following is None:
            return  # the last one in its folder, or the folder could not be read
        target.context = play_context({LOCAL_NEXT: following})
        self.overlays.transport.set_next_available(self._autoplay.has_next(target))
        self._dock_follows()

    def _dock_follows(self) -> None:
        """What follows changed (a folder's next episode, a playlist's next entry): the audio
        dock's ⏭ says so, while it shows."""
        if getattr(self.overlays.transport, "audio", lambda: False)():
            self._show_audio_state()

    def _play_following(self, item: dict, kind: str, context: dict) -> None:
        """What follows a movie or an episode (Autoplay's play): a file's next episode plays
        as a file opened in QiTV, held as Play again holds it, its play context only what its
        own folder's listing gives it; a playlist's next entry as the list's (_play_entry); a
        provider's title through play_vod."""
        path = item.get("local_path")
        if not path:
            self.play_vod(item, kind, context)
            return
        size = item.get("size")
        self._flow.hold_for_file(True)  # nothing opens over it (the welcome card) until it ends
        listed = self._list
        if listed is not None and listed.target is not None and listed.target is self._current:
            following = listed.index + 1
            if following < len(listed.entries) and listed.entries[following] == path:
                self._play_entry(following)
                return
        self.play_file(str(path), size if isinstance(size, int) else None)

    def _release_file_hold(self) -> None:
        """The Next card over a file that ended closed without playing (Esc), or gave way to
        another choice: the hold kept for the card goes (what waited for the film, the welcome
        card). A provider's title's card leaves alone the hold of a file still looked at."""
        current = self._current
        if not self._closed and current is not None and current.local:
            self._flow.hold_for_file(False)

    def _drop_card(self) -> None:
        """A newer choice than the Next card's offer (a link asked for, the welcome card asked
        for, a provider setup): the card closes without a reply, and a file's end that kept its
        hold for the card lets go, after the card is gone. Autoplay.drop() alone never does:
        a file opened, or Next, drops it just before holding again."""
        offered = self._autoplay.offered()
        self._autoplay.drop()
        if offered:
            self._release_file_hold()  # a file's end kept the hold for the card

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
        """The keymap context for the next key: the OpenSubtitles search, the open guide,
        Settings (the drawer's keys) or the drawer takes the keys; a card over the TV types as
        the drawer does."""
        if self._shown_card() is not None:
            return "drawer"
        if self._online is not None and self._online.visible():
            return "subtitles"
        if self._guide.is_open():
            return "guide"
        if self._settings_shown():
            return "drawer"
        return "drawer" if self.overlays.drawer.isVisible() else "player"

    def choice_number(self) -> int:
        """2.15: the newest choice's number (a tune, a film, Stop, an outside play): a
        phone's wait for a channel ends once the TV makes another (one screen at a time)."""
        return self._lanes.generation()

    def tv_waits_for(self, target: Target) -> bool:
        """2.15: the large picture waits for `target`'s channel to be live (a phone's wait for
        it gives way: one asks)."""
        return not self._closed and self._live_controls.waits_for(target)

    def phone_field(self) -> Any:
        """2.15: the text box a typed letter goes into now, in `handle`'s order, for a phone's
        typing sheet (ui/remote_typing.py; its questions and menus are asked first there): a
        card's over the TV, Settings', or the channel list's Find box; None for none."""
        if self._closed:
            return None
        card = self._shown_card()
        if card is not None:
            return field_of(card)
        if self._online is not None and self._online.visible():
            return self._online.phone_field()  # the OpenSubtitles search's field
        if self._guide.is_open():
            return None
        if self.settings_layer.shown():
            return self.settings_layer.phone_field()
        drawer = self.overlays.drawer
        return drawer.phone_field() if drawer.isVisible() else None

    # ---- Settings in the drawer (ui/settings_layer.py) ----------------------------------------

    def set_settings(self, settings: SettingsController) -> None:
        """Settings in the drawer: while it shows it takes the drawer's keys, the mouse's Back
        and Forward, and a click on the picture closes it."""
        self.settings_layer.set_settings(settings)

    def make_way_for_settings(self, keep_place: bool = False) -> None:
        """Settings is opening over the player: what would keep the keys hidden under it
        closes (SettingsLayer.make_way_for_settings), a card over the TV cancels."""
        self.cancel_cards()
        self.settings_layer.make_way_for_settings(keep_place=keep_place)

    def _settings_shown(self) -> bool:
        return self.settings_layer.shown()

    def close_settings(self) -> None:
        """Hide Settings, the list not given back (a click on the picture, the Library, the
        phone's TV button)."""
        self.settings_layer.close_settings()

    def reopen_drawer_on_settings(self) -> None:
        """Backed out of Settings opened from the drawer: the list again, where it was
        (SettingsLayer.reopen_drawer_on_settings)."""
        self.settings_layer.reopen_drawer_on_settings()

    # ---- cards over the TV ---------------------------------------------------------------------

    def add_card(self, card: Any) -> None:
        """A card over the TV of its own, not Settings' (File → Open Link…'s; a FormCard of
        ui/overlays/settings_card.py): while it shows it has every key but QiTV's questions'
        (it hands back the sound's and the window's), before the OpenSubtitles search, the
        guide, Settings and the channel list, so none reaches what is under it; the keys type
        into it (the drawer's context), and Esc is its own. Nothing opens over it by itself:
        once it goes, what waited for it comes (`_card_went`)."""
        self._cards.append(card)
        card.installEventFilter(self)  # its hiding (`eventFilter`)

    def _card_went(self) -> None:
        """A card over the TV went (cancelled, or once it handed its link over: the link holds
        first): what waited for it (the welcome card, the list due meanwhile) comes now, as
        after Settings, unless Settings or another card still covers the TV."""
        if self._closed or self._settings_shown() or self._shown_card() is not None:
            return
        self._flow.settings_closed()

    def _shown_card(self) -> Optional[Any]:
        return next((card for card in reversed(self._cards) if not card.isHidden()), None)

    def cancel_cards(self) -> None:
        """Something else takes the screen (Settings, or a section of it asked for while it
        shows: an Add provider; the Library, everything stopping): each card over the TV still
        showing cancels, as its Esc does (a read on its way is dropped)."""
        for card in reversed(self._cards):
            if not card.isHidden():
                card.handle(Action.ESCAPE)
            if not card.isHidden():
                card.hide()

    def overlay_open(self) -> bool:
        """Whether Esc would close something first: a card over the TV (Open Link…'s),
        Settings, the drawer, the guide, the Next card (or the wait for the next season), a
        number being typed, the zap column or the banner."""
        overlays = self.overlays
        return bool(
            self._shown_card() is not None
            or (self._online is not None and self._online.visible())
            or self._guide.is_open()
            or self._settings_shown()
            or self._mixer.visible()
            or self._tracks.visible()
            or self._picture.visible()
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
        pairing = self.overlays.pairing
        if pairing is not None and pairing.isVisible():
            pairing.dismiss()  # any key closes the pairing screen; Esc does only that
            if action is Action.ESCAPE:
                return
        if self._question.handle(action):
            return  # 2.6: QiTV asks (stop a recording?): Enter, Esc, the arrows are its own
        if self._quality is not None and self._quality.handle(action, key):
            return  # 2.8: the ⚙ quality menu, or its question (convert on the server?)
        captions = getattr(self.window_actions, "captions", None)
        if captions is not None and captions.handle(action):
            return  # 2.9: its question (download AI captions' speech model?)
        card = self._shown_card()
        if card is not None and card.handle(action, payload):
            return  # a card over the TV (File → Open Link…'s): its keys, none under it
        if self._online is not None and self._online.handle(action, payload):
            return  # the OpenSubtitles search shows: typing, the lists, Enter and Esc
        if self._guide.is_open() and self._guide.handle(action, payload):
            return
        drawer = self.overlays.drawer
        layer = self.settings_layer
        if layer.shown():
            if layer.handle(action, payload):
                return  # Settings' keys; the others of the list's, never the list under it
        elif drawer.isVisible() and drawer.field_key(action, payload):
            return  # 2.9.3: the Find box's caret, selection and clipboard
        if not drawer.isVisible() and self._tracks.handle(action, key):
            return  # a Sound or Subtitles panel shows: the arrows, Enter and Esc are its own
        if not drawer.isVisible() and self._picture.handle(action, key):
            return  # the Picture panel or the sleep timer's notice shows: Enter is theirs
        if not drawer.isVisible() and self._mixer.handle(action, key):
            return  # the mixer shows: the arrows, M, Tab and Esc are its own
        if not drawer.isVisible() and self._grid is not None and self._grid.handle_key(action):
            return  # the multiview's layouts show: ←/→, Enter and Esc are theirs
        if action in (Action.CH_NEXT, Action.CH_PREV):
            delta = 1 if action is Action.CH_NEXT else -1
            if self._on_demand():
                self._change_volume(-VOLUME_STEP * delta)  # ↑ is louder
            else:
                self._zap(delta)
        elif action in (Action.MEDIA_NEXT, Action.MEDIA_PREV):
            delta = 1 if action is Action.MEDIA_NEXT else -1
            if drawer.isVisible():
                return  # the drawer is browsing; play/pause still works
            if self._album_plays() or self._keys_follow_dock():  # the dock's ⏮ ⏭ (Plan 4)
                self._next_track() if delta > 0 else self._previous_track()
            elif delta < 0 and self._audio_dock_shows():
                self._previous_track()  # a lone song's ⏮, as the dock's: from its start
            elif self._on_demand():
                self._seek(delta * SEEK_STEP_MS)
            else:
                self._zap(delta)
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
            elif self._live_controls.go_back():
                pass  # 2.6: the Resume pill shows: back to where the channel was left
            elif self._live_controls.retry_now():
                pass  # the channel's stream is down and waited for: Try now
            else:
                self._open_drawer()
        elif action is Action.BACK_TO:
            self._live_controls.go_back()  # the phone's Resume (nothing when it is gone)
        elif action is Action.OPEN_DRAWER:
            from_column = self.overlays.column.isVisible()
            self.overlays.column.close()
            self._open_drawer(from_column=from_column)
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            back = action is Action.SEEK_BACK
            if not self._live_controls.seek_live(back):
                self._seek(-SEEK_STEP_MS if back else SEEK_STEP_MS)
        elif action in (Action.TAB, Action.BACK_TAB):
            if self._grid is not None and self.multiview_shown():
                self._grid.step_focus(1 if action is Action.TAB else -1)  # 2.14: tile by key
        elif action in (Action.PIP, Action.SWAP):
            if self.multiview_shown():
                self._toast(LEAVE_FIRST)  # the grid's tiles are the pictures meanwhile
            elif self._pip is not None:
                self._pip.pip_key() if action is Action.PIP else self._pip.swap_key()
        else:
            call = self._plain_call(action, payload)
            if call is not None:
                call()

    def _plain_call(self, action: Action, payload: str) -> Optional[Callable[[], None]]:
        """What an action without a guard of its own does: one call each, the pairs a step
        each way. Looked up at the key press: the large picture's tuner changes with a swap."""
        drawer, tracks = self.overlays.drawer, self._tracks
        calls: dict[Action, Callable[[], None]] = {
            Action.PLAY_PAUSE: self._play_pause,
            Action.STOP: self.stop,
            Action.INFO: self._info,
            Action.GO_LIVE: lambda: self._live_controls.go_live(),
            Action.RECORD: lambda: self._live_controls.toggle_recording(),
            Action.GUIDE: self._open_guide,
            Action.FAVORITE: self._favorite,
            Action.FULLSCREEN: lambda: self.window_actions.toggle_fullscreen(),
            Action.MINI_PLAYER: lambda: self.window_actions.toggle_mini_player(),
            Action.MULTIVIEW: self.toggle_multiview,
            Action.MUTE: lambda: self._mixer.mute(),
            Action.SOUND_EARLIER: lambda: tracks.nudge_sound(-1),
            Action.SOUND_LATER: lambda: tracks.nudge_sound(1),
            Action.SUBTITLES_EARLIER: lambda: tracks.nudge_subtitles(-1),
            Action.SUBTITLES_LATER: lambda: tracks.nudge_subtitles(1),
            Action.PICTURE: self._picture.open_from_key,
            Action.STATS: self._picture.toggle_stats,
            Action.VOL_UP: lambda: self._change_volume(VOLUME_STEP),
            Action.VOL_DOWN: lambda: self._change_volume(-VOLUME_STEP),
            Action.SLOWER: lambda: self._step_speed(-1),
            Action.FASTER: lambda: self._step_speed(1),
            Action.ESCAPE: self._escape,
            Action.NAV_UP: lambda: drawer.move_by(-1),
            Action.NAV_DOWN: lambda: drawer.move_by(1),
            Action.NAV_PAGE_UP: lambda: drawer.page(-1),
            Action.NAV_PAGE_DOWN: lambda: drawer.page(1),
            Action.NAV_LEFT: lambda: drawer.left(),
            Action.NAV_RIGHT: lambda: drawer.right(),
            Action.NAV_ACTIVATE: lambda: drawer.activate(),
            Action.NAV_TEXT: lambda: drawer.type_text(payload),
            Action.NAV_BACKSPACE: lambda: drawer.backspace(),
            Action.FORGET: self._forget_highlighted,
            Action.CONTEXT_MENU: self._context_key,  # 2.13.1: Menu, Shift+F10
        }
        return calls.get(action)

    def _step_speed(self, step: int) -> None:
        """[ and ]: the speed chip's − and +, as a click on them, at any width; the controls
        show, so the chip reads the speed (at either end too). Without a chosen speed (the
        live edge, live TV without time-shift, a song), nothing. The controls show first: a
        channel's come back with its last timeline, whose rate is the one before the step."""
        transport = self.overlays.transport
        if self._current is None or not transport.speed_steps():
            return
        self._show_transport()
        transport.step_speed(step, "the ] key" if step > 0 else "the [ key")

    def _play_pause(self) -> None:
        if self._stopped is not None and self._current is None:
            self.play_again()  # 2.6: after Stop, Play plays it again
            return
        failed = self._failed_live()
        if failed is not None:
            if not self._live_controls.retry_now():  # one that comes back by itself: now
                self._retry_failed(failed)
            return
        self._tuner.toggle_pause()
        self._show_transport()

    def stop(self) -> None:
        """Stop (■, X, the phone's; 2.6): every picture stops, nothing streams or reconnects,
        and "Stopped" shows with Play again (only "Stopped" when nothing could play again,
        e.g. an empty grid). A recording goes on in the background (its own
        Stop ends it); a channel's buffer is kept, as when it is left. Nothing when nothing
        plays (but an open waiting for a stopped recording's stream: it never opens)."""
        if self._closed:
            return
        self._count_start()  # Stop (with nothing playing too)
        if self._media is not None:
            self._media.stopped()  # 2.8: a server's title stopped where it was
        sessions = self.sessions
        small = sessions is not None and (sessions.pip_open() or sessions.pip_opening())
        busy = self._lanes.busy() or self._tuner.opening() or self.multiview_shown()
        target = self._current or self._lanes.pending() or self._beside_target()
        if target is None and not small and not busy:
            return
        self.leave_multiview(keep_pip=False)
        self._live_controls.forget_back()  # nothing to resume into: it stopped
        self._stop_all()
        self._stopped = target
        logger.info("Stopped: nothing plays")
        notice = self.overlays.stopped_notice
        if target is None:
            self._toast(translate(STOPPED))  # an empty grid: nothing to play again
        elif notice is not None:
            notice.show_notice(translate(STOPPED))

    def _beside_target(self) -> Optional[Target]:
        """What the small picture shows (2.6: after a Stop with nothing large, Play plays it
        again)."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or not (sessions.pip_open() or sessions.pip_opening()):
            return None
        return pip.view(sessions.other().name).target

    def play_again(self) -> None:
        """Play what Stop stopped: a channel live (from its group), a movie or an episode
        where it stopped, a file from the start."""
        target = self._stopped
        self._forget_stopped()
        if target is None or self._closed:
            return
        if target.local:
            self._flow.hold_for_file(True)  # a film again: the welcome card goes until it ends
            listed = self._list
            if listed is not None and listed.target is target:
                self._play_entry(listed.index, start_over=True)  # its list goes on after it
                return
            self.play_file(QUrl(str(target.item.get("cmd") or "")).toLocalFile())
            return
        if self._network_url(target.item) is not None:
            if self.hides_link(target.item):
                return  # parental controls hide it while locked: nothing is held for it
            self._hold_for_link()  # a link again: held as its first play was, without a provider
        if target.live:
            key = target.group_key or ALL_GROUP_ID
            channels = list(self.catalog.items("live", key) or []) or self._all_live()
            self.tune(target.item, group_key=key, channels=channels)
        else:
            self.play_vod(target.item, target.item_type, target.context)

    def _forget_stopped(self) -> None:
        """Something else plays (or is on its way): the Stop's pill goes."""
        self._stopped = None
        notice = self.overlays.stopped_notice
        if notice is not None and not notice.isHidden():
            notice.hide()

    def video_clicked(self) -> None:
        """A click on the picture: Settings or the channel list closes; else on live TV the
        list opens, and during a movie or an episode (2.9.3: the list came up over a film; the
        owner: never the channel list there) the film pauses or plays on, its controls
        showing."""
        if self._settings_shown():
            self.settings_layer.click_elsewhere()  # a text being edited is kept; it goes
        elif self.overlays.drawer.isVisible():
            self.overlays.drawer.close_drawer()
        elif self._on_demand():
            self._play_pause()
        else:
            self.handle(Action.OPEN_DRAWER)

    def video_context(self, global_pos: QPoint) -> None:
        """2.9.3: a right-click on the picture: the menu of what plays (a channel's as its row
        has it in the channel list, a film's or an episode's players and link). Nothing over
        the multiview's grid (its tiles are the pictures), nor with nothing playing."""
        target = self._current
        if self._closed or target is None or self.multiview_shown():
            return
        # Its players and its link go outside QiTV: a Plex title's file (2.14: _as_its_file).
        self._context.show_playing(self._as_its_file(target), global_pos, self.overlays.transport)

    def video_double_clicked(self) -> None:
        """Fullscreen; on the multiview's grid, the focused tile big (its first click focused
        it), or the grid again."""
        if self._grid is not None and self.multiview_shown():
            self._grid.toggle_big()
        else:
            self.window_actions.toggle_fullscreen()

    def mouse_back(self) -> None:
        """The mouse's Back button: a drawer layer up (spec §5.3), Settings' as Esc; closed,
        the drawer opens."""
        self._mouse_layer(Action.NAV_LEFT, Action.ESCAPE)

    def mouse_forward(self) -> None:
        """The mouse's Forward button: a drawer layer down (Settings' too, where it never
        steps a value); closed, it opens."""
        self._mouse_layer(Action.NAV_RIGHT, Action.NAV_RIGHT, MOUSE_FORWARD)

    def _mouse_layer(self, action: Action, in_settings: Action, payload: str = "") -> None:
        if self._closed:
            return
        if self._settings_shown():
            self.handle(in_settings, payload)  # through what shows over it first (a question)
        elif self.overlays.drawer.isVisible():
            self.handle(action)
        else:
            self.handle(Action.OPEN_DRAWER)

    def mouse_moved(self) -> None:
        self._show_transport()
        if self._grid is not None:
            self._grid.pointer_moved()  # the tile under the pointer shows its controls

    def _close_online(self) -> None:
        if self._online is not None:
            self._online.close()

    def pointer_may_hide(self) -> bool:
        """The pointer may go with the player controls: a picture plays on the player page and
        no list, guide, mixer or card that the pointer clicks is open."""
        if self._closed or self._current is None or not self._has_media():
            return False
        if self._flow.in_library() or self._settings_shown() or self._shown_card() is not None:
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
            overlays.more_menu,  # the audio dock's ⋯
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
        if any(watched is card for card in self._cards):  # a card over the TV: its own events
            if event.type() == QEvent.Type.Hide:
                # After what hid it (Play hands its link over right after: it holds first).
                QTimer.singleShot(0, self, self._card_went)
            return False
        if isinstance(event, QWheelEvent) and self.overlays.column.isVisible():
            self._column_wheel.wheel_event(event)
            event.accept()
            return True
        return False

    def _on_column_wheel(self, steps: int, modifiers) -> None:
        if self._closed:
            return
        if as_int(modifiers) & _VOLUME_MODIFIERS:
            self._change_volume(-VOLUME_STEP * steps)
        else:
            # The column's channels (zap.step re-highlights, which keeps the column open),
            # never the kept links' steps.
            self._zap_steps(steps, links=False)

    def _zap_steps(self, steps: int, links: bool = True) -> None:
        for _ in range(abs(steps)):
            delta = 1 if steps > 0 else -1
            if links:
                self._zap(delta)
            else:
                self.zap.step(delta)

    def _zap(self, delta: int) -> None:
        """Channel up/down over the picture (the keys, the media keys', the phone's, the
        wheel's): while a link kept in Network streams plays, the next or previous kept link
        (the network player's); else the next or previous channel of the zap's list."""
        channel = self._channel
        if channel is not None and self._step_link(channel, delta):
            return
        self.zap.step(delta)

    # ---- tuning and playing -------------------------------------------------------------------

    def tune(self, channel: dict, *, group_key: str, channels: list[dict]) -> None:
        """Tune a channel the shell chose (drawer, autotune, column click)."""
        self._tune(channel, group_key, channels, from_zap=False)

    def _tune(
        self,
        channel: dict,
        group_key: str,
        channels: list[dict],
        *,
        from_zap: bool,
        archive: Optional[tuple[float, Optional[float]]] = None,
    ) -> None:
        """`archive`: the channel's provider archive from a moment to another (Unix seconds:
        the guide's past programme) instead of live."""
        if self._closed:
            return
        channel = self._owned(channel)  # a link: whose stream it is now
        if self.hides_link(channel):
            return  # parental controls hide it while locked (a link held from before)
        self._count_start()  # a tune, whichever way it goes (it stays, swaps in or asks)
        if group_key == RECENT_GROUP:  # (where links are listed: a few rows)
            channels = [self._owned(c) for c in channels]
        self._cancel_external()  # a newer choice: the settling external launch is dropped
        self._lanes.supersede()  # and an external play still waiting for its link
        context = self.zap.context
        if group_key != context.group_key or context.index_of(channel) < 0:
            # Numbers reach every live channel, not only the group's (TV numbers are global).
            fresh = ZapContext(group_key, channels, fallback=self._all_live())
            if fresh.index_of(channel) < 0:  # the rows it keeps for ↑/↓, built once
                # Previous or a number outside the group: ↑/↓ walk every channel from it, as
                # the small picture's Previous does (never the old group from the old place).
                group_key, channels = ALL_GROUP_ID, self._all_live()
                fresh = ZapContext(group_key, channels, fallback=channels)
            picked = item_provider.pick_source(self.catalog, channel, group_key)
            fresh.source = picked
            self.zap.set_context(fresh)
        else:
            picked = item_provider.pick_source(self.catalog, channel, group_key)
            if not from_zap or not context.source:
                context.source = picked  # a zap keeps the list its group was picked from
        playing = self._playing_live()
        stay = playing is not None and ZapContext.same(channel, playing.item)
        if stay and archive is not None and self._large_records():
            stay = False  # its archive opens beside the recording, which records on (2.6)
        if not stay and self._shown_elsewhere(channel, group_key, channels):
            if archive is not None:
                # The small picture's channel swapped in (or the background player recording
                # it is large again): its archive opens there now, as on the channel playing.
                self._tune(channel, group_key, channels, from_zap=from_zap, archive=archive)
            return
        if not stay and self._asks_first(channel, group_key, channels, archive):
            return  # 2.6: no stream is free while a recording holds one: asked
        if not from_zap and not stay:
            self.zap.note_request(channel)  # ↑/↓ move on from it while its link is pending
        self.overlays.column.close()
        self._channel = channel
        self._playing_moved()
        self._update_guides()
        self._show_banner(channel)
        if stay and archive is not None and playing is not None:
            # Its archive on the player that plays it (live, or its archive already).
            if self._live_controls.catchup.open(playing, archive[0], until=archive[1]):
                if self._lanes.busy():
                    self._lanes.queued = playing  # a choice still on its way is dropped
                self.zap.tuned(channel)
                self._show_transport()
                return
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
            self._let_go_unless_a_link_plays()  # a link chosen held never comes
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
                archive_at=archive[0] if archive is not None else None,
                archive_until=archive[1] if archive is not None else None,
            ),
            chosen_now=True,
        )
        self._tuning_since = time.monotonic()
        self._tuning.start(TUNING_KEEP_MS)

    def play_item(self, node: NavNode, siblings: Sequence[NavNode], parent: NavNode) -> None:
        """A drawer row was chosen. Only channels (and Network streams' links) play from the
        drawer: Movies and Series open the Library, whose titles and episodes play through
        play_vod()."""
        if self._closed:
            return
        if node.layer == LINK_LAYER:  # Network streams' (plan 4): as it was kept
            if self._play_kept(node.key):
                self.overlays.drawer.close_drawer()
            return
        if node.layer != "item" or self.nav.section_of(node) != "live":
            logger.debug("Not playable from the drawer: %s", node)
            return
        if self._choosing_small and self._pip is not None:
            self._end_choosing_small()
            self._pip.open_node(node)  # the list was opened for the small picture
            self.overlays.drawer.close_drawer()
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
        channel found by the Library's search. A link of Network streams plays as it was kept
        (a film as a film)."""
        if self._closed:
            return
        if provider_of(channel) == NETWORK_PROVIDER:
            if self._play_kept(str(channel.get("id") or "")):
                self.overlays.drawer.close_drawer()
            return
        key, channels = self._found_group(channel, group_id)
        if self._choosing_small and self._pip is not None:
            self._end_choosing_small()  # the list was opened for the small picture
            listed = channels if key is not None else self._all_live()
            source = item_provider.pick_source(self.catalog, channel, key or ALL_GROUP_ID)
            context = ZapContext(
                key or ALL_GROUP_ID, listed, fallback=self._all_live(), source=source
            )
            self._pip.open_channel(channel, context)
            self.overlays.drawer.close_drawer()
            return
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

    def title_target(self, item: dict, kind: str, context: Optional[dict] = None) -> Target:
        """A movie or an episode (`kind`) as what plays: its title, and the Library's `context`
        (see play_vod)."""
        context = play_context(context)
        episode = kind == "episode"
        series = context["series"]
        return Target(
            item,
            "series" if episode else "movies",
            item_title(item, episode=episode),
            subtitle=item_title(series) if episode and isinstance(series, dict) else "",
            context=context,
            start_over=bool(context["start_over"]),
        )

    def resume_key(self, target: Target) -> Optional[str]:
        """Where a movie's or an episode's position is kept, as while it plays here (None: a
        live channel, or a file from the disk)."""
        if target.live or target.local:
            return None
        provider_name = item_provider.provider_name(self.catalog, target.item)
        owner = self._resume_owner(target.item, provider_name)
        return cast(
            Optional[str],
            self.watch.content_id(owner, target.item, item_type=target.item_type),
        )

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
        item = self._owned(item)  # a film link: whose stream it is now
        if self.hides_link(item):
            return  # parental controls hide it while locked
        self._count_start()  # a title starts, asked about or not (before a channel goes small)
        if from_library is None:
            from_library = self._flow.in_library()

        def again() -> None:
            self.play_vod(item, kind, context, from_library=from_library)

        if self._ask_for_stream(item, self._tuner.purpose, again):
            return  # 2.6: no stream is free while a recording holds one: asked
        self.leave_multiview()  # live only: the focused tile is the big picture again
        self._flow.leave_library()
        target = self.title_target(item, kind, context)
        converted = self._media_quality.get(
            provider_of(item) or self.catalog.current_provider_name()
        )
        if converted is not None:  # 2.8: the server's conversion chosen goes on for its titles
            target = replace(target, quality=converted)
            if self._external_launcher() is not None:
                target = self._as_its_file(target)  # 2.14: Settings' outside player takes it
        title = target.title
        beside = self._title_beside_channel()  # what plays large is a title, a channel small
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
        self._request(target, chosen_now=True)

    def _media_menu(self, item: dict) -> Optional[dict]:
        """What the ⚙ quality menu offers for a title (the catalog's media_menu; None: none)."""
        menu = getattr(self.catalog, "media_menu", None)
        return menu(item) if callable(menu) else None

    def _as_its_file(self, target: Target) -> Target:
        """`target` as anything outside QiTV's own player gets it (2.14: a phone, Settings'
        outside player, the picture's menu: its players and Copy stream link): a Plex title
        as its file is, never the server's conversion, a session of the server's that only
        QiTV's own play stops (services.media_reporter)."""
        quality = target.quality
        if target.live or target.local or not getattr(quality, "converted", False):
            return target
        if not is_plex(item_provider.provider_type(self.catalog, target.item)):
            return target
        # The version the conversion converts, when its server said (review M2: a ⚙ row's
        # conversion names none), else the one chosen, else the settings' pick.
        playing = (self._media_menu(target.item) or {}).get("quality")
        source = str(getattr(playing, "source", "") or quality.source or "")
        return replace(target, quality=Quality(source=source))

    def _conversion_refused(self, target: Target) -> None:
        """2.14: the server didn't make the conversion the ⚙ asked for (its owner allows none,
        say): the title plays as its file is, the viewer is told, and the conversion goes on
        to none of that server's next titles (choosing one again asks the server again)."""
        provider = provider_of(target.item) or self.catalog.current_provider_name()
        self._media_quality.pop(provider, None)
        said = (provider, str(target.item.get("id") or ""))
        if said != self._refusal_said:  # once for the title (review M1: not at each reopen)
            self._refusal_said = said
            self._toast(translate(CONVERSION_REFUSED))

    def change_quality(self, quality: Any) -> None:
        """The quality menu's choice (2.8): the film or episode playing opens again in that
        quality (a media server's version as it is, or its conversion), on from where it is.
        A conversion goes on for that server's titles until QiTV closes; a version as it is
        ends it."""
        target = self._current
        if self._closed or target is None or target.live or target.local:
            return
        provider = provider_of(target.item) or self.catalog.current_provider_name()
        self._refusal_said = None  # a new choice: a refusal of it is said again
        if getattr(quality, "converted", False):
            self._media_quality[provider] = quality
        else:
            self._media_quality.pop(provider, None)
        start = max(0, int(self.session.time_ms() or 0)) if self._has_media() else None
        again = replace(target, quality=quality, start_ms=start, start_over=False, zapped=False)
        if self._external_launcher() is not None:
            again = self._as_its_file(again)  # 2.14: Settings' outside player takes it
        self._request(again, chosen_now=True)

    # ---- AI captions on a provider's film (2.9) ----------------------------------------------
    def film_relay_wanted(self) -> bool:
        """The large picture plays a provider's film or episode, which AI captions can read only
        through a film relay (services.film_relay): the picture's one provider stream, shared.
        A file, a media server's film (no stream limit) or an HLS one is read as it is, or not."""
        target, url = self._current, self.session.url
        return target is not None and url is not None and self._relayable(target, url)

    def relay_film(self) -> bool:
        """AI captions came on during a provider's film: it plays on through a film relay, opened
        again where it is (a paused one stays paused). False: it plays as it did (nothing to
        relay, one already relays it, or nothing plays to open again)."""
        session, target = self.session, self._current
        if self._closed or target is None or not self.film_relay_wanted():
            return False
        if session.relay() is not None or self._state not in _ACTIVE_STATES:
            return False  # one relays it, or it ended (or failed): none made for nothing
        url = str(session.url)
        relay = self._film_relay(target, url, item_provider.stream_agent(self.catalog, target.item))
        if relay is None:
            return False
        if not session.play_through(relay):
            relay.stop()
            return False
        logger.info("AI captions: the film plays on through the film relay")
        return True

    def _captions_on(self) -> bool:
        captions = getattr(self.window_actions, "captions", None)
        return captions is not None and bool(captions.enabled())

    def _relayable(self, target: Target, url: str) -> bool:
        if target.live or target.local or not relayable(url):
            return False
        return not is_media_server(item_provider.provider_type(self.catalog, target.item))

    def _film_relay(self, target: Target, url: str, agent: Optional[str]) -> Optional[FilmRelay]:
        """A started film relay of `url`, asking the provider as the picture's own play does
        (its user agent, else libVLC's; no referrer, as for any film); None: the film plays as
        it is (not a provider's film the relay carries, or no loopback port)."""
        if not self._relayable(target, url):
            return None
        provider = item_provider.provider_dict(self.catalog, target.item)
        verify = bool(provider.get("ssl_verify", getattr(self.config, "ssl_verify", True)))
        relay = FilmRelay(url, user_agent=agent or DEFAULT_USER_AGENT, verify_ssl=verify)
        try:
            relay.start()
        except OSError as error:
            logger.warning("AI captions: no film relay (%s)", type(error).__name__)
            return None
        return relay

    def live_menu(self) -> Optional[dict]:
        """The live ⚙ (2.8): the large picture's channel's picture sizes, when its stream comes
        in two or more (LiveControls.sizes); None in the multiview or while an archive plays."""
        target = self._current
        if target is None or not target.live or self._closed:
            return None
        if self.multiview_shown() or self._live_controls.catchup.active():
            return None
        return self._live_controls.sizes(target)

    def _offer_live_quality(self) -> None:
        """The channel's sizes came (or an archive started or ended): the ⚙ shows or goes."""
        current = self._current
        if current is not None and current.live:
            self.overlays.transport.set_quality_available(self.live_menu() is not None)

    def change_live_quality(self, max_height: int) -> None:
        """The live ⚙'s choice (2.8): the channel tunes again with that size cap (0: its
        best), which it keeps for its next tunes. Its time-shift buffer is kept (the tuner parks
        it and takes it back) and it plays on from where the user was. While it records it
        stays as it is: a tune would set the recording aside on a second stream."""
        target, sizes = self._current, self.live_menu()
        cap = max(0, int(max_height))
        if target is None or sizes is None or cap == sizes["max_height"]:
            return
        live = self._live_controls.live
        if live is not None and live.recording():
            self._toast(SIZE_WHILE_RECORDING)
            return
        self._request(replace(target, max_height=cap, zapped=False, retried=False), chosen_now=True)
        self._live_controls.choose_size(target, cap)

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

    def _listed(self, kept: dict) -> dict:
        """The live list's channel that `kept` (a scheduled recording's copy) is: by its
        provider, then its address, id or name (`recording_schedule.is_listed`); `kept` itself
        when the list has none."""
        provider = str(kept.get("_provider") or "")
        listed = recording_schedule.listed_matcher(kept)  # its pattern built once
        for channel in self._all_live():
            if provider and item_provider.provider_name(self.catalog, channel) != provider:
                continue
            if listed(channel):
                return channel
        return kept

    def _request(self, target: Target, *, chosen_now: bool = False) -> None:
        """Ask for `target`'s link. `chosen_now`: the viewer chose it now (pressing a channel,
        the guide, the quality menu); False, the default, for anything automatic (a retry,
        an archive's end) or chosen earlier and only asked for again (queued behind another
        link, or held by its provider's setup): a file still looked at is not dropped by it."""
        if chosen_now and not target.local:
            self._file_gives_way()  # a file still looked at is older than this choice
        if chosen_now:
            self._count_start()  # a start that counts (a tune, a title, Go live, Next)
        self._forget_stopped()  # 2.6: a newer choice than what Stop stopped
        self._give_back_later = False  # the large picture has a newer choice
        self._cancel_external()
        self._flow.drop_autotune()  # a choice made before the live section loaded comes first
        # A folder's late listing gives the file no Next now, nor once this link fails; nor a
        # song's late album its list.
        self._listing_generation += 1
        self._album_generation += 1
        self._drop_card()  # a newer choice than what the Next card offers
        if self._tune_stop is not None:
            # A newer choice: the stream a site is asked for, for the tune in flight, would
            # play nowhere; its ask ends between its requests (or answers, dropped), and the
            # newer choice is asked now, not after it.
            self._tune_stop.set()
            self._tune_stop = None
            self._lanes.forget_own_ask()
        self._live_controls.chosen(target)  # a site's channel waited for is no longer
        self._asking_tune = True  # a site asked meanwhile is asked for this tune
        try:
            self._lanes.request(target)
        finally:
            self._asking_tune = False

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
            self._request(queued, chosen_now=False)  # chosen as it was queued
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
            self._link_dropped(url)  # 2.14 review I2
            return
        self._play_link(target, url)

    def _play_link(self, target: Target, url: str) -> None:
        previous = self._current
        try:
            self._deliver(target, url)
        except Exception as e:
            self._current = previous  # whatever was playing before is still the current one
            self._log_failure(f"play {target.title}", e)
            self._let_go_unless_a_link_plays()  # a link chosen held never came
            self._restore_channel()
            self._error(self._named(target.title, str(e)))

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
        if self._waits_for_live(target, message):
            return  # a site's channel that is not live waits on screen for it
        self._forget_room(target)
        self._give_large_back(target)
        self._let_go_unless_a_link_plays()  # a link chosen held never came
        self._restore_channel()
        if self._network_url(target.item) is not None:
            # A site's page its site gave no stream for: counted in its row, said once.
            self._on_link_failed(target, message)
            return
        self._error(self._named(target.title, message))
        self._explain_item(target.item, target.title)

    def _waits_for_live(self, target: Target, message: str) -> bool:
        """2026-11-25 (the owner: "streamer is offline and comes online, just when you sense
        resync to latest"): a site's channel chosen for the large picture (not a zap step:
        those step past it), or asked again as its stream ended, whose site says it is not
        live now waits there for it (LiveControls.wait_for_live): what played stops, the pill
        says so, its site is asked again calmly, and it plays from its newest once it is live.
        Counted in its row once; while it waits, an ask that did not reach the site waits on.
        False where it does not wait (a zap step, a multiview, an outside player: said as
        ever)."""
        if (
            not target.live
            or target.zapped
            or self._site_page(target.item) is None
            or self.multiview_shown()
            or self._external_launcher() is not None
        ):
            return False
        waiting = self._live_controls.waits_for(target)
        if not is_offline(message) and not (waiting and message in (TIMED_OUT, FAILED)):
            if waiting:
                self._live_controls.stop_waiting()  # (its site said another thing: said)
            return False
        self._forget_room(target)
        if not waiting:
            # It takes the large picture as a delivered link does (what records there records on
            # in the background; a file or the audio screen gives way); nothing plays meanwhile.
            self._before_new_target(target, delivered=True)
            self._set_recording_aside()
            if self._media is not None and not self._plays_small(self._media.active_url()):
                self._media.stopped()  # 2.8: a server's title stopped where it was
            try:
                self.watch.flush()  # a film's last position
            except Exception as e:
                self._log_failure("save the watch state", e)
            self.overlays.transport.dismiss()
            self._live_controls.new_target()
        self._live_controls.wait_for_live(target, message if is_offline(message) else "")
        self._current, self._cid = target, None
        self._restore_channel()
        self.zap.tuned(target.item)  # ↑/↓ move on from it
        if not waiting:
            url = self._network_url(target.item) or ""
            self.linkFailed.emit(target.item, LIVE, url, message)
        return True

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
        # 2.15: a tile or the small picture whose site channel isn't live waits for it (as the
        # large picture's): no error toast; its row counts it once, as it begins to wait.
        waits: list[Optional[bool]] = []
        for name, waiting in list(self._tile_links.items()):
            if waiting is target:
                del self._tile_links[name]
                if self._grid is not None:
                    waits.append(self._grid.link_failed(name, target.item, message))
        if self._pip is not None:
            waits.append(self._pip.offline(target, message))
        for at, (asked, hear) in enumerate(self._elsewhere_failed):
            if asked is target:  # 2.15: a phone's Watch (play_elsewhere's `failed`)
                del self._elsewhere_failed[at]
                try:
                    heard = hear(target, message)
                except Exception as error:  # a phone's ask must never take QiTV down
                    logger.error("A phone's failed link: %s", type(error).__name__)
                    heard = None
                if heard is True and self._live_controls.waits_for(target):
                    self._live_controls.stop_waiting()  # the phone waits for it: one asks
                waits.append(heard)
                break
        if False in waits:
            return  # it waits on: said and counted once already
        if True not in waits:
            self._menu_error(self._own_words(message) or self._named(target.title, message))
        page = self._site_link(target.item)
        if page is not None and is_site_failure(message):
            # Its site gave no stream (for the PiP, a tile, an outside player, the phone):
            # counted in its row as a play's. (What failed using a link it gave is not: its
            # words are an exception's, which may carry that address.)
            self.linkFailed.emit(target.item, LIVE if target.live else FILM, page, message)

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
        sessions = self.sessions
        if open_ or sessions is None:
            return
        self._give_back_later = False
        closed = sessions.other()  # the small picture's slot: once closed, it plays nothing
        if closed.channel is None and closed is not sessions.large():
            self._player_keys.forget(closed.playback)

    def _on_slot_state(self, name: str, open_: bool) -> None:
        """A multiview tile opened or closed: a closed one's player keeps no channel key."""
        if not open_ and self.sessions is not None:
            self._player_keys.forget(self.sessions.slot(name).playback)

    def _restore_channel(self) -> None:
        """After a failed tune, INFO and the drawer follow the channel still playing."""
        current = self._current
        self._channel = current.item if current is not None and current.live else None
        self._playing_moved()

    def playing_channel(self) -> Optional[dict]:
        """The channel tuned last (playing, or on its way): its row in the channel list, the
        guide and the fast-zap list has a small accent ▶."""
        return self._channel

    def _playing_moved(self) -> None:
        for listing in (self.overlays.drawer, self.overlays.column, self.overlays.guide):
            listing.playing_changed()

    def _deliver(self, target: Target, url: str) -> None:
        # Its link came: it replaces what plays. It was chosen as it was asked for
        # (_request), perhaps before a file opened since: that file isn't dropped for it.
        self._before_new_target(target, delivered=True)
        item = target.item
        launcher = self._external_launcher()
        multiview = target.live and self.multiview_shown()
        if self._media is not None and launcher is None and not multiview:
            # 2.8: a server's title starts (another's stops); one handed to an outside player
            # is told stopped as it goes (_launch), nothing after: QiTV can't see it play.
            # 2.14: a channel tuned large replaces no title swapped small: it plays on (a
            # Plex conversion's session too); the next tune after it closed tells its stop.
            if not (target.live and self._plays_small(self._media.active_url())):
                self._media.playing(url)
        self._freed_for = None  # it came: the large picture is the title's
        if target is self._room_for and self._pip is not None:
            self._room_for = None
            self._pip.make_room()  # before it opens: a closing small picture is waited for
        # Direct plays book the slot under the item's own provider, as pipeline tunes do.
        provider_name = item_provider.provider_name(self.catalog, item)
        self._set_recording_aside()  # 2.6: what records large records on in the background
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
        self._phone_slot.give_back()  # it plays here again: the phone's was its stream
        if target.live and target.archive_at is not None:
            # Catch-up (2.2): the channel's archive from that moment, when it offers one now;
            # else the channel plays live. Either is one more stream at its provider: refused,
            # as a live tune is, when the other pictures take its whole limit.
            if self._live_controls.refuse_over_limit(target):
                return
            kept = self._current, self._cid
            self._current, self._cid = target, None
            if self._live_controls.open_archive(target, url):
                if self.overlays.transport.isVisible():
                    self._live_controls.show_live_transport(target)
                self._delivered(target, url)
                return
            self._current, self._cid = kept
            target = replace(target, archive_at=None, archive_until=None)
        if target.live and self.multiview_shown():
            logger.info("Tuning “%s” (%s) in a multiview tile.", _log_name(item), provider_name)
            self._open_in_tile(target, url, previous, provider_name)
            if self.overlays.transport.isVisible():
                self._live_controls.show_live_transport(target)
        elif target.live:
            kind = classify(url, self.catalog.is_live("live"))
            if self._live_controls.through_pipeline(url, kind, item):
                if not self._live_controls.tune_pipeline(target, url):
                    return  # over the provider's stream limit: nothing opens
                if target.max_height is not None:
                    self._live_controls.go_back_when_offered()  # 2.8: a new size, on from there
                    # Asked once: the channel keeps its cap, and a later tune of this target
                    # (Live after an archive, a retry, a resume) is any tune.
                    target = replace(target, max_height=None)
                # The old stream is stopped: the channel's sync waits for its player's open.
                self._prepare(self.session, target, previous)
                self._current, self._cid = target, None
                self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            else:
                if self._live_controls.refuse_over_limit(target):
                    return  # over the provider's stream limit: nothing opens
                target = replace(target, max_height=None)  # 2.8: asked once (see above)
                self._current, self._cid = target, None
                session = self.session  # the large picture's, whatever swaps before it opens
                logger.info(
                    "Tuning “%s” (%s) in the big picture: played directly, without the buffer.",
                    _log_name(item),
                    provider_name,
                )

                def play_direct() -> None:
                    self._prepare(session, target, previous)
                    session.play(
                        url,
                        kind=kind,
                        latency=self.config.live_latency,
                        **stream_options(item, self.catalog),
                    )
                    self._reset_speed(session)

                self._tuner.open(
                    self._guarded(target, play_direct), direct=True, provider=provider_name
                )
            if self.overlays.transport.isVisible():
                self._live_controls.show_live_transport(target)
        else:
            owner = self._resume_owner(item, provider_name)  # a link's: Network streams'
            cid = self.watch.content_id(owner, item, item_type=target.item_type)
            start: Optional[int]
            if target.start_ms is not None:  # 2.8: its quality changed: on from where it was
                start = target.start_ms
            else:  # 2.8: or where a media server says it stopped, unless QiTV saw it finished
                start = 0 if target.start_over else self.watch.resume_from(cid, item)
            changed = target.start_ms is not None
            # 2.8: a quality change's start is asked once: a later reopen of this target goes
            # where the watch state saved it, as any title's.
            target = replace(target, start_ms=None)
            self._current, self._cid = target, cid
            session = self.session

            def open_vod() -> None:
                self._prepare(session, target, previous)
                agent = None if target.local else item_provider.stream_agent(self.catalog, item)
                relay = self._film_relay(target, url, agent) if self._captions_on() else None
                session.play(
                    url,
                    kind=classify(url, False),
                    start_ms=start,
                    **({"user_agent": agent} if agent else {}),  # 2.8: an STB portal's box's
                    **({"relay": relay} if relay is not None else {}),  # 2.9: AI captions
                )
                self._reset_speed(session)

            self._tuner.open(self._guarded(target, open_vod), direct=True, provider=provider_name)
            if self._current is not None and self._current is not target:
                return  # its open failed at once: the channel kept small took the picture back
            if start and not changed:
                # i18n: a toast; {time} is where the film resumes, e.g. 1:02:05
                self._toast(tr("Resumed at {time}", time=fmt_duration(start)))
            self.overlays.banner.dismiss()  # the transport takes the bottom of the video
            self.overlays.transport.set_next_available(self._autoplay.has_next(target))
            self.overlays.transport.show_vod(target.title, target.subtitle)
        if self._current is target:  # else opening it failed at once (see _guarded)
            self._delivered(target, url)

    def _small_title_gone(self, url: str) -> None:
        """2.14 review I1: the film swapped small closed or ended there: its server hears it
        stop (a Plex conversion's session ends with it), unless it heard another start since
        (a phone's Watch, another title)."""
        if self._media is not None and url and self._media.active_url() == url:
            self._media.stopped()

    def _link_dropped(self, url: str) -> None:
        """2.14 review I2: a link made that will not play: a Plex conversion's session the
        server started for it is stopped there (off this thread: the reporter's)."""
        if self._media is not None:
            self._media.abandoned(url)

    def _plays_small(self, url: str) -> bool:
        """Whether the small picture plays the movie or episode at `url` (swapped small)."""
        sessions, pip = self.sessions, self._pip
        small = sessions.small() if sessions is not None else None
        if not url or small is None or pip is None or pip.small_title(small.name) is None:
            return False
        return pip.view(small.name).url == url

    def _guarded(self, target: Target, open_: Callable[[], None]) -> Callable[[], None]:
        """`open_` as an open once the old stream is released: now, or from a signal later."""

        def run() -> None:
            try:
                open_()
            except Exception as e:
                # What played before was stopped to open this: nothing is current now.
                self._current, self._cid = None, None
                if target.local:
                    self._flow.hold_for_file(False)  # the file could not open
                    self._end_sound()  # a song's screen kept from the song before it
                    self._opening_over()
                elif self._link_holds(target):
                    self._let_go_of_link()  # the link could not open
                self._log_failure(f"play {target.title}", e)
                if not target.live:
                    self._give_small_channel_large()
                self._restore_channel()
                self._error(self._named(target.title, str(e)))

        return run

    def _launch(
        self, target: Target, url: str, launcher: Optional[Launcher] = None, *, phone: bool = False
    ) -> None:
        """Hand the link to the external player (the in-app stream is released by now).

        `launcher` is the context menu's choice; by default, the settings' player. `phone`: a
        phone's Watch, whose stream is counted (`_phone_slot`).
        """
        launcher = launcher or self._external_launcher()
        if launcher is None:  # the setting changed meanwhile: play it here
            self._play_link(replace(target, zapped=False), url)
            return
        http: dict[str, Any] = {"local": True}  # a file's URI: QiTV's own (play_file)
        if not target.local:  # 2.8: asked as QiTV asks it (an STB portal's box's agent)
            agent = item_provider.stream_agent(self.catalog, target.item)
            referrer = str(target.item.get("referrer") or "")
            http = {k: v for k, v in (("user_agent", agent), ("referrer", referrer)) if v}
        if self._media is not None:
            self._media.stopped()  # 2.8: the server's title that played stopped where it was
        self._phone_slot.give_back()  # a newer outside play takes over (the phones stop)
        if phone and not target.local:
            self._phone_slot.set_purpose(self._tuner.purpose)
            # Held when one is free (never queued: the phone already streams).
            self._phone_slot.try_take(item_provider.provider_name(self.catalog, target.item), None)
        ok, message = launcher(url, **http)
        if ok and phone and not target.local and self._media is not None:
            self._media.playing(url)  # 2.8: its server hears it start; the phone says where
        if not ok:
            self._phone_slot.give_back()
            self._let_go_unless_a_link_plays()  # a link chosen held never came
            first, _, rest = (message or "").strip().partition("\n")
            if rest.strip():
                logger.info("External player: %s", scrub(rest))
            self._restore_channel()
            first = first.strip()
            self._menu_error(
                translate(first) if first else tr("The external player could not start")
            )
            return
        self.handedOn.emit()  # the phones that watched before let go
        self._current, self._cid = target, None
        self._delivered(target, url)
        if self._link_holds(target):
            # An outside player took the link: nothing plays in QiTV, so nothing holds the
            # welcome card back (final review M4); a file's hold under it stays the file's.
            if self._file_under_link:
                self._link_hold = self._file_under_link = False
            else:
                self._let_go_of_link()

    def phone_stopped(self) -> None:
        """2.8.12: the phone that watched left for good (`RemoteController`): the stream it
        held goes back."""
        self._phone_slot.give_back()

    def _delivered(self, target: Target, url: str) -> None:
        """The target is playing (or opening): confirm the zap and record it in the history."""
        self._current_url = url
        # 2.8: the ⚙ quality menu, for a media server's title its server offers choices for,
        # and a channel whose stream comes in several sizes (known once it played this run).
        if target.live:
            offered = self.live_menu() is not None
        else:
            menu = None if target.local else self._media_menu(target.item)
            offered = bool(menu)
            if menu and menu.get("refused") and getattr(target.quality, "converted", False):
                self._conversion_refused(target)
        self.overlays.transport.set_quality_available(offered)
        if self._quality is not None:
            self._quality.drop_question()  # one asked about what played before
        if self.sessions is not None:  # what the large picture shows (a zap onto it stays)
            large = self.sessions.large()
            large.channel = target.item if target.live else None
            large.group_key = target.group_key if target.live else None
        if target.live:
            self.zap.tuned(target.item)
        if not target.local:  # a file is no provider's: no history, as play_file
            self._record_watched(target, url)

    def _record_watched(self, target: Target, url: str) -> None:
        guard = self.parental
        if guard is not None and self._protects_play(guard, target):
            return  # 2.8: what parental controls hide never enters the history (nor Recent)
        # 2.14 review M5: a Plex conversion's session ends with its play: its file is kept.
        menu = None if target.live or target.local else self._media_menu(target.item)
        # A site's channel page is remembered as its page, not the address with its token.
        link = str((menu or {}).get("file") or self._site_link(target.item) or url)
        try:
            self.watch.record_play(
                item_provider.provider_name(self.catalog, target.item),
                target.item,
                item_type=target.item_type,
                link=link,
                group_id=target.group_key if target.live else None,
                # A link is never the channel QiTV starts on (its token may be gone by then):
                # the provider's channel watched before it stays (final review M2).
                last_channel=self._network_url(target.item) is None,
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
        cast = self._cast
        small = (self.session, sessions.large().live, self._current)  # small after the swap
        if not sessions.swap():
            return False
        if cast is not None and cast.casting():
            cast.swapped(*small)  # 2.8: the cast follows the large picture
        return True

    def open_pip(self, channel: dict, *, group_key: str, channels: list[dict]) -> None:
        """Open `channel` small (the drawer's menu or Ctrl/⌘+P), or show it in the PiP."""
        if self.multiview_shown():
            self._toast(LEAVE_FIRST)  # the grid's tiles are the pictures meanwhile
            return
        if self._pip is not None and not self._closed:
            source = item_provider.pick_source(self.catalog, channel, group_key)
            context = ZapContext(group_key, channels, fallback=self._all_live(), source=source)
            self._pip.open_channel(channel, context)

    def _pip_takes(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """A tune of the large picture onto the PiP's channel: never a second stream of it.
        Shown small, the pictures swap; on its way there, it opens large."""
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
        """The small picture closes for something that starts large (a film, a file, an
        outside player, a cast) or the phone's Close: a stop that counts (`_count_start`)."""
        if self._pip is not None:
            self._count_start()
            self._pip.close()

    def _count_start(self) -> None:
        """Something starts or stops in a picture, or the viewer changes the pictures: an open
        waiting for a stopped recording's stream runs only if nothing did since it was
        answered (StreamQuestion.started). Called by the shared start and stop paths (a link
        chosen now, a file, Stop, the small picture's open and close); not by QiTV's own
        swap(), which starts nothing, nor counted while that open runs."""
        self._question.started()

    # ---- the multiview ------------------------------------------------------------------------

    def multiview_shown(self) -> bool:
        """The multiview's grid shows: its focused tile is the large picture."""
        return self._grid is not None and self._grid.shown()

    def toggle_multiview(self) -> None:
        """V, View → Multiview, the grid button: the grid, or back to one picture."""
        if self._grid is None or self._closed:
            return
        if not self._grid.big():
            self._count_start()  # the grid, or one picture again (a big tile's grid: neither)
        if self._cast is not None and self._cast.casting() and not self._grid.shown():
            self._toast(MULTIVIEW_WHILE_CASTING)  # 2.8: one picture goes to the device
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
        self._count_start()  # the focus moves the large picture's role
        return sessions.set_large(name)

    def _shown_elsewhere(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """A tune onto a channel another picture shows: never a second stream of it."""
        if self.multiview_shown():
            return self._grid_takes(channel, group_key, channels)
        return self._pip_takes(channel, group_key, channels) or self._background_takes(
            channel, group_key, channels
        )

    # ---- 2.6: recordings in the background ----------------------------------------------------

    def answer_phone(self, yes: bool) -> None:
        """A phone's answer to the question QiTV asked it (`phoneAsked`)."""
        if not self._closed:
            self._question.answer(yes)

    def rec_list(self) -> list[tuple[str, str, float]]:
        """What records in the background: (player, channel name, recording since, on
        LiveSession's clock), the oldest first."""
        sessions = self.sessions
        recordings: list[tuple[str, str, float]] = []
        if sessions is not None and not self._closed:
            for slot in sessions.background():
                since = slot.live.recording_since() if records(slot) else None
                if since is not None:
                    name = str((slot.channel or {}).get("name") or "")
                    recordings.append((slot.name, name, since))
        if not self._closed:  # 2.8: what records on schedule (no player of its own)
            recordings += [
                (name, channel, since) for name, channel, since, _ in self.scheduled_now()
            ]
        return sorted(recordings, key=lambda entry: entry[2])

    def scheduled_changed(self) -> None:
        """What records on schedule changed (2.8): the REC sign follows, and a background
        player kept for a next programme that did not start on it lets go (2.9.7)."""
        if not self._closed:
            if self.sessions is not None:
                self.sessions.recheck_aside()
            self._refresh_rec_sign()

    def asks_before_quit(self, then: Callable[[], object]) -> bool:
        """QiTV is to close while channels record: True, asked (Quit stops them, saved, then
        `then` closes); False: nothing records."""
        return self._question.quit(then)

    def quit_note(self) -> str:
        """The quit question while channels record ("" while none does): the self-update's.
        2.8: those that record on schedule count too."""
        recording = self._all_recordings()
        return quit_text(recording) if recording else ""

    def _refresh_rec_sign(self) -> None:
        """The REC sign lists the recordings in the background (the big picture's own shows
        on its controls, the small picture's on its label)."""
        recordings = self.rec_list()
        for sign in (self.overlays.rec_sign, self.overlays.library_rec_sign):
            if sign is not None:
                sign.set_recordings(recordings)

    def go_back_to_recording(self, name: str) -> None:
        """The REC sign's Go back (the phone's too): the channel player `name` records in the
        background plays large again, from its group."""
        sessions = self.sessions
        if self._closed:
            return
        self._count_start()  # its player plays large again (a swap, a focus or a tune)
        scheduled = next((s for s in self.scheduled_now() if s[0] == name), None)
        if scheduled is not None:  # 2.8: a scheduled recording's channel, live
            self.leave_multiview()
            # 2.9.7: the list's own channel (the schedule keeps its address without the
            # account); it plays the recording's session, with no stream of its own.
            self.tune_live(self._listed(scheduled[3]), ALL_GROUP_ID, self._all_live())
            return
        if sessions is None:
            return
        name = self.scheduled_host(name) or name  # 2.9.7: one a player records: that player
        if sessions.large().name == name:
            return  # it shows large already
        background = sessions.background()
        if all(s.name != name for s in background):  # release review M2: one that shows
            if self.multiview_shown():
                self.focus_tile(name)  # a tile: it takes the focus
            elif sessions.pip_open() and getattr(sessions.small(), "name", None) == name:
                self.swap()  # the small picture: it plays large
            return
        slot = next((s for s in background if s.name == name), None)
        if slot is None or slot.channel is None:
            return  # gone meanwhile (a phone's stale REC line)
        self.leave_multiview()  # the grid's tiles never show a background player
        key = slot.group_key or ALL_GROUP_ID
        channels = list(self.catalog.items("live", key) or []) or self._all_live()
        self.tune_live(slot.channel, key, channels)

    def stop_background_recording(self, name: str) -> None:
        """The REC sign's Stop recording (the phone's too): saved, and its player leaves the
        channel. Nothing for a player not in the background."""
        sessions = self.sessions
        if self._closed:
            return
        if any(s[0] == name for s in self.scheduled_now()):
            self.stop_scheduled(name)  # 2.8: a scheduled recording: this airing stops
            return
        if sessions is None:
            return
        for slot in sessions.background():
            if slot.name == name:
                self.stop_player_recording(slot.live)

    def stop_player_recording(self, live: Any) -> None:
        """2.9.7: the user stops the recording on player `live`: said to the scheduler first
        (a scheduled recording hosted there ends as the user's stop), then stopped."""
        self.recording_user_stop(live)
        live.stop_recording()

    def players_showing(self) -> list[tuple[str, str, dict, Any]]:
        """2.9.7: each player showing a live channel: (its name, the channel's provider, the
        channel, its LiveSession), for a scheduled recording to record on (no stream of its
        own)."""
        sessions = self.sessions
        if sessions is None or self._closed:
            return []
        found = []
        for slot in sessions.slots():
            channel = slot.channel
            if channel:
                provider = item_provider.provider_name(self.catalog, channel)
                found.append((slot.name, provider, channel, slot.live))
        return found

    def _recordings(self) -> list[Slot]:
        """Every player that records, the oldest recording first."""
        sessions = self.sessions
        if sessions is None:
            return []
        recording = [slot for slot in sessions.slots() if records(slot)]
        return sorted(recording, key=lambda slot: slot.live.recording_since() or 0.0)

    def _all_recordings(self) -> list[Any]:
        """Every player that records and every recording on schedule (2.8: no player, a
        stream of its own), the oldest first: what the stream question and Quit count."""
        recording: list[tuple[float, Any]] = [
            (slot.live.recording_since() or 0.0, slot) for slot in self._recordings()
        ]
        if not self._closed:
            recording += [
                (since, ScheduledRecording(name, dict(channel or {}), since))
                for name, _channel, since, channel in self.scheduled_now()
            ]
        return [entry for _since, entry in sorted(recording, key=lambda pair: pair[0])]

    def _question_card(self) -> Any:
        """The question's card on the page that shows (the Library's while it shows, the
        player in its corner or not)."""
        library = self._flow.in_library() if self._page is None else self._page != "player"
        card = self.overlays.library_question
        if card is None or not library:
            card = self.overlays.question
        return card

    def page_changed(self, mode: str) -> None:
        """The window shows another page (PageStack's mode): a question goes along."""
        self._page = mode
        self._question.follow_page()

    def answer_key(self, action: Action) -> bool:
        """A key while QiTV asks (any page): True when it is the question's."""
        return not self._closed and self._question.handle(action)

    def _ask_for_stream(
        self,
        item: dict,
        purpose: str,
        then: Callable[[], None],
        *,
        gives_way: bool = False,
        ask_on: Optional[str] = None,
    ) -> bool:
        """Opening `item` over `purpose`'s stream needs one that a recording holds: True,
        asked (StreamQuestion.needs; on phone `ask_on` when it asked); `then` runs once the
        recording let it go."""
        provider = item_provider.provider_name(self.catalog, item)
        limit = self._live_controls.limit(item)
        return self._question.needs(
            provider, limit, purpose, then, gives_way=gives_way, ask_on=ask_on
        )

    def _asks_first(
        self,
        channel: dict,
        group_key: str,
        channels: list[dict],
        archive: Optional[tuple[float, Optional[float]]],
    ) -> bool:
        """A tune that would stop a recording, or needs the stream one holds: asked."""

        def again() -> None:
            self._tune(channel, group_key, channels, from_zap=False, archive=archive)

        sessions = self.sessions
        if self.multiview_shown() and sessions is not None:
            if self._question.confirm(sessions.large(), again):
                return True  # a tile has no background to record on in
        gives_way = self._external_launcher() is not None
        provider = item_provider.provider_name(self.catalog, channel)
        # 2.9.7: its recording's session plays it: no stream to free. Not in a tile, which
        # opens a stream of its own (release review M1).
        lends = not gives_way and not self.multiview_shown()
        if lends and self.lendable_scheduled(channel, provider):
            return False
        return self._ask_for_stream(channel, self._tuner.purpose, again, gives_way=gives_way)

    def _confirm_tile(self, name: str, then: Callable[[], None]) -> bool:
        """A multiview tile that records would close: asked first."""
        sessions = self.sessions
        return sessions is not None and self._question.confirm(sessions.slot(name), then)

    def _stop_for_stream(self, slot: Any, replaced: bool) -> None:
        """Stop recording: the user said so for a stream. Unless the open that asked takes
        its stream over (`replaced`), its player lets the stream go: a background one by
        itself, a small picture closes, a tile closes. 2.8: a scheduled recording's airing
        stops (as the REC sign's Stop), its stream going back once its session closed."""
        if isinstance(slot, ScheduledRecording):
            self.stop_scheduled(slot.name)
            return
        sessions = self.sessions
        background = sessions is not None and slot in sessions.background()
        self.stop_player_recording(slot.live)
        if replaced or background or sessions is None:
            return
        if sessions.small() is slot and self._pip is not None:
            self._pip.close(keep_recording=False)
        elif self.multiview_shown() and self._grid is not None and slot is not sessions.large():
            self._grid.close_tile(slot.name)

    def _stop_for_guard(self, slot: Slot) -> None:
        """RecordGuard: a stream its provider closed a recording's for stops, by its role."""
        sessions = self.sessions
        if sessions is None or self._closed:
            return
        if slot is sessions.large():
            self._lanes.drop_tune()
            self._live_controls.stop_tune()
            self._current, self._cid = None, None
            self._restore_channel()
        elif sessions.small() is slot and self._pip is not None:
            self._pip.close(keep_recording=False)
        elif self.multiview_shown() and self._grid is not None:
            self._grid.close_tile(slot.name)
        else:
            self._tuner_of(slot).stop()

    def _large_records(self) -> bool:
        live = self.live
        return live is not None and bool(getattr(live, "recording", lambda: False)())

    def _set_recording_aside(self) -> bool:
        """The large picture's channel is left while it records: its player records on in
        the background and another takes the large picture (SessionManager.set_aside). False:
        it does not record, the grid shows (its tiles are named), or no player could be had
        (then it is left as before)."""
        sessions = self.sessions
        if sessions is None or self._closed or self.multiview_shown() or not self._large_records():
            return False
        return sessions.set_aside(sessions.large().name) is not None

    def _background_takes(self, channel: dict, group_key: str, channels: list[dict]) -> bool:
        """A tune onto the channel a background player records: that player is large again
        (live, where the user was offered), never a second stream of it."""
        sessions, pip = self.sessions, self._pip
        slot = sessions.background_showing(channel) if sessions is not None else None
        if slot is None or pip is None:
            return False
        pip.view(slot.name).context = ZapContext(
            group_key,
            channels,
            fallback=self._all_live(),
            source=item_provider.pick_source(self.catalog, channel, group_key),
        )
        self.overlays.column.close()
        self._cancel_external()
        self._lanes.drop_tune()  # a choice on its way for the large picture is superseded
        return sessions.bring_back(slot.name) is not None

    def _on_set_aside(self, name: str, fresh: str) -> None:
        """Slot `name` records on in the background; `fresh` took its role. The large role
        is the shell's to follow (the small one's is PipControls')."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or self._closed or sessions.large().name != fresh:
            return
        slot = sessions.slot(name)
        current = self._current
        pip.take_large(name, fresh, current, self.zap.context, self._current_url, self._cid)
        pip.forget_view(fresh)  # a stand-in shows nothing yet
        slot.channel = current.item if current is not None and current.live else None
        slot.group_key = current.group_key if current is not None and current.live else None
        self._use_slot(sessions.slot(fresh))
        self._current, self._cid, self._current_url = None, None, ""
        self._state = self._states.get(fresh, "idle")
        pip.follow_roles()
        slot.live.set_aside()

    def _on_brought_back(self, name: str, old_name: str) -> None:
        """Background slot `name` is the large picture again: the channel it records plays
        live, where the user was is offered, and the stand-in leaves what it showed."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or self._closed:
            return
        slot, old = sessions.slot(name), sessions.slot(old_name)
        current = self._current
        view = pip.take_large(
            old_name, name, current, self.zap.context, self._current_url, self._cid
        )
        self._live_controls.new_target()  # the stand-in's ticks and held notice
        if any(s is old for s in sessions.background()):
            old.live.set_aside()  # it records too: on in the background (Stage C review C1)
        else:
            self._tuner.stop()  # the stand-in leaves its channel (its buffer is kept)
            old.channel = old.group_key = None
            pip.forget_view(old_name)
        self._use_slot(slot)
        target = view.target
        if target is None and slot.channel is not None:  # a multiview tile's: no view kept
            title = str(slot.channel.get("name") or "")
            target = Target(slot.channel, "live", title, group_key=slot.group_key or "")
        self._before_new_target(target)
        self._current, self._cid, self._current_url = target, view.cid, view.url
        self._state = self._states.get(name, "idle")
        self._sound_after_swap()  # a radio channel brought back: its screen as it plays
        self._tuning.stop()
        self._autoplay.drop()  # the Next card was the stand-in's
        if self._digits:
            self.zap.cancel_digits()
        context = view.context
        if context is not None and context is not self.zap.context:
            self.zap.set_context(context)
        pip.follow_roles()
        slot.live.bring_back()
        self._restore_channel()
        self.overlays.transport.set_paused(False)
        current = self._current
        if current is not None and self._channel is not None:
            self.zap.tuned(self._channel)
            self._record_watched(current, view.url)
            self._live_controls.show_live_transport(current)
            self._show_banner(self._channel)
        self._tracks.refresh()
        self._picture.refresh()
        self._update_guides()

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
        if self._live_controls.through_pipeline(url, kind, item):
            # In memory: the session manager sizes the buffer as the tile starts (focused
            # then, or not: `SessionManager.tile_bytes`).
            start = self._live_controls.live_start(target, url)
            kwargs = tune_kwargs(start, self.config, disk=False)
            grid.open_focused(item, provider=provider_name, limit=limit, start_kwargs=kwargs)
            # The old stream is stopped: the channel's sync waits for its player's open.
            self._prepare(session, target, previous)
            self.overlays.transport.set_speed(1.0)  # LiveSession starts every tune at 1x
            return

        def play_direct() -> None:
            self._prepare(session, target, previous)
            session.play(
                url,
                kind=kind,
                latency=self.config.live_latency,
                **stream_options(item, self.catalog),
            )
            self._reset_speed(session)

        grid.open_focused(
            item, self._guarded(target, play_direct), provider=provider_name, limit=limit
        )

    def _fail_focused_tile(self, *, ended: bool = False) -> None:
        """The large picture's channel failed for good (said): in the multiview, its focused
        tile stops and shows a card, as a tile beside the focus does (GridControls)."""
        if self._grid is not None and self.multiview_shown():
            self._grid.focused_failed(ended=ended)

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
        if self._hidden_now(channel):
            return  # 2.12: hidden, and locked
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
        channel = self._owned(channel)  # a link: whose stream it is now
        if self._hidden_now(channel):
            return  # 2.12: hidden, and locked: no tile plays it
        if name not in grid.names():
            return
        listed = channels if channels is not None else self._all_live()
        if name == sessions.large().name:
            self.tune(channel, group_key=group_key, channels=listed)
            return
        if grid.tile_showing(channel) not in (None, name) or grid.refuse(channel, name):
            return  # another tile shows it, or its provider has no stream to spare
        if not grid.linking(name, channel):
            return  # its player could not be made (said)
        title = str(channel.get("name") or "")
        source = item_provider.pick_source(self.catalog, channel, group_key)
        target = Target(channel, "live", title, group_key=group_key, source=source, retried=retried)
        context = ZapContext(group_key, listed, fallback=self._all_live(), source=source)
        self._tile_links[name] = target
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
        if self._live_controls.through_pipeline(url, kind, item):
            kwargs = tune_kwargs(
                self._live_controls.live_start(target, url), self.config, disk=False
            )
            grid.open_tile(name, item, provider=provider_name, limit=limit, start_kwargs=kwargs)
        else:
            session = slot.playback

            def play_direct() -> None:
                session.play(
                    url,
                    kind=kind,
                    latency=self.config.live_latency,
                    **stream_options(item, self.catalog),
                )

            grid.open_tile(name, item, play_direct, provider=provider_name, limit=limit)
        self._prepare_small(slot.playback, item)  # its sound sync and shape, kept per channel

    def _forget_tile(self, name: str) -> None:
        """Tile `name` left the grid (a smaller layout, or the grid went): its link on its way
        and its wait for a provider are dropped, so nothing opens in it."""
        self._tile_links.pop(name, None)
        self._waiting_tiles = [entry for entry in self._waiting_tiles if entry[0] != name]

    def _focused_link(self) -> Optional[dict]:
        """The live channel whose link the focused tile waits for (the grid counts it as a
        stream of its provider)."""
        pending = self._lanes.pending()
        return pending.item if pending is not None and pending.live else None

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
        if self._hidden_now(channel):
            return  # 2.12: hidden since, and locked: its tile stays "+"
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
        """A tile without the focus failed: its channel's link is made again and it opens in
        that tile; the grid says when (an STB portal's at once, once: its portal's session may
        have expired, so it signs in again; a site's channel refused, at once: its lane asks
        the site again; any channel that played, after the revive's wait, `services.revive`)."""
        logger.info("Making the link of a tile's channel again after it failed")
        if item_provider.provider_type(self.catalog, channel) == "STB":
            item_provider.sign_in_again(self.catalog, channel)
        self.play_in_tile(name, channel, retried=True)

    def _drop_pip_on_its_way(self) -> None:
        """The grid shows: a channel still on its way to the small picture never opens."""
        if self._pip is not None and self._pip.on_its_way():
            self._pip.close()

    def _mixer_tiles(self) -> Optional[list[tuple[str, str, str]]]:
        return self._grid.mixer_rows() if self._grid is not None else None

    def _on_tile_control(self, name: str, control: str) -> None:
        """A multiview tile's own control (its strip): it acts on that tile, the focus stays.
        "play" pauses or plays it (the focused one as Space does), "live" goes live,
        "speaker" mutes or unmutes it, "close" stops the focused tile's stream and "closed"
        forgets what a closed tile without the focus showed."""
        sessions = self.sessions
        if self._closed or sessions is None:
            return
        try:
            slot = sessions.slot(name)
        except KeyError:
            return
        focused = slot is sessions.large()
        if control == "play":
            if focused:
                self._play_pause()
            else:
                self._tuner_of(slot).toggle_pause()
        elif control == "live":
            if focused:
                self._live_controls.go_live()
            else:
                self._tuner_of(slot).go_live()
        elif control == "speaker":
            self._mixer.toggle_mute_of(name)
        elif control == "close" and focused:
            self._close_focused_tile()
        elif control == "closed" and self._pip is not None:
            self._pip.forget_view(name)  # it takes the focus as "+", never as its old channel

    def _close_focused_tile(self) -> None:
        """The focused tile's ✕: its stream stops (its provider's stream goes back once
        released), a link on its way never opens, and the tile shows "+" with the focus; the
        player controls are the grid's until a channel is picked there."""
        sessions = self.sessions
        if sessions is None:
            return
        self._lanes.drop_tune()
        self._tuning.stop()
        if self._digits:
            self.zap.cancel_digits()
        self._tracks.close()
        self._live_controls.stop_tune()
        large = sessions.large()
        large.channel = large.group_key = None
        self._current, self._cid, self._current_url = None, None, ""
        self._restore_channel()
        self.overlays.banner.dismiss()
        if self.overlays.transport.isVisible():
            self._show_grid_transport()

    def _open_pip_node(self, node: NavNode) -> None:
        """The channel list's "Open in picture-in-picture": never over the grid's tiles."""
        if self.multiview_shown():
            self._toast(LEAVE_FIRST)
        elif self._pip is not None:
            self._pip.open_node(node)

    def _on_large_changed(self, name: str, old_name: str) -> None:
        """A swap: the large picture's player, stream and state are slot `name`'s now, and
        slot `old_name` shows what the large picture showed."""
        sessions, pip = self.sessions, self._pip
        if sessions is None or pip is None or self._closed:
            return
        new, old = sessions.slot(name), sessions.slot(old_name)
        # An ended stream on its way to open again goes with its player, in its new role.
        reopens = self._live_controls.take_reopen(), pip.take_reopen(new.name)
        view = pip.take_large(
            old.name, new.name, self._current, self.zap.context, self._current_url, self._cid
        )
        shown = self._current if self._current is not None and self._current.live else None
        old.channel = shown.item if shown is not None else None  # what the small one shows
        old.group_key = shown.group_key if shown is not None else None
        self._use_slot(new)
        self._before_new_target(view.target)
        # A movie's resume key comes back with its slot: its position is saved again.
        self._current, self._cid, self._current_url = view.target, view.cid, view.url
        large_reopen, small_reopen = reopens
        if small_reopen is not None:
            self._live_controls.adopt_reopen(small_reopen)
        if large_reopen is not None and not self.multiview_shown():
            pip.adopt_reopen(old.name, large_reopen)
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
        self._sound_after_swap()  # a radio channel swapped in: its screen, without a new state
        grid = self.multiview_shown()  # a tile took the focus: the grid shows it
        if channel is not None:
            self.zap.tuned(channel)
            current = self._current
            if current is not None:
                # Recent and the next start's channel: a swap's, and a tile's taking the focus.
                self._record_watched(current, view.url)
                self._archive_after_small()
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
        self._picture.refresh()  # and the Picture panel and the statistics its picture
        if grid:
            drawer = self.overlays.drawer
            if drawer.isVisible() and not self._settings_shown() and not drawer.finding():
                self._open_drawer()  # the channel list follows the focus: that tile's place
            if large_reopen is not None and self._grid is not None:
                # A tile beside the focus never reconnects by itself (its direct stream that
                # ends fails): one the focus left during its back-off says its stream ended.
                self._grid.stream_ended(old.name)
            return  # the grid follows the focus itself (its ring, the tiles' labels)
        pip.after_swap()
        if self._tuner_of(old).idle():
            pip.close()  # the old large picture was between two channels: nothing to show

    def _archive_after_small(self) -> None:
        """A swap: a channel's archive that ended, or failed, while it was small goes on as at
        its end now that it is large (its next stretch, or live), never a dead archive bar."""
        catchup = self._live_controls.catchup
        if not catchup.active():
            return
        if self._state == "ended":
            catchup.on_ended()
        elif self._state == "error":
            catchup.on_error("")

    def _use_slot(self, slot: Slot) -> None:
        """Act on `slot`'s player, live session and tuner (it is large now)."""
        tuner = slot.tuner
        if not isinstance(tuner, LiveTuner):
            raise TypeError("Each picture-in-picture slot needs a LiveTuner")
        if self._wired:
            self._connect_session(self.session, False)
        self.session, self.live, self._tuner = slot.playback, slot.live, tuner
        self._phone_slot.give_back()  # another player shows large: no phone plays for it
        if self._wired:
            self._connect_session(slot.playback, True)
        self._live_controls.use_slot(tuner, slot.live, slot.playback)

    def _connect_session(self, session, connect: bool) -> None:
        pairs = [
            (session.stateChanged, self._on_state),
            (session.positionChanged, self._on_position),
            (session.errorOccurred, self._on_error),
            (session.ended, self._on_ended),
        ]
        shown = getattr(session, "pictureShown", None)
        if shown is not None:  # a picture shows: the audio screen goes
            pairs.append((shown, self._on_picture_shown))
        for signal, slot in pairs:
            if connect:
                signal.connect(slot)
            else:
                signal.disconnect(slot)

    def apply_deinterlacing(self) -> None:
        """The players have the deinterlacing Settings saved (a step in the drawer)."""
        self._picture.apply_deinterlacing()

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
        self._watch_recordings(slot.live)
        if self._guard is not None:
            self._guard.watch_slot(slot)
        if self._pip is not None:
            self._pip.watch_slot(slot)
        if self._grid is not None:
            self._grid.watch_slot(slot)
        self._picture.add_player(slot.playback)  # deinterlacing, as every player's

    def _lives(self) -> list[Any]:
        """Every player's live session (the large picture's alone without picture in picture)."""
        sessions = self.sessions
        if sessions is None:
            return [self.live] if self.live is not None else []
        return [slot.live for slot in sessions.slots()]

    def _watch_recordings(self, live) -> None:
        """A recording of any player's channel says how it ended (2.2): saved, or why not."""
        live.recordingEnded.connect(self._on_recording_ended)

    def _on_recording_ended(self, reason: str, path: str, written: int) -> None:
        if self._closed:
            return
        if path:
            self.note_recording_end(path)  # 2.9.7: its tile's length is what the file holds
        text, kind = recording.ended_text(reason, path, written)
        self._toast(text, kind, ERROR_MS if kind == "error" else RECORDING_MS)

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

    def _before_new_target(self, target: Optional[Target], *, delivered: bool = False) -> None:
        """`target` replaces what the large picture shows (None: an outside player or a phone
        takes it, nothing plays here). A file opened in QiTV gives way to anything but another
        file: what waited for it (the welcome card, the update question) may come now, and
        QiTV's mark over it goes (_opening_over). A file still being looked at gives way too,
        to a choice made now (a swap, a bring-back, an outside player), not to a link
        `delivered` for a choice made before it was opened. A link played with no provider
        holds on as a file would (_hold_for_link): what waits, waits until it is over.
        AI captions that came on by themselves for a video file go off before anything else
        opens (CaptionsController.target_changing). The audio screen and its dock go too, but
        for a song after a song (no black page between two)."""
        self._sound_gives_way(target)
        self._forget_kept_listings(target)  # 2.15: a recording's kept listing goes with its film
        if target is None or not target.local:
            self._list_job.stop()  # a list of songs given up: its songs stop being read
            if not delivered:
                self._file_gives_way()
            # A link after the held link keeps its hold (the file it replaces gave way: the hold
            # is the link's alone); once a provider exists it plays as a channel does, and the
            # hold goes.
            if not self._link_holds(target) or self.catalog.provider_names():
                self._link_hold = False
                self._flow.hold_for_file(False)
            self._file_under_link = False
            self._opening_over()
        else:
            self._link_hold = self._file_under_link = False  # a file's rules from now on
        captions = getattr(self.window_actions, "captions", None)
        if captions is not None:
            captions.target_changing(target is not None and target.video_file)

    def _opening_over(self) -> None:
        """A file opened in QiTV no longer opens in the large picture (it failed or ended,
        another title replaces it, Stop): QiTV's mark over it goes (TVWindow.opening_over)."""
        over = getattr(self.window_actions, "opening_over", None)
        if callable(over):
            over()

    def _file_gives_way(self) -> None:
        """Something other than a file was chosen for the large picture: a file opened before
        it, still being looked at, isn't played once its check is back (TVWindow); the later
        choice wins."""
        gives_way = getattr(self.window_actions, "file_gives_way", None)
        if callable(gives_way):
            gives_way()

    def _video_file_playing(self) -> None:
        """The large picture's player plays: a video file's first time (not a pause's end, nor
        a song) lets AI captions come back on for it (CaptionsController.local_video_playing)."""
        target = self._current
        if target is None or target is self._video_played or not target.video_file:
            return
        if self.session.url != target.item.get("cmd"):
            return  # what played before it, still reported
        self._video_played = target
        captions = getattr(self.window_actions, "captions", None)
        if captions is not None:
            captions.local_video_playing()

    def own_subtitles_shown(self) -> bool:
        """The large picture shows one of its own subtitle tracks, or is about to: a subtitle
        file chosen (libVLC lists its track a little later), or files opened with the video
        that load once it plays (the window's)."""
        if self._closed:
            return False
        if any(shown for _id, _name, shown in self._tracks.subtitle_choices()):
            return True
        if self.session.subtitle_file() is not None:
            return True
        due = getattr(self.window_actions, "subtitles_due", None)
        return callable(due) and bool(due())

    def _prepare(self, session, target: Target, previous: Optional[Target]) -> None:
        """`target` is about to open in `session`, the large picture's player: its sound, its
        subtitles and its picture's shape as the viewer set them (a retry keeps the player's)."""
        self._tracks.prepare(session, target, previous)
        self._picture.prepare(session, target, previous)

    def _prepare_small(self, session, channel: dict) -> None:
        """A channel opens in a small picture's player: its sound sync and shape."""
        self._tracks.prepare_small(session, channel)
        self._picture.prepare_small(session, channel)

    def _live_lag(self) -> Optional[float]:
        """How far the large picture's channel plays behind live, as the time-shift helper
        reports it (None: it plays directly, an archive, or nothing yet)."""
        live = self.live
        if live is None or not live.is_pipeline() or self._live_controls.catchup.active():
            return None
        timeline = self._tuner.timeline
        lag = timeline.get("lag") if timeline is not None else None
        return float(lag) if isinstance(lag, (int, float)) else None

    def back_label(self) -> Optional[str]:
        """When the channel come back to was left ("20:10"), while Resume is offered (2.6)."""
        return self._live_controls.back_label()

    def behind_live(self) -> float:
        """How far the large picture plays behind live once the viewer left live (paused, went
        back, or another speed): 0 while it stays live, its buffer aside, as the player shows
        Go live only then."""
        timeline = self._tuner.timeline
        if self._live_lag() is None or not isinstance(timeline, dict):
            return 0.0
        if timeline.get("mode", "live") == "live":
            return 0.0
        return live_parts.behind_in(timeline) or 0.0  # 2.6: the time away included

    def _behind_broadcaster(self) -> Optional[float]:
        """How far the large picture plays behind the broadcaster, by the dates a dated
        channel's pieces carry, as the time-shift helper reports it (None: as `_live_lag`,
        or the channel's pieces are undated)."""
        if self._live_lag() is None:
            return None
        timeline = self._tuner.timeline
        behind = timeline.get("behind_broadcaster") if isinstance(timeline, dict) else None
        return float(behind) if isinstance(behind, (int, float)) else None

    def _live_info(self) -> dict:
        """The statistics' source, lag and how far behind the broadcaster, for the large
        picture."""
        live = self.live
        kind = live.source_kind() if live is not None and live.is_pipeline() else ""
        return {
            "source": kind or SOURCE_DIRECT,
            "lag": self._live_lag(),
            "behind_broadcaster": self._behind_broadcaster(),
        }

    def _programme_left(self) -> Optional[float]:
        """Seconds until the programme the large picture's channel shows ends (a time-shifted
        channel's later by as much as it plays behind); None: not a channel, or not known."""
        target = self._playing_live()
        if target is None or self._live_controls.catchup.active():
            return None
        timeline = self._tuner.timeline
        lag = self._live_lag() or 0.0
        if lag and isinstance(timeline, dict):
            lag = live_parts.behind_in(timeline) or lag  # 2.6: the time away included
        watched = self._now() - timedelta(seconds=lag)
        for programme in self._programmes(target.item, watched, 1):
            if programme.is_on(watched):
                left = (programme.stop - watched).total_seconds()
                return left if left > 0 else None
        return None

    def _sync_key(self, item: dict) -> str:
        """A channel's key for its sound sync: its provider's and its own (as live_tuning's)."""
        return channel_key(item_provider.provider_name(self.catalog, item), item)

    def _toggle_pause(self) -> None:
        failed = self._failed_live()
        if failed is not None:
            self._retry_failed(failed)  # ▶ over a channel that failed to open
            return
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
            self._error(self._named(name, message))
            self._explain(name, name)

    def _ask_again(self, newest: Target) -> None:
        """A provider settled the newest choice's request: it asks for its link (again), or,
        when it is the marker that keeps the playing channel (see _tune), it is dropped."""
        if self._is_stay(newest):
            self._lanes.take_queued()
        else:
            self._request(newest, chosen_now=False)  # chosen before its provider was ready

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
        names.update(self._phone_guides)  # the channels the phone remote lists
        names.update(self.extra_guides())  # 2.8: a scheduled series follows its channel's guide
        names.update(self.view_guides())  # 2.9.7: the Recordings home's rows
        source = self.catalog.current_provider_name()
        if source != ALL_PROVIDERS:
            names.add(source)
        elif self._guide.is_open():
            names.update(provider_of(c) for c in self._guide.visible_channels())
        elif self.overlays.drawer.isVisible():
            names.update(self._drawer_providers())
        names.discard("")
        return names

    def pip_shown(self) -> bool:
        """Whether the small picture (picture in picture) shows or is opening."""
        sessions = self.sessions
        return sessions is not None and bool(sessions.pip_open() or sessions.pip_opening())

    def close_pip(self) -> None:
        """Close the small picture, whatever shows (the phone remote's Close picture in
        picture; P opens the channel list's highlighted channel while the list shows)."""
        if not self._closed:
            self._close_pip()

    def use_phone_guides(self, names: set[str]) -> None:
        """The providers of the channels the phone remote lists: their guides load too."""
        self._phone_guides = set(names)
        self._update_guides()

    def guide_loading(self, channel: dict) -> bool:
        """Whether `channel`'s provider guide is on its way (a load runs or waits, or its
        provider still sets up and then loads it): an empty timetable then says so instead of
        "no guide". A load that failed is not on its way."""
        name = provider_of(channel) or self.catalog.current_provider_name()
        catalogs = self.catalog.catalogs
        if name not in catalogs.names():
            return False
        catalog = catalogs.catalog(name)
        if catalog.epg_ready() or not catalog.epg_enabled:
            return bool(catalog.epg_loading())
        return bool(catalog.epg_loading() or catalog.is_loading())

    def channel_list_group(self) -> str:
        """The live group the channel list is in: the drawer's, else the one zapped through
        ("" when neither)."""
        group = self._drawer_group()
        return group.key if group is not None else self.zap.context.group_key

    def _guide_context(self) -> ZapContext:
        """What the guide lists: the zap group's channels, but in all mode the live group the
        drawer shows when it is another one (its channels may be any provider's)."""
        context: ZapContext = self.zap.context
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
        group: Optional[NavNode] = next((n for n in self.nav.path if n.layer == "group"), None)
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

    def update_guides(self) -> None:
        """The guides in use again (2.8: the schedule changed)."""
        self._update_guides()

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
            return translate(_PINNED_TITLES[group_key])
        for group in self.catalog.groups("live"):
            if group.id == group_key:
                # QiTV's own group names ("All", "Uncategorized") show translated; a
                # provider's keep theirs.
                return shown_label(str(group.title))
        # i18n: the fast-zap column's title over every live channel
        return tr("All channels") if group_key == ALL_GROUP_ID else group_key

    # ---- zap events ---------------------------------------------------------------------------

    def _on_zap_tune(self, channel: dict) -> None:
        context = self.zap.context
        self._tune(channel, context.group_key, context.channels, from_zap=True)

    def _on_column_requested(self, open_: bool) -> None:
        if self._closed:
            return  # zap timers may fire while the window is torn down
        column = self.overlays.column
        if open_:
            # The column stands alone over the video: the banner, and a
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
            # i18n: a toast after a channel number was typed that no channel has
            self._toast(tr("No channel {number}", number=number))

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
        self._live_controls.catchup.on_state(state)  # an archive that plays has answered
        if state in ("paused", "playing"):
            self.overlays.transport.set_paused(state == "paused")
        if state == "playing":
            self._skipped_to = None  # it plays: no longer a row of songs that can't
            self._live_controls.on_playing()  # direct play: a held "unavailable" shows now
            self._link_played()  # plan 4: a link that played is kept
            self._picture.on_playing()  # after the sleep timer: the computer stays awake again
            self._video_file_playing()
        self._sound_state(state)

    def _on_position(self, position_ms: int, length_ms: int) -> None:
        if self._closed or self._current is None or self._current.live:
            return
        if self._cid is not None:  # a channel has none; a title or a local file has one
            try:
                self.watch.update_position(self._cid, position_ms, length_ms)
            except Exception as e:
                self._log_failure("save the playback position", e)
        if self._media is not None:
            self._media.position(position_ms)  # 2.8: a server's title, every 10 s
        self.overlays.transport.update_position(position_ms, length_ms)

    def _on_small_position(self, name: str, position_ms: int, length_ms: int) -> None:
        """A movie or an episode swapped small still plays: its position is saved under its
        own resume key (and its end marks it finished), as while it was large, and its media
        server hears where it is (2.8) while it is the title the server heard start."""
        cid = self._pip.small_resume_key(name) if self._pip is not None else None
        if self._closed or cid is None:
            return
        try:
            self.watch.update_position(cid, position_ms, length_ms)
        except Exception as e:
            self._log_failure("save the playback position", e)
        if self._media is not None and self._pip is not None:
            self._media.position(position_ms, url=self._pip.view(name).url)

    def phone_position(self, url: str, position_ms: int) -> None:
        """Where a phone's Watch plays `url` (2.8): its media server hears it, while that is
        the title it heard start (QiTV handed it to the phone)."""
        if self._media is not None and not self._closed:
            self._media.position(position_ms, url=url)

    def _on_small_error(self, name: str, message: str) -> None:
        """A movie, an episode or a channel's archive swapped small failed to play: said as
        the large picture says it (a channel's player errors are its live session's to recover
        or report)."""
        pip = self._pip
        target = (pip.small_title(name) or pip.small_archive(name)) if pip is not None else None
        if not self._closed and target is not None:
            self._error(self._named(target.title, message))
            self._explain_item(target.item, target.title)

    def _on_error(self, message: str) -> None:
        if self._closed:
            return
        self._opening_over()  # the error toast shows instead (even as the file is played)
        current = self._current
        if current is not None and current.local:
            if self._skip_unplayable(current):
                return  # its album's next song plays instead
            self._flow.hold_for_file(False)  # the file failed to play
            self._end_sound()  # a song's screen and dock (an album stops there)
        if self._tuner.running():
            return  # LiveSession recovers pipeline errors and reports the rest (failed)
        if self._live_controls.catchup.on_error(message):
            return  # the archive's: it says so and goes live
        if current is not None and current.live:
            if self._live_controls.relink(current):
                return  # STB play tokens expire: a fresh link first (after a wait, again)
            if self._live_controls.reconnect(current, failed=True):
                return  # it played: it opens again by itself, in the same slot
        title = current.title if current is not None else ""
        if current is not None and not current.live:
            self._give_small_channel_large()
        self._error(self._named(title, message))
        if current is not None:
            if message != translate(NO_VLC_MESSAGE):  # QiTV's own trouble, not the link's
                self._on_link_failed(current, failure_reason(message))
            self._explain_item(current.item, title)
            if current.live:
                self._fail_focused_tile()
            if self._link_holds(current):
                self._let_go_of_link()  # it failed to play: as a file that failed

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

    def _skip_unplayable(self, current: Target) -> bool:
        """A song of an album or of a list of songs (a folder's too) can't be played (gone
        since the listing, broken): its next one plays, held, after a short toast, as a music
        player skips it (item 4). False, and the error ends the album as before, for a second
        in a row (the song a skip opened fails before it plays), with nothing after it, or
        with a newer choice on its way. A video's or a mixed list's error ends its list."""
        skipped_to, self._skipped_to = self._skipped_to, None
        queue = self._queue
        if current is skipped_to or queue is None or not self._album_plays():
            return False
        if self._lanes.busy():
            return False  # the newer choice plays when its link comes
        before = queue.current
        index = queue.next(auto=False)  # (repeat one: the next song, not this one again)
        if index is None or index == before:
            return False
        logger.info("A song that can't be played is skipped")
        # i18n: an error toast: a file opened in QiTV is gone or unreadable ({name}: its name)
        self._error(tr("Couldn't open “{name}”", name=current.title))
        self._play_track(index, start_over=False)
        self._skipped_to = self._current
        return True

    def _on_ended(self) -> None:
        if self._closed:
            return
        self._opening_over()  # a file that ended before any picture
        if self._current is None:
            return
        current = self._current
        if not current.live and self._media is not None:
            self._media.stopped()  # 2.8: a server's title ended where it was
        if current.live:
            if self._live_controls.catchup.on_ended():
                return  # the archive's next stretch opens, or the channel live
            if self._live_controls.pipeline_live():
                return  # the buffer reopens
            if self._live_controls.relink(current):
                return  # an expired STB token can end it at once: a fresh link
            live = self._live_controls.live
            revives = getattr(live, "revives", None) if self._tuner.running() else None
            if callable(revives) and revives():
                return  # LiveSession opens its direct play again itself, in the same slot
            if self._live_controls.reconnect(current):
                return  # the same stream opens again, in the same slot (the pill says when)
            if self._network_url(current.item) is not None:
                # A link ended for good: said, and kept with why (final review M5); a held
                # one lets go (_on_link_failed).
                self._fail_focused_tile(ended=True)
                self._on_link_failed(current, DOWN)
                return
            self._toast(tr("The stream ended"))  # i18n: a toast
            self._fail_focused_tile(ended=True)
            return
        try:
            self.watch.flush()
        except Exception as e:
            self._log_failure("save the watch state", e)
        if self._sound_goes_on(current):
            return  # the album's next track (or this one again) plays, held: no card
        if not self._autoplay.on_ended(current):
            if current.local:
                self._flow.hold_for_file(False)  # the file played to its end
            elif self._link_holds(current):
                self._let_go_of_link()  # a link's film played to its end
            self._toast(translate(FINISHED_TEXT))
        # Else the Next card offers what follows: a file's hold waits for it (Esc lets go).

    def _on_next_requested(self) -> None:
        """The transport's Next: what follows the movie or episode plays at once."""
        current = self._current
        if not self._closed and current is not None:
            self._autoplay.next_requested(current)

    def _still_current(self, target: Target) -> bool:
        """`target` still plays and nothing newer was chosen (autoplay may follow it)."""
        return target is self._current and not self._lanes.busy()

    # ---- the audio screen (spec §10, Plan 4) --------------------------------------------------

    def sound_shown(self) -> bool:
        """The audio screen shows what plays (a song, a file with no picture, a radio channel)."""
        return not self._closed and self._sound_shown

    def song_shown(self) -> bool:
        """The audio screen shows a song or a file with no picture (not a radio channel's): a
        song opened now keeps it (the window draws no mark over it)."""
        return self.sound_shown() and self._sound_kind in ("song", "file")

    def panel_facts(self) -> Optional[tuple[str, str, str]]:
        """What the system's media panel shows of the audio screen (TVWindow._show_now_playing,
        with the screen's cover): the song's title, artist and album; a radio channel's name
        alone (its stream's song or programme is no artist). None while the screen shows
        nothing of what plays now (a film, a channel's picture, a song opening after another
        song's facts): the panel has the title alone, as for a film."""
        current = self._current
        if not self.sound_shown() or current is None or current is not self._sound:
            return None
        if self._sound_kind == "radio":
            return (current.title, "", "")
        facts = self._song_facts
        if self._sound_kind not in ("song", "file") or facts is None or facts[0] is not current:
            return None
        return facts[1:]

    def _screen(self) -> Any:
        return self.overlays.audio_screen

    @staticmethod
    def _local_path(target: Target) -> str:
        return QUrl(str(target.item.get("cmd") or "")).toLocalFile()

    def _sound_file(self, target: Target) -> str:
        """`target`'s file, as it was opened (the album lists it so), else from its URL."""
        if target is self._sound and self._sound_path:
            return self._sound_path
        return self._local_path(target)

    def _is_song(self, target: Optional[Target]) -> bool:
        return target is not None and target.local and is_music(self._local_path(target))

    def _sound_opens(self, target: Target, path: str, *, in_list: bool) -> bool:
        """A local file opens in the large picture (_play_local). A song: its dock now, its
        screen at its first `playing` (True), its album listed on a thread; the same album's
        song takes the list it has (one listing an album). Another file: looked at once it
        plays (False: a film's controls meanwhile). A playlist's song (`in_list`) follows its
        list, never its folder's album: a list of songs is its album (_list_song_opens), a
        mixed list's song a lone song."""
        screen = self._screen()
        if screen is None:
            return False
        self._sound_poll.stop()
        self._sound, self._sound_in_list = target, in_list
        # Its path as given (the album's own spelling: a file URL's round trip turns Windows'
        # backslashes into slashes, and the album's list would no longer know it).
        self._sound_path = path
        if not is_music(path):
            self._sound_kind = "file"
            return False
        self._sound_kind = "song"
        listed = self._list if in_list else None
        if listed is not None and listed.songs:
            return self._list_song_opens(screen, target, path, listed)
        queue = self._queue
        index = None
        if queue is not None and not in_list and self._song_list() is None:
            index = queue.index_of(path)  # (a song opened by hand gives a list of songs up)
        if queue is not None and index is not None:
            queue.pick(index)  # the same album: its list stays, on this song's row now
            screen.set_tracks(queue.tracks, index)
            if self._sound_shown:  # the listing's tags until the player's (as it plays)
                self._show_song(screen, queue.track(index), path, target)
            return True
        self._queue = None
        screen.set_tracks([], -1)  # another album's list goes at once
        screen.set_view(self._opening_view())
        if not in_list:
            self._album_generation += 1
            self._album_for = (target, path)
            self._album_job.start(self._album_generation, path)
        return True

    def _list_song_opens(self, screen: Any, target: Target, path: str, listed: _LocalList) -> bool:
        """A list of songs' entry opens (its row: `listed.index`): the list is its album (on
        the Album view, unless a view was picked), its queue the one it plays through while
        its songs play on; after anything else (a film, a Stop) one anew from this entry, as
        shuffle and repeat are now, and QiTV's drawn cover until the song's own. The song: its
        dock now, its screen at its first `playing` (_show_sound). Always True."""
        index = listed.index
        queue = self._queue
        if queue is None or queue is not listed.queue:
            queue = AudioQueue(
                listed.tracks, index, shuffle=self._shuffle_setting(), repeat=self._repeat_setting()
            )
            listed.queue = self._queue = queue
            screen.set_cover(None, BRAND_COLOURS)  # another song's cover never shows on it
            screen.set_view(self._opening_view(listed=len(queue) >= 2))
        else:
            queue.pick(index)  # (its queue chose it: it is that row already)
        screen.set_tracks(queue.tracks, index)
        if self._sound_shown:  # the read tags until the player's (as it plays)
            self._show_song(screen, queue.track(index), path, target)
        return True

    def _song_list(self) -> Optional[_LocalList]:
        """The list of songs (play_list) whose queue plays, else None (an album's, or none)."""
        listed, queue = self._list, self._queue
        if listed is None or queue is None or queue is not listed.queue:
            return None
        return listed

    def _single_in_list(self) -> bool:
        """The audio screen's is an entry of a mixed playlist (a lone song, a file with no
        picture): the list's next entry follows it, and it has no shuffle or repeat."""
        current = self._current
        return (
            self._sound_in_list
            and current is not None
            and current is self._sound
            and self._song_list() is None
        )

    def _show_dock(self, title: str) -> None:
        """The transport as the audio screen's dock (its audio mode), its toggles as they are."""
        transport = self.overlays.transport
        show_audio = getattr(transport, "show_audio", None)
        if show_audio is None:
            transport.show_vod(title, "")
            return
        show_audio(title, "")
        self._show_audio_state()

    def _show_audio_state(self) -> None:
        set_state = getattr(self.overlays.transport, "set_audio_state", None)
        if set_state is None:
            return
        queue = self._queue
        if queue is not None:
            set_state(queue.shuffle, queue.repeat, len(queue) >= 2)
        elif self._single_in_list():  # its list's order: no shuffle, no repeat of its own
            set_state(False, "off", False, has_next=self._follows(), has_repeat=False)
        else:  # ⏭ still acts when its folder's next episode or its playlist's entry follows
            set_state(
                self._shuffle_setting(),
                self._repeat_setting(),
                False,
                has_next=self._follows(),
                has_repeat=self._sound_kind == "song",  # a file with no picture: no repeat
            )

    def _follows(self) -> bool:
        """What plays on the audio screen has a next of its own, with no album: an audio-only
        episode's next one in its folder, a playlist's next entry (Autoplay's)."""
        current = self._current
        return current is not None and current is self._sound and self._autoplay.has_next(current)

    def _keys_follow_dock(self) -> bool:
        """The audio dock shows with its ⏭ lit by what follows (_follows): the media keys do
        what its ⏮ and ⏭ do (a film still opening, its dock not up yet, seeks as a film)."""
        return self._audio_dock_shows() and self._follows()

    def _audio_dock_shows(self) -> bool:
        """The audio dock shows for what plays (a song, a file with no picture): the media
        keys' ⏮ is its ⏮ (item 6); ⏭ is its own only while it is lit."""
        current = self._current
        return (
            bool(getattr(self.overlays.transport, "audio", lambda: False)())
            and current is not None
            and current is self._sound
            and self._sound_kind in ("song", "file")
        )

    def _shuffle_setting(self) -> bool:
        return getattr(self.config, "audio_shuffle", False) is True

    def _repeat_setting(self) -> str:
        repeat = getattr(self.config, "audio_repeat", "off")
        return repeat if repeat in REPEAT_MODES else "off"

    def _picked_view(self) -> str:
        view = getattr(self.config, "audio_view", "") or ""
        return view if view in ("cover", "album", "stage") else ""

    def _opening_view(self, *, listed: bool = False) -> str:
        """The view a song or a channel opens on: the one picked by hand (remembered), else
        Stage while Words turned on from the screen stay on, else Album with an album's list,
        else Cover. Captions on from elsewhere don't change it."""
        picked = self._picked_view()
        if picked:
            return picked
        captions = getattr(self.window_actions, "captions", None)
        if self._stage_for_words and captions is not None and self._words_on(captions):
            return "stage"
        return "album" if listed else "cover"

    def _sound_gives_way(self, target: Optional[Target]) -> None:
        """`target` replaces what plays: the audio screen and its dock go, but a song after a
        song (or after a file shown on it) keeps them, its album too while it is on it (the
        next song's tags take the old ones' place as it plays)."""
        if self._is_song(target) and self._sound_kind in ("song", "file"):
            self._sound_poll.stop()
            visualizer = self._visualizer()
            if visualizer is not None:
                visualizer.rest()  # (its bars rest until the next one's sound is read)
            return
        self._end_sound()

    def _end_sound(self) -> None:
        """The audio screen goes with its dock and its album (a stop, an error, a film or a
        channel instead): its album's read stops, and a late album or cover reaches nothing."""
        self._sound_poll.stop()
        self._sound, self._sound_kind, self._sound_played = None, "", None
        self._sound_path = ""
        self._song_facts = None
        self._silent_since = None
        self._queue = None
        self._album_generation += 1
        self._album_job.stop()  # a slow share's listing is read no longer
        self._album_for = None
        self._cover_generation += 1
        self._cover_for = None
        self._visualize("")
        self._hide_sound()
        transport = self.overlays.transport
        if getattr(transport, "audio", lambda: False)():
            transport.end_audio()

    def _visualizer(self) -> Any:
        """The Stage's bars (ui.visualizer; None: none)."""
        return getattr(self.window_actions, "visualizer", None)

    def _visualize(self, path: str) -> None:
        """The Stage's bars follow `path`, the local file shown on the audio screen ("": none,
        as for a radio channel; and a local HLS playlist's stream, which a helper would open a
        second time)."""
        visualizer = self._visualizer()
        if visualizer is None:
            return
        if Path(path).suffix.lower() in PLAYLIST_EXTENSIONS:
            path = ""
        visualizer.follow(path, str(self.session.url or "") if path else "")

    def _hide_sound(self) -> None:
        self._sound_shown = False
        screen = self._screen()
        if screen is not None and not screen.isHidden():
            screen.hide()

    def _video_track(self) -> Optional[bool]:
        has = getattr(self.session, "has_video_track", None)
        return has() if callable(has) else None

    def _tags(self) -> dict:
        tags = getattr(self.session, "tags", None)
        found = tags() if callable(tags) else None
        return found if isinstance(found, dict) else {}

    def _sound_state(self, state: str) -> None:
        """The large picture's player changed state (_on_state): a song is known at its first
        `playing` (its tags are read then), a file once its tracks are; a channel is watched
        for its picture while it plays (only playing time counts)."""
        screen = self._screen()
        if screen is None or self._closed:
            return
        if state not in ("opening", "buffering"):  # (between two tracks: nothing flickers)
            screen.set_playing(state == "playing")  # ended, failed or stopped: it rests
        target = self._current
        if target is None:
            return
        if target.live:
            self._channel_state(target, state)
        elif state == "playing" and target is self._sound and target is not self._sound_played:
            if self.session.url == target.item.get("cmd"):  # not what played before it
                self._file_playing(target)

    def _channel_state(self, target: Target, state: str) -> None:
        """A live channel's state: watched for its picture while it plays (_sound_watch). Its
        stream ended or failed: the watch stops (a reconnect's `playing` starts it again)."""
        if state != "playing":
            self._silent_since = None  # only playing time counts
            if state in ("ended", "error", "idle"):
                self._sound_poll.stop()
            return
        if self._sound is not target or self._sound_kind != "radio":
            self._sound, self._sound_kind, self._sound_played = target, "radio", None
            self._sound_in_list, self._silent_since = False, None
        if not self._sound_poll.isActive():
            self._sound_poll.start()
        self._sound_watch()

    def _sound_after_swap(self) -> None:
        """Another player is large now (a swap, a bring-back): a channel already playing there
        is watched for its picture from now, not from its next state change."""
        if self._state == "playing":
            self._sound_state("playing")

    def _file_playing(self, target: Target) -> None:
        """A local file's first `playing`: a song shows its screen (unless its media has a
        picture: an .m4a's video); another file shows it once its tracks say no picture."""
        self._sound_played = target
        has = self._video_track()
        if self._sound_kind == "song":
            if has is True:
                self._sound_is_film(target)
            else:
                self._show_sound(target)
        elif self._sound_kind == "file":
            if has is False:
                self._show_sound(target)
            elif has is None:  # its tracks aren't listed yet: looked at again (_sound_watch)
                self._sound_since = self._sound_clock()
                self._sound_poll.start()
            else:
                self._sound, self._sound_kind = None, ""  # a film

    def _sound_watch(self) -> None:
        """The poll (SOUND_POLL_MS): a channel playing with no picture listed for NO_PICTURE_S
        is radio (its screen shows); a picture listed hides it at once and the watch rests
        until the channel's next `playing` (libVLC 3's track list only grows within an open:
        a picture that goes away stays listed); a file is shown once its tracks say no
        picture. Not listed (None): nothing changes. What it watched gone without a new
        target (a recording's guard, the grid): the screen goes with it."""
        target = self._current
        if self._closed:
            self._sound_poll.stop()
            return
        if target is None or target is not self._sound:
            self._end_sound()
            return
        if self._state != "playing":
            if target.live:
                self._silent_since = None
            return
        if self._sound_kind == "radio":
            self._watch_channel(target, self._video_track())
        elif self._sound_kind == "file" and not self._sound_shown:
            self._watch_file(target, self._video_track())
        else:
            self._sound_poll.stop()

    def _picture_listed(self) -> None:
        """A live channel shows a picture (listed, or pictureShown): its screen goes, and its
        watch rests until its next `playing` (_channel_state)."""
        self._silent_since = None
        self._sound_poll.stop()
        self._sound_kind = "picture"
        self._hide_sound()

    def _watch_channel(self, target: Target, has: Optional[bool]) -> None:
        if has is True:
            self._picture_listed()
        elif self.multiview_shown():
            self._silent_since = None
            self._hide_sound()  # the grid's tiles are the pictures
        elif has is False:
            now = self._sound_clock()
            if self._silent_since is None:
                self._silent_since = now
            if self._sound_shown:
                self._refresh_radio(target)  # a stream's song, as it changes
            elif now - self._silent_since >= NO_PICTURE_S:
                self._show_sound(target)

    def _watch_file(self, target: Target, has: Optional[bool]) -> None:
        if has is False:
            self._sound_poll.stop()
            self._show_sound(target)
        elif has is True or self._sound_clock() - self._sound_since > FILE_TRACKS_WAIT_S:
            self._sound_poll.stop()
            if has is True:
                self._sound, self._sound_kind = None, ""  # a film

    def _show_sound(self, target: Target) -> None:
        """The audio screen shows `target`: a song or a file with its tags (the player's, else
        its album listing's, else its file's name) and its cover (read on a thread); a radio
        channel with its name, its song or programme, and its logo for a cover. QiTV's mark
        opens out over it."""
        screen = self._screen()
        if screen is None:
            return
        screen.set_live(self._sound_kind == "radio")  # its ‹: the channel list, or the Library
        if self._sound_kind == "radio":
            screen.set_tracks([], -1)
            screen.set_view(self._opening_view())
            self._radio_texts = ("", "")
            self._refresh_radio(target)
            self._radio_cover(target)
            self._visualize("")  # no bars for a live channel
        else:
            path = self._sound_file(target)
            self._visualize(path)
            tags = self._tags()
            queue = self._queue
            index = queue.index_of(path) if queue is not None else None
            listed = queue.track(index) if queue is not None and index is not None else {}
            self._show_song(
                screen, {**listed, **{k: v for k, v in tags.items() if v}}, path, target
            )
            if index is None:  # no album's cover with it: QiTV's drawn one until its own
                screen.set_cover(None, BRAND_COLOURS)
            if self._sound_kind == "file":
                screen.set_tracks([], -1)
                screen.set_view(self._opening_view())
                self._show_dock(target.title)
            self._cover_generation += 1
            self._cover_for = target
            self._cover_job.start(self._cover_generation, path, str(tags.get("art_url") or ""))
        self.words_changed()
        if not self._sound_shown or screen.isHidden():
            self._sound_shown = True
            screen.show()
        screen.set_playing(self._state == "playing")
        if target.local:
            self._opening_over()  # QiTV's mark goes (a file's waited for a picture)

    def _show_song(self, screen: Any, tags: dict, path: str, target: Target) -> None:
        """`target`'s song on the screen: its title (else its file's name), artist, album and
        year (a list of songs' song: the list's name for its album, and no year, the song's
        album's); the system's media panel has them too (panel_facts)."""
        title = str(tags.get("title") or "") or Path(path).stem
        artist, album = str(tags.get("artist") or ""), str(tags.get("album") or "")
        year = str(tags.get("year") or "")
        listed = self._song_list()
        if listed is not None:
            album, year = listed.name, ""
        self._song_facts = (target, title, artist, album)
        screen.show_song(title, artist, album, year=year)

    def _refresh_radio(self, target: Target) -> None:
        """A radio channel's name, then the song its stream names (else the programme on):
        VLC's, or through the time-shift buffer a station's as the helper read it where it
        plays (2.14: VLC never sees the station's own metadata there)."""
        screen = self._screen()
        if screen is None:
            return
        playing = str(self._tags().get("now_playing") or "")
        live = self.live
        if not playing and live is not None and self._live_controls.pipeline_live():
            playing = live.stream_title(free_radio.codepage(target.item))
        if not playing:
            on_now = self.now_title(target.item)
            playing = on_now[0] if on_now else ""
        texts = (target.title, playing)
        if texts != self._radio_texts:
            self._radio_texts = texts
            screen.show_song(target.title, playing, "")

    def _radio_cover(self, target: Target) -> None:
        """A radio channel's logo is its cover (none yet: QiTV's drawn one, and the logo once
        it comes, _on_logo_ready); its colours read from a small copy, as a song's."""
        screen = self._screen()
        if screen is None:
            return
        logo = self._logo(target.item)
        image = logo.toImage() if logo is not None and not logo.isNull() else None
        screen.set_cover(image, cover_colours(image))

    def _sound_is_film(self, target: Target) -> None:
        """What opened as sound shows a picture after all: a film's controls, no screen (a list
        of songs' entry: the list goes on after it as after any video, _list_follows)."""
        self._end_sound()
        listed = self._list
        if listed is not None and listed.songs and listed.target is target:
            self._list_follows(listed, target)
        transport = self.overlays.transport
        transport.set_next_available(self._autoplay.has_next(target))
        transport.show_vod(target.title, target.subtitle)

    def _on_picture_shown(self) -> None:
        """The large picture's first picture of this open: the audio screen goes at once (a
        live channel's watch rests until its next `playing`: _picture_listed)."""
        target = self._current
        if self._closed or target is None or target is not self._sound:
            return
        if target.live:
            self._picture_listed()
        elif self._sound_shown or getattr(self.overlays.transport, "audio", lambda: False)():
            self._sound_is_film(target)  # the screen or the dock was up: the film's controls
        else:  # a film whose tracks weren't listed yet: its controls stay as they are
            self._sound_poll.stop()
            self._sound, self._sound_kind = None, ""

    def _on_album_read(self, generation: int, tracks: list, whole: bool) -> None:
        """The song's album, listed on its thread (AlbumJob.read): its list on the screen (the
        Album view, unless a view was picked) and the queue the song's end follows. A listing
        that comes after another title was chosen changes nothing; one cut short (its time,
        its cap) can't say what follows its last track: no album (review item 12)."""
        listed_for = self._album_for
        if self._closed or generation != self._album_generation or listed_for is None:
            return
        target, path = listed_for
        self._album_for = None
        if target is not self._current or target is not self._sound:
            return
        if not whole:
            logger.info("A song's folder was read only in part: it plays as a lone song")
            return
        index = next((i for i, t in enumerate(tracks) if t.get("path") == path), None)
        if index is None or len(tracks) < 2:
            return  # a lone song: no list, nothing after it
        self._queue = AudioQueue(
            tracks, index, shuffle=self._shuffle_setting(), repeat=self._repeat_setting()
        )
        screen = self._screen()
        if screen is not None:
            screen.set_tracks(tracks, index)
            screen.set_view(self._opening_view(listed=True))
        self._show_audio_state()

    def _on_list_read(self, generation: int, tracks: list, whole: bool) -> None:
        """A list of songs' tags, read by name on its thread (AlbumJob.start_files): its rows
        take them, in the list's order (one not read keeps its file's name, and plays all the
        same). Only the newest list's answer counts, whatever plays now."""
        listed = self._list
        if self._closed or generation != self._list_generation or listed is None:
            return
        if not whole:
            logger.info(
                "A playlist's songs were read only in part (%d of %d): the rest show their names",
                len(tracks),
                len(listed.tracks),
            )
        for row, read in zip(listed.tracks, tracks):
            if read.get("parsed") and read.get("path") == row.get("path"):
                row.update(read)  # (its queue's row too: the same Track)
        queue, screen = self._queue, self._screen()
        if self._song_list() is listed and queue is not None and screen is not None:
            screen.update_tracks(queue.tracks)  # a list scrolled by hand stays where it is

    def _on_cover_ready(self, generation: int, image: Any, colours: list) -> None:
        """The song's cover, read on its thread (CoverJob): only for the song still playing."""
        if self._closed or generation != self._cover_generation or self._cover_for is None:
            return
        screen = self._screen()
        if screen is not None and self._cover_for is self._current:
            screen.set_cover(image, colours)

    def _album_plays(self) -> bool:
        """An album's song plays: the tracks' keys and buttons go to its queue."""
        queue, sound = self._queue, self._sound
        return (
            not self._closed
            and queue is not None
            and len(queue) >= 2
            and sound is not None
            and sound is self._current
        )

    def _sound_goes_on(self, current: Target) -> bool:
        """A song ended (_on_ended): its album's next track plays, held as a file opened in
        QiTV, with no card (or itself again with repeat one; a lone song with repeat on); a
        list of songs is its album. False when nothing follows (the last with repeat off says
        "Finished"), and while a newer choice is on its way: it plays when its link comes, as
        Autoplay never offers over one. A file with no picture has no repeat (item 5): its
        folder's next episode follows it as a film's does."""
        if current is not self._sound or self._sound_kind not in ("song", "file"):
            return False
        if self._lanes.busy():
            return False
        if self._single_in_list():
            return self._entry_goes_on(current)
        queue = self._queue
        if queue is not None:
            before = queue.current
            index = queue.next(auto=True)
            if index is None:
                return False
            self._play_track(index, start_over=index == before)
            return True
        if self._sound_shown and self._sound_kind == "song" and self._repeat_setting() != "off":
            self._replay(current)
            return True
        return False

    def _entry_goes_on(self, current: Target) -> bool:
        """A mixed list's song ended: the list's next entry plays at once, held, with no card,
        whatever "Auto-play next episode" says (as an album's next track). False after a file
        with no picture (the list's next follows it as a video's: Task 1's card) and after the
        last entry."""
        listed = self._list
        if self._sound_kind != "song" or listed is None or listed.target is not current:
            return False
        if listed.index + 1 >= len(listed.entries):
            return False
        self._flow.hold_for_file(True)
        self._play_entry(listed.index + 1)
        return True

    def _play_track(self, index: int, *, start_over: bool) -> None:
        """The album's track at `index` plays (play_file), held until it ends, as the next
        file opened in QiTV is (_play_following); its list and the screen stay. A list of
        songs' plays as the list's entry (its own size, its row: _play_entry)."""
        queue = self._queue
        if queue is None:
            return
        self._flow.hold_for_file(True)
        if self._song_list() is not None:
            self._play_entry(index, start_over=start_over)
            return
        track = queue.track(index)
        size = track.get("size")
        self.play_file(
            str(track.get("path") or ""),
            size if isinstance(size, int) else None,
            start_over=start_over,
        )

    def _replay(self, target: Target) -> None:
        """`target`'s file again from its start (a playlist's entry: as the list's, which goes
        on after it; never its folder's album)."""
        self._flow.hold_for_file(True)
        listed = self._list
        if listed is not None and listed.target is target:
            self._play_entry(listed.index, start_over=True)
            return
        self.play_file(self._sound_file(target), start_over=True)

    def _restart_sound(self, target: Target) -> None:
        """The song from its start: a seek, or played again once it can't seek (it ended)."""
        if self.session.is_seekable():
            self.session.seek(0)
            self._show_transport()
        else:
            self._replay(target)

    def _previous_track(self) -> None:
        """⏮ (the dock's; the media key's with an album): past 3 s into the song, the song
        from its start; else the album's track before (the first: from its start)."""
        target = self._current
        if self._closed or target is None or target is not self._sound or target.live:
            return
        queue = self._queue
        if not self._album_plays() or queue is None:
            self._restart_sound(target)
            return
        before = queue.current
        position = getattr(self.session, "time_ms", lambda: 0)()
        index = queue.previous(int(position or 0))
        if index is None or index == before:
            self._restart_sound(target)
        else:
            self._play_track(index, start_over=False)

    def _next_track(self) -> None:
        """⏭ (the dock's; the media key's with an album): the album's next track (none after
        the last with repeat off); with no album, the next episode or playlist entry, as the
        film's Next."""
        queue = self._queue
        if not self._album_plays() or queue is None:
            if queue is None and self._follows():
                self._on_next_requested()
            return
        before = queue.current
        index = queue.next(auto=False)
        if index is not None:
            self._play_track(index, start_over=index == before)

    def _on_track_picked(self, index: int) -> None:
        """A row of the album's list: that track plays (the one playing: from its start)."""
        queue, target = self._queue, self._current
        if not self._album_plays() or queue is None or target is None:
            return
        if not 0 <= index < len(queue):
            return
        if index == queue.current:
            self._restart_sound(target)
            return
        queue.pick(index)
        self._play_track(index, start_over=False)

    def _remember(self, name: str, value: Any) -> None:
        """An audio screen setting (optional config fields: older builds ignore them)."""
        setattr(self.config, name, value)
        try:
            self.config.save_config()
        except Exception as e:
            self._log_failure(f"save {name}", e)

    def _toggle_shuffle(self) -> None:
        if self._single_in_list():
            return  # a mixed list's entry plays in its list's order
        queue = self._queue
        on = queue.toggle_shuffle() if queue is not None else not self._shuffle_setting()
        self._remember("audio_shuffle", on)
        self._show_audio_state()

    def _cycle_repeat(self) -> None:
        if self._single_in_list() or self._sound_kind == "file":
            return  # a mixed list's entry, a file with no picture: never itself again
        queue = self._queue
        if queue is not None:
            repeat = queue.cycle_repeat()
        else:
            at = REPEAT_MODES.index(self._repeat_setting())
            repeat = REPEAT_MODES[(at + 1) % len(REPEAT_MODES)]
        self._remember("audio_repeat", repeat)
        self._show_audio_state()

    def _on_view_picked(self, view: str) -> None:
        """A view picked on the screen's switch (it shows it already): remembered."""
        if view in ("cover", "album", "stage"):
            self._remember("audio_view", view)

    @staticmethod
    def _words_shown(captions: Any) -> bool:
        """AI captions are on, downloading, or their question is asked."""
        return any(
            bool(getattr(captions, name, lambda: False)())
            for name in ("enabled", "downloading", "asking")
        )

    @classmethod
    def _words_on(cls, captions: Any) -> bool:
        """AI captions are on, or on their way: downloading, their question asked, or waiting
        for this computer's speed (CaptionsController.wanted)."""
        wanted = getattr(captions, "wanted", None)
        return bool(wanted()) if callable(wanted) else cls._words_shown(captions)

    def _on_words_requested(self) -> None:
        """The screen's Words pill: AI captions on or off, as the Subtitles panel's row turns
        them (the subtitle track shown goes; where they can't run, it says so, as the row's
        state does); turned on from here, the Stage shows them, unless a view was picked."""
        captions = getattr(self.window_actions, "captions", None)
        if self._closed or captions is None:
            return
        if not captions.available():  # this computer runs no speech model, or the stream
            self._toast(translate(NOT_AVAILABLE))
            return
        was_on = self._words_on(captions)
        if was_on and not self._words_shown(captions):
            captions.toggle()  # pressed again while the speed is measured: not wanted now
        else:
            self._tracks.set_captions(not was_on)
        self._stage_for_words = not was_on
        screen = self._screen()
        if not was_on and screen is not None and not self._picked_view():
            screen.set_view("stage")
        self.words_changed()

    def words_changed(self) -> None:
        """AI captions changed (CaptionsController.changed): the Words pill says so, and the
        language they show in."""
        screen = self._screen()
        captions = getattr(self.window_actions, "captions", None)
        if self._closed or screen is None or captions is None:
            return
        on = bool(getattr(captions, "enabled", lambda: False)())
        screen.set_words(on, self._words_language(captions) if on else "")
        if not self._words_on(captions):
            self._stage_for_words = False

    @staticmethod
    def _words_language(captions: Any) -> str:
        """The language Words show in, in its own name: what they're translated into, else the
        one they're heard in ("" while the model detects it)."""
        target = str(getattr(captions, "translate_to", lambda: "")() or "")
        if target:
            from services.captions.mt_languages import own_name

            return own_name(target)
        language = getattr(captions, "language", lambda: ("", ""))()
        name = str(language[0] if language else "")
        if not name or name == "auto":
            return ""
        from services.captions.languages import own_name as spoken_name

        return spoken_name(name)

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

    def now_next(self, channel: dict) -> tuple[Optional[Programme], Optional[Programme]]:
        """The programme on now on `channel` and the next one (from the loaded guide)."""
        now = self._now()
        return now_next(self._programmes(channel, now, 3), now)

    def channel_number(self, channel: dict) -> str:
        """A channel's number as the banner shows it ("" when it has none)."""
        return self._number(channel)

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
        if index >= 0 and not context.numbered():
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
            if shown is self._sound and self._sound_shown:
                self._radio_cover(shown)  # a radio channel's screen: its logo is its cover
        banner, channel = self.overlays.banner, self._channel
        if channel is None or channel.get("logo") != url:
            return
        if banner.isVisible() and banner.info is not None and banner.info.logo is None:
            banner.show_info(self.banner_info(channel))

    def _choose_small(self) -> None:
        """P (⧉, the phone's) with no previous channel to open small: the channel list opens,
        and the channel picked there opens small (Enter, a click, the find's); Esc closes it
        and nothing opens."""
        if self._closed:
            return
        self._open_drawer()
        if self.overlays.drawer.isVisible():
            self._choosing_small = True
            self._toast(translate(OPEN_PIP_TEXT), "info")  # what the pick is for

    def _end_choosing_small(self) -> None:
        self._choosing_small = False

    def _open_drawer(self, from_column: bool = False) -> None:
        self._choosing_small = False  # (the small picture's choice opens it, then says so)
        if not self.catalog.provider_names():
            self._flow.welcome(chosen=True)  # asked for: over a film opened in QiTV too
            return
        # On the channel the big picture plays (its ▶: the one tuned last, playing or on its
        # way; after a swap, the new large one's), where it was picked: its target's group and
        # list once it plays, else the zap's (set as it was tuned). Its row is the one its ▶
        # marks (its address first: an M3U id is a list position, another channel's once the
        # list changed). While the fast-zap column is open the user is choosing in it, so the
        # list opens at the zap's own channel and group, as before 2.1.
        shown = self._current if self._current is not None and self._current.live else None
        playing = self._channel if shown is not None else None  # (a film's: the list as it is)
        if from_column or self.overlays.column.isVisible():
            playing = None
        context = self.zap.context
        if playing is not None and shown is not None:
            if ZapContext.same(shown.item, playing):
                source, group = shown.source, shown.group_key or context.group_key
            else:  # still on its way: picked from the zap's group
                source, group = context.source, context.group_key
            self.nav.reveal(source, "live", group, playing)
        elif "live" not in self.catalog.sections():  # 2.8: a media server: its sections
            self.nav.focus_source(self.catalog.current_provider_name())
        else:
            self.nav.reset_to("live", context.group_key or ALL_GROUP_ID)
        channel = self._channel or context.current()
        focus = None
        if channel:
            provider = provider_of(channel)
            focus = (provider, item_key(channel)) if provider else item_key(channel)
        self.overlays.drawer.open_drawer(focus_key=focus, focus_item=channel or None)
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

    def _archive_beside_recording(
        self, target: Target, moment: float, until: Optional[float]
    ) -> None:
        """A rewind past the buffer (←, ⏮, the bar's dashed part) into the archive of the
        channel the large picture records: it opens as the guide's "Watch from the start"
        does, so the recording goes on in the background, or QiTV asks first when no stream
        is free (never stopped unasked)."""
        if self._closed:
            return
        context = self.zap.context
        if context.group_key == target.group_key and context.index_of(target.item) >= 0:
            key, channels = context.group_key, context.channels
        else:
            key, channels = ALL_GROUP_ID, self._all_live()
        self._tune(target.item, key, channels, from_zap=False, archive=(moment, until))

    def _on_guide_catchup(
        self, channel: dict, programme: Programme, group_key: str, channels: list[dict]
    ) -> None:
        """The guide's "Watch from the start": the channel's archive from the programme's start
        to its end (catch-up, 2.2); live when the archive does not play. Within the channel's
        own server window (2.7), the live session plays from there instead."""
        if self._closed:
            return
        start = programme.start.timestamp()
        reach = self._live_controls.window_reach(channel)
        if reach > 0.0 and self._now().timestamp() - start <= reach:
            self._tune(channel, group_key, channels, from_zap=False)  # live, where it plays
            self._live_controls.watch_from(channel, start)
            return
        span = (start, programme.stop.timestamp())
        self._tune(channel, group_key, channels, from_zap=False, archive=span)

    def archive_reach(self, channel: dict) -> timedelta:
        """How far back the guide offers the channel's past programmes: its provider archive's
        days, or (2.7) its own server window as seen this run, the longer."""
        days = timedelta(days=self.archive_days(channel))
        window = timedelta(seconds=self._live_controls.window_reach(channel))
        return max(days, window)

    def deepest_window(self, channels: Sequence[dict], beyond: timedelta) -> timedelta:
        """(2.7) The longest own server window seen this run of any of `channels`, when it
        reaches further back than `beyond` (a list's deepest archive), else none: what
        archive_reach adds to the guide's reach of a whole list, in one go."""
        seconds = self._live_controls.deepest_window(channels, beyond.total_seconds())
        return timedelta(seconds=seconds)

    def archive_days(self, channel: dict) -> int:
        """The days of the channel's provider archive that may play now (the guide's reach):
        0 without one, when it did not answer lately, or while archives can't play."""
        archive = archive_of(channel)
        if archive is None or self._closed or not self._archive_allowed():
            return 0
        if self._live_controls.catchup.known_failed(Target(channel, "live", "")):
            return 0
        return archive.days

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
        elif self._live_controls.dismiss_back():
            pass  # 2.13.3: the Resume pill closes (Esc, the phone's Back), its offer gone
        elif overlays.banner.isVisible():
            overlays.banner.dismiss()
        elif self._grid is not None and self._grid.big():
            self._grid.back_to_grid()  # a big tile: the grid again
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
            self._toast(translate(self.live.no_rewind_reason))
        else:
            # i18n: a toast when a channel is rewound with no time-shift buffer to rewind in
            self._toast(tr("Rewind arrives with time-shift"))

    def _show_transport(self) -> None:
        target = self._current
        if self.overlays.drawer.isVisible() or self._settings_shown():
            return
        if (target is None or not self._has_media()) and self.multiview_shown():
            self._show_grid_transport()  # the grid's button and layout chip stay in reach
            return
        if target is None or not self._has_media():
            failed = self._failed_live()
            if failed is not None and not self._digits:
                self._show_failed_transport(failed)
            return
        if self._digits:
            return  # the banner shows the number being typed
        self.overlays.banner.dismiss()
        transport = self.overlays.transport
        if target.live and (not transport.isVisible() or transport.failed()):
            self._live_controls.show_live_transport(target)
        else:
            transport.poke()

    def _failed_live(self) -> Optional[Target]:
        """The live channel that failed to open, or whose stream ended for good (2.8.12): it is
        still current, but nothing plays or opens, Stop did not end it and no other choice is
        on its way."""
        target = self._current
        if target is None or not target.live or self._has_media():
            return None
        if self._stopped is not None or self._lanes.busy():
            return None
        return target if self._state in ("error", "ended") else None

    def _show_failed_transport(self, target: Target) -> None:
        """Live TV's controls over a channel that failed to open (2.8.12): a mouse was left
        with nothing to click but the picture (the channel list). ▶ tries it again, and the
        guide, the previous channel and Search are in reach."""
        self.overlays.banner.dismiss()
        transport = self.overlays.transport
        if transport.isVisible() and transport.failed():
            transport.poke()
            return
        now = self.now_title(target.item)
        transport.set_favorite(favorites.is_favorite(self.config, target.item))
        transport.show_failed(target.title, now[0] if now else "", self._logo(target.item))

    def _retry_failed(self, target: Target) -> None:
        """▶ or Space over a channel that failed to open: it is asked for again, from its
        group (as Play again after Stop)."""
        self.overlays.transport.dismiss()
        key = target.group_key or ALL_GROUP_ID
        channels = list(self.catalog.items("live", key) or []) or self._all_live()
        self.tune(target.item, group_key=key, channels=channels)

    def _show_grid_transport(self) -> None:
        """The player controls over a grid whose focused tile plays nothing: live TV's, named
        for the multiview, so its button and layout chip stay in reach."""
        if self._digits:
            return  # the banner shows the number being typed
        self.overlays.banner.dismiss()
        self.overlays.transport.show_live(translate(MULTIVIEW_TITLE), "")

    def _change_volume(self, delta: int) -> None:
        self._mixer.change(delta)

    def _picture_names(self) -> tuple[str, str]:
        """The large and the small picture's channel (or movie) names, for the mixer."""
        current, pip, sessions = self._current, self._pip, self.sessions
        large = current.title if current is not None else ""
        small = sessions.small() if sessions is not None else None
        target = pip.view(small.name).target if pip is not None and small is not None else None
        return large, target.title if target is not None else ""

    def _open_mixer(self) -> None:
        """The transport's speaker: the mixer, the controls held up (the speaker lit) until it
        closes; a click while it shows closes it."""
        if self._mixer.visible():
            self._mixer.hide()  # closed → _on_mixer_closed lets the controls fade again
            return
        self._mixer.open_main()
        if self._mixer.visible():
            self.overlays.transport.hold("volume")

    def _on_mixer_closed(self) -> None:
        transport = self.overlays.transport
        if transport.held() == "volume":
            transport.hold("")

    def _on_audio_changed(self) -> None:
        """A level or mute changed, or the pictures swapped: the PiP label's speaker and the
        transport's follow (crossed out, or a wave per third of the volume)."""
        self.overlays.transport.set_muted(self._mixer.main_muted())
        self.overlays.transport.set_level(self._mixer.main_level())
        if self._pip is not None:
            self._pip.refresh_label()
        if self._grid is not None:
            self._grid.refresh_tiles()  # a tile's speaker shows its mute

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
            self._toast(translate(FAVORITES_SAVE_FAILED_TEXT), "error", ERROR_MS)
            return
        self._toast(tr("Added to favorites") if added else tr("Removed from favorites"))

    def _forget_highlighted(self) -> None:
        """Delete in the channel list: the highlighted channel leaves ⟲ Recent (nothing on any
        other row, nor while the find lists what it found: Delete there is the find's)."""
        drawer = self.overlays.drawer
        if not drawer.isVisible() or drawer.finding():
            return
        node = drawer.highlighted()
        if node is not None and self.nav.is_recent_row(node):
            self._forget_recent(node.payload)

    def parental_changed(self) -> None:
        """Parental controls changed (2.8): the lists read their catalog again, what they
        protect leaves the history, and while locked, a hidden channel stops and zapping
        leaves the hidden ones out."""
        if self._closed:
            return
        guard = self.parental
        if guard is not None:
            self._forget_protected(guard)
            if guard.active():
                self._lock_out(guard)
            self._forget_protected_tiles(guard)  # after it: a grid it left was saved
        links_changed = getattr(self.nav, "links_changed", None)
        if links_changed is not None:
            links_changed()  # Network streams leaves out a hidden link (emptied: it goes)
        self._repaint()
        self._guide.invalidate()

    def _forget_protected(self, guard: Any) -> None:
        """What parental controls protect leaves the history (Recent, Continue) and is no
        longer the channel QiTV starts on, also what was watched before it was hidden."""
        sections = {"channel": "live", "movie": "movies", "episode": "series"}

        def protected(entry: dict) -> bool:
            item = entry.get("item_data")
            if not isinstance(item, dict):
                return False
            section = sections.get(str(entry.get("item_type") or ""), "live")
            stamped = with_provider(item, str(entry.get("provider_name") or ""))
            return bool(guard.protects(*self._guard_view(section, stamped)))

        forget = getattr(self.watch, "forget", None)
        if forget is not None and forget(protected):
            self._recents_changed()
        for name in ("last_live_channel", "last_watched"):
            record = getattr(self.config, name, None)
            if not isinstance(record, dict):
                continue
            item = record.get("item") if name == "last_live_channel" else record.get("item_data")
            entry = {"item_data": item, "provider_name": record.get("provider_name")}
            entry["item_type"] = (
                "channel" if name == "last_live_channel" else record.get("item_type")
            )
            if protected(entry):
                setattr(self.config, name, None)

    def _lock_out(self, guard: Any) -> None:
        """Locked: zapping skips the hidden channels; a hidden one that plays stops (the small
        picture closes, a multiview tile closes)."""
        context = self.zap.context
        channels = guard.visible("live", context.channels)
        if context.group_key == RECENT_GROUP:  # ⟲ Recent's links (a few rows), as known
            channels = [channel for channel in channels if not self.hides_link(channel)]
        fallback = guard.visible("live", context.fallback)
        if len(channels) != len(context.channels) or len(fallback) != len(context.fallback):
            self.zap.set_context(
                ZapContext(context.group_key, channels, fallback=fallback, source=context.source)
            )
        small = self._beside_target()
        if small is not None and self._protects_play(guard, small):
            self._close_pip()
        self._close_hidden_tiles()
        current = self._current or self._lanes.pending()
        if current is not None and self._protects_play(guard, current):
            self.stop()

    def _hidden_now(self, channel: dict) -> bool:
        """2.12: parental controls hide the live `channel` now (locked), stamped with its
        provider as the lists stamp it."""
        guard = self.parental
        if guard is None:
            return False
        provider = item_provider.provider_name(self.catalog, channel)
        return bool(guard.hides_item(*self._guard_view("live", with_provider(channel, provider))))

    def _close_hidden_tiles(self) -> None:
        """Locked (2.12): a multiview tile whose channel is hidden closes, as its ✕ does: one
        beside the focus, and the focused one by its card or what it waits for (what it plays
        is the lock-out's own target); while one of them records, the grid is left as Stop
        leaves it instead (that recording goes on in the background, the other tiles beside
        the focus stop)."""
        grid, sessions = self._grid, self.sessions
        if grid is None or sessions is None or not grid.shown():
            return
        names = grid.tiles_with(self._hidden_now)
        if any(slot.name in names and records(slot) for slot in sessions.slots()):
            self.leave_multiview(keep_pip=False)
            return
        for name in names:
            grid.close_tile(name)

    def _forget_protected_tiles(self, guard: Any) -> None:
        """2.12: what parental controls protect leaves the last multiview (config
        `multiview`) too, as it leaves the history: "Last grid" never brings it back."""
        saved = getattr(self.config, "multiview", None)
        tiles = saved.get("tiles") if isinstance(saved, dict) else None
        if not isinstance(saved, dict) or not isinstance(tiles, list):
            return

        def protected(record: Any) -> bool:
            item = record.get("item") if isinstance(record, dict) else None
            if not isinstance(item, dict):
                return False
            provider = str(record.get("provider_name") or "")
            return bool(guard.protects(*self._guard_view("live", with_provider(item, provider))))

        if not any(protected(record) for record in tiles):
            return
        kept = [None if protected(record) else record for record in tiles]
        self.config.multiview = {**saved, "tiles": kept}
        try:
            self.config.save_config()
        except Exception as e:
            self._log_failure("save the multiview", e)

    def _hide_from_menu(self, node: NavNode) -> None:
        """A channel's or a group's "Hide (parental controls)" (2.8): hidden while locked; the
        PIN comes first (Settings)."""
        guard = self.parental
        if guard is None or not guard.has_pin():
            self._toast(translate(SET_PIN_FIRST), "error", ERROR_MS)
            return
        section = self.nav.section_of(node) or "live"
        if node.layer == "group":
            name, own = split_group_id(node.key)
            provider = name or self.catalog.current_provider_name()
            self.hide_group(section, provider, own or node.key, node.title)
            return
        if guard.hide_item(*self._guard_view(section, node.payload)):  # a link: by its id
            self._toast(translate(HIDDEN_TEXT))
            self.parental_changed()  # out of the history; locked, it stops if it plays

    def _tidy_from_menu(self, node: NavNode) -> None:
        """A group's "Hide group" (2.9.1): out of the lists, without a PIN."""
        section = self.nav.section_of(node) or "live"
        name, own = split_group_id(node.key)
        provider = name or self.catalog.current_provider_name()
        self.tidy_group(section, provider, own or node.key, node.title)

    def tidy_group(self, section: str, provider: str, group_id: str, title: str) -> None:
        """A group or a Library genre leaves the lists (2.9.1, services.hidden_groups):
        nothing else changes, and Settings → Providers shows it again."""
        tidy = self.hidden_groups
        if tidy is not None and tidy.hide(section, provider, group_id, title=title):
            self._toast(translate(TIDIED_TEXT))
            self.lists_changed()

    def lists_changed(self) -> None:
        """What the lists leave out changed (a group shown again, 2.9.1): they read again."""
        if not self._closed:
            self._repaint()
            self._guide.invalidate()

    def hide_group(self, section: str, provider: str, group_id: str, title: str) -> None:
        """A group's or a Library genre's "Hide (parental controls)" (2.8): hidden while
        locked; the PIN comes first (Settings)."""
        guard = self.parental
        if guard is None or not guard.has_pin():
            self._toast(translate(SET_PIN_FIRST), "error", ERROR_MS)
            return
        if guard.hide_group(section, provider, group_id, title=title):
            self._toast(translate(HIDDEN_TEXT))
            self.parental_changed()  # out of the history; locked, it stops if it plays

    def _forget_recent(self, item: dict) -> None:
        """The channel leaves ⟲ Recent: its plays leave the watch history (saved with it). A
        link's go whoever's stream they were kept as (⟲ Recent lists it once, as whose it is
        now)."""
        wanted = (provider_of(item), item_key(item))
        if self._network_url(item) is None:
            matches = lambda entry: recent_channel(entry) == wanted  # noqa: E731
        else:
            matches = self._same_link(item)
        if self.watch.forget(matches):
            self._recents_changed()
            self._toast(tr("Removed from Recents"))  # i18n: a toast (see "Remove from Recents")

    def _same_link(self, item: dict) -> Callable[[dict], bool]:
        """Whether a watch-history entry is a play of the link `item`, under any stamp."""
        key = item_key(item)

        def matches(entry: dict) -> bool:
            channel = recent_channel(entry)
            if channel is None or channel[1] != key:
                return False
            played = entry.get("item_data")
            stamped = with_provider(played if isinstance(played, dict) else {}, channel[0])
            return self._network_url(stamped) is not None

        return matches

    def _clear_recents(self) -> None:
        """Clear Recents: every channel leaves ⟲ Recent (films and episodes stay in the
        history: the Library's Continue)."""
        if self.watch.forget(lambda entry: recent_channel(entry) is not None):
            self._recents_changed()
            self._toast(tr("Recents cleared"))  # i18n: a toast: the "Recent" list was emptied

    def _recents_changed(self) -> None:
        """⟲ Recent follows; emptied while it shows, the list goes a layer up to the genres
        (its row is gone from them)."""
        path = self.nav.path
        if path and path[-1].key == RECENT_GROUP and not self.nav.recent_count():
            self.nav.up()
        else:
            self.nav.changed.emit()

    def _on_context(self, node: NavNode, global_pos: Where) -> None:
        """A right-click on a drawer row (or the menu key on it, 2.13.1): its context menu. A
        link of Network streams' (its row, or the find's: keyed by its id) is the kept link's,
        as it plays."""
        if self._closed:
            return
        if node.layer == LINK_LAYER:
            kept = self._link_item(node.key)
            if kept is not None:
                self._context.show_link(*kept, global_pos, self.overlays.drawer)
            return
        self._context.show(node, global_pos, self.overlays.drawer)

    def _context_key(self) -> None:
        """The menu key or Shift+F10 (2.13.1): the channel list's highlighted row's menu,
        beside the row, as its right-click opens it."""
        drawer = self.overlays.drawer
        if drawer.isVisible():
            drawer.context_key()

    def _on_guide_context(self, channel: dict, global_pos: Where) -> None:
        """2.9.3: a right-click on a channel of the guide (or the menu key, 2.13.1): its menu,
        as its row's in the channel list."""
        if not self._closed:
            guide = self.overlays.guide
            self._context.show_channel(channel, global_pos, guide, group_key=self._guide.group())

    def show_library_menu(
        self, item: dict, section: str, global_pos: Where, parent: Optional[QObject] = None
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

    def _on_cast_requested(self, anchor: QPoint) -> None:
        """The transport's cast button (2.8): the devices to cast to, and Stop casting."""
        if not self._closed and self._cast is not None:
            self._cast.show_menu(anchor, self.overlays.transport)

    def cast_controls(self) -> Optional[CastControls]:
        """Casting (2.8; None without libVLC's renderers): the phone's Cast to… uses it too."""
        return self._cast

    def track_controls(self) -> Optional[TrackControls]:
        """The large picture's sound and subtitles (None once closed): the phone's Subtitles
        and Sound sheets use them too (2.9)."""
        return None if self._closed else self._tracks

    def _one_picture_for_cast(self) -> None:
        """One picture goes to the device: the multiview's grid leaves (its focused tile is the
        large picture) and the picture in picture closes, as for an external player."""
        self.leave_multiview(keep_pip=False)
        self._close_pip()

    def play_elsewhere(
        self,
        launch: Callable[[Target, str], tuple[bool, str]],
        target: Optional[Target] = None,
        *,
        ask_on: Optional[str] = None,
        recordings: bool = False,
        failed: Optional[Callable[[Target, str], Optional[bool]]] = None,
        again: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """What plays now, or `target`, handed to `launch` with its link (a phone's player) as
        Play in VLC hands it: the in-app stream is released first, so the provider counts one.
        False when nothing from a provider is current (a file opened from the disk stays here).
        2.6: with no stream free while a recording holds one, phone `ask_on` is asked.
        2.8: with `recordings`, a recording of the Library goes too, its file's URI as its
        link (a phone gets it from QiTV's home door).
        2.15: `failed` hears why its link failed (a phone's Watch of a site's channel that is
        not live waits for it: ui/remote_waits.py): True, it begins to wait (nothing is said
        here; its row counts it once), False, it waits on (nothing said nor counted), None, as
        ever. `again`: a waiting phone's ask, asked beside the TV's choices (none is dropped
        for it) and handed on only if `again()` still says so when its link comes."""
        target = target or self._current
        if self._closed or target is None:
            return False
        # 2.14: a phone gets a Plex title through QiTV's home door, which passes its file (the
        # server's conversion is a playlist of pieces on the server): as it is.
        target = self._as_its_file(self._owned_target(target))  # a link: whose stream it is now
        # 2.15: each phone's ask its own (a copy): its `failed` hears its own answer, and a
        # second ask for the same target (another phone's) is asked, not dropped as the same.
        chosen = replace(target) if failed is not None else target
        if target.local:
            path = str(target.item.get("path") or "")
            if not (recordings and target.item.get("_recording") and path):
                return False
            uri = Path(path).absolute().as_uri()
            self._play_external(
                chosen, uri, lambda link, **_http: launch(chosen, link), ask_on=ask_on, phone=True
            )
            return True

        def play(url: str) -> None:
            self._elsewhere_failed = [e for e in self._elsewhere_failed if e[0] is not chosen]
            if again is not None and not again():
                return  # 2.15: the phone waits for it no more (its site's address unused)
            self._play_external(
                chosen, url, lambda link, **_http: launch(chosen, link), ask_on=ask_on, phone=True
            )

        if failed is not None:  # (the newest few: one that never answers is not kept for ever)
            self._elsewhere_failed = [*self._elsewhere_failed[-7:], (chosen, failed)]
        self._lanes.request_side(chosen, play, play=again is None)
        return True

    def _play_external(
        self,
        target: Target,
        url: str,
        launcher: Launcher,
        *,
        ask_on: Optional[str] = None,
        phone: bool = False,
    ) -> None:
        """The context menu's external play: the newest choice, once the in-app stream is gone.
        `phone`: a phone's Watch (its stream is counted: `_launch`)."""
        if self._closed:
            return
        target = self._owned_target(target)  # a link: whose stream it is now

        def again() -> None:
            self._play_external(target, url, launcher, ask_on=ask_on, phone=phone)

        self._count_start()  # an outside player takes it, asked about or not
        if not target.local and self._ask_for_stream(
            target.item, self._tuner.purpose, again, gives_way=True, ask_on=ask_on
        ):
            return  # 2.6: no stream is free while a recording holds one: asked
        self._before_new_target(None)  # an outside player (a phone's too) takes it
        self._forget_stopped()
        self._cancel_external()
        self.leave_multiview(keep_pip=False)  # an external player is outside the budget
        self._close_pip()  # an external player is outside the budget: the PiP gives way
        self._set_recording_aside()  # 2.6: a recording records on in the background
        self._autoplay.drop()
        self._lanes.drop_tune()  # e.g. an STB retry still on its way would open in-app

        def launch() -> None:
            self._launch(target, url, launcher, phone=phone)
            if self._current is target:
                self._restore_channel()  # INFO, the guide and Ctrl+D follow what it plays

        # Nothing plays in the app: no provider slot is held, for a stream or a file alike.
        self._tuner.open(self._guarded(target, launch))

    # ---- state helpers ------------------------------------------------------------------------

    def _archive_allowed(self) -> bool:
        """Provider archives play in the large picture, not in the multiview's tiles nor in
        an external player."""
        return not self._closed and not self.multiview_shown() and not self._external_launcher()

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
        # The helpers' texts: one marked where it was written (a catalog's failure among them)
        # shows in the active language.
        toast.show_message(shown_message(text), kind, ms)

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
            self._error(self._named(title, text))

    def _notify(self, text: str, kind: str) -> None:
        """A toast for the shell's helpers: errors stay up longer."""
        self._toast(text, kind, ERROR_MS if kind == "error" else INFO_MS)

    @staticmethod
    def _named(name: str, message: str) -> str:
        """An error's text after what failed (a channel, a film or a provider), when named.
        `message` may come marked from a service: it shows in the active language."""
        message = shown_message(message)
        if not name:
            return message
        # i18n: an error toast: a channel's, a film's or a provider's name, then what went wrong
        return tr("{name}: {message}", name=name, message=message)

    def _error(self, text: str) -> None:
        """Toast an error; addresses and query strings never reach the toast or any log (the
        toast logs it as it shows: "Error shown: …")."""
        self._toast(scrub(text), "error", ERROR_MS)

    def _menu_notify(self, text: str, kind: str) -> None:
        """A context menu's message: on the Library's toast while the Library shows (the
        player's page, with its toast, is hidden then), else on the player's."""
        self._notify(text, kind)

    def _menu_error(self, text: str) -> None:
        """An external play or a context menu's link failed: _error's text, on _menu_notify's
        toast."""
        self._menu_notify(scrub(text), "error")  # the toast logs it ("Error shown: …")

    @staticmethod
    def _log_failure(what: str, error: Exception) -> None:
        logger.warning("Could not %s: %s (%s)", what, scrub(error), type(error).__name__)
        # The frames only: the exception's text can carry a provider URL with credentials.
        logger.debug("Could not %s:\n%s", what, safe_traceback(error))


def _log_name(item: Optional[dict]) -> str:
    """A channel's name for the log (2.9.7); never its address."""
    return str((item or {}).get("name") or "?")[:80]
