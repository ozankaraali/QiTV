from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMenu, QWidget
from mixed_fixtures import DEMO, HARBOR, HeldSetups, live_content, mixed_config, shut_hub
from test_browsed_catalog import FakeEpg, ScriptedFactory
from timer_fixtures import elapse, wait_until

from services.browsed_catalog import BrowsedCatalog
from services.catalog import PROVIDER_LOADING_MESSAGE, UNREACHABLE_MESSAGE
from services.catalogs import Catalogs
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.live.live_session import CRAWLING, UNAVAILABLE
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.live.tuning import channel_key
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode, item_key
from services.provider_ids import ALL_PROVIDERS, mixed_group_id, provider_of
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind
from services.watch_state import WatchState
from services.zapping import ZapController
from tests.test_navigation import FakeCatalog as NavCatalog
from ui.keymap import Action
from ui.link_lanes import Target
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.channel_rows import ChannelRowsModel
from ui.overlays.drawer import LOADING_TEXT, PROVIDER_ERROR_TEXT, Drawer
from ui.overlays.guide import Guide
from ui.overlays.next_card import NextCard
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import ZapBanner
from ui.tv_controller import Overlays, TVController, scrub

NOW = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
MIB = 1024 * 1024
CHANNELS = [
    {"id": i, "name": name, "number": str(i), "cmd": f"ffrt http://tv/{i}"}
    for i, name in ((1, "One"), (2, "Two"), (3, "Three"), (4, "Four"))
]
NO_MODS = Qt.KeyboardModifier.NoModifier
# Channels of other providers than the browsed "Home" (QiTV 2.1 mixes providers).
HARBOR_ONE = {**CHANNELS[0], "_provider": "Harbor IPTV"}  # an Xtream panel, 1 stream
PORTAL_ONE = {**CHANNELS[0], "_provider": "Portal B"}  # an STB portal
HOME_ONE = {**CHANNELS[0], "_provider": "Home"}
BACKUP_TWO = {**CHANNELS[1], "_provider": "Backup"}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class FakeOwnCatalog:
    """One provider's own catalog behind the hub: its live channels, loaded on request."""

    def __init__(self, channels):
        self.channels = channels
        self.loaded = False
        self.requests = []
        self.sectionFailed = FakeSignal()

    def has_content(self, section):
        return self.loaded

    def request_section(self, section, *, force=False):
        self.requests.append(section)

    def items(self, section, group_id):
        return list(self.channels) if self.loaded else None


class FakeHub(QObject):
    """The Catalogs hub as far as the last-channel start needs it."""

    contentChanged = Signal(str)
    providerReady = Signal(str)
    providerFailed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.own = {}
        self.ensured = []
        self.ready = set()

    def ensure(self, name):
        self.ensured.append(name)

    def is_ready(self, name):
        return name in self.ready

    def catalog(self, name):
        return self.own[name]


class FakeCatalog(NavCatalog):
    providerFailed = Signal(str, str)
    epgReady = Signal()
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.live = {"10": list(CHANNELS)}
        self.names = ["Home", "Backup"]
        self.links = []  # (request_id, item, section, is_episode)
        self.epg = {}
        self.loading = False
        self.epg_refreshes = 0
        self.guides_used = []  # the sets of providers whose guides were asked for
        self.episode_map = {}  # (series id, season id) -> episodes; missing: not loaded
        # The other providers of the list (the hub's `provider(name)`), their reported limits
        # and the sign-ins asked again by name.
        self.specs = {
            "Harbor IPTV": {"name": "Harbor IPTV", "type": "XTREAM"},
            "Portal B": {"name": "Portal B", "type": "STB"},
        }
        self.limits_of = {"Harbor IPTV": 1}
        self.setups = []
        self.catalogs = FakeHub()

    def provider_names(self):
        return list(self.names)

    def source(self):
        return self.provider

    def select_provider(self, name=None, **kwargs):
        super().select_provider(name, **kwargs)
        if name in self.names:
            self.provider = name  # the page shows it from now on

    def episodes(self, series, season):
        episodes = self.episode_map.get((series["id"], season["id"]))
        return None if episodes is None else list(episodes)

    def items(self, section, group_id):
        if section == "live" and group_id == "*":
            return [c for channels in self.live.values() for c in channels]
        return super().items(section, group_id)

    def is_loading(self):
        return self.loading

    def refresh_epg(self, *, force=False):
        self.epg_refreshes += 1

    def use_guides(self, names):
        self.guides_used.append(set(names))

    def request_link(self, item, *, section, is_episode=False):
        request_id = len(self.links) + 1
        self.links.append((request_id, item, section, is_episode))
        return request_id

    def programmes(self, channel, now, limit=8):
        return self.epg.get(channel.get("id"), [])[:limit]

    def is_live(self, section):
        return section == "live"

    def max_connections(self):
        return 1

    def provider_entry(self, name):
        if name == self.provider:
            return self.current_provider()
        return self.specs[name]  # KeyError: not in the list

    def max_connections_of(self, name):
        return self.limits_of.get(name)

    def sign_in_again(self, name):
        self.setups.append(name)
        self.catalogs.ready.discard(name)  # until its setup is done again

    def hub_ready(self, name):
        """A provider's setup is done: the hub says so (link lanes), then the page's view."""
        self.catalogs.providerReady.emit(name)
        self.providerReady.emit(name)

    def hub_failed(self, name, message):
        self.catalogs.providerFailed.emit(name, message)
        self.providerFailed.emit(name, message)

    def current_provider(self):
        return {"name": self.provider, "type": self.ptype}


class FakeSession(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    inputEnded = Signal()

    def __init__(self):
        super().__init__()
        self.holding = False  # a stop leaves libVLC's input open until let_go()
        self.plays, self.seeks, self.relative, self.rates = [], [], [], []
        self.stops = self.toggles = 0
        self.seekable = False
        self.level = 50
        self.muted = False
        self.audio = [(-1, "Disable"), (1, "English"), (2, "Deutsch")]
        self.subtitles = [(-1, "Disable"), (3, "Français")]
        self.audio_set, self.subtitle_set = [], []
        self.audio_playing = 1  # audio_track(): the track libVLC plays
        self.delay = 0  # audio_delay(): the sound's sync, in ms
        self.delays = []  # every set_audio_delay()
        self.play_delays = []  # the sync each play() opened with (PlaybackSession sets it then)
        self.subtitle_on = -1  # subtitle_track()
        self.subtitle_ms = 0  # subtitle_delay()
        self.files = []  # add_subtitle_file()
        self.clears = 0  # clear_subtitles()
        self.url, self.kind = None, None  # what plays, as PlaybackSession.url and .kind
        self.shape = "auto"  # aspect(): the picture's shape (2.3)
        self.shapes = []  # every set_aspect()
        self.deinterlacing = "auto"
        self.stats = {}  # what video_stats() reads
        self.software = False  # decodes_in_software(): asked for, or the picture froze

    def play(self, url, **kwargs):
        self.plays.append((url, kwargs))
        self.play_delays.append(self.delay)
        self.url, self.kind = url, kwargs.get("kind")
        self.software = bool(kwargs.get("software"))
        self.stateChanged.emit("opening")

    def decodes_in_software(self):
        return self.software

    def stop(self):
        self.stops += 1
        self.stateChanged.emit("idle")

    def is_seekable(self):
        return self.seekable

    def holds_input(self):
        return self.holding

    def let_go(self):
        """The worker ended libVLC's input: the provider connection is closed."""
        self.holding = False
        self.inputEnded.emit()

    def seek(self, ms):
        self.seeks.append(ms)

    def seek_relative(self, ms):
        self.relative.append(ms)

    def toggle_pause(self):
        self.toggles += 1

    def set_rate(self, rate):
        self.rates.append(rate)

    def volume(self):
        return self.level

    def set_volume(self, value):
        self.level = value

    def toggle_mute(self):
        self.muted = not self.muted
        return self.muted

    def set_muted(self, muted):
        self.muted = bool(muted)

    def audio_tracks(self):
        return list(self.audio)

    def set_audio_track(self, track_id):
        self.audio_set.append(track_id)
        self.audio_playing = track_id

    def audio_track(self):
        return self.audio_playing

    def audio_delay(self):
        return self.delay

    def set_audio_delay(self, ms):
        self.delay = ms
        self.delays.append(ms)

    def subtitle_tracks(self):
        return list(self.subtitles)

    def set_subtitle_track(self, track_id):
        self.subtitle_set.append(track_id)
        self.subtitle_on = track_id

    def subtitle_track(self):
        return self.subtitle_on

    def subtitle_delay(self):
        return self.subtitle_ms

    def set_subtitle_delay(self, ms):
        self.subtitle_ms = ms

    def add_subtitle_file(self, path):
        """As libVLC adds a slave: a new track, named after the file, chosen."""
        self.files.append(path)
        track = max(i for i, _ in self.subtitles) + 1
        self.subtitles.append((track, os.path.basename(path)))
        self.subtitle_on = track
        return True

    def clear_subtitles(self):
        self.clears += 1
        self.files, self.subtitle_ms = [], 0

    def aspect(self):
        return self.shape

    def set_aspect(self, aspect):
        self.shape = aspect
        self.shapes.append(aspect)

    def set_video_size(self, width, height):
        pass

    def deinterlace(self):
        return self.deinterlacing

    def set_deinterlace(self, choice):
        self.deinterlacing = choice
        return True

    def video_stats(self):
        return dict(self.stats)


class FakeOverlay(QObject):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.visible = False

    def isVisible(self):
        return self.visible

    def record(self, name, *args):
        self.calls.append((name, *args))

    def named(self, name):
        return [c[1:] for c in self.calls if c[0] == name]


class FakeBanner(FakeOverlay):
    info = None

    def show_info(self, info):
        self.record("show_info", info)
        self.info = info
        self.visible = True

    def show_digits(self, text):
        self.record("show_digits", text)

    def dismiss(self):
        self.record("dismiss")
        self.visible = False


class FakeColumn(FakeOverlay):
    closed = Signal()
    rowClicked = Signal(int)

    def open(self, title, channels, index, show_provider=False):
        self.record("open", title, channels, index)
        self.shows_provider = show_provider
        self.visible = True

    def highlight(self, index):
        self.record("highlight", index)

    def current_index(self):
        moves = [c[-1] for c in self.calls if c[0] in ("open", "highlight")]
        return moves[-1] if moves else -1

    def close(self):
        was_visible, self.visible = self.visible, False
        self.record("close")
        if was_visible:
            self.closed.emit()
        return True

    def refresh_now(self):
        self.record("refresh_now")


class FakeDrawer(FakeOverlay):
    playRequested = Signal(object, object, object)
    channelFound = Signal(object, str)
    contextRequested = Signal(object, object)
    closed = Signal()
    highlight = None
    found = False  # the find lists what it found

    def highlighted(self):
        return self.highlight

    def finding(self):
        return self.found

    def open_drawer(self, focus_key=None, focus_side="right"):
        if focus_side == "right":
            self.record("open_drawer", focus_key)
        else:
            self.record("open_drawer", focus_key, focus_side)
        self.visible = True

    def close_drawer(self):
        self.record("close_drawer")
        self.visible = False

    def escape(self):
        self.record("escape")
        self.visible = False
        return True

    def move_by(self, delta):
        self.record("move_by", delta)

    def page(self, delta):
        self.record("page", delta)

    def left(self):
        self.record("left")

    def right(self):
        self.record("right")

    def activate(self):
        self.record("activate")

    def type_text(self, text):
        self.record("type_text", text)

    def backspace(self):
        self.record("backspace")


class FakeTransport(FakeOverlay):
    badge = False  # a live channel shows: badge_rect() is not empty
    logo = None  # the logo the last show_live/show_timeshift was given

    playPauseRequested = Signal()
    stopRequested = Signal()  # 2.6
    seekRequested = Signal(int)
    seekRelativeRequested = Signal(int)
    speedRequested = Signal(float)
    fullscreenRequested = Signal()
    audioRequested = Signal()
    subtitleRequested = Signal()
    pictureRequested = Signal()
    goLiveRequested = Signal()
    programmeStartRequested = Signal()
    seekToRequested = Signal(float)
    nextRequested = Signal()
    pipRequested = Signal()
    gridRequested = Signal()
    layoutRequested = Signal()
    restoreRequested = Signal()
    miniPlayerRequested = Signal()
    externalRequested = Signal(QPoint)
    volumeRequested = Signal()
    searchRequested = Signal()
    guideRequested = Signal()
    previousRequested = Signal()
    infoRequested = Signal()
    favoriteRequested = Signal()
    recordRequested = Signal()  # 2.3
    bandChanged = Signal()
    recording_since = None

    def set_muted(self, muted):
        self.record("set_muted", muted)

    def set_level(self, level):
        self.record("set_level", level)

    def set_recording(self, since):
        self.record("set_recording", since)
        self.recording_since = since

    def set_favorite(self, favorite):
        self.record("set_favorite", favorite)

    def set_next_available(self, available):
        self.record("set_next_available", available)

    def set_pip_state(self, available, open_):
        self.record("set_pip_state", available, open_)

    def set_grid_state(self, available, shown, layout="", restorable=False, cells=0):
        self.record("set_grid_state", available, shown, layout)
        self.restorable = restorable

    def follow_pip(self, rect):
        self.record("follow_pip", rect)

    held_button = ""  # hold(): the button whose panel shows

    def hold(self, button):
        self.record("hold", button)
        self.held_button = button

    def held(self):
        return self.held_button

    def button_rect(self, name):
        return QRectF(1000, 650, 90, 28)  # where its buttons are, as far as panels need

    def controls_top(self):
        return 640

    def control_at(self, pos):
        return None

    def pos(self):
        return QPoint(0, 0)

    def show_vod(self, title, subtitle):
        self.record("show_vod", title, subtitle)
        self.visible = True

    def show_live(self, title, subtitle, logo=None):
        self.record("show_live", title, subtitle)
        self.logo = logo
        self.visible = True

    def show_timeshift(self, title, subtitle, logo=None):
        self.record("show_timeshift", title, subtitle)
        self.logo = logo
        self.visible = True

    def badge_rect(self):
        # The live badge is only there while a live channel shows (the test says when).
        return QRectF(0, 0, 40, 24) if self.badge else QRectF()

    def set_logo(self, logo):
        self.record("set_logo", logo)

    def update_timeline(self, state, ticks):
        self.record("update_timeline", state, ticks)

    def update_position(self, position, length):
        self.record("update_position", position, length)

    def set_paused(self, paused):
        self.record("set_paused", paused)

    def set_speed(self, rate):
        self.record("set_speed", rate)

    def poke(self):
        self.record("poke")
        self.visible = True

    def dismiss(self):
        self.record("dismiss")
        self.visible = False


class FakeToast(FakeOverlay):
    hint = None

    def show_message(self, text, kind="info", ms=3000):
        self.record("show_message", text, kind, ms)

    def set_hint(self, text):
        self.record("set_hint", text)
        self.hint = text

    def messages(self):
        return [(text, kind) for text, kind, _ms in self.named("show_message")]


class FakeGuide(FakeOverlay):
    tuneRequested = Signal(object)
    closed = Signal()
    searchRequested = Signal()
    fullscreenRequested = Signal()
    rowsMoved = Signal()
    current = None
    visible_rows = ()

    def current_channel(self):
        return self.current

    def visible_channels(self):
        return list(self.visible_rows)

    def open_guide(self, channels, current):
        self.record("open_guide", channels, current)
        self.visible = True

    def close_guide(self):
        self.record("close_guide")
        was_visible, self.visible = self.visible, False
        if was_visible:
            self.closed.emit()

    def escape(self):
        self.close_guide()
        return True

    def move_by(self, delta):
        self.record("move_by", delta)

    def page(self, delta):
        self.record("page", delta)

    def shift(self, delta):
        self.record("shift", delta)

    def activate(self):
        self.record("activate")

    def invalidate(self):
        self.record("invalidate")


class FakeNextCard(FakeOverlay):
    playNow = Signal()
    cancelled = Signal()

    def show_next(self, title, subtitle, seconds=10, *, kind="episode"):
        self.record("show_next", title, subtitle, kind)
        self.visible = True

    def activate(self):
        if self.visible:
            self.visible = False
            self.playNow.emit()

    def escape(self):
        if not self.visible:
            return False
        self.visible = False
        self.cancelled.emit()
        return True


class FakeWelcome(FakeOverlay):
    addRequested = Signal()

    def show_card(self):
        self.record("show_card")
        self.visible = True

    def dismiss(self):
        self.record("dismiss")
        self.visible = False

    def activate(self):
        if not self.visible:
            return False
        self.addRequested.emit()
        return True


class FakeLogos(QObject):
    logoReady = Signal(str)

    def __init__(self):
        super().__init__()
        self.pixmaps = {}
        self.requested = []

    def pixmap(self, url):
        return self.pixmaps.get(url)

    def request(self, urls):
        self.requested.extend(urls)


class FakeLiveSession(QObject):
    """LiveSession's surface: records calls; the test drives its mode and released()."""

    modeChanged = Signal(str)
    timelineChanged = Signal(dict)
    notice = Signal(str)
    failed = Signal(str)
    released = Signal()
    recordingChanged = Signal(bool)  # 2.3
    recordingEnded = Signal(str, str, int)  # (reason, path, written)
    backOffered = Signal(float, float)  # 2.6: (where it was left, that moment's Unix time)

    def go_back_to(self, t):
        self.calls.append(("go_back_to", t))

    def set_aside(self):  # 2.6: its recording goes on while its channel was left
        self.calls.append(("set_aside",))

    def bring_back(self):
        self.calls.append(("bring_back",))

    def __init__(self, playback):
        super().__init__()
        self.playback = playback
        self.calls = []
        self.mode = ""
        self.running = False
        self.no_rewind_reason = UNAVAILABLE
        self.resizes = []  # the memory buffer sizes the multiview gave it
        self.background = False  # a multiview tile without the focus
        self.recorded_since = None  # recording since (a clock reading), else None

    def recordable(self):
        return self.running and self.mode == "pipeline"

    def recording(self):
        return self.recorded_since is not None

    def recording_since(self):
        return self.recorded_since

    def start_recording(self, path, **kwargs):
        if not self.recordable() or self.recording():
            return False
        self.calls.append(("start_recording", path))
        self.recorded_since = 100.0
        self.recordingChanged.emit(True)
        return True

    def stop_recording(self):
        if self.recording():
            self.calls.append(("stop_recording",))
            self.recorded_since = None
            self.recordingChanged.emit(False)

    def named(self, name):
        return [c[1:] for c in self.calls if c[0] == name]

    def is_pipeline(self):
        return self.mode == "pipeline"

    def start(self, url, **kwargs):
        self.calls.append(("start", url, kwargs))
        self.running, self.mode = True, ""
        self.url = url

    def stop(self):
        self.stop_recording()  # as LiveSession: a recording stops, its file is kept
        if not self.running:
            return
        self.calls.append(("stop",))
        direct = self.mode == "direct"
        self.running, self.mode = False, ""
        self.playback.stop()
        if direct and not self.playback.holds_input():
            self.released.emit()  # as LiveSession: at once when nothing is left open

    def play_pipeline(self):
        """The session is ready: VLC opens the loopback stream."""
        self.playback.play("http://127.0.0.1:9/t/s1/stream/0.ts", kind=PlayKind.LIVE_PIPELINE)
        self.mode = "pipeline"
        self.modeChanged.emit("pipeline")

    def play_direct(self):
        """It fell back to playing the provider's stream."""
        self.playback.play(self.url, kind=PlayKind.LIVE, latency="low", user_agent=None)
        self.mode = "direct"
        self.modeChanged.emit("direct")

    def toggle_pause(self):
        self.calls.append(("toggle_pause",))

    def seek_to(self, t):
        self.calls.append(("seek_to", t))

    def seek_relative(self, dt):
        self.calls.append(("seek_relative", dt))

    def go_live(self):
        self.calls.append(("go_live",))

    def set_speed(self, rate):
        self.calls.append(("set_speed", rate))

    def set_latency(self, name):
        self.calls.append(("set_latency", name))

    def resize(self, max_bytes):
        """The multiview: its memory buffer grows with the focus or shrinks without it."""
        self.resizes.append(max_bytes)

    def set_background(self, background):
        """The multiview: a tile without the focus plays at 1x and only jumps to live."""
        self.background = background


class FakeWindow:
    def __init__(self):
        self.calls = []

    def toggle_fullscreen(self):
        self.calls.append("toggle_fullscreen")

    def leave_fullscreen(self):
        self.calls.append("leave_fullscreen")
        return False

    def toggle_mini_player(self):
        self.calls.append("toggle_mini_player")


class FakeWatch:
    def __init__(self):
        self.plays, self.positions = [], []
        self.flushes = 0
        self.resume = {}

    @staticmethod
    def content_id(provider_name, item, *, item_type):
        return f"{provider_name}:{item_type}:{item.get('id')}"

    def resume_position(self, cid):
        return self.resume.get(cid)

    def record_play(self, provider_name, item, *, item_type, link, group_id=None):
        self.plays.append((provider_name, item, item_type, link, group_id))

    def update_position(self, cid, position_ms, duration_ms):
        self.positions.append((cid, position_ms, duration_ms))

    def flush(self):
        self.flushes += 1


class FakeConfig(SimpleNamespace):
    """The settings the controller reads; save_config() counts the saves."""

    saves = 0

    def save_config(self):
        self.saves += 1


class FakeExternal:
    def __init__(self, result=(True, "")):
        self.calls = []
        self.result = result
        self.missing = set()  # players find_vlc/find_mpv do not find

    def find_vlc(self):
        return None if "vlc" in self.missing else "/Applications/VLC.app"

    def find_mpv(self):
        return None if "mpv" in self.missing else "/usr/local/bin/mpv"

    def launch_vlc(self, url):
        self.calls.append(("vlc", url))
        return self.result

    def launch_mpv(self, url):
        self.calls.append(("mpv", url))
        return self.result


class ControllerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = FakeConfig(
            favorites=[],
            watch_history=[],
            last_live_channel=None,
            start_with_last_channel=True,
            play_in_vlc=False,
            play_in_mpv=False,
            live_latency="low",
            mouse_wheel_action="channel",
            channel_epg=True,
            auto_play_episodes=True,
            auto_play_movies=True,
        )
        self.catalog = FakeCatalog()
        self.nav = NavModel(self.catalog, self.config)
        self.clock = Clock()
        self.zap = ZapController(rest_ms=20, burst_ms=1000, digits_ms=30, clock=self.clock)
        self.session = FakeSession()
        self.watch = FakeWatch()
        self.window = FakeWindow()
        self.external = FakeExternal()
        self.banner, self.column = FakeBanner(), FakeColumn()
        self.drawer, self.transport, self.toast = FakeDrawer(), FakeTransport(), FakeToast()
        self.guide, self.next_card = FakeGuide(), FakeNextCard()
        self.welcome = FakeWelcome()
        self.library_toast = FakeToast()  # the Library page's
        self.controller = self.make_controller(self.column)

    def make_controller(self, column, **extra):
        controller = TVController(
            config=self.config,
            catalog=self.catalog,
            nav=self.nav,
            zap=self.zap,
            session=self.session,
            watch=self.watch,
            overlays=self.overlays(column),
            window_actions=self.window,
            external=self.external,
            now=lambda: NOW,
            **extra,
        )
        live = extra.get("live")

        def close():
            controller.shutdown()  # late zap timers must not reach a dead overlay
            # The fakes report the releases the shutdown waits for, so no capped wait
            # (LiveTuner's input wait, the main stream's) outlives the test and logs later.
            if self.session.holding:
                self.session.let_go()
            if live is not None:
                live.released.emit()

        self.addCleanup(close)
        return controller

    def overlays(self, column):
        return Overlays(
            self.banner,
            column,
            self.drawer,
            self.transport,
            self.toast,
            self.guide,
            self.next_card,
            self.welcome,
            library_toast=self.library_toast,
        )

    def tune(self, index):
        self.controller.tune(CHANNELS[index], group_key="10", channels=CHANNELS)

    def ready(self, url="http://stream/1"):
        """Resolve the newest link request."""
        self.catalog.linkReady.emit(self.catalog.links[-1][0], url)

    def play(self, index):
        self.tune(index)
        self.ready(f"http://stream/{CHANNELS[index]['id']}")

    def played_urls(self):
        return [url for url, _ in self.session.plays]

    def requested(self):
        return [item.get("name") for _, item, _, _ in self.catalog.links]


class LinkPipelineTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()

    def test_two_quick_tunes_play_only_the_second(self):
        self.tune(0)
        self.tune(1)
        self.assertEqual(self.requested(), ["One"])  # one request in flight at a time
        self.assertEqual(self.banner.named("show_info")[-1][0].name, "Two")
        self.catalog.linkReady.emit(1, "http://stream/1")  # the first resolves: discarded
        self.assertEqual(self.session.plays, [])
        self.assertEqual(self.requested(), ["One", "Two"])
        self.catalog.linkReady.emit(1, "http://stream/1")  # a late duplicate is ignored
        self.catalog.linkReady.emit(2, "http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        url, kwargs = self.session.plays[0]
        self.assertEqual(kwargs["kind"], PlayKind.LIVE)
        self.assertEqual(kwargs["latency"], "low")
        self.assertEqual(self.zap.context.current(), CHANNELS[1])
        self.assertEqual(self.watch.plays, [("Home", CHANNELS[1], "channel", url, "10")])

    def test_the_list_opens_where_the_big_picture_was_picked(self):
        self.catalog.select_provider("Backup")
        self.play(1)
        self.catalog.select_provider("Home")  # browsing elsewhere
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual([n.key for n in self.nav.path][:3], ["Backup", "live", "10"])
        self.assertEqual(self.nav.revealed()["name"], "Two")
        self.assertEqual(self.catalog.provider, "Backup")
        self.assertEqual(self.drawer.named("open_drawer")[-1], (item_key(CHANNELS[1]),))

    def stamped(self, provider="Harbor IPTV"):
        """The channels as that provider's list holds them."""
        if provider not in self.catalog.names:
            self.catalog.names.append(provider)
        return [{**c, "_provider": provider} for c in CHANNELS]

    def test_a_zap_after_browsing_elsewhere_keeps_the_list_it_was_picked_from(self):
        harbor = self.stamped()
        self.catalog.select_provider("Harbor IPTV")
        self.controller.tune(harbor[1], group_key="10", channels=harbor)
        self.ready("http://stream/2")
        self.catalog.select_provider("Home")  # the drawer's sources: browsing another
        self.controller._on_zap_tune(harbor[2])  # then up/down
        self.ready("http://stream/3")
        self.assertEqual(self.controller._current.source, "Harbor IPTV")
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual([n.key for n in self.nav.path][:3], ["Harbor IPTV", "live", "10"])
        self.assertEqual(self.nav.revealed()["name"], "Three")

    def test_a_channel_started_from_outside_the_browsed_list_reveals_its_provider(self):
        harbor = self.stamped()  # the last channel of an owner other than the browsed one
        self.controller.tune(harbor[0], group_key="*", channels=harbor)
        self.ready("http://stream/1")
        self.assertEqual(self.catalog.provider, "Home")
        self.assertEqual(self.controller._current.source, "Harbor IPTV")
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual(self.nav.path[0].key, "Harbor IPTV")
        self.assertEqual(self.nav.revealed()["name"], "One")

    def test_after_a_swap_the_list_follows_the_new_big_picture(self):
        self.play(0)
        self.catalog.select_provider("Backup")
        # What _on_large_changed leaves: the other slot's target is the large one's now.
        self.controller._current = Target(
            CHANNELS[1], "live", "Two", group_key="10", source="Backup"
        )
        self.catalog.select_provider("Home")
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual(self.nav.path[0].key, "Backup")
        self.assertEqual(self.nav.revealed()["name"], "Two")

    def test_three_tunes_during_one_request_make_two_requests(self):
        self.tune(0)
        for index in (1, 2, 3):
            self.tune(index)
        self.assertEqual(len(self.catalog.links), 1)
        self.catalog.linkReady.emit(1, "http://stream/1")
        self.assertEqual(self.requested(), ["One", "Four"])
        self.catalog.linkReady.emit(2, "http://stream/4")
        self.assertEqual(len(self.catalog.links), 2)
        self.assertEqual(self.played_urls(), ["http://stream/4"])
        self.assertEqual([p[1]["name"] for p in self.watch.plays], ["Four"])

    def test_superseded_failure_is_silent_and_requests_the_newest(self):
        self.tune(0)
        self.tune(2)
        self.catalog.linkFailed.emit(1, "Portal busy")
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One", "Three"])

    def test_link_failure_toasts_an_error(self):
        self.tune(0)
        self.catalog.linkFailed.emit(1, "Portal down")
        self.assertEqual(self.toast.messages(), [("One: Portal down", "error")])
        self.assertEqual(self.session.plays, [])
        self.tune(1)  # the pipeline is free again
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_play_in_vlc_launches_externally(self):
        self.config.play_in_vlc = True
        self.play(0)
        self.assertEqual(self.external.calls, [("vlc", "http://stream/1")])
        self.assertEqual(self.session.plays, [])
        self.assertEqual(len(self.watch.plays), 1)

    def test_play_elsewhere_hands_the_link_once_the_in_app_stream_is_gone(self):
        """A phone's Watch on this phone: what plays, its link handed on as Play in VLC hands
        one; the in-app stream stops first, so the provider counts one."""
        self.play(0)
        self.assertTrue(self.controller.showing_here())
        handed = []

        def launch(target, url):
            handed.append((target.item["name"], url))
            return True, ""

        self.assertTrue(self.controller.play_elsewhere(launch))
        self.ready("http://stream/1")  # its link, asked for the hand-off
        self.assertEqual(handed, [("One", "http://stream/1")])
        self.assertEqual(self.session.stops, 1)
        self.assertEqual(self.played_urls(), ["http://stream/1"])  # nothing more plays here
        self.assertFalse(self.controller.playing())
        self.assertFalse(self.controller.showing_here())  # what the remote tells a TV play by

    def test_play_elsewhere_a_chosen_channel(self):
        """The phone's own list: a channel the TV doesn't play goes to the phone, and the TV's
        stream stops for it all the same."""
        self.play(0)
        handed = []

        def launch(target, url):
            handed.append((target.item["name"], url))
            return True, ""

        chosen = Target(CHANNELS[2], "live", "Three", group_key="10")
        self.assertTrue(self.controller.play_elsewhere(launch, chosen))
        self.assertEqual(self.requested()[-1], "Three")
        self.ready("http://stream/3")
        self.assertEqual(handed, [("Three", "http://stream/3")])
        self.assertEqual(self.session.stops, 1)
        self.assertFalse(self.controller.playing())

    def test_a_titles_resume_key_is_the_tvs_and_a_channel_has_none(self):
        film = self.controller.title_target(VodTests.MOVIE, "movie")
        key = self.controller.resume_key(film)
        self.assertEqual(key, self.watch.content_id("Home", VodTests.MOVIE, item_type="movie"))
        self.assertIsNone(self.controller.resume_key(Target(CHANNELS[0], "live", "One")))

    def test_play_elsewhere_with_nothing_current(self):
        self.assertFalse(self.controller.play_elsewhere(lambda target, url: (True, "")))
        self.assertEqual(self.catalog.links, [])

    def test_external_failure_toasts(self):
        self.config.play_in_mpv = True
        self.external.result = (
            False,
            "MPV Media Player is not installed.\n\nPlease install MPV:\n• macOS: brew install mpv",
        )
        self.play(0)
        self.assertEqual(self.external.calls, [("mpv", "http://stream/1")])
        self.assertEqual(self.toast.messages(), [("MPV Media Player is not installed.", "error")])
        self.assertEqual(self.watch.plays, [])

    def test_browsing_another_provider_keeps_the_pending_tune(self):
        self.catalog.hub_ready("Home")
        self.controller.tune(HOME_ONE, group_key="10", channels=[HOME_ONE])  # a link on its way
        self.controller.select_provider("Backup")
        self.assertEqual(self.catalog.selected[-1], "Backup")
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.assertEqual(self.requested(), ["One"])  # nothing asked again, nothing dropped
        self.ready("http://stream/1")  # the link still arrives
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_browsing_another_provider_keeps_the_zap_list_and_the_guide(self):
        self.catalog.hub_ready("Home")
        self.play(0)
        self.controller.handle(Action.GUIDE)
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.assertEqual(self.zap.context.group_key, "10")
        self.assertEqual(len(self.zap.context.channels), len(CHANNELS))
        self.assertTrue(self.guide.visible)
        self.assertEqual(self.guide.named("close_guide"), [])
        self.assertEqual(self.session.stops, 0)

    def tune_stamped(self, item):
        self.controller.tune(item, group_key="10", channels=[item])

    def test_a_browsed_providers_failure_keeps_another_providers_pending_tune(self):
        self.catalog.hub_ready("Home")
        self.catalog.provider = "Backup"  # the list shows Backup
        self.catalog.hub_ready("Backup")
        self.tune_stamped(HOME_ONE)
        self.catalog.hub_failed("Backup", "down")
        self.assertEqual(self.requested(), ["One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_failed_provider_releases_its_request_in_all_mode(self):
        self.catalog.provider = "@all"
        self.tune_stamped(HOME_ONE)
        self.catalog.hub_failed("Home", "down")  # its link never settles
        self.tune_stamped(BACKUP_TWO)  # not queued behind the dead request
        self.assertEqual(self.requested(), ["One", "Two"])
        self.ready("http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])

    def test_another_providers_failed_sign_in_says_so_and_info_follows_what_plays(self):
        # The list shows Home; a channel of Harbor was picked (Recent, the
        # Library's search, the previous channel) and Harbor can't sign in.
        self.catalog.hub_ready("Home")
        self.play(0)  # One plays
        self.tune_stamped({**CHANNELS[2], "_provider": "Harbor IPTV"})
        self.assertEqual(self.controller._channel["name"], "Three")
        # The list shows Home: only the hub hears Harbor's failure.
        self.catalog.catalogs.providerFailed.emit("Harbor IPTV", "Portal down")
        self.assertEqual(self.toast.messages()[-1], ("Harbor IPTV: Portal down", "error"))
        self.assertEqual(self.controller._channel, CHANNELS[0])  # INFO, ★ and the guide
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")  # late: ignored
        self.assertEqual(len(self.toast.messages()), 1)

    def test_the_shown_providers_failure_is_said_once(self):
        self.catalog.hub_ready("Home")
        self.play(0)
        self.tune_stamped({**CHANNELS[2], "_provider": "Home"})
        self.catalog.hub_failed("Home", "Portal down")  # the flow says it, as before 2.1
        self.assertEqual(self.toast.messages(), [("Home: Portal down", "error")])
        self.assertEqual(self.controller._channel, CHANNELS[0])

    def test_a_failed_providers_request_lets_the_queued_other_one_go(self):
        self.catalog.provider = "@all"
        self.tune_stamped(HOME_ONE)
        self.tune_stamped(BACKUP_TWO)  # queued behind One
        self.catalog.hub_failed("Home", "down")
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_refreshing_every_provider_asks_a_link_in_flight_again_in_all_mode(self):
        self.catalog.provider = "@all"
        self.tune_stamped(HOME_ONE)
        self.catalog.hub_ready("Backup")  # another provider's setup: untouched
        self.assertEqual(self.requested(), ["One"])
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_forced_setup_of_a_provider_not_shown_asks_its_link_again(self):
        self.catalog.provider = "Backup"
        self.tune_stamped(HOME_ONE)
        self.catalog.hub_ready("Home")  # the hub's signal; the view shows Backup
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_tune_waiting_for_its_loading_provider_is_asked_when_that_one_is_ready(self):
        self.catalog.provider = "@all"
        self.tune_stamped(HOME_ONE)
        self.catalog.linkFailed.emit(1, PROVIDER_LOADING_MESSAGE)  # Home is loading
        self.assertEqual(self.requested(), ["One"])
        self.catalog.hub_ready("Backup")  # another provider: Home's tune waits on
        self.assertEqual(self.requested(), ["One"])
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_ready_provider_never_asks_while_another_providers_tune_is_in_flight(self):
        # Home's choice waits behind Backup's request, and one link at a time.
        self.catalog.provider = "@all"
        self.tune_stamped(BACKUP_TWO)  # in flight
        self.tune_stamped(HOME_ONE)  # the newest choice, queued
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["Two"])
        self.ready("http://stream/2")  # Two was superseded: One is asked now
        self.assertEqual(self.requested(), ["Two", "One"])
        self.assertEqual(self.played_urls(), [])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_loading_providers_failure_lets_another_providers_newer_choice_go(self):
        # Backup's choice, queued behind Home's request, waited for Home's setup
        # (only Home's own choices are asked again when it is ready): the tune hung.
        self.catalog.provider = "@all"
        self.tune_stamped(HOME_ONE)
        self.tune_stamped(BACKUP_TWO)  # queued behind One
        self.catalog.linkFailed.emit(1, PROVIDER_LOADING_MESSAGE)  # Home is loading
        self.assertEqual(self.requested(), ["One", "Two"])
        self.ready("http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        self.catalog.hub_ready("Home")  # One was superseded: never asked again
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_a_loading_providers_failure_drops_a_choice_to_stay_on_another_providers(self):
        self.catalog.provider = "@all"
        self.tune_stamped(BACKUP_TWO)
        self.ready("http://stream/2")  # Two plays
        self.tune_stamped(HOME_ONE)
        self.tune_stamped(BACKUP_TWO)  # back to Two: stay on it
        self.catalog.linkFailed.emit(2, PROVIDER_LOADING_MESSAGE)  # Home is loading
        self.assertFalse(self.controller._lanes.busy())  # nothing waits for Home's setup
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["Two", "One"])
        self.assertEqual(self.played_urls(), ["http://stream/2"])

    def test_provider_refresh_requests_the_pending_tune_again(self):
        self.catalog.hub_ready("Home")
        self.tune(0)
        self.controller.select_provider(force_refresh=True)
        self.tune(1)
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["One", "Two"])
        self.catalog.linkReady.emit(1, "http://stream/1")  # stale
        self.ready("http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_session_error_toasts(self):
        self.catalog.ptype = "M3UPLAYLIST"  # no link to make again
        self.play(0)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])

    def test_a_playback_error_in_the_corner_shows_on_the_librarys_toast(self):
        # The player's own toast is hidden while it sits in the corner.
        self.catalog.ptype = "M3UPLAYLIST"  # no link to make again
        self.play(0)
        self.controller.enter_library_playing()
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [])
        expected = [("One: The stream could not be opened", "error")]
        self.assertEqual(self.library_toast.messages(), expected)
        self.controller.leave_library_playing()
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), expected)  # back on the player page

    def test_an_stb_channel_failing_in_direct_play_makes_its_link_again_once(self):
        # STB play tokens expire: a fresh link before the error toast, as the pipeline's
        # final failure does.
        self.play(0)  # the controller plays it itself (no time-shift pipeline)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1?play_token=new")
        self.assertTrue(
            wait_until(lambda: self.played_urls()[-1:] == ["http://stream/1?play_token=new"])
        )
        self.session.errorOccurred.emit("The stream could not be opened")  # once per play
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])
        self.assertEqual(self.requested(), ["One", "One"])
        self.play(1)  # a new play may try again
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.requested(), ["One", "One", "Two", "Two"])

    def test_a_failed_stb_link_is_made_again_by_its_own_provider(self):
        self.catalog.catalogs.ready.add("Portal B")  # set up: its link was made
        self.controller.tune(PORTAL_ONE, group_key="10", channels=[PORTAL_ONE])
        self.ready("http://stream/1")
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.catalog.setups, ["Portal B"])  # signed in again, then...
        self.assertEqual(self.catalog.links[-1][1]["_provider"], "Portal B")  # ...its 2nd link
        self.assertEqual(len(self.catalog.links), 2)

    def test_a_browsed_providers_retry_does_not_sign_in_again(self):
        self.play(0)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.catalog.setups, [])
        self.assertEqual(self.requested(), ["One", "One"])

    def test_the_retry_follows_the_channels_provider_type(self):
        # Browsed Home is an STB portal, but Harbor's panel (Xtream) has no link to make again.
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(len(self.catalog.links), 1)
        self.assertEqual(self.catalog.setups, [])
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])

    def test_xtream_and_m3u_channels_failing_in_direct_play_are_not_made_again(self):
        for index, ptype in enumerate(("XTREAM", "M3UPLAYLIST")):
            with self.subTest(ptype=ptype):
                self.catalog.ptype = ptype
                self.play(index)
                self.session.errorOccurred.emit("Broken")
                self.assertEqual(self.requested()[-1], CHANNELS[index]["name"])
                self.assertEqual(len(self.catalog.links), index + 1)
                self.assertEqual(
                    self.toast.messages()[-1], (f"{CHANNELS[index]['name']}: Broken", "error")
                )


class StartupTests(ControllerTestCase):
    def test_no_providers_shows_the_welcome_card_until_one_exists(self):
        self.catalog.names = []
        self.controller.start()
        self.assertTrue(self.welcome.visible)
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.drawer.calls, [])
        self.controller.handle(Action.OPEN_DRAWER)
        self.controller.select_provider(force_refresh=True)
        self.assertEqual(self.drawer.calls, [])
        self.assertEqual(self.catalog.selected, [])
        self.assertTrue(self.welcome.visible)
        added = []
        self.welcome.addRequested.connect(lambda: added.append(True))
        self.controller.handle(Action.OK)  # Enter, or OK on a remote: its button
        self.assertEqual(added, [True])
        self.assertEqual(self.drawer.calls, [])
        self.catalog.names = ["Home"]  # saved in Settings
        self.controller.select_provider(force_refresh=True)
        self.assertFalse(self.welcome.visible)
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.controller.handle(Action.OK)  # a provider exists: the drawer, not the card
        self.assertEqual(added, [True])

    def test_the_loading_hint_shows_until_the_first_live_section(self):
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.catalog.hub_ready("Home")
        self.assertEqual(self.toast.hint, "Loading Home…")  # the channels are still on their way
        self.catalog.sectionReady.emit("live")
        self.assertIsNone(self.toast.hint)

    def test_the_loading_hint_names_the_selected_provider(self):
        self.config.selected_provider_name = "Backup"
        self.controller.start()
        self.assertEqual(self.toast.hint, "Loading Backup…")

    def test_the_loading_hint_ends_when_the_live_section_fails(self):
        self.controller.start()
        self.catalog.loaded = False
        self.catalog.hub_ready("Home")
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.toast.hint, "Loading Home…")  # a setup is still running
        self.catalog.sectionFailed.emit("live", "Timed out")
        self.assertIsNone(self.toast.hint)

    def test_a_provider_chosen_in_the_drawer_shows_the_loading_hint(self):
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.nav.open(NavNode("source", "Backup", "Backup", "Backup"))
        self.assertEqual(self.catalog.selected, [None, "Backup"])
        self.assertEqual(self.toast.hint, "Loading Backup…")
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.catalog.sectionReady.emit("live")
        self.assertIsNone(self.toast.hint)

    def test_shutdown_clears_the_hint(self):
        self.controller.start()
        self.controller.shutdown()
        self.assertIsNone(self.toast.hint)

    def test_autotunes_the_last_channel(self):
        self.config.last_live_channel = {
            "provider_name": "Home",
            "group_id": "10",
            "item": CHANNELS[2],
        }
        self.controller.start()
        self.assertEqual(self.catalog.selected, [None])
        self.catalog.hub_ready("Home")
        self.assertEqual(self.catalog.requests[-1], ("section", "live"))
        self.catalog.sectionReady.emit("live")
        self.assertEqual([n.key for n in self.nav.path], ["Home", "live", "10"])
        self.assertEqual(self.requested(), ["Three"])
        self.ready("http://stream/3")
        self.assertEqual(self.played_urls(), ["http://stream/3"])
        self.assertEqual(self.zap.context.group_key, "10")
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def start_with_an_old_address(self, ptype):
        # The stored address holds the provider's old server and password.
        self.catalog.ptype = ptype
        self.config.last_live_channel = {
            "provider_name": "Home",
            "group_id": "10",
            "item": {**CHANNELS[2], "cmd": "http://old.test:8080/live/user/oldpass/3.ts"},
        }
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")

    def test_an_xtream_last_channel_survives_a_change_of_server_or_password(self):
        self.start_with_an_old_address("XTREAM")
        self.assertEqual(self.requested(), ["Three"])  # its stream id still names it

    def test_another_providers_last_channel_is_matched_by_address_alone(self):
        self.start_with_an_old_address("STB")  # an STB channel's id is not its stream
        self.assertEqual(self.requested(), [])

    def test_last_channel_of_another_provider_opens_the_drawer(self):
        self.config.last_live_channel = {
            "provider_name": "Other",
            "group_id": "10",
            "item": CHANNELS[2],
        }
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])
        self.catalog.sectionReady.emit("live")  # later section loads do not reopen it
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)

    def start_with_last_of_harbor(self):
        self.catalog.names = ["Home", "Harbor IPTV"]
        self.config.last_live_channel = {
            "provider_name": "Harbor IPTV",
            "group_id": "41",
            "item": CHANNELS[0],
        }
        hub = self.catalog.catalogs
        hub.own["Harbor IPTV"] = FakeOwnCatalog(
            [HARBOR_ONE, {**CHANNELS[1], "_provider": "Harbor IPTV"}]
        )
        self.controller.start()
        self.catalog.hub_ready("Home")  # the browsed provider is another one
        self.catalog.sectionReady.emit("live")
        return hub

    def test_start_with_last_channel_uses_its_own_provider(self):
        hub = self.start_with_last_of_harbor()
        self.assertEqual(hub.ensured, ["Harbor IPTV"])
        self.assertEqual(self.catalog.links, [])  # its channels are not here yet
        self.assertEqual(self.drawer.named("open_drawer"), [])  # the drawer waits for it
        hub.ready.add("Harbor IPTV")
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(hub.own["Harbor IPTV"].requests, ["live"])
        hub.own["Harbor IPTV"].loaded = True
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(
            [item["_provider"] for _, item, _, _ in self.catalog.links], ["Harbor IPTV"]
        )
        self.assertEqual(self.requested(), ["One"])
        self.assertEqual(self.zap.context.channels, hub.own["Harbor IPTV"].items("live", "*"))

    def test_another_xtream_providers_last_channel_is_found_by_its_stream_id(self):
        hub = self.start_with_last_of_harbor()
        hub.own["Harbor IPTV"].channels[0] = {**HARBOR_ONE, "cmd": "http://new.test/live/u/p/1"}
        hub.ready.add("Harbor IPTV")
        hub.own["Harbor IPTV"].loaded = True
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(self.requested(), ["One"])

    def test_a_last_channel_whose_provider_failed_opens_the_drawer(self):
        hub = self.start_with_last_of_harbor()
        hub.providerFailed.emit("Harbor IPTV", "Handshake failed")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])

    def test_a_last_channel_whose_live_section_fails_opens_the_drawer_with_the_error(self):
        hub = self.start_with_last_of_harbor()
        hub.ready.add("Harbor IPTV")
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(self.drawer.named("open_drawer"), [])
        hub.own["Harbor IPTV"].sectionFailed.emit("live", "Network down")
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])
        self.assertEqual(self.toast.messages(), [("Harbor IPTV: Network down", "error")])
        self.assertEqual(self.catalog.links, [])

    def test_a_rebuilt_providers_live_failure_ends_a_later_wait(self):
        # The flow heard the live list failures of the catalog it first
        # waited on (by name); a provider rebuilt since (an edit in Settings) failed unheard,
        # and the wait ran until its timeout.
        hub = self.start_with_last_of_harbor()
        hub.ready.add("Harbor IPTV")
        hub.contentChanged.emit("Harbor IPTV")
        hub.own["Harbor IPTV"].sectionFailed.emit("live", "Network down")  # gives up
        self.drawer.visible = False
        hub.own["Harbor IPTV"] = FakeOwnCatalog([HARBOR_ONE])  # rebuilt after an edit
        self.controller._flow._start_foreign(("Harbor IPTV", HARBOR_ONE))  # waits again
        self.assertEqual(hub.own["Harbor IPTV"].requests, ["live"])
        hub.own["Harbor IPTV"].sectionFailed.emit("live", "Portal down")
        self.assertEqual(self.toast.messages()[-1], ("Harbor IPTV: Portal down", "error"))
        self.assertEqual(len(self.drawer.named("open_drawer")), 2)
        self.assertIsNone(self.controller._flow._foreign)

    def test_a_last_channel_that_never_arrives_gives_up_and_opens_the_drawer(self):
        self.start_with_last_of_harbor()
        self.controller._flow._foreign_timer.setInterval(30)
        self.controller._flow._foreign_timer.start()
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.assertTrue(wait_until(lambda: len(self.drawer.named("open_drawer")) == 1))
        self.assertEqual(self.catalog.links, [])

    def test_a_last_channel_of_another_provider_waits_for_nothing_else_played(self):
        hub = self.start_with_last_of_harbor()
        self.play(1)  # the viewer chose a channel meanwhile
        hub.ready.add("Harbor IPTV")
        hub.own["Harbor IPTV"].loaded = True
        hub.contentChanged.emit("Harbor IPTV")
        self.assertEqual(self.requested(), ["Two"])

    def test_previous_channel_goes_back_across_providers(self):
        self.controller.start()  # wires the link lanes and the zapping to the catalog
        self.catalog.hub_ready("Home")
        self.controller.tune(CHANNELS[1], group_key="10", channels=CHANNELS)
        self.ready("http://stream/2")
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.controller.handle(Action.PREVIOUS)
        self.assertEqual(self.requested(), ["Two", "One", "Two"])
        self.assertEqual(self.catalog.links[-1][1].get("_provider"), None)
        self.ready("http://stream/2")  # Two is tuned (one tune in flight at a time)
        self.controller.handle(Action.PREVIOUS)  # and forth again
        self.assertEqual(self.catalog.links[-1][1].get("_provider"), "Harbor IPTV")

    def test_provider_failure_toasts_and_opens_the_drawer_on_the_sources(self):
        self.controller.start()
        self.catalog.hub_failed("Home", "Handshake failed")
        self.assertEqual(self.toast.messages(), [("Home: Handshake failed", "error")])
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.drawer.named("open_drawer"), [(None, "left")])
        self.assertEqual([(n.layer, n.key) for n in self.nav.path], [("source", "Home")])

    def test_live_section_failure_opens_the_drawer_with_a_hint(self):
        self.controller.start()
        self.catalog.loaded = False  # nothing saved to fall back on
        self.catalog.hub_ready("Home")
        self.catalog.sectionFailed.emit("live", "Timed out")
        self.assertEqual(self.toast.messages(), [("Live TV: Timed out", "error")])
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])


class KeyTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()

    def test_ch_next_zaps_within_the_context(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested(), ["One", "Two"])
        self.ready("http://stream/2")
        self.clock.now += 5
        self.controller.handle(Action.CH_PREV)
        self.assertEqual(self.requested(), ["One", "Two", "One"])

    def test_burst_opens_the_column(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)
        title, channels, _index = self.column.named("open")[0]
        self.assertEqual((title, channels), ("News", CHANNELS))
        self.assertEqual(self.column.named("highlight")[-1], (2,))
        # the zap rest tunes the highlighted channel (closing the column), queued behind Two
        self.assertTrue(wait_until(lambda: not self.column.visible))
        self.assertEqual(self.requested(), ["One", "Two"])
        self.ready("http://stream/2")
        self.assertEqual(self.requested(), ["One", "Two", "Three"])

    def test_the_column_opens_alone_and_the_rest_tune_announces_its_channel(self):
        # The banner stayed under the column, showing the previous channel.
        self.play(0)
        self.controller.handle(Action.CH_NEXT)  # Two: the banner announces it
        self.assertTrue(self.banner.visible)
        self.controller.handle(Action.CH_NEXT)  # a burst: the column opens on Three
        self.assertTrue(self.column.visible)
        self.assertFalse(self.banner.visible)
        self.ready("http://stream/2")  # Two's link arrives while the column is open
        self.assertFalse(self.banner.visible)
        self.controller.handle(Action.INFO)  # I: the column already names the channel
        self.assertFalse(self.banner.visible)
        self.assertTrue(wait_until(lambda: not self.column.visible))  # the zap rest
        self.assertTrue(self.banner.visible)
        self.assertEqual(self.banner.info.name, "Three")

    def test_the_column_takes_over_from_a_number_being_typed(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.DIGIT, "4")
        self.assertEqual(self.banner.named("show_digits")[-1], ("4",))
        self.controller.handle(Action.CH_NEXT)  # a burst: the column opens
        self.assertTrue(self.column.visible)
        self.assertEqual(self.banner.named("show_digits")[-1], ("",))  # the number is dropped
        self.assertFalse(self.banner.visible)

    def test_a_digit_closes_the_column_for_the_number(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # column open on Three
        self.controller.handle(Action.DIGIT, "4")
        self.assertFalse(self.column.visible)  # the banner shows the number alone
        self.assertEqual(self.banner.named("show_digits")[-1], ("4",))
        self.ready("http://stream/2")
        self.assertTrue(wait_until(lambda: self.requested()[-1] == "Four"))
        self.assertNotIn("Three", self.requested())  # the column's rest tune was dropped

    def test_digits(self):
        self.play(0)
        self.controller.handle(Action.DIGIT, "4")
        self.assertEqual(self.banner.named("show_digits"), [("4",)])
        self.assertTrue(wait_until(lambda: len(self.catalog.links) == 2))
        self.assertEqual(self.requested()[-1], "Four")
        self.controller.handle(Action.DIGIT, "9")
        self.assertTrue(wait_until(lambda: self.toast.messages()))
        self.assertEqual(self.toast.messages(), [("No channel 9", "info")])

    def test_open_drawer_then_escape(self):
        self.play(1)
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual([n.key for n in self.nav.path], ["Home", "live", "10"])
        self.assertEqual(self.drawer.named("open_drawer"), [("2",)])
        self.controller.handle(Action.NAV_DOWN)
        self.controller.handle(Action.NAV_UP)
        self.controller.handle(Action.NAV_TEXT, "ç")
        self.assertEqual(
            self.drawer.calls[1:], [("move_by", 1), ("move_by", -1), ("type_text", "ç")]
        )
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.drawer.calls[-1], ("escape",))
        self.assertFalse(self.drawer.visible)

    def test_escape_order_ends_with_leaving_fullscreen(self):
        self.play(0)  # banner visible
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.banner.calls[-1], ("dismiss",))
        self.assertEqual(self.window.calls, [])
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.window.calls, ["leave_fullscreen"])

    def test_seek_needs_a_seekable_stream(self):
        self.play(0)
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.toast.messages(), [("Rewind arrives with time-shift", "info")])
        self.session.seekable = True
        self.controller.handle(Action.SEEK_FWD)
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.session.relative, [10000, -10000])

    def test_volume_and_mute(self):
        self.controller.handle(Action.VOL_UP)
        self.assertEqual(self.session.level, 55)
        self.controller.handle(Action.MUTE)
        self.assertEqual(self.transport.named("set_muted")[-1], (True,))  # its speaker
        self.controller.handle(Action.MUTE)
        self.assertEqual(self.transport.named("set_muted")[-1], (False,))
        self.transport.volumeRequested.emit()  # the transport's speaker, without a mixer
        self.assertEqual(
            self.toast.messages()[-4:],
            [
                ("Volume 55%", "info"),
                ("Muted", "info"),
                ("Volume 55%", "info"),
                ("Volume 55%", "info"),  # the transport's speaker says it too
            ],
        )

    def test_window_actions(self):
        self.controller.handle(Action.FULLSCREEN)
        self.controller.handle(Action.MINI_PLAYER)
        self.controller.video_double_clicked()
        self.assertEqual(
            self.window.calls, ["toggle_fullscreen", "toggle_mini_player", "toggle_fullscreen"]
        )
        self.controller.handle(Action.GO_LIVE)  # nothing to do without a time-shifted channel
        self.assertEqual(self.toast.messages(), [])

    def test_click_toggles_the_drawer(self):
        self.controller.video_clicked()
        self.assertTrue(self.drawer.visible)
        self.controller.video_clicked()
        self.assertEqual(self.drawer.calls[-1], ("close_drawer",))

    def test_info_shows_the_banner_with_epg(self):
        programmes = [
            Programme("News", NOW - timedelta(minutes=30), NOW + timedelta(minutes=30)),
            Programme("Film", NOW + timedelta(minutes=30), NOW + timedelta(hours=2)),
        ]
        self.catalog.epg[2] = programmes
        self.play(1)
        self.banner.dismiss()
        self.controller.handle(Action.INFO)
        info = self.banner.named("show_info")[-1][0]
        self.assertEqual((info.number, info.name, info.is_live), ("2", "Two", True))
        unnumbered = [{"id": "a", "name": "A"}, {"id": "b", "name": "B", "number": None}]
        self.controller.tune(unnumbered[1], group_key="x", channels=unnumbered)
        self.assertEqual(self.banner.named("show_info")[-1][0].number, "2")
        self.assertEqual((info.now, info.next, info.clock), (programmes[0], programmes[1], NOW))
        self.assertEqual(self.controller.now_title(CHANNELS[1]), ("News", 0.5))
        self.assertIsNone(self.controller.now_title(CHANNELS[0]))

    def test_mouse_move_shows_the_live_transport_while_playing(self):
        before = list(self.transport.calls)  # the speaker's remembered level, from the start
        self.controller.mouse_moved()
        self.assertEqual(self.transport.calls, before)
        self.play(0)
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("show_live"), [("One", "")])
        self.controller.mouse_moved()
        self.assertEqual(self.transport.calls[-1], ("poke",))

    def test_the_pointer_may_hide_only_over_a_playing_picture_with_nothing_open(self):
        self.assertFalse(self.controller.pointer_may_hide())  # nothing plays
        self.play(0)
        self.assertTrue(self.controller.pointer_may_hide())
        for overlay in (self.drawer, self.column, self.guide, self.next_card, self.welcome):
            overlay.visible = True
            self.assertFalse(self.controller.pointer_may_hide(), type(overlay).__name__)
            overlay.visible = False
        self.assertTrue(self.controller.pointer_may_hide())


class VodTests(ControllerTestCase):
    MOVIE = {"id": "m7", "name": "Heat", "cmd": "http://vod/heat.mkv"}

    def setUp(self):
        super().setUp()
        self.controller.start()

    def play_movie(self):
        self.controller.play_vod(self.MOVIE, "movie", play_context(genre_items=[self.MOVIE]))

    def test_resumes_and_tracks_position(self):
        self.watch.resume["Home:movie:m7"] = 125_000
        self.play_movie()
        self.assertEqual(self.catalog.links[0][2:], ("movies", False))
        self.catalog.linkReady.emit(1, "http://vod/heat.mkv")
        url, kwargs = self.session.plays[0]
        self.assertEqual(kwargs, {"kind": PlayKind.VOD_FILE, "start_ms": 125_000})
        self.assertEqual(self.toast.messages(), [("Resumed at 2:05", "info")])
        self.assertEqual(self.watch.plays, [("Home", self.MOVIE, "movie", url, None)])
        self.assertEqual(self.transport.named("show_vod"), [("Heat", "")])
        self.session.positionChanged.emit(130_000, 6_000_000)
        self.assertEqual(self.watch.positions, [("Home:movie:m7", 130_000, 6_000_000)])
        self.assertEqual(self.transport.calls[-1], ("update_position", 130_000, 6_000_000))
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("Finished", "info"))
        self.assertEqual(self.watch.flushes, 1)

    def test_episode_requests_a_series_link(self):
        episode = {"id": "s", "cmd": "x", "series": 2}
        self.controller.play_vod(episode, "episode", play_context())
        self.assertEqual(self.catalog.links[0][2:], ("series", True))

    def test_channel_keys_change_the_volume_during_vod(self):
        self.play_movie()
        self.catalog.linkReady.emit(1, "http://vod/heat.mkv")
        self.controller.handle(Action.CH_PREV)
        self.assertEqual(self.session.level, 55)
        self.assertEqual(len(self.catalog.links), 1)


class GuidesInUseTests(ControllerTestCase):
    """Only the guides of the providers on screen or playing load (QiTV 2.1)."""

    def test_guides_in_use_follow_the_picture_and_the_browsed_provider(self):
        self.controller.start()
        self.assertEqual(self.controller.guides_in_use(), {"Home"})
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.assertEqual(self.controller.guides_in_use(), {"Home", "Harbor IPTV"})
        self.assertEqual(self.catalog.guides_used[-1], {"Home", "Harbor IPTV"})

    def test_the_hub_is_told_only_when_the_set_changes(self):
        self.controller.start()
        self.nav.changed.emit()
        self.nav.changed.emit()
        self.assertEqual(self.catalog.guides_used, [{"Home"}])

    def test_all_mode_loads_the_providers_of_the_drawers_group(self):
        self.controller.start()
        self.catalog.provider = ALL_PROVIDERS
        source = NavNode("source", ALL_PROVIDERS, "All", ALL_PROVIDERS)
        section = NavNode("section", "live", "Live", "live")
        group = NavNode("group", mixed_group_id("Portal B", "7"), "Sports", "x")
        self.nav._path = [source, section, group]
        self.assertEqual(self.controller.guides_in_use(), set())  # the drawer is closed
        self.drawer.visible = True
        self.assertEqual(self.controller.guides_in_use(), {"Portal B"})

    def test_opening_the_drawer_on_a_mixed_group_loads_its_providers_and_closing_releases(self):
        self.controller.start()
        self.catalog.provider = ALL_PROVIDERS
        self.catalog.live["41"] = [HARBOR_ONE, PORTAL_ONE]
        source = NavNode("source", ALL_PROVIDERS, "All", ALL_PROVIDERS)
        section = NavNode("section", "live", "Live", "live")
        self.nav._path = [source, section, NavNode("group", "41", "Mixed", "41")]
        with mock.patch.object(self.nav, "reset_to"):  # the path above stays
            self.controller.handle(Action.OPEN_DRAWER)
        self.assertTrue(self.drawer.visible)
        self.assertEqual(self.catalog.guides_used[-1], {"Harbor IPTV", "Portal B"})
        self.drawer.visible = False
        self.drawer.closed.emit()
        self.assertEqual(self.catalog.guides_used[-1], set())

    def test_all_mode_guide_lists_the_drawers_group(self):
        self.controller.start()
        self.catalog.provider = ALL_PROVIDERS
        self.catalog.live["41"] = [HARBOR_ONE, PORTAL_ONE]
        source = NavNode("source", ALL_PROVIDERS, "All", ALL_PROVIDERS)
        section = NavNode("section", "live", "Live", "live")
        self.nav._path = [source, section, NavNode("group", "41", "Mixed", "41")]
        self.controller.handle(Action.GUIDE)
        self.assertEqual(self.guide.named("open_guide"), [([HARBOR_ONE, PORTAL_ONE], 0)])

    def test_all_mode_loads_the_providers_of_the_guides_rows(self):
        self.controller.start()
        self.catalog.provider = ALL_PROVIDERS
        self.guide.visible = True
        self.guide.visible_rows = [HARBOR_ONE, PORTAL_ONE]
        self.assertEqual(self.controller.guides_in_use(), {"Harbor IPTV", "Portal B"})
        self.guide.visible_rows = [PORTAL_ONE]
        self.guide.rowsMoved.emit()
        self.assertEqual(self.catalog.guides_used[-1], {"Portal B"})


class DrawerPlayTests(ControllerTestCase):
    def test_live_item_tunes_the_whole_group_and_closes(self):
        self.controller.start()
        self.nav.reset_to("live", "10")
        group = self.nav.path[-1]
        nodes = self.nav.children(group)
        self.drawer.visible = True
        filtered = [nodes[2]]  # the drawer emits only the rows its filter shows
        self.drawer.playRequested.emit(nodes[2], filtered, group)
        self.assertEqual(self.requested(), ["Three"])
        self.assertEqual(self.zap.context.channels, CHANNELS)
        self.assertEqual(self.drawer.calls[-1], ("close_drawer",))

    def test_a_channel_the_find_found_plays_in_its_genre_and_the_list_closes(self):
        self.catalog.live["20"] = [{"id": 9, "name": "Nine", "cmd": "ffrt http://tv/9"}]
        self.controller.start()
        self.drawer.visible = True
        self.drawer.channelFound.emit({**CHANNELS[1], "_provider": "Home"}, "10")
        self.assertEqual(self.requested(), ["Two"])
        self.assertEqual(self.zap.context.group_key, "10")  # ↑/↓ walk its genre, as in 2.0
        self.assertEqual(
            [c["name"] for c in self.zap.context.channels], ["One", "Two", "Three", "Four"]
        )
        self.assertEqual(self.drawer.calls[-1], ("close_drawer",))

    def test_a_found_channel_the_list_cannot_show_in_its_genre_walks_every_channel(self):
        self.catalog.live["20"] = [{"id": 9, "name": "Nine", "cmd": "ffrt http://tv/9"}]
        self.controller.start()
        for channel, group in (
            (BACKUP_TWO, "10"),  # another provider's, the list on Home
            ({**CHANNELS[1], "_provider": "Home"}, "20"),  # a genre that does not list it
            ({**CHANNELS[1], "_provider": "Home"}, ""),  # in no genre
        ):
            self.drawer.visible = True
            self.drawer.channelFound.emit(channel, group)
            self.assertEqual(self.zap.context.group_key, "*", (channel, group))
            self.assertEqual(len(self.zap.context.channels), 5)

    def test_the_drawer_plays_only_channels(self):
        # Movies and Series open the Library from the drawer: their titles and episodes
        # play from there (play_vod), never from a drawer row.
        self.controller.start()
        self.drawer.visible = True
        movie_row = NavNode("item", "m7", "Heat", VodTests.MOVIE)
        episode_row = NavNode("episode", "s:1:2", "Pilot", {"id": "s", "cmd": "x", "series": 2})
        self.nav.reset_to("movies")
        self.controller.play_item(movie_row, [movie_row], NavNode("group", "5", "Films"))
        self.controller.play_item(episode_row, [episode_row], NavNode("season", "1", "S1"))
        self.assertEqual(self.catalog.links, [])
        self.assertTrue(self.drawer.visible)


class TransportAndWheelTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()

    def test_transport_requests_reach_the_session(self):
        self.play(0)
        self.transport.playPauseRequested.emit()
        self.transport.seekRequested.emit(42_000)
        self.transport.seekRelativeRequested.emit(-10_000)
        self.transport.speedRequested.emit(1.5)
        self.transport.fullscreenRequested.emit()
        self.assertEqual(self.session.toggles, 1)
        self.assertEqual(self.session.seeks, [42_000])
        self.assertEqual(self.session.relative, [-10_000])
        self.assertEqual(self.session.rates, [1.0, 1.5])  # each play starts at 1x
        self.assertEqual(self.window.calls, ["toggle_fullscreen"])
        self.session.stateChanged.emit("paused")
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.transport.named("set_paused"), [(True,), (False,)])

    def test_tracks_cycle_and_toast(self):
        self.play(0)
        self.transport.audioRequested.emit()
        self.transport.audioRequested.emit()
        self.transport.subtitleRequested.emit()
        self.transport.subtitleRequested.emit()
        self.assertEqual(self.session.audio_set, [2, 1])
        self.assertEqual(self.session.subtitle_set, [3, -1])
        self.assertEqual(
            self.toast.messages(),
            [
                ("Audio: Deutsch", "info"),
                ("Audio: English", "info"),
                ("Subtitles: Français", "info"),
                ("Subtitles: Off", "info"),
            ],
        )

    def test_mouse_back_and_forward_open_the_drawer_then_change_its_layer(self):
        self.controller.mouse_back()
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)  # closed: it opens
        self.controller.mouse_back()
        self.controller.mouse_forward()
        self.assertEqual(self.drawer.named("left"), [()])
        self.assertEqual(self.drawer.named("right"), [()])
        self.drawer.close_drawer()
        self.controller.mouse_forward()
        self.assertEqual(len(self.drawer.named("open_drawer")), 2)
        self.assertEqual(self.drawer.named("right"), [()])

    def test_wheel_with_the_volume_setting_changes_volume(self):
        self.play(0)
        self.config.mouse_wheel_action = "volume"
        self.controller.wheel(-2, NO_MODS)  # two notches up
        self.assertEqual(self.session.level, 60)
        self.assertEqual(self.toast.messages()[-1], ("Volume 60%", "info"))
        self.assertEqual(self.requested(), ["One"])

    def test_ctrl_wheel_is_volume_and_plain_wheel_zaps(self):
        self.play(0)
        self.controller.wheel(1, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.session.level, 45)
        self.controller.wheel(1, NO_MODS)
        self.assertEqual(self.requested(), ["One", "Two"])


class ColumnWheelTests(ControllerTestCase):
    def test_wheel_over_the_open_column_zaps(self):
        host = QWidget()
        self.addCleanup(host.close)
        host.resize(1280, 720)
        column = ChannelColumn(None, lambda channel: None, parent=host)
        self.controller = self.make_controller(column)
        self.controller.start()
        host.show()
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: the column opens on Three
        self.assertTrue(column.isVisible())
        self.assertEqual(column.current_index(), 2)
        viewport = column.findChild(QAbstractItemView).viewport()
        event = QWheelEvent(
            QPointF(20, 20),
            QPointF(viewport.mapToGlobal(QPoint(20, 20))),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            NO_MODS,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(viewport, event)
        self.assertEqual(column.current_index(), 3)  # one notch down: the next channel
        self.assertTrue(column.isVisible())
        self.assertTrue(wait_until(lambda: not column.isVisible()))  # the rest tuned Four
        self.ready("http://stream/2")  # Two settles; the queued Four is requested instead
        self.assertEqual(self.requested(), ["One", "Two", "Four"])


class RealOverlayTests(ControllerTestCase):
    """The controller against the real overlays: catches API drift the fakes would hide."""

    def test_start_browse_tune_and_escape(self):
        host = QWidget()
        self.addCleanup(host.close)
        host.resize(1280, 720)

        def now_provider(channel):
            return controller.now_title(channel)

        overlays = Overlays(
            ZapBanner(host),
            ChannelColumn(None, now_provider, parent=host),
            Drawer(self.nav, None, now_provider, parent=host),
            Transport(host),
            Toast(host),
            Guide(None, lambda channel, start, limit: [], lambda: NOW, parent=host),
            NextCard(host),
            WelcomeCard(host),
        )
        controller = TVController(
            config=self.config,
            catalog=self.catalog,
            nav=self.nav,
            zap=self.zap,
            session=self.session,
            watch=self.watch,
            overlays=overlays,
            window_actions=self.window,
            external=self.external,
            now=lambda: NOW,
        )
        self.addCleanup(controller.shutdown)
        host.show()
        controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertTrue(overlays.drawer.isVisible())
        self.assertEqual(overlays.drawer.highlighted().title, "News")  # at Section | Group
        controller.handle(Action.NAV_RIGHT)
        controller.handle(Action.NAV_DOWN)
        self.assertEqual(overlays.drawer.highlighted().title, "Two")
        controller.handle(Action.NAV_ACTIVATE)
        self.assertFalse(overlays.drawer.isVisible())
        self.assertTrue(overlays.banner.isVisible())
        self.assertEqual(overlays.banner.info.name, "Two")
        self.ready("http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        controller.mouse_moved()
        self.assertTrue(overlays.transport.isVisible())
        controller.handle(Action.ESCAPE)
        self.assertFalse(overlays.banner.isVisible())
        controller.handle(Action.OPEN_DRAWER)  # at Group | Item, on the tuned channel
        self.assertTrue(overlays.drawer.isVisible())
        self.assertEqual([n.key for n in self.nav.path], ["Home", "live", "10"])
        self.assertEqual(overlays.drawer.highlighted().title, "Two")
        controller.handle(Action.NAV_TEXT, "x")
        controller.handle(Action.ESCAPE)  # clears the filter first
        self.assertTrue(overlays.drawer.isVisible())
        controller.handle(Action.ESCAPE)
        self.assertFalse(overlays.drawer.isVisible())


class DebugLogRedactionTests(ControllerTestCase):
    """DEBUG logs never carry raw provider text: it can hold Xtream credentials."""

    SECRET = "http://h:80/get.php?username=bob&password=hunter2"

    def test_errors_and_failures_log_no_raw_text(self):
        with self.assertLogs("ui.tv_controller", "DEBUG") as logs:
            self.controller._error(f"One: refused at {self.SECRET}")
            try:
                raise RuntimeError(f"boom {self.SECRET}")
            except RuntimeError as e:
                self.controller._log_failure("play One", e)
        text = "\n".join(logs.output)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("username=bob", text)
        self.assertIn("RuntimeError", text)  # the type and the frames stay
        self.assertIn("Traceback", text)


class ProviderRetryTests(ControllerTestCase):
    """A provider that fails to set up can be retried from the drawer (real overlays)."""

    def setUp(self):
        super().setUp()
        self.host = QWidget()
        self.addCleanup(self.host.close)
        self.host.resize(1280, 720)
        self.real_drawer = Drawer(self.nav, None, lambda channel: None, parent=self.host)
        self.real_toast = Toast(self.host)
        self.drawer, self.toast = self.real_drawer, self.real_toast
        self.controller = self.make_controller(self.column)
        self.host.show()

        def select(name=None, **kwargs):
            self.catalog.selected.append(name)
            self.catalog.loading = True  # as CatalogService: the setup runs until it settles

        self.catalog.select_provider = select
        self.controller.start()

    def fail_setup(self):
        self.catalog.loading = False
        self.catalog.hub_failed("Home", "Couldn't reach the provider")

    def test_the_drawer_opens_on_the_failed_source_with_a_retry_row(self):
        self.fail_setup()
        drawer = self.real_drawer
        self.assertTrue(drawer.isVisible())
        self.assertEqual(drawer.focus_side, "left")
        self.assertEqual((drawer.highlighted().layer, drawer.highlighted().key), ("source", "Home"))
        self.assertEqual(drawer.message_for("right"), PROVIDER_ERROR_TEXT)
        self.assertEqual(self.real_toast.text(), "Home: Couldn't reach the provider")

    def test_enter_on_the_failed_source_sets_it_up_again(self):
        self.fail_setup()
        self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, [None, "Home"])
        self.assertEqual(self.real_drawer.message_for("right"), LOADING_TEXT)
        self.assertEqual(self.real_toast.text(), "Loading Home…")
        self.catalog.loading = False
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.real_drawer.highlighted().title, "News")

    def test_enter_on_the_retry_row_sets_it_up_again(self):
        self.fail_setup()
        self.controller.handle(Action.NAV_RIGHT)  # onto the row
        self.assertEqual(self.real_drawer.focus_side, "right")
        self.controller.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.catalog.selected, [None, "Home"])


class UnnumberedChannelTests(ControllerTestCase):
    """An M3U without numbers: the banner, the column and digits use the list position."""

    def test_the_banner_column_and_digits_agree_on_positions(self):
        host = QWidget()
        self.addCleanup(host.close)
        host.resize(1280, 720)
        column = ChannelColumn(None, lambda channel: None, parent=host)
        self.controller = self.make_controller(column)
        self.controller.start()
        host.show()
        plain = [{"id": c["id"], "name": c["name"], "cmd": c["cmd"]} for c in CHANNELS]
        self.controller.tune(plain[2], group_key="10", channels=plain)
        self.assertEqual(self.banner.info.number, "3")
        self.ready()
        self.controller.handle(Action.DIGIT, "2")
        self.assertTrue(wait_until(lambda: self.requested()[-1] == "Two"))
        self.ready()
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # a burst: the column opens
        self.assertTrue(column.isVisible())
        rows = column.findChild(QAbstractItemView).model()
        shown = [rows.index(r).data(ChannelRowsModel.NumberRole) for r in range(4)]
        self.assertEqual(shown, ["1", "2", "3", "4"])


class TuningBannerTests(ControllerTestCase):
    """The banner stays up while a slow link resolves."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("ui.tv_controller.TUNING_KEEP_MS", 40)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = QWidget()
        self.addCleanup(self.host.close)
        self.host.resize(1280, 720)
        self.banner = ZapBanner(self.host, visible_ms=100)
        self.controller = self.make_controller(self.column)
        self.host.show()
        self.controller.start()

    def test_the_banner_stays_until_the_channel_plays_then_fades(self):
        self.tune(0)
        elapse(800)  # well past the banner's own time and its fade
        self.assertTrue(self.banner.isVisible())
        self.assertEqual(self.banner.info.name, "One")
        self.ready()  # the link is in, but the stream still opens (a slow start)
        elapse(800)
        self.assertTrue(self.banner.isVisible())
        self.session.stateChanged.emit("playing")
        self.assertTrue(wait_until(lambda: not self.banner.isVisible()))

    def test_the_keep_alive_gives_up_after_its_cap(self):
        with mock.patch("ui.tv_controller.TUNING_CAP_S", 0.3):
            self.tune(0)
            self.ready()  # opens and never plays
            self.assertTrue(wait_until(lambda: not self.banner.isVisible(), 3000))

    def test_the_banner_stays_until_the_link_fails(self):
        self.tune(0)
        elapse(800)  # well past the banner's own time and its fade
        self.assertTrue(self.banner.isVisible())
        self.catalog.linkFailed.emit(1, "Portal down")
        self.assertTrue(wait_until(lambda: not self.banner.isVisible()))

    def test_a_dismissed_banner_is_not_brought_back(self):
        self.tune(0)
        self.controller.handle(Action.ESCAPE)
        elapse(200)
        self.assertFalse(self.banner.isVisible())


class BottomOverlayTests(ControllerTestCase):
    """The banner and the full-host transport share the bottom: only one shows at a time."""

    def setUp(self):
        super().setUp()
        self.host = QWidget()
        self.addCleanup(self.host.close)
        self.host.resize(1280, 720)
        self.banner, self.transport = ZapBanner(self.host), Transport(self.host)
        self.controller = self.make_controller(self.column)
        self.host.show()
        self.controller.start()

    def test_the_transport_replaces_the_banner_and_a_zap_replaces_the_transport(self):
        self.play(0)
        self.assertTrue(self.banner.isVisible())
        self.controller.mouse_moved()
        self.assertTrue(self.transport.isVisible())
        self.assertEqual(self.transport.geometry(), self.host.rect())
        self.assertFalse(self.banner.isVisible())
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)  # a zap announces the new channel instead
        self.assertTrue(self.banner.isVisible())
        self.assertFalse(self.transport.isVisible())

    def test_a_vod_start_replaces_the_banner_with_the_transport(self):
        self.play(0)
        self.assertTrue(self.banner.isVisible())  # the live tune's banner
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertTrue(self.transport.isVisible())
        self.assertFalse(self.banner.isVisible())

    def test_a_media_key_zap_keeps_the_transport(self):
        self.play(0)
        self.clock.now += 5
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertTrue(self.transport.isVisible())
        self.assertFalse(self.banner.isVisible())

    def test_number_entry_keeps_the_banner(self):
        self.play(0)
        self.controller.mouse_moved()
        self.assertTrue(self.transport.isVisible())
        self.controller.handle(Action.DIGIT, "2")
        self.assertTrue(self.banner.isVisible())
        self.assertFalse(self.transport.isVisible())
        self.controller.mouse_moved()
        self.assertTrue(self.banner.isVisible())
        self.assertFalse(self.transport.isVisible())


class TuneFailureAndKeyTests(ControllerTestCase):
    """Tunes, failures and keys: what the first controller got wrong."""

    LEAK = "Error: http://h/player_api.php?username=u&password=p failed"

    def setUp(self):
        super().setUp()
        self.controller.start()

    def assert_no_leak(self, logs):
        for text, _kind in self.toast.messages():
            for secret in ("player_api", "password", "http://"):
                self.assertNotIn(secret, text)
        for record in logs.records:
            if record.levelno >= logging.INFO:
                self.assertNotIn("password", record.getMessage())
                self.assertNotIn("player_api", record.getMessage())

    def test_failure_text_never_leaks_addresses(self):
        with self.assertLogs(level="DEBUG") as logs:
            self.tune(0)
            self.catalog.linkFailed.emit(1, self.LEAK)
            self.catalog.loaded = False  # no saved groups: the live failure is an error
            self.catalog.hub_ready("Home")
            self.catalog.sectionFailed.emit("live", self.LEAK)
        self.assertEqual(
            self.toast.messages(),
            [("One: Error: … failed", "error"), ("Live TV: Error: … failed", "error")],
        )
        self.assert_no_leak(logs)
        debug = [r.getMessage() for r in logs.records if r.levelno == logging.DEBUG]
        self.assertFalse(any("password=p" in m for m in debug))  # not even at DEBUG

    def test_drawer_pick_in_flight_then_down_moves_on_from_it(self):
        self.play(0)
        self.tune(2)  # a drawer pick of Three, its link in flight
        self.controller.handle(Action.CH_NEXT)
        self.catalog.linkReady.emit(2, "http://stream/3")
        self.assertEqual(self.requested(), ["One", "Three", "Four"])

    def test_autotune_in_flight_then_down_moves_on_from_it(self):
        self.config.last_live_channel = {
            "provider_name": "Home",
            "group_id": "10",
            "item": CHANNELS[2],
        }
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")  # autotunes Three
        self.controller.handle(Action.CH_NEXT)
        self.catalog.linkReady.emit(1, "http://stream/3")
        self.assertEqual(self.requested(), ["Three", "Four"])

    def test_another_provider_in_the_list_keeps_zapping(self):
        self.catalog.hub_ready("Home")
        self.play(0)
        self.play(1)  # previous channel: One
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.controller.handle(Action.PREVIOUS)
        self.ready("http://stream/1")  # One is tuned: the next step starts from it
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested(), ["One", "Two", "One", "Two"])  # Home's list walks on

    def test_link_failure_while_loading_waits_for_the_provider(self):
        self.catalog.hub_ready("Home")
        self.tune(0)
        self.catalog.loading = True
        self.catalog.linkFailed.emit(1, PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One"])
        self.catalog.loading = False
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_live_section_still_loading_is_ignored(self):
        self.catalog.loaded = False
        self.catalog.hub_ready("Home")
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.catalog.loaded = True
        self.catalog.sectionReady.emit("live")  # still waiting for the live section
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])

    def test_saved_catalog_failure_still_autotunes_and_notices(self):
        self.config.last_live_channel = {
            "provider_name": "Home",
            "group_id": "10",
            "item": CHANNELS[2],
        }
        self.catalog.hub_ready("Home")
        self.catalog.sectionFailed.emit("live", UNREACHABLE_MESSAGE)
        self.assertEqual(self.requested(), ["Three"])
        self.assertEqual(
            self.toast.messages(), [("Couldn't refresh Live TV; showing saved channels", "info")]
        )

    def test_epg_ready_repaints_but_never_asks_for_the_guide_again(self):
        self.catalog.epgReady.emit()  # a loaded guide repaints; asking again would loop
        self.assertEqual(self.catalog.epg_refreshes, 0)
        self.assertEqual(self.column.named("refresh_now"), [()])

    def test_numbers_reach_channels_outside_the_group(self):
        self.catalog.live = {"10": CHANNELS[:2], "20": CHANNELS[2:]}
        self.controller.tune(CHANNELS[0], group_key="10", channels=CHANNELS[:2])
        self.ready("http://stream/1")
        self.controller.handle(Action.DIGIT, "4")
        self.assertTrue(wait_until(lambda: len(self.catalog.links) == 2))
        self.assertEqual(self.requested(), ["One", "Four"])

    def test_same_item_queued_keeps_the_link_in_flight(self):
        self.tune(0)
        self.tune(1)
        self.tune(0)  # back to One before its link resolved
        self.catalog.linkReady.emit(1, "http://stream/1")
        self.assertEqual(self.requested(), ["One"])
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_failed_tune_restores_the_playing_channel(self):
        self.play(0)
        self.tune(2)
        self.catalog.linkFailed.emit(2, UNREACHABLE_MESSAGE)
        self.banner.dismiss()
        self.controller.handle(Action.INFO)
        self.assertEqual(self.banner.info.name, "One")

    def test_escape_cancels_number_entry(self):
        self.play(0)
        self.controller.handle(Action.DIGIT, "4")
        self.controller.handle(Action.ESCAPE)
        self.assertEqual(self.banner.named("show_digits")[-1], ("",))
        self.assertEqual(self.banner.calls[-1], ("dismiss",))
        elapse(80)  # past the digit commit
        self.assertEqual(self.requested(), ["One"])
        self.assertEqual(self.window.calls, [])

    def test_escape_with_the_column_open_cancels_the_rest_tune(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: column on Three
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.column.visible)
        self.ready("http://stream/2")
        elapse(80)  # past the zap rest
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_enter_with_the_column_open_tunes_the_highlight(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: column on Three
        self.controller.handle(Action.OK)
        self.assertFalse(self.column.visible)
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.ready("http://stream/2")  # Two settles; the queued Three is requested
        elapse(80)
        self.assertEqual(self.requested(), ["One", "Two", "Three"])

    def test_media_keys(self):
        self.play(0)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertEqual(self.transport.named("show_live"), [("One", "")])
        self.ready("http://stream/2")
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.session.seekable = True
        self.controller.handle(Action.MEDIA_PREV)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.session.relative, [-10_000, 10_000])
        self.assertEqual(len(self.catalog.links), 3)

    def test_live_link_in_flight_then_vod_plays_only_the_vod(self):
        self.tune(0)
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.catalog.linkReady.emit(1, "http://stream/1")
        self.assertEqual(self.requested(), ["One", "Heat"])
        self.catalog.linkReady.emit(2, "http://vod/heat.mkv")
        self.assertEqual(self.played_urls(), ["http://vod/heat.mkv"])

    def test_external_vod_stops_the_in_app_session(self):
        self.play(0)
        self.config.play_in_vlc = True
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.external.calls, [("vlc", "http://vod/heat.mkv")])
        self.assertEqual(self.session.stops, 1)

    def test_every_play_starts_at_normal_speed(self):
        self.play(0)
        self.assertEqual(self.session.rates, [1.0])
        self.assertEqual(self.transport.named("set_speed"), [(1.0,)])

    def test_closed_controller_ignores_catalog_signals(self):
        self.controller.shutdown()
        self.toast.calls.clear()  # the startup hint, cleared by the shutdown
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")
        self.catalog.sectionFailed.emit("live", UNREACHABLE_MESSAGE)
        self.catalog.hub_failed("Home", UNREACHABLE_MESSAGE)
        self.catalog.epgReady.emit()
        self.zap.digit("9")  # zap timers may still fire while the window is torn down
        elapse(80)  # past the digit commit: "No channel 9" must not be toasted
        self.assertEqual(self.catalog.requests, [])
        self.assertEqual(self.toast.calls, [])
        self.assertEqual(self.banner.calls, [])
        self.assertEqual(self.drawer.calls, [])
        self.assertEqual(self.column.calls, [])


class LoadingQueueAndDrawerTests(ControllerTestCase):
    """The loading queue, OK versus the drawer, re-choosing the playing channel."""

    def setUp(self):
        super().setUp()
        self.controller.start()
        self.catalog.hub_ready("Home")

    def test_two_loading_failures_keep_the_newest_choice(self):
        self.play(0)
        self.catalog.loading = True
        self.tune(1)
        self.catalog.linkFailed.emit(2, PROVIDER_LOADING_MESSAGE)
        self.tune(2)  # the newest choice, sent while Two waits for the provider
        self.catalog.linkFailed.emit(3, PROVIDER_LOADING_MESSAGE)
        self.catalog.loading = False
        self.catalog.hub_ready("Home")
        self.assertEqual(self.requested(), ["One", "Two", "Three", "Three"])
        self.ready("http://stream/3")
        self.assertEqual(self.played_urls(), ["http://stream/1", "http://stream/3"])

    def test_a_direct_request_clears_a_stale_loading_queue(self):
        self.play(0)
        self.catalog.loading = True
        self.tune(1)
        self.catalog.linkFailed.emit(2, PROVIDER_LOADING_MESSAGE)
        self.catalog.loading = False
        self.tune(2)
        self.ready("http://stream/3")
        self.assertEqual(self.requested(), ["One", "Two", "Three"])
        self.assertEqual(self.played_urls(), ["http://stream/1", "http://stream/3"])

    def burst_to_column(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: the column opens on Three
        self.assertTrue(self.column.visible)

    def test_video_click_with_the_column_open_opens_the_drawer(self):
        self.burst_to_column()
        self.controller.video_clicked()
        self.assertFalse(self.column.visible)
        self.assertEqual(self.drawer.named("open_drawer"), [("2",)])
        self.ready("http://stream/2")
        elapse(80)  # the column's highlight is not tuned
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_channel_list_with_the_column_open_opens_the_drawer(self):
        self.burst_to_column()
        self.controller.handle(Action.OPEN_DRAWER)  # the menu's "Channel list"
        self.assertFalse(self.column.visible)
        self.assertTrue(self.drawer.visible)
        self.ready("http://stream/2")
        elapse(80)
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_ok_opens_the_drawer_without_the_column(self):
        self.play(0)
        self.controller.handle(Action.OK)
        self.assertEqual(self.drawer.named("open_drawer"), [("1",)])

    def test_media_next_does_nothing_with_the_drawer_open(self):
        self.play(0)
        self.drawer.open_drawer()
        self.controller.handle(Action.MEDIA_NEXT)
        self.controller.handle(Action.MEDIA_NEXT)
        self.controller.handle(Action.PLAY_PAUSE)  # play/pause still works
        self.assertEqual(self.requested(), ["One"])
        self.assertFalse(self.column.visible)
        self.assertEqual(self.session.toggles, 1)

    def test_ok_on_the_playing_row_does_not_restart_it(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.ready("http://stream/2")
        self.controller.handle(Action.CH_PREV)  # burst: column on One
        self.controller.handle(Action.CH_NEXT)  # column on Two, which is playing
        self.controller.handle(Action.OK)
        elapse(60)
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertFalse(self.column.visible)
        self.assertEqual(self.banner.info.name, "Two")

    def test_row_click_on_the_playing_row_does_not_restart_it(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.ready("http://stream/2")
        self.controller.handle(Action.CH_PREV)  # burst: the column opens
        self.column.rowClicked.emit(1)  # Two, which is playing
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertFalse(self.column.visible)

    def test_choosing_the_playing_channel_drops_a_pending_choice(self):
        self.play(0)
        self.tune(1)  # Two in flight
        self.tune(0)  # back to One, which is still playing: stay
        self.tune(0)
        self.catalog.linkReady.emit(2, "http://stream/2")
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertEqual(self.played_urls(), ["http://stream/1"])
        self.controller.handle(Action.CH_NEXT)  # the zap moves on from One
        self.assertEqual(self.requested(), ["One", "Two", "Two"])

    def test_scrub_keeps_ordinary_question_marks(self):
        self.assertEqual(scrub("Who?Me: http://h/a?b=c failed"), "Who?Me: … failed")
        self.assertEqual(scrub("player_api.php?username=u&password=p"), "…")
        self.assertEqual(scrub("Why? Because"), "Why? Because")

    def test_external_failure_restores_the_playing_channel(self):
        self.play(0)
        self.config.play_in_vlc = True
        self.external.result = (False, "VLC Media Player is not installed.")
        self.play(2)
        self.banner.dismiss()
        self.controller.handle(Action.INFO)
        self.assertEqual(self.banner.info.name, "One")

    def test_play_exception_restores_the_playing_channel(self):
        self.play(0)

        def broken(url, **kwargs):
            raise RuntimeError(f"cannot open {url}")

        self.session.play = broken
        with self.assertLogs("ui.tv_controller", "WARNING") as logs:
            self.play(2)
        self.assertEqual(
            logs.output,
            ["WARNING:ui.tv_controller:Could not play Three: cannot open … (RuntimeError)"],
        )
        self.assertEqual(self.toast.messages()[-1], ("Three: cannot open …", "error"))
        self.assertEqual([p[1]["name"] for p in self.watch.plays], ["One"])  # not recorded
        # One was stopped to open Three: nothing is current, nothing claims to play.
        self.assertIsNone(self.controller._current)
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("show_live"), [])
        self.banner.dismiss()
        self.controller.handle(Action.INFO)  # the zap still starts from One
        self.assertEqual(self.banner.info.name, "One")


class LogoTests(ControllerTestCase):
    ONE, TWO = "http://logo/1.png", "http://logo/2.png"

    def with_logos(self):
        logos = FakeLogos()
        self.controller = TVController(
            config=self.config,
            catalog=self.catalog,
            nav=self.nav,
            zap=self.zap,
            session=self.session,
            watch=self.watch,
            overlays=self.overlays(self.column),
            window_actions=self.window,
            external=self.external,
            now=lambda: NOW,
            logos=logos,
        )
        self.addCleanup(self.controller.shutdown)
        self.controller.start()
        self.channels = [
            dict(CHANNELS[0], logo=self.ONE),
            dict(CHANNELS[1], logo=self.TWO),
        ]
        return logos

    def tune_logo(self, index):
        self.controller.tune(self.channels[index], group_key="10", channels=self.channels)

    def test_banner_refreshes_when_its_logo_arrives(self):
        logos = self.with_logos()
        self.tune_logo(0)
        self.assertIsNone(self.banner.info.logo)
        self.assertEqual(logos.requested, [self.ONE])
        logos.logoReady.emit("http://logo/other.png")
        self.assertEqual(len(self.banner.named("show_info")), 1)
        logos.pixmaps[self.ONE] = QPixmap(4, 4)
        logos.logoReady.emit(self.ONE)
        self.assertIsNotNone(self.banner.info.logo)

    def test_a_logo_that_arrives_for_the_shown_channel_reaches_the_transport(self):
        logos = self.with_logos()
        self.tune_logo(0)
        self.ready("http://stream/1")
        self.transport.badge = True
        pixmap = logos.pixmaps[self.ONE] = QPixmap(4, 4)
        logos.logoReady.emit(self.ONE)
        self.assertEqual(self.transport.named("set_logo"), [(pixmap,)])

    def test_a_logo_for_a_channel_not_shown_yet_does_not_replace_the_shown_one(self):
        # During a zap the controller's channel is the requested one, but the
        # transport still names the playing one: its logo must not sit beside that name.
        logos = self.with_logos()
        self.tune_logo(0)
        self.ready("http://stream/1")
        self.transport.badge = True
        self.tune_logo(1)  # Two is requested; its link has not arrived
        logos.pixmaps[self.TWO] = QPixmap(4, 4)
        logos.logoReady.emit(self.TWO)
        self.assertEqual(self.transport.named("set_logo"), [])
        self.ready("http://stream/2")  # once it plays, its logo is the shown one's
        logos.logoReady.emit(self.TWO)
        self.assertEqual(self.transport.named("set_logo"), [(logos.pixmaps[self.TWO],)])

    def test_the_logo_live_controls_passes_reaches_the_transport(self):
        logos = self.with_logos()
        pixmap = logos.pixmaps[self.ONE] = QPixmap(4, 4)
        self.tune_logo(0)
        self.ready("http://stream/1")
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("show_live"), [("One", "")])
        self.assertIs(self.transport.logo, pixmap)


class ShutdownTests(ControllerTestCase):
    def test_shutdown_flushes_and_stops(self):
        self.controller.start()
        self.play(0)
        self.tune(1)
        self.controller.shutdown()
        self.assertEqual(self.watch.flushes, 1)
        self.assertEqual(self.session.stops, 1)
        self.catalog.linkReady.emit(2, "http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/1"])


class PipelineCase(ControllerTestCase):
    """A controller with the time-shift pipeline (a fake LiveSession) and a real budget."""

    def setUp(self):
        super().setUp()
        self.config.ssl_verify = True
        self.config.timeshift_enabled = False
        self.config.timeshift_max_mib = 2048
        self.live = FakeLiveSession(self.session)
        self.budget = ConnectionBudget()
        self.controller = self.make_controller(self.column, live=self.live, budget=self.budget)
        self.controller.start()

    def starts(self):
        return self.live.named("start")

    def start_channel(self, index):
        """Tune a channel and wait for its live session to start (the grant is deferred)."""
        count, pipeline = len(self.starts()), self.live.running and self.live.mode != "direct"
        self.play(index)
        if pipeline:  # the old channel's upstream closes first
            self.assertEqual(self.live.calls[-1], ("stop",))
            self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.starts()) == count + 1))

    def start_pipeline(self, index=0):
        self.start_channel(index)
        self.live.play_pipeline()


class BackNoticeTests(PipelineCase):
    """2.6: coming back to a channel plays it live and offers where it was left, a minute."""

    def setUp(self):
        super().setUp()
        from ui.overlays.back_notice import BackNotice

        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.close)
        self.notice = BackNotice(self.host)
        self.controller.overlays.back_notice = self.notice
        self.start_pipeline()
        self.left_at = datetime(2026, 10, 15, 20, 10).astimezone().timestamp()

    def test_it_offers_where_it_was_and_enter_goes_back(self):
        self.live.backOffered.emit(30.0, self.left_at)
        self.assertTrue(self.notice.isVisible())
        self.assertEqual(self.notice.text(), "Left off at 20:10")
        self.assertEqual(self.controller.back_label(), "20:10")
        self.controller.handle(Action.OK)
        self.assertEqual(self.live.calls[-1], ("go_back_to", 30.0))
        self.assertFalse(self.notice.isVisible())
        self.assertIsNone(self.controller.back_label())

    def test_a_click_goes_back_too(self):
        self.live.backOffered.emit(30.0, self.left_at)
        self.notice.keepWatching.emit()
        self.assertEqual(self.live.calls[-1], ("go_back_to", 30.0))

    def test_the_phone_goes_back_too(self):
        self.live.backOffered.emit(30.0, self.left_at)
        self.controller.handle(Action.BACK_TO)
        self.assertEqual(self.live.calls[-1], ("go_back_to", 30.0))
        self.controller.handle(Action.BACK_TO)  # gone now: nothing more
        self.assertEqual(
            [c for c in self.live.calls if c[0] == "go_back_to"], [("go_back_to", 30.0)]
        )

    def test_it_goes_after_a_minute_and_with_the_channel(self):
        import ui.live_controls as live_controls

        self.assertEqual(live_controls.BACK_NOTICE_MS, 60_000)
        with mock.patch.object(live_controls, "BACK_NOTICE_MS", 30):
            self.live.backOffered.emit(30.0, self.left_at)
            elapse(60)
        self.assertFalse(self.notice.isVisible())
        self.assertIsNone(self.controller.back_label())
        self.live.backOffered.emit(30.0, self.left_at)
        self.start_channel(1)  # another channel
        self.assertFalse(self.notice.isVisible())


class PipelineTuneTests(PipelineCase):
    def test_a_tune_takes_a_budget_slot_and_starts_the_live_session_on_grant(self):
        self.play(0)
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.starts(), [])  # the grant is always deferred
        self.assertTrue(wait_until(lambda: self.starts()))
        url, kwargs = self.starts()[0]
        self.assertEqual(url, "http://stream/1")
        self.assertEqual(
            kwargs,
            {
                "headers": {"User-Agent": DEFAULT_USER_AGENT},
                "verify_ssl": True,
                "latency": "low",
                "user_agent": DEFAULT_USER_AGENT,
                "disk": False,
                "max_bytes": 256 * MIB,  # without consent, the memory per stream: 256 MiB
                "channel": channel_key("Home", CHANNELS[0]),  # what it taught is kept (2.0.2)
                "one_stream": True,  # 2.6.2: a provider with a limit: no second download
            },
        )
        self.assertEqual(self.session.plays, [])  # the live session plays it, not the controller
        self.assertEqual(self.zap.context.current(), CHANNELS[0])
        self.assertEqual(self.watch.plays, [("Home", CHANNELS[0], "channel", url, "10")])
        self.assertEqual(self.banner.named("show_info")[0][0].name, "One")  # shown at once

    def test_disk_time_shift_uses_the_configured_size_the_channel_agent_and_provider_tls(self):
        self.config.timeshift_enabled = True
        self.config.timeshift_max_mib = 4096
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "ssl_verify": False}
        channel = dict(CHANNELS[0], user_agent="Box/1.0")
        self.controller.tune(channel, group_key="10", channels=[channel])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.starts()))
        kwargs = self.starts()[0][1]
        self.assertEqual(kwargs["headers"], {"User-Agent": "Box/1.0"})
        self.assertEqual(kwargs["user_agent"], "Box/1.0")
        self.assertNotIn("referrer", kwargs)
        self.assertFalse(kwargs["verify_ssl"])
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (True, 4096 * MIB))

    def test_a_second_tune_stops_the_first_and_starts_only_after_released(self):
        self.start_pipeline(0)
        self.play(1)
        self.assertEqual(self.live.calls[-1], ("stop",))
        elapse(30)
        self.assertEqual(len(self.starts()), 1)  # the old upstream still holds the slot
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.assertEqual(self.starts()[1][0], "http://stream/2")
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_an_unlimited_provider_also_waits_for_the_old_upstream(self):
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_pipeline(0)
        self.play(1)
        elapse(30)  # its slot is granted at once…
        self.assertEqual(len(self.starts()), 1)  # …but the old stream is not closed yet
        self.live.released.emit()
        self.assertEqual(self.starts()[1][0], "http://stream/2")
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_zapping_past_a_waiting_tune_starts_only_the_newest(self):
        self.start_pipeline(0)
        self.play(1)
        self.play(2)  # Two never started: its queued slot is dropped
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        elapse(30)
        self.assertEqual([url for url, _ in self.starts()], ["http://stream/1", "http://stream/3"])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_budget_denial_toasts_and_opens_nothing(self):
        self.budget.request("Home", 1, purpose="pip")  # the PiP holds the only slot
        self.play(0)
        elapse(30)
        self.assertEqual(
            self.toast.messages(), [("Your provider allows 1 stream at a time", "error")]
        )
        self.assertEqual(self.live.calls, [])
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        self.assertEqual(self.watch.plays, [])

    def test_a_channel_file_or_other_scheme_plays_directly(self):
        self.start_pipeline(0)
        self.tune(1)
        self.ready("file:///media/clip.mp4")
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.live.released.emit()
        self.assertEqual(self.session.plays[-1][0], "file:///media/clip.mp4")
        self.assertEqual(self.session.plays[-1][1]["kind"], PlayKind.VOD_FILE)
        self.tune(2)
        self.ready("udp://239.0.0.1:1234")
        self.assertEqual(self.session.plays[-1][0], "udp://239.0.0.1:1234")
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.budget.holders("Home"), ["main"])  # the direct play's own

    def test_a_channel_from_another_provider_holds_that_providers_slot(self):
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.starts()))
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])
        self.assertEqual(self.budget.holders("Home"), [])
        self.assertEqual(self.watch.plays[-1][0], "Harbor IPTV")
        self.assertEqual(self.starts()[0][1]["channel"], channel_key("Harbor IPTV", HARBOR_ONE))

    def test_a_direct_play_of_another_providers_channel_books_its_slot(self):
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("udp://239.0.0.1:1234")
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["main"])
        self.assertEqual(self.budget.holders("Home"), [])
        self.assertEqual(self.watch.plays[-1][0], "Harbor IPTV")

    def test_the_limit_is_the_channels_own_providers(self):
        # Home is an STB portal (one stream); Harbor's panel reported two.
        self.catalog.limits_of["Harbor IPTV"] = 2
        self.budget.request("Harbor IPTV", 2, purpose="pip")
        self.controller.tune(HARBOR_ONE, group_key="10", channels=[HARBOR_ONE])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.starts()))  # one of two taken: it opens
        self.assertEqual(sorted(self.budget.holders("Harbor IPTV")), ["main", "pip"])

    def test_a_direct_play_books_its_slot_under_the_catalogs_provider(self):
        # During a provider setup the selected provider is already the next one, while the
        # catalog (and every pipeline tune) still uses the old: one name for both.
        self.config.selected_provider_name = "Backup"
        self.tune(1)
        self.ready("udp://239.0.0.1:1234")
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.budget.holders("Backup"), [])

    def test_a_movie_stops_the_live_channel_and_gives_its_slot_back(self):
        self.start_pipeline(0)
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.assertIn(("stop",), self.live.calls)
        self.live.released.emit()
        self.assertEqual(self.played_urls()[-1], "http://vod/heat.mkv")
        self.assertEqual(self.budget.holders("Home"), ["main"])  # the movie's, not the channel's
        self.controller.handle(Action.SEEK_BACK)  # the movie's keys reach the player again
        self.assertEqual(self.live.named("seek_relative"), [])

    def test_a_channel_after_a_movie_stops_the_movie(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        stops = self.session.stops
        self.start_channel(0)
        self.assertEqual(self.session.stops, stops + 1)

    def test_shutdown_stops_the_live_session(self):
        self.start_pipeline(0)
        self.controller.shutdown()
        self.assertEqual(self.live.calls[-1], ("stop",))

    def test_rechoosing_the_channel_while_it_opens_does_not_restart_it(self):
        self.play(0)
        self.tune(0)  # still waiting for its slot or the pipeline
        self.assertTrue(wait_until(lambda: self.starts()))
        self.tune(0)
        elapse(30)
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.requested(), ["One"])


class ReleaseOrderTests(PipelineCase):
    """Nothing opens the provider again before the previous stream is released (spec §6.9)."""

    def play_movie(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")

    def test_pipeline_to_movie(self):
        self.start_pipeline(0)
        self.play_movie()
        self.assertEqual(self.played_urls(), ["http://127.0.0.1:9/t/s1/stream/0.ts"])
        self.assertEqual(self.transport.named("show_vod"), [("Heat", "")])  # shown at once
        self.live.released.emit()
        self.assertEqual(self.played_urls()[-1], "http://vod/heat.mkv")

    def test_movie_to_pipeline(self):
        self.play_movie()
        self.session.holding = True  # VLC still holds the movie's connection
        self.play(0)
        elapse(30)
        self.assertEqual(self.starts(), [])  # VLC has not let go of the movie…
        self.assertEqual(self.budget.holders("Home"), ["main"])  # …which keeps its slot
        self.session.let_go()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 1))

    def test_direct_channel_to_pipeline(self):
        self.tune(0)
        self.ready("udp://239.0.0.1:1234")
        self.session.holding = True
        self.play(1)
        elapse(30)
        self.assertEqual(self.starts(), [])
        self.session.let_go()
        self.assertTrue(wait_until(lambda: self.starts()))
        self.assertEqual(self.starts()[0][0], "http://stream/2")

    def test_pipeline_to_external_player(self):
        self.start_pipeline(0)
        self.config.play_in_vlc = True
        self.play(1)
        self.assertEqual(self.external.calls, [])
        self.live.released.emit()
        self.assertEqual(self.external.calls, [("vlc", "http://stream/2")])
        self.assertEqual(self.budget.holders("Home"), [])

    def test_direct_fallback_to_pipeline_needs_no_second_release(self):
        self.start_channel(0)
        self.live.play_direct()
        self.play(1)  # its stop releases at once (re-entrant): nothing more to wait for
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.assertEqual(self.starts()[1][0], "http://stream/2")
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_the_wait_for_vlc_is_capped(self):
        self.enterContext(mock.patch("ui.live_tuner.RELEASE_CAP_MS", 40))
        controller = self.make_controller(self.column, live=FakeLiveSession(self.session))
        controller.start()
        self.controller.shutdown()
        self.controller = controller
        self.play_movie()
        self.session.holding = True
        with self.assertLogs("ui.live_tuner", "WARNING"):
            self.play_movie()
            self.assertTrue(wait_until(lambda: len(self.played_urls()) == 2))

    def test_a_movie_after_a_movie_waits_for_the_first_to_end(self):
        self.play_movie()
        self.session.holding = True
        self.play_movie()
        self.assertEqual(len(self.played_urls()), 1)
        self.session.let_go()
        self.assertEqual(len(self.played_urls()), 2)


class LocalFileExternalTests(PipelineCase):
    """An opened file sent to VLC keeps play_file's contract: no provider slot,
    no history."""

    def test_an_opened_file_sent_to_vlc_takes_no_slot_and_leaves_no_history(self):
        path = Path("clip.mkv")
        self.controller.play_file(str(path))
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append(menu)):
            self.transport.externalRequested.emit(QPoint(300, 600))
        [menu] = shown
        self.addCleanup(menu.deleteLater)
        with mock.patch.object(self.budget, "request", wraps=self.budget.request) as request:
            ContextMenuTests.choose(menu, "Play in VLC")
        self.assertEqual(self.external.calls, [("vlc", path.absolute().as_uri())])
        request.assert_not_called()
        self.assertEqual(self.budget.holders("Home"), [])
        self.assertEqual(self.watch.plays, [])


class SlotWaitTests(PipelineCase):
    def test_pause_pressed_while_the_tune_waits_applies_at_its_start(self):
        self.play(0)
        self.controller.handle(Action.PLAY_PAUSE)
        self.assertEqual(self.session.toggles, 0)
        self.assertTrue(wait_until(lambda: self.starts()))
        self.assertEqual(self.live.calls[1:], [("toggle_pause",)])

    def test_go_live_while_the_tune_waits_undoes_the_pause(self):
        self.play(0)
        self.controller.handle(Action.PLAY_PAUSE)
        self.controller.handle(Action.GO_LIVE)
        self.assertTrue(wait_until(lambda: self.starts()))
        elapse(10)
        self.assertEqual(self.live.calls[1:], [])

    def test_a_start_that_raises_gives_its_slot_back_and_says_so(self):
        def broken(url, **kwargs):
            raise RuntimeError(f"cannot open {url}")

        self.live.start = broken
        with self.assertLogs("ui.live_tuner", "WARNING"):
            self.play(0)
            self.assertTrue(wait_until(lambda: self.toast.messages()))
        self.assertEqual(self.toast.messages(), [("One: cannot open …", "error")])
        self.assertEqual(self.budget.holders("Home"), [])
        del self.live.start
        self.start_channel(1)  # the next tune works
        self.assertEqual(self.starts()[0][0], "http://stream/2")


class StreamLimitTests(PipelineCase):
    def test_a_stream_limit_refusal_is_never_retried(self):
        # Spec §9: the limit is reached; asking again (not even an STB fresh link) would only
        # be refused again, or take a slot another device of the user's holds.
        self.start_pipeline(0)
        self.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(self.requested(), ["One"])  # no new link
        self.assertEqual(
            self.toast.messages(), [("Your provider allows 1 stream at a time", "error")]
        )
        self.assertEqual(self.live.calls[-1], ("stop",))  # its slot goes back
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), [])

    def test_a_limit_above_one_says_streams(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.start_pipeline(0)
        self.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(
            self.toast.messages(), [("Your provider allows 2 streams at a time", "error")]
        )

    def test_an_unknown_limit_is_said_plainly(self):
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_pipeline(0)
        self.live.failed.emit(STREAM_LIMIT)
        self.assertEqual(
            self.toast.messages(), [("Your provider limits concurrent streams", "error")]
        )

    def refuse(self, message):
        """As LiveSession: the refused session closes, nothing plays, then failed()."""
        self.live.mode = ""
        self.live.modeChanged.emit("")
        self.live.failed.emit(message)

    def test_a_plain_403_says_the_provider_may_limit_streams(self):
        self.start_pipeline(0)
        self.refuse(STREAM_REFUSED)  # an STB token first
        self.ready("http://stream/1?token=new")
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.refuse(STREAM_REFUSED)
        self.live.released.emit()  # the refused session closed
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_channel(1)
        self.refuse(STREAM_REFUSED)
        self.assertEqual(
            self.toast.messages(),
            [
                (
                    "The provider refused the stream — it may allow only 1 stream at a time",
                    "error",
                ),
                ("The provider refused the stream — it may limit concurrent streams", "error"),
            ],
        )

    def test_a_refusal_whose_new_link_fails_ends_the_tune(self):
        self.start_pipeline(0)
        self.refuse(STREAM_REFUSED)  # a plain 403 may be an expired STB token: one new link
        self.assertEqual(self.requested(), ["One", "One"])
        self.assertEqual(self.live.calls[-1], ("stop",))  # ended before the new link
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), [])  # its slot went back
        self.catalog.linkFailed.emit(2, "Portal down")
        self.assertFalse(self.controller._has_media())
        self.controller.handle(Action.SEEK_BACK)  # not silent: nothing is opening
        self.assertEqual(
            self.toast.messages(), [("One: Portal down", "error"), (UNAVAILABLE, "info")]
        )


class ResumeTests(PipelineCase):
    """A Windows self-update stops what plays; when it cannot launch, it plays again."""

    def test_a_live_channel_is_tuned_again(self):
        self.start_pipeline(0)
        self.controller.stop_live()
        self.live.released.emit()
        self.controller.resume_stopped()
        self.assertEqual(self.requested(), ["One", "One"])  # a fresh link (STB tokens)
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))

    def test_a_movie_reopens_at_its_last_position(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.watch.resume["Home:movie:m7"] = 600_000  # the watch state's last position
        self.controller.stop_live()
        self.controller.resume_stopped()
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 600_000)
        self.assertEqual(self.requested(), ["Heat", "Heat"])

    def test_nothing_resumes_when_nothing_played(self):
        self.controller.stop_live()
        self.controller.resume_stopped()
        self.config.play_in_vlc = True  # an external player: the app played nothing
        self.play(0)
        self.controller.stop_live()
        self.controller.resume_stopped()
        self.assertEqual(self.requested(), ["One"])


class PipelineControlTests(PipelineCase):
    STATE = {
        "start": 0.0,
        "end": 3600.0,
        "position": 3000.0,
        "live_edge": 3600.0,
        "lag": 600.0,
        "mode": "shifted",
        "rate": 1.0,
        "paused": False,
        "ended": False,
        "target": 2.5,
    }

    def test_keys_during_live_reach_the_live_session(self):
        self.start_pipeline(0)
        before = len(self.live.calls)
        for action in (Action.SEEK_BACK, Action.SEEK_FWD, Action.GO_LIVE, Action.PLAY_PAUSE):
            self.controller.handle(action)
        self.assertEqual(
            self.live.calls[before:],
            [("seek_relative", -10.0), ("seek_relative", 10.0), ("go_live",), ("toggle_pause",)],
        )
        self.assertEqual((self.session.relative, self.session.toggles), ([], 0))

    def test_transport_requests_reach_the_live_session(self):
        self.start_pipeline(0)
        before = len(self.live.calls)
        transport = self.transport
        transport.seekRelativeRequested.emit(-10_000)
        transport.seekRelativeRequested.emit(60_000)  # the +1 min button
        transport.seekToRequested.emit(42.5)
        transport.speedRequested.emit(1.5)
        transport.goLiveRequested.emit()
        transport.playPauseRequested.emit()
        self.assertEqual(
            self.live.calls[before:],
            [
                ("seek_relative", -10.0),
                ("seek_relative", 60.0),
                ("seek_to", 42.5),
                ("set_speed", 1.5),
                ("go_live",),
                ("toggle_pause",),
            ],
        )
        self.assertEqual((self.session.relative, self.session.rates), ([], []))

    def test_a_direct_fallback_keeps_play_pause_but_has_no_rewind(self):
        self.start_channel(0)
        self.live.play_direct()
        self.controller.handle(Action.SEEK_BACK)
        self.controller.handle(Action.PLAY_PAUSE)
        self.assertEqual(self.live.calls[1:], [("toggle_pause",)])
        self.assertEqual(self.toast.messages(), [(UNAVAILABLE, "info")])

    def test_a_rewind_on_a_channel_played_directly_for_smoothness_says_why(self):
        self.start_channel(0)
        self.live.no_rewind_reason = CRAWLING  # it crawled through the time-shift buffer
        self.live.play_direct()
        self.controller.handle(Action.SEEK_BACK)
        self.assertEqual(self.toast.messages(), [(CRAWLING, "info")])

    def test_seeking_while_the_pipeline_still_opens_says_nothing(self):
        self.play(0)
        self.controller.handle(Action.SEEK_BACK)  # waiting for its slot
        self.assertTrue(wait_until(lambda: self.starts()))
        self.controller.handle(Action.SEEK_BACK)  # the pipeline is not ready yet
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.live.named("seek_relative"), [])

    def test_pause_and_go_live_before_the_stream_opens_reach_the_live_session(self):
        self.start_channel(0)  # the pipeline is still opening: LiveSession remembers them
        self.controller.handle(Action.PLAY_PAUSE)
        self.controller.handle(Action.GO_LIVE)
        self.assertEqual(self.live.calls[1:], [("toggle_pause",), ("go_live",)])

    def test_go_live_without_a_live_channel_does_nothing(self):
        self.controller.handle(Action.GO_LIVE)
        self.start_channel(0)
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.controller.handle(Action.GO_LIVE)
        self.assertEqual(self.live.named("go_live"), [])
        self.assertEqual(self.toast.messages(), [])

    def test_the_transport_shows_the_time_shift_bar_with_programme_ticks(self):
        self.catalog.epg[1] = [
            Programme("News", NOW - timedelta(minutes=30), NOW + timedelta(minutes=30)),
            Programme("Film", NOW + timedelta(minutes=30), NOW + timedelta(hours=2)),
        ]
        self.start_pipeline(0)
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("show_timeshift"), [("One", "News")])
        self.live.timelineChanged.emit(dict(self.STATE))
        state, ticks = self.transport.named("update_timeline")[-1]
        self.assertEqual(state, self.STATE)
        self.assertEqual(ticks, [(1800.0, "News"), (5400.0, "Film")])  # the edge is now

    def test_the_transport_switches_to_the_bar_when_the_pipeline_plays(self):
        self.start_channel(0)
        self.controller.mouse_moved()  # still opening: plain live
        self.assertEqual(self.transport.named("show_live"), [("One", "")])
        self.live.play_pipeline()
        self.assertEqual(self.transport.named("show_timeshift"), [("One", "")])
        self.live.play_direct()
        self.assertEqual(self.transport.named("show_live")[-1], ("One", ""))

    def test_a_hidden_transport_never_gets_the_last_channels_ticks(self):
        self.catalog.epg[1] = [
            Programme("News", NOW - timedelta(minutes=30), NOW + timedelta(minutes=30))
        ]
        self.start_pipeline(0)
        self.controller.mouse_moved()
        self.live.timelineChanged.emit(dict(self.STATE))
        self.assertEqual(self.transport.named("update_timeline")[-1][1], [(1800.0, "News")])
        self.transport.dismiss()
        self.start_channel(1)
        self.live.play_pipeline()
        self.live.timelineChanged.emit(dict(self.STATE))
        self.assertEqual(self.transport.named("update_timeline")[-1][1], [])

    def test_programme_start_seeks_to_the_buffered_start_else_the_oldest_point(self):
        self.catalog.epg[1] = [
            Programme("News", NOW - timedelta(minutes=30), NOW + timedelta(minutes=30))
        ]
        self.start_pipeline(0)
        self.live.timelineChanged.emit(dict(self.STATE))  # watching 10 min behind: News
        self.transport.programmeStartRequested.emit()
        self.live.timelineChanged.emit(dict(self.STATE, start=2000.0))  # its start evicted
        self.transport.programmeStartRequested.emit()
        self.catalog.epg[1] = []  # no guide: the oldest point
        self.transport.programmeStartRequested.emit()
        self.assertEqual(self.live.named("seek_to"), [(1800.0,), (2000.0,), (2000.0,)])

    def test_a_latency_change_reaches_the_running_channel(self):
        self.start_pipeline(0)
        self.controller.settings_saved()  # unchanged
        self.config.live_latency = "stable"
        self.controller.settings_saved()
        self.controller.settings_saved()
        self.assertEqual(self.live.named("set_latency"), [("stable",)])


class PipelineErrorTests(PipelineCase):
    def test_the_unavailable_notice_is_silent_on_load(self):
        # A rewind says it (test_a_direct_fallback_keeps_play_pause_but_has_no_rewind); a
        # tune that falls back does not: on slow channels it was noise at every first tune.
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)
        self.live.notice.emit("Rewound past the buffer")
        self.start_channel(1)
        self.live.notice.emit(UNAVAILABLE)
        self.live.notice.emit("Rewound past the buffer")  # other notices every time
        self.assertEqual(
            self.toast.messages(),
            [("Rewound past the buffer", "info"), ("Rewound past the buffer", "info")],
        )

    def test_pipeline_playback_errors_are_left_to_the_live_session(self):
        self.catalog.ptype = "M3UPLAYLIST"  # no link to make again
        self.start_pipeline(0)
        self.session.errorOccurred.emit("VLC could not open the stream")
        self.session.ended.emit()
        self.assertEqual(self.toast.messages(), [])  # LiveSession reopens the buffer
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])

    def test_a_direct_fallback_reports_its_errors_once_and_its_end(self):
        self.catalog.ptype = "M3UPLAYLIST"
        self.start_channel(0)
        self.live.play_direct()
        self.session.errorOccurred.emit("Broken")  # LiveSession forwards it as failed
        self.live.failed.emit("Broken")
        self.session.ended.emit()  # first opened again (2.0.1), see ReconnectTests
        self.assertEqual(
            self.toast.messages(), [("One: Broken", "error"), ("Reconnecting…", "info")]
        )

    def test_an_stb_failure_makes_the_link_again_once_per_tune(self):
        self.start_pipeline(0)  # an STB provider
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One", "One"])  # a fresh play token
        self.ready("http://stream/1?play_token=new")
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.assertEqual(self.starts()[1][0], "http://stream/1?play_token=new")
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])
        self.assertEqual(len(self.catalog.links), 2)
        self.start_channel(1)  # a new tune may try again
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.requested(), ["One", "One", "Two", "Two"])

    def test_an_stb_stream_that_ends_at_once_in_direct_play_is_made_again(self):
        self.start_channel(0)
        self.live.play_direct()
        self.session.ended.emit()  # VLC reports some refusals (403) as an end
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1?token=new")
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.live.play_direct()
        self.session.ended.emit()  # once per tune: then the same link opens again
        self.assertEqual(self.toast.messages(), [("Reconnecting…", "info")])
        self.assertEqual(self.requested(), ["One", "One"])

    def test_a_failure_before_the_retry_does_not_use_up_the_notice(self):
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)  # an expired token also fails the pipeline
        self.live.play_direct()
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [])
        self.ready("http://stream/1?token=new")
        self.assertTrue(wait_until(lambda: len(self.starts()) == 2))
        self.live.notice.emit(UNAVAILABLE)  # it really cannot time-shift
        self.live.play_direct()
        self.assertEqual(self.toast.messages(), [])  # silent on load; a rewind says it

    def test_a_held_notice_shows_once_the_direct_play_works(self):
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)
        self.live.play_direct()
        self.assertEqual(self.toast.messages(), [])
        self.session.stateChanged.emit("playing")
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.toast.messages(), [])  # silent on load; a rewind says it

    def test_a_failure_after_the_user_moved_on_is_not_retried(self):
        self.start_pipeline(0)
        self.tune(1)  # its link is still on the way
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])


class PipelineReferrerTests(PipelineCase):
    def test_an_m3u_channels_referrer_goes_with_its_tune(self):
        channel = dict(CHANNELS[0], user_agent="UA/1", referrer="https://site.test/")
        self.controller.tune(channel, group_key="10", channels=[channel])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.starts()))
        kwargs = self.starts()[0][1]
        self.assertEqual(kwargs["headers"], {"User-Agent": "UA/1", "Referer": "https://site.test/"})
        self.assertEqual((kwargs["user_agent"], kwargs["referrer"]), ("UA/1", "https://site.test/"))


class ReconnectTests(PipelineCase):
    """A live channel played directly whose stream ends (2.0.1): an on-demand restream
    may show its placeholder and end. It opens again in the same player, so in the same
    budget slot, twice at most, before "The stream ended"."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.catalog.ptype = "XTREAM"  # no link to make again

    def fall_back(self, index=0):
        self.start_channel(index)
        self.live.play_direct()  # it plays the provider's stream itself
        return len(self.session.plays)

    def end_and_reopen(self, plays):
        started = time.monotonic()
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("Reconnecting…", "info"))
        self.assertEqual(len(self.session.plays), plays)  # after a back-off
        self.assertTrue(wait_until(lambda: len(self.session.plays) == plays + 1))
        return time.monotonic() - started

    def test_an_ended_fallback_opens_again_twice_then_says_it_ended(self):
        plays = self.fall_back()
        url, kwargs = self.session.plays[-1]
        stops = self.session.stops
        first = self.end_and_reopen(plays)
        self.assertEqual(self.session.plays[-1], (url, kwargs))  # the same stream
        second = self.end_and_reopen(plays + 1)
        self.assertEqual(self.session.plays[-1], (url, kwargs))
        self.assertGreater(second, first)  # a longer back-off the second time
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("The stream ended", "info"))
        self.assertEqual([text for text, _ in self.toast.messages()].count("Reconnecting…"), 2)
        QTest.qWait(200)
        self.assertEqual(len(self.session.plays), plays + 2)
        # One slot, the same one, all along: nothing was stopped or given back.
        self.assertEqual(self.session.stops, stops)
        self.assertEqual(self.live.named("stop"), [])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_stream_that_played_a_while_since_may_reconnect_twice_again(self):
        self.enterContext(mock.patch("ui.live_controls.HEALTHY_S", 0.05))
        plays = self.fall_back()
        self.end_and_reopen(plays)
        self.end_and_reopen(plays + 1)
        self.session.stateChanged.emit("playing")
        QTest.qWait(80)  # it played HEALTHY_S since its last reopen
        self.end_and_reopen(plays + 2)
        self.session.stateChanged.emit("playing")  # too briefly this time
        self.end_and_reopen(plays + 3)
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("The stream ended", "info"))

    def test_a_reopen_decodes_in_software_as_the_stream_did(self):
        plays = self.fall_back()
        self.assertNotIn("software", self.session.plays[-1][1])
        self.session.software = True  # hardware decoding froze its picture: software since
        self.end_and_reopen(plays)
        self.assertIs(self.session.plays[-1][1]["software"], True)

    def test_each_tune_may_reconnect_twice(self):
        plays = self.fall_back(0)
        self.end_and_reopen(plays)
        plays = self.fall_back(1)
        self.end_and_reopen(plays)
        self.end_and_reopen(plays + 1)

    def test_a_zap_during_the_back_off_opens_nothing_old(self):
        self.fall_back(0)
        self.session.ended.emit()
        self.start_channel(1)
        QTest.qWait(200)
        self.assertNotIn("http://stream/1", self.played_urls()[1:])

    def test_the_time_shift_buffer_is_never_reconnected_here(self):
        self.start_pipeline(0)
        plays = len(self.session.plays)
        self.session.ended.emit()  # LiveSession reopens its buffer itself
        QTest.qWait(200)
        self.assertEqual((len(self.session.plays), self.toast.messages()), (plays, []))


class DirectReconnectTests(ControllerTestCase):
    """The same for a channel the pipeline cannot read, which the player opens itself."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (40, 120)))
        self.catalog.ptype = "XTREAM"
        self.controller.start()

    def test_an_ended_direct_channel_opens_again_in_the_same_slot(self):
        self.play(0)
        url, kwargs = self.session.plays[-1]
        self.assertEqual(kwargs["kind"], PlayKind.LIVE)
        self.session.holding = True  # libVLC holds the ended input until the reopen
        for count in (2, 3):
            self.session.ended.emit()
            self.assertEqual(self.toast.messages()[-1], ("Reconnecting…", "info"))
            self.assertTrue(wait_until(lambda: len(self.session.plays) == count))
            self.assertEqual(self.session.plays[-1], (url, kwargs))
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("The stream ended", "info"))
        self.assertEqual(self.session.stops, 0)  # the slot was never given back

    def test_an_m3u_channels_agent_and_referrer_go_with_its_play_and_reopen(self):
        # 2.1: #EXTVLCOPT http-user-agent and http-referrer reach VLC's own requests.
        channel = dict(CHANNELS[0], user_agent="UA/1", referrer="https://site.test/")
        self.controller.tune(channel, group_key="10", channels=[channel])
        self.ready("http://stream/1")
        url, kwargs = self.session.plays[-1]
        self.assertEqual((kwargs["user_agent"], kwargs["referrer"]), ("UA/1", "https://site.test/"))
        self.session.ended.emit()
        self.assertTrue(wait_until(lambda: len(self.session.plays) == 2))
        self.assertEqual(self.session.plays[-1], (url, kwargs))  # the reopen, alike
        self.play(1)  # a channel without them plays as before
        self.assertNotIn("referrer", self.session.plays[-1][1])

    def test_a_file_listed_as_a_channel_is_not_reconnected(self):
        self.tune(0)
        self.ready("http://stream/1.mp4")
        self.assertEqual(self.session.plays[-1][1]["kind"], PlayKind.VOD_FILE)
        self.session.ended.emit()
        self.assertEqual(self.toast.messages(), [("The stream ended", "info")])
        QTest.qWait(200)
        self.assertEqual(len(self.session.plays), 1)

    def test_a_movie_is_never_reconnected(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.catalog.linkReady.emit(1, "http://vod/heat.mkv")
        self.session.ended.emit()
        QTest.qWait(200)
        self.assertEqual(len(self.session.plays), 1)
        self.assertNotIn(("Reconnecting…", "info"), self.toast.messages())


class ExternalZapTests(ControllerTestCase):
    """External-player mode launches the player once per settled zap, not per step."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("ui.tv_controller.EXTERNAL_SETTLE_MS", 60)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config.play_in_vlc = True
        self.controller.start()

    def test_zap_steps_launch_only_the_channel_the_zapping_settles_on(self):
        self.play(0)  # a pick launches at once
        self.assertEqual(self.external.calls, [("vlc", "http://stream/1")])
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)  # Two
        self.ready("http://stream/2")
        self.assertEqual(len(self.external.calls), 1)
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)  # Three, before Two settled
        self.ready("http://stream/3")
        self.assertTrue(wait_until(lambda: len(self.external.calls) == 2))
        elapse(120)
        self.assertEqual(
            self.external.calls, [("vlc", "http://stream/1"), ("vlc", "http://stream/3")]
        )
        self.assertEqual([p[1]["name"] for p in self.watch.plays], ["One", "Three"])
        self.assertEqual(self.zap.context.current(), CHANNELS[2])

    def test_a_pick_during_the_settling_drops_the_zapped_launch(self):
        self.play(0)
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)
        self.ready("http://stream/2")
        self.play(3)  # a drawer pick of Four
        elapse(120)
        self.assertEqual(
            self.external.calls, [("vlc", "http://stream/1"), ("vlc", "http://stream/4")]
        )


SERIES = {"id": "s9", "name": "Harbour Lights"}
SEASON_1 = {"id": "1", "number": "1", "name": "Season 1"}
SEASON_2 = {"id": "2", "number": "2", "name": "Season 2"}


def episode(season, number):
    sid = season["id"]
    return {
        "id": f"e{sid}{number}",
        "ename": f"Episode {number}",
        "name": f"Episode {number}",
        "cmd": f"http://vod/e{sid}{number}.mkv",
        "season": season["number"],
        "episode_num": str(number),
        "number": str(number),
    }


def movie(number):
    return {
        "id": f"m{number}",
        "name": f"Film {number}",
        "cmd": f"http://vod/m{number}.mkv",
        "category_id": "5",
    }


def play_context(**values):
    """What the Library hands to play_vod (LibraryController._context)."""
    context = {
        "start_over": False,
        "series": None,
        "season": None,
        "episodes": None,
        "genre_items": None,
    }
    context.update(values)
    return context


class GuideTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.play(1)  # Two, from the group of four

    def test_g_opens_the_guide_on_the_zap_channels_and_a_pick_tunes_and_closes_it(self):
        self.controller.handle(Action.GUIDE)
        self.assertEqual(self.guide.named("open_guide"), [(CHANNELS, 1)])
        self.assertEqual(self.controller.key_context(), "guide")
        self.guide.tuneRequested.emit(CHANNELS[3])
        self.assertFalse(self.guide.visible)
        self.assertEqual(self.requested(), ["Two", "Four"])
        self.assertEqual(self.zap.context.group_key, "10")
        self.assertEqual(self.zap.context.channels, CHANNELS)
        self.assertEqual(self.controller.key_context(), "player")

    def test_keys_go_to_the_open_guide(self):
        self.controller.handle(Action.GUIDE)
        for action in (
            Action.NAV_UP,
            Action.NAV_DOWN,
            Action.NAV_PAGE_UP,
            Action.NAV_PAGE_DOWN,
            Action.NAV_LEFT,
            Action.NAV_RIGHT,
            Action.NAV_ACTIVATE,
        ):
            self.controller.handle(action)
        self.assertEqual(self.guide.named("move_by"), [(-1,), (1,)])
        self.assertEqual(self.guide.named("page"), [(-1,), (1,)])
        self.assertEqual(self.guide.named("shift"), [(-1,), (1,)])
        self.assertEqual(self.guide.named("activate"), [()])
        self.assertEqual(self.drawer.calls, [])  # nothing reached the drawer behind
        self.assertEqual(self.requested(), ["Two"])
        self.controller.handle(Action.GUIDE)  # G again closes it
        self.assertFalse(self.guide.visible)
        self.controller.handle(Action.GUIDE)
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.guide.visible)
        self.assertEqual(self.window.calls, [])  # Esc closed the guide, not fullscreen

    def test_the_guide_waits_for_live_tv(self):
        self.controller.play_vod(movie(1), "movie", play_context())
        self.ready("http://vod/m1.mkv")
        self.controller.handle(Action.GUIDE)
        self.assertEqual(self.guide.named("open_guide"), [])

    def test_new_guide_data_reaches_the_open_guide(self):
        self.controller.handle(Action.GUIDE)
        self.catalog.epgReady.emit()
        self.assertEqual(self.guide.named("invalidate"), [()])

    def test_the_guides_fullscreen_button_toggles_fullscreen_and_keeps_it_open(self):
        self.controller.handle(Action.GUIDE)
        self.guide.fullscreenRequested.emit()
        self.assertEqual(self.window.calls, ["toggle_fullscreen"])
        self.assertTrue(self.guide.visible)


class AutoplayCase(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")  # the setup's "Loading Home…" ends
        self.drawer.visible = False  # it opened on the idle player
        self.assertIsNone(self.toast.hint)
        self.catalog.season_map["s9"] = [SEASON_1, SEASON_2]

    def play_episode(self, season, number, **values):
        item = episode(season, number)
        episodes = self.catalog.episode_map.get(("s9", season["id"]))
        context = play_context(series=SERIES, season=season, episodes=episodes, **values)
        self.controller.play_vod(item, "episode", context)
        self.assertEqual(self.catalog.links[-1][1:], (item, "series", True))
        self.ready(item["cmd"])
        return item

    def link_items(self):
        return [item.get("id") for _, item, _, _ in self.catalog.links]

    def end(self):
        """As PlaybackSession: the state is "ended" before ended() is emitted."""
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()


class EpisodeAutoplayTests(AutoplayCase):
    def setUp(self):
        super().setUp()
        self.catalog.episode_map[("s9", "1")] = [episode(SEASON_1, 1), episode(SEASON_1, 2)]

    def test_an_episode_ends_and_play_now_plays_the_next_one_with_resume(self):
        self.play_episode(SEASON_1, 1)
        self.assertEqual(self.transport.named("set_next_available"), [(True,)])
        self.assertEqual(self.transport.named("show_vod"), [("Episode 1", "Harbour Lights")])
        self.watch.resume["Home:episode:e12"] = 61_000
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [("Episode 2", "S1 · E2", "episode")])
        self.assertNotIn(("Finished", "info"), self.toast.messages())
        self.next_card.activate()  # Play now
        self.assertEqual(self.link_items(), ["e11", "e12"])
        self.ready("http://vod/e12.mkv")
        url, kwargs = self.session.plays[-1]
        self.assertEqual((url, kwargs["start_ms"]), ("http://vod/e12.mkv", 61_000))
        self.assertEqual(self.toast.messages()[-1], ("Resumed at 1:01", "info"))
        self.assertEqual(self.transport.named("show_vod")[-1], ("Episode 2", "Harbour Lights"))

    def test_enter_plays_now_and_escape_cancels_while_the_card_shows(self):
        self.play_episode(SEASON_1, 1)
        self.end()
        opened = len(self.drawer.named("open_drawer"))  # once in setUp, on the idle player
        self.controller.handle(Action.OK)
        self.assertEqual(self.link_items(), ["e11", "e12"])
        self.assertEqual(len(self.drawer.named("open_drawer")), opened)  # Enter did not open it
        self.ready("http://vod/e12.mkv")
        self.end()  # the last episode of a season whose next is unloaded
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1)]
        self.catalog.episodesReady.emit("s9", "2")
        self.assertEqual(len(self.next_card.named("show_next")), 2)
        self.transport.visible = False
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.link_items(), ["e11", "e12"])  # cancelled: nothing more plays
        self.assertTrue(self.transport.visible)  # on the final frame, with the transport
        self.assertEqual(self.window.calls, [])

    def test_a_season_boundary_waits_for_the_next_season_then_offers_its_first_episode(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.assertIn(("episodes", "s9", "2"), self.catalog.requests)
        self.assertEqual(self.next_card.named("show_next"), [])  # not until it loads
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1), episode(SEASON_2, 2)]
        self.catalog.episodesReady.emit("s9", "2")
        self.assertEqual(self.next_card.named("show_next"), [("Episode 1", "S2 · E1", "episode")])
        self.next_card.activate()
        self.assertEqual(self.link_items(), ["e12", "e21"])

    def test_a_failed_next_season_toasts_and_shows_no_card(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.catalog.episodesFailed.emit("s9", "2", "Couldn't reach the portal")
        self.assertEqual(self.toast.messages()[-1], ("Couldn't load the next season", "error"))
        self.assertEqual(self.next_card.named("show_next"), [])
        self.catalog.episodesReady.emit("s9", "2")  # a late answer changes nothing
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.link_items(), ["e12"])

    def test_a_season_still_empty_after_loading_is_not_asked_for_again(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.catalog.episodesReady.emit("s9", "2")  # yet the catalog has none for it
        self.assertEqual(self.catalog.requests.count(("episodes", "s9", "2")), 1)
        self.assertEqual(self.toast.messages()[-1], ("Couldn't load the next season", "error"))
        self.assertEqual(self.next_card.named("show_next"), [])

    def test_the_last_episode_of_the_last_season_offers_nothing(self):
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1)]
        self.play_episode(SEASON_2, 1)
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.toast.messages()[-1], ("Finished", "info"))
        self.assertEqual(self.link_items(), ["e21"])

    def test_the_transport_next_plays_at_once_without_the_card(self):
        self.play_episode(SEASON_1, 1)
        self.transport.nextRequested.emit()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.link_items(), ["e11", "e12"])

    def test_autoplay_off_only_finishes(self):
        self.config.auto_play_episodes = False
        self.play_episode(SEASON_1, 1)
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.toast.messages()[-1], ("Finished", "info"))

    def test_start_over_plays_from_the_beginning(self):
        self.watch.resume["Home:episode:e11"] = 90_000
        self.play_episode(SEASON_1, 1, start_over=True)
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 0)

    def test_an_episode_without_its_series_has_no_next(self):
        item = episode(SEASON_1, 1)
        self.controller.play_vod(item, "episode", play_context())  # Continue in Series
        self.ready(item["cmd"])
        self.assertEqual(self.transport.named("set_next_available"), [(False,)])
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])

    def test_external_players_never_autoplay(self):
        self.config.play_in_vlc = True
        self.play_episode(SEASON_1, 1)
        self.assertEqual(self.external.calls, [("vlc", "http://vod/e11.mkv")])
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.link_items(), ["e11"])


class MovieAutoplayTests(AutoplayCase):
    def test_a_movie_ends_and_the_card_names_the_next_in_its_genre(self):
        films = [movie(1), movie(2), movie(3)]
        self.controller.play_vod(films[1], "movie", play_context(genre_items=films))
        self.assertEqual(self.catalog.links[-1][2:], ("movies", False))
        self.ready("http://vod/m2.mkv")
        self.assertEqual(self.transport.named("set_next_available"), [(True,)])
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [("Film 3", "Films", "movie")])
        self.next_card.activate()
        self.assertEqual(self.link_items(), ["m2", "m3"])

    def test_auto_play_movies_off_only_finishes(self):
        self.config.auto_play_movies = False
        films = [movie(1), movie(2)]
        self.controller.play_vod(films[0], "movie", play_context(genre_items=films))
        self.ready("http://vod/m1.mkv")
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [])
        self.assertEqual(self.toast.messages()[-1], ("Finished", "info"))

    def test_play_vod_stops_the_live_channel_through_the_release_gate(self):
        self.play(0)
        self.session.holding = True  # libVLC still holds the channel after the stop
        self.controller.play_vod(movie(1), "movie", play_context())
        self.ready("http://vod/m1.mkv")
        self.assertEqual(self.played_urls(), ["http://stream/1"])  # not before the release
        self.session.let_go()
        self.assertEqual(self.played_urls(), ["http://stream/1", "http://vod/m1.mkv"])


class FavoriteTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.nav.reset_to("live", "10")
        self.channels = self.nav.children(self.nav.path[-1])

    def test_ctrl_d_toggles_the_highlighted_drawer_channel(self):
        self.drawer.visible = True
        self.drawer.highlight = self.channels[2]  # Three
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["Three"])
        self.assertEqual(self.config.saves, 1)
        self.assertEqual(self.toast.messages()[-1], ("Added to favorites", "info"))
        pinned = self.nav.children(self.nav.path[1])[0]
        self.assertEqual((pinned.key, pinned.count), (FAVORITES_GROUP, 1))
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, [])
        self.assertEqual(self.toast.messages()[-1], ("Removed from favorites", "info"))

    def test_ctrl_d_with_the_drawer_closed_toggles_the_current_channel(self):
        self.play(1)
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["Two"])

    def test_only_live_items_become_favorites_from_the_drawer(self):
        self.drawer.visible = True
        self.drawer.highlight = self.nav.path[-1]  # a group
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, [])


class PlayerButtonTests(ControllerTestCase):
    """The player controls' buttons for the player's keys (every key has a control on screen)."""

    def setUp(self):
        super().setUp()
        self.controller.start()
        self.nav.reset_to("live", "10")

    def test_the_guide_button_opens_the_guide(self):
        self.play(1)
        self.transport.guideRequested.emit()
        self.assertEqual(self.guide.named("open_guide"), [(CHANNELS, 1)])

    def test_the_previous_channel_button_goes_back_a_channel(self):
        self.play(0)
        self.play(2)
        self.transport.previousRequested.emit()
        self.assertEqual(self.requested(), ["One", "Three", "One"])

    def test_the_channels_name_shows_its_banner(self):
        self.play(1)
        self.banner.dismiss()
        shown = len(self.banner.named("show_info"))
        self.transport.infoRequested.emit()
        self.assertEqual(len(self.banner.named("show_info")), shown + 1)
        self.assertEqual(self.banner.named("show_info")[-1][0].name, "Two")

    def test_the_star_adds_and_removes_the_channel_playing(self):
        self.play(1)
        self.drawer.visible = True  # Ctrl+D would take its highlight; the star never does
        self.drawer.highlight = self.nav.children(self.nav.path[-1])[3]
        self.transport.favoriteRequested.emit()
        self.assertEqual(self.config.favorites, ["Two"])
        self.assertEqual(self.transport.named("set_favorite")[-1], (True,))
        self.assertEqual(self.toast.messages()[-1], ("Added to favorites", "info"))
        self.transport.favoriteRequested.emit()
        self.assertEqual(self.config.favorites, [])
        self.assertEqual(self.transport.named("set_favorite")[-1], (False,))

    def test_the_star_shows_whether_the_channel_is_a_favorite(self):
        self.config.favorites = ["Two"]
        self.play(1)
        self.controller.mouse_moved()  # the transport shows the channel
        self.assertEqual(self.transport.named("set_favorite")[-1], (True,))
        self.play(2)
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("set_favorite")[-1], (False,))

    def test_a_favorite_chosen_elsewhere_lights_the_star(self):
        self.play(1)
        self.controller.handle(Action.FAVORITE)  # Ctrl+D on the channel playing
        self.assertEqual(self.transport.named("set_favorite")[-1], (True,))

    def test_the_star_does_nothing_during_a_movie(self):
        self.controller.play_vod(movie(1), "movie", play_context())
        self.ready("http://vod/m1.mkv")
        self.transport.favoriteRequested.emit()
        self.assertEqual(self.config.favorites, [])


class ContextMenuTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.nav.reset_to("live", "10")
        self.channels = self.nav.children(self.nav.path[-1])
        QGuiApplication.clipboard().setText("")

    def menu_for(self, node):
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append((menu, pos))):
            self.drawer.contextRequested.emit(node, QPoint(40, 50))
        if not shown:
            return None
        menu, pos = shown[0]
        self.addCleanup(menu.deleteLater)
        self.assertEqual(pos, QPoint(40, 50))
        return menu

    @staticmethod
    def texts(menu):
        return [a.text() for a in menu.actions() if not a.isSeparator()]

    @staticmethod
    def choose(menu, text):
        next(a for a in menu.actions() if a.text() == text).trigger()

    def test_a_channel_offers_favorites_external_players_and_its_link(self):
        menu = self.menu_for(self.channels[0])
        self.assertEqual(
            self.texts(menu), ["Add to favorites", "Play in VLC", "Play in MPV", "Copy stream link"]
        )
        self.choose(menu, "Add to favorites")
        self.assertEqual(self.config.favorites, ["One"])
        self.assertEqual(self.texts(self.menu_for(self.channels[0]))[0], "Remove from favorites")

    def test_copy_stream_link_sets_the_clipboard_once_the_link_is_ready(self):
        self.choose(self.menu_for(self.channels[0]), "Copy stream link")
        self.assertEqual(self.requested(), ["One"])
        self.assertEqual(QGuiApplication.clipboard().text(), "")
        self.catalog.linkReady.emit(1, "http://stream/1?token=abc")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://stream/1?token=abc")
        self.assertEqual(self.toast.messages()[-1], ("Link copied", "info"))
        self.assertEqual(self.session.plays, [])  # a link request opens no stream

    def test_link_requests_are_serialized_beside_the_tune(self):
        self.tune(1)  # Two: the tune's link is in flight
        self.choose(self.menu_for(self.channels[2]), "Copy stream link")
        self.choose(self.menu_for(self.channels[3]), "Copy stream link")
        self.assertEqual(self.requested(), ["Two", "Three"])  # Four waits for Three
        self.catalog.linkReady.emit(2, "http://stream/3")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://stream/3")
        self.assertEqual(self.requested(), ["Two", "Three", "Four"])
        self.catalog.linkReady.emit(1, "http://stream/2")  # the tune still plays its channel
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        self.catalog.linkFailed.emit(3, "No stream")
        self.assertEqual(self.toast.messages()[-1], ("Four: No stream", "error"))

    def harbor_nodes(self):
        """Three and Four of another provider than the one the list shows."""
        return [
            NavNode("item", str(c["id"]), c["name"], {**c, "_provider": "Harbor IPTV"})
            for c in CHANNELS[2:4]
        ]

    def test_menu_links_waiting_for_a_providers_setup_are_all_asked_once_it_is_ready(self):
        # Of two menu actions, the one waiting behind the other was
        # dropped without a word when their provider's setup ended.
        self.choose(self.menu_for(self.channels[2]), "Copy stream link")
        self.choose(self.menu_for(self.channels[3]), "Copy stream link")  # waits behind Three
        self.catalog.hub_ready("Home")  # a setup ended: Three's answer never comes
        self.assertEqual(self.requested(), ["Three", "Three"])  # asked again, first
        self.catalog.linkReady.emit(2, "http://stream/3")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://stream/3")
        self.assertEqual(self.requested(), ["Three", "Three", "Four"])  # then the next, in order
        self.catalog.linkReady.emit(3, "http://stream/4")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://stream/4")

    def test_menu_links_of_a_provider_whose_setup_failed_are_dropped_and_said_once(self):
        three, four = self.harbor_nodes()
        self.choose(self.menu_for(three), "Copy stream link")
        self.choose(self.menu_for(four), "Copy stream link")
        self.assertEqual(self.requested(), ["Three"])
        self.catalog.catalogs.providerFailed.emit("Harbor IPTV", "Portal down")  # the hub's
        self.assertEqual(self.toast.messages(), [("Harbor IPTV: Portal down", "error")])
        self.catalog.linkFailed.emit(1, "Portal down")  # what the hub held fails: said already
        self.assertEqual(len(self.toast.messages()), 1)
        self.assertEqual(self.requested(), ["Three"])  # Four is never asked: no sign-in again
        self.assertEqual(self.catalog.setups, [])

    def test_menu_links_of_the_shown_provider_that_failed_are_said_once(self):
        self.choose(self.menu_for(self.channels[2]), "Copy stream link")
        self.choose(self.menu_for(self.channels[3]), "Copy stream link")
        self.catalog.hub_failed("Home", "Portal down")  # the flow says it
        self.assertEqual(self.toast.messages(), [("Home: Portal down", "error")])
        self.assertEqual(self.requested(), ["Three"])

    def test_play_in_vlc_stops_the_in_app_stream_first(self):
        self.play(0)
        stops = self.session.stops
        self.choose(self.menu_for(self.channels[1]), "Play in VLC")
        self.catalog.linkReady.emit(2, "http://stream/2")
        self.assertEqual(self.session.stops, stops + 1)
        self.assertEqual(self.external.calls, [("vlc", "http://stream/2")])

    def test_play_in_vlc_supersedes_a_tune_on_its_way(self):
        self.play(0)
        self.tune(2)  # Three: its link is still on its way
        self.choose(self.menu_for(self.channels[1]), "Play in VLC")
        self.catalog.linkReady.emit(3, "http://stream/2")
        self.catalog.linkReady.emit(2, "http://stream/3")  # too late: VLC plays Two
        self.assertEqual(self.external.calls, [("vlc", "http://stream/2")])
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_a_stale_play_in_vlc_never_overrides_a_newer_zap(self):
        self.play(0)  # One plays in-app
        self.choose(self.menu_for(self.channels[2]), "Play in VLC")  # Three: link 2 pending
        self.tune(1)  # then a zap to Two: link 3
        self.catalog.linkReady.emit(3, "http://stream/2")  # Two plays
        self.catalog.linkReady.emit(2, "http://stream/3")  # the older external choice lands
        self.assertEqual(self.external.calls, [])
        self.assertEqual(self.played_urls(), ["http://stream/1", "http://stream/2"])
        self.assertEqual(self.session.stops, 1)  # One, for Two; Two keeps playing

    def test_a_copy_is_never_superseded(self):
        self.choose(self.menu_for(self.channels[2]), "Copy stream link")
        self.tune(1)
        self.catalog.linkReady.emit(1, "http://stream/3")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://stream/3")

    def test_a_missing_player_toasts_and_keeps_playing(self):
        self.play(0)
        stops = self.session.stops
        self.external.missing = {"mpv"}
        self.choose(self.menu_for(self.channels[1]), "Play in MPV")
        self.assertEqual(self.requested(), ["One"])  # no link was even asked for
        self.assertEqual(self.session.stops, stops)
        self.assertEqual(self.toast.messages()[-1], ("MPV Media Player is not installed.", "error"))

    def test_an_overtaken_tune_gives_the_channel_back_to_what_plays(self):
        self.play(0)  # One plays
        self.tune(1)  # Two: its link is on its way
        self.choose(self.menu_for(self.channels[2]), "Play in VLC")  # Three overtakes it
        self.catalog.linkFailed.emit(3, "portal said no")
        self.catalog.linkReady.emit(2, "http://stream/2")  # dropped: One goes on
        self.assertEqual(self.played_urls(), ["http://stream/1"])
        self.controller.handle(Action.INFO)
        self.assertEqual(self.banner.info.name, "One")
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["One"])
        self.clock.now += 5
        self.controller.handle(Action.CH_NEXT)  # from One, not from the dropped Two
        self.assertEqual(self.requested()[-1], "Two")

    def test_after_an_external_play_the_channel_is_the_played_one(self):
        self.play(0)
        self.tune(1)
        self.choose(self.menu_for(self.channels[2]), "Play in VLC")
        self.catalog.linkReady.emit(2, "http://stream/2")  # the overtaken tune: dropped
        self.catalog.linkReady.emit(3, "http://stream/3")
        self.assertEqual(self.external.calls, [("vlc", "http://stream/3")])
        self.controller.handle(Action.INFO)
        self.assertEqual(self.banner.info.name, "Three")

    def test_a_superseded_side_play_never_asks_for_its_link(self):
        self.play(0)
        self.choose(self.menu_for(self.channels[1]), "Copy stream link")  # link 2
        self.choose(self.menu_for(self.channels[2]), "Play in VLC")  # waits behind it
        self.tune(3)  # a newer choice: Four (link 3)
        self.catalog.linkReady.emit(2, "http://stream/2")
        self.assertEqual(self.requested(), ["One", "Two", "Four"])
        self.assertEqual(self.external.calls, [])

    def test_a_failed_external_player_toasts(self):
        self.external.result = (False, "MPV is not installed\nsearched: /usr/bin")
        self.choose(self.menu_for(self.channels[1]), "Play in MPV")
        self.catalog.linkReady.emit(1, "http://stream/2")
        self.assertEqual(self.external.calls, [("mpv", "http://stream/2")])
        self.assertEqual(self.toast.messages()[-1], ("MPV is not installed", "error"))

    def test_a_movie_has_no_favorites_entry_and_a_series_no_menu(self):
        self.nav.reset_to("movies")
        film = NavNode("item", "m1", "Film 1", movie(1))
        self.assertEqual(
            self.texts(self.menu_for(film)), ["Play in VLC", "Play in MPV", "Copy stream link"]
        )
        self.nav.reset_to("series")
        self.assertIsNone(self.menu_for(NavNode("item", "s9", "Harbour Lights", SERIES)))


def watched(provider, channel, kind="channel"):
    """A watch-history entry as ConfigManager.add_to_watch_history writes it."""
    return {
        "content_id": f"{provider}:{channel['id']}",
        "item_data": dict(channel),
        "item_type": kind,
        "link": f"http://tv/{channel['id']}",
        "provider_name": provider,
        "timestamp": "2026-10-19T20:00:00",
        "name": channel["name"],
    }


class RecentsTests(ControllerTestCase):
    """A channel taken out of ⟲ Recent: its row's menu, Delete, and Clear Recents."""

    menu_for = ContextMenuTests.menu_for
    texts = staticmethod(ContextMenuTests.texts)
    choose = staticmethod(ContextMenuTests.choose)

    def setUp(self):
        super().setUp()
        self.film = watched("Home", {"id": 9, "name": "Film"}, "movie")
        self.home_two, self.backup_two = watched("Home", CHANNELS[1]), watched(
            "Backup", CHANNELS[1]
        )
        self.home_one, self.older_two = watched("Home", CHANNELS[0]), watched("Home", CHANNELS[1])
        self.history = [self.home_two, self.film, self.backup_two, self.home_one, self.older_two]
        self.config.watch_history = list(self.history)
        self.controller.watch = WatchState(self.config)  # the real history keeper: it saves
        self.controller.start()
        self.nav.reset_to("live", RECENT_GROUP)
        self.rows = self.nav.children(self.nav.path[-1])

    def shown(self):
        return [(provider_of(n.payload), n.title) for n in self.nav.children(self.nav.path[-1])]

    def test_a_recent_channel_s_menu_takes_it_out_and_saves_the_history(self):
        self.assertEqual(self.shown(), [("Home", "Two"), ("Backup", "Two"), ("Home", "One")])
        menu = self.menu_for(self.rows[0])
        self.assertEqual(
            self.texts(menu),
            [
                "Add to favorites",
                "Remove from Recents",
                "Clear Recents",
                "Play in VLC",
                "Play in MPV",
                "Copy stream link",
            ],
        )
        saves = self.config.saves
        self.choose(menu, "Remove from Recents")
        # Both its plays at that provider go; the same channel at another provider, the other
        # channels and the film stay, as they were (older builds read the same entries).
        self.assertEqual(self.config.watch_history, [self.film, self.backup_two, self.home_one])
        self.assertEqual(self.config.saves, saves + 1)
        self.assertEqual(self.shown(), [("Backup", "Two"), ("Home", "One")])
        self.assertEqual(self.toast.messages()[-1], ("Removed from Recents", "info"))

    def test_a_channel_elsewhere_offers_no_recents_entries(self):
        self.nav.reset_to("live", "10")
        two = self.nav.children(self.nav.path[-1])[1]
        texts = self.texts(self.menu_for(two))
        self.assertNotIn("Remove from Recents", texts)
        self.assertNotIn("Clear Recents", texts)

    def test_clear_recents_asks_once_more_then_empties_the_list_and_goes_up(self):
        menu = self.menu_for(self.rows[2])
        sub = next(a for a in menu.actions() if a.text() == "Clear Recents").menu()
        self.assertEqual(self.texts(sub), ["Clear all 3 channels"])
        self.assertEqual(self.config.watch_history, self.history)  # the submenu only asks
        saves = self.config.saves
        self.choose(sub, "Clear all 3 channels")
        self.assertEqual(self.config.watch_history, [self.film])  # the Library's Continue stays
        self.assertEqual(self.config.saves, saves + 1)
        self.assertEqual(self.toast.messages()[-1], ("Recents cleared", "info"))
        # Its row is gone from the genres: the list is back on them.
        self.assertEqual([n.layer for n in self.nav.path], ["source", "section"])
        self.assertNotIn(RECENT_GROUP, [n.key for n in self.nav.children(self.nav.path[-1])])

    def test_the_recent_row_itself_offers_clear_recents(self):
        recent = next(n for n in self.nav.children(self.nav.path[1]) if n.key == RECENT_GROUP)
        menu = self.menu_for(recent)
        self.assertEqual(self.texts(menu), ["Clear Recents"])
        sub = menu.actions()[0].menu()
        self.choose(sub, "Clear all 3 channels")
        self.assertEqual(self.config.watch_history, [self.film])
        self.assertIsNone(self.menu_for(recent))  # nothing left to clear

    def test_delete_takes_the_highlighted_recent_channel_out(self):
        self.drawer.visible = True
        self.drawer.highlight = self.rows[1]  # Two, at Backup
        self.controller.handle(Action.FORGET)
        self.assertEqual(
            self.config.watch_history, [self.home_two, self.film, self.home_one, self.older_two]
        )
        self.assertEqual(self.toast.messages()[-1], ("Removed from Recents", "info"))
        history = list(self.config.watch_history)
        self.drawer.found = True  # the find lists what it found: Delete is the find's then
        self.drawer.highlight = self.rows[0]
        self.controller.handle(Action.FORGET)
        self.assertEqual(self.config.watch_history, history)
        self.drawer.found = False
        self.nav.reset_to("live", "10")  # a genre's channel: nothing to take out
        self.drawer.highlight = self.nav.children(self.nav.path[-1])[0]
        self.controller.handle(Action.FORGET)
        self.assertEqual(self.config.watch_history, history)
        self.nav.reset_to("live", RECENT_GROUP)
        self.drawer.highlight, self.drawer.visible = self.rows[0], False  # the list is closed
        self.controller.handle(Action.FORGET)
        self.assertEqual(self.config.watch_history, history)


class ExternalButtonTests(ControllerTestCase):
    """The player controls send what plays now to VLC or MPV, the way the
    channel list's right-click does: the in-app stream is released first."""

    BUTTON = QPoint(300, 600)  # the button's top-left, as the transport says it

    def setUp(self):
        super().setUp()
        self.controller.start()

    def open_menu(self):
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append((menu, pos))):
            self.transport.externalRequested.emit(self.BUTTON)
        if not shown:
            return None
        menu, pos = shown[0]
        self.addCleanup(menu.deleteLater)
        self.assertEqual(pos.x(), self.BUTTON.x())
        self.assertLess(pos.y(), self.BUTTON.y())  # it opens upwards, over the video
        return menu

    def choose(self, text):
        menu = self.open_menu()
        self.assertEqual(ContextMenuTests.texts(menu), ["Play in VLC", "Play in MPV"])
        ContextMenuTests.choose(menu, text)

    def test_a_live_channel_goes_to_vlc_once_the_in_app_stream_is_released(self):
        self.play(0)
        stops = self.session.stops
        self.choose("Play in VLC")
        self.assertEqual(self.catalog.links[-1][1:], (CHANNELS[0], "live", False))
        self.assertEqual(self.session.stops, stops)  # nothing stops before the link is there
        self.ready("http://stream/1?fresh")
        self.assertEqual(self.session.stops, stops + 1)
        self.assertEqual(self.external.calls, [("vlc", "http://stream/1?fresh")])
        self.controller.handle(Action.INFO)
        self.assertEqual(self.banner.info.name, "One")

    def test_a_movie_goes_to_mpv(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.choose("Play in MPV")
        self.assertEqual(self.catalog.links[-1][1:], (VodTests.MOVIE, "movies", False))
        self.ready("http://vod/heat.mkv?fresh")
        self.assertEqual(self.external.calls, [("mpv", "http://vod/heat.mkv?fresh")])

    def test_an_episode_asks_for_an_episode_link(self):
        episode = {"id": "s", "cmd": "x", "series": 2, "name": "Pilot"}
        self.controller.play_vod(episode, "episode", play_context())
        self.ready("http://vod/pilot.mkv")
        self.choose("Play in VLC")
        self.assertEqual(self.catalog.links[-1][1:], (episode, "series", True))
        self.ready("http://vod/pilot.mkv?fresh")
        self.assertEqual(self.external.calls, [("vlc", "http://vod/pilot.mkv?fresh")])

    def test_a_missing_player_toasts_and_keeps_playing(self):
        self.play(0)
        stops = self.session.stops
        self.external.missing = {"vlc"}
        self.choose("Play in VLC")
        self.assertEqual(self.requested(), ["One"])  # no link was even asked for
        self.assertEqual(self.session.stops, stops)
        self.assertEqual(self.toast.messages()[-1], ("VLC Media Player is not installed.", "error"))

    def test_a_local_file_goes_out_as_it_is(self):
        path = Path("clip.mkv")
        self.controller.play_file(str(path))
        self.choose("Play in VLC")
        self.assertEqual(self.catalog.links, [])  # a file is no provider's: no link to ask for
        self.assertEqual(self.external.calls, [("vlc", path.absolute().as_uri())])
        # Nor its history: record_play keeps the watch history and last_watched.
        self.assertEqual(self.watch.plays, [])

    def test_with_nothing_played_there_is_no_menu(self):
        self.assertIsNone(self.open_menu())
        self.assertEqual(self.catalog.links, [])


class LibraryMenuTests(ControllerTestCase):
    """A right-click on a Library title or episode offers VLC, MPV and its
    link, as a channel's menu does, while the Library shows."""

    def setUp(self):
        super().setUp()
        self.controller.start()
        self.controller.stop_for_library()  # the window shows the Library

    def menu_for(self, item, section):
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append((menu, pos))):
            self.controller.show_library_menu(item, section, QPoint(40, 50))
        menu, pos = shown[0]
        self.addCleanup(menu.deleteLater)
        self.assertEqual(pos, QPoint(40, 50))
        return menu

    def test_a_movie_offers_the_players_and_its_link(self):
        menu = self.menu_for(VodTests.MOVIE, "movies")
        self.assertEqual(
            ContextMenuTests.texts(menu), ["Play in VLC", "Play in MPV", "Copy stream link"]
        )
        ContextMenuTests.choose(menu, "Play in VLC")
        self.assertEqual(self.catalog.links[-1][1:], (VodTests.MOVIE, "movies", False))
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.external.calls, [("vlc", "http://vod/heat.mkv")])
        self.assertEqual(self.watch.plays[-1][1:3], (VodTests.MOVIE, "movie"))
        self.assertEqual(self.session.plays, [])  # nothing plays in the app

    def test_an_episode_asks_for_an_episode_link(self):
        episode = {"id": "s", "cmd": "x", "series": 2, "name": "Pilot"}
        ContextMenuTests.choose(self.menu_for(episode, "series"), "Play in MPV")
        self.assertEqual(self.catalog.links[-1][1:], (episode, "series", True))
        self.ready("http://vod/pilot.mkv")
        self.assertEqual(self.external.calls, [("mpv", "http://vod/pilot.mkv")])
        self.assertEqual(self.watch.plays[-1][1:3], (episode, "episode"))

    def test_its_messages_show_on_the_librarys_toast(self):
        # The player's page, and its toast, are hidden while the Library shows.
        ContextMenuTests.choose(self.menu_for(VodTests.MOVIE, "movies"), "Copy stream link")
        self.ready("http://vod/heat.mkv")
        self.assertEqual(QGuiApplication.clipboard().text(), "http://vod/heat.mkv")
        self.assertEqual(self.library_toast.messages()[-1], ("Link copied", "info"))
        self.external.missing = {"vlc"}
        ContextMenuTests.choose(self.menu_for(VodTests.MOVIE, "movies"), "Play in VLC")
        self.assertEqual(
            self.library_toast.messages()[-1], ("VLC Media Player is not installed.", "error")
        )
        ContextMenuTests.choose(self.menu_for(VodTests.MOVIE, "movies"), "Play in MPV")
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "No stream")
        self.assertEqual(self.library_toast.messages()[-1], ("Heat: No stream", "error"))
        self.external.result = (False, "MPV could not start\ndetails")
        ContextMenuTests.choose(self.menu_for(VodTests.MOVIE, "movies"), "Play in MPV")
        self.ready("http://vod/heat.mkv")
        self.assertEqual(self.library_toast.messages()[-1], ("MPV could not start", "error"))
        self.assertEqual(self.toast.messages(), [])


class CardRoutingTests(AutoplayCase):
    """While the Next card is up (the episode ended), zap keys never leave the series."""

    def setUp(self):
        super().setUp()
        self.catalog.episode_map[("s9", "1")] = [episode(SEASON_1, 1), episode(SEASON_1, 2)]
        self.play(1)  # a live channel first: the zap context has channels
        self.play_episode(SEASON_1, 1)
        self.end()
        self.assertTrue(self.next_card.visible)
        self.links = len(self.catalog.links)

    def test_up_down_and_the_wheel_change_the_volume(self):
        self.controller.handle(Action.CH_PREV)  # ↑
        self.assertEqual(self.session.level, 55)
        self.controller.handle(Action.CH_NEXT)
        self.controller.wheel(1, NO_MODS)
        self.assertEqual(self.session.level, 45)
        self.assertEqual(len(self.catalog.links), self.links)
        self.assertTrue(self.next_card.visible)

    def test_digits_previous_and_g_do_nothing(self):
        self.controller.handle(Action.DIGIT, "3")
        self.controller.handle(Action.PREVIOUS)
        self.controller.handle(Action.GUIDE)
        self.assertEqual(self.banner.named("show_digits"), [])
        self.assertEqual(self.guide.named("open_guide"), [])
        elapse(60)  # the zap's digit timer would have tuned by now
        self.assertEqual(len(self.catalog.links), self.links)
        self.assertTrue(self.next_card.visible)

    def test_ctrl_d_keeps_the_last_channel_as_it_was(self):
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, [])

    def test_after_cancelling_the_keys_still_stay_on_demand(self):
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.next_card.visible)
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(len(self.catalog.links), self.links)


class ErrorStateRoutingTests(AutoplayCase):
    def test_a_failed_episode_keeps_the_zap_keys(self):
        self.catalog.episode_map[("s9", "1")] = [episode(SEASON_1, 1)]
        self.play(1)
        self.play_episode(SEASON_1, 1)
        self.session.stateChanged.emit("error")
        links = len(self.catalog.links)
        self.controller.handle(Action.CH_NEXT)
        self.controller.wheel(1, NO_MODS)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.session.level, 40)


class SeasonWaitTests(AutoplayCase):
    def setUp(self):
        super().setUp()
        self.catalog.episode_map[("s9", "1")] = [episode(SEASON_1, 1), episode(SEASON_1, 2)]

    def test_the_wait_for_the_next_season_shows_a_hint_until_it_loads(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.assertEqual(self.toast.hint, "Loading next season…")
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1)]
        self.catalog.episodesReady.emit("s9", "2")
        self.assertIsNone(self.toast.hint)
        self.assertEqual(len(self.next_card.named("show_next")), 1)

    def test_escape_cancels_the_wait(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.controller.handle(Action.ESCAPE)
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.window.calls, [])  # Esc ended the wait, not fullscreen
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1)]
        self.catalog.episodesReady.emit("s9", "2")
        self.assertEqual(self.next_card.named("show_next"), [])

    def test_a_provider_setups_hint_comes_first_and_the_wait_shows_after_it(self):
        self.controller.select_provider("Home")
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.play_episode(SEASON_1, 2)
        self.end()  # the wait starts while the setup still loads
        self.assertEqual(self.toast.hint, "Loading Home…")
        self.catalog.hub_ready("Home")
        self.catalog.sectionReady.emit("live")  # the setup ends; the wait goes on
        self.assertEqual(self.toast.hint, "Loading next season…")
        self.catalog.episode_map[("s9", "2")] = [episode(SEASON_2, 1)]
        self.catalog.episodesReady.emit("s9", "2")
        self.assertIsNone(self.toast.hint)
        self.assertEqual(len(self.next_card.named("show_next")), 1)

    def test_a_wait_ending_during_a_setup_leaves_its_hint(self):
        self.controller.select_provider("Backup")
        self.play_episode(SEASON_1, 2)
        self.end()
        self.catalog.episodesFailed.emit("s9", "2", "boom")
        self.assertEqual(self.toast.hint, "Loading Backup…")
        self.assertEqual(self.toast.messages()[-1], ("Couldn't load the next season", "error"))

    def test_a_failed_wait_clears_the_hint(self):
        self.play_episode(SEASON_1, 2)
        self.end()
        self.catalog.episodesFailed.emit("s9", "2", "boom")
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.toast.messages()[-1], ("Couldn't load the next season", "error"))


class GuideKeyFixTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.play(1)
        self.controller.handle(Action.GUIDE)

    def test_digits_and_channel_keys_stay_in_the_guide(self):
        self.guide.current = CHANNELS[1]
        self.controller.handle(Action.DIGIT, "4")
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.guide.named("move_by"), [(2,), (1,)])  # to Four, then one on
        self.assertEqual(self.banner.named("show_digits"), [])
        self.assertTrue(self.guide.visible)
        self.assertEqual(self.requested(), ["Two"])

    def test_ctrl_d_toggles_the_highlighted_row(self):
        self.guide.current = CHANNELS[3]
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["Four"])


class FavoriteFallbackTests(ControllerTestCase):
    def test_a_channel_without_a_name_is_kept_by_its_title(self):
        self.controller.start()
        self.nav.reset_to("live", "10")
        self.drawer.visible = True
        self.drawer.highlight = NavNode("item", "7", "Seven", {"id": 7, "title": "Seven"})
        self.controller.handle(Action.FAVORITE)
        self.assertEqual(self.config.favorites, ["Seven"])


class MouseLayerTests(ControllerTestCase):
    def test_back_opens_the_closed_drawer(self):
        self.controller.start()
        self.controller.mouse_back()
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)
        self.controller.mouse_back()
        self.assertEqual(self.drawer.named("left"), [()])


class LibraryTests(AutoplayCase):
    """The window's Library hooks: stop for it, back to live TV, its plays."""

    LAST = {"provider_name": "Home", "group_id": "10", "item": CHANNELS[2]}

    def test_stop_for_library_stops_what_plays_and_closes_the_player_overlays(self):
        self.play(0)
        self.controller.handle(Action.GUIDE)
        self.assertTrue(self.guide.visible)
        self.drawer.visible = self.column.visible = True
        stops = self.session.stops
        self.controller.stop_for_library()
        self.assertEqual(self.session.stops, stops + 1)
        self.assertFalse(self.guide.visible)
        self.assertEqual((self.drawer.visible, self.column.visible), (False, False))
        self.assertTrue(self.transport.named("dismiss") and self.banner.named("dismiss"))
        self.assertFalse(self.controller.vod_current())

    def test_stop_for_library_gives_a_direct_plays_slot_back_once_vlc_let_go(self):
        self.config.selected_provider_name = "Home"
        self.play(0)  # a direct channel holds the main slot too (a PiP must see it taken)
        budget = self.controller.budget
        self.assertEqual(budget.holders("Home"), ["main"])
        self.session.holding = True  # libVLC ends the provider's stream on its worker
        self.controller.stop_for_library()
        self.assertEqual(budget.holders("Home"), ["main"])  # still open upstream
        self.session.let_go()
        self.assertEqual(budget.holders("Home"), [])

    def test_stop_for_library_closes_the_next_card(self):
        films = [movie(1), movie(2)]
        self.controller.play_vod(films[0], "movie", play_context(genre_items=films))
        self.ready("http://vod/m1.mkv")
        self.end()
        self.assertTrue(self.next_card.visible)
        self.controller.stop_for_library()
        self.assertFalse(self.next_card.visible)
        self.assertEqual(self.link_items(), ["m1"])  # nothing follows it

    def test_a_link_still_on_its_way_never_opens_in_the_library(self):
        self.tune(1)
        self.controller.stop_for_library()
        self.ready("http://stream/2")
        self.assertEqual(self.session.plays, [])

    def test_in_the_library_a_provider_change_neither_tunes_nor_opens_the_drawer(self):
        self.config.last_live_channel = dict(self.LAST, provider_name="Backup")
        self.controller.stop_for_library()
        self.drawer.calls.clear()
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_resume_live_tunes_the_last_channel_in_its_group(self):
        self.config.last_live_channel = dict(self.LAST)
        self.controller.stop_for_library()
        self.controller.resume_live()
        self.assertEqual(self.requested(), ["Three"])
        self.ready("http://stream/3")
        self.assertEqual(self.played_urls(), ["http://stream/3"])
        self.assertEqual(self.zap.context.group_key, "10")

    def test_resume_live_tunes_even_when_startup_does_not(self):
        self.config.start_with_last_channel = False  # that setting is about launching QiTV
        self.config.last_live_channel = dict(self.LAST)
        self.controller.stop_for_library()
        self.controller.resume_live()
        self.assertEqual(self.requested(), ["Three"])

    def test_resume_live_without_a_last_channel_opens_the_drawer(self):
        self.controller.stop_for_library()
        self.drawer.calls.clear()
        self.controller.resume_live()
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)

    def test_a_provider_that_failed_in_the_library_shows_its_retry_row_back_on_tv(self):
        self.config.last_live_channel = dict(self.LAST, provider_name="Backup")
        self.controller.stop_for_library()
        self.drawer.calls.clear()
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.hub_failed("Backup", "Handshake failed")
        self.assertEqual(self.drawer.named("open_drawer"), [])  # not behind the Library
        requests = list(self.catalog.requests)
        self.controller.resume_live()
        self.assertEqual(self.drawer.named("open_drawer"), [(None, "left")])
        self.assertEqual([(n.layer, n.key) for n in self.nav.path], [("source", "Backup")])
        self.assertEqual((self.catalog.links, self.catalog.requests), ([], requests))

    def test_resume_live_during_a_provider_switch_tunes_once_it_is_ready(self):
        self.config.start_with_last_channel = False
        self.config.last_live_channel = dict(self.LAST, provider_name="Backup")
        self.controller.stop_for_library()
        self.controller.select_provider("Backup")
        self.catalog.loading = True
        self.controller.resume_live()
        self.assertEqual(self.catalog.links, [])
        self.catalog.loading = False
        self.catalog.provider = "Backup"
        self.catalog.hub_ready("Backup")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.requested(), ["Three"])

    def test_a_library_play_made_as_its_provider_becomes_ready_wins_over_the_last_channel(self):
        # The Library runs a play waiting for its provider from its own providerReady slot,
        # after the controller's (connected first): the play must survive the provider change.
        self.config.last_live_channel = dict(self.LAST, provider_name="Backup")
        film = movie(4)
        self.controller.stop_for_library()
        self.controller.select_provider("Backup")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.connect(
            lambda _name: self.controller.play_vod(film, "movie", play_context())
        )
        self.catalog.hub_ready("Backup")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.link_items(), ["m4"])
        self.ready(film["cmd"])
        self.assertEqual(self.played_urls(), [film["cmd"]])
        self.assertTrue(self.controller.vod_current())

    def test_tune_live_tunes_among_every_channel(self):
        self.controller.tune_live(CHANNELS[1])
        self.assertEqual(self.requested(), ["Two"])
        self.ready("http://stream/2")
        self.assertEqual(self.zap.context.group_key, "*")
        self.assertEqual(self.zap.context.channels, self.catalog.items("live", "*"))

    def test_tune_live_before_the_live_section_loads_zaps_once_it_does(self):
        self.controller.stop_for_library()
        live, self.catalog.live = self.catalog.live, {}
        self.controller.tune_live(CHANNELS[1])
        self.ready("http://stream/2")
        self.catalog.live = live
        self.catalog.sectionReady.emit("live")
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested(), ["Two", "Three"])

    def test_play_file_plays_a_local_file_without_a_link_or_history(self):
        self.play(0)
        plays = len(self.watch.plays)
        self.controller.play_file("/videos/Holiday clip.mp4")
        url, kwargs = self.session.plays[-1]
        self.assertEqual(
            url, Path("/videos/Holiday clip.mp4").absolute().as_uri()
        )  # a drive letter on Windows
        self.assertEqual(kwargs["kind"], PlayKind.VOD_FILE)
        self.assertEqual(len(self.catalog.links), 1)  # only the channel's
        self.assertEqual(len(self.watch.plays), plays)  # not in any provider's history
        self.assertEqual(self.transport.named("show_vod")[-1], ("Holiday clip.mp4", ""))
        self.assertTrue(self.controller.vod_current())
        self.session.positionChanged.emit(5_000, 60_000)
        self.assertEqual(self.transport.calls[-1], ("update_position", 5_000, 60_000))

    def test_vod_chosen_follows_the_newest_choice(self):
        self.controller.stop_for_library()
        self.assertTrue(self.controller.vod_chosen())  # nothing current: no live channel
        self.controller.play_vod(movie(1), "movie", play_context())
        self.assertTrue(self.controller.vod_chosen())  # its link is on its way
        self.tune(0)  # a channel chosen meanwhile (queued behind the movie's link)
        self.assertFalse(self.controller.vod_chosen())
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        self.catalog.linkFailed.emit(self.catalog.links[-1][0], "Portal down")
        self.assertTrue(self.controller.vod_chosen())  # both failed: nothing plays

    def test_a_local_file_takes_no_provider_slot(self):
        self.config.selected_provider_name = "Home"
        self.play(0)  # a direct channel holds the main slot
        budget = self.controller.budget
        self.assertEqual(budget.holders("Home"), ["main"])
        self.controller.play_file("/videos/clip.mp4")
        self.assertEqual(self.played_urls()[-1], Path("/videos/clip.mp4").absolute().as_uri())
        self.assertEqual(budget.holders("Home"), [])  # no provider stream: a PiP may use it
        self.play(1)  # the next channel takes it again
        self.assertEqual(budget.holders("Home"), ["main"])

    def test_overlay_open_names_what_esc_would_close_first(self):
        self.controller.play_vod(movie(1), "movie", play_context())
        self.ready("http://vod/m1.mkv")
        self.assertFalse(self.controller.overlay_open())
        self.drawer.visible = True
        self.assertTrue(self.controller.overlay_open())
        self.drawer.visible = False
        self.end()  # the Next card is not up without genre items: nothing else is open
        self.assertFalse(self.controller.overlay_open())


class AllProvidersLaunchTests(ControllerTestCase):
    """The real hub and TV page view over two providers, the page saved on All providers."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="qitv-all-launch-")
        self.addCleanup(directory.cleanup)
        config, registry = mixed_config(
            directory.name,
            [DEMO, HARBOR],
            {
                "Demo TV": live_content(["Demo 1", "Demo 2"], {"News": [0, 1]}),
                "Harbor IPTV": live_content(["Harbor 1", "Harbor 2"], {"41": [0, 1]}),
            },
        )
        self.addCleanup(registry._cache_writer.shutdown, wait=True)
        self.runner = HeldSetups("Harbor IPTV")  # the last channel's provider loads second
        self.factory = ScriptedFactory()
        self.hub = Catalogs(
            config,
            registry,
            runner=self.runner,
            factory=self.factory,
            make_epg=lambda manager: FakeEpg(manager._pinned_provider),
        )
        self.addCleanup(shut_hub, self.hub)
        config.selected_provider_name = "Demo TV"
        config.tv_all_providers = True
        config.start_with_last_channel = True
        harbor_one = {"id": 1, "name": "Harbor 1", "cmd": "http://127.0.0.1:9/live/1.ts"}
        config.last_live_channel = {
            "provider_name": "Harbor IPTV",
            "group_id": "41",
            "item": harbor_one,
        }
        self.config = config
        self.catalog = BrowsedCatalog(
            self.hub, config, source_key="selected_provider_name", all_key="tv_all_providers"
        )
        self.nav = NavModel(self.catalog, config)
        self.asked = []
        ask = self.hub.request_link

        def request_link(item, **kwargs):
            self.asked.append(item)
            return ask(item, **kwargs)

        self.hub.request_link = request_link
        self.controller = self.make_controller(self.column)

    def test_the_last_channel_of_the_provider_ready_second_tunes_once_and_no_drawer(self):
        # Another provider's setup no longer replaces the last channel's
        # autotune, and the drawer never opens on the idle player meanwhile.
        self.controller.start()
        QTest.qWait(20)
        self.assertEqual(self.asked, [])  # its provider still sets up
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.runner.release("Harbor IPTV")
        QTest.qWait(20)
        shown = [(provider_of(item), item["name"]) for item in self.asked]
        self.assertEqual(shown, [("Harbor IPTV", "Harbor 1")])
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.assertEqual([n.key for n in self.nav.path][:2], [ALL_PROVIDERS, "live"])
        self.assertEqual(len(self.zap.context.channels), 4)  # up/down walk every provider's
        self.assertIsNone(self.toast.hint)

    def test_a_provider_failing_while_nothing_plays_keeps_the_list_on_all_providers(self):
        self.config.last_live_channel = None
        self.controller.start()
        QTest.qWait(20)
        self.drawer.calls.clear()
        self.hub.catalog("Demo TV").providerFailed.emit("Demo TV", "Portal down")
        self.assertEqual(self.toast.messages()[-1], ("Demo TV: Portal down", "error"))
        self.assertEqual(self.nav.path[0].key, ALL_PROVIDERS)
        self.assertNotIn((None, "left"), self.drawer.named("open_drawer"))

    def test_a_last_channels_provider_failing_first_signs_in_once_and_is_said_once(self):
        # Polish: Harbor (the last channel's) fails while Demo's playlist downloads. Waiting
        # for it once Demo's channels came signed it in again, with a second identical toast.
        registry = self.hub.registry
        for name in ("Demo TV", "Harbor IPTV"):
            os.remove(registry._provider_cache_name(name))  # nothing listed yet
        self.factory.fail_setup("Harbor IPTV", "Portal down")
        self.factory.results["m3u"] = {
            "result": live_content(["Demo 1", "Demo 2"], {"News": [0, 1]}),
            "on_run": lambda: self.runner.release("Harbor IPTV"),  # it fails meanwhile
        }
        self.runner.held.add("Demo TV")
        self.controller.start()
        QTest.qWait(20)
        self.runner.release("Demo TV")  # Demo is set up and downloads its playlist
        QTest.qWait(20)
        self.assertEqual(self.factory.setups.count("Harbor IPTV"), 1)
        said = [text for text, _kind in self.toast.messages() if text.startswith("Harbor IPTV:")]
        self.assertEqual(len(said), 1)
        self.assertEqual(self.asked, [])  # nothing to tune
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)  # the idle player's list
        self.assertEqual(self.nav.path[0].key, ALL_PROVIDERS)


if __name__ == "__main__":
    unittest.main()
