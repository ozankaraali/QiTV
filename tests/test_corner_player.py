import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from timer_fixtures import elapse, wait_until

from tests.http_fixtures import LocalSource
from tests.test_tv_window import INSTANCE, WindowCase
from tests.ts_fixtures import make_stream
from ui.overlays.corner_controls import BUTTONS, MIN_HIT_PX
from ui.page_stack import PageStack


class NothingPlayingTests(WindowCase):
    def test_with_nothing_playing_the_library_has_the_whole_window(self):
        self.show()
        self.window.screens.open_library("movies")
        self.assertEqual(self.window.pages.mode(), PageStack.LIBRARY)
        self.assertFalse(self.window.player_page.isVisible())
        self.assertFalse(self.window.corner_controls.isVisible())


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class CornerPlayerTests(WindowCase):
    """The player page in a corner of the Library while a channel and the PiP play on."""

    vlc_instance = INSTANCE

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.payload = make_stream(60)  # 60 s of H.264 and AAC in MPEG-TS

    def setUp(self):
        stop, payload = threading.Event(), self.payload

        def paced(request):  # a live-looking stream, a few times real time
            try:
                for offset in range(0, len(payload), 188 * 20):
                    if stop.wait(0.02):
                        return
                    request.wfile.write(payload[offset : offset + 188 * 20])
            except OSError:
                pass

        route = lambda _request, _count: (200, {"Content-Type": "video/mp2t"}, paced)  # noqa: E731
        self.source = LocalSource({"/live/1.ts": route, "/live/2.ts": route})
        self.addCleanup(self.source.close)  # after the window closed (cleanups run last first)
        self.addCleanup(stop.set)
        urls = [f"{self.source.url}/live/1.ts", f"{self.source.url}/live/2.ts"]
        patcher = mock.patch("tests.fixtures.local_provider.channel_urls", return_value=urls)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.stops: list[str] = []
        for name, session in (("main", self.window.session), ("pip", self.window.pip_session)):
            stop_session = session.stop

            def counted(name=name, stop_session=stop_session):
                self.stops.append(name)
                return stop_session()

            patcher = mock.patch.object(session, "stop", side_effect=counted)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ---- helpers ------------------------------------------------------------------------

    def play_channel(self):
        self.open_first_channel()  # Channel 1
        window = self.window
        self.assertTrue(wait_until(window.live_session.is_pipeline))

    def open_pip(self):
        """Channel 2 large, Channel 1 small."""
        window = self.window
        QTest.keyClick(window, Qt.Key.Key_Down)
        self.assertTrue(wait_until(lambda: window.zap.context.current()["name"] == "Channel 2"))
        QTest.keyClick(window, Qt.Key.Key_P)
        self.assertTrue(wait_until(window.sessions.pip_open))

    def click(self, name):
        """A mouse click on a corner control, through the host's window."""
        controls, host = self.window.corner_controls, self.window.host
        point = controls.mapTo(
            host, self.window.corner_controls.button_rect(name).center().toPoint()
        )
        QTest.mouseClick(host.windowHandle(), Qt.MouseButton.LeftButton, pos=point)

    # ---- tests --------------------------------------------------------------------------

    def test_opening_the_library_keeps_playing(self):
        self.play_channel()
        self.open_pip()
        window = self.window
        self.stops.clear()  # starting a channel stops first
        window.screens.open_library("movies")
        self.assertEqual(window.pages.mode(), PageStack.CORNER)
        self.assertEqual(self.stops, [])
        self.assertTrue(window.sessions.pip_open())
        self.assertTrue(window.library_page.isVisible())
        self.assertTrue(window.player_page.isVisible())
        self.assertTrue(wait_until(window.corner_controls.isVisible))
        self.click("back")
        self.assertEqual(window.pages.mode(), PageStack.PLAYER)
        self.assertEqual(self.stops, [])
        self.assertTrue(window.sessions.pip_open())
        self.assertFalse(window.library_page.isVisible())

    def test_only_the_corner_controls_stay_on_the_host(self):
        self.play_channel()
        self.open_pip()
        window = self.window
        window.controller.mouse_moved()  # the transport shows
        window.screens.notify("A message")
        window.screens.open_library("movies")
        self.assertTrue(wait_until(window.corner_controls.isVisible))
        self.assertEqual(window.host.visible_overlays(), [window.corner_controls])
        window.controller.mouse_moved()  # asks for the transport: it stays away
        window.screens.notify("Another")
        self.assertEqual(window.host.visible_overlays(), [window.corner_controls])
        window.screens.back_to_player()
        self.assertNotIn(window.corner_controls, window.host.visible_overlays())

    def test_the_drawer_and_the_guide_close_and_nothing_stops(self):
        self.play_channel()
        window = self.window
        window.overlays.drawer.open_drawer()
        self.assertTrue(window.overlays.drawer.isVisible())
        self.stops.clear()
        window.screens.open_library("movies")
        self.assertFalse(window.overlays.drawer.isVisible())
        self.assertFalse(window.controller.overlay_open())
        self.assertEqual(self.stops, [])

    def test_the_controls_are_big_enough_to_click_at_the_default_window(self):
        self.play_channel()
        self.window.resize(1280, 720)
        self.window.screens.open_library("movies")
        controls = self.window.corner_controls
        self.assertTrue(wait_until(controls.isVisible))
        for name in BUTTONS:
            rect = controls.button_rect(name)
            self.assertGreaterEqual(rect.width(), MIN_HIT_PX, name)
            self.assertGreaterEqual(rect.height(), MIN_HIT_PX, name)
            self.assertTrue(self.window.host.mask().contains(rect.center().toPoint()), name)

    def test_the_stop_button_stops_and_the_library_stays(self):
        self.play_channel()
        window = self.window
        window.screens.open_library("movies")
        self.assertTrue(wait_until(window.corner_controls.isVisible))
        self.stops.clear()
        self.click("stop")
        self.assertEqual(window.pages.mode(), PageStack.LIBRARY)
        self.assertIn("main", self.stops)
        self.assertFalse(window.player_page.isVisible())
        self.assertTrue(window.library_page.isVisible())
        self.assertFalse(window.corner_controls.isVisible())

    def test_the_sound_button_mutes_what_plays(self):
        self.play_channel()
        window = self.window
        window.screens.open_library("movies")
        self.assertTrue(wait_until(window.corner_controls.isVisible))
        self.assertFalse(window.controller.main_muted())
        self.click("sound")
        self.assertTrue(window.controller.main_muted())
        self.click("sound")
        self.assertFalse(window.controller.main_muted())

    def test_a_click_on_the_corner_player_and_the_live_tv_row_bring_it_back(self):
        self.play_channel()
        window = self.window
        window.screens.open_library("movies")
        window.pages.cornerClicked.emit()  # what a press released on the video emits
        self.assertEqual(window.pages.mode(), PageStack.PLAYER)
        window.screens.open_library("movies")
        window.screens.back_to_tv()  # Esc at the Library's top level, or the Live TV tab
        self.assertEqual(window.pages.mode(), PageStack.PLAYER)
        self.assertEqual(self.stops.count("pip"), 0)

    def test_a_title_from_the_library_leaves_the_corner(self):
        self.play_channel()
        window = self.window
        window.screens.open_library("movies")
        movie = {"id": "m1", "name": "Quiet Pier", "cmd": f"{self.source.url}/live/2.ts"}
        with mock.patch.object(window.controller, "play_vod") as play:
            window.screens.play(movie, "movie", {})
        play.assert_called_once()
        self.assertEqual(window.pages.mode(), PageStack.PLAYER)

    def test_from_the_mini_player_and_fullscreen(self):  # Review Focus 5
        self.play_channel()
        window = self.window
        window.toggle_mini_player()  # into the mini player
        window.screens.open_library("movies")
        self.assertFalse(window.mini_player_active())
        window.showFullScreen()
        elapse(100)
        window.screens.open_library("movies")
        elapse(100)
        self.assertEqual(window.pages.mode(), PageStack.CORNER)
        self.assertEqual(window.pages.corner_rect().width(), round(window.pages.width() / 3))
        self.assertEqual(window.player_page.geometry(), window.pages.corner_rect())


if __name__ == "__main__":
    unittest.main()
