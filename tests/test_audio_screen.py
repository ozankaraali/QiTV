"""The audio screen (ui/overlays/audio_screen.py): what shows on the player page while something
plays with sound and no picture, in three views (Cover, Album and Stage), lit by its cover's
colours. Its controls are its own (back, Words, Up next, the view switch, the album's rows);
every other click, double-click and wheel reaches the overlay host (play/pause, fullscreen and
volume stay where they were). The album, its songs and the lines are fictional."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRegion, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from motion_fixtures import pin_motion
from timer_fixtures import elapse

from services import i18n
from services.song_facts import BRAND_COLOURS
from ui.overlay_host import SETTLE_CHECK_MS, OverlayHost
from ui.overlays.audio_screen import DOCK_FRACTION, AudioScreen, name_upper
from ui.overlays.captions import CaptionsOverlay
from ui.overlays.transport import Transport

SONGS = [
    ("Low Tide", 242_000),
    ("Ferries at Night", 221_000),
    ("Salt and Neon", 195_000),
    ("Lantern Street", 288_000),
    ("The Long Pier", 177_000),
    ("Gulls", 210_000),
    ("Last Boat Home", 306_000),
    ("Harbour Lights", 260_000),
]
TOP_BAR = {"back", "words", "view_cover", "view_album", "view_stage"}
GREEN = (40, 200, 120)


def track(index: int, title: str, ms: int) -> dict:
    """A Track (services.song_facts) of the fictional album."""
    name = f"{index + 1:02d} {title}.mp3"
    return {
        "path": f"/music/Harbour Lights/{name}",
        "name": name,
        "size": 4_000_000 + index,
        "title": title,
        "artist": "Mira Sol",
        "album_artist": "",
        "album": "Harbour Lights",
        "disc": 1,
        "track": index + 1,
        "duration_ms": ms,
        "year": "2024",
        "art_url": "",
        "now_playing": "",
        "parsed": True,
    }


ALBUM = [track(i, title, ms) for i, (title, ms) in enumerate(SONGS)]


def solid(colour: tuple[int, int, int], size: int = 64) -> QImage:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(*colour))
    return image


def diff_box(a: QImage, b: QImage) -> QRect:
    """Where two grabs differ (an empty rect when they don't)."""
    box = QRect()
    for y in range(0, a.height(), 2):
        for x in range(0, a.width(), 2):
            if a.pixel(x, y) != b.pixel(x, y):
                box = box.united(QRect(x, y, 2, 2))
    return box


class Painted:
    """The texts painted (QPainter.drawText) while it is installed."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.texts: list[str] = []
        original = QPainter.drawText

        def draw(painter, *args, **kwargs):
            self.texts += [a for a in args if isinstance(a, str)]
            return original(painter, *args, **kwargs)

        QPainter.drawText = draw
        case.addCleanup(setattr, QPainter, "drawText", original)


class ScreenCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        pin_motion(self)
        self.now = [100.0]

    def make(
        self, size=(1280, 720), *, view="cover", tracks=ALBUM, current=1, show=True
    ) -> AudioScreen:
        page = QWidget()
        page.resize(*size)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.close)
        screen = AudioScreen(page, clock=lambda: self.now[0])
        screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights", year="2024")
        screen.set_cover(None, BRAND_COLOURS)
        screen.set_tracks(tracks, current)
        screen.set_view(view)
        page.show()
        if show:
            screen.show()
        return screen

    def click(self, screen: AudioScreen, name: str) -> None:
        QTest.mouseClick(screen, Qt.LeftButton, Qt.NoModifier, screen.controls()[name].center())

    def assert_laid_out(self, screen: AudioScreen) -> None:
        """Every control is inside the screen, above the dock's room, and on its own."""
        dock_top = screen.height() * (1 - DOCK_FRACTION)
        rects = screen.controls()
        for name, rect in rects.items():
            with self.subTest(name):
                self.assertTrue(screen.rect().contains(rect), (rect, screen.rect()))
                self.assertLessEqual(rect.bottom(), dock_top)
        names = list(rects)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                self.assertFalse(rects[a].intersects(rects[b]), (a, b))
        cover = screen.cover_rect()
        self.assertTrue(screen.rect().contains(cover))
        self.assertLessEqual(cover.bottom(), dock_top)


class LayoutTests(ScreenCase):
    def test_views_lay_out_their_controls(self):
        screen = self.make(view="cover")
        self.assertEqual(set(screen.controls()), TOP_BAR | {"up_next"})
        self.assert_laid_out(screen)
        screen.set_view("album")
        rows = {f"track_{i}" for i in range(len(ALBUM))}
        self.assertEqual(set(screen.controls()), TOP_BAR | rows)
        self.assert_laid_out(screen)
        screen.set_view("stage")
        self.assertEqual(set(screen.controls()), TOP_BAR)
        self.assert_laid_out(screen)
        # A single song (or a radio channel): no list, so no Up next and no Album to switch to.
        screen.set_tracks([], -1)
        for view in ("cover", "stage"):
            screen.set_view(view)
            self.assertEqual(set(screen.controls()), TOP_BAR - {"view_album"})

    def test_a_wide_window_keeps_everything_above_the_dock(self):
        for view in ("cover", "album", "stage"):
            with self.subTest(view):
                screen = self.make((1280, 540), view=view)
                self.assert_laid_out(screen)
        small = self.make((640, 360), view="album")
        self.assert_laid_out(small)

    def test_the_album_view_without_a_list_draws_the_cover_view(self):
        screen = self.make(view="album", tracks=[], current=-1)
        self.assertEqual(screen.view(), "album")  # as set (a remembered pick stays)
        self.assertEqual(screen.shown_view(), "cover")
        screen.set_tracks(ALBUM, 0)
        self.assertEqual(screen.shown_view(), "album")

    def test_it_covers_its_page_and_follows_its_size(self):
        screen = self.make()
        page = screen.parentWidget()
        self.assertEqual(screen.geometry(), page.rect())
        page.resize(1000, 600)
        self.assertEqual(screen.geometry(), QRect(0, 0, 1000, 600))

    def test_only_its_controls_claim_a_picture_in_picture_hole(self):
        screen = self.make(view="album")
        claims = screen.hole_claims()
        for rect in screen.controls().values():
            self.assertTrue(claims.contains(rect.center()))
        self.assertFalse(claims.contains(QPoint(screen.width() // 2, screen.height() - 20)))
        self.assertIsInstance(claims, QRegion)


class SwitchAndRowTests(ScreenCase):
    def test_switch_emits_view_picked(self):
        screen = self.make(view="cover")
        picked: list[str] = []
        screen.viewPicked.connect(picked.append)
        self.click(screen, "view_stage")
        self.assertEqual(picked, ["stage"])
        self.assertEqual(screen.view(), "stage")
        self.click(screen, "view_album")
        self.assertEqual(picked, ["stage", "album"])
        self.assertEqual(screen.shown_view(), "album")

    def test_a_view_set_by_the_controller_is_no_pick(self):
        screen = self.make(view="cover")
        picked: list[str] = []
        screen.viewPicked.connect(picked.append)
        for view in ("album", "stage", "cover"):
            screen.set_view(view)
            self.assertEqual(screen.view(), view)
        self.assertEqual(picked, [])
        with self.assertRaises(ValueError):
            screen.set_view("poster")

    def test_a_track_row_click_emits_its_index(self):
        screen = self.make(view="album")
        picked: list[int] = []
        screen.trackPicked.connect(picked.append)
        self.click(screen, "track_4")
        self.click(screen, "track_0")
        self.assertEqual(picked, [4, 0])

    def test_back_words_and_up_next_emit_their_requests(self):
        screen = self.make(view="cover")
        heard: list[str] = []
        screen.backRequested.connect(lambda: heard.append("back"))
        screen.wordsRequested.connect(lambda: heard.append("words"))
        screen.upNextRequested.connect(lambda: heard.append("up_next"))
        for name in ("back", "words", "up_next"):
            self.click(screen, name)
        self.assertEqual(heard, ["back", "words", "up_next"])

    def test_a_long_album_keeps_the_playing_row_in_view_and_scrolls(self):
        many = [track(i, f"Song {i + 1}", 180_000) for i in range(40)]
        screen = self.make(view="album", tracks=many, current=30)
        names = set(screen.controls())
        self.assertIn("track_30", names)
        self.assertNotIn("track_0", names)
        self.assert_laid_out(screen)
        row = screen.controls()["track_30"].center()
        for _ in range(40):  # up the list, a notch at a time
            wheel(screen, row, 120)
        self.assertIn("track_0", screen.controls())
        picked: list[int] = []
        screen.trackPicked.connect(picked.append)
        self.click(screen, "track_0")
        self.assertEqual(picked, [0])


def wheel(widget: QWidget, pos: QPoint, dy: int) -> bool:
    """A wheel notch over `widget` at `pos`; whether the widget took it."""
    event = QWheelEvent(
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, dy),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    event.ignore()
    QApplication.sendEvent(widget, event)
    return event.isAccepted()


class PaintTests(ScreenCase):
    def test_no_cover_draws_the_stand_in(self):
        painted = Painted(self)
        screen = self.make(view="cover")
        screen.set_cover(None, BRAND_COLOURS)
        image = screen.grab().toImage()
        label = i18n.upper("Harbour Lights")  # the album's, as the mockup's cover label
        self.assertIn(label, painted.texts)
        cover = screen.cover_rect()
        warm = QColor(image.pixel(cover.center().x(), cover.top() + cover.height() // 5))
        self.assertGreater(warm.red(), warm.blue() + 60)
        # A cover of its own: drawn instead, with no label.
        painted.texts.clear()
        screen.set_cover(solid((30, 60, 220)), [(30, 60, 220)])
        image = screen.grab().toImage()
        blue = QColor(image.pixel(cover.center()))
        self.assertGreater(blue.blue(), 180)
        self.assertLess(blue.red(), 60)
        self.assertNotIn(label, painted.texts)

    def test_the_small_stage_cover_has_no_label_on_a_hidpi_screen_either(self):
        # The mockup's Stage cover is bare: too small for the label, at any pixel ratio.
        painted = Painted(self)
        screen = self.make(view="stage")
        label = i18n.upper("Harbour Lights")
        for ratio in (1.0, 2.0):
            with (
                self.subTest(ratio=ratio),
                mock.patch.object(screen, "devicePixelRatioF", return_value=ratio),
            ):
                painted.texts.clear()
                screen.set_cover(None, BRAND_COLOURS)  # drawn again, at this ratio
                screen.grab()
                self.assertNotIn(label, painted.texts)
        screen.set_view("cover")  # the large cover has it
        with mock.patch.object(screen, "devicePixelRatioF", return_value=2.0):
            painted.texts.clear()
            screen.grab()
            self.assertIn(label, painted.texts)

    def test_a_logo_is_fitted_on_a_tile_not_cropped(self):
        screen = self.make(view="cover", tracks=[], current=-1)
        logo = QImage(300, 100, QImage.Format.Format_ARGB32)
        logo.fill(QColor(0, 0, 0, 0))
        painter = QPainter(logo)
        painter.fillRect(QRect(40, 20, 220, 60), QColor(220, 40, 40))
        painter.end()
        screen.set_cover(logo, [(220, 40, 40)])
        image = screen.grab().toImage()
        cover = screen.cover_rect()
        centre = QColor(image.pixel(cover.center()))
        corner = QColor(image.pixel(cover.left() + cover.width() // 10, cover.top() + 8))
        self.assertGreater(centre.red(), 150)  # the logo, in the middle
        self.assertLess(corner.red(), 60)  # the tile around it, not the logo stretched over it

    def test_stage_lines_and_bars_render(self):
        painted = Painted(self)
        screen = self.make(view="stage")
        screen.set_cover(None, [GREEN])
        height = screen.height()
        bare = screen.grab().toImage()
        lines = ("Kıyıda kimse kalmadı", "Gece vapurları geçiyor", "Die Nachtfähren ziehen vorbei")
        screen.show_lines(*lines)
        worded = screen.grab().toImage()
        for line in lines:
            self.assertIn(line, painted.texts)
        box = diff_box(bare, worded)
        self.assertFalse(box.isEmpty())
        self.assertGreater(box.top(), height * 0.2)  # under the title, around 40% of the page
        self.assertLess(box.bottom(), height * 0.6)
        screen.set_levels([1.0] * 60)
        barred = screen.grab().toImage()
        box = diff_box(worded, barred)
        self.assertFalse(box.isEmpty())
        self.assertGreater(box.top(), height * 0.45)  # the lower part, above the dock
        self.assertLessEqual(box.bottom(), height * (1 - DOCK_FRACTION))
        y = box.center().y()
        greenest = max(
            (QColor(barred.pixel(x, y)) for x in range(screen.width())), key=QColor.green
        )
        self.assertGreater(greenest.green(), 140)  # the bars are the cover's colour
        self.assertGreater(greenest.green(), greenest.red() + 60)
        screen.set_levels(None)  # no bars (a radio channel)
        self.assertTrue(diff_box(worded, screen.grab().toImage()).isEmpty())
        screen.clear_lines()
        self.assertTrue(diff_box(bare, screen.grab().toImage()).isEmpty())

    def test_one_two_or_three_colours_light_the_screen(self):
        screen = self.make(view="stage")
        screen.set_levels([0.2 + (i % 7) / 10 for i in range(60)])
        for colours in (
            [(220, 50, 50)],
            [(220, 50, 50), (50, 50, 220)],
            [(220, 50, 50), (50, 200, 50), (50, 50, 220)],
        ):
            with self.subTest(len(colours)):
                screen.set_cover(None, colours)
                image = screen.grab().toImage()
                glow = QColor(image.pixel(screen.width() // 2, screen.height() // 4))
                self.assertGreater(glow.red(), glow.green())  # lit by the first colour
        canvas = QColor("#0b0f14")
        for view in ("cover", "album"):
            screen.set_view(view)
            image = screen.grab().toImage()
            self.assertNotEqual(QColor(image.pixel(screen.width() // 4, 40)), canvas)

    def test_a_name_is_capitalised_as_its_own_language_writes_it(self):
        # QiTV's own words in the app's language; an album's name in its own letters.
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        painted = Painted(self)
        screen = self.make(view="album")
        screen.grab()
        self.assertIn("HARBOUR LIGHTS · 8 PARÇA", painted.texts)  # never "LİGHTS"
        self.assertIn("HARBOUR LIGHTS", painted.texts)  # the stand-in's label
        self.assertEqual(name_upper("Bir İz Bıraktım"), "BİR İZ BIRAKTIM")  # in any language
        self.assertEqual(name_upper("Gece Işıkları"), "GECE IŞIKLARI")
        self.assertEqual(name_upper("Straße der Lichter"), "STRASSE DER LICHTER")

    def test_the_top_bar_reads_its_states(self):
        painted = Painted(self)
        screen = self.make(view="cover")
        screen.grab()
        self.assertIn("Words", painted.texts)
        self.assertIn("Up next · 7", painted.texts)  # the album's other tracks
        painted.texts.clear()
        screen.set_words(True, "Deutsch")
        screen.grab()
        self.assertIn("Words · Deutsch", painted.texts)
        painted.texts.clear()
        screen.set_view("album")
        screen.grab()
        self.assertIn(i18n.upper("Harbour Lights · 8 tracks"), painted.texts)
        self.assertIn("Mira Sol · Harbour Lights · 2024", painted.texts)
        self.assertIn("4:02", painted.texts)


class WordsTests(ScreenCase):
    def test_the_stage_says_when_it_takes_the_words(self):
        # Plan 4: while the Stage shows, the words are its own and the captions over its page
        # give way (CaptionsController asks takes_words(); stageShown tells it at once). Cover,
        # Album, the corner and a hidden screen leave them to the captions.
        screen = self.make(view="cover")
        said: list[bool] = []
        screen.stageShown.connect(said.append)
        self.assertFalse(screen.takes_words())
        screen.set_view("stage")
        self.assertTrue(screen.takes_words())
        screen.set_compact(True)  # the Library's corner: its cover and title
        self.assertFalse(screen.takes_words())
        screen.set_compact(False)
        screen.hide()  # a film opens
        self.assertFalse(screen.takes_words())
        screen.show()
        screen.set_view("album")
        self.assertEqual(said, [True, False, True, False, True, False])
        screen.hide()
        screen.set_view("stage")  # hidden: nothing taken
        self.assertEqual(said, [True, False, True, False, True, False])
        screen.show()
        self.assertEqual(said[-1], True)
        screen.show_lines("", "", "")  # its words change nothing of it
        self.assertEqual(len(said), 7)


class CaptionsRoomTests(ScreenCase):
    """Ruling P4-10: with Words on, Cover and Album lift the cover and its title so that two
    lines of the captions over the picture fit between them and the dock; Words off, the layout
    is as it was."""

    LINE = "The night ferries pass by in silence, the harbour lights go out."  # two lines

    def scene(self, size, view, tracks=ALBUM):
        """The screen, the captions over the picture and the audio dock, as the window stacks
        them (the captions follow the dock; the screen leaves them room)."""
        page = QWidget()
        page.resize(*size)
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.close)
        screen = AudioScreen(page, clock=lambda: self.now[0])
        captions = CaptionsOverlay(page)
        transport = Transport(page)
        screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights", year="2024")
        screen.set_cover(None, BRAND_COLOURS)
        screen.set_tracks(tracks, 1)
        screen.set_view(view)
        page.show()
        screen.show()
        transport.show_audio("Ferries at Night", "Mira Sol · Harbour Lights")
        transport.set_audio_state(False, "off", True)
        captions.follow(transport)
        screen.follow(transport)
        screen.leave_room_for(captions)
        return screen, captions

    @staticmethod
    def info(screen: AudioScreen) -> QRectF:
        lay = screen._layout()
        return lay.title.united(lay.subtitle)

    def test_with_words_on_two_caption_lines_never_cover_the_title(self):
        for size in ((1280, 720), (640, 360)):
            for view in ("cover", "album"):
                with self.subTest(size=size, view=view):
                    screen, captions = self.scene(size, view)
                    captions.show_text(self.LINE)
                    self.assertEqual(len(captions.lines()), 2)
                    box = QRectF(captions.geometry())
                    if size == (1280, 720):  # as the shot showed: over the artist · album line
                        self.assertTrue(box.intersects(self.info(screen)))
                    screen.set_words(True, "English")
                    self.assertFalse(box.intersects(self.info(screen)), (box, self.info(screen)))
                    self.assertTrue(screen.rect().contains(screen.cover_rect()))
                    self.assert_laid_out(screen)
                    # It holds still as captions come and go: Words decide, not a caption.
                    laid = screen._lay_out()
                    captions.clear()
                    self.assertEqual(screen._lay_out(), laid)
                    captions.show_text("Evet.")
                    self.assertEqual(screen._lay_out(), laid)

    def test_with_words_on_the_albums_list_ends_above_the_captions(self):
        # No row under a caption either; a long list still scrolls.
        long = [track(i, f"Song {i + 1}", 200_000) for i in range(30)]
        for size in ((1280, 720), (640, 360)):
            with self.subTest(size=size):
                screen, captions = self.scene(size, "album", long)
                captions.show_text(self.LINE)
                box = QRectF(captions.geometry())
                screen.set_words(True, "English")
                lay = screen._layout()
                self.assertFalse(box.intersects(lay.rows[-1][1]), (box, lay.rows[-1][1]))
                self.assertFalse(box.intersects(lay.list_area))
                self.assertTrue(lay.more_below)  # the rest is a scroll away
                self.assertTrue(wheel(screen, lay.rows[0][1].center().toPoint(), -120))
                self.assertGreater(screen._layout().first, 0)

    def test_with_words_off_the_layout_is_as_before(self):
        for size in ((1280, 720), (640, 360)):
            for view in ("cover", "album"):
                with self.subTest(size=size, view=view):
                    screen, _captions = self.scene(size, view)
                    plain = self.make(size, view=view)
                    self.assertEqual(screen._lay_out(), plain._lay_out())
                    screen.set_words(True, "English")
                    self.assertNotEqual(screen._lay_out().cover, plain._lay_out().cover)
                    screen.set_words(False, "")
                    self.assertEqual(screen._lay_out(), plain._lay_out())


class FollowAndCornerTests(ScreenCase):
    def test_the_top_bar_steps_below_the_live_controls_while_they_show(self):
        # Task 5, review item 9: a radio channel keeps live TV's controls, whose title row and
        # clock sit where the top bar is: while they show, the bar sits below them.
        from ui.overlays.transport import Transport

        screen = self.make(view="cover", tracks=[], current=-1)
        page = screen.parentWidget()
        transport = Transport(page)
        screen.follow(transport)
        top = screen.controls()["back"].top()
        cover = screen.cover_rect()
        transport.show_live("Night Ferry FM", "The Long Pier")
        self.assertTrue(transport.isVisible())
        band = transport.top_band_bottom()
        self.assertGreater(band, top)
        self.assertEqual(screen.cover_rect(), cover)  # review M6: only the bar moves
        rows = [r.toAlignedRect() for r in transport.title_rects().values() if not r.isEmpty()]
        for name, rect in screen.controls().items():
            with self.subTest(name):
                self.assertGreaterEqual(rect.top(), band)
                self.assertFalse(any(rect.intersects(row) for row in rows))
        self.assert_laid_out(screen)
        transport.dismiss()  # they fade: the bar goes back up
        self.assertEqual(screen.controls()["back"].top(), top)
        self.assertEqual(screen.cover_rect(), cover)
        transport.show_audio("Ferries at Night", "")  # a song's dock has no title row
        self.assertEqual(transport.top_band_bottom(), 0)
        self.assertEqual(screen.controls()["back"].top(), top)

    def test_in_the_corner_it_shows_the_cover_and_the_title_only(self):
        # Task 5, review item 10: the Library's corner player keeps the song's screen, small: its
        # cover and title, no top bar and no room left for a dock.
        painted = Painted(self)
        screen = self.make((426, 240), view="album")
        screen.set_compact(True)
        self.assertTrue(screen.compact())
        self.assertEqual(screen.controls(), {})
        self.assertEqual(screen.shown_view(), "cover")
        cover = screen.cover_rect()
        self.assertTrue(screen.rect().contains(cover))
        # No dock under it: the cover sits lower than over the dock's room.
        over_the_dock = (screen.height() * (1 - DOCK_FRACTION) - cover.height()) / 2
        self.assertGreater(cover.top(), over_the_dock)
        screen.grab()
        self.assertIn("Ferries at Night", painted.texts)
        self.assertNotIn("Mira Sol · Harbour Lights", painted.texts)
        self.assertFalse(screen.hole_claims().contains(cover.center()))
        screen.set_compact(False)
        self.assertEqual(screen.shown_view(), "album")
        self.assertIn("back", screen.controls())


class MotionTests(ScreenCase):
    def test_the_glow_waits_while_its_window_is_not_exposed(self):
        # Review item 11: a window shown but fully covered (macOS reports it not exposed) has
        # no breath to show: the light's timer waits until it is exposed again.
        screen = self.make(view="cover")
        screen.set_playing(True)
        self.assertTrue(screen.animating())
        with mock.patch.object(AudioScreen, "_exposed", return_value=False):
            screen._glow_tick()
            self.assertFalse(screen.animating())
        screen._window_exposed()  # the window's Expose
        self.assertTrue(screen.animating())

    def test_the_glow_breathes_only_while_shown_and_playing(self):
        screen = self.make(view="cover")
        self.assertFalse(screen.animating())  # nothing plays yet
        screen.set_playing(True)
        self.assertTrue(screen.animating())
        screen.hide()
        self.assertFalse(screen.animating())
        screen.show()
        self.assertTrue(screen.animating())
        screen.set_playing(False)  # paused: it holds still
        self.assertFalse(screen.animating())

    def test_the_glow_changes_as_it_breathes(self):
        screen = self.make(view="cover")
        screen.set_playing(True)
        first = screen.grab().toImage()
        self.now[0] += 3.5  # half a 7 s breath: brighter and larger
        elapse(250)
        later = screen.grab().toImage()
        self.assertFalse(diff_box(first, later).isEmpty())

    def test_reduced_motion_holds_it_still(self):
        pin_motion(self, reduced=True)
        screen = self.make(view="album")
        screen.set_playing(True)
        self.assertFalse(screen.animating())
        first = screen.grab().toImage()
        self.now[0] += 3.5
        elapse(250)
        self.assertTrue(diff_box(first, screen.grab().toImage()).isEmpty())


class HostTests(ScreenCase):
    def make_hosted(self, view="album"):
        window = QWidget()
        window.resize(1280, 720)
        host = OverlayHost(window, window)
        host.resize(1280, 720)
        screen = AudioScreen(clock=lambda: self.now[0])
        host.add_overlay(screen)
        screen.show_song("Ferries at Night", "Mira Sol", "Harbour Lights", year="2024")
        screen.set_cover(None, BRAND_COLOURS)
        screen.set_tracks(ALBUM, 1)
        screen.set_view(view)
        window.show()
        self.addCleanup(window.close)
        self.addCleanup(host.close)
        screen.show()
        elapse(SETTLE_CHECK_MS + 50)  # the host's look after the window showed is through
        return host, screen

    def press(self, host, pos, double=False):
        # Through the host's window, so Qt hit-tests to the child under the point.
        if double:
            QTest.mouseDClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, pos)
        else:
            QTest.mouseClick(host.windowHandle(), Qt.LeftButton, Qt.NoModifier, pos)
        elapse(QApplication.doubleClickInterval() + 150)

    def test_unhandled_clicks_reach_the_host(self):
        host, screen = self.make_hosted()
        self.assertEqual(screen.geometry(), host.rect())
        clicks, doubles = [], []
        host.clicked.connect(lambda: clicks.append(1))
        host.doubleClicked.connect(lambda: doubles.append(1))
        empty = QPoint(screen.width() // 5, screen.height() - 40)
        self.assertIs(host.childAt(empty), screen)
        self.press(host, empty)
        self.assertEqual(clicks, [1])  # play/pause stays a click on the page
        self.press(host, empty, double=True)
        self.assertEqual(doubles, [1])  # and fullscreen a double-click
        # The volume is the host's: a wheel the screen leaves goes on to it (a real one does;
        # Qt 6 never passes a synthesized wheel to the parent, so its ignore is what shows).
        self.assertFalse(wheel(screen, empty, 120))
        # Its own controls keep their clicks.
        back = []
        screen.backRequested.connect(lambda: back.append(1))
        self.press(host, screen.controls()["back"].center())
        self.assertEqual(back, [1])
        self.press(host, screen.controls()["track_3"].center(), double=True)
        self.assertEqual((clicks, doubles), ([1], [1]))

    def test_a_short_list_leaves_the_wheel_to_the_host(self):
        _host, screen = self.make_hosted()
        self.assertFalse(wheel(screen, screen.controls()["track_2"].center(), 120))


if __name__ == "__main__":
    unittest.main()
