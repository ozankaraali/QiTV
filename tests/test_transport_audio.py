"""The player controls' audio mode (the audio screen's dock) and its "⋯" menu (Task 4).

The dock is the mockup's: the scrub across the whole width, then shuffle, previous, play (an
accent circle), next and repeat in the middle, the volume at the right (the speaker opens the
mixer, the bar is a slider), ⋯ at the left; Words is the screen's own pill. It never fades, it
takes only its own band's clicks (the screen under it keeps its top bar's), and what the panels
and the captions anchor on keeps working. Every other mode lays out as before.
"""

from datetime import datetime
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from timer_fixtures import elapse

from services import i18n
from ui.keymap import Action, key_hint
from ui.overlays.audio_screen import AudioScreen
from ui.overlays.captions import CaptionsOverlay
from ui.overlays.more_menu import MoreMenu
from ui.overlays.transport import MENU_CONTROLS, Transport

MOCK_W = 920.0  # the mockup's page width (its px are fractions of it)
DOCK = {"seek", "times", "shuffle", "prev_track", "play", "next_track", "repeat", "volume"}
DOCK |= {"level", "more"}
MENU = ("stop", "audio", "subtitles", "picture", "fullscreen", "mini", "library", "search")
TIMELINE = {
    "start": 0.0,
    "end": 600.0,
    "position": 588.0,
    "live_edge": 600.0,
    "lag": 12.5,
    "mode": "live",
    "rate": 1.05,
    "paused": False,
}
SIZES = ((1280, 720), (1280, 540), (640, 360), (480, 270))


class _Host(QWidget):
    """The overlay host: what no overlay takes reaches it."""

    def __init__(self) -> None:
        super().__init__()
        self.presses: list[QPoint] = []

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.presses.append(event.position().toPoint())


def _extras(t: Transport, *, next_=False, library=False, pip=False, grid=False) -> None:
    t.set_next_available(next_)
    t.set_library_available(library)
    t.set_pip_state(pip, False)
    t.set_grid_state(pip, grid, "3×3" if grid else "", grid, cells=9 if grid else 0)


def _movie(t: Transport, **extras) -> None:
    _extras(t, **extras)
    t.show_vod("Film", "2024")
    t.update_position(30_000, 100_000)


def _live(t: Transport, **extras) -> None:
    _extras(t, **extras)
    t.show_live("One", "News")


def _failed(t: Transport) -> None:
    _extras(t)
    t.show_failed("One", "News")


def _timeshift(t: Transport, behind: bool) -> None:
    _extras(t)
    t.show_timeshift("One", "News")
    if behind:
        t.update_timeline(dict(TIMELINE, position=300.0, lag=300.0, mode="shifted", rate=1.0), [])
    else:
        t.update_timeline(TIMELINE, [])


# Every mode but audio, each set up whole (a name, and how).
OLD_MODES = (
    ("movie", _movie),
    ("movie from the Library, with Next", lambda t: _movie(t, next_=True, library=True)),
    ("live", _live),
    ("live, second channel and multiview", lambda t: _live(t, pip=True, grid=True)),
    ("failed", _failed),
    ("time-shift, live edge", lambda t: _timeshift(t, False)),
    ("time-shift, behind", lambda t: _timeshift(t, True)),
)
NOON = datetime(2026, 11, 18, 12, 0).astimezone()  # the title row's clock, held still


def snapshot(t: Transport) -> dict:
    layout = t._layout()
    return {
        "layout": {name: rect.getRect() for name, rect in layout.items()},
        "titles": {name: rect.getRect() for name, rect in t.title_rects().items()},
        "top": t.controls_top(),
        "mask": [rect.getRect() for rect in t.mask_region()],
        "widget mask": t.mask().isEmpty(),
        "texts": t.texts(),
    }


def double_press(widget: QWidget, point: QPointF) -> None:
    """A quick double-click as Qt delivers it: press, release, the second press as a
    double-click, release."""
    local = QPointF(point)
    world = QPointF(widget.mapToGlobal(local.toPoint()))
    for kind, buttons in (
        (QEvent.Type.MouseButtonPress, Qt.LeftButton),
        (QEvent.Type.MouseButtonRelease, Qt.NoButton),
        (QEvent.Type.MouseButtonDblClick, Qt.LeftButton),
        (QEvent.Type.MouseButtonRelease, Qt.NoButton),
    ):
        event = QMouseEvent(kind, local, world, Qt.LeftButton, buttons, Qt.NoModifier)
        QApplication.sendEvent(widget, event)


def accent_ink(image: QImage, rect: QRectF) -> int:
    """How far the reddest pixel in `rect` leans to the accent (red minus blue): the accent
    #e08a5f reads about 130, the dim grey #8d98a4 and the dark shade below zero."""
    box = rect.toAlignedRect().intersected(image.rect())
    best = -255
    for y in range(box.top(), box.bottom() + 1):
        for x in range(box.left(), box.right() + 1):
            c = QColor(image.pixel(x, y))
            best = max(best, c.red() - c.blue())
    return best


class AudioCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def host(self, width=1280, height=720) -> _Host:
        host = _Host()
        host.resize(width, height)
        host.show()
        self.addCleanup(host.deleteLater)
        self.addCleanup(host.close)
        return host

    def dock(self, host=None, idle_ms=3000, *, has_list=True) -> Transport:
        t = Transport(host if host is not None else self.host(), idle_ms=idle_ms)
        t.show_audio("Ferries at Night", "Mira Sol · Harbour Lights")
        t.set_audio_state(False, "off", has_list)
        t.update_position(84_000, 221_000)
        return t

    def click(self, widget, point):
        QTest.mouseClick(widget, Qt.LeftButton, Qt.NoModifier, QPointF(point).toPoint())


class DockLayoutTests(AudioCase):
    def test_audio_mode_lays_out_the_dock(self):
        host = self.host()
        t = self.dock(host)
        rects = t._layout()
        self.assertEqual(set(rects), DOCK)
        self.assertEqual(t.title_rects(), {})  # the screen has the title, the dock none
        k = 1280 / MOCK_W  # a mockup px at this width
        # The middle row: shuffle, previous, play, next, repeat, centred, on one line.
        row = [rects[n] for n in ("shuffle", "prev_track", "play", "next_track", "repeat")]
        for left, right in zip(row, row[1:]):
            self.assertLess(left.right(), right.left())
            self.assertAlmostEqual(right.left() - left.right(), 22 * k, delta=1.0)
        middle = (row[0].left() + row[-1].right()) / 2
        self.assertAlmostEqual(middle, 640, delta=1.0)
        for rect in row:
            self.assertAlmostEqual(rect.center().y(), rects["play"].center().y(), delta=0.5)
        # Play is the accent circle, 54 mockup px; shuffle and repeat 18, previous and next 22.
        self.assertAlmostEqual(rects["play"].width(), 54 * k, delta=1.0)
        self.assertAlmostEqual(rects["play"].height(), rects["play"].width(), delta=0.01)
        self.assertAlmostEqual(rects["shuffle"].width(), 18 * k, delta=1.0)
        self.assertAlmostEqual(rects["prev_track"].width(), 22 * k, delta=1.0)
        # The row sits 18 px from the bottom, the scrub 18 px above it, across the dock.
        self.assertAlmostEqual(rects["play"].bottom(), 720 - 18 * k, delta=1.0)
        self.assertLess(rects["seek"].bottom(), rects["play"].top())
        self.assertAlmostEqual(rects["times"].left(), 26 * k, delta=1.0)
        self.assertAlmostEqual(rects["times"].right(), 1280 - 26 * k, delta=1.0)
        self.assertGreater(rects["seek"].left(), rects["times"].left())
        self.assertLess(rects["seek"].right(), rects["times"].right())
        self.assertGreater(rects["seek"].width(), 1000)  # the scrub runs across the dock
        # The volume at the right: the speaker, 8 px, then its 90 px bar; ⋯ at the left.
        self.assertAlmostEqual(rects["level"].right(), 1280 - 26 * k, delta=1.0)
        self.assertAlmostEqual(rects["level"].width(), 90 * k, delta=1.0)
        self.assertAlmostEqual(rects["level"].left() - rects["volume"].right(), 8 * k, delta=1.0)
        self.assertAlmostEqual(rects["volume"].width(), 18 * k, delta=1.0)
        self.assertAlmostEqual(rects["more"].left(), 26 * k, delta=1.0)
        self.assertLess(rects["more"].right(), rects["shuffle"].left())
        self.assertGreater(rects["volume"].left(), rects["repeat"].right())
        self.assertEqual(t.texts(), ("1:24", "3:41"))  # elapsed, and the song's length
        # Every control lies in the dock's band, under controls_top().
        top = t.controls_top() - t.geometry().top()
        for name, rect in rects.items():
            self.assertGreaterEqual(rect.top(), top, name)

    def test_the_dock_stays_under_the_screens_content_and_its_controls_never_overlap(self):
        tracks = [{"title": f"Song {i}", "track": i + 1, "duration_ms": 200_000} for i in range(30)]
        for width, height in SIZES:
            for view in ("cover", "album", "stage"):
                with self.subTest(size=(width, height), view=view):
                    host = self.host(width, height)
                    screen = AudioScreen(host)
                    screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights")
                    screen.set_tracks(tracks, 1)
                    screen.set_view(view)
                    screen.show()
                    t = self.dock(host)
                    lay = screen._layout()
                    content = [lay.subtitle.bottom(), lay.bars.bottom()]
                    content += [rect.bottom() for _, rect in lay.rows]
                    self.assertLessEqual(max(content), t.controls_top() + 0.5)
                    hits = t._hits(t._layout())
                    for name, rect in hits.items():
                        for other, rect2 in hits.items():
                            if name < other and "times" not in (name, other):
                                # Two hit areas may meet half way between their buttons.
                                both = rect.intersected(rect2)
                                touch = min(both.width(), both.height()) < 0.01
                                self.assertTrue(both.isEmpty() or touch, (name, other))

    def test_a_small_window_keeps_every_control_apart_in_each_language(self):
        def assert_apart(t, where):
            hits = t._hits(t._layout())
            for name, rect in hits.items():
                for other, rect2 in hits.items():
                    if name < other and "times" not in (name, other):
                        both = rect.intersected(rect2)
                        touch = min(both.width(), both.height()) < 0.01
                        self.assertTrue(both.isEmpty() or touch, (where, name, other))

        self.addCleanup(i18n.set_language, "en")
        for language in ("en", "tr", "de", "fr"):
            self.assertEqual(i18n.set_language(language), language)
            for width in (300, 360, 480):
                with self.subTest(language=language, width=width):
                    t = self.dock(self.host(width, width * 9 // 16))
                    self.assertEqual(set(t._layout()), DOCK)
                    assert_apart(t, (language, width))

    def test_shuffle_and_repeat_show_their_state_and_say_what_they_do(self):
        t = self.dock()
        rects = t._layout()
        dim = t.grab().toImage()
        self.assertLess(accent_ink(dim, rects["shuffle"]), 20)
        self.assertLess(accent_ink(dim, rects["repeat"]), 20)
        self.assertEqual(t.icon("repeat"), "repeat")
        t.set_audio_state(True, "all", True)
        lit = t.grab().toImage()
        self.assertGreater(accent_ink(lit, rects["shuffle"]), 90)
        self.assertGreater(accent_ink(lit, rects["repeat"]), 90)
        t.set_audio_state(True, "one", True)
        self.assertEqual(t.icon("repeat"), "repeat_one")
        self.assertEqual(t.tooltip_at(rects["repeat"].center()), "Repeat one")
        t.set_audio_state(False, "off", True)
        for name, text in (
            ("shuffle", "Shuffle"),
            ("repeat", "Repeat"),
            ("prev_track", "Previous track"),
            ("next_track", "Next track"),
            ("more", "More"),
            ("play", key_hint("Play / pause", "Space")),
            ("volume", "Volume (+/−, M)"),
            ("level", "Volume (+/−, M)"),
        ):
            self.assertEqual(t.tooltip_at(rects[name].center()), text, name)
        with self.assertRaises(ValueError):
            t.set_audio_state(False, "twice", True)

    def test_each_dock_control_asks_for_its_action(self):
        host = self.host()
        t = self.dock(host)
        rects = t._layout()
        for name, signal in (
            ("shuffle", t.shuffleRequested),
            ("prev_track", t.prevTrackRequested),
            ("play", t.playPauseRequested),
            ("next_track", t.nextTrackRequested),
            ("repeat", t.repeatRequested),
            ("volume", t.volumeRequested),  # the speaker: the mixer
        ):
            spy = QSignalSpy(signal)
            self.click(t, rects[name].center())
            self.assertEqual(spy.count(), 1, name)
            QTest.qWait(QApplication.doubleClickInterval() + 20)  # each a single click
        spy = QSignalSpy(t.moreRequested)
        self.click(t, rects["more"].center())
        self.assertEqual(spy.count(), 1)
        self.assertEqual(spy.at(0)[0], t.mapToGlobal(rects["more"].topLeft().toPoint()))
        # The scrub seeks where it is clicked (the song is 221 s long).
        seek = rects["seek"]
        spy = QSignalSpy(t.seekRequested)
        self.click(t, QPointF(seek.left() + seek.width() / 2, seek.center().y()))
        self.assertEqual(spy.count(), 1)
        self.assertAlmostEqual(spy.at(0)[0], 110_500, delta=400)
        self.assertFalse(t.nextRequested is t.nextTrackRequested)  # a film's Next stays its own

    def test_a_quick_second_press_on_previous_or_next_counts(self):
        # ⏮⏮: the song from its start, then the one before; ⏭⏭: two songs on. A toggle (repeat)
        # still counts once: a second press would undo the first.
        t = self.dock()
        rects = t._layout()
        for name, signal, presses in (
            ("prev_track", t.prevTrackRequested, 2),
            ("next_track", t.nextTrackRequested, 2),
            ("repeat", t.repeatRequested, 1),
        ):
            QTest.qWait(QApplication.doubleClickInterval() + 20)
            spy = QSignalSpy(signal)
            double_press(t, rects[name].center())
            self.assertEqual(spy.count(), presses, name)

    def test_the_volume_bar_is_a_slider_and_the_speaker_opens_the_mixer(self):
        t = self.dock()
        rects = t._layout()
        bar = rects["level"]
        y = round(bar.center().y())

        def at(fraction: float) -> QPoint:
            return QPoint(round(bar.left() + bar.width() * fraction), y)

        sets, mixer = QSignalSpy(t.volumeSetRequested), QSignalSpy(t.volumeRequested)
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, at(0.25))
        self.assertEqual(sets.count(), 1)
        self.assertAlmostEqual(sets.at(0)[0], 25, delta=1)
        QTest.mouseMove(t, at(0.75))
        self.assertAlmostEqual(sets.at(sets.count() - 1)[0], 75, delta=1)
        QTest.mouseMove(t, QPoint(round(bar.right()) + 40, y))  # past its end: all the way
        self.assertEqual(sets.at(sets.count() - 1)[0], 100)
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, QPoint(round(bar.right()) + 40, y))
        self.assertEqual(t.icon("volume"), "volume_3")  # the speaker shows the level set
        self.assertEqual(mixer.count(), 0)
        self.assertEqual(t.tooltip_at(QPointF(at(0.5))), "Volume (+/−, M)")
        count = sets.count()
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        self.click(t, rects["volume"].center())  # the speaker: the mixer, no level
        self.assertEqual(mixer.count(), 1)
        self.assertEqual(sets.count(), count)

    def test_the_scrubs_press_area_never_reaches_the_buttons(self):
        for width, height in ((360, 203), (300, 169)):
            with self.subTest(size=(width, height)):
                t = self.dock(self.host(width, height))
                rects = t._layout()
                hits = t._hits(rects)
                press = t._seek_hit(rects)
                self.assertTrue(press.contains(rects["seek"].center()))
                for name, hit in hits.items():
                    if name not in ("seek", "times"):
                        both = press.intersected(hit)
                        touch = min(both.width(), both.height()) < 0.01
                        self.assertTrue(both.isEmpty() or touch, name)
                # A press just inside a button's hit area acts there, never seeks.
                QTest.qWait(QApplication.doubleClickInterval() + 20)
                seeks, previous = QSignalSpy(t.seekRequested), QSignalSpy(t.prevTrackRequested)
                top = QPointF(hits["prev_track"].center().x(), hits["prev_track"].top() + 1)
                self.click(t, top)
                self.assertEqual((seeks.count(), previous.count()), (0, 1))

    def test_without_an_album_list_shuffle_and_next_are_dim_and_ask_nothing(self):
        t = self.dock(has_list=False)
        rects = t._layout()
        self.assertEqual(set(rects), DOCK)  # the buttons keep their places
        image = t.grab().toImage()
        for name in ("shuffle", "next_track"):
            QTest.qWait(QApplication.doubleClickInterval() + 20)
            spy = QSignalSpy(t.shuffleRequested if name == "shuffle" else t.nextTrackRequested)
            self.click(t, rects[name].center())
            self.assertEqual(spy.count(), 0, name)

        # Next is drawn dim: its brightest pixel is well under the previous track's.
        def brightest(rect):
            box = rect.toAlignedRect()
            return max(
                QColor(image.pixel(x, y)).lightness()
                for y in range(box.top(), box.bottom() + 1)
                for x in range(box.left(), box.right() + 1)
            )

        self.assertLess(brightest(rects["next_track"]), brightest(rects["prev_track"]) - 60)
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        spy = QSignalSpy(t.prevTrackRequested)  # previous still starts the song again
        self.click(t, rects["prev_track"].center())
        self.assertEqual(spy.count(), 1)
        # Task 5 review M8: no list, but something follows (an audio-only episode's next one, a
        # playlist's next entry): next is lit and acts; shuffle stays dim and asks nothing.
        t.set_audio_state(False, "off", False, has_next=True)
        image = t.grab().toImage()
        self.assertGreaterEqual(brightest(rects["next_track"]), brightest(rects["prev_track"]) - 5)
        for name, wanted in (("next_track", 1), ("shuffle", 0)):
            QTest.qWait(QApplication.doubleClickInterval() + 20)
            spy = QSignalSpy(t.nextTrackRequested if name == "next_track" else t.shuffleRequested)
            self.click(t, rects[name].center())
            self.assertEqual(spy.count(), wanted, name)


class DockBehaviourTests(AudioCase):
    def test_audio_mode_never_fades(self):
        t = self.dock(idle_ms=60)
        self.assertTrue(t.isVisible())
        self.assertTrue(t.audio())
        elapse(300)
        self.assertTrue(t.isVisible())
        t.poke()
        elapse(300)
        self.assertTrue(t.isVisible())
        t.dismiss()  # a click on the picture, the Library opening: it stays
        self.assertTrue(t.isVisible())
        t.hold("audio")
        t.hold("")  # a panel closing
        elapse(300)
        self.assertTrue(t.isVisible())
        seek = t._layout()["seek"]  # a drag on the scrub, then its release
        QTest.mousePress(t, Qt.LeftButton, Qt.NoModifier, seek.center().toPoint())
        QTest.mouseRelease(t, Qt.LeftButton, Qt.NoModifier, seek.center().toPoint())
        elapse(300)
        self.assertTrue(t.isVisible())
        # Another mode fades again.
        t.show_vod("Film", "")
        self.assertFalse(t.audio())
        elapse(300)
        self.assertFalse(t.isVisible())

    def test_end_audio_leaves_the_mode_and_hides(self):
        t = self.dock(idle_ms=60)
        t.end_audio()
        self.assertFalse(t.audio())
        self.assertFalse(t.isVisible())
        self.assertTrue(t.mask().isEmpty())
        t.poke()
        self.assertTrue(t.isVisible())
        elapse(300)
        self.assertFalse(t.isVisible())  # it fades as before

    def test_the_dock_lets_the_screen_under_it_take_its_clicks(self):
        # The transport covers the host and sits over the screen: in audio mode it is the dock's
        # band only, so the screen's top bar, the album rows and the page reach their owners.
        host = self.host()
        screen = AudioScreen(host)
        screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights")
        screen.set_tracks([{"title": "One"}, {"title": "Two"}], 0)
        screen.show()
        t = self.dock(host)
        t.raise_()
        back = QPointF(screen.controls()["back"].center())
        self.assertIs(host.childAt(back.toPoint()), screen)
        self.assertIs(host.childAt(t._layout()["play"].center().toPoint()), t)
        self.assertIs(host.childAt(QPoint(640, 300)), screen)  # the page: the screen, then host
        spy = QSignalSpy(screen.backRequested)
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, back.toPoint())
        self.assertEqual(spy.count(), 1)
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(640, 300))
        self.assertEqual(host.presses, [QPoint(640, 300)])
        self.assertEqual(
            [rect.getRect() for rect in t.mask_region()],
            [rect.getRect() for rect in t.mask()],
        )
        host.resize(960, 540)  # the band follows the host
        self.assertEqual(t.mask().boundingRect().bottom(), 539)
        self.assertGreater(t.mask().boundingRect().top(), 540 * 0.7)

    def test_other_modes_lay_out_exactly_as_before_audio_mode(self):
        # Each old mode, set up on a transport that never saw the dock and on one that comes
        # from it, lays out the same: names and rects, the band, the masks, the times.
        for width, height in SIZES:
            visited = Transport(self.host(width, height), now=lambda: NOON)
            for name, mode in OLD_MODES:
                with self.subTest(size=(width, height), mode=name):
                    fresh = Transport(self.host(width, height), now=lambda: NOON)
                    mode(fresh)
                    visited.show_audio("Song", "Artist")
                    visited.set_audio_state(True, "one", True)
                    visited.update_position(84_000, 221_000)
                    self.assertNotEqual(snapshot(visited)["layout"], snapshot(fresh)["layout"])
                    mode(visited)
                    self.assertFalse(visited.audio())
                    self.assertEqual(snapshot(visited), snapshot(fresh))
                    self.assertTrue(visited.mask().isEmpty())


class AnchorTests(AudioCase):
    def test_panels_still_anchor_in_audio_mode(self):
        host = self.host()
        t = Transport(host)
        t.show_vod("Film", "")
        spy = QSignalSpy(t.bandChanged)
        captions = CaptionsOverlay(host)
        captions.follow(t)
        t.show_audio("Ferries at Night", "Mira Sol")
        self.assertGreaterEqual(spy.count(), 1)  # its band moved: the captions and panels hear
        rects = t._layout()
        # What lives behind ⋯ anchors on ⋯: its panel opens above it.
        for name in MENU_CONTROLS:
            self.assertEqual(t.button_rect(name), rects["more"], name)
        self.assertEqual(t.button_rect("volume"), rects["volume"])  # the mixer's speaker
        self.assertEqual(t.button_rect("play"), rects["play"])
        with self.assertRaises(KeyError):
            t.button_rect("guide")  # live TV's: none here
        # The band starts at the dock's top: the captions sit on it, as the panels do.
        top = t.controls_top()
        self.assertEqual(captions._floor, top)
        self.assertLessEqual(top, rects["seek"].top() + t.geometry().top())
        self.assertGreater(top, 720 * 0.7)
        # A panel held lights ⋯ (it is the button its panel came from) and keeps nothing up.
        more = rects["more"]
        self.assertLess(accent_ink(t.grab().toImage(), more), 60)
        t.hold("audio")
        self.assertEqual(t.held(), "audio")
        self.assertGreater(accent_ink(t.grab().toImage(), more), 60)
        t.hold("")
        self.assertLess(accent_ink(t.grab().toImage(), more), 60)
        # The mixer held lights the speaker, as on a film's controls.
        volume = rects["volume"]
        speaker = QRectF(volume.left(), volume.top(), volume.height(), volume.height())
        self.assertLess(accent_ink(t.grab().toImage(), speaker), 20)
        t.hold("volume")
        self.assertEqual(t.button_rect("volume"), volume)
        self.assertGreater(accent_ink(t.grab().toImage(), speaker), 90)
        self.assertLess(accent_ink(t.grab().toImage(), more), 60)
        t.hold("")
        # Back to a film: the band and the captions follow.
        t.show_vod("Film", "")
        t.update_position(0, 100_000)
        self.assertEqual(captions._floor, t.controls_top())
        self.assertNotEqual(t.controls_top(), top)


class MoreMenuTests(AudioCase):
    def menu(self):
        host = self.host()
        t = self.dock(host)
        menu = MoreMenu(t, host)
        t.moreRequested.connect(menu.open_at)  # Task 5's wiring, here by hand
        return host, t, menu

    def open(self, t, menu):
        self.click(t, t._layout()["more"].center())
        self.assertTrue(menu.isVisible())

    def test_more_menu_items_emit_their_actions(self):
        host, t, menu = self.menu()
        signals = {
            "stop": t.stopRequested,
            "audio": t.audioRequested,
            "subtitles": t.subtitleRequested,
            "picture": t.pictureRequested,
            "fullscreen": t.fullscreenRequested,
            "mini": t.miniPlayerRequested,
            "library": t.libraryRequested,
            "search": t.searchRequested,
        }
        self.assertEqual(tuple(MENU_CONTROLS), MENU)
        for name in MENU:
            with self.subTest(item=name):
                QTest.qWait(QApplication.doubleClickInterval() + 20)
                self.open(t, menu)
                controls = menu.controls()
                self.assertEqual(tuple(controls), MENU)  # top to bottom, in this order
                spies = {n: QSignalSpy(s) for n, s in signals.items()}
                self.click(menu, controls[name].center())
                self.assertFalse(menu.isVisible())  # it closes, then the action goes
                for other, spy in spies.items():
                    self.assertEqual(spy.count(), 1 if other == name else 0, (name, other))

    def test_it_opens_above_its_button_and_names_its_items_as_the_buttons_do(self):
        host, t, menu = self.menu()
        self.open(t, menu)
        more = t._layout()["more"].translated(t.pos())
        self.assertLessEqual(menu.geometry().bottom(), more.top())
        self.assertTrue(host.rect().contains(menu.geometry()))
        self.assertEqual(t.held(), "more")  # ⋯ is lit while it shows
        self.assertEqual(menu.label("stop"), key_hint("Stop", "X"))
        self.assertEqual(menu.label("search"), key_hint("Search", "Ctrl+F"))
        self.assertEqual(menu.label("library"), key_hint("Back to the Library", "Esc"))
        self.assertEqual(menu.label("fullscreen"), "Fullscreen (F)")
        for name in MENU:
            self.assertEqual(menu.label(name), t.control_text(name))
        t.set_window_mode(mini=False, fullscreen=True)
        self.assertEqual(menu.label("fullscreen"), "Exit fullscreen (F)")
        menu.dismiss()
        self.assertEqual(t.held(), "")

    def test_esc_closes_the_menu_not_the_player(self):
        host, t, menu = self.menu()
        self.assertFalse(menu.handle(Action.ESCAPE))  # closed: Esc goes on to the player
        self.open(t, menu)
        self.assertTrue(menu.handle(Action.ESCAPE))
        self.assertFalse(menu.isVisible())
        self.assertFalse(menu.handle(Action.ESCAPE))
        self.open(t, menu)
        self.assertFalse(menu.handle(Action.PLAY_PAUSE))  # another key closes it and goes on
        self.assertFalse(menu.isVisible())

    def test_a_click_outside_closes_it(self):
        host, t, menu = self.menu()
        self.open(t, menu)
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, QPoint(640, 200))
        self.assertFalse(menu.isVisible())
        self.assertEqual(host.presses, [])  # it closed the menu and did nothing more
        # Its own button only closes it.
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        self.open(t, menu)
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        spy = QSignalSpy(t.moreRequested)
        more = t._layout()["more"].center().toPoint()
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, more)
        self.assertFalse(menu.isVisible())
        self.assertEqual(spy.count(), 0)
        # Another of the dock's buttons closes it and acts.
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        self.open(t, menu)
        QTest.qWait(QApplication.doubleClickInterval() + 20)
        spy = QSignalSpy(t.playPauseRequested)
        play = t._layout()["play"].center().toPoint()
        QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, play)
        self.assertFalse(menu.isVisible())
        self.assertEqual(spy.count(), 1)

    def test_the_menu_goes_with_its_dock(self):
        # Another mode (a playlist's next entry is a film) and end_audio() each close it and let
        # go of ⋯, so a film's controls fade as theirs do.
        for leave in ("film", "end"):
            with self.subTest(leave=leave):
                host = self.host()
                t = self.dock(host, idle_ms=60)
                menu = MoreMenu(t, host)
                menu.open_at(t.mapToGlobal(t.button_rect("more").topLeft().toPoint()))
                self.assertTrue(menu.isVisible())
                if leave == "film":
                    t.show_vod("Film", "")
                    self.assertFalse(menu.isVisible())
                    self.assertEqual(t.held(), "")
                    elapse(300)
                    self.assertFalse(t.isVisible())  # faded
                else:
                    t.end_audio()
                    self.assertFalse(menu.isVisible())
                    self.assertEqual(t.held(), "")

    def test_the_menu_fits_a_short_window_and_scrolls(self):
        # Sized with the dock's unit (capped by the window's height), its rows fit the room
        # above ⋯; with less room than its rows need, they scroll (the wheel), as the Sound
        # panel's do. It never covers ⋯ and stays inside the window.
        for height in (360, 150):
            with self.subTest(height=height):
                host = self.host(1280, height)
                t = self.dock(host)
                menu = MoreMenu(t, host)
                menu.open_at(t.mapToGlobal(t.button_rect("more").topLeft().toPoint()))
                more = t.button_rect("more").translated(QPointF(t.pos()))
                self.assertTrue(host.rect().contains(menu.geometry()), menu.geometry())
                self.assertLessEqual(menu.geometry().bottom(), more.top())
                inside = QRectF(menu.rect())
                for rect in menu.controls().values():
                    self.assertTrue(inside.contains(QRectF(rect)))
                if height == 360:
                    self.assertEqual(tuple(menu.controls()), MENU)  # all of them fit
                    continue
                shown = tuple(menu.controls())
                self.assertLess(len(shown), len(MENU))
                self.assertEqual(shown, MENU[: len(shown)])
                centre = QPointF(menu.rect().center())
                wheel = QWheelEvent(
                    centre,
                    QPointF(menu.mapToGlobal(centre.toPoint())),
                    QPoint(),
                    QPoint(0, -120 * len(MENU)),  # down, past the end
                    Qt.NoButton,
                    Qt.NoModifier,
                    Qt.NoScrollPhase,
                    False,
                )
                QApplication.sendEvent(menu, wheel)
                shown = tuple(menu.controls())
                self.assertEqual(shown, MENU[-len(shown) :])  # the last rows, Search included
                spy = QSignalSpy(t.searchRequested)
                self.click(menu, menu.controls()["search"].center())
                self.assertEqual(spy.count(), 1)

    def test_the_dock_speaks_turkish(self):
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        t = self.dock()
        rects = t._layout()
        self.assertEqual(t.tooltip_at(rects["shuffle"].center()), "Karıştır")
        self.assertEqual(t.tooltip_at(rects["next_track"].center()), "Sonraki parça")
        # The button's verb; the record timer's field keeps the noun.
        self.assertEqual(t.tooltip_at(rects["repeat"].center()), "Tekrarla")
        self.assertEqual(i18n.tr("Repeat"), "Tekrar")
        t.set_audio_state(False, "one", True)
        self.assertEqual(t.tooltip_at(rects["repeat"].center()), "Bu parçayı tekrarla")


if __name__ == "__main__":
    unittest.main()
