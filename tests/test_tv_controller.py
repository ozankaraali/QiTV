from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QGuiApplication, QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QMenu, QWidget

from services.catalog import PROVIDER_LOADING_MESSAGE, UNREACHABLE_MESSAGE
from services.connection_budget import ConnectionBudget
from services.epg_view import Programme
from services.live.live_session import UNAVAILABLE
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.navigation import FAVORITES_GROUP, NavModel, NavNode
from services.sessions import PIP_FAILED, SessionManager, Slot
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind
from services.zapping import ZapController
from tests.test_navigation import FakeCatalog as NavCatalog
from ui.keymap import Action
from ui.live_tuner import LiveTuner
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.channel_rows import ChannelRowsModel
from ui.overlays.drawer import LOADING_TEXT, PROVIDER_ERROR_TEXT, Drawer
from ui.overlays.guide import Guide
from ui.overlays.next_card import NextCard
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import ZapBanner
from ui.pip_controls import ALREADY_LARGE, NO_PREVIOUS, OPEN_PIP_FIRST, PIP_LIVE_ONLY
from ui.tv_controller import Overlays, TVController, scrub

NOW = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
MIB = 1024 * 1024
CHANNELS = [
    {"id": i, "name": name, "number": str(i), "cmd": f"ffrt http://tv/{i}"}
    for i, name in ((1, "One"), (2, "Two"), (3, "Three"), (4, "Four"))
]
NO_MODS = Qt.KeyboardModifier.NoModifier


def wait_until(predicate, timeout_ms=2000):
    """Poll the event loop: a slow first paint can outlast a fixed wait."""
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(5)
        waited += 5
    return predicate()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


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
        self.episode_map = {}  # (series id, season id) -> episodes; missing: not loaded

    def provider_names(self):
        return list(self.names)

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

    def play(self, url, **kwargs):
        self.plays.append((url, kwargs))
        self.stateChanged.emit("opening")

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

    def subtitle_tracks(self):
        return list(self.subtitles)

    def set_subtitle_track(self, track_id):
        self.subtitle_set.append(track_id)


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

    def open(self, title, channels, index):
        self.record("open", title, channels, index)
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
    contextRequested = Signal(object, object)
    closed = Signal()
    highlight = None

    def highlighted(self):
        return self.highlight

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
    playPauseRequested = Signal()
    seekRequested = Signal(int)
    seekRelativeRequested = Signal(int)
    speedRequested = Signal(float)
    fullscreenRequested = Signal()
    audioRequested = Signal()
    subtitleRequested = Signal()
    goLiveRequested = Signal()
    programmeStartRequested = Signal()
    seekToRequested = Signal(float)
    nextRequested = Signal()
    pipRequested = Signal()
    miniPlayerRequested = Signal()

    def set_next_available(self, available):
        self.record("set_next_available", available)

    def set_pip_state(self, available, open_):
        self.record("set_pip_state", available, open_)

    def show_vod(self, title, subtitle):
        self.record("show_vod", title, subtitle)
        self.visible = True

    def show_live(self, title, subtitle):
        self.record("show_live", title, subtitle)
        self.visible = True

    def show_timeshift(self, title, subtitle):
        self.record("show_timeshift", title, subtitle)
        self.visible = True

    def update_timeline(self, state, ticks):
        self.record("update_timeline", state, ticks)

    def show_live_edge(self, lag_s, catching_up):
        self.record("show_live_edge", lag_s, catching_up)

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
    current = None

    def current_channel(self):
        return self.current

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

    def __init__(self, playback):
        super().__init__()
        self.playback = playback
        self.calls = []
        self.mode = ""
        self.running = False

    def named(self, name):
        return [c[1:] for c in self.calls if c[0] == name]

    def is_pipeline(self):
        return self.mode == "pipeline"

    def start(self, url, **kwargs):
        self.calls.append(("start", url, kwargs))
        self.running, self.mode = True, ""

    def stop(self):
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

    def test_provider_change_drops_pending_tunes(self):
        self.catalog.providerReady.emit("Home")
        self.tune(0)  # the catalog drops its result once the provider changes
        self.controller.select_provider("Backup")
        self.assertEqual(self.catalog.selected[-1], "Backup")
        self.tune(1)  # an old-provider channel, queued behind the dropped request
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertEqual(self.requested(), ["One"])
        self.tune(2)  # the pipeline is free again
        self.assertEqual(self.requested(), ["One", "Three"])

    def test_provider_refresh_requests_the_pending_tune_again(self):
        self.catalog.providerReady.emit("Home")
        self.tune(0)
        self.controller.select_provider(force_refresh=True)
        self.tune(1)
        self.catalog.providerReady.emit("Home")
        self.assertEqual(self.requested(), ["One", "Two"])
        self.catalog.linkReady.emit(1, "http://stream/1")  # stale
        self.ready("http://stream/2")
        self.assertEqual(self.played_urls(), ["http://stream/2"])
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_session_error_toasts(self):
        self.play(0)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])


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
        self.catalog.providerReady.emit("Home")
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
        self.catalog.providerReady.emit("Home")
        self.catalog.sectionFailed.emit("live", PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.toast.hint, "Loading Home…")  # a setup is still running
        self.catalog.sectionFailed.emit("live", "Timed out")
        self.assertIsNone(self.toast.hint)

    def test_a_provider_chosen_in_the_drawer_shows_the_loading_hint(self):
        self.controller.start()
        self.catalog.providerReady.emit("Home")
        self.catalog.sectionReady.emit("live")
        self.nav.open(NavNode("source", "Backup", "Backup", "Backup"))
        self.assertEqual(self.catalog.selected, [None, "Backup"])
        self.assertEqual(self.toast.hint, "Loading Backup…")
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
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
        self.catalog.providerReady.emit("Home")
        self.assertEqual(self.catalog.requests[-1], ("section", "live"))
        self.catalog.sectionReady.emit("live")
        self.assertEqual([n.key for n in self.nav.path], ["Home", "live", "10"])
        self.assertEqual(self.requested(), ["Three"])
        self.ready("http://stream/3")
        self.assertEqual(self.played_urls(), ["http://stream/3"])
        self.assertEqual(self.zap.context.group_key, "10")
        self.assertEqual(self.drawer.named("open_drawer"), [])

    def test_last_channel_of_another_provider_opens_the_drawer(self):
        self.config.last_live_channel = {
            "provider_name": "Other",
            "group_id": "10",
            "item": CHANNELS[2],
        }
        self.controller.start()
        self.catalog.providerReady.emit("Home")
        self.catalog.sectionReady.emit("live")
        self.assertEqual(self.catalog.links, [])
        self.assertEqual(self.drawer.named("open_drawer"), [(None,)])
        self.catalog.sectionReady.emit("live")  # later section loads do not reopen it
        self.assertEqual(len(self.drawer.named("open_drawer")), 1)

    def test_provider_failure_toasts_and_opens_the_drawer_on_the_sources(self):
        self.controller.start()
        self.catalog.providerFailed.emit("Home", "Handshake failed")
        self.assertEqual(self.toast.messages(), [("Home: Handshake failed", "error")])
        self.assertIsNone(self.toast.hint)
        self.assertEqual(self.drawer.named("open_drawer"), [(None, "left")])
        self.assertEqual([(n.layer, n.key) for n in self.nav.path], [("source", "Home")])

    def test_live_section_failure_opens_the_drawer_with_a_hint(self):
        self.controller.start()
        self.catalog.loaded = False  # nothing saved to fall back on
        self.catalog.providerReady.emit("Home")
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
        self.controller.handle(Action.MUTE)
        self.assertEqual(
            self.toast.messages()[-3:],
            [("Volume 55%", "info"), ("Muted", "info"), ("Volume 55%", "info")],
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
        self.controller.mouse_moved()
        self.assertEqual(self.transport.calls, [])
        self.play(0)
        self.controller.mouse_moved()
        self.assertEqual(self.transport.named("show_live"), [("One", "")])
        self.controller.mouse_moved()
        self.assertEqual(self.transport.calls[-1], ("poke",))


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
        self.catalog.providerReady.emit("Home")
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
    """DEBUG logs never carry raw provider text: it can hold Xtream credentials (Minor 8)."""

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
        self.catalog.providerFailed.emit("Home", "Couldn't reach the provider")

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
        self.catalog.providerReady.emit("Home")
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
    """The banner stays up while a slow link resolves (Minor 11)."""

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

    def test_the_banner_stays_until_the_link_resolves_then_fades(self):
        self.tune(0)
        QTest.qWait(800)  # well past the banner's own time and its fade
        self.assertTrue(self.banner.isVisible())
        self.assertEqual(self.banner.info.name, "One")
        self.ready()
        self.assertTrue(wait_until(lambda: not self.banner.isVisible(), 2000))

    def test_the_banner_stays_until_the_link_fails(self):
        self.tune(0)
        QTest.qWait(800)  # well past the banner's own time and its fade
        self.assertTrue(self.banner.isVisible())
        self.catalog.linkFailed.emit(1, "Portal down")
        self.assertTrue(wait_until(lambda: not self.banner.isVisible(), 2000))

    def test_a_dismissed_banner_is_not_brought_back(self):
        self.tune(0)
        self.controller.handle(Action.ESCAPE)
        QTest.qWait(200)
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


class ReviewFixTests(ControllerTestCase):
    """Fix round 1: findings from the review of the first controller."""

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
            self.catalog.providerReady.emit("Home")
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
        self.catalog.providerReady.emit("Home")
        self.catalog.sectionReady.emit("live")  # autotunes Three
        self.controller.handle(Action.CH_NEXT)
        self.catalog.linkReady.emit(1, "http://stream/3")
        self.assertEqual(self.requested(), ["Three", "Four"])

    def test_another_provider_resets_zapping(self):
        self.catalog.providerReady.emit("Home")
        self.play(0)
        self.play(1)  # previous channel: One
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.controller.handle(Action.PREVIOUS)
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested(), ["One", "Two"])  # nothing of Home's is tuned

    def test_link_failure_while_loading_waits_for_the_provider(self):
        self.catalog.providerReady.emit("Home")
        self.tune(0)
        self.catalog.loading = True
        self.catalog.linkFailed.emit(1, PROVIDER_LOADING_MESSAGE)
        self.assertEqual(self.toast.messages(), [])
        self.assertEqual(self.requested(), ["One"])
        self.catalog.loading = False
        self.catalog.providerReady.emit("Home")
        self.assertEqual(self.requested(), ["One", "One"])
        self.ready("http://stream/1")
        self.assertEqual(self.played_urls(), ["http://stream/1"])

    def test_live_section_still_loading_is_ignored(self):
        self.catalog.loaded = False
        self.catalog.providerReady.emit("Home")
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
        self.catalog.providerReady.emit("Home")
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
        QTest.qWait(80)  # past the digit commit
        self.assertEqual(self.requested(), ["One"])
        self.assertEqual(self.window.calls, [])

    def test_escape_with_the_column_open_cancels_the_rest_tune(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: column on Three
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.column.visible)
        self.ready("http://stream/2")
        QTest.qWait(80)  # past the zap rest
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_enter_with_the_column_open_tunes_the_highlight(self):
        self.play(0)
        self.controller.handle(Action.CH_NEXT)
        self.controller.handle(Action.CH_NEXT)  # burst: column on Three
        self.controller.handle(Action.OK)
        self.assertFalse(self.column.visible)
        self.assertEqual(self.drawer.named("open_drawer"), [])
        self.ready("http://stream/2")  # Two settles; the queued Three is requested
        QTest.qWait(80)
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
        self.catalog.providerReady.emit("Home")
        self.catalog.sectionReady.emit("live")
        self.catalog.sectionFailed.emit("live", UNREACHABLE_MESSAGE)
        self.catalog.providerFailed.emit("Home", UNREACHABLE_MESSAGE)
        self.catalog.epgReady.emit()
        self.zap.digit("9")  # zap timers may still fire while the window is torn down
        QTest.qWait(80)  # past the digit commit: "No channel 9" must not be toasted
        self.assertEqual(self.catalog.requests, [])
        self.assertEqual(self.toast.calls, [])
        self.assertEqual(self.banner.calls, [])
        self.assertEqual(self.drawer.calls, [])
        self.assertEqual(self.column.calls, [])


class FixRound2Tests(ControllerTestCase):
    """Fix round 2: the loading queue, OK versus the drawer, re-choosing the playing channel."""

    def setUp(self):
        super().setUp()
        self.controller.start()
        self.catalog.providerReady.emit("Home")

    def test_two_loading_failures_keep_the_newest_choice(self):
        self.play(0)
        self.catalog.loading = True
        self.tune(1)
        self.catalog.linkFailed.emit(2, PROVIDER_LOADING_MESSAGE)
        self.tune(2)  # the newest choice, sent while Two waits for the provider
        self.catalog.linkFailed.emit(3, PROVIDER_LOADING_MESSAGE)
        self.catalog.loading = False
        self.catalog.providerReady.emit("Home")
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
        QTest.qWait(80)  # the column's highlight is not tuned
        self.assertEqual(self.requested(), ["One", "Two"])

    def test_channel_list_with_the_column_open_opens_the_drawer(self):
        self.burst_to_column()
        self.controller.handle(Action.OPEN_DRAWER)  # the menu's "Channel list"
        self.assertFalse(self.column.visible)
        self.assertTrue(self.drawer.visible)
        self.ready("http://stream/2")
        QTest.qWait(80)
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
        QTest.qWait(60)
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
    def test_banner_refreshes_when_its_logo_arrives(self):
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
        channel = dict(CHANNELS[0], logo="http://logo/1.png")
        self.controller.tune(channel, group_key="10", channels=[channel])
        self.assertIsNone(self.banner.info.logo)
        self.assertEqual(logos.requested, ["http://logo/1.png"])
        logos.logoReady.emit("http://logo/other.png")
        self.assertEqual(len(self.banner.named("show_info")), 1)
        logos.pixmaps["http://logo/1.png"] = QPixmap(4, 4)
        logos.logoReady.emit("http://logo/1.png")
        self.assertIsNotNone(self.banner.info.logo)


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
                "max_bytes": 128 * MIB,  # without consent, a RAM ring of 128 MiB
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
        self.assertFalse(kwargs["verify_ssl"])
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (True, 4096 * MIB))

    def test_a_second_tune_stops_the_first_and_starts_only_after_released(self):
        self.start_pipeline(0)
        self.play(1)
        self.assertEqual(self.live.calls[-1], ("stop",))
        QTest.qWait(30)
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
        QTest.qWait(30)  # its slot is granted at once…
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
        QTest.qWait(30)
        self.assertEqual([url for url, _ in self.starts()], ["http://stream/1", "http://stream/3"])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_budget_denial_toasts_and_opens_nothing(self):
        self.budget.request("Home", 1, purpose="pip")  # Phase 4: the PiP holds the only slot
        self.play(0)
        QTest.qWait(30)
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
        QTest.qWait(30)
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
        QTest.qWait(30)
        self.assertEqual(self.starts(), [])  # VLC has not let go of the movie…
        self.assertEqual(self.budget.holders("Home"), ["main"])  # …which keeps its slot
        self.session.let_go()
        self.assertTrue(wait_until(lambda: len(self.starts()) == 1))

    def test_direct_channel_to_pipeline(self):
        self.tune(0)
        self.ready("udp://239.0.0.1:1234")
        self.session.holding = True
        self.play(1)
        QTest.qWait(30)
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
            self.assertTrue(wait_until(lambda: len(self.played_urls()) == 2, 1000))

    def test_a_movie_after_a_movie_waits_for_the_first_to_end(self):
        self.play_movie()
        self.session.holding = True
        self.play_movie()
        self.assertEqual(len(self.played_urls()), 1)
        self.session.let_go()
        self.assertEqual(len(self.played_urls()), 2)


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
        QTest.qWait(10)
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
    def test_the_unavailable_notice_shows_once_per_channel(self):
        self.catalog.ptype = "M3UPLAYLIST"  # no link to make again
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)
        self.live.notice.emit("Rewound past the buffer")
        self.start_channel(1)
        self.live.notice.emit(UNAVAILABLE)
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)  # One again: already said
        self.live.notice.emit("Rewound past the buffer")  # other notices every time
        self.assertEqual(
            self.toast.messages(),
            [
                (UNAVAILABLE, "info"),
                ("Rewound past the buffer", "info"),
                (UNAVAILABLE, "info"),
                ("Rewound past the buffer", "info"),
            ],
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
        self.session.ended.emit()
        self.assertEqual(
            self.toast.messages(), [("One: Broken", "error"), ("The stream ended", "info")]
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
        self.session.ended.emit()  # once per tune
        self.assertEqual(self.toast.messages(), [("The stream ended", "info")])

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
        self.assertEqual(self.toast.messages(), [(UNAVAILABLE, "info")])

    def test_a_held_notice_shows_once_the_direct_play_works(self):
        self.start_channel(0)
        self.live.notice.emit(UNAVAILABLE)
        self.live.play_direct()
        self.assertEqual(self.toast.messages(), [])
        self.session.stateChanged.emit("playing")
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.toast.messages(), [(UNAVAILABLE, "info")])

    def test_a_failure_after_the_user_moved_on_is_not_retried(self):
        self.start_pipeline(0)
        self.tune(1)  # its link is still on the way
        self.live.failed.emit("The stream could not be opened")
        self.assertEqual(self.requested(), ["One", "Two"])
        self.assertEqual(self.toast.messages(), [("One: The stream could not be opened", "error")])


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
        QTest.qWait(120)
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
        QTest.qWait(120)
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


class AutoplayCase(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.catalog.providerReady.emit("Home")
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
        QTest.qWait(60)  # the zap's digit timer would have tuned by now
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
        self.catalog.providerReady.emit("Home")
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
    """The window's Library hooks (Task 12, R5): stop for it, back to live TV, its plays."""

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
        self.catalog.providerReady.emit("Backup")
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
        self.catalog.providerFailed.emit("Backup", "Handshake failed")
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
        self.catalog.providerReady.emit("Backup")
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
        self.catalog.providerReady.emit("Backup")
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


if __name__ == "__main__":
    unittest.main()


# ---- picture in picture (Phase 4 Task 3) -----------------------------------------------------

GROUP_B = [
    {"id": i, "name": name, "number": str(i), "cmd": f"ffrt http://tv/{i}"}
    for i, name in ((5, "Five"), (6, "Six"))
]
SHIFTED = {
    "start": 0.0,
    "end": 300.0,
    "position": 100.0,
    "live_edge": 300.0,
    "lag": 200.0,
    "mode": "shifted",
    "rate": 1.0,
    "paused": False,
    "target": 2.5,
}


class FakeFrame(QObject):
    swapRequested = Signal()

    def __init__(self):
        super().__init__()
        self.calls = []
        self.shown = False
        self.large = "main"

    def show_pip(self):
        self.calls.append(("show_pip",))
        self.shown = True

    def hide_pip(self):
        self.calls.append(("hide_pip",))
        self.shown = False

    def set_large(self, name):
        self.calls.append(("set_large", name))
        self.large = name


class FakePipLabel(FakeOverlay):
    closeRequested = Signal()
    mixerRequested = Signal()
    info = None
    audible = False

    def set_info(self, name, state):
        self.record("set_info", name, state)
        self.info = (name, state)

    def set_audible(self, audible):
        self.audible = audible

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


class FakeMixer(FakeOverlay):
    levelRequested = Signal(int, int)
    muteRequested = Signal(int)
    rowSelected = Signal(int)
    closed = Signal()
    rows = ()
    selected = 0

    def show_rows(self, rows, selected):
        self.record("show_rows", rows, selected)
        self.rows, self.selected = list(rows), selected
        self.visible = True

    def dismiss(self):
        if self.visible:
            self.visible = False
            self.closed.emit()

    def shown(self):
        """(title, channel, level, muted) per row, and the selected row."""
        return [(r.title, r.channel, r.level, r.muted) for r in self.rows], self.selected


class PipCase(ControllerTestCase):
    """Two players (fake sessions and live sessions, each with its real LiveTuner), the real
    SessionManager and budget, a fake PiP frame and label."""

    def setUp(self):
        super().setUp()
        self.config.ssl_verify = True
        self.config.timeshift_enabled = False
        self.config.timeshift_max_mib = 2048
        self.config.selected_provider_name = "Home"
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.catalog.live["20"] = list(GROUP_B)
        self.budget = ConnectionBudget()
        self.live = FakeLiveSession(self.session)
        self.pip_session = FakeSession()
        self.pip_live = FakeLiveSession(self.pip_session)
        self.main_slot = Slot(
            "main",
            self.session,
            self.live,
            tuner=LiveTuner(self.session, self.config, self.live, self.budget),
        )
        self.pip_slot = Slot(
            "pip",
            self.pip_session,
            self.pip_live,
            tuner=LiveTuner(
                self.pip_session, self.config, self.pip_live, self.budget, purpose="pip"
            ),
        )
        self.sessions = SessionManager(self.main_slot, self.pip_slot, self.budget)
        self.frame = FakeFrame()
        self.label = FakePipLabel()
        self.controller = self.make_controller(
            self.column, sessions=self.sessions, pip_frame=self.frame
        )
        for live in (self.live, self.pip_live):
            self.addCleanup(live.released.emit)  # no capped release wait outlives the test
        self.controller.start()

    mixer = None  # MixerTests: a FakeMixer

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.pip_label = getattr(self, "label", None)
        overlays.mixer = self.mixer
        return overlays

    def start_large(self, index):
        """Tune a channel on the large picture and play it from its pipeline."""
        live = self.controller.live
        count, running = len(live.named("start")), live.running
        self.play(index)
        if running:
            self.assertEqual(live.calls[-1], ("stop",))
            live.released.emit()
        self.assertTrue(wait_until(lambda: len(live.named("start")) == count + 1))
        live.play_pipeline()

    def open_small(self, channel, group_key="10", channels=CHANNELS):
        """Open `channel` small from the drawer's menu and play it from its pipeline."""
        self.controller.open_pip(channel, group_key=group_key, channels=channels)
        small = self.sessions.other()
        count = len(small.live.named("start"))
        self.ready(f"http://stream/{channel['id']}")
        self.assertTrue(wait_until(lambda: len(small.live.named("start")) == count + 1))
        self.assertTrue(self.sessions.pip_open())
        small.live.play_pipeline()
        return small

    def counts(self):
        """What each player and live session was asked to do so far."""
        return (
            len(self.session.plays),
            self.session.stops,
            len(self.live.calls),
            len(self.pip_session.plays),
            self.pip_session.stops,
            len(self.pip_live.calls),
        )


class PipTests(PipCase):
    def test_p_opens_the_previous_channel_small_once_its_link_comes(self):
        self.start_large(0)
        self.start_large(1)  # One is the previous channel now
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "One")
        self.assertEqual(self.pip_live.calls, [])
        self.ready("http://stream/1")
        self.assertTrue(wait_until(lambda: self.pip_live.named("start")))
        url, kwargs = self.pip_live.named("start")[0]
        self.assertEqual(url, "http://stream/1")
        self.assertEqual(  # a main tune's start arguments
            kwargs,
            {
                "headers": {"User-Agent": DEFAULT_USER_AGENT},
                "verify_ssl": True,
                "latency": "low",
                "user_agent": DEFAULT_USER_AGENT,
                "disk": False,
                "max_bytes": 128 * MIB,
            },
        )
        self.assertTrue(self.sessions.pip_open())
        self.assertTrue(self.pip_session.muted)
        self.assertFalse(self.session.muted)
        self.assertEqual(self.frame.calls, [("show_pip",)])
        self.assertTrue(self.label.visible)
        self.assertEqual(self.label.info, ("One", "Connecting…"))
        self.assertEqual(self.transport.named("set_pip_state")[-1], (True, True))
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])
        self.assertEqual(len(self.live.named("start")), 2)  # the large picture plays on

    def test_p_again_closes_pip(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))
        large = self.counts()[:3]
        self.controller.handle(Action.PIP)
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.assertEqual(self.frame.calls[-2:], [("set_large", "main"), ("hide_pip",)])
        self.assertFalse(self.label.visible)
        self.assertEqual(self.transport.named("set_pip_state")[-1], (True, False))
        self.pip_live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.counts()[:3], large)

    def test_p_while_its_link_is_on_the_way_cancels_it(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.controller.handle(Action.PIP)  # changed one's mind
        self.ready("http://stream/1")
        QTest.qWait(20)
        self.assertEqual(self.pip_live.calls, [])
        self.assertFalse(self.sessions.pip_open())

    def test_p_without_a_previous_channel_says_so(self):
        self.start_large(0)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.toast.messages()[-1], (NO_PREVIOUS, "info"))
        self.assertEqual(self.requested(), ["One"])

    def test_s_swaps_the_banner_the_zap_group_and_the_transport_follow_the_pip(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.pip_live.timelineChanged.emit(SHIFTED)
        self.assertEqual(self.label.info, ("Five", "−03:20"))
        before = self.counts()
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.frame.calls[-1], ("set_large", "pip"))
        self.assertEqual(self.banner.info.name, "Five")
        self.assertEqual(self.zap.context.group_key, "20")
        self.assertEqual(self.zap.context.current(), GROUP_B[0])
        self.assertEqual(self.transport.named("show_timeshift")[-1], ("Five", ""))
        self.assertEqual(self.transport.named("update_timeline")[-1][0], SHIFTED)
        self.assertEqual(self.label.info, ("One", "Connecting…"))  # One is small now
        self.assertEqual(self.counts(), before)  # nothing played, stopped or started again
        self.assertTrue(self.session.muted)  # audio follows the large picture
        self.assertFalse(self.pip_session.muted)
        self.assertIs(self.controller.session, self.pip_session)
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])

    def test_a_zap_after_a_swap_tunes_the_large_slot_and_leaves_the_small_one_alone(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)
        main = self.counts()[:3]
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.requested()[-1], "Six")
        self.ready("http://stream/6")
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.pip_live.released.emit()
        self.assertTrue(
            wait_until(lambda: self.pip_live.named("start")[-1][0] == "http://stream/6")
        )
        self.assertEqual(self.counts()[:3], main)  # the small picture got no calls
        self.assertEqual(self.zap.context.current(), GROUP_B[1])
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "pip"])
        self.assertEqual(self.label.info[0], "One")

    def test_zapping_the_large_slot_onto_the_pips_channel_swaps_instead(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        links, before = len(self.catalog.links), self.counts()
        self.controller.handle(Action.CH_NEXT)  # One -> Two, which the PiP shows
        self.assertEqual(len(self.catalog.links), links)  # no second stream of it
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(self.zap.context.current(), CHANNELS[1])
        self.assertEqual(self.counts(), before)
        self.controller.handle(Action.PREVIOUS)  # back to One: the small picture again
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertEqual(len(self.catalog.links), links)

    def test_s_without_pip_says_how_to_open_it(self):
        self.start_large(0)
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.toast.messages()[-1], (OPEN_PIP_FIRST, "info"))

    def test_a_click_on_the_small_picture_swaps(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.frame.swapRequested.emit()
        self.assertIs(self.sessions.large(), self.pip_slot)

    def test_denied_toasts_the_providers_limit(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB"}  # one stream
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.ready("http://stream/1")
        QTest.qWait(20)
        self.assertIn(("Your provider allows 1 stream at a time", "error"), self.toast.messages())
        self.assertEqual(self.pip_live.calls, [])
        self.assertEqual(self.frame.calls, [])
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_p_during_a_movie_toasts_and_opens_nothing(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.live.released.emit()
        links = len(self.catalog.links)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.toast.messages()[-1], (PIP_LIVE_ONLY, "info"))
        self.assertEqual(len(self.catalog.links), links)

    def test_a_movie_closes_the_small_picture_first(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_the_library_stops_both_pictures(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.stop_for_library()
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))
        self.assertEqual(self.live.calls[-1], ("stop",))

    def test_shutdown_closes_the_small_picture_then_stops_the_large(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        order = []
        self.live.stop = lambda: order.append("large")
        self.pip_live.stop = lambda: order.append("small")
        self.controller.shutdown()
        self.assertEqual(order, ["small", "large"])

    def test_closing_while_swapped_keeps_the_picture_that_is_large(self):
        self.start_large(0)
        self.open_small(GROUP_B[0], "20", GROUP_B)
        self.controller.handle(Action.SWAP)
        large = self.counts()[3:]
        self.label.closeRequested.emit()  # the × on the small picture: One
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.assertEqual(self.counts()[3:], large)  # Five plays on, large
        self.assertIs(self.controller.session, self.pip_session)
        self.assertEqual(self.frame.calls[-2:], [("set_large", "pip"), ("hide_pip",)])
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])  # Five's, as the large one
        self.controller.handle(Action.CH_NEXT)  # zapping goes on in Five's group
        self.assertEqual(self.requested()[-1], "Six")

    def test_the_label_names_the_small_pictures_state(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.assertEqual(self.label.info, ("Two", "Connecting…"))
        self.pip_live.timelineChanged.emit(dict(SHIFTED, lag=3.4, target=2.5))
        self.assertEqual(self.label.info, ("Two", "LIVE"))  # within a second of its target
        self.pip_live.timelineChanged.emit(dict(SHIFTED, lag=3.6, target=2.5))
        self.assertEqual(self.label.info, ("Two", "−00:03"))
        self.live.timelineChanged.emit(SHIFTED)  # the large picture's: not the label's
        self.assertEqual(self.label.info, ("Two", "−00:03"))

    def test_a_pip_failure_closes_it_and_says_why(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.pip_live.failed.emit("http://user:secret@tv/2: 404")
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.toast.messages()[-1], (PIP_FAILED, "error"))

    def test_the_drawers_menu_opens_a_channel_small(self):
        self.start_large(0)
        self.nav.reset_to("live", "10")
        node = self.nav.children(self.nav.path[-1])[2]
        shown = []
        with mock.patch.object(QMenu, "popup", lambda menu, pos, *a: shown.append(menu)):
            self.drawer.contextRequested.emit(node, QPoint(40, 50))
        menu = shown[0]
        self.addCleanup(menu.deleteLater)
        action = next(a for a in menu.actions() if a.text() == "Open in picture-in-picture")
        action.trigger()
        self.assertEqual(self.requested()[-1], "Three")
        self.ready("http://stream/3")
        self.assertTrue(wait_until(self.sessions.pip_open))
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.zap.context.current(), CHANNELS[2])
        self.controller.handle(Action.CH_NEXT)  # ↓ walks the drawer group from Three
        self.assertEqual(self.requested()[-1], "Four")

    def test_ctrl_p_in_the_drawer_opens_the_highlighted_channel_small(self):
        self.start_large(0)
        self.nav.reset_to("live", "10")
        self.drawer.visible = True
        self.drawer.highlight = self.nav.children(self.nav.path[-1])[2]
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "Three")
        self.assertEqual(self.drawer.named("close_drawer"), [()])

    def test_the_large_pictures_channel_is_not_opened_again_small(self):
        self.start_large(0)
        self.controller.open_pip(CHANNELS[0], group_key="10", channels=CHANNELS)
        self.assertEqual(self.toast.messages()[-1], (ALREADY_LARGE, "info"))
        self.assertEqual(self.requested(), ["One"])

    def test_zapping_onto_the_channel_on_its_way_to_the_pip_opens_it_large_only(self):
        self.start_large(0)
        self.controller.open_pip(CHANNELS[1], group_key="10", channels=CHANNELS)
        self.controller.handle(Action.CH_NEXT)  # Two, whose PiP link is on its way
        self.ready("http://stream/2")  # the PiP's link: it opens nothing now
        QTest.qWait(20)
        self.assertEqual(self.pip_live.calls, [])
        self.assertEqual(self.requested()[-1], "Two")
        self.ready("http://stream/2")  # the large picture's own
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.live.named("start")[-1][0] == "http://stream/2"))
        self.assertFalse(self.sessions.pip_open())

    def test_another_provider_closes_the_small_picture(self):
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.catalog.providerReady.emit("Home")  # the same provider again: it stays open
        self.assertTrue(self.sessions.pip_open())
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")
        self.assertFalse(self.sessions.pip_open())
        self.assertEqual(self.pip_live.calls[-1], ("stop",))

    def test_a_provider_setup_while_the_pips_link_is_on_its_way_asks_again(self):
        # Review I1: a setup drops every link request; the PiP's must not stay stuck.
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.assertEqual(self.requested()[-1], "One")
        links = len(self.catalog.links)
        self.catalog.providerReady.emit("Home")  # a refresh: the same provider
        self.assertEqual(len(self.catalog.links), links + 1)  # asked again
        self.assertEqual(self.requested()[-1], "One")
        self.ready("http://stream/1")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_after_another_provider_pip_still_opens(self):
        self.catalog.providerReady.emit("Home")
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)  # its link is on its way…
        self.catalog.provider = "Backup"
        self.catalog.providerReady.emit("Backup")  # …and dropped with the old provider
        links = len(self.catalog.links)
        self.assertEqual(len(self.catalog.links), links)  # the old channel is not asked again
        self.start_large(2)
        self.controller.open_pip(CHANNELS[3], group_key="10", channels=CHANNELS)
        self.assertEqual(self.requested()[-1], "Four")
        self.ready("http://stream/4")
        self.assertTrue(wait_until(self.sessions.pip_open))

    def test_a_provider_failure_forgets_the_pips_link_and_p_works_again(self):
        self.start_large(0)
        self.start_large(1)
        self.controller.handle(Action.PIP)
        self.catalog.providerFailed.emit("Home", "Portal down")
        links = len(self.catalog.links)
        self.controller.handle(Action.PIP)  # nothing was open: P asks again
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_swap_waits_for_nothing_while_the_large_pictures_stream_opens(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.play(2)  # Three: waits for One's release
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.main_slot)  # nothing swapped
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.live.named("start")[-1][0] == "http://stream/3"))
        self.controller.handle(Action.SWAP)
        self.assertIs(self.sessions.large(), self.pip_slot)


class MixerTests(PipCase):
    """The volume keys on the mixer: a row per picture (Phase 4 Task 3b)."""

    def setUp(self):
        self.mixer = FakeMixer()
        super().setUp()
        self.config.pip_volume = 30

    def test_the_pips_level_comes_from_the_config_at_startup(self):
        self.assertEqual(self.sessions.level("pip"), 30)
        self.config.pip_volume = 45
        controller = self.make_controller(self.column, sessions=self.sessions)
        controller.start()
        self.assertEqual(self.sessions.level("pip"), 45)

    def test_a_volume_key_without_pip_shows_one_row(self):
        self.start_large(0)
        self.controller.handle(Action.VOL_DOWN)
        self.assertEqual(self.mixer.shown(), ([("Main", "One", 95, False)], 0))
        self.assertEqual(self.sessions.level("main"), 95)
        self.assertEqual(self.session.level, 95)
        self.assertEqual(self.toast.messages(), [])  # the mixer says it

    def test_with_pip_it_shows_two_rows_and_the_keys_act_on_the_selected_one(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_DOWN)  # the large picture's row first
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 95, False), ("PiP", "Two", 30, True)], 0)
        )
        self.controller.handle(Action.SEEK_FWD)  # → the PiP's row (not a seek)
        self.assertEqual(self.mixer.selected, 1)
        self.assertEqual(self.live.named("seek_relative"), [])
        self.controller.handle(Action.CH_PREV)  # ↑: louder, and audible now
        self.assertEqual(self.mixer.shown()[0][1], ("PiP", "Two", 35, False))
        self.assertFalse(self.pip_session.muted)
        self.assertEqual(self.pip_session.level, 35)
        self.assertTrue(self.label.audible)
        self.controller.handle(Action.CH_NEXT)  # ↓ (not a zap)
        self.controller.handle(Action.CH_NEXT)
        self.assertEqual(self.sessions.level("pip"), 25)
        self.assertEqual(self.requested(), ["One", "Two"])
        self.controller.handle(Action.TAB)  # back to Main
        self.assertEqual(self.mixer.selected, 0)
        self.controller.handle(Action.MUTE)  # M mutes the selected row
        self.assertTrue(self.sessions.muted("main"))
        self.assertFalse(self.sessions.muted("pip"))
        self.controller.handle(Action.ESCAPE)  # hides it first
        self.assertFalse(self.mixer.visible)
        self.assertEqual(self.window.calls, [])

    def test_m_mutes_the_large_picture_and_shows_the_mixer(self):
        self.start_large(0)
        self.controller.handle(Action.MUTE)
        self.assertTrue(self.sessions.muted("main"))
        self.assertTrue(self.session.muted)
        self.assertEqual(self.mixer.shown(), ([("Main", "One", 100, True)], 0))
        self.mixer.dismiss()
        self.controller.handle(Action.MUTE)
        self.assertFalse(self.sessions.muted("main"))

    def test_a_swap_moves_each_level_with_its_stream(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_DOWN)
        self.mixer.levelRequested.emit(0, 80)
        self.mixer.levelRequested.emit(1, 20)
        self.assertEqual(
            self.mixer.shown(), ([("Main", "One", 80, False), ("PiP", "Two", 20, False)], 1)
        )
        self.controller.handle(Action.SWAP)
        self.assertEqual(
            self.mixer.shown()[0], [("Main", "Two", 20, False), ("PiP", "One", 80, False)]
        )
        self.assertEqual((self.session.level, self.pip_session.level), (80, 20))

    def test_raising_the_pip_row_from_zero_unmutes_the_small_picture(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.label.mixerRequested.emit()  # the PiP label's speaker: the PiP's row
        self.assertEqual(self.mixer.selected, 1)
        self.mixer.levelRequested.emit(1, 0)
        self.assertTrue(self.sessions.muted("pip"))
        self.controller.handle(Action.CH_PREV)
        self.assertEqual(self.sessions.level("pip"), 5)
        self.assertFalse(self.sessions.muted("pip"))
        self.assertFalse(self.pip_session.muted)

    def test_a_click_on_a_speaker_toggles_that_rows_mute(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.controller.handle(Action.VOL_UP)
        self.mixer.muteRequested.emit(1)
        self.assertFalse(self.sessions.muted("pip"))
        self.mixer.muteRequested.emit(1)
        self.assertTrue(self.sessions.muted("pip"))
        self.mixer.muteRequested.emit(0)
        self.assertTrue(self.sessions.muted("main"))

    def test_the_pips_level_is_remembered_once_the_mixer_hides(self):
        self.start_large(0)
        self.open_small(CHANNELS[1])
        self.label.mixerRequested.emit()
        self.controller.handle(Action.CH_PREV)
        self.assertEqual(self.config.saves, 0)
        self.mixer.dismiss()  # 3 s after the last change
        self.assertEqual((self.config.pip_volume, self.config.saves), (35, 1))
        self.mixer.dismiss()
        self.assertEqual(self.config.saves, 1)

    def test_the_wheel_during_a_movie_shows_the_mixer(self):
        self.controller.play_vod(VodTests.MOVIE, "movie", play_context())
        self.ready("http://vod/heat.mkv")
        self.controller.wheel(1, NO_MODS)
        self.assertEqual(self.mixer.shown(), ([("Main", "Heat", 95, False)], 0))
