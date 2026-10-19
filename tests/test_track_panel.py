"""The Sound and Subtitles panels (2.2): ♫ (or the subtitles button) opens one above its
button, a click or the keys choose a track and nudge the sync, "Load a file…" and a drop load
a subtitle file, and a click elsewhere, Esc or the idle wait closes it."""

import os
import tempfile
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services.sound_sync import SoundSync
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.track_panel import IDLE_MS, PanelModel, PanelRow, PanelSync, TrackPanel
from ui.overlays.transport import Transport
from ui.track_controls import TrackControls, sync_text, toast_text

CHANNEL = Target({"id": 1, "name": "Harbour Nature", "cmd": "http://tv/1"}, "live", "Harbour")
OTHER = Target({"id": 2, "name": "Coast", "cmd": "http://tv/2"}, "live", "Coast")
FILM = Target({"id": "m1", "name": "Lighthouse", "cmd": "http://vod/1"}, "movies", "Lighthouse")
UP, DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value


class FakePlayer(QObject):
    """A player's tracks and sync, as PlaybackSession reports them."""

    stateChanged = Signal(str)

    def __init__(self, audio=((-1, "Disable"), (1, "English"), (2, "Deutsch"))):
        super().__init__()
        self.audio = list(audio)
        self.playing = 1
        self.delay = 0
        self.subtitles = [(-1, "Disable"), (3, "English"), (4, "Français")]
        self.spu = -1  # no subtitles shown
        self.spu_delay = 0
        self.files = []  # what add_subtitle_file() was given
        self.loads = True  # whether libVLC takes a file
        self.clears = 0

    def audio_tracks(self):
        return list(self.audio)

    def audio_track(self):
        return self.playing

    def set_audio_track(self, track_id):
        self.playing = track_id

    def audio_delay(self):
        return self.delay

    def set_audio_delay(self, ms):
        self.delay = ms

    def subtitle_tracks(self):
        return list(self.subtitles)

    def subtitle_track(self):
        return self.spu

    def set_subtitle_track(self, track_id):
        self.spu = track_id

    def subtitle_delay(self):
        return self.spu_delay

    def set_subtitle_delay(self, ms):
        self.spu_delay = ms

    def add_subtitle_file(self, path):
        """As PlaybackSession does once libVLC read it: a track named after the file, chosen."""
        self.files.append(path)
        if not self.loads:
            return False
        self.spu = max(i for i, _ in self.subtitles) + 1
        self.subtitles.append((self.spu, os.path.basename(path)))
        return True

    def clear_subtitles(self):
        self.clears += 1
        self.spu_delay = 0


class TextTests(unittest.TestCase):
    def test_the_sync_reads_in_plain_words(self):
        self.assertEqual(sync_text("Sound", 0), "In sync")
        self.assertEqual(sync_text("Sound", 150), "Sound 150 ms later")
        self.assertEqual(sync_text("Sound", -1050), "Sound 1,050 ms earlier")
        self.assertEqual(toast_text("Sound", 0), "Sound in sync")
        self.assertEqual(toast_text("Sound", -50), "Sound 50 ms earlier")


class PanelCase(unittest.TestCase):
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
        self.player = FakePlayer()
        self.current = CHANNEL
        self.store = SoundSync(None)
        self.toasts = []
        self.playing = True
        self.config = SimpleNamespace(subtitle_folder="")
        self.picks = []  # the folders the file dialog opened in
        self.chosen = ""  # what the file dialog gives back
        self.panel = TrackPanel(self.host)
        self.subtitle_panel = TrackPanel(self.host)
        self.controls = self.make_controls()

    def pick(self, folder):
        self.picks.append(folder)
        return self.chosen

    def make_controls(self, panel="default", subtitle_panel="default"):
        controls = TrackControls(
            sound_panel=self.panel if panel == "default" else panel,
            subtitle_panel=self.subtitle_panel if subtitle_panel == "default" else subtitle_panel,
            transport=self.transport,
            session=lambda: self.player,
            current=lambda: self.current,
            playing=lambda: self.playing,
            channel_key=lambda item: f"key-{item['id']}",
            store=self.store,
            toast=lambda text, *args: self.toasts.append(text),
            show_transport=self.transport.poke,
            pick_file=self.pick,
            config=self.config,
        )
        controls.wire()
        return controls

    def click(self, widget, point):
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)

    def click_button(self, name):
        self.click(self.transport, self.transport.button_rect(name).center().toPoint())

    def click_panel(self, rect):
        self.click(self.panel, rect.center().toPoint())

    def shown(self, panel=None):
        """What the panel shows: its rows (ticked marked "*") and its Sync text."""
        model = (panel or self.panel).model()
        rows = [("*" if r.checked else "") + r.text for r in model.rows]
        return rows, model.sync.text if model.sync else None


class SoundPanelTests(PanelCase):
    def test_the_button_opens_it_above_itself_and_the_controls_stay_up(self):
        self.click_button("audio")
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.shown(), (["*English", "Deutsch"], "In sync"))
        self.assertEqual(self.panel.model().title, "Sound")
        button = self.transport.button_rect("audio")
        self.assertAlmostEqual(self.panel.geometry().center().x(), button.center().x(), delta=2)
        self.assertLessEqual(self.panel.geometry().bottom(), self.transport.controls_top())
        self.assertEqual(self.transport.held(), "audio")  # lit, and no fade meanwhile
        elapse(400)
        self.assertTrue(self.transport.isVisible())

    def test_a_click_on_a_track_plays_it(self):
        self.click_button("audio")
        self.click_panel(self.panel.row_rect(1))
        self.assertEqual(self.player.playing, 2)
        self.assertEqual(self.shown()[0], ["English", "*Deutsch"])
        self.assertTrue(self.panel.isVisible())

    def test_one_track_is_no_choice(self):
        self.player.audio = [(-1, "Disable"), (1, "English")]
        self.click_button("audio")
        self.assertEqual(self.shown(), ([], "In sync"))

    def test_minus_and_plus_nudge_the_sound_and_reset_brings_it_back(self):
        self.click_button("audio")
        for _ in range(3):
            self.click_panel(self.panel.control_rect("later"))
        self.assertEqual(self.player.delay, 150)
        self.assertEqual(self.shown()[1], "Sound 150 ms later")
        self.assertEqual(self.store.audio_ms("key-1"), 150)  # kept for the channel
        self.click_panel(self.panel.control_rect("reset"))
        self.assertEqual((self.player.delay, self.shown()[1]), (0, "In sync"))
        self.assertTrue(self.panel.control_rect("reset").isEmpty())
        self.click_panel(self.panel.control_rect("earlier"))
        self.assertEqual(self.shown()[1], "Sound 50 ms earlier")
        self.assertEqual(self.toasts, [])  # the panel shows it

    def test_a_quick_second_click_steps_again(self):
        self.click_button("audio")
        point = self.panel.mapTo(self.host, self.panel.control_rect("later").center().toPoint())
        QTest.mouseDClick(  # as a mouse does it: press, release, a press that is a double-click
            self.host.windowHandle(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            point,
        )
        self.assertEqual(self.player.delay, 100)

    def test_the_sync_stops_at_five_seconds(self):
        self.player.delay = 4950
        self.click_button("audio")
        self.click_panel(self.panel.control_rect("later"))
        self.click_panel(self.panel.control_rect("later"))
        self.assertEqual(self.player.delay, 5000)
        self.assertFalse(self.panel.model().sync.later)

    def test_minus_and_plus_name_their_keys(self):
        self.click_button("audio")
        tip = self.panel.tooltip_at(self.panel.control_rect("earlier").center())
        self.assertEqual(tip, "Sound 50 ms earlier (J)")
        tip = self.panel.tooltip_at(self.panel.control_rect("later").center())
        self.assertEqual(tip, "Sound 50 ms later (K)")

    def test_a_click_elsewhere_closes_it_and_does_nothing_more(self):
        self.click_button("audio")
        spy = QSignalSpy(self.transport.playPauseRequested)
        closed = QSignalSpy(self.panel.closed)
        self.click_button("play")
        self.assertFalse(self.panel.isVisible())
        self.assertEqual((spy.count(), closed.count()), (0, 1))
        self.assertEqual(self.transport.held(), "")  # the fade runs again
        self.click_button("play")
        self.assertEqual(spy.count(), 1)

    def test_a_click_on_its_button_again_closes_it(self):
        self.click_button("audio")
        self.click_button("audio")
        self.assertFalse(self.panel.isVisible())

    def test_a_click_in_another_window_leaves_it_open(self):
        # A dialog (the subtitles' file dialog, a message box) is not "elsewhere".
        self.click_button("audio")
        other = QWidget()
        other.resize(200, 200)
        other.show()
        self.addCleanup(other.deleteLater)
        self.click(other, QPoint(50, 50))
        self.assertTrue(self.panel.isVisible())

    def test_it_closes_on_its_own_after_a_while_and_the_controls_fade_then(self):
        self.assertEqual(IDLE_MS, 10_000)
        self.transport.audioRequested.disconnect(self.controls.open_sound)
        self.panel = TrackPanel(self.host, idle_ms=300)
        self.controls = self.make_controls()
        self.click_button("audio")
        elapse(150)
        self.controls.nudge_sound(1)  # a touch: the wait starts again
        elapse(200)
        self.assertTrue(self.panel.isVisible())
        elapse(200)
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.transport.isVisible())
        elapse(300)
        self.assertFalse(self.transport.isVisible())

    def test_it_closes_with_the_player_controls(self):
        self.click_button("audio")
        self.transport.dismiss()  # the banner took the bottom, the guide opened …
        self.assertFalse(self.panel.isVisible())

    def test_the_keys_move_choose_step_and_close(self):
        self.click_button("audio")
        handle = self.controls.handle
        self.assertTrue(handle(Action.CH_NEXT, DOWN))  # lands on the track playing
        self.assertEqual(self.panel.focused(), 0)
        self.assertTrue(handle(Action.CH_NEXT, DOWN))
        self.assertTrue(handle(Action.OK))
        self.assertEqual(self.player.playing, 2)
        self.assertTrue(handle(Action.CH_NEXT, DOWN))  # the Sync row
        self.assertEqual(self.panel.focused(), 2)
        self.assertTrue(handle(Action.SEEK_FWD))
        self.assertTrue(handle(Action.SEEK_FWD))
        self.assertTrue(handle(Action.SEEK_BACK))
        self.assertEqual(self.player.delay, 50)
        self.assertTrue(handle(Action.CH_PREV, UP))
        self.assertEqual(self.panel.focused(), 1)
        self.assertTrue(handle(Action.ESCAPE))
        self.assertFalse(self.panel.isVisible())
        self.assertFalse(handle(Action.ESCAPE))  # closed: Esc goes on

    def test_another_key_closes_it_and_goes_on_but_the_sync_keys_leave_it_open(self):
        self.click_button("audio")
        self.assertFalse(self.controls.handle(Action.CH_NEXT, Qt.Key.Key_PageDown.value))
        self.assertFalse(self.panel.isVisible())  # PageDown zaps
        self.click_button("audio")
        self.assertFalse(self.controls.handle(Action.SOUND_LATER))
        self.assertTrue(self.panel.isVisible())

    def test_j_and_k_nudge_the_sound_with_a_toast(self):
        self.controls.nudge_sound(1)
        self.controls.nudge_sound(1)
        self.controls.nudge_sound(-1)
        self.assertEqual(self.player.delay, 50)
        self.assertEqual(
            self.toasts, ["Sound 50 ms later", "Sound 100 ms later", "Sound 50 ms later"]
        )
        self.controls.nudge_sound(-1)
        self.assertEqual(self.toasts[-1], "Sound in sync")

    def test_j_and_k_update_an_open_panel_instead_of_a_toast(self):
        self.click_button("audio")
        self.controls.nudge_sound(-1)
        self.assertEqual(self.shown()[1], "Sound 50 ms earlier")
        self.assertEqual(self.toasts, [])

    def test_a_channel_keeps_its_sync_a_film_only_while_it_plays(self):
        self.controls.nudge_sound(3)
        self.assertEqual(self.store.audio_ms("key-1"), 150)
        self.current = FILM
        self.controls.nudge_sound(1)
        self.assertEqual(self.store.audio_ms("key-m1"), 0)  # never stored

    def test_a_channel_opens_with_its_sync_and_a_retry_keeps_the_players(self):
        self.store.set_audio_ms("key-1", 200)
        self.controls.prepare(self.player, CHANNEL, OTHER)
        self.assertEqual(self.player.delay, 200)
        self.controls.prepare(self.player, OTHER, CHANNEL)
        self.assertEqual(self.player.delay, 0)
        self.player.delay = 350  # nudged meanwhile
        self.controls.prepare(self.player, OTHER, OTHER)  # its link made again
        self.assertEqual(self.player.delay, 350)
        self.controls.prepare(self.player, FILM, OTHER)  # a film starts in sync
        self.assertEqual(self.player.delay, 0)
        small = FakePlayer()
        self.controls.prepare_small(small, CHANNEL.item)
        self.assertEqual(small.delay, 200)

    def test_without_a_panel_the_button_cycles_the_tracks(self):
        controls = self.make_controls(panel=None, subtitle_panel=None)
        controls.open_sound()
        controls.open_sound()
        controls.open_subtitles()
        controls.open_subtitles()
        self.assertEqual(
            self.toasts,
            ["Audio: Deutsch", "Audio: English", "Subtitles: English", "Subtitles: Français"],
        )

    def test_nothing_playing_nothing_opens(self):
        self.playing = False
        self.click_button("audio")
        self.controls.nudge_sound(1)
        self.assertFalse(self.panel.isVisible())
        self.assertEqual((self.player.delay, self.toasts), (0, []))


class SubtitlesPanelTests(PanelCase):
    def setUp(self):
        super().setUp()
        self.current = FILM
        self.transport.show_vod("Lighthouse", "")
        self.transport.update_position(0, 100_000)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.srt = os.path.join(directory.name, "Lighthouse.en.srt")
        with open(self.srt, "w", encoding="utf-8") as f:
            f.write("1\n00:00:01,000 --> 00:00:04,000\nHello\n")

    def subtitles(self):
        return self.shown(self.subtitle_panel)

    def test_off_and_the_tracks_then_load_a_file_and_no_sync_while_off(self):
        self.click_button("subtitles")
        panel = self.subtitle_panel
        self.assertTrue(panel.isVisible())
        self.assertEqual(panel.model().title, "Subtitles")
        self.assertEqual(self.subtitles(), (["*Off", "English", "Français", "Load a file…"], None))
        self.assertTrue(panel.model().rows[-1].action)
        self.assertEqual(self.transport.held(), "subtitles")
        button = self.transport.button_rect("subtitles")
        self.assertAlmostEqual(panel.geometry().center().x(), button.center().x(), delta=2)

    def test_a_track_shows_and_its_sync_row_comes(self):
        self.click_button("subtitles")
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(2).center().toPoint())
        self.assertEqual(self.player.spu, 4)
        self.assertEqual(
            self.subtitles(), (["Off", "English", "*Français", "Load a file…"], "In sync")
        )
        later = self.subtitle_panel.control_rect("later")
        self.click(self.subtitle_panel, later.center().toPoint())
        self.click(self.subtitle_panel, later.center().toPoint())
        self.assertEqual(self.player.spu_delay, 100)
        self.assertEqual(self.subtitles()[1], "Subtitles 100 ms later")
        self.assertEqual(self.player.delay, 0)  # the sound's is its own
        self.assertEqual(self.store.audio_ms("key-m1"), 0)
        tip = self.subtitle_panel.tooltip_at(later.center())
        self.assertEqual(tip, key_hint("Subtitles 50 ms later", "Shift+K"))

    def test_load_a_file_asks_for_one_and_shows_it_chosen(self):
        self.chosen = self.srt
        self.click_button("subtitles")
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(3).center().toPoint())
        self.assertEqual(self.picks, [""])
        self.assertEqual(self.player.files, [self.srt])
        self.assertTrue(self.subtitle_panel.isVisible())  # back, with the file
        rows, sync = self.subtitles()
        self.assertEqual(rows, ["Off", "English", "Français", "*Lighthouse.en.srt", "Load a file…"])
        self.assertEqual(sync, "In sync")
        self.assertEqual(self.config.subtitle_folder, os.path.dirname(self.srt))
        self.assertEqual(self.toasts, [])  # the panel shows it
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(4).center().toPoint())
        self.assertEqual(self.picks, ["", os.path.dirname(self.srt)])  # the last folder

    def test_a_dialog_cancelled_loads_nothing(self):
        self.click_button("subtitles")
        self.click(self.subtitle_panel, self.subtitle_panel.row_rect(3).center().toPoint())
        self.assertEqual((self.picks, self.player.files), ([""], []))
        self.assertFalse(self.subtitle_panel.isVisible())
        self.assertEqual(self.transport.held(), "")

    def test_a_file_libvlc_refuses_is_said(self):
        self.player.loads = False
        self.assertFalse(self.controls.load_subtitle_file(self.srt))
        self.assertEqual(self.toasts, ["Couldn't load the subtitles Lighthouse.en.srt"])
        self.assertEqual(self.config.subtitle_folder, "")

    def test_shift_j_and_shift_k_move_the_subtitles_while_one_shows(self):
        self.controls.nudge_subtitles(1)
        self.assertEqual(self.toasts, ["Subtitles are off"])
        self.player.spu = 3
        self.controls.nudge_subtitles(-1)
        self.controls.nudge_subtitles(-1)
        self.assertEqual(self.player.spu_delay, -100)
        self.assertEqual(self.toasts[1:], ["Subtitles 50 ms earlier", "Subtitles 100 ms earlier"])
        self.click_button("subtitles")
        self.controls.nudge_subtitles(1)
        self.assertEqual(self.subtitles()[1], "Subtitles 50 ms earlier")
        self.assertEqual(len(self.toasts), 3)  # the panel says it
        self.assertFalse(self.controls.handle(Action.SUBTITLES_LATER))  # it stays open
        self.assertTrue(self.subtitle_panel.isVisible())

    def test_a_dropped_subtitle_file_loads_and_says_so(self):
        movie = os.path.join(os.path.dirname(self.srt), "Lighthouse.mkv")
        self.assertTrue(self.controls.accepts_drop([movie, self.srt]))
        self.assertFalse(self.controls.accepts_drop([movie]))
        self.assertTrue(self.controls.drop([movie, self.srt]))
        self.assertEqual(self.player.files, [self.srt])
        self.assertEqual(self.toasts, ["Subtitles: Lighthouse.en.srt"])
        self.assertEqual(self.player.spu, 5)
        self.assertFalse(self.controls.drop([movie]))

    def test_nothing_playing_takes_no_drop(self):
        self.playing = False
        self.assertFalse(self.controls.accepts_drop([self.srt]))
        self.assertFalse(self.controls.drop([self.srt]))
        self.assertEqual(self.player.files, [])

    def test_one_panel_at_a_time(self):
        self.click_button("audio")
        self.controls.open_subtitles()
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.subtitle_panel.isVisible())
        self.assertEqual(self.transport.held(), "subtitles")
        self.assertTrue(self.controls.handle(Action.ESCAPE))
        self.assertFalse(self.subtitle_panel.isVisible())
        self.assertEqual(self.transport.held(), "")

    def test_the_panel_follows_a_track_that_comes_later(self):
        self.click_button("subtitles")
        self.player.subtitles.append((9, "Track 3"))
        elapse(700)
        self.assertIn("Track 3", self.subtitles()[0])

    def test_another_title_starts_without_the_files_a_retry_keeps_them(self):
        self.controls.prepare(self.player, FILM, FILM)
        self.assertEqual(self.player.clears, 0)
        self.controls.prepare(self.player, CHANNEL, FILM)
        self.assertEqual(self.player.clears, 1)
        self.controls.prepare_small(self.player, OTHER.item)
        self.assertEqual(self.player.clears, 2)


class PanelWidgetTests(PanelCase):
    def model(self, reset=True):
        rows = (PanelRow("a", "First", True), PanelRow("b", "Second"))
        sync = PanelSync("Sound 150 ms later", "Sound 5,000 ms earlier", reset=reset)
        return PanelModel("Sound", rows, sync)

    def test_it_sits_near_the_bottom_without_the_player_controls(self):
        self.panel.show_panel(self.model())
        geometry = self.panel.geometry()
        self.assertAlmostEqual(geometry.center().x(), 640, delta=1)
        self.assertLess(geometry.bottom(), 720)
        self.assertGreater(geometry.bottom(), 600)

    def test_it_keeps_its_width_while_the_text_changes(self):
        self.panel.show_panel(self.model())
        width = self.panel.width()
        shorter = PanelSync("In sync", "Sound 5,000 ms earlier")
        self.panel.show_panel(PanelModel("Sound", self.model().rows, shorter))
        self.assertEqual(self.panel.width(), width)
        self.assertTrue(self.panel.control_rect("reset").isEmpty())

    def test_back_and_forward_buttons_go_on_to_the_host(self):
        self.panel.show_panel(self.model())
        spy = QSignalSpy(self.panel.rowChosen)
        QTest.mouseClick(
            self.panel,
            Qt.MouseButton.BackButton,
            Qt.KeyboardModifier.NoModifier,
            self.panel.row_rect(0).center().toPoint(),
        )
        self.assertEqual(spy.count(), 0)
        self.assertTrue(self.panel.isVisible())

    def test_the_pointer_lights_the_row_under_it(self):
        self.panel.show_panel(self.model())
        QTest.mouseMove(self.panel, self.panel.row_rect(1).center().toPoint())
        self.assertEqual(self.panel._hover, 1)
        self.assertIsNone(self.panel.focused())  # the keys' row is the keys'

    def test_wheel_over_it_does_not_reach_the_host(self):
        self.panel.show_panel(self.model())
        pos = QPointF(self.panel.row_rect(0).center())
        from PySide6.QtGui import QWheelEvent

        event = QWheelEvent(
            pos,
            QPointF(self.panel.mapToGlobal(pos.toPoint())),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(self.panel, event)
        self.assertTrue(event.isAccepted())


if __name__ == "__main__":
    unittest.main()
