"""Catch-up in the guide (2.3): on a channel with a provider archive the window goes back, a
past programme is chosen (the window's left edge, or a click), and Enter, a double-click or
the footer's "Watch from the start" play it from its start; the controller opens its archive.
"""

from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from test_catchup_playback import ARCHIVED, PLAIN, CatchupCase
from test_tv_controller import NOW as CONTROLLER_NOW
from timer_fixtures import wait_until

from services.epg_view import Programme
from services.live.tuning import channel_key
from services.zapping import ZapContext
from ui.guide_controller import GuideController
from ui.keymap import key_hint
from ui.overlays.guide import WINDOW, Guide, slot_start

NOW = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)
CHANNELS = [
    {"id": "a", "name": "Harbour News", "number": "1", "archive_days": 2},
    {"id": "b", "name": "Quiet Pier", "number": "2"},
]


def half_hours(channel, start):
    """Half-hour programmes from two days ago to five hours ahead, as a guide lists them."""
    first = slot_start(NOW) - timedelta(days=2, hours=1)
    shows = [
        Programme(
            f"Show {i}", first + i * timedelta(minutes=30), first + (i + 1) * timedelta(minutes=30)
        )
        for i in range(110)
    ]
    return [p for p in shows if p.stop > start][:12]


class GuideCatchupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.guide = Guide(
            None,
            lambda channel, start, limit: half_hours(channel, start),
            lambda: NOW,
            self.host,
            archive_days=lambda channel: int(channel.get("archive_days") or 0),
        )
        self.tuned, self.asked = [], []
        self.guide.tuneRequested.connect(self.tuned.append)
        self.guide.programmeRequested.connect(lambda c, p: self.asked.append((c["id"], p.title)))
        self.guide.open_guide(CHANNELS, 0)

    def x_at(self, moment):
        layout = self.guide._layout()
        offset = (moment - self.guide.window_start()).total_seconds() / WINDOW.total_seconds()
        return layout.times.left() + offset * layout.times.width()

    def point(self, row, moment):
        layout = self.guide._layout()
        y = layout.rows.top() + (row + 0.5) * layout.row_h
        return QPoint(round(self.x_at(moment)), round(y))

    def test_the_window_goes_back_as_far_as_the_deepest_archive(self):
        self.guide.shift(-1)
        self.assertEqual(self.guide.window_start(), slot_start(NOW) - timedelta(minutes=30))
        self.guide.shift(-1000)
        self.assertEqual(self.guide.window_start(), slot_start(NOW) - timedelta(days=2))
        self.guide.open_guide([CHANNELS[1]], 0)  # no archive listed: from now, as before
        self.guide.shift(-1)
        self.assertEqual(self.guide.window_start(), slot_start(NOW))

    def test_enter_tunes_the_channel_at_now_and_plays_the_left_edge_back_in_time(self):
        self.guide.activate()
        self.assertEqual((self.tuned, self.asked), ([CHANNELS[0]], []))
        self.guide.shift(-2)  # 18:30: the programme at the window's left edge
        self.assertEqual(self.guide.chosen().start, slot_start(NOW) - timedelta(hours=1))
        self.guide.activate()
        self.assertEqual(self.asked, [("a", self.guide.chosen().title)])
        self.guide.move_by(1)  # no archive: Enter tunes it
        self.assertIsNone(self.guide.chosen())
        self.guide.activate()
        self.assertEqual(self.tuned[-1], CHANNELS[1])

    def test_a_click_chooses_an_ended_programme_and_a_double_click_plays_it(self):
        self.guide.shift(-2)
        ended = slot_start(NOW) - timedelta(minutes=30)  # 19:00-19:30
        QTest.mouseClick(
            self.guide, Qt.LeftButton, Qt.NoModifier, self.point(0, ended + timedelta(minutes=10))
        )
        self.assertEqual(self.guide.chosen().start, ended)
        QTest.mouseDClick(
            self.guide, Qt.LeftButton, Qt.NoModifier, self.point(0, ended + timedelta(minutes=10))
        )
        self.assertEqual(self.asked[-1][0], "a")
        self.assertEqual(self.tuned, [])

    def test_the_footer_button_plays_the_programme_on_now_from_its_start(self):
        button = self.guide.watch_button()
        self.assertFalse(button.isEmpty())  # on now, and its start is in the archive
        self.assertEqual(self.guide.tooltip_at(button.center()), "Watch from the start")
        center = button.center()
        QTest.mouseClick(
            self.guide, Qt.LeftButton, Qt.NoModifier, QPoint(round(center.x()), round(center.y()))
        )
        self.assertEqual(self.asked, [("a", half_hours(None, NOW)[0].title)])
        self.guide.shift(-1)
        self.assertEqual(
            self.guide.tooltip_at(self.guide.watch_button().center()),
            key_hint("Watch from the start", "Return"),
        )
        self.guide.move_by(1)
        self.assertTrue(self.guide.watch_button().isEmpty())
        self.assertFalse(self.guide.grab().isNull())

    def test_an_archive_that_failed_lately_offers_nothing(self):
        guide = Guide(None, lambda c, s, n: half_hours(c, s), lambda: NOW, self.host)
        guide.open_guide(CHANNELS, 0)  # archive_days: none
        guide.shift(-1)
        self.assertEqual(guide.window_start(), slot_start(NOW) - timedelta(minutes=30))
        self.assertIsNone(guide.chosen())
        self.assertTrue(guide.watch_button().isEmpty())
        guide.close_guide()


class GuideControllerCatchupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_programme_closes_the_guide_and_asks_with_its_group(self):
        host = QWidget()
        self.addCleanup(host.close)
        guide = Guide(None, lambda c, s, n: [], lambda: NOW, parent=host)
        controller = GuideController(guide)
        asked = []
        controller.catchupRequested.connect(lambda *args: asked.append(args))
        controller.open(ZapContext("news", CHANNELS), CHANNELS[0], lambda: [])
        show = Programme("Show", NOW - timedelta(hours=2), NOW - timedelta(hours=1))
        guide.programmeRequested.emit(CHANNELS[0], show)
        self.assertEqual(asked, [(CHANNELS[0], show, "news", CHANNELS)])
        self.assertFalse(controller.is_open())


class ControllerCatchupTests(CatchupCase):
    """The guide's "Watch from the start" through the controller (fake player and session)."""

    SHOW = Programme(
        "Morning",
        CONTROLLER_NOW - timedelta(hours=2, minutes=30),
        CONTROLLER_NOW - timedelta(hours=1, minutes=30),
    )

    def watch_programme(self, channel, programme=None):
        self.controller._guide.catchupRequested.emit(
            channel, programme or self.SHOW, "10", [ARCHIVED, PLAIN]
        )

    def test_another_channels_programme_opens_its_archive_once_live_lets_go(self):
        self.start_archived(PLAIN)
        self.watch_programme(ARCHIVED)
        _id, item, _section, _episode = self.catalog.links[-1]
        self.assertIs(item, ARCHIVED)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.assertEqual(self.live.calls[-1], ("stop",))  # the playing channel closes first
        self.assertEqual(self.archive_plays(), [])
        self.live.released.emit()
        url, kwargs = self.archive_plays()[-1]
        # 18:00 to 19:00, and 5 minutes more: 65 minutes from 18:00.
        self.assertEqual(url, "http://panel.test/timeshift/viewer/secret/65/2026-10-15:18-00/1.ts")
        self.assertIsNone(kwargs["start_ms"])
        self.assertTrue(self.catchup.active())
        self.assertEqual(self.controller._current.item, ARCHIVED)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_the_channel_playing_opens_its_archive_without_a_new_link(self):
        self.start_archived()
        links = len(self.catalog.links)
        self.watch_programme(ARCHIVED)
        self.assertEqual(len(self.catalog.links), links)
        self.live.released.emit()
        self.assertIn("/65/2026-10-15:18-00/1.ts", self.archive_plays()[-1][0])

    def test_the_programmes_end_runs_on_into_the_next_stretch(self):
        self.start_archived()
        self.watch_programme(ARCHIVED)
        self.live.released.emit()
        self.session.stateChanged.emit("playing")
        self.played_ms = 65 * 60_000  # 19:05: the stretch's end, far from now
        self.session.ended.emit()
        self.assertTrue(self.catchup.active())
        self.assertIn("/85/2026-10-15:19-05/1.ts", self.archive_plays()[-1][0])  # on to now

    def test_an_archive_that_fails_plays_the_channel_live(self):
        self.start_archived(PLAIN)
        self.watch_programme(ARCHIVED)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.live.released.emit()
        links = len(self.catalog.links)
        self.session.errorOccurred.emit("The stream could not be opened")
        self.runner.answer(-1, "none")  # probed once VLC let go: nothing there
        self.assertTrue(self.memory.failed(channel_key("Home", ARCHIVED)))
        self.assertEqual(len(self.catalog.links), links + 1)
        self.catalog.linkReady.emit(self.catalog.links[-1][0], ARCHIVED["cmd"])
        self.session.stateChanged.emit("idle")
        self.assertTrue(wait_until(lambda: self.live.running))
        self.assertEqual(self.starts()[-1][0], ARCHIVED["cmd"])  # live through the buffer
        self.assertEqual(self.controller.archive_days(ARCHIVED), 0)  # the guide offers none
        self.assertEqual(self.controller.archive_days(PLAIN), 0)

    def test_the_guide_reach_is_the_channels_archive(self):
        self.assertEqual(self.controller.archive_days(ARCHIVED), 3)
        self.config.play_in_vlc = True  # an external player plays the channels: none
        self.assertEqual(self.controller.archive_days(ARCHIVED), 0)


if __name__ == "__main__":
    unittest.main()
