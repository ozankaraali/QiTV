"""Every file opened in QiTV goes through one router (TVWindow.open_paths): a dropped video
plays, a subtitle file dropped with it loads once it opens, a file that is gone says so, the
file that plays already only comes forward, and a multi-select's hand-overs play the first by
name. Fictional files in a temporary folder; the controller's play_file is a fake."""

import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from timer_fixtures import elapse, wait_until

from services.playback import PlaybackState
from tests import test_tv_window as base
from tests.test_provider_drop import PANEL, drop
from ui import tv_window
from ui.keymap import Action
from ui.link_lanes import Target

SETTLE_MS = 300  # a stat a wrong path would have started is back by then
FILM = Path(__file__).parent / "fixtures" / "film" / "h264_aac.mkv"  # 8 s
SUBTITLES = "1\n00:00:00,000 --> 00:00:08,000\nHallo\n"


class RouterCase(base.WindowCase):
    """The window's router, over a fake play_file: what it plays is recorded."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.calls: list[tuple] = []  # ("play", path, size) and ("subtitles", files), in order
        self.fronts = 0
        self.current = None  # the fake controller's local target
        self.state = PlaybackState.IDLE  # its player's state
        controller = self.window.controller

        def play_file(path, size=None):
            self.calls.append(("play", path, size))
            name = os.path.basename(path)
            item = {"name": name, "cmd": Path(path).absolute().as_uri()}
            self.current = Target(item, "movies", name, local=True)

        def drop_subtitles(paths):
            self.calls.append(("subtitles", list(paths)))
            return True

        def come_to_front():
            self.fronts += 1

        for obj, name, fake in (
            (controller, "play_file", play_file),
            (controller, "drop_subtitles", drop_subtitles),
            (controller, "now_playing", lambda: self.current),
            (controller, "playing", lambda: self.current is not None),
            (controller.session, "state", lambda: self.state),
            (self.window, "come_to_front", come_to_front),
        ):
            patch = mock.patch.object(obj, name, side_effect=fake)
            patch.start()
            self.addCleanup(patch.stop)

    def film(self, name, data=b"film"):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def plays(self):
        return [call[1:] for call in self.calls if call[0] == "play"]

    def wait_for_plays(self, count):
        self.assertTrue(wait_until(lambda: len(self.plays()) >= count), self.calls)

    def handover(self, *paths):
        self.window.open_paths([str(p) for p in paths], source="handover")


class OpenRouterTests(RouterCase):
    def test_drop_video_plays_it(self):
        path = self.film("ep.mkv", b"x" * 1234)
        self.window.screens.open_library("movies")
        self.assertEqual(drop(self.window, paths=[path]), (True, True))
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(path), 1234)])
        self.assertIs(self.window.pages.currentWidget(), self.window.player_page)
        self.assertEqual(self.fronts, 0)  # a drop is on the window already

    def test_drop_video_and_subtitle(self):
        video = self.film("ep.mkv", b"x" * 10)
        srt = self.folder / "ep.de.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        self.assertEqual(drop(self.window.host, paths=[srt, video]), (True, True))
        self.wait_for_plays(1)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [("play", str(video), 10)])  # not before it opened
        self.state = PlaybackState.PLAYING
        self.assertTrue(wait_until(lambda: len(self.calls) == 2), self.calls)
        self.assertEqual(self.calls[1], ("subtitles", [str(srt)]))

    def test_subtitles_dropped_with_a_video_wait_for_that_video(self):
        video, other = self.film("ep.mkv"), self.film("other.mkv")
        srt = self.folder / "ep.de.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        drop(self.window, paths=[video, srt])
        self.wait_for_plays(1)
        drop(self.window, paths=[other])  # chosen before the first one opened
        self.wait_for_plays(2)
        self.state = PlaybackState.PLAYING
        elapse(SETTLE_MS)
        self.assertEqual([call[0] for call in self.calls], ["play", "play"])

    def test_subtitles_alone_go_at_once(self):
        srt = self.folder / "ep.de.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nHallo\n", encoding="utf-8")
        self.handover(srt)
        self.assertEqual(self.calls, [("subtitles", [str(srt)])])

    def test_missing_file_toasts_and_keeps_playing(self):
        on_now = self.film("on now.mkv")
        self.handover(on_now)
        self.wait_for_plays(1)
        playing = self.current
        runs = []
        self.window.when_idle(lambda: runs.append(True))  # the update question waits
        folder = self.folder / "Season 1.mkv"
        folder.mkdir()
        link = self.folder / "link.mkv"
        link.symlink_to(self.folder / "deleted.mkv")
        for path in (self.folder / "gone.mkv", folder, link):
            with self.subTest(path=path.name):
                drop(self.window, paths=[path])
                text = f"Couldn't open “{path.name}”"
                self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertEqual(self.plays(), [(str(on_now), 4)])
        self.assertIs(self.current, playing)
        self.assertEqual(runs, [])  # the film on keeps its hold

    def test_same_file_again_only_comes_forward(self):
        path = self.film("ep.mkv")
        drop(self.window, paths=[path])
        self.wait_for_plays(1)
        self.assertEqual(drop(self.window, paths=[path]), (True, True))
        self.assertEqual(self.fronts, 1)
        # A hand-over spells it another way: the same file still.
        self.handover(os.path.join(str(self.folder), ".", "ep.mkv"))
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [(str(path), 4)])
        self.assertEqual(self.fronts, 2)

    def test_a_film_that_ended_opens_again(self):
        path = self.film("ep.mkv")
        drop(self.window, paths=[path])
        self.wait_for_plays(1)
        with mock.patch.object(self.window.controller, "playing", return_value=False):
            drop(self.window, paths=[path])  # its target is kept after its end
            self.wait_for_plays(2)

    def test_multiselect_first_by_name_wins(self):
        now = [100.0]
        self.window._clock = lambda: now[0]
        ep3, ep1, ep2 = self.film("Ep 3.mkv"), self.film("Ep 1.mkv"), self.film("Ep 2.mkv")
        self.handover(ep3)
        self.wait_for_plays(1)
        now[0] = 101.0
        self.handover(ep1)
        self.wait_for_plays(2)
        now[0] = 101.5
        self.handover(ep2)
        elapse(SETTLE_MS)
        self.assertEqual([path for path, _size in self.plays()], [str(ep3), str(ep1)])
        self.assertEqual(self.fronts, 3)  # each hand-over comes forward

    def test_late_file_after_3s_replaces(self):
        now = [100.0]
        self.window._clock = lambda: now[0]
        ep1, ep9 = self.film("Ep 1.mkv"), self.film("Ep 9.mkv")
        self.handover(ep1)
        self.wait_for_plays(1)
        now[0] = 104.0
        self.handover(ep9)
        self.wait_for_plays(2)
        self.assertEqual([path for path, _size in self.plays()], [str(ep1), str(ep9)])

    def test_unicode_and_long_paths_unchanged(self):
        deep = self.folder
        for part in ("Dizi Bölüm klasörü " + "ş" * 60, "İkinci sezon " + "ğ" * 60, "Ç" * 60):
            deep = deep / part
        deep.mkdir(parents=True)
        dropped = deep / "Dizi Bölüm 1 — çğıöşü İ.mkv"
        handed = deep / "Dizi Bölüm 2 — ÇĞIÖŞÜ i.mkv"
        for path in (dropped, handed):
            path.write_bytes(b"x" * 7)
            self.assertGreater(len(str(path)), 260)
        drop(self.window, paths=[dropped])
        self.wait_for_plays(1)
        self.handover(handed)
        self.wait_for_plays(2)
        played = [path for path, _size in self.plays()]
        self.assertEqual(played, [str(dropped), str(handed)])
        expected = [os.fsencode(dropped), os.fsencode(handed)]
        self.assertEqual([os.fsencode(p) for p in played], expected)
        # Its address (percent-encoded) is the same file: it only comes forward.
        self.handover(handed)
        elapse(SETTLE_MS)
        self.assertEqual(len(self.plays()), 2)

    def test_open_file_plays_what_it_picks(self):
        # Open File plays an HLS playlist or any file as a film, never as a provider.
        path = self.film("stream.m3u8", b"#EXTM3U\n#EXT-X-VERSION:3\n")
        before = list(self.window.provider_manager.providers)
        with mock.patch("ui.tv_pages.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.window.screens.open_file()
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(path), path.stat().st_size)])
        self.assertEqual(self.window.provider_manager.providers, before)

    def test_a_handed_over_playlist_becomes_a_provider(self):
        path = self.folder / "My channels.m3u"
        path.write_text(f'#EXTM3U\n#EXTINF:-1 group-title="News",Dropped One\n{PANEL}/1.ts\n')
        self.handover(path)
        providers = self.window.provider_manager.providers
        self.assertTrue(wait_until(lambda: any(p.get("name") == "My channels" for p in providers)))
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.fronts, 1)

    def test_a_file_handed_over_while_closing_is_logged(self):
        # Ruling 7: lost (QiTV quits), but the log says so, once for each film.
        film = self.film("Dizi Bölüm 2.mkv")
        srt = self.folder / "Dizi Bölüm 2.srt"
        srt.write_text(SUBTITLES, encoding="utf-8")
        with (
            mock.patch.object(self.window, "_closing", True),
            self.assertLogs("ui.tv_window", "INFO") as logs,
        ):
            self.handover(film, srt)
        elapse(SETTLE_MS)
        said = [line for line in logs.output if "QiTV is closing" in line]
        self.assertEqual(
            said,
            [
                "INFO:ui.tv_window:QiTV is closing: a file it was handed isn't opened"
                " (Dizi Bölüm 2.mkv)"
            ],
        )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.fronts, 0)

    def test_a_media_drag_is_taken(self):
        self.assertEqual(drop(self.window, paths=[self.folder / "notes.txt"]), (False, False))
        entered, _dropped = drop(self.window.host, paths=[self.folder / "clip.webm"])
        self.assertTrue(entered)


class NoProviderRouterTests(RouterCase):
    def provider_list(self):
        return []

    def test_late_file_open_with_no_providers(self):
        # Ruling 1: macOS's open event after the window (or a hand-over) holds as a launch's
        # file does: the welcome card that is up goes until the film is over.
        film = self.film("Dizi Bölüm 1.mkv")
        card = self.window.overlays.welcome
        self.show()
        self.assertTrue(wait_until(card.isVisible))
        self.window.open_paths([str(film)], source="handover")
        self.assertFalse(card.isVisible())
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(film), 4)])
        self.assertFalse(card.isVisible())
        self.window.controller.hold_for_file(False)  # it ended (the fake player says nothing)
        self.assertTrue(card.isVisible())


class LongWindowsPathTests(unittest.TestCase):
    """Windows: a QiTV that isn't long-path aware can't stat a path past 260 characters as it
    is spelled; the check tries its extended spelling, and the path plays as given."""

    LONG = "C:\\Films\\" + "Dizi Bölüm klasörü\\" * 14 + "Ep 1.mkv"

    def check(self, plain_works):
        seen = []

        def fake_stat(path):
            seen.append(path)
            if path.startswith("\\\\?\\") or plain_works:
                return os.stat_result((stat.S_IFREG | 0o644, 0, 0, 1, 0, 0, 1234, 0, 0, 0))
            raise FileNotFoundError(path)

        with (
            mock.patch("sys.platform", "win32"),
            mock.patch.object(os, "stat", side_effect=fake_stat),
            mock.patch("ui.tv_window.open", mock.mock_open(), create=True),
        ):
            size = tv_window._readable_size(self.LONG)
        return size, seen

    def test_the_extended_spelling_is_tried_when_the_plain_one_fails(self):
        self.assertGreater(len(self.LONG), 260)
        size, seen = self.check(plain_works=False)
        self.assertEqual(size, 1234)
        self.assertEqual(seen, [self.LONG, "\\\\?\\" + self.LONG])

    def test_a_path_that_works_is_looked_at_once(self):
        size, seen = self.check(plain_works=True)
        self.assertEqual((size, seen), (1234, [self.LONG]))

    def test_elsewhere_there_is_no_second_try(self):
        with (
            mock.patch("sys.platform", "linux"),
            mock.patch.object(os, "stat", side_effect=FileNotFoundError) as fake_stat,
        ):
            self.assertIsNone(tv_window._readable_size(self.LONG))
        fake_stat.assert_called_once_with(self.LONG)


@unittest.skipIf(base.INSTANCE is None, "libVLC is not available")
class RealPlayerTests(base.WindowCase):
    """The router's hand-off to the real player (libVLC with no picture or sound)."""

    vlc_instance = base.INSTANCE

    def test_a_dropped_film_plays_with_its_subtitles_and_again_only_comes_forward(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        video = Path(directory.name) / "Dizi Bölüm 1.mkv"
        shutil.copyfile(FILM, video)
        srt = video.with_name("Dizi Bölüm 1.de.srt")
        srt.write_text(SUBTITLES, encoding="utf-8")
        before = shutil.copyfile(FILM, video.with_name("Önceki.mkv"))
        window, controller = self.window, self.window.controller
        self.show()
        # Another film plays first: the new one opens once libVLC let go of it.
        window.open_paths([str(before)], source="handover")
        self.assertTrue(
            wait_until(
                lambda: controller.session.url == before.as_uri()
                and controller.session.state() is PlaybackState.PLAYING
            )
        )
        self.assertEqual(drop(window, paths=[video, srt]), (True, True))

        def shown():
            session = controller.session
            named = dict(session.subtitle_tracks()).get(session.subtitle_track())
            return session.url == video.as_uri() and named == srt.name

        self.assertTrue(wait_until(shown))
        self.assertIn((f"Subtitles: {srt.name}", "info"), self.toasts)
        controller.handle(Action.PLAY_PAUSE)  # it stays on through the next steps
        self.assertTrue(wait_until(lambda: controller.session.state() is PlaybackState.PAUSED))
        target = controller.now_playing()
        with mock.patch.object(controller, "play_file") as play_file:
            self.assertEqual(drop(window, paths=[video]), (True, True))
            window.open_paths([str(video)], source="handover")
            elapse(SETTLE_MS)
        play_file.assert_not_called()
        self.assertIs(controller.now_playing(), target)
        self.assertIs(controller.session.state(), PlaybackState.PAUSED)


if __name__ == "__main__":
    unittest.main()
