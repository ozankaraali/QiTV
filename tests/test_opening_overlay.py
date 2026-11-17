"""The mark that draws itself on the player page while a file opened in QiTV opens
(ui/overlays/opening.py): it shows from the router (TVWindow.open_paths) as the file is played,
unless a picture shows already, and opens out of the page once the file's picture shows
(PlaybackSession.pictureShown), a song plays, the file fails, another title is chosen, Stop is
pressed, or after 20 s. It takes no click, key or focus. Fictional files; the controller's
play_file is the router tests' fake, and the session's picture is said by the test."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtWidgets import QApplication
from timer_fixtures import elapse, wait_until

from services.playback import NullSession, PlaybackSession, PlaybackState
from services.vlc_profile import PlayKind
from tests.test_open_router import SETTLE_MS, RouterCase
from tests.test_playback_session import _FakeInstance, _HeldRunner, _ShapePlayer, vlc
from ui.link_lanes import Target
from ui.opening_mark import MARK_PX, STAGE_PX, OpeningMark
from ui.overlays.opening import OPENING_TIMEOUT_MS, OpeningOverlay

FILM_URL = "http://provider.test/film.mp4"
FAILED = "The stream could not be opened"


class OverlayCase(RouterCase):
    """The router over a fake play_file, the large picture's session saying what the test
    says: whether a picture shows (`picture`), and its address: the fake's file, or what the
    test sets (`address`)."""

    def setUp(self):
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
