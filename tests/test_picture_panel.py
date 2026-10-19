"""The Picture panel (2.3): the transport's Picture button (or A) opens it above the button; its
rows step the aspect ratio (kept per channel), deinterlacing (every player's, in the config) and
the sleep timer, and toggle the statistics. The sleep timer warns a minute before its end, Keep
watching turns it off, and at its end playback stops and the computer may sleep again. The
statistics read libVLC's numbers as plain rows.

PlaybackSession's side: the shape and deinterlacing reach libVLC only while no worker holds the
player's input, again once each media's video output exists, and Fill crops to the surface's
shape once a resize stops.
"""

import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import i18n, picture
from services.picture import FILL, STANDARD, WIDE, ChannelPictures, stats_rows
from services.playback import PlaybackSession
from services.sleep_timer import SleepTimer, clock_text
from services.sound_sync import SoundSync
from services.vlc_profile import PlayKind
from tests import test_playback_session as sessions
from tests.test_track_panel import CHANNEL, FILM, OTHER, FakePlayer
from ui.keymap import Action, key_hint
from ui.overlays.sleep_notice import SleepNotice
from ui.overlays.stats import StatsOverlay
from ui.overlays.track_panel import TrackPanel
from ui.overlays.transport import Transport
from ui.picture_controls import PictureControls

UP, DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class PicturePlayer(FakePlayer):
    """A player's picture, as PlaybackSession keeps it (and its numbers for the statistics)."""

    def __init__(self) -> None:
        super().__init__()
        self.shape = picture.AUTO
        self.deinterlacing = picture.AUTO
        self.auto_back = True  # set_deinterlace("auto") can give the player Auto again
        self.stats = {}

    def aspect(self):
        return self.shape

    def set_aspect(self, aspect):
        self.shape = aspect

    def deinterlace(self):
        return self.deinterlacing

    def set_deinterlace(self, choice):
        self.deinterlacing = choice
        if choice != picture.AUTO:
            self.auto_back = False
        return choice != picture.AUTO or self.auto_back

    def video_stats(self):
        return dict(self.stats)


class Config:
    deinterlace = picture.AUTO
    saves = 0

    def save_config(self):
        self.saves += 1


class PictureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.transport = Transport(self.host, idle_ms=200)
        self.transport.set_pip_state(True, False)
        self.transport.show_live("Harbour Nature", "Rivers")
        self.panel = TrackPanel(self.host)
        self.stats = StatsOverlay(self.host)
        self.notice = SleepNotice(self.host)
        self.player = PicturePlayer()
        self.small = PicturePlayer()  # a second player (PiP or a tile)
        self.current = CHANNEL
        self.playing = True
        self.store = ChannelPictures(None)
        self.config = Config()
        self.toasts = []
        self.left = None  # programme_left(): unknown
        self.info = {"source": "hls", "lag": 7.5}
        self.stops = 0
        self.sleeps = []  # what the sleep manager was asked: "allow", "prevent"
        self.clock = Clock()
        self.timer = SleepTimer(clock=self.clock, tick_ms=10)
        self.controls = PictureControls(
            panel=self.panel,
            transport=self.transport,
            stats=self.stats,
            notice=self.notice,
            session=lambda: self.player,
            current=lambda: self.current,
            playing=lambda: self.playing,
            players=lambda: [self.player, self.small],
            channel_key=lambda item: f"key-{item['id']}",
            store=self.store,
            config=self.config,
            toast=lambda text, *args: self.toasts.append(text),
            programme_left=lambda: self.left,
            live_info=lambda: dict(self.info),
            stop=self.stop,
            show_transport=self.transport.poke,
            allow_sleep=lambda: self.sleeps.append("allow"),
            prevent_sleep=lambda: self.sleeps.append("prevent"),
            timer=self.timer,
        )
        self.controls.wire()
        self.addCleanup(self.controls.shutdown)

    def stop(self):
        self.stops += 1
        self.playing = False

    def click(self, widget, point):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)

    def open(self):
        self.click(self.transport, self.transport.button_rect("picture").center().toPoint())

    def shown(self):
        """The panel's rows: (name, value) for a setting, "*name" for a ticked row."""
        rows = []
        for row in self.panel.model().rows:
            rows.append(
                (row.text, row.value) if row.value else ("*" if row.checked else "") + row.text
            )
        return rows

    def click_label(self, index):
        rect = self.panel.row_rect(index)
        self.click(
            self.panel, QPoint(round(rect.left() + 2 * rect.height()), round(rect.center().y()))
        )

    def step(self, index, delta):
        self.click(self.panel, self.panel.step_rect(index, delta).center().toPoint())


class PanelTests(PictureCase):
    def test_the_button_opens_it_above_itself_and_the_controls_stay_up(self):
        self.open()
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.transport.held(), "picture")
        button = self.transport.button_rect("picture").translated(self.transport.pos())
        self.assertLessEqual(self.panel.geometry().bottom(), self.transport.controls_top())
        self.assertLess(abs(self.panel.geometry().center().x() - button.center().x()), 2)
        self.assertEqual(self.panel.model().title, "Picture")
        self.assertEqual(
            self.shown(),
            [
                ("Aspect ratio", "Auto"),
                ("Deinterlacing", "Auto"),
                ("Sleep timer", "Off"),
                "Statistics",
            ],
        )

    def test_the_buttons_tooltip_names_its_key(self):
        rect = self.transport.button_rect("picture")
        self.assertEqual(
            self.transport.tooltip_at(rect.center()), "Picture, sleep timer and statistics (A)"
        )

    def test_the_aspect_steps_round_its_choices_and_the_channel_keeps_it(self):
        self.open()
        for expected in ("16:9", "4:3", "Fill the window", "Auto", "16:9"):
            self.step(0, 1)
            self.assertEqual(self.shown()[0], ("Aspect ratio", expected))
        self.step(0, -1)
        self.step(0, -1)
        self.assertEqual(self.player.shape, picture.FILL)
        self.assertEqual(self.store.aspect("key-1"), picture.FILL)
        self.assertTrue(self.panel.model().rows[0].lit)
        self.click_label(0)  # the row itself (its name): on
        self.assertEqual(self.player.shape, picture.AUTO)
        self.assertEqual(self.store.aspect("key-1"), picture.AUTO)  # Auto is not kept
        self.assertFalse(self.panel.model().rows[0].lit)

    def test_a_films_aspect_is_never_kept(self):
        self.current = FILM
        self.open()
        self.step(0, 1)
        self.assertEqual(self.player.shape, WIDE)
        self.assertEqual(self.store._entries, {})

    def test_deinterlacing_is_every_players_and_kept_in_the_config(self):
        self.open()
        self.step(1, 1)
        self.assertEqual((self.player.deinterlacing, self.small.deinterlacing), ("on", "on"))
        self.assertEqual((self.config.deinterlace, self.config.saves), ("on", 1))
        self.assertEqual(self.shown()[1], ("Deinterlacing", "On"))
        self.step(1, 1)
        self.assertEqual(self.shown()[1], ("Deinterlacing", "Off"))
        self.assertEqual(self.toasts, [])
        self.step(1, 1)  # back to Auto: libVLC 3 keeps on or off on a player till a restart
        self.assertEqual(self.config.deinterlace, picture.AUTO)
        self.assertEqual(self.toasts, ["Deinterlacing is back on Auto when QiTV starts again"])

    def test_a_player_made_later_deinterlaces_as_the_config_says(self):
        self.config.deinterlace = "off"
        tile = PicturePlayer()
        self.controls.add_player(tile)
        self.assertEqual(tile.deinterlacing, "off")
        self.config.deinterlace = picture.AUTO
        other = PicturePlayer()
        self.controls.add_player(other)
        self.assertTrue(other.auto_back)  # never touched: libVLC's own Auto

    def test_the_statistics_row_shows_and_hides_them(self):
        self.player.stats = {"video_codec": "h264", "width": 1920, "height": 1080, "fps": 25.0}
        self.open()
        self.click_label(3)
        self.assertTrue(self.stats.isVisible())
        self.assertEqual(self.shown()[3], "*Statistics")
        self.assertEqual(self.stats.rows()[:1], [("Video", "H.264 · 1920×1080 · 25 fps")])
        self.assertIn(("Source", "Time-shift, HLS"), self.stats.rows())
        self.assertIn(("Behind live", "7.5 s"), self.stats.rows())
        self.player.stats["fps"] = 50.0
        elapse(1100)  # once a second while they show
        self.assertEqual(self.stats.rows()[0], ("Video", "H.264 · 1920×1080 · 50 fps"))
        self.controls.handle(Action.STATS)  # Shift+I, the panel stays
        self.assertFalse(self.stats.isVisible())
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.shown()[3], "Statistics")

    def test_shift_i_shows_them_without_the_panel_and_nothing_playing_hides_them(self):
        self.controls.toggle_stats()
        self.assertTrue(self.stats.isVisible())
        self.assertFalse(self.panel.isVisible())
        self.playing = False
        elapse(1100)
        self.assertFalse(self.stats.isVisible())
        self.assertTrue(self.controls.stats_shown())  # they come back with the next picture
        self.playing = True
        elapse(1100)
        self.assertTrue(self.stats.isVisible())

    def test_a_opens_it_on_the_aspect_and_again_steps_it(self):
        self.transport.dismiss()
        self.controls.open_from_key()
        self.assertTrue(self.panel.isVisible())
        self.assertTrue(self.transport.isVisible())  # it opens above its button
        self.assertLessEqual(self.panel.geometry().bottom(), self.transport.controls_top())
        self.assertEqual(self.panel.focused(), 0)
        self.assertTrue(self.controls.handle(Action.PICTURE))
        self.assertEqual(self.player.shape, WIDE)
        self.assertTrue(self.controls.handle(Action.PICTURE))
        self.assertEqual(self.player.shape, STANDARD)
        self.assertEqual(self.toasts, [])  # the panel shows it

    def test_the_keys_move_step_choose_and_close(self):
        self.controls.open_from_key()
        self.assertTrue(self.controls.handle(Action.SEEK_FWD))  # → on the aspect row
        self.assertEqual(self.player.shape, WIDE)
        self.assertTrue(self.controls.handle(Action.SEEK_BACK))
        self.assertEqual(self.player.shape, picture.AUTO)
        self.assertTrue(self.controls.handle(Action.CH_NEXT, DOWN))
        self.assertTrue(self.controls.handle(Action.OK))  # Enter on a setting: on
        self.assertEqual(self.config.deinterlace, "on")
        for _ in range(2):
            self.controls.handle(Action.CH_NEXT, DOWN)
        self.assertEqual(self.panel.focused(), 3)
        self.controls.handle(Action.OK)
        self.assertTrue(self.controls.stats_shown())
        self.assertTrue(self.controls.handle(Action.ESCAPE))
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.transport.held(), "")

    def test_the_first_down_lands_on_the_first_row_though_statistics_are_ticked(self):
        self.controls.toggle_stats()
        self.open()
        self.controls.handle(Action.CH_NEXT, DOWN)
        self.assertEqual(self.panel.focused(), 0)

    def test_another_key_closes_it_and_goes_on(self):
        self.open()
        self.assertFalse(self.controls.handle(Action.GUIDE))
        self.assertFalse(self.panel.isVisible())
        self.assertFalse(self.controls.handle(Action.GUIDE))  # closed: not its own

    def test_the_setting_rows_name_their_keys(self):
        self.open()
        tips = [self.panel.tooltip_at(self.panel.row_rect(i).center()) for i in (0, 3)]
        self.assertEqual(tips, [key_hint("Aspect ratio", "A"), key_hint("Statistics", "Shift+I")])

    def test_without_a_panel_a_steps_the_aspect_with_a_toast(self):
        controls = PictureControls(
            panel=None,
            transport=self.transport,
            stats=None,
            notice=None,
            session=lambda: self.player,
            current=lambda: self.current,
            playing=lambda: True,
            players=lambda: [self.player],
            channel_key=lambda item: f"key-{item['id']}",
            store=self.store,
            config=self.config,
            toast=lambda text, *args: self.toasts.append(text),
            programme_left=lambda: None,
            live_info=dict,
            stop=self.stop,
        )
        controls.open_from_key()
        self.assertEqual(self.toasts, ["Aspect ratio: 16:9"])
        controls.toggle_stats()  # no overlay: nothing to show, nothing breaks

    def test_nothing_playing_nothing_opens(self):
        self.playing = False
        self.open()
        self.assertFalse(self.panel.isVisible())


class SmallWindowTests(PictureCase):
    def test_it_fits_a_mini_player_and_its_rows_scroll(self):
        self.host.resize(360, 203)
        self.transport.setGeometry(self.host.rect())
        self.open()
        self.assertTrue(self.panel.isVisible())
        geometry = self.panel.geometry()
        self.assertGreaterEqual(geometry.top(), 0)
        self.assertLessEqual(geometry.right(), 360)
        self.assertLessEqual(geometry.bottom(), self.transport.controls_top())
        shown = self.panel.shown_rows()
        for index in shown:
            row = self.panel.model().rows[index]
            if row.value:
                self.assertFalse(self.panel.step_rect(index, 1).isEmpty())
                self.assertFalse(
                    self.panel.step_rect(index, -1).intersects(self.panel.step_rect(index, 1))
                )


class AspectMemoryTests(PictureCase):
    def test_a_channel_opens_in_the_shape_kept_for_it_and_a_film_in_its_own(self):
        self.store.set_aspect("key-1", FILL)
        self.controls.prepare(self.player, CHANNEL, None)
        self.assertEqual(self.player.shape, FILL)
        self.controls.prepare(self.player, OTHER, CHANNEL)
        self.assertEqual(self.player.shape, picture.AUTO)
        self.player.shape = STANDARD  # chosen during a film: a retry keeps it
        self.controls.prepare(self.player, FILM, OTHER)
        self.assertEqual(self.player.shape, picture.AUTO)
        self.player.shape = STANDARD
        self.controls.prepare(self.player, FILM, FILM)
        self.assertEqual(self.player.shape, STANDARD)
        self.controls.prepare_small(self.small, CHANNEL.item)  # PiP, a tile
        self.assertEqual(self.small.shape, FILL)


class ChannelPicturesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.path = os.path.join(self.dir, picture.FILE_NAME)

    def test_a_restart_reads_what_was_kept_and_auto_is_forgotten(self):
        store = ChannelPictures(self.path, now=lambda: 50.0)
        store.set_aspect("a", WIDE)
        store.set_aspect("b", FILL)
        store.set_aspect("b", picture.AUTO)
        store.flush()
        with open(self.path, "rb") as f:
            data = json.loads(f.read())
        self.assertEqual(data, {"version": 1, "channels": {"a": {"aspect": "16:9", "set": 50.0}}})
        again = ChannelPictures(self.path)
        self.assertEqual((again.aspect("a"), again.aspect("b")), (WIDE, picture.AUTO))

    def test_a_later_builds_optional_fields_are_read_past(self):
        # Version 1 stays readable: a later build adds only optional fields to an entry.
        with open(self.path, "w") as f:
            json.dump(
                {
                    "version": 1,
                    "channels": {
                        "a": {"aspect": "4:3", "set": 1.0, "zoom": 1.5},
                        "b": {"aspect": "21:9", "set": 1.0},  # not one this build has
                        "c": {"zoom": 2, "set": 1.0},
                    },
                },
                f,
            )
        store = ChannelPictures(self.path)
        self.assertEqual(store.aspect("a"), STANDARD)
        self.assertEqual((store.aspect("b"), store.aspect("c")), (picture.AUTO, picture.AUTO))

    def test_the_sound_syncs_file_is_left_as_older_builds_read_it(self):
        sync = SoundSync(os.path.join(self.dir, "sound_sync.json"))
        sync.set_audio_ms("a", 150)
        sync.flush()
        store = ChannelPictures(self.path)
        store.set_aspect("a", WIDE)
        store.flush()
        self.assertEqual(SoundSync(os.path.join(self.dir, "sound_sync.json")).audio_ms("a"), 150)
        self.assertEqual(sorted(os.listdir(self.dir)), ["picture.json", "sound_sync.json"])

    def test_another_version_or_a_damaged_file_counts_as_empty(self):
        for content in ('{"version": 2, "channels": {"a": {"aspect": "4:3", "set": 1}}}', "{x"):
            with self.subTest(content=content):
                with open(self.path, "w") as f:
                    f.write(content)
                self.assertEqual(ChannelPictures(self.path).aspect("a"), picture.AUTO)

    def test_the_oldest_choices_go_first_beyond_the_cap(self):
        times = iter(range(10))
        store = ChannelPictures(None, max_entries=2, now=lambda: float(next(times)))
        for key in ("a", "b", "c"):
            store.set_aspect(key, WIDE)
        self.assertEqual(sorted(store._entries), ["b", "c"])


class ShapeArgsTests(unittest.TestCase):
    def test_libvlc_gets_an_aspect_or_a_crop_to_the_windows_shape(self):
        self.assertEqual(picture.shape_args(picture.AUTO, 1280, 720), (None, None))
        self.assertEqual(picture.shape_args(WIDE, 1280, 720), ("16:9", None))
        self.assertEqual(picture.shape_args(STANDARD, 0, 0), ("4:3", None))
        self.assertEqual(picture.shape_args(FILL, 1280, 720), (None, "16:9"))
        self.assertEqual(picture.shape_args(FILL, 1279, 719), (None, "1279:719"))
        self.assertEqual(picture.shape_args(FILL, 0, 720), (None, None))  # no size yet


class StatsTextTests(unittest.TestCase):
    STATS = {
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "fps": 29.97,
        "audio_codec": "mp4a",
        "audio_rate": 48000,
        "decoded": 12345,
        "shown": 12300,
        "lost": 45,
        "input_kbps": 3657.4,
        "demux_kbps": 850.2,
        "decoding": "hardware",
        "source": "fmp4",
        "lag": 12.34,
    }

    def tearDown(self):
        i18n.set_language("en")

    def test_the_rows_read_in_plain_words(self):
        self.assertEqual(
            stats_rows(self.STATS),
            [
                ("Video", "H.264 · 1920×1080 · 29.97 fps"),
                ("Audio", "AAC · 48 kHz"),
                ("Decoding", "Hardware where VLC can"),
                ("Pictures", "12,345 decoded · 12,300 shown · 45 too late"),
                ("Bitrate", "3.7 Mb/s read · 850 kb/s demuxed"),
                ("Source", "Time-shift, HLS (fragmented MP4 as TS)"),
                ("Behind live", "12.3 s"),
            ],
        )

    def test_what_is_not_known_yet_is_left_out(self):
        self.assertEqual(stats_rows({}), [])
        rows = stats_rows({"video_codec": "mpgv", "decoding": "froze", "source": "direct"})
        self.assertEqual(
            rows,
            [
                ("Video", "MPEG-2"),
                ("Decoding", "Software (the picture froze in hardware)"),
                ("Source", "Direct"),
            ],
        )
        self.assertEqual(stats_rows({"lag": None, "source": ""}), [])

    def test_an_unknown_codec_reads_as_its_code(self):
        self.assertEqual(picture.codec_name("WVC1"), "WVC1")
        self.assertEqual(picture.codec_name("a52 "), "AC-3")
        self.assertEqual(picture.bitrate_text(0.0), "0 kb/s")

    def test_turkish_writes_its_own_numbers(self):
        i18n.set_language("tr")
        rows = dict(stats_rows(self.STATS))
        self.assertEqual(rows["Video"], "H.264 · 1920×1080 · 29,97 kare/sn")
        self.assertEqual(rows["Kareler"], "12.345 çözüldü · 12.300 gösterildi · 45 geç kaldı")
        self.assertEqual(rows["Canlının gerisinde"], "12,3 sn")


class SleepTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = Clock()
        self.timer = SleepTimer(clock=self.clock, tick_ms=10)
        self.said = []
        self.timer.warning.connect(lambda: self.said.append("warning"))
        self.timer.expired.connect(lambda: self.said.append("expired"))
        self.addCleanup(self.timer.cancel)

    def test_it_warns_a_minute_before_and_ends_once(self):
        self.timer.start(15 * 60)
        self.assertEqual(self.timer.remaining(), 900.0)
        self.clock.now += 839
        elapse(40)
        self.assertEqual(self.said, [])
        self.clock.now += 2
        elapse(40)
        self.assertEqual(self.said, ["warning"])
        self.assertTrue(self.timer.warned())
        self.clock.now += 59
        elapse(40)
        self.assertEqual(self.said, ["warning", "expired"])
        self.assertFalse(self.timer.running())
        elapse(40)
        self.assertEqual(self.said, ["warning", "expired"])

    def test_a_start_with_less_than_a_minute_warns_at_once_and_cancel_is_unsaid(self):
        self.timer.start(30)
        elapse(40)
        self.assertEqual(self.said, ["warning"])
        self.timer.cancel()
        self.clock.now += 60
        elapse(40)
        self.assertEqual(self.said, ["warning"])
        self.assertEqual(self.timer.remaining(), 0.0)

    def test_the_time_left_reads_as_a_clock(self):
        self.assertEqual(clock_text(28 * 60 + 41), "28:41")
        self.assertEqual(clock_text(5400), "1:30:00")
        self.assertEqual(clock_text(0.2), "0:01")  # rounded up: never 0:00 early


class SleepFlowTests(PictureCase):
    def test_its_row_offers_the_programmes_end_only_when_the_guide_knows_it(self):
        self.open()
        values = []
        for _ in range(5):
            self.step(2, 1)
            values.append(self.controls.sleep_choice())
        self.assertEqual(values, [15, 30, 60, 90, "off"])
        self.left = 47 * 60.0
        self.step(2, -1)
        self.assertEqual(self.controls.sleep_choice(), "end")
        self.assertEqual(self.timer.remaining(), 47 * 60.0)
        self.assertEqual(self.shown()[2], ("Sleep timer", "47:00 left"))
        self.assertTrue(self.panel.model().rows[2].lit)

    def test_the_row_counts_down_while_the_panel_shows(self):
        self.open()
        self.step(2, 1)
        self.assertEqual(self.shown()[2], ("Sleep timer", "15:00 left"))
        self.clock.now += 61
        elapse(40)
        self.assertEqual(self.shown()[2], ("Sleep timer", "13:59 left"))

    def test_a_minute_before_a_notice_says_so_and_keep_watching_turns_it_off(self):
        self.controls.set_sleep(15)
        self.clock.now += 14 * 60 + 18
        elapse(40)
        self.assertTrue(self.notice.isVisible())
        self.assertEqual(self.notice.text(), "Playback stops in 0:42")
        self.click(self.notice, self.notice.rect().center())
        self.assertFalse(self.notice.isVisible())
        self.assertFalse(self.timer.running())
        self.assertEqual(self.toasts, ["Sleep timer off"])
        self.clock.now += 120
        elapse(40)
        self.assertEqual((self.stops, self.sleeps), (0, []))

    def test_enter_keeps_watching_while_the_notice_shows(self):
        self.controls.set_sleep(30)
        self.assertFalse(self.controls.handle(Action.OK))  # no notice yet: the drawer's
        self.clock.now += 29 * 60 + 30
        elapse(40)
        self.assertTrue(self.controls.handle(Action.OK))
        self.assertFalse(self.timer.running())

    def test_at_the_end_playback_stops_and_the_computer_may_sleep(self):
        self.controls.toggle_stats()
        self.open()
        self.controls.set_sleep(15)
        self.clock.now += 15 * 60
        elapse(40)
        self.assertEqual(self.stops, 1)
        self.assertEqual(self.sleeps, ["allow"])
        self.assertEqual(self.toasts, ["Sleep timer: playback stopped"])
        self.assertFalse(self.notice.isVisible())
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.controls.sleep_choice(), "off")
        self.controls.on_playing()  # something plays again: the computer stays awake
        self.controls.on_playing()
        self.assertEqual(self.sleeps, ["allow", "prevent"])

    def test_it_is_never_kept_and_a_close_ends_it_unsaid(self):
        self.controls.set_sleep(60)
        self.controls.shutdown()
        self.clock.now += 3600
        elapse(40)
        self.assertEqual((self.stops, self.sleeps, self.toasts), (0, [], []))


class PictureSessionTests(unittest.TestCase):
    """PlaybackSession's shape and deinterlacing, against the fake libVLC player."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = _PicturePlayer()
        self.runner = sessions._HeldRunner(self.player)
        self.session = PlaybackSession(
            sessions._FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.addCleanup(self.session.release)

    def play(self, url):
        self.session.play(url, kind=PlayKind.LIVE)

    def picture_calls(self):
        return [c for c in self.player.calls if c[0].startswith("video_set_")]

    def test_nothing_reaches_libvlc_until_a_shape_or_deinterlacing_is_asked_for(self):
        self.play(sessions.A)
        self.session._tick()
        self.assertEqual(self.picture_calls(), [])
        self.assertTrue(self.session.set_deinterlace(picture.AUTO))
        self.assertEqual(self.picture_calls(), [])

    def test_a_shape_applies_at_once_and_again_on_each_media_once_its_output_exists(self):
        self.play(sessions.A)
        self.session.set_aspect(WIDE)
        self.assertEqual(
            self.picture_calls(),
            [("video_set_aspect_ratio", "16:9"), ("video_set_crop_geometry", None)],
        )
        self.player.calls.clear()
        self.play(sessions.B)  # a zap: libVLC's input is being joined
        self.session.set_aspect(STANDARD)
        self.assertEqual(self.picture_calls(), [])  # never while the worker holds it
        self.runner.finish()
        self.assertEqual(
            self.picture_calls(),
            [("video_set_aspect_ratio", "4:3"), ("video_set_crop_geometry", None)],
        )
        self.player.calls.clear()
        self.session._tick()  # no video output yet
        self.assertEqual(self.picture_calls(), [])
        self.player.vouts = 1  # the kept video output is this media's now: set again
        self.session._tick()
        self.session._tick()
        self.assertEqual(
            self.picture_calls(),
            [("video_set_aspect_ratio", "4:3"), ("video_set_crop_geometry", None)],
        )

    def test_auto_after_a_shape_resets_libvlc(self):
        self.play(sessions.A)
        self.session.set_aspect(WIDE)
        self.session.set_aspect(picture.AUTO)
        self.assertEqual(
            self.picture_calls()[-2:],
            [
                ("video_set_aspect_ratio", None),
                ("video_set_crop_geometry", None),
            ],
        )

    def test_fill_crops_to_the_surfaces_shape_once_a_resize_stops(self):
        self.session.set_video_size(1280, 720)
        self.play(sessions.A)
        self.session.set_aspect(FILL)
        self.assertEqual(self.picture_calls()[-1], ("video_set_crop_geometry", "16:9"))
        self.player.calls.clear()
        for width in (1000, 1100, 1200):
            self.session.set_video_size(width, 600)
        self.assertEqual(self.picture_calls(), [])
        elapse(400)
        self.assertEqual(self.picture_calls()[-1], ("video_set_crop_geometry", "2:1"))
        self.assertEqual(len(self.picture_calls()), 2)  # once, after the last

    def test_deinterlacing_on_or_off_stays_and_auto_needs_a_restart(self):
        self.play(sessions.A)
        self.assertTrue(self.session.set_deinterlace(picture.ON))
        self.assertIn(("video_set_deinterlace", "yadif"), self.picture_calls())
        self.assertTrue(self.session.set_deinterlace(picture.OFF))
        self.assertEqual(self.picture_calls()[-1], ("video_set_deinterlace", None))
        self.assertFalse(self.session.set_deinterlace(picture.AUTO))
        self.assertEqual(self.session.deinterlace(), picture.AUTO)
        self.session.set_deinterlace(picture.ON)
        self.session.set_deinterlace(picture.AUTO)  # libVLC keeps On till a restart
        self.session.set_aspect(WIDE)  # another call: deinterlacing is given again
        self.assertEqual(self.picture_calls()[-1], ("video_set_deinterlace", "yadif"))

    def test_a_fill_crop_waiting_for_a_worker_follows_once_it_let_go(self):
        self.session.set_video_size(1280, 720)
        self.play(sessions.A)
        self.session.set_aspect(FILL)
        self.play(sessions.B)  # the old input is being joined
        self.session.set_video_size(1000, 500)
        elapse(400)
        self.assertNotIn(("video_set_crop_geometry", "2:1"), self.picture_calls())
        self.runner.finish()  # the new media opens: the crop follows the surface
        self.assertEqual(self.picture_calls()[-1], ("video_set_crop_geometry", "2:1"))


class _PicturePlayer(sessions._FakePlayer):
    """libVLC's picture calls (they take its input lock to reach the video outputs)."""

    def __init__(self):
        super().__init__()
        self.vouts = 0

    def video_set_aspect_ratio(self, aspect):
        self._call("video_set_aspect_ratio", aspect)

    def video_set_crop_geometry(self, crop):
        self._call("video_set_crop_geometry", crop)

    def video_set_deinterlace(self, mode):
        self._call("video_set_deinterlace", mode)

    def has_vout(self):
        self._call("has_vout")
        return self.vouts

    def get_media(self):
        return None


class OverlayTests(PictureCase):
    def test_the_statistics_sit_under_the_title_row_and_take_no_click(self):
        self.stats.show_rows("Statistics", [("Video", "H.264"), ("Bitrate", "3.7 Mb/s read")])
        rects = [*self.transport.title_rects().values(), self.transport.badge_rect()]
        title_bottom = max(r.bottom() for r in rects)  # the name, its logo, the star…
        self.assertGreater(self.stats.geometry().top(), title_bottom)
        self.assertTrue(self.stats.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        width = self.stats.width()
        self.stats.show_rows("Statistics", [("Video", "H.264"), ("Bitrate", "3 kb/s read")])
        self.assertEqual(self.stats.width(), width)  # it stands still as numbers change

    def test_the_statistics_stay_under_what_covers_the_player(self):
        drawer = QWidget(self.host)  # added later: above it (the channel list, the guide)
        drawer.show()
        self.stats.show_rows("Statistics", [("Video", "H.264")])
        self.stats.show_rows("Statistics", [("Video", "HEVC")])  # each second's refresh
        siblings = self.host.children()
        self.assertLess(siblings.index(self.stats), siblings.index(drawer))

    def test_the_notice_names_its_key(self):
        self.notice.show_notice("Playback stops in 0:42")
        tip = self.notice.tooltip_at(self.notice.button_rect().center())
        self.assertEqual(tip, key_hint("Keep watching", "Return"))
        self.assertGreater(self.notice.button_rect().left(), 0)
        self.assertLessEqual(self.notice.button_rect().right(), self.notice.width())


if __name__ == "__main__":
    unittest.main()
