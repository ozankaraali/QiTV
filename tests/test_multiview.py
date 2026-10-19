"""The multiview (stage 1): the grid's rects, its frame, its tiles overlay, the session
manager's tiles, and the shell's grid over fake players (test_tv_pip's PipCase: real tuners,
manager and budget), then once in the window with libVLC's dummy outputs."""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, qInstallMessageHandler
from PySide6.QtGui import QMouseEvent, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import wait_until

from services.connection_budget import ConnectionBudget
from services.sessions import SessionManager, Slot
from services.zapping import ZapContext
from tests.test_tv_controller import CHANNELS, FakeLiveSession, FakeSession
from tests.test_tv_pip import MOVIE, FakeMixer, PipCase
from tests.test_tv_window import INSTANCE, WindowCase
from ui.grid_controls import LAYOUT_NAMES, LEAVE_FIRST, LIVE_ONLY, NO_STREAM
from ui.grid_frame import GridFrame, grid_rects
from ui.keymap import Action
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.overlays.grid_tiles import (
    DENIED,
    EMPTY,
    FAILED,
    OPENING,
    PLAYING,
    GridTiles,
    TileView,
)
from ui.overlays.pip_label import LIVE

LEFT = Qt.MouseButton.LeftButton
# Another provider's channel, with an address of its own (not One to Four's at Home).
HARBOR_NINE = {"id": 9, "name": "Nine", "number": "9", "cmd": "ffrt http://harbor/9"}
HARBOR_NINE["_provider"] = "Harbor IPTV"
PAGE = QSize(1280, 720)
_OFFSCREEN_NOISE = (
    "This plugin does not support raise()",
    "This plugin does not support setting window masks",
)
_previous_handler = None


def _quiet_offscreen(mode, context, message):
    if message in _OFFSCREEN_NOISE:
        return
    if _previous_handler is not None:
        _previous_handler(mode, context, message)
    else:
        sys.stderr.write(message + "\n")


def setUpModule():
    global _previous_handler
    _previous_handler = qInstallMessageHandler(_quiet_offscreen)


def tearDownModule():
    qInstallMessageHandler(_previous_handler)


def press(widget, point):
    """A left press at `point` (the widget's coordinates), as the platform delivers it."""
    global_point = QPointF(widget.mapToGlobal(point))
    kind, modifiers = QEvent.Type.MouseButtonPress, Qt.KeyboardModifier.NoModifier
    event = QMouseEvent(kind, QPointF(point), global_point, LEFT, LEFT, modifiers)
    QApplication.sendEvent(widget, event)


class Recorder(QWidget):
    """A surface that counts the presses reaching it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.presses = 0

    def mousePressEvent(self, event):
        self.presses += 1


class GridRectsTests(unittest.TestCase):
    def tiles(self, cells, size=PAGE):
        rects = grid_rects(size, cells)
        self.assertEqual(len(rects), cells)
        page = QRect(QPoint(0, 0), size)
        for index, rect in enumerate(rects):
            self.assertTrue(page.contains(rect), rect)
            self.assertAlmostEqual(rect.width() / rect.height(), 16 / 9, delta=0.02)
            for other in rects[index + 1 :]:
                self.assertFalse(rect.intersects(other), (rect, other))
        return rects

    def test_four_tiles_fill_a_16_by_9_page_two_by_two(self):
        rects = self.tiles(4)
        self.assertEqual(len({r.x() for r in rects}), 2)
        self.assertEqual(len({r.y() for r in rects}), 2)
        self.assertGreater(rects[0].width(), 600)  # nearly half the page: a small gap only
        self.assertLess(rects[0].left(), 10)
        self.assertGreater(rects[3].right(), PAGE.width() - 10)

    def test_two_tiles_sit_side_by_side_centred_vertically(self):
        left, right = self.tiles(2)
        self.assertEqual(left.y(), right.y())
        self.assertLess(left.x(), right.x())
        self.assertAlmostEqual(left.top(), PAGE.height() - 1 - left.bottom(), delta=1)

    def test_six_tiles_are_three_by_two_and_nine_three_by_three(self):
        six = self.tiles(6)
        self.assertEqual((len({r.x() for r in six}), len({r.y() for r in six})), (3, 2))
        nine = self.tiles(9)
        self.assertEqual((len({r.x() for r in nine}), len({r.y() for r in nine})), (3, 3))

    def test_tiles_come_row_by_row(self):
        rects = self.tiles(4)
        self.assertEqual([r.y() for r in rects][:2], [rects[0].y()] * 2)
        self.assertLess(rects[0].x(), rects[1].x())
        self.assertLess(rects[1].y(), rects[2].y())

    def test_a_tall_page_letterboxes_the_grid(self):
        rects = self.tiles(4, QSize(800, 1000))
        self.assertGreater(rects[0].top(), 100)  # centred, 16:9 each

    def test_an_unknown_layout_raises_and_an_empty_page_gives_empty_rects(self):
        with self.assertRaises(ValueError):
            grid_rects(PAGE, 5)
        self.assertTrue(all(r.isEmpty() for r in grid_rects(QSize(0, 0), 4)))


class FrameCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = QWidget()
        self.page.resize(PAGE)
        self.surfaces = {name: Recorder(self.page) for name in ("main", "pip", "t2", "t3")}
        self.frame = GridFrame(self.page, self.surfaces)
        self.page.show()
        self.addCleanup(self.page.close)
        self.focus, self.laid = [], []
        self.frame.focusRequested.connect(self.focus.append)
        self.frame.tilesChanged.connect(lambda: self.laid.append(True))


class GridFrameTests(FrameCase):
    def test_it_lays_the_tiles_out_and_hides_those_that_show_nothing(self):
        self.frame.show_grid(["main", "pip", "t2", "t3"], "main", {"main", "pip"})
        rects = grid_rects(PAGE, 4)
        self.assertEqual(self.surfaces["main"].geometry(), rects[0])
        self.assertEqual(self.surfaces["pip"].geometry(), rects[1])
        self.assertTrue(self.surfaces["main"].isVisible())
        self.assertTrue(self.surfaces["t2"].isHidden())
        self.assertEqual(self.frame.rect_of("t3"), rects[3])
        self.assertTrue(self.laid)

    def test_a_press_on_a_tile_without_the_focus_asks_for_it_and_goes_no_further(self):
        self.frame.show_grid(["main", "pip"], "main", {"main", "pip"})
        pip = self.surfaces["pip"]
        press(pip, QPoint(5, 5))
        self.assertEqual((self.focus, pip.presses), (["pip"], 0))

    def test_the_focused_tiles_press_goes_on_to_the_shell(self):
        self.frame.show_grid(["main", "pip"], "main", {"main", "pip"})
        press(self.surfaces["main"], QPoint(5, 5))
        self.assertEqual((self.focus, self.surfaces["main"].presses), ([], 1))

    def test_a_resized_page_lays_the_tiles_out_again(self):
        self.frame.show_grid(["main", "pip"], "main", {"main", "pip"})
        self.page.resize(960, 540)
        QApplication.processEvents()
        self.assertEqual(self.surfaces["pip"].geometry(), grid_rects(QSize(960, 540), 2)[1])

    def test_hidden_it_lays_out_nothing_and_takes_no_press(self):
        self.frame.show_grid(["main", "pip"], "main", {"main", "pip"})
        self.frame.hide_grid()
        press(self.surfaces["pip"], QPoint(5, 5))
        self.assertEqual(self.focus, [])
        self.assertEqual(self.frame.tiles(), [])

    def test_a_tile_added_later_is_laid_out_and_unknown_ones_raise(self):
        later = Recorder(self.page)
        self.frame.add_surface("t4", later)
        self.frame.show_grid(["main", "pip", "t2", "t3", "t4", "pip"][:4], "main", set())
        with self.assertRaises(ValueError):
            self.frame.set_tiles(["main", "nine"], "main", set())
        with self.assertRaises(ValueError):
            self.frame.add_surface("t4", later)


class GridTilesTests(FrameCase):
    def setUp(self):
        super().setUp()
        self.view = GridTiles(self.page)
        self.view.show()
        self.events = []
        self.view.tilePressed.connect(lambda n: self.events.append(("pressed", n)))
        self.view.tileClicked.connect(lambda n: self.events.append(("clicked", n)))
        self.view.leaveRequested.connect(lambda n: self.events.append(("leave", n)))
        rects = grid_rects(PAGE, 4)
        self.rects = rects
        self.view.set_tiles(
            [
                TileView("main", rects[0], PLAYING, "Channel One", LIVE, focused=True),
                TileView("pip", rects[1], DENIED, "Channel Two", NO_STREAM.format(provider="P")),
                TileView("t2", rects[2], EMPTY),
                TileView("t3", rects[3], OPENING, "Channel Three", "Connecting…"),
            ]
        )

    def test_it_takes_its_cards_labels_and_the_focus_ring_only(self):
        mask = self.view.mask_region()
        self.assertEqual(self.view.hits.mask_region(), mask)  # no controls over it
        self.assertTrue(mask.contains(self.rects[1].center()))  # the card
        self.assertTrue(mask.contains(self.rects[2].center()))  # the "+"
        self.assertTrue(mask.contains(self.view.pill_rect("t3").center().toPoint()))
        self.assertFalse(mask.contains(self.rects[0].center()))  # the focused picture
        self.assertFalse(mask.contains(self.rects[3].center()))  # a picture
        self.assertTrue(mask.contains(self.rects[0].topLeft() + QPoint(1, 300)))  # its ring
        self.assertEqual(self.view.hole_claims(), mask)

    def test_a_press_asks_for_the_focus_at_once_and_its_click_follows(self):
        QTest.mouseClick(self.view.hits, LEFT, pos=self.rects[2].center())
        self.assertEqual(self.events, [("pressed", "t2")])
        self.assertTrue(wait_until(lambda: ("clicked", "t2") in self.events))

    def test_a_double_click_asks_to_leave_and_drops_the_click(self):
        QTest.mouseDClick(self.view.hits, LEFT, pos=self.rects[1].center())
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        self.assertIn(("leave", "pip"), self.events)
        self.assertNotIn(("clicked", "pip"), self.events)

    def test_the_controls_buttons_stay_the_controls_drawn_over_the_cards(self):
        controls = Avoided(self.page, QRect(self.rects[2].center(), QSize(40, 40)))
        self.view.avoid(controls)
        controls.show()
        centre = self.rects[2].center()
        self.assertTrue(self.view.mask_region().contains(centre))  # the card is drawn under
        self.assertFalse(self.view.hits.mask_region().contains(centre))  # the button's click
        controls.hide()
        self.assertTrue(self.view.hits.mask_region().contains(centre))

    def test_a_label_the_controls_title_would_cover_hides_while_they_show(self):
        pill = self.view.pill_rect("t3").toAlignedRect()
        controls = Avoided(self.page, pill.adjusted(10, 2, 0, 0))
        self.view.avoid(controls)
        controls.show()
        self.assertTrue(self.view.pill_rect("t3").isEmpty())
        self.assertFalse(self.view.mask_region().contains(pill.topLeft() + QPoint(2, 2)))
        controls.hide()
        self.assertFalse(self.view.pill_rect("t3").isEmpty())


class Avoided(QWidget):
    """The player controls, as far as the tiles overlay sees them."""

    def __init__(self, parent, claim):
        super().__init__(parent)
        self.claim = claim
        self.hide()

    def hole_claims(self):
        return QRegion(self.claim)


class TileSlotTests(unittest.TestCase):
    """The session manager's tiles: direct opens within the limit, closes, reports."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.budget = ConnectionBudget()
        self.config = type("Config", (), {"live_latency": "low", "selected_provider_name": "P"})()
        slots = [self.slot(name) for name in ("main", "pip", "t2")]
        self.sessions = SessionManager(slots[0], slots[1], self.budget)
        self.sessions.add_slot(slots[2])
        self.tile = slots[2]
        wait_for_each_other(*(slot.tuner for slot in slots))
        self.reports = []
        self.sessions.slotDenied.connect(lambda n, limit: self.reports.append(("denied", n, limit)))
        self.sessions.slotFailed.connect(lambda n, m: self.reports.append(("failed", n, m)))
        self.sessions.slotState.connect(lambda n, on: self.reports.append(("state", n, on)))

    def slot(self, name):
        session = FakeSession()
        live = FakeLiveSession(session)
        tuner = LiveTuner(session, self.config, live, self.budget, purpose=name)
        return Slot(name, session, live, tuner=tuner)

    def open(self, limit, played):
        self.sessions.open_slot(
            "t2", lambda: played.append("t2"), channel=CHANNELS[2], provider="P", limit=limit
        )

    def test_a_tile_plays_within_the_limit_holding_a_stream_under_its_name(self):
        played = []
        self.budget.request("P", 2, purpose="main")
        self.open(2, played)
        self.assertEqual(played, ["t2"])
        self.assertEqual(sorted(self.budget.holders("P")), ["main", "t2"])
        self.assertEqual(self.reports, [("state", "t2", True)])
        self.assertIs(self.tile.channel, CHANNELS[2])

    def test_a_full_provider_denies_the_tile_queues_nothing_and_plays_nothing(self):
        played = []
        self.budget.request("P", 1, purpose="main")
        self.open(1, played)
        self.assertEqual(played, [])
        self.assertEqual(self.reports[-1], ("denied", "t2", 1))
        self.assertIsNone(self.tile.channel)
        self.assertEqual(self.budget.holders("P"), ["main"])
        self.assertEqual(self.budget._waiting, {})

    def test_close_slot_stops_the_tile_and_gives_its_stream_back(self):
        played = []
        self.open(2, played)
        self.sessions.close_slot("t2")
        self.assertEqual(self.tile.playback.stops, 1)
        self.assertTrue(self.tile.playback.muted)  # silent while it stops
        self.assertEqual(self.budget.holders("P"), [])
        self.assertEqual(self.reports[-1], ("state", "t2", False))

    def test_a_tiles_player_error_is_reported_by_its_name_once_it_opened(self):
        self.tile.playback.errorOccurred.emit("before")  # nothing opened: not the tile's
        self.open(2, [])
        self.tile.playback.errorOccurred.emit("boom")
        self.assertEqual(self.reports[-1], ("failed", "t2", "boom"))

    def test_leaving_the_grid_keeps_a_tile_as_the_pip(self):
        states = []
        self.sessions.pipStateChanged.connect(states.append)
        self.sessions.enter_grid()
        self.assertTrue(self.sessions.grid())
        self.sessions.set_large("t2")
        self.sessions.leave_grid("main")
        self.assertEqual(states, [True])
        self.assertIs(self.sessions.small(), self.sessions.slot("main"))
        self.assertIs(self.sessions.large(), self.tile)

    def test_leaving_without_a_tile_closes_the_pip(self):
        states = []
        self.sessions.pipStateChanged.connect(states.append)
        self.sessions.enter_grid()
        self.sessions.leave_grid(None)
        self.assertEqual((states, self.sessions.pip_open()), ([False], False))

    def test_raising_a_tiles_level_in_the_grid_makes_it_heard(self):
        self.sessions.enter_grid()
        self.assertTrue(self.sessions.muted("t2"))
        self.sessions.set_level("t2", 60)
        self.assertFalse(self.sessions.muted("t2"))


class MultiviewCase(PipCase):
    """PipCase's two players and fakes, with the grid: tiles made as the window makes them."""

    hardware = False

    def make_controller(self, column, **extra):
        self.config.hardware_decoding = self.hardware
        if "sessions" in extra and "grid_frame" not in extra:
            self.page = QWidget()
            self.page.resize(PAGE)
            self.surfaces = {"main": QWidget(self.page), "pip": QWidget(self.page)}
            self.grid_frame = GridFrame(self.page, self.surfaces)
            extra.update(grid_frame=self.grid_frame, make_slot=self.make_slot)
        return super().make_controller(column, **extra)

    def make_slot(self, name):
        session = FakeSession()
        live = FakeLiveSession(session)
        tuner = LiveTuner(session, self.config, live, self.budget, purpose=name)
        slot = Slot(name, session, live, tuner=tuner)
        self.sessions.add_slot(slot)
        tuners = [s.tuner for s in self.sessions.slots()]
        one_disk_ring(*tuners)
        wait_for_each_other(*tuners)
        surface = QWidget(self.page)
        self.surfaces[name] = surface
        self.grid_frame.add_surface(name, surface)
        self.controller.slot_added(slot)
        self.addCleanup(live.released.emit)
        return slot

    @property
    def grid(self):
        return self.controller._grid

    def v(self):
        self.controller.handle(Action.MULTIVIEW)

    def two_pictures(self):
        """One large, Two small, both playing; then the grid."""
        self.start_large(0)
        self.session.stateChanged.emit("playing")
        self.open_small(CHANNELS[1])
        self.pip_session.stateChanged.emit("playing")
        self.v()

    def pick_in(self, name, channel, url):
        """A click on tile `name` ("+": the channel list opens), then `channel` picked there."""
        self.grid_frame.tileClicked.emit(name)
        self.controller.tune(channel, group_key="10", channels=CHANNELS)
        self.ready(url)
        return self.sessions.slot(name)


class MultiviewTests(MultiviewCase):
    def test_v_makes_the_big_picture_tile_1_and_the_pip_tile_2_reopening_nothing(self):
        self.start_large(0)
        self.session.stateChanged.emit("playing")
        self.open_small(CHANNELS[1])
        before = self.counts()
        self.v()
        grid = self.grid
        self.assertTrue(grid.shown())
        self.assertEqual(grid.names(), ["main", "pip", "t2", "t3"])
        self.assertEqual(self.counts(), before)  # nothing stopped, nothing opened again
        self.assertFalse(self.sessions.pip_open())
        self.assertIn(("suspend",), self.frame.calls)
        self.assertEqual(grid.state("main"), PLAYING)
        self.assertEqual(grid.state("pip"), OPENING)  # its player said nothing more yet
        self.assertEqual(grid.state("t2"), EMPTY)
        self.assertTrue(self.surfaces["main"].isVisibleTo(self.page))
        self.assertFalse(self.surfaces["t2"].isVisibleTo(self.page))
        self.assertEqual(self.transport.named("set_grid_state")[-1], (True, True, "2×2"))
        self.assertIs(self.sessions.large(), self.main_slot)

    def test_a_press_on_a_tile_gives_it_the_focus_and_the_sound(self):
        self.two_pictures()
        self.grid_frame.focusRequested.emit("pip")
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual((self.session.muted, self.pip_session.muted), (True, False))
        self.assertEqual(self.banner.info.name, "Two")  # the controls act on it now
        self.assertIs(self.controller.session, self.pip_session)
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual((self.session.muted, self.pip_session.muted), (False, True))

    def test_leaving_makes_the_focused_tile_big_and_the_one_before_it_small(self):
        self.two_pictures()
        self.grid_frame.focusRequested.emit("pip")
        before = self.counts()
        self.v()
        self.assertFalse(self.grid.shown())
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertIs(self.sessions.small(), self.main_slot)
        self.assertEqual(self.counts(), before)  # both play on
        self.assertIn(("set_roles", "pip", "main"), self.frame.calls)
        self.assertEqual(self.frame.calls[-1], ("show_pip",))
        self.assertTrue(self.label.visible)
        self.assertEqual(self.label.info[0], "One")
        self.assertEqual(self.transport.named("set_grid_state")[-1], (True, False, ""))

    def test_leaving_stops_the_other_tiles(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.assertEqual(t2.playback.plays[-1][0], "http://stream/3")
        t3 = self.pick_in("t3", CHANNELS[3], "http://stream/4")
        self.grid_frame.focusRequested.emit("main")
        self.v()
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertIs(self.sessions.small(), t3)  # focused just before
        self.assertEqual(t2.playback.stops, 1)
        self.assertEqual(t3.playback.stops, 0)
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t3"])

    def test_a_plus_tile_takes_the_focus_opens_the_channel_list_and_plays_the_pick(self):
        self.start_large(0)
        self.v()
        self.grid_frame.tileClicked.emit("t2")
        self.assertEqual(self.sessions.large().name, "t2")
        self.assertTrue(self.drawer.visible)
        live_calls = len(self.live.calls)
        self.controller.tune(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("http://stream/3")
        t2 = self.sessions.slot("t2")
        url, kwargs = t2.playback.plays[-1]
        self.assertEqual(url, "http://stream/3")  # directly: no time-shift in a tile yet
        self.assertEqual(t2.live.calls, [])
        self.assertTrue(ZapContext.same(t2.channel, CHANNELS[2]))
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t2"])
        self.assertEqual(len(self.live.calls), live_calls)  # tile 1 plays on
        self.assertEqual(self.grid.state("t2"), OPENING)
        t2.playback.stateChanged.emit("playing")
        self.assertEqual(self.grid.state("t2"), PLAYING)
        self.assertEqual(self.watch.plays[-1][1]["name"], "Three")

    def test_zapping_onto_a_channel_another_tile_shows_moves_the_focus_there(self):
        self.two_pictures()
        links = len(self.catalog.links)
        self.controller.tune(CHANNELS[1], group_key="10", channels=CHANNELS)
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertEqual(len(self.catalog.links), links)  # never a second stream of it
        self.assertEqual(self.pip_session.stops, 0)

    def test_esc_with_nothing_open_and_a_double_click_leave_the_grid(self):
        self.start_large(0)
        self.banner.visible = False
        self.v()
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.grid.shown())
        self.assertNotIn("leave_fullscreen", self.window.calls)
        self.v()
        self.controller.video_double_clicked()
        self.assertFalse(self.grid.shown())
        self.assertNotIn("toggle_fullscreen", self.window.calls)

    def test_the_library_films_files_and_external_players_leave_the_grid_first(self):
        self.start_large(0)
        for leave in (
            self.controller.enter_library_playing,
            self.controller.stop_for_library,
            lambda: self.controller.play_vod(MOVIE, "movie"),
            lambda: self.controller.play_file("/tmp/film.mkv"),
        ):
            with self.subTest(leave=leave):
                self.v()
                self.assertTrue(self.grid.shown())
                leave()
                self.assertFalse(self.grid.shown())
                self.controller.leave_library_playing()

    def test_p_and_s_in_the_grid_say_to_leave_it_first(self):
        self.start_large(0)
        self.v()
        self.controller.handle(Action.PIP)
        self.controller.handle(Action.SWAP)
        self.assertEqual(self.toast.messages()[-2:], [(LEAVE_FIRST, "info")] * 2)

    def test_a_film_playing_keeps_the_grid_away(self):
        self.controller.play_vod(MOVIE, "movie")
        self.ready("http://vod/heat.mkv")
        self.v()
        self.assertFalse(self.grid.shown())
        self.assertEqual(self.toast.messages()[-1], (LIVE_ONLY, "info"))

    def test_without_hardware_decoding_the_chip_offers_two_and_four_tiles(self):
        self.start_large(0)
        self.v()
        self.assertEqual(self.grid.choices(), (2, 4))
        self.transport.layoutRequested.emit()
        self.assertEqual(self.grid.names(), ["main", "pip"])
        self.assertEqual(self.transport.named("set_grid_state")[-1], (True, True, "1×2"))
        self.transport.layoutRequested.emit()
        self.assertEqual(len(self.grid.names()), 4)

    def test_a_tile_whose_stream_fails_stops_and_says_so_until_clicked(self):
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        t2.playback.errorOccurred.emit("the stream broke")
        self.assertEqual(self.grid.state("t2"), FAILED)
        self.assertEqual(t2.playback.stops, 1)
        self.assertEqual(self.budget.holders("Home"), ["main"])
        links = len(self.catalog.links)
        self.grid_frame.tileClicked.emit("t2")  # focus and try again
        self.assertEqual(self.sessions.large().name, "t2")
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_the_guides_of_every_tiles_channel_are_in_use(self):
        self.start_large(0)
        self.v()
        self.pick_in("t2", HARBOR_NINE, "http://harbor/9")
        self.assertIn("Harbor IPTV", self.controller.guides_in_use())


class OneStreamMultiviewTests(MultiviewCase):
    """A provider allowing one stream: the tile that would be its second shows the card."""

    def setUp(self):
        super().setUp()
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB"}  # one stream

    def deny_t2(self):
        self.start_large(0)
        self.v()
        self.grid_frame.tileClicked.emit("t2")
        links = len(self.catalog.links)
        self.controller.tune(CHANNELS[1], group_key="10", channels=CHANNELS)
        self.assertEqual(len(self.catalog.links), links)  # no link asked for
        return links

    def test_the_tile_shows_no_stream_to_spare_and_keeps_its_channel(self):
        self.deny_t2()
        self.assertEqual(self.grid.state("t2"), DENIED)
        card = self.grid._cards["t2"]
        self.assertEqual(card.detail, NO_STREAM.format(provider="Home"))
        self.assertIs(card.channel, CHANNELS[1])
        self.assertTrue(self.live.running)  # tile 1 plays on
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_another_providers_channel_plays_in_that_tile(self):
        self.deny_t2()
        t2 = self.sessions.slot("t2")
        self.controller.tune(HARBOR_NINE, group_key="10", channels=[HARBOR_NINE])
        self.ready("http://harbor/9")
        self.assertEqual(t2.playback.plays[-1][0], "http://harbor/9")
        self.assertEqual(self.budget.holders("Harbor IPTV"), ["t2"])
        self.assertEqual(self.grid.state("t2"), OPENING)

    def test_a_click_on_the_card_tries_again_and_never_by_itself(self):
        links = self.deny_t2()
        QTest.qWait(50)
        self.assertEqual(len(self.catalog.links), links)  # nothing asked again by itself
        self.grid_frame.tileClicked.emit("t2")
        self.assertEqual(len(self.catalog.links), links)  # still full: said, not asked
        self.assertIn((NO_STREAM.format(provider="Home"), "error"), self.toast.messages())
        # Tile 1 changes to another provider's channel: Home's stream is free for tile 2.
        self.grid_frame.focusRequested.emit("main")
        self.controller.tune(HARBOR_NINE, group_key="10", channels=[HARBOR_NINE])
        self.ready("http://harbor/9")
        self.live.released.emit()  # tile 1's time-shifted stream let go
        self.assertTrue(wait_until(lambda: self.session.plays[-1][0] == "http://harbor/9"))
        self.assertEqual(self.budget.holders("Home"), [])
        self.grid_frame.tileClicked.emit("t2")
        self.assertEqual(len(self.catalog.links), links + 2)
        self.ready("http://stream/2")
        self.assertEqual(self.sessions.slot("t2").playback.plays[-1][0], "http://stream/2")
        self.assertEqual(self.budget.holders("Home"), ["t2"])


class HardwareMultiviewTests(MultiviewCase):
    hardware = True

    def test_six_and_nine_tiles_with_hardware_decoding(self):
        self.start_large(0)
        self.v()
        self.assertEqual(self.grid.choices(), (2, 4, 6, 9))
        self.grid.set_cells(9)
        self.assertEqual(
            self.grid.names(), ["main", "pip", "t2", "t3", "t4", "t5", "t6", "t7", "t8"]
        )
        self.assertEqual(self.transport.named("set_grid_state")[-1], (True, True, LAYOUT_NAMES[9]))

    def test_a_smaller_layout_keeps_the_focused_tile_and_stops_the_rest(self):
        self.start_large(0)
        self.v()
        self.grid.set_cells(9)
        t5 = self.pick_in("t5", CHANNELS[2], "http://stream/3")
        self.grid.set_cells(2)
        self.assertEqual(self.grid.names(), ["main", "t5"])
        self.assertEqual(t5.playback.stops, 0)
        self.assertIs(self.sessions.large(), t5)


class MultiviewMixerTests(MultiviewCase):
    def setUp(self):
        self.mixer = FakeMixer()
        super().setUp()

    def test_the_mixer_lists_the_focused_tile_first_then_the_others_that_play(self):
        self.two_pictures()
        self.grid_frame.focusRequested.emit("pip")
        self.controller.handle(Action.VOL_UP)
        rows, selected = self.mixer.shown()
        self.assertEqual([(r[0], r[1]) for r in rows], [("Tile 2", "Two"), ("Tile 1", "One")])
        self.assertEqual(selected, 0)
        self.assertEqual(rows[0][3], False)  # the focused tile is heard
        self.assertEqual(rows[1][3], True)


class MultiviewWindowTests(WindowCase):
    """The window's grid with libVLC's dummy outputs: no stream plays."""

    vlc_instance = INSTANCE

    def test_v_lays_the_players_out_as_tiles_and_a_plus_opens_the_channel_list(self):
        self.show()
        window = self.window
        drawer = self.wait_for_drawer()  # no last channel: the list opens at the start
        QTest.keyClick(window, Qt.Key.Key_Escape)
        self.assertTrue(wait_until(lambda: not drawer.isVisible()))
        QTest.keyClick(window, Qt.Key.Key_V)
        grid = window.controller._grid
        self.assertTrue(grid.shown())
        self.assertEqual(grid.names(), ["main", "pip", "t2", "t3"])
        self.assertEqual(set(window.surfaces), {"main", "pip", "t2", "t3"})
        self.assertEqual([s.name for s in window.sessions.slots()], ["main", "pip", "t2", "t3"])
        tiles = window.grid_tiles.tiles()
        self.assertEqual([t.state for t in tiles], [EMPTY] * 4)
        self.assertTrue(tiles[0].focused)
        self.assertTrue(window.grid_tiles.isVisible())
        plus = tiles[2].rect.center()
        QTest.mouseClick(window.grid_tiles.hits, LEFT, pos=plus)
        self.assertEqual(window.sessions.large().name, "t2")
        self.assertTrue(wait_until(drawer.isVisible))
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the list closes
        self.assertTrue(wait_until(lambda: not drawer.isVisible()))
        QTest.keyClick(window, Qt.Key.Key_V)
        self.assertFalse(grid.shown())
        page = window.player_page.rect()
        self.assertEqual(window.surfaces["t2"].geometry(), page)  # the focused tile is big
        self.assertTrue(window.surfaces["t2"].isVisible())
        self.assertFalse(window.grid_tiles.isVisible())
        for name in ("main", "pip", "t3"):
            self.assertFalse(window.surfaces[name].isVisible(), name)


if __name__ == "__main__":
    unittest.main()
