"""The multiview: the grid's rects, its frame, its tiles overlay, the session
manager's tiles, and the shell's grid over fake players (test_tv_pip's PipCase: real tuners,
manager and budget), then once in the window with libVLC's dummy outputs."""

import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    qInstallMessageHandler,
)
from PySide6.QtGui import QMouseEvent, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import wait_until

from services.connection_budget import ConnectionBudget
from services.live.refusals import STREAM_LIMIT
from services.navigation import NavNode
from services.sessions import SessionManager, Slot
from services.zapping import ZapContext
from tests.test_tv_controller import CHANNELS, FakeLiveSession, FakeSession
from tests.test_tv_pip import MOVIE, SHIFTED, FakeMixer, PipCase
from tests.test_tv_window import INSTANCE, WindowCase
from ui.grid_controls import LAYOUT_NAMES, LEAVE_FIRST, LIVE_ONLY, NEEDS_HARDWARE, NO_STREAM
from ui.grid_frame import GridFrame, grid_rects
from ui.keymap import Action
from ui.live_tuner import LiveTuner, one_disk_ring, wait_for_each_other
from ui.mixer_controls import FOLLOW_ACTION, FOLLOWS_TEXT, MIXED_TEXT
from ui.overlays.grid_tiles import (
    DENIED,
    EMPTY,
    FAILED,
    OPENING,
    PLAYING,
    GridTiles,
    TileView,
)
from ui.overlays.layout_row import LayoutChoice, LayoutRow
from ui.overlays.mixer import MixerMode
from ui.overlays.pip_label import CONNECTING, LIVE

LEFT = Qt.MouseButton.LeftButton
MIB = 1024 * 1024
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

    def test_a_tile_without_a_player_yet_is_laid_out_once_its_surface_comes(self):
        # A "+" tile has no player; it is made when the tile first takes the focus
        # or a channel, and its surface is laid out then.
        self.frame.show_grid(["main", "pip", "t2", "t3", "t4", "t5"], "main", {"t4"})
        self.assertEqual(len(self.frame.tiles()), 6)
        later = Recorder(self.page)
        self.frame.add_surface("t4", later)
        self.assertEqual(later.geometry(), grid_rects(PAGE, 6)[4])
        self.assertTrue(later.isVisible())
        with self.assertRaises(ValueError):
            self.frame.add_surface("t4", later)
        with self.assertRaises(ValueError):
            self.frame.set_tiles(["main", "pip", "t2"], "main", set())  # no 3-tile layout


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

    def test_a_dragged_channel_lights_its_tile_and_its_name_follows_the_pointer(self):
        point = self.rects[3].center()
        self.view.set_drop("t3", "Channel Nine", point)
        mask = self.view.mask_region()
        self.assertTrue(mask.contains(self.rects[3].center() + QPoint(-100, 40)))  # the tile
        self.assertTrue(mask.contains(point + QPoint(30, 25)))  # the name beside the pointer
        self.assertEqual(self.view.drop(), ("t3", "Channel Nine", point))
        self.view.set_drop(None, "", QPoint())
        self.assertIsNone(self.view.drop())
        self.assertFalse(
            self.view.mask_region().contains(self.rects[3].center() + QPoint(-100, 40))
        )

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


class TileStripTests(FrameCase):
    """A tile's own controls: the strip the tile under the pointer shows."""

    def setUp(self):
        super().setUp()
        self.view = GridTiles(self.page)
        self.view.show()
        self.events = []
        self.view.tilePressed.connect(lambda n: self.events.append(("pressed", n)))
        self.view.leaveRequested.connect(lambda n: self.events.append(("leave", n)))
        self.view.tileAction.connect(lambda n, c: self.events.append((c, n)))
        self.rects = grid_rects(PAGE, 4)
        self.show(4)

    def show(self, cells, size=PAGE):
        rects = grid_rects(size, cells)
        self.rects = rects
        tiles = [
            TileView("main", rects[0], PLAYING, "One", LIVE, True, muted=False, position=LIVE),
            TileView("pip", rects[1], PLAYING, "Two", "−01:30", position="−01:30", behind=True),
            TileView("t2", rects[2], EMPTY),
            TileView("t3", rects[3], DENIED, "Four", NO_STREAM.format(provider="P")),
        ]
        names = ("t4", "t5", "t6", "t7", "t8")
        tiles += [
            TileView(names[i], rect, PLAYING, f"Ch {i}", LIVE, position=LIVE)
            for i, rect in enumerate(rects[4:])
        ]
        self.view.set_tiles(tiles)

    def hover(self, index, dy=-12):
        rect = self.rects[index]
        self.view.hover_at(QPointF(rect.center().x(), rect.bottom() + dy))

    def test_the_tile_under_the_pointer_shows_its_controls_and_only_it(self):
        self.hover(1)
        self.assertEqual(self.view.hovered(), "pip")
        strip = self.view.strip_rect("pip")
        self.assertTrue(self.rects[1].contains(strip.toRect()))
        self.assertGreater(strip.center().y(), self.rects[1].center().y())  # at its bottom
        for control in ("play", "speaker", "live", "big", "close"):
            rect = self.view.control_rect("pip", control)
            self.assertFalse(rect.isEmpty(), control)
            self.assertTrue(self.view.mask_region().contains(rect.center().toPoint()), control)
        self.assertTrue(self.view.strip_rect("main").isEmpty())
        self.hover(0)
        self.assertEqual(self.view.hovered(), "main")
        self.assertTrue(self.view.strip_rect("pip").isEmpty())

    def test_a_plus_has_none_a_card_its_close_only_and_off_the_tiles_none_shows(self):
        self.hover(2)
        self.assertIsNone(self.view.hovered())
        self.hover(3)
        self.assertEqual(self.view.hovered(), "t3")
        self.assertFalse(self.view.control_rect("t3", "close").isEmpty())
        self.assertTrue(self.view.control_rect("t3", "play").isEmpty())
        self.view.hover_at(QPointF(-5, -5))
        self.assertIsNone(self.view.hovered())

    def test_a_press_on_a_control_acts_on_its_tile_without_focusing_it(self):
        self.hover(1)
        for control in ("play", "speaker", "live", "big", "close"):
            centre = self.view.control_rect("pip", control).center().toPoint()
            QTest.mouseClick(self.view.hits, LEFT, pos=centre)
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        self.assertEqual(
            self.events,
            [(c, "pip") for c in ("play", "speaker", "live", "big", "close")],
        )  # no press, no click: the focus stays

    def test_a_quick_second_click_on_a_control_counts_once_and_never_leaves(self):
        self.hover(1)
        centre = self.view.control_rect("pip", "play").center().toPoint()
        QTest.mouseDClick(self.view.hits, LEFT, pos=centre)
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        self.assertNotIn(("leave", "pip"), self.events)
        self.assertLessEqual(self.events.count(("play", "pip")), 1)

    def test_its_tooltips_name_what_each_does_now(self):
        self.hover(1)
        tip = lambda c: self.view.tooltip_at(self.view.control_rect("pip", c).center())  # noqa
        self.assertEqual(tip("play"), "Pause this tile")
        self.assertEqual(tip("speaker"), "Unmute this tile")
        self.assertEqual(tip("live"), "Go live")
        self.assertEqual(tip("close"), "Close this tile")
        self.hover(0)
        main = self.view.control_rect("main", "live").center()
        self.assertEqual(self.view.tooltip_at(main), "")  # at live: nothing to do

    def test_it_hides_after_the_controls_idle_time(self):
        self.view.set_idle_ms(30)
        self.hover(1)
        self.assertTrue(wait_until(lambda: self.view.hovered() is None))
        self.assertFalse(self.view.mask_region().contains(self.rects[1].center()))

    def test_nine_small_tiles_keep_the_speaker_and_close_only(self):
        small = QSize(360, 203)  # a mini player's
        self.page.resize(small)
        self.show(9, small)
        self.hover(4)
        shown = [c for c in ("play", "speaker", "live", "big", "close")
                 if not self.view.control_rect("t4", c).isEmpty()]  # fmt: skip
        self.assertEqual(shown, ["speaker", "close"])
        strip = self.view.strip_rect("t4")
        self.assertTrue(QRectF(self.rects[4]).contains(strip))  # never overflows its tile

    def test_it_sits_above_the_player_controls_buttons_when_they_cover_its_place(self):
        bottom = self.rects[3]
        claim = QRect(bottom.left(), bottom.bottom() - 60, bottom.width(), 50)
        controls = Avoided(self.page, claim)
        self.view.avoid(controls)
        controls.show()
        self.hover(1)  # a tile above them: at its bottom
        self.assertAlmostEqual(
            self.view.strip_rect("pip").bottom(), self.rects[1].bottom() - 7, delta=2
        )
        self.show(4)
        self.view.set_tiles(
            [t if t.name != "t3" else TileView("t3", t.rect, PLAYING, "Four", LIVE, position=LIVE)
             for t in self.view.tiles()]
        )  # fmt: skip
        self.hover(3, dy=-150)
        strip = self.view.strip_rect("t3")
        self.assertLess(strip.bottom(), claim.top())
        self.assertTrue(QRectF(self.rects[3]).contains(strip))

    def test_with_the_controls_up_to_its_label_it_takes_the_labels_place(self):
        # A small window, behind live: the bar and the buttons reach high over a bottom tile.
        bottom = self.rects[3]
        claim = QRect(bottom.left(), bottom.top() + 50, bottom.width(), bottom.height() - 50)
        controls = Avoided(self.page, claim)
        self.view.avoid(controls)
        controls.show()
        self.view.set_tiles(
            [t if t.name != "t3" else TileView("t3", t.rect, PLAYING, "Four", LIVE, position=LIVE)
             for t in self.view.tiles()]
        )  # fmt: skip
        self.assertFalse(self.view.pill_rect("t3").isEmpty())
        self.hover(3, dy=-10)
        strip = self.view.strip_rect("t3")
        self.assertLess(strip.top(), bottom.top() + 12)
        self.assertFalse(strip.intersects(QRectF(claim)))
        self.assertTrue(self.view.pill_rect("t3").isEmpty())  # the label waits meanwhile
        self.view.hover_at(QPointF(-5, -5))
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

    def test_a_time_shifted_tile_asks_for_its_slot_once_released_and_never_queues(self):
        started, denied = [], []
        self.sessions.slotDenied.connect(lambda n, limit: denied.append((n, limit)))
        kwargs = {"url": "http://p/2", "headers": {}, "verify_ssl": True, "disk": False}
        kwargs.update(latency="low", user_agent="QiTV", max_bytes=64 * MIB)
        self.budget.request("P", 2, purpose="main")
        self.sessions.open_slot(
            "t2", channel=CHANNELS[2], provider="P", limit=2, start_kwargs=kwargs
        )
        self.assertTrue(wait_until(lambda: self.tile.live.running))
        started.append(self.tile.live.url)
        self.assertEqual(sorted(self.budget.holders("P")), ["main", "t2"])
        self.budget.request("P", 3, purpose="pip")  # a third stream: P is full now
        self.sessions.open_slot(
            "t2", channel=CHANNELS[3], provider="P", limit=2, start_kwargs=kwargs
        )
        self.tile.live.released.emit()  # its first stream closed: then it asks
        self.assertEqual(denied, [("t2", 2)])
        self.assertIsNone(self.tile.channel)
        self.assertEqual(self.budget._waiting, {})
        self.assertEqual(started, ["http://p/2"])

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
        """A click on tile `name` ("+": the channel list opens), then `channel` picked there
        and played from its time-shift buffer."""
        self.grid_frame.tileClicked.emit(name)
        self.controller.tune(channel, group_key="10", channels=CHANNELS)
        self.ready(url)
        slot = self.sessions.slot(name)
        self.assertTrue(wait_until(lambda: slot.live.running and slot.live.url == url))
        slot.live.play_pipeline()
        return slot


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
        self.assertNotIn("t2", self.surfaces)  # a "+" tile has no player yet
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
        t3 = self.pick_in("t3", CHANNELS[3], "http://stream/4")
        self.grid_frame.focusRequested.emit("main")
        self.v()
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertIs(self.sessions.small(), t3)  # focused just before
        self.assertEqual(t2.live.calls[-1], ("stop",))
        self.assertTrue(t3.live.running)
        t2.live.released.emit()  # its upstream closed
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t3"])

    def test_a_stopped_tiles_player_keeps_no_channel_key(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        t3 = self.pick_in("t3", CHANNELS[3], "http://stream/4")
        keys = self.controller._player_keys
        self.assertCountEqual(keys.players(), [self.session, t2.playback, t3.playback])
        self.grid_frame.focusRequested.emit("main")
        self.v()  # t2 stops; t3 plays on as the small picture
        self.assertCountEqual(keys.players(), [self.session, t3.playback])

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
        self.assertTrue(wait_until(lambda: t2.live.named("start")))
        url, kwargs = t2.live.named("start")[-1]
        self.assertEqual(url, "http://stream/3")  # through its time-shift buffer
        self.assertEqual((kwargs["disk"], kwargs["max_bytes"]), (False, 256 * MIB))  # focused
        self.assertEqual(t2.playback.plays, [])  # never directly
        self.assertTrue(ZapContext.same(t2.channel, CHANNELS[2]))
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t2"])
        self.assertEqual(len(self.live.calls), live_calls)  # tile 1 plays on
        self.assertEqual(self.grid.state("t2"), OPENING)
        t2.live.play_pipeline()
        t2.playback.stateChanged.emit("playing")
        self.assertEqual(self.grid.state("t2"), PLAYING)
        self.assertEqual(self.watch.plays[-1][1]["name"], "Three")

    def test_the_focused_tile_and_the_big_picture_after_it_keep_the_main_level(self):
        # Tile 2 is the PiP's player: a remembered quiet PiP level never makes it quiet once
        # it has the focus, nor once it is the big picture.
        self.sessions.set_role_levels(main=70, pip=0)
        self.start_large(0)
        self.session.stateChanged.emit("playing")
        self.v()
        tile = self.pick_in("pip", CHANNELS[2], "http://stream/3")
        self.assertEqual((self.sessions.level("pip"), tile.playback.level), (70, 70))
        self.assertEqual(self.sessions.level("main"), 30)  # a tile of its own, muted meanwhile
        self.assertTrue(self.sessions.muted("main"))
        self.v()  # leave: tile 2 is the big picture, tile 1 the PiP
        self.assertIs(self.sessions.large(), tile)
        self.assertEqual((self.sessions.level("pip"), tile.playback.level), (70, 70))
        self.assertEqual((self.sessions.level("main"), self.session.level), (0, 0))

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
        self.catalog.ptype = "XTREAM"  # no portal: no fresh link by itself (STB tiles: below)
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        t2.live.failed.emit("The stream broke")
        self.assertEqual(self.grid.state("t2"), FAILED)
        self.assertEqual(t2.live.calls[-1], ("stop",))
        t2.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        links = len(self.catalog.links)
        self.grid_frame.tileClicked.emit("t2")  # focus and try again
        self.assertEqual(self.sessions.large().name, "t2")
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_focused_tile_left_while_it_waits_to_reconnect_says_its_stream_ended(self):
        self.enterContext(mock.patch("ui.live_controls.RECONNECT_MS", (80, 120)))
        self.catalog.ptype = "XTREAM"  # no link to make again: it reconnects
        self.start_large(0)
        self.live.play_direct()  # the big picture's live session fell back
        self.session.stateChanged.emit("playing")
        self.open_small(CHANNELS[1])
        self.pip_session.stateChanged.emit("playing")
        self.v()
        plays = len(self.session.plays)
        self.session.ended.emit()
        self.assertEqual(self.toast.messages()[-1], ("Reconnecting…", "info"))
        self.grid_frame.focusRequested.emit("pip")  # the focus moves on during its back-off
        self.assertEqual(self.grid.state("main"), FAILED)
        self.assertEqual(self.grid.label("main"), ("One", "The stream ended"))
        self.assertEqual(self.live.calls[-1], ("stop",))  # its stream stops
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), ["pip"])
        QTest.qWait(150)
        self.assertEqual(len(self.session.plays), plays)  # never opened again behind its card
        self.grid_frame.focusRequested.emit("main")  # the focus back: nothing reopens either
        QTest.qWait(150)
        self.assertEqual(len(self.session.plays), plays)

    def test_a_channel_that_cannot_be_time_shifted_plays_directly_and_says_when_it_fails(self):
        self.catalog.ptype = "XTREAM"
        self.start_large(0)
        self.v()
        self.grid_frame.tileClicked.emit("t2")
        self.controller.tune(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("rtsp://stream/3")  # not a stream the time-shift buffer reads
        t2 = self.sessions.slot("t2")
        self.assertEqual(t2.playback.plays[-1][0], "rtsp://stream/3")
        self.assertEqual(t2.live.calls, [])
        self.grid_frame.focusRequested.emit("main")
        t2.playback.errorOccurred.emit("the stream broke")
        self.assertEqual(self.grid.state("t2"), FAILED)
        self.assertEqual(t2.playback.stops, 1)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_the_focused_tiles_buffer_is_the_full_one_and_the_others_small(self):
        # A tile out of focus keeps 64 MiB; the focus grows it to the memory per
        # stream (and shrinks the one that had it); leaving gives the PiP its full buffer.
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.two_pictures()
        self.assertEqual(self.pip_live.resizes, [64 * MIB])
        self.assertEqual(self.live.resizes, [])
        self.grid_frame.focusRequested.emit("pip")
        self.assertEqual(self.pip_live.resizes[-1], 256 * MIB)
        self.assertEqual(self.live.resizes[-1], 64 * MIB)
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.assertEqual(t2.live.named("start")[-1][1]["max_bytes"], 256 * MIB)
        self.assertEqual(self.pip_live.resizes[-1], 64 * MIB)
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual(t2.live.resizes[-1], 64 * MIB)
        self.v()  # tile 2 (t2, focused before) stays small: a PiP's full buffer again
        self.assertEqual(t2.live.resizes[-1], 256 * MIB)
        self.assertEqual(self.live.resizes[-1], 256 * MIB)

    def test_the_tiles_buffer_follows_the_optional_setting_never_under_48_mib(self):
        self.config.multiview_tile_ram_mib = 96
        self.assertEqual(self.grid.ring_sizes(), (256 * MIB, 96 * MIB))
        self.config.multiview_tile_ram_mib = 16  # a 4K keyframe interval needs 48
        self.assertEqual(self.grid.ring_sizes()[1], 48 * MIB)
        self.config.timeshift_ram_mib = 128
        self.config.multiview_tile_ram_mib = 512
        self.assertEqual(self.grid.ring_sizes(), (128 * MIB, 128 * MIB))

    # ---- stay-live per tile, labels, an STB tile's fresh link -------------------------------

    def test_tiles_without_the_focus_play_at_1x_and_only_jump_to_live(self):
        self.two_pictures()
        self.assertEqual((self.live.background, self.pip_live.background), (False, True))
        self.grid_frame.focusRequested.emit("pip")
        self.assertEqual((self.live.background, self.pip_live.background), (True, False))
        self.v()  # the PiP's stay-live steers as ever
        self.assertEqual((self.live.background, self.pip_live.background), (False, False))

    def test_a_tiles_label_says_live_or_how_far_behind_live_it_plays(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.assertEqual(self.grid.label("t2"), ("Three", CONNECTING))
        t2.live.timelineChanged.emit({"lag": 2.6, "target": 2.5})
        self.assertEqual(self.grid.label("t2"), ("Three", LIVE))
        t2.live.timelineChanged.emit({"lag": 42.0, "target": 2.5})
        self.assertEqual(self.grid.label("t2"), ("Three", "−00:42"))

    def test_a_channel_opened_beside_the_focus_keeps_the_focus_and_a_small_buffer(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        links = len(self.catalog.links)
        self.controller.play_in_tile("t2", CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(len(self.catalog.links), links + 1)  # on the side lane
        self.assertEqual(self.grid.label("t2"), ("Three", CONNECTING))
        self.ready("http://stream/3")
        t2 = self.sessions.slot("t2")
        self.assertTrue(wait_until(lambda: t2.live.running))
        self.assertEqual(t2.live.named("start")[-1][1]["max_bytes"], 64 * MIB)
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertTrue(t2.playback.muted)
        self.grid_frame.focusRequested.emit("t2")  # the controls act on what it plays
        self.assertEqual(self.banner.info.name, "Three")
        self.assertEqual(self.controller._current.title, "Three")
        self.assertEqual(self.zap.context.group_key, "10")

    def test_an_stb_tile_without_the_focus_gets_a_fresh_link_once(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        links = len(self.catalog.links)
        t2.live.failed.emit("The stream broke")
        t2.live.released.emit()
        self.assertEqual(len(self.catalog.links), links + 1)  # its link made again
        self.assertEqual(self.catalog.links[-1][1], CHANNELS[2])
        self.ready("http://stream/3-again")
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url.endswith("again")))
        self.assertEqual(self.grid.state("t2"), OPENING)
        t2.live.failed.emit("The stream broke")  # again: it says so, and asks nothing more
        self.assertEqual(self.grid.state("t2"), FAILED)
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_refusal_is_never_asked_again(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        links = len(self.catalog.links)
        t2.live.failed.emit(STREAM_LIMIT)
        self.assertEqual((self.grid.state("t2"), len(self.catalog.links)), (FAILED, links))

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
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url == "http://harbor/9"))
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
        self.assertTrue(wait_until(lambda: self.live.running and self.live.url.endswith("/9")))
        self.assertEqual(self.budget.holders("Home"), [])
        self.grid_frame.tileClicked.emit("t2")
        self.assertEqual(len(self.catalog.links), links + 2)
        self.ready("http://stream/2")
        t2 = self.sessions.slot("t2")
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url == "http://stream/2"))
        self.assertEqual(self.budget.holders("Home"), ["t2"])


class LastGridTests(MultiviewCase):
    """The grid left is kept (config `multiview`) and comes back on a click."""

    def setUp(self):
        super().setUp()
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.catalog.names.append("Harbor IPTV")
        self.catalog.catalogs.ready = {"Home"}

    def test_leaving_keeps_the_layout_the_focus_and_each_tiles_channel(self):
        self.start_large(0)
        self.v()
        self.pick_in("t2", CHANNELS[2], "http://stream/3")  # focused now
        self.v()
        saved = self.config.multiview
        self.assertEqual((saved["version"], saved["cells"], saved["focus"]), (1, 4, 2))
        tiles = saved["tiles"]
        self.assertEqual(tiles[0]["item"]["name"], "One")
        self.assertEqual(tiles[0]["provider_name"], "Home")
        self.assertIsNone(tiles[1])  # the PiP's player played nothing
        self.assertEqual((tiles[2]["item"]["name"], tiles[2]["group_id"]), ("Three", "10"))
        self.assertIsNone(tiles[3])
        self.assertGreater(self.config.saves, 0)

    def test_the_last_grid_comes_back_each_tile_once_its_provider_is_ready(self):
        self.config.multiview = {
            "version": 1,
            "cells": 4,
            "focus": 0,
            "tiles": [
                {"provider_name": "Home", "group_id": "10", "item": CHANNELS[0]},  # plays
                {"provider_name": "Home", "group_id": "10", "item": CHANNELS[2]},
                {"provider_name": "Harbor IPTV", "group_id": "", "item": HARBOR_NINE},
                {"provider_name": "Gone provider", "group_id": "", "item": CHANNELS[3]},
            ],
        }
        self.start_large(0)
        self.v()
        self.assertTrue(self.transport.restorable)  # the "Last grid" chip
        links = len(self.catalog.links)
        self.transport.restoreRequested.emit()
        self.assertFalse(self.transport.restorable)
        self.assertEqual(len(self.catalog.links), links + 1)  # Three, beside tile 1
        self.assertEqual(self.catalog.links[-1][1], CHANNELS[2])
        self.assertIn("Harbor IPTV", self.catalog.catalogs.ensured)
        self.assertEqual(self.grid.label("t2"), ("Nine", CONNECTING))  # waits for Harbor
        self.assertEqual(self.grid.state("t3"), EMPTY)  # a provider gone: nothing
        self.ready("http://stream/3")
        pip = self.sessions.slot("pip")
        self.assertTrue(wait_until(lambda: pip.live.running and pip.live.url.endswith("/3")))
        self.catalog.catalogs.ready.add("Harbor IPTV")
        self.catalog.hub_ready("Harbor IPTV")
        self.assertEqual(self.catalog.links[-1][1]["name"], "Nine")
        self.ready("http://harbor/9")
        t2 = self.sessions.slot("t2")
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url.endswith("/9")))
        self.assertIs(self.sessions.large(), self.main_slot)  # the saved focus: tile 1

    def test_a_grid_of_another_version_is_never_read(self):
        self.config.multiview = {"version": 2, "cells": 4, "tiles": [{"anything": 1}]}
        self.start_large(0)
        self.v()
        self.assertIsNone(self.grid.saved())
        self.assertFalse(self.transport.restorable)

    def test_a_channel_dropped_on_a_tile_plays_there_and_the_focus_stays(self):
        self.start_large(0)
        self.v()
        self.drawer.visible = True
        node = NavNode("item", "3", "Three", CHANNELS[2])
        point = self.page.mapToGlobal(self.grid_frame.rect_of("t3").center())
        self.assertEqual(self.grid.tile_at(point), "t3")
        links = len(self.catalog.links)
        self.controller._on_channel_dropped(node, point)
        self.assertFalse(self.drawer.visible)
        self.assertEqual(len(self.catalog.links), links + 1)
        self.ready("http://stream/3")
        t3 = self.sessions.slot("t3")
        self.assertTrue(wait_until(lambda: t3.live.running))
        self.assertIs(self.sessions.large(), self.main_slot)
        outside = self.page.mapToGlobal(QPoint(-50, -50))
        self.controller._on_channel_dropped(node, outside)  # on no tile: nothing
        self.assertEqual(len(self.catalog.links), links + 1)

    def test_a_tile_taking_the_focus_is_recorded_as_watched(self):
        self.two_pictures()
        self.grid_frame.focusRequested.emit("pip")
        self.assertEqual(self.watch.plays[-1][1]["name"], "Two")

    def test_a_reconnecting_tile_without_a_stream_to_spare_shows_the_card(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.catalog.ptype = "XTREAM"
        self.start_large(0)
        self.v()
        self.grid_frame.tileClicked.emit("t2")
        self.controller.tune(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("rtsp://stream/3")  # played directly: its ended stream reconnects
        t2 = self.sessions.slot("t2")
        self.assertEqual(t2.playback.plays[-1][0], "rtsp://stream/3")
        t2.tuner._held.give_back()  # its slot went (as the provider may count it gone) ...
        self.budget.request("Home", 2, purpose="pip")  # ... and another stream took it
        controls = self.controller._live_controls
        self.assertTrue(controls.reconnect(self.controller._current))
        plays = len(t2.playback.plays)
        controls._reopen()
        self.assertEqual(len(t2.playback.plays), plays)  # nothing opens over the limit
        self.assertEqual(self.grid.state("t2"), DENIED)


class DrawerDragTests(unittest.TestCase):
    """A channel row dragged out of the channel list (the multiview's grid shows)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from types import SimpleNamespace

        from services.navigation import NavModel
        from tests.test_navigation import FakeCatalog as NavCatalog
        from ui.overlays.drawer import Drawer

        self.host = QWidget()
        self.host.resize(PAGE)
        self.host.show()
        self.addCleanup(self.host.close)
        nav = NavModel(NavCatalog(), SimpleNamespace(favorites=[], watch_history=[]))
        self.drawer = Drawer(nav, None, lambda item: None, self.host)
        nav.reset_to("live", "10")
        self.drawer.open_drawer()
        self.events = []
        self.drawer.channelDragged.connect(lambda node, at: self.events.append(("drag", node)))
        self.drawer.channelDropped.connect(lambda node, at: self.events.append(("drop", node)))
        self.drawer.dragEnded.connect(lambda: self.events.append(("ended",)))
        self.drawer.playRequested.connect(lambda *args: self.events.append(("play",)))
        view = self.drawer._columns["right"].view
        self.viewport = view.viewport()
        self.row = view.visualRect(view.model().index(0, 0)).center()
        self.node = self.drawer._columns["right"].nodes[0]

    def mouse(self, kind, point, buttons=LEFT):
        global_point = QPointF(self.viewport.mapToGlobal(point))
        event = QMouseEvent(kind, QPointF(point), global_point, LEFT, buttons, Qt.NoModifier)
        QApplication.sendEvent(self.viewport, event)

    def drag(self):
        self.mouse(QEvent.Type.MouseButtonPress, self.row)
        self.mouse(QEvent.Type.MouseMove, self.row + QPoint(60, 0))
        self.mouse(QEvent.Type.MouseMove, self.row + QPoint(300, 40))
        self.mouse(QEvent.Type.MouseButtonRelease, self.row + QPoint(300, 40), Qt.NoButton)

    def test_with_the_grid_a_channel_row_drags_out_and_drops_without_a_click(self):
        self.drawer.set_drag_enabled(lambda: True)
        self.drag()
        kinds = [event[0] for event in self.events]
        self.assertEqual(kinds, ["drag", "ended", "drop"])  # once out of the list
        self.assertIs(self.events[0][1], self.node)
        self.assertIsNone(self.drawer.graphicsEffect())  # the list is whole again
        self.assertIsNone(self.drawer.dragging())

    def test_a_click_that_moves_but_stays_over_the_list_is_a_click(self):
        # A trackpad's click that moved a little: a drop there would land on the tile behind
        # the list, not on the one the list was opened for.
        self.drawer.set_drag_enabled(lambda: True)
        self.mouse(QEvent.Type.MouseButtonPress, self.row)
        self.mouse(QEvent.Type.MouseMove, self.row + QPoint(40, 6))
        self.mouse(QEvent.Type.MouseButtonRelease, self.row + QPoint(40, 6), Qt.NoButton)
        self.assertNotIn("drag", [event[0] for event in self.events])
        self.assertNotIn("drop", [event[0] for event in self.events])
        self.assertIsNone(self.drawer.dragging())

    def test_without_the_grid_nothing_drags(self):
        self.drag()
        self.assertNotIn("drag", [event[0] for event in self.events])


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
        # No player is made for a "+" tile (each one when its tile first needs it).
        self.assertEqual([slot.name for slot in self.sessions.slots()], ["main", "pip"])

    def test_a_smaller_layout_keeps_the_focused_tile_and_stops_the_rest(self):
        self.start_large(0)
        self.v()
        self.grid.set_cells(9)
        t5 = self.pick_in("t5", CHANNELS[2], "http://stream/3")
        self.grid.set_cells(2)
        self.assertEqual(self.grid.names(), ["main", "t5"])
        self.assertEqual(t5.playback.stops, 0)
        self.assertIs(self.sessions.large(), t5)


class TileLifecycleTests(MultiviewCase):
    """Nothing opens, or stays open, in a tile the grid no longer has; an open
    on its way survives leaving; a tile's size, rate and sound follow it when it opens."""

    hardware = True

    def setUp(self):
        super().setUp()
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}

    def others_hold_nothing(self):
        self.assertEqual(self.budget.holders("Home"), ["main"])
        for slot in self.sessions.slots():
            if slot is not self.main_slot:
                self.assertIsNone(slot.channel, slot.name)

    def test_a_link_on_its_way_never_opens_in_a_tile_a_smaller_layout_dropped(self):
        self.start_large(0)
        self.v()
        self.grid.set_cells(9)
        self.controller.play_in_tile("t6", CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(self.grid.state("t6"), OPENING)
        self.grid.set_cells(4)  # tile 7 goes while its link is on its way
        self.assertEqual(self.controller._tile_links, {})
        self.ready("http://stream/3")
        QTest.qWait(20)
        self.assertEqual(self.sessions.slot("t6").live.named("start"), [])
        self.others_hold_nothing()
        self.v()
        self.others_hold_nothing()

    def test_a_link_on_its_way_never_opens_once_the_grid_is_left_nor_stays_small(self):
        self.start_large(0)
        self.v()
        self.controller.play_in_tile("t3", CHANNELS[2], group_key="10", channels=CHANNELS)
        self.v()
        self.assertFalse(self.sessions.pip_open())  # it had no picture to keep small
        self.ready("http://stream/3")
        QTest.qWait(20)
        self.assertEqual(self.sessions.slot("t3").live.named("start"), [])
        self.others_hold_nothing()

    def test_leaving_stops_every_player_but_the_big_and_the_small_one(self):
        self.start_large(0)
        self.v()
        self.grid.set_cells(9)
        self.grid.set_cells(4)
        t6 = self.sessions.slot("t6") if "t6" in self.surfaces else self.make_slot("t6")
        kwargs = {"url": "http://stream/3", "headers": {}, "verify_ssl": True, "disk": False}
        kwargs.update(latency="low", user_agent="QiTV", max_bytes=64 * MIB)
        self.sessions.open_slot(  # a stream a tile left behind, however it came
            "t6", channel=CHANNELS[2], provider="Home", limit=4, start_kwargs=kwargs
        )
        self.assertTrue(wait_until(lambda: t6.live.running))
        self.v()
        self.assertEqual(t6.live.calls[-1], ("stop",))
        t6.live.released.emit()
        self.others_hold_nothing()

    def test_a_zap_just_before_leaving_still_plays_once_the_old_stream_is_released(self):
        self.start_large(0)
        self.v()
        self.controller.tune(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("http://stream/3")  # tile 1's old stream is still closing
        self.v()
        self.live.released.emit()
        self.assertTrue(wait_until(lambda: self.live.url == "http://stream/3"))
        self.assertTrue(self.live.running)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_open_in_picture_in_picture_says_to_leave_the_grid_first(self):
        self.start_large(0)
        self.v()
        links = len(self.catalog.links)
        self.controller._open_pip_node(NavNode("item", "3", "Three", CHANNELS[2]))
        self.controller.open_pip(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(self.toast.messages()[-2:], [(LEAVE_FIRST, "info")] * 2)
        self.assertEqual(len(self.catalog.links), links)
        self.assertEqual(self.grid.state("pip"), EMPTY)

    def test_a_tile_focused_before_its_stream_starts_opens_with_the_full_buffer(self):
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        self.controller.play_in_tile("t2", CHANNELS[3], group_key="10", channels=CHANNELS)
        self.ready("http://stream/4")  # it waits for its old stream's release
        self.grid_frame.focusRequested.emit("t2")
        t2.live.released.emit()
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url == "http://stream/4"))
        self.assertEqual(t2.live.named("start")[-1][1]["max_bytes"], 256 * MIB)

    def test_a_direct_tile_whose_stream_ends_gives_its_stream_back_and_says_so(self):
        self.catalog.ptype = "XTREAM"  # no portal: no fresh link by itself
        self.start_large(0)
        self.v()
        self.grid_frame.tileClicked.emit("t2")
        self.controller.tune(CHANNELS[2], group_key="10", channels=CHANNELS)
        self.ready("rtsp://stream/3")  # not time-shifted: it plays directly
        t2 = self.sessions.slot("t2")
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t2"])
        t2.playback.stateChanged.emit("ended")
        self.assertEqual(self.grid.state("t2"), FAILED)
        self.assertEqual(t2.playback.stops, 1)
        self.assertEqual(self.budget.holders("Home"), ["main"])

    def test_a_tile_made_while_the_grid_shows_is_small_and_at_1x(self):
        self.start_large(0)
        self.v()
        self.controller.play_in_tile("t2", CHANNELS[2], group_key="10", channels=CHANNELS)
        t2 = self.sessions.slot("t2")
        self.assertTrue(t2.live.background)
        self.assertEqual(t2.live.resizes, [64 * MIB])

    def test_a_tile_made_heard_is_muted_again_for_its_next_channel(self):
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        self.sessions.set_level("t2", 80)  # the mixer: heard beside the focused tile
        self.assertFalse(self.sessions.muted("t2"))
        self.controller.play_in_tile("t2", CHANNELS[3], group_key="10", channels=CHANNELS)
        self.assertTrue(self.sessions.muted("t2"))
        t2.live.released.emit()
        self.ready("http://stream/4")
        self.assertTrue(wait_until(lambda: t2.live.running and t2.live.url == "http://stream/4"))
        self.assertTrue(t2.playback.muted)

    def test_links_on_their_way_count_against_the_providers_limit(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 2}
        self.start_large(0)  # tile 1 holds one of Home's two streams
        self.v()
        self.grid_frame.tileClicked.emit("t2")  # tile 3 focused, its link on its way
        self.controller.tune(CHANNELS[1], group_key="10", channels=CHANNELS)
        links = len(self.catalog.links)
        self.controller.play_in_tile("t3", CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(self.grid.state("t3"), DENIED)  # tile 1's and tile 3's: none left
        self.assertEqual(len(self.catalog.links), links)
        self.grid_frame.focusRequested.emit("main")  # tile 3's choice is dropped
        self.controller.play_in_tile("pip", CHANNELS[2], group_key="10", channels=CHANNELS)
        self.assertEqual(self.grid.state("pip"), OPENING)  # its link on its way
        self.controller.play_in_tile("t3", CHANNELS[3], group_key="10", channels=CHANNELS)
        self.assertEqual(self.grid.state("t3"), DENIED)  # tile 1's and tile 2's
        self.assertEqual(len(self.catalog.links), links + 1)


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


class LayoutRowTests(unittest.TestCase):
    """The layouts above the layout chip: drawn, the one showing marked, those not offered
    dimmed; one click picks."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = QWidget()
        self.page.resize(PAGE)
        self.video = Recorder(self.page)
        self.video.setGeometry(0, 0, 400, 300)
        self.row = LayoutRow(self.page)
        self.page.show()
        self.addCleanup(self.page.close)
        self.events = []
        self.row.layoutChosen.connect(lambda n: self.events.append(("chosen", n)))
        self.row.unavailableChosen.connect(lambda n: self.events.append(("unavailable", n)))
        self.row.closed.connect(lambda: self.events.append(("closed",)))
        self.anchor = QRectF(1000, 650, 90, 28)

    def open(self, offered=(2, 4), current=4, hint=False):
        choices = [
            LayoutChoice(c, LAYOUT_NAMES[c], c in offered, "" if c in offered else "why")
            for c in (2, 4, 6, 9)
        ]
        self.row.show_layouts(choices, current, self.anchor, 640, hint=hint)

    def click(self, cells):
        QTest.mouseClick(self.row, LEFT, pos=self.row.cell_rect(cells).center().toPoint())

    def test_it_shows_the_four_layouts_above_the_chip_and_the_controls(self):
        self.open()
        self.assertTrue(self.row.isVisible())
        self.assertEqual([c.name for c in self.row.choices()], ["1×2", "2×2", "3×2", "3×3"])
        self.assertEqual(self.row.current(), 4)
        geometry = self.row.geometry()
        self.assertLess(geometry.bottom(), 640)
        self.assertLess(abs(geometry.center().x() - self.anchor.center().x()), 2)
        self.assertLessEqual(geometry.right(), PAGE.width())
        widths = {self.row.cell_rect(c).width() for c in (2, 4, 6, 9)}
        self.assertEqual(len(widths), 1)  # alike, side by side

    def test_one_click_picks_a_layout_offered_and_closes(self):
        self.open()
        self.click(2)
        self.assertEqual(self.events, [("closed",), ("chosen", 2)])
        self.assertFalse(self.row.isVisible())

    def test_a_dimmed_layout_says_why_and_picks_nothing(self):
        self.open()
        self.assertEqual(self.row.tooltip_at(self.row.cell_rect(9).center()), "why")
        self.assertEqual(self.row.tooltip_at(self.row.cell_rect(4).center()), "")
        self.click(9)
        self.assertEqual(self.events, [("unavailable", 9)])
        self.assertTrue(self.row.isVisible())

    def test_a_click_elsewhere_closes_it_and_does_nothing_more(self):
        self.open()
        QTest.mouseClick(self.video, LEFT, pos=QPoint(20, 20))
        self.assertEqual((self.events, self.video.presses), ([("closed",)], 0))

    def test_as_a_hint_a_click_elsewhere_goes_on(self):
        self.open(hint=True)
        self.assertTrue(self.row.hinting())
        QTest.mouseClick(self.video, LEFT, pos=QPoint(20, 20))
        self.assertEqual((self.events, self.video.presses), ([("closed",)], 1))

    def test_the_keys_move_from_the_layout_showing_and_enter_picks(self):
        self.open(offered=(2, 4, 6, 9))
        self.row.move_focus(1)
        self.assertEqual(self.row.focused(), 6)
        self.row.move_focus(1)
        self.row.move_focus(1)  # never past the last
        self.assertEqual(self.row.focused(), 9)
        self.row.activate()
        self.assertEqual(self.events[-1], ("chosen", 9))


class TileControlCase(MultiviewCase):
    """The grid with the layouts to pick from (a real LayoutRow over a page) and the mixer."""

    def setUp(self):
        self.mixer = FakeMixer()
        self.row_page = QWidget()
        self.row_page.resize(PAGE)
        self.layout_row = LayoutRow(self.row_page)
        self.row_page.show()
        self.addCleanup(self.row_page.close)
        super().setUp()
        self.config.multiview_hinted = True  # no hint: the tests open them

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.layout_row = self.layout_row
        return overlays

    def click_layout(self, cells):
        row = self.layout_row
        QTest.mouseClick(row, LEFT, pos=row.cell_rect(cells).center().toPoint())


class LayoutChoiceTests(TileControlCase):
    def test_the_layout_chip_opens_the_layouts_and_a_click_picks_one(self):
        self.start_large(0)
        self.v()
        self.transport.layoutRequested.emit()
        row = self.layout_row
        self.assertTrue(row.isVisible())
        self.assertEqual(row.current(), 4)
        self.assertEqual([c.available for c in row.choices()], [True, True, False, False])
        self.assertEqual(row.choices()[3].tooltip, NEEDS_HARDWARE)
        self.assertEqual(self.transport.held(), "layout")  # the controls stay, the chip lit
        self.click_layout(2)
        self.assertEqual(self.grid.names(), ["main", "pip"])
        self.assertFalse(row.isVisible())
        self.assertEqual(self.transport.held(), "")
        self.transport.layoutRequested.emit()
        self.click_layout(9)
        self.assertEqual(self.toast.messages()[-1], (NEEDS_HARDWARE, "info"))
        self.assertEqual(self.grid.names(), ["main", "pip"])
        self.transport.layoutRequested.emit()  # the chip again: they close
        self.assertFalse(row.isVisible())

    def test_the_keys_pick_a_layout_while_they_show_and_esc_closes_them(self):
        self.start_large(0)
        self.v()
        self.transport.layoutRequested.emit()
        self.controller.handle(Action.SEEK_BACK)  # ←: 1×2
        self.controller.handle(Action.OK)
        self.assertEqual(self.grid.names(), ["main", "pip"])
        self.transport.layoutRequested.emit()
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.layout_row.isVisible())
        self.assertTrue(self.grid.shown())  # Esc closed them, not the grid

    def test_the_first_grid_shows_the_layouts_once_by_themselves(self):
        self.config.multiview_hinted = False
        saves = self.config.saves
        self.start_large(0)
        self.v()
        self.assertTrue(self.layout_row.hinting())
        self.assertTrue(self.config.multiview_hinted)
        self.assertGreater(self.config.saves, saves)
        self.v()  # leaving closes them
        self.assertFalse(self.layout_row.isVisible())
        self.v()
        self.assertFalse(self.layout_row.isVisible())  # once only


class HardwareLayoutChoiceTests(TileControlCase):
    hardware = True

    def test_with_hardware_decoding_every_layout_is_offered(self):
        self.start_large(0)
        self.v()
        self.transport.layoutRequested.emit()
        self.assertTrue(all(c.available for c in self.layout_row.choices()))
        self.click_layout(9)
        self.assertEqual(len(self.grid.names()), 9)
        self.assertEqual(self.transport.named("set_grid_state")[-1], (True, True, "3×3"))


class TileControlTests(TileControlCase):
    def test_each_control_acts_on_its_tile_and_the_focus_stays(self):
        self.two_pictures()
        main_calls = len(self.live.calls)
        self.grid.tile_control("pip", "play")
        self.assertEqual(self.pip_live.calls[-1], ("toggle_pause",))
        self.grid.tile_control("pip", "live")  # at live: LIVE only says so
        self.assertNotIn(("go_live",), self.pip_live.calls)
        self.pip_live.timelineChanged.emit(SHIFTED)  # 200 s behind live
        self.assertEqual(self.grid.label("pip")[1], "−03:20")
        self.grid.tile_control("pip", "live")
        self.assertEqual(self.pip_live.calls[-1], ("go_live",))
        self.grid.tile_control("pip", "speaker")
        self.assertFalse(self.sessions.muted("pip"))
        self.assertFalse(self.sessions.muted("main"))
        self.assertEqual(len(self.live.calls), main_calls)  # the focused tile: untouched
        self.assertIs(self.sessions.large(), self.main_slot)
        self.grid.tile_control("main", "play")  # the focused tile: as Space
        self.assertEqual(self.live.calls[-1], ("toggle_pause",))
        self.assertIs(self.sessions.large(), self.main_slot)

    def test_close_stops_the_tile_gives_its_stream_back_and_shows_plus(self):
        self.catalog.current_provider = lambda: {"name": "Home", "type": "STB", "max_streams": 4}
        self.start_large(0)
        self.v()
        t2 = self.pick_in("t2", CHANNELS[2], "http://stream/3")
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual(sorted(self.budget.holders("Home")), ["main", "t2"])
        self.grid.tile_control("t2", "close")
        self.assertEqual(t2.live.calls[-1], ("stop",))
        t2.live.released.emit()  # its upstream closed
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.assertEqual(self.grid.state("t2"), EMPTY)
        self.assertIs(self.sessions.large(), self.main_slot)
        self.grid_frame.focusRequested.emit("t2")  # as "+", never as its old channel
        self.assertIsNone(self.controller._current)

    def test_closing_the_focused_tile_stops_its_stream_and_keeps_the_focus(self):
        self.start_large(0)
        self.v()
        self.assertEqual(self.budget.holders("Home"), ["main"])
        self.grid.tile_control("main", "close")
        self.assertEqual(self.live.calls[-1], ("stop",))
        self.live.released.emit()
        self.assertEqual(self.budget.holders("Home"), [])
        self.assertEqual(self.grid.state("main"), EMPTY)
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertIsNone(self.controller._current)

    def test_the_big_picture_leaves_the_grid_with_that_tile_big(self):
        self.two_pictures()
        self.grid.tile_control("pip", "big")
        self.assertFalse(self.grid.shown())
        self.assertIs(self.sessions.large(), self.pip_slot)
        self.assertIs(self.sessions.small(), self.main_slot)


class GridMixShellTests(TileControlCase):
    def test_a_mix_keeps_the_sound_as_the_focus_moves_and_leaving_ends_it(self):
        self.two_pictures()
        self.grid.tile_control("pip", "speaker")  # heard beside the focused tile: a mix
        self.assertTrue(self.sessions.mixed())
        self.grid_frame.focusRequested.emit("pip")  # the controls move, the sound stays
        self.assertEqual((self.session.muted, self.pip_session.muted), (False, False))
        self.grid.tile_control("main", "speaker")  # the tile without the focus now
        self.grid_frame.focusRequested.emit("main")
        self.assertEqual((self.session.muted, self.pip_session.muted), (True, False))
        self.v()  # leaving: the big picture is heard, the normal rule again
        self.assertFalse(self.sessions.mixed())
        self.assertIs(self.sessions.large(), self.main_slot)
        self.assertFalse(self.session.muted)
        self.assertFalse(self.pip_session.muted)  # the PiP stays heard, as one made audible
        self.v()
        self.assertFalse(self.sessions.mixed())  # the next grid follows the focus again

    def test_the_mixer_says_whose_sound_plays_and_follow_focus_goes_back(self):
        self.two_pictures()
        self.controller._mixer.open_main()
        self.assertEqual(self.mixer.mode, MixerMode(FOLLOWS_TEXT))
        self.mixer.muteRequested.emit(1)  # tile 2's row: heard
        self.assertEqual(self.mixer.mode, MixerMode(MIXED_TEXT, FOLLOW_ACTION))
        self.mixer.followRequested.emit()
        self.assertEqual(self.mixer.mode, MixerMode(FOLLOWS_TEXT))
        self.assertEqual((self.session.muted, self.pip_session.muted), (False, True))
        self.mixer.dismiss()
        self.controller.handle(Action.VOL_UP)  # the focused tile's level: never a mix
        self.assertFalse(self.sessions.mixed())
        self.controller.handle(Action.MUTE)
        self.controller.handle(Action.MUTE)
        self.assertFalse(self.sessions.mixed())
        self.v()
        self.controller._mixer.open_main()
        self.assertIsNone(self.mixer.mode)  # one picture: no line


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
        self.assertEqual(set(window.surfaces), {"main", "pip"})  # "+" tiles: no player yet
        tiles = window.grid_tiles.tiles()
        self.assertEqual([t.state for t in tiles], [EMPTY] * 4)
        self.assertTrue(tiles[0].focused)
        self.assertTrue(window.grid_tiles.isVisible())
        plus = tiles[2].rect.center()
        QTest.mouseClick(window.grid_tiles.hits, LEFT, pos=plus)
        self.assertEqual(window.sessions.large().name, "t2")  # its player made now
        self.assertEqual([s.name for s in window.sessions.slots()], ["main", "pip", "t2"])
        self.assertTrue(wait_until(drawer.isVisible))
        QTest.keyClick(window, Qt.Key.Key_Escape)  # the list closes
        self.assertTrue(wait_until(lambda: not drawer.isVisible()))
        QTest.keyClick(window, Qt.Key.Key_V)
        self.assertFalse(grid.shown())
        page = window.player_page.rect()
        self.assertEqual(window.surfaces["t2"].geometry(), page)  # the focused tile is big
        self.assertTrue(window.surfaces["t2"].isVisible())
        self.assertFalse(window.grid_tiles.isVisible())
        for name in ("main", "pip"):
            self.assertFalse(window.surfaces[name].isVisible(), name)


if __name__ == "__main__":
    unittest.main()
