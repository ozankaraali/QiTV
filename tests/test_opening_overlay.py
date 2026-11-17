"""The mark that draws itself on the player page while a file opened in QiTV opens
(ui/overlays/opening.py): it shows from the router (TVWindow.open_paths) as the file is played,
unless a picture shows already, and opens out of the page once the file's picture shows
(PlaybackSession.pictureShown), a song plays, the file fails, another title is chosen, Stop is
pressed, or after 20 s. It takes no click, key or focus. Fictional files; the controller's
play_file is the router tests' fake, and the session's picture is said by the test."""

import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QRectF, QSize, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from motion_fixtures import pin_motion
from timer_fixtures import elapse, wait_until

from services.playback import NullSession, PlaybackSession, PlaybackState
from services.vlc_profile import PlayKind
from tests import test_tv_window as base
from tests.test_open_router import FILM, SETTLE_MS, RouterCase
from tests.test_playback_session import _FakeInstance, _HeldRunner, _ShapePlayer, vlc
from ui.link_lanes import Target
from ui.opening_mark import MARK_PX, STAGE_PX, STILL_T, OpeningMark
from ui.overlays.opening import OPENING_TIMEOUT_MS, OpeningOverlay

FILM_URL = "http://provider.test/film.mp4"
FAILED = "The stream could not be opened"


class OverlayCase(RouterCase):
    """The router over a fake play_file, the large picture's session saying what the test
    says: whether a picture shows (`picture`), and its address: the fake's file, or what the
    test sets (`address`)."""

    def setUp(self):
        pin_motion(self)  # as the system asks would differ by runner (a CI Mac asks for less)
        super().setUp()
        self.picture = False
        self.address = None
        self.session = self.window.controller.session
        self.overlay = self.window.opening_overlay

        url = mock.patch.object(NullSession, "url", new_callable=mock.PropertyMock)
        url.start().side_effect = self.url
        self.addCleanup(url.stop)
        picture = mock.patch.object(self.session, "shows_picture", side_effect=lambda: self.picture)
        picture.start()
        self.addCleanup(picture.stop)

    def url(self):
        if self.address is not None:
            return self.address
        return self.current.item["cmd"] if self.current is not None else None

    def open_file(self, name, count=1, source="handover"):
        """`name` opened (a later one by Open File: a hand-over's burst would leave it out)."""
        path = self.film(name)
        self.window.open_paths([str(path)], source=source)
        self.wait_for_plays(count)
        return path

    def shown(self):
        return not self.overlay.isHidden()

    def gone(self):
        """It opened out of the page (its finish takes at most 0.75 s), then hid."""
        return wait_until(lambda: self.overlay.isHidden())


class OpeningOverlayTests(OverlayCase):
    def test_overlay_until_first_picture(self):
        self.open_file("Film.mkv")
        self.assertTrue(self.shown())
        self.assertTrue(self.overlay.opening())
        self.session.pictureShown.emit()
        self.assertFalse(self.overlay.opening())
        self.assertTrue(self.shown())  # it opens out of the page first, no cut
        self.assertTrue(self.gone())

    def test_overlay_gone_on_error(self):
        # Review Focus 4: a film that fails to open while the mark shows
        self.open_file("Film.mkv")
        self.assertTrue(self.shown())
        self.session.errorOccurred.emit(FAILED)
        self.assertTrue(self.gone())
        self.assertIn((FAILED, "error"), self.toasts)

    def test_a_film_failing_as_it_is_played_shows_no_overlay(self):
        # The error comes inside play_file (NullSession's play does so): the mark never shows.
        controller = self.window.controller
        play = controller.play_file.side_effect

        def failing(path, size=None):
            play(path, size)
            self.session.errorOccurred.emit(FAILED)

        controller.play_file.side_effect = failing
        self.open_file("Film.mkv")
        elapse(SETTLE_MS)
        self.assertTrue(self.overlay.isHidden())
        self.assertIn((FAILED, "error"), self.toasts)

    def test_song_hides_on_playing(self):
        self.open_file("Şarkı.mp3")
        self.assertTrue(self.shown())
        self.session.stateChanged.emit("opening")
        self.assertTrue(self.overlay.opening())
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.gone())

    def test_a_film_that_plays_waits_for_its_picture(self):
        self.open_file("Film.mkv")
        self.session.stateChanged.emit("playing")
        elapse(SETTLE_MS)
        self.assertTrue(self.overlay.opening())

    def test_no_overlay_over_a_playing_picture(self):
        self.picture = True  # a film shows its picture
        self.open_file("Next.mkv")
        elapse(SETTLE_MS)
        self.assertTrue(self.overlay.isHidden())

    def test_a_film_opened_over_a_song_shows_it(self):
        self.open_file("Şarkı.flac")
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.gone())
        self.open_file("Film.mkv", 2, "dialog")  # the song shows no picture
        self.assertTrue(self.overlay.opening())

    def test_hidden_after_20s(self):
        self.assertEqual(OPENING_TIMEOUT_MS, 20_000)
        timeout = self.overlay._timeout
        self.assertEqual(timeout.interval(), OPENING_TIMEOUT_MS)
        timeout.setInterval(50)
        self.open_file("Film.mkv")
        self.assertTrue(self.shown())
        self.assertTrue(self.gone())

    def test_a_channel_chosen_hides_it(self):
        self.open_file("Film.mkv")
        channel = Target({"name": "Kanal 1", "cmd": "http://x/1"}, "live", "Kanal 1")
        self.window.controller._before_new_target(channel)
        self.assertFalse(self.overlay.opening())
        self.assertTrue(self.gone())

    def test_stop_hides_it(self):
        self.open_file("Film.mkv")
        self.window.controller._stop_all()
        self.assertTrue(self.gone())

    def test_another_file_keeps_the_mark_drawing(self):
        self.open_file("A.mkv")
        mark = self.overlay.mark
        started = mark.frame().t
        self.open_file("B.mkv", 2, "dialog")
        self.assertTrue(self.overlay.opening())
        self.assertGreaterEqual(mark.frame().t, started)  # the same loop, not a new one
        self.session.pictureShown.emit()  # B's picture
        self.assertTrue(self.gone())

    def test_a_late_picture_of_what_played_before_is_not_the_files(self):
        self.address = "http://x/1"  # the channel before it plays on until it lets go
        self.open_file("Film.mkv")
        self.session.pictureShown.emit()  # the channel's
        self.session.stateChanged.emit("playing")
        self.assertTrue(self.overlay.opening())
        self.address = None  # the file opens
        self.session.pictureShown.emit()
        self.assertTrue(self.gone())

    def test_a_picture_of_a_title_chosen_after_the_file_ends_it(self):
        self.open_file("Film.mkv")
        self.address = "file:///Recordings/News.ts"  # a recording from the Library plays
        self.session.pictureShown.emit()
        self.assertTrue(self.gone())

    def test_a_file_opened_as_it_opens_out_draws_again(self):
        self.open_file("A.mkv")
        self.session.errorOccurred.emit(FAILED)
        self.assertFalse(self.overlay.opening())
        self.open_file("B.mkv", 2, "dialog")
        self.assertTrue(self.overlay.opening())
        elapse(1000)  # A's finish is over: the mark draws again for B
        self.assertTrue(self.shown())
        self.assertTrue(self.overlay.opening())
        self.assertLess(self.overlay.mark.frame().t, 0.5)
        self.session.pictureShown.emit()
        self.assertTrue(self.gone())

    def test_under_reduced_motion_the_mark_holds_its_lit_frame(self):
        pin_motion(self, reduced=True)
        self.open_file("Film.mkv")
        self.assertTrue(self.shown())
        elapse(400)
        frame = self.overlay.mark.frame()
        self.assertEqual((frame.t, frame.still), (STILL_T, True))
        self.session.pictureShown.emit()
        self.assertTrue(self.gone())

    def test_a_file_that_cannot_be_opened_hides_it(self):
        self.open_file("Film.mkv")
        controller = self.window.controller

        def broken():
            raise OSError("no such device")

        with (
            mock.patch.object(controller, "_restore_channel"),
            self.assertLogs("ui.tv_controller", "WARNING"),
        ):
            controller._guarded(self.current, broken)()
        self.assertTrue(self.gone())

    def test_a_file_that_ends_before_its_picture_hides_it(self):
        self.open_file("Film.mkv")
        self.session.ended.emit()
        self.assertTrue(self.gone())

    def test_a_stop_or_a_channel_with_no_mark_up_does_nothing(self):
        with mock.patch.object(self.overlay.mark, "finish") as finish:
            self.window.controller._stop_all()
            self.window.controller._before_new_target(None)
        finish.assert_not_called()
        self.assertTrue(self.overlay.isHidden())

    def test_it_takes_no_input_and_stays_centred(self):
        self.show()
        self.open_file("Film.mkv")
        overlay, host = self.overlay, self.window.host
        for widget in (overlay, overlay.mark):
            self.assertTrue(widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
            self.assertEqual(widget.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(overlay.size(), QSize(STAGE_PX, STAGE_PX))
        self.assertEqual(overlay.mark.geometry(), overlay.rect())
        self.assertTrue(wait_until(host.isVisible))

        def centred():
            offset = QRectF(overlay.geometry()).center() - QRectF(host.rect()).center()
            return offset.manhattanLength() <= 1

        self.assertTrue(centred())
        size = host.size()
        self.window.resize(self.window.width() + 201, self.window.height() + 121)
        self.assertTrue(wait_until(lambda: host.size() != size))
        self.assertTrue(centred())
        self.assertIsNot(QApplication.focusWidget(), overlay)
        self.assertIsNot(QApplication.focusWidget(), overlay.mark)
        clicks, doubles = [], []
        host.clicked.connect(lambda: clicks.append(True))
        host.doubleClicked.connect(lambda: doubles.append(True))
        middle = overlay.geometry().center()
        QTest.mouseClick(host, Qt.MouseButton.LeftButton, pos=middle)
        self.assertTrue(wait_until(lambda: clicks))  # on to the player, as on the black
        QTest.mouseDClick(host, Qt.MouseButton.LeftButton, pos=middle)
        self.assertTrue(wait_until(lambda: doubles))  # fullscreen, as over the picture


@unittest.skipIf(base.INSTANCE is None, "libVLC is not available")
class RealPlayerOpeningTests(base.WindowCase):
    """The mark over the real player (libVLC with a dummy picture and no sound), the router
    and the controller unfaked: it draws as the file is played and goes once its picture
    shows; a file opened over that picture opens with none."""

    vlc_instance = base.INSTANCE

    def setUp(self):
        pin_motion(self)
        super().setUp()

    def test_the_mark_draws_until_the_real_players_picture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        film = Path(directory.name) / "Film.mkv"
        shutil.copyfile(FILM, film)
        window, controller = self.window, self.window.controller
        overlay = window.opening_overlay
        self.show()
        session = controller.session
        pictures = []
        session.pictureShown.connect(lambda: pictures.append(session.url))
        with mock.patch.object(overlay, "open_for", wraps=overlay.open_for) as open_for:
            window.open_paths([str(film)], source="dialog")
            self.assertTrue(wait_until(lambda: open_for.called))
            self.assertTrue(overlay.opening() or pictures)  # (a quick picture closes it)
            self.assertTrue(wait_until(lambda: overlay.isHidden(), OPENING_TIMEOUT_MS // 2))
            self.assertEqual(pictures, [film.as_uri()])  # its picture closed it
            self.assertTrue(session.shows_picture())
            other = shutil.copyfile(FILM, film.with_name("Next.mkv"))
            window.open_paths([str(other)], source="dialog")
            self.assertTrue(wait_until(lambda: session.url == other.as_uri()))
            elapse(SETTLE_MS)
        self.assertEqual(open_for.call_count, 1)  # none over the first film's picture
        self.assertTrue(overlay.isHidden())


class _Session(QObject):
    """A session as the overlay listens to it."""

    pictureShown = Signal()
    stateChanged = Signal(str)
    url = None


class OverlayWidgetTests(unittest.TestCase):
    """The overlay on its own: its mark's size, and a close with nothing shown."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_mark_is_104_px(self):
        overlay = OpeningOverlay()
        self.addCleanup(overlay.deleteLater)
        self.assertIsInstance(overlay.mark, OpeningMark)
        self.assertEqual(MARK_PX, 104)
        self.assertEqual(overlay.mark._mark_px, MARK_PX)

    def test_a_close_before_any_open_runs_no_finish(self):
        overlay = OpeningOverlay()
        self.addCleanup(overlay.deleteLater)
        with mock.patch.object(overlay.mark, "finish") as finish:
            overlay.close_opening()
        finish.assert_not_called()

    def page(self):
        page = QWidget()
        page.resize(800, 600)
        self.addCleanup(page.deleteLater)
        overlay = OpeningOverlay(page, clock=lambda: 50.0)
        page.show()
        return overlay

    def test_while_the_splash_stands_there_its_mark_draws_unseen_on_its_loop(self):
        # ui/splash_helper.py: the splash's mark glides onto this one, then this one shows.
        overlay = self.page()
        overlay.loop_origin = lambda: 42.0  # the splash's loop origin
        overlay.hold_mark(True)
        overlay.open_for(_Session(), "file:///Film.mkv")
        self.assertTrue(overlay.opening())
        self.assertFalse(overlay.isHidden())
        self.assertEqual(overlay.mark.origin, 42.0)  # the same frame as the splash's
        self.assertTrue(overlay.mark.isHidden())
        overlay.hold_mark(False)
        self.assertFalse(overlay.mark.isHidden())

    def test_a_mark_after_a_still_is_held_lit_on_the_splashs_origin(self):
        # the splash came from the bootloader's still and never goes out: nor does this one
        overlay = self.page()
        overlay.loop_origin = lambda: 42.0
        overlay.loop_held = lambda: True
        overlay.open_for(_Session(), "file:///Film.mkv")
        self.assertEqual(overlay.mark.origin, 42.0)
        self.assertTrue(overlay.mark._held)
        frame = overlay.mark.frame(42.0 + 1000.0)
        self.assertGreaterEqual(frame.t, 0.56)
        self.assertLessEqual(frame.t, 0.88)
        plain = self.page()
        plain.open_for(_Session(), "file:///Film.mkv")  # no splash: the full loop
        self.assertFalse(plain.mark._held)

    def test_a_mark_let_go_as_it_opens_out_stays_unseen(self):
        overlay = self.page()
        overlay.hold_mark(True)
        overlay.open_for(_Session(), "file:///Film.mkv")
        self.assertEqual(overlay.mark.origin, 50.0)  # no splash loop: its own, from now
        overlay.close_opening()  # the picture showed during the glide
        overlay.hold_mark(False)
        self.assertTrue(overlay.mark.isHidden())  # its open-out is not seen
        overlay.open_for(_Session(), "file:///Film 2.mkv")  # the next file's shows
        self.assertFalse(overlay.mark.isHidden())


class _VoutPlayer(_ShapePlayer):
    """A player whose video output appears when the test says (`vout`)."""

    def __init__(self):
        super().__init__()
        self.vout = 0

    def has_vout(self):
        return self.vout


@unittest.skipIf(vlc is None, "python-vlc's states are needed to poll the player")
class PictureShownTests(unittest.TestCase):
    """PlaybackSession.pictureShown: once per open, as VLC's video output for the media
    appears, whatever the picture settings (none asked for here)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.player = _VoutPlayer()
        self.runner = _HeldRunner(self.player)
        self.session = PlaybackSession(
            _FakeInstance(self.player), runner=self.runner, poll_ms=60_000
        )
        self.addCleanup(self.session.release)
        self.shown = []
        self.session.pictureShown.connect(lambda: self.shown.append(self.session.url))

    def play(self, url):
        self.session.play(url, kind=PlayKind.VOD_FILE)
        while self.runner.jobs:
            self.runner.finish()
        self.player.vout = 0

    def tick(self, state=None):
        if state is not None:
            self.player.state = state
        self.session._tick()

    def test_shown_once_its_video_output_appears(self):
        self.play(FILM_URL)
        self.tick(vlc.State.Opening)
        self.tick(vlc.State.Playing)
        self.assertEqual(self.shown, [])
        self.assertFalse(self.session.shows_picture())
        self.player.vout = 1
        self.tick()
        self.assertEqual(self.shown, [FILM_URL])
        self.assertTrue(self.session.shows_picture())
        self.tick()
        self.session.set_aspect("16:9")  # a shape set while it plays: no second picture
        self.tick()
        self.assertEqual(self.shown, [FILM_URL])

    def test_each_open_shows_its_own_picture(self):
        self.play(FILM_URL)
        self.player.vout = 1
        self.tick(vlc.State.Playing)
        self.play("http://provider.test/next.mp4")
        self.assertFalse(self.session.shows_picture())  # black until its picture
        self.tick(vlc.State.Playing)
        self.player.vout = 1
        self.tick()
        self.assertEqual(self.shown, [FILM_URL, "http://provider.test/next.mp4"])

    def test_no_picture_once_it_stops_or_ends(self):
        self.play(FILM_URL)
        self.player.vout = 1
        self.tick(vlc.State.Playing)
        self.tick(vlc.State.Paused)
        self.assertTrue(self.session.shows_picture())  # paused on its picture
        self.session.stop()
        self.assertFalse(self.session.shows_picture())
        self.play(FILM_URL)
        self.player.vout = 1
        self.tick(vlc.State.Playing)
        self.tick(vlc.State.Ended)
        self.assertIs(self.session.state(), PlaybackState.ENDED)
        self.assertFalse(self.session.shows_picture())

    def test_without_libvlc_no_picture_shows(self):
        session = NullSession()
        self.assertFalse(session.shows_picture())
        session.pictureShown.emit()  # the signal is there, as on a PlaybackSession


if __name__ == "__main__":
    unittest.main()
