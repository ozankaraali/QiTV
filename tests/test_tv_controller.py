from datetime import datetime, timedelta, timezone
import logging
import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView, QApplication, QWidget

from services.catalog import PROVIDER_LOADING_MESSAGE, UNREACHABLE_MESSAGE
from services.epg_view import Programme
from services.navigation import NavModel, NavNode
from services.vlc_profile import PlayKind
from services.zapping import ZapController
from tests.test_navigation import FakeCatalog as NavCatalog
from ui.keymap import Action
from ui.overlays.channel_column import ChannelColumn
from ui.overlays.drawer import LOADING_TEXT, PROVIDER_ERROR_TEXT, Drawer
from ui.overlays.toast import Toast
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import ZapBanner
from ui.tv_controller import Overlays, TVController, scrub

NOW = datetime(2026, 10, 15, 20, 30, tzinfo=timezone.utc)
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

    def provider_names(self):
        return list(self.names)

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


class FakeSession(QObject):
    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()

    def __init__(self):
        super().__init__()
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
    closed = Signal()

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

    def show_vod(self, title, subtitle):
        self.record("show_vod", title, subtitle)
        self.visible = True

    def show_live(self, title, subtitle):
        self.record("show_live", title, subtitle)
        self.visible = True

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


class FakeExternal:
    def __init__(self, result=(True, "")):
        self.calls = []
        self.result = result

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
        self.config = SimpleNamespace(
            favorites=[],
            watch_history=[],
            last_live_channel=None,
            start_with_last_channel=True,
            play_in_vlc=False,
            play_in_mpv=False,
            live_latency="low",
            mouse_wheel_action="channel",
            channel_epg=True,
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
        self.controller = self.make_controller(self.column)

    def make_controller(self, column):
        controller = TVController(
            config=self.config,
            catalog=self.catalog,
            nav=self.nav,
            zap=self.zap,
            session=self.session,
            watch=self.watch,
            overlays=Overlays(self.banner, column, self.drawer, self.transport, self.toast),
            window_actions=self.window,
            external=self.external,
            now=lambda: NOW,
        )
        self.addCleanup(controller.shutdown)  # late zap timers must not reach a dead overlay
        return controller

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
    def test_no_providers_shows_the_welcome_hint_until_one_exists(self):
        self.catalog.names = []
        self.controller.start()
        self.assertEqual(self.toast.hint, "Add a provider in Settings to start watching")
        self.assertEqual(self.toast.messages(), [])  # a hint: no message that times out
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.drawer.calls, [])
        self.controller.handle(Action.OPEN_DRAWER)
        self.controller.select_provider(force_refresh=True)
        self.assertEqual(self.drawer.calls, [])
        self.assertEqual(self.catalog.selected, [])
        self.assertEqual(self.toast.hint, "Add a provider in Settings to start watching")
        self.catalog.names = ["Home"]  # saved in Settings
        self.controller.select_provider(force_refresh=True)
        self.assertEqual(self.toast.hint, "Loading Home…")

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

    def test_window_actions_and_placeholders(self):
        self.controller.handle(Action.FULLSCREEN)
        self.controller.handle(Action.MINI_PLAYER)
        self.controller.video_double_clicked()
        self.assertEqual(
            self.window.calls, ["toggle_fullscreen", "toggle_mini_player", "toggle_fullscreen"]
        )
        self.controller.handle(Action.GUIDE)
        self.controller.handle(Action.GO_LIVE)
        self.assertEqual(self.toast.messages(), [("Coming soon", "info")] * 2)

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
        self.nav.reset_to("movies")
        self.node = NavNode("item", "m7", "Heat", self.MOVIE)
        self.group = NavNode("group", "5", "Films")

    def test_resumes_and_tracks_position(self):
        self.watch.resume["Home:movie:m7"] = 125_000
        self.drawer.visible = True
        self.controller.play_item(self.node, [self.node], self.group)
        self.assertEqual(self.catalog.links[0][2:], ("movies", False))
        self.assertFalse(self.drawer.visible)
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
        episode = NavNode("episode", "s:1:2", "Pilot", {"id": "s", "cmd": "x", "series": 2})
        self.controller.play_item(episode, [episode], NavNode("season", "1", "Season 1"))
        self.assertEqual(self.catalog.links[0][2:], ("series", True))

    def test_channel_keys_change_the_volume_during_vod(self):
        self.controller.play_item(self.node, [self.node], self.group)
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
        self.assertTrue(any("password=p" in m for m in debug))  # the raw text, at DEBUG only

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

    def test_refresh_overlays_reloads_the_guide_but_epg_ready_does_not(self):
        self.controller.refresh_overlays()
        self.assertEqual(self.catalog.epg_refreshes, 1)
        self.catalog.epgReady.emit()  # a loaded guide repaints; asking again would loop
        self.assertEqual(self.catalog.epg_refreshes, 1)
        self.assertEqual(self.column.named("refresh_now"), [(), ()])

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
        self.nav.reset_to("movies")
        movie = NavNode("item", "m7", "Heat", VodTests.MOVIE)
        self.controller.play_item(movie, [movie], NavNode("group", "5", "Films"))
        self.ready("http://vod/heat.mkv")
        self.session.seekable = True
        self.controller.handle(Action.MEDIA_PREV)
        self.controller.handle(Action.MEDIA_NEXT)
        self.assertEqual(self.session.relative, [-10_000, 10_000])
        self.assertEqual(len(self.catalog.links), 3)

    def test_live_link_in_flight_then_vod_plays_only_the_vod(self):
        self.tune(0)
        self.nav.reset_to("movies")
        movie = NavNode("item", "m7", "Heat", VodTests.MOVIE)
        self.controller.play_item(movie, [movie], NavNode("group", "5", "Films"))
        self.catalog.linkReady.emit(1, "http://stream/1")
        self.assertEqual(self.requested(), ["One", "Heat"])
        self.catalog.linkReady.emit(2, "http://vod/heat.mkv")
        self.assertEqual(self.played_urls(), ["http://vod/heat.mkv"])

    def test_external_vod_stops_the_in_app_session(self):
        self.play(0)
        self.config.play_in_vlc = True
        self.nav.reset_to("movies")
        movie = NavNode("item", "m7", "Heat", VodTests.MOVIE)
        self.controller.play_item(movie, [movie], NavNode("group", "5", "Films"))
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
        self.banner.dismiss()
        self.controller.handle(Action.INFO)
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
            overlays=Overlays(self.banner, self.column, self.drawer, self.transport, self.toast),
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


if __name__ == "__main__":
    unittest.main()
