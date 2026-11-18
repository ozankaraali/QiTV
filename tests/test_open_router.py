"""Every file opened in QiTV goes through one router (TVWindow.open_paths): a dropped video
plays, a subtitle file dropped with it loads once it opens, a file that is gone says so, the
file that plays already only comes forward, and a multi-select's hand-overs play the first by
name. Fictional files in a temporary folder; the controller's play_file is a fake, but for the
real player's tests (libVLC with no picture or sound)."""

import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_local_playlist import fake_tracks
from test_provider_flow_file_hold import LaunchCase
from timer_fixtures import elapse, wait_until

from services import local_playlist
from services.next_episode import LOCAL_NEXT
from services.playback import PlaybackState
from services.watch_state import local_key
from tests import test_tv_window as base
from tests.test_provider_drop import PANEL, drop
from ui import tv_window
from ui.keymap import Action
from ui.link_lanes import Target

SETTLE_MS = 300  # a stat a wrong path would have started is back by then
FILM = Path(__file__).parent / "fixtures" / "film" / "h264_aac.mkv"  # 8 s
SMOKE = Path(__file__).parent / "fixtures" / "smoke.mp4"  # about 6 s
TO_ITS_END_MS = 20_000  # the smoke film plays to its end, with margin for a loaded runner
SUBTITLES = "1\n00:00:00,000 --> 00:00:08,000\nHallo\n"
# A playlist, a folder or files dropped together (a list) gave way to a newer choice.
GAVE_WAY = "INFO:ui.tv_window:A list opened in QiTV gave way to a newer choice"


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
        self.assertTrue(self.window.subtitles_due())  # AI captions don't come on over them
        self.state = PlaybackState.PLAYING
        self.assertTrue(wait_until(lambda: len(self.calls) == 2), self.calls)
        self.assertEqual(self.calls[1], ("subtitles", [str(srt)]))
        self.assertFalse(self.window.subtitles_due())

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

    def test_a_launch_with_a_qt_film_plays_it(self):
        # .qt is a QuickTime movie, as .mov is: the type QiTV claims brings it along
        path = self.film("Dizi Bölüm 1.qt")
        self.window.open_paths([str(path)], source="launch")
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(path), 4)])

    def test_a_handed_over_f4v_plays(self):
        path = self.film("x.f4v")
        self.handover(path)
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(path), 4)])
        entered, _dropped = drop(self.window.host, paths=[self.film("y.f4v")])
        self.assertTrue(entered)  # a drag of one is taken too
        self.wait_for_plays(2)

    def test_superscript_and_circled_digit_names_play(self):
        # natural_key raised on '²' and '①' (str.isdigit, then int()): in the drag's check and
        # in the hand-over's slot alike.
        first, second = self.film("①1.mp4"), self.film("1²2.mkv")
        self.handover(first, second)
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(second), 4)])  # "1²2" sorts before "①1"
        entered, _dropped = drop(self.window.host, paths=[self.film("²1 Film.mkv")])
        self.assertTrue(entered)
        self.wait_for_plays(2)
        self.assertEqual(self.plays()[-1], (str(self.folder / "²1 Film.mkv"), 4))

    def test_a_file_qitv_wont_play_says_so_and_the_film_plays_on(self):
        on_now = self.film("on now.mkv")
        self.handover(on_now)
        self.wait_for_plays(1)
        runs = []
        self.window.when_idle(lambda: runs.append(True))  # the update question waits
        notes, srt = self.folder / "Notlar ğ.txt", self.folder / "on now.srt"
        notes.write_text("x", encoding="utf-8")
        srt.write_text(SUBTITLES, encoding="utf-8")
        self.handover(notes, srt)
        text = "QiTV can't play “Notlar ğ.txt”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertEqual(self.calls[-1], ("subtitles", [str(srt)]))  # to what plays
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [(str(on_now), 4)])
        self.assertEqual(runs, [])  # the film on keeps its hold

    def test_subtitles_handed_over_while_a_film_is_looked_at_keep_its_hold(self):
        film = self.film("ep.mkv")
        runs = []
        with mock.patch.object(self.window._file_check, "start"):  # still being looked at
            self.handover(film)
            self.window.when_idle(lambda: runs.append(True))
            srt = self.folder / "ep.srt"
            srt.write_text(SUBTITLES, encoding="utf-8")
            self.handover(srt)
        self.assertEqual(runs, [])

    def looked_at(self, path, how="handover"):
        """`path` opened, its check still on its way (a slow share): the check's arguments."""
        with mock.patch.object(self.window._file_check, "start") as check:
            if how == "handover":
                self.handover(path)
            else:
                drop(self.window, paths=[path])
        check.assert_called_once()
        return check.call_args.args

    def test_a_choice_made_while_a_file_is_looked_at_wins(self):
        # Review M5: the check of a file on a slow share came back after the viewer chose
        # something else (here an outside player takes the title: TVController's own call),
        # and the file played over it. The later choice wins; its hold goes too.
        generation, path = self.looked_at(self.film("ep.mkv"))
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [])  # held while it is looked at
        self.window.controller._before_new_target(None)
        self.assertEqual(runs, [True])
        self.window._file_check.checked.emit(generation, path, 4)  # the share answers
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [])

    def test_a_channel_chosen_before_the_file_lets_the_file_play(self):
        # The link of a channel asked for before the file was opened comes while the file is
        # looked at: it plays a moment, and the file, the later choice, still replaces it.
        generation, path = self.looked_at(self.film("ep.mkv"))
        channel = Target({"name": "Kanal 1", "cmd": "http://x/1"}, "live", "Kanal 1")
        self.window.controller._before_new_target(channel, delivered=True)
        self.window._file_check.checked.emit(generation, path, 4)
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(path, 4)])
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [])  # held again for it

    def channel_on(self):
        """A channel playing under the file's check: the controller's lane is faked."""
        channel = Target({"name": "Kanal 1", "cmd": "http://x/1"}, "live", "Kanal 1")
        lanes = mock.patch.object(self.window.controller._lanes, "request")
        self.lanes = lanes.start()
        self.addCleanup(lanes.stop)
        return channel

    def file_still_plays(self, generation, path):
        self.window._file_check.checked.emit(generation, path, 4)
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(path, 4)])

    def test_an_automatic_retry_while_a_file_is_looked_at_leaves_the_file(self):
        # Review of plan 1: a stream failure retried by itself during the check dropped the
        # file (the retry asked as if the viewer had chosen it).
        generation, path = self.looked_at(self.film("ep.mkv"))
        channel = self.channel_on()
        controls = self.window.controller._live_controls
        with (
            mock.patch.object(controls, "_current", return_value=channel),
            mock.patch.object(controls, "may_retry", return_value=True),
            mock.patch.object(controls, "stop_tune"),
            mock.patch("ui.live_controls.item_provider.sign_in_again"),
        ):
            controls._on_live_failed("boom")
        self.lanes.assert_called_once()  # the retry was asked for
        self.file_still_plays(generation, path)

    def test_an_archive_that_ends_while_a_file_is_looked_at_leaves_the_file(self):
        generation, path = self.looked_at(self.film("ep.mkv"))
        channel = self.channel_on()
        catchup = self.window.controller._live_controls.catchup
        ask = mock.Mock(target=channel, moment=0.0, ends=0.0, programme=False)
        playing = mock.Mock(ask=ask, played=True)
        with (
            mock.patch.object(
                type(catchup), "_playing", new_callable=mock.PropertyMock, return_value=playing
            ),
            mock.patch.object(catchup, "position", return_value=4e9),
            mock.patch.object(catchup, "_set_playing"),
            mock.patch.object(catchup._tuner, "stop"),
        ):
            self.assertTrue(catchup.on_ended())  # back to live by itself
        self.lanes.assert_called_once()
        self.file_still_plays(generation, path)

    def test_a_channel_chosen_while_a_file_is_looked_at_wins_over_a_retry_too(self):
        # The viewer's own choice (a channel pressed) still beats the file, after a retry.
        generation, path = self.looked_at(self.film("ep.mkv"))
        channel = self.channel_on()
        controller = self.window.controller
        controller._request(channel, chosen_now=True)
        self.window._file_check.checked.emit(generation, path, 4)
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [])
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [True])  # its hold went too

    def test_go_live_pressed_while_a_file_is_looked_at_wins(self):
        generation, path = self.looked_at(self.film("ep.mkv"))
        channel = self.channel_on()
        catchup = self.window.controller._live_controls.catchup
        playing = mock.Mock(ask=mock.Mock(target=channel))
        with (
            mock.patch.object(
                type(catchup), "_playing", new_callable=mock.PropertyMock, return_value=playing
            ),
            mock.patch.object(catchup, "_set_playing"),
            mock.patch.object(catchup._tuner, "stop"),
        ):
            catchup.go_live(chosen_now=True)  # LiveControls.go_live, the viewer's button
        self.window._file_check.checked.emit(generation, path, 4)
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [])

    def test_a_film_that_plays_keeps_its_hold_when_the_next_file_gives_way(self):
        on_now = self.film("on now.mkv")
        self.handover(on_now)
        self.wait_for_plays(1)
        generation, path = self.looked_at(self.film("next.mkv"), how="drop")
        runs = []
        self.window.when_idle(lambda: runs.append(True))
        self.window.file_gives_way()  # a newer choice, still on its way
        self.assertEqual(runs, [])  # the film on now keeps its hold until it gives way
        self.window._file_check.checked.emit(generation, path, 4)
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [(str(on_now), 4)])

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
        entered, _dropped = drop(self.window.host, paths=[self.folder / "clip.webm"])
        self.assertTrue(entered)
        # Task 11: a file of no kind QiTV knows may be a folder, which only the disk tells (off
        # the GUI thread, once dropped): it is taken too (FolderRouterTests). Words are not.
        self.assertEqual(drop(self.window, text="just words"), (False, False))


class ListCase(RouterCase):
    """The router over a fake play_list too, which records what it is given (its `start` in
    `starts`) and opens the entry it starts at as the controller does (list_entry_opens)."""

    def setUp(self):
        super().setUp()
        self.starts: list = []

        def play_list(name, entries, sizes=(), start=None):
            self.calls.append(("list", name, list(entries), list(sizes)))
            self.starts.append(start)
            self.open_entry(entries[start or 0])

        patch = mock.patch.object(self.window.controller, "play_list", side_effect=play_list)
        patch.start()
        self.addCleanup(patch.stop)

    def open_entry(self, path):
        """The list's entry at `path` opens (TVController._play_entry)."""
        item = {"name": os.path.basename(path), "cmd": Path(path).absolute().as_uri()}
        self.current = Target(item, "movies", item["name"], local=True)
        self.window.list_entry_opens(self.current, str(path))

    def lists(self):
        return [call[1:] for call in self.calls if call[0] == "list"]


class PlaylistRouterTests(ListCase):
    """A playlist opened in QiTV is read on a thread first (spec §1): links make a provider,
    an HLS one plays as one film, and local files play as a list (the controller's play_list
    is a fake that records it)."""

    def provider_names(self):
        return [p.get("name") for p in self.window.provider_manager.providers]

    def write(self, name, text, encoding="utf-8"):
        path = self.folder / name
        path.write_bytes(text.encode(encoding))
        return path

    def slow_reads(self, gate):
        """Each playlist is read once `gate` is set (a playlist on a slow share)."""
        from services.local_playlist import read_playlist

        def slow(path):
            gate.wait(10)
            return read_playlist(path)

        return mock.patch("ui.tv_window.read_playlist", side_effect=slow)

    def test_a_local_playlist_plays_as_a_list_and_says_what_is_missing(self):
        second, first = self.film("Bölüm 2.mkv", b"x" * 3), self.film("Bölüm 1.mkv", b"x" * 5)
        path = self.write("Akşam.m3u", f"#EXTM3U\nBölüm 2.mkv\nyok.mkv\n{first}\nyok.mp3\n")
        before = self.provider_names()
        self.window.screens.open_library("movies")
        self.handover(path)
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Akşam", [str(second), str(first)], [3, 5])])
        self.assertIs(self.window.pages.currentWidget(), self.window.player_page)
        text = "2 files in the playlist are missing"
        self.assertTrue(wait_until(lambda: (text, "info") in self.toasts), self.toasts)
        runs: list[bool] = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [])  # held, as a file opened in QiTV is
        self.assertEqual(self.plays(), [])
        self.assertEqual(self.provider_names(), before)
        self.assertEqual(self.fronts, 1)
        # One missing: said in the singular.
        self.handover(self.write("Tek.m3u8", f"{second}\nyok.mkv\n"))
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        text = "1 file in the playlist is missing"
        self.assertTrue(wait_until(lambda: (text, "info") in self.toasts), self.toasts)
        # None missing: nothing said. A dropped playlist is read too.
        self.toasts.clear()
        whole = self.write("Tam.m3u", f"{first}\n")
        self.assertEqual(drop(self.window, paths=[whole]), (True, True))
        self.assertTrue(wait_until(lambda: len(self.lists()) == 3), self.calls)
        self.assertEqual(self.lists()[-1], ("Tam", [str(first)], [5]))
        elapse(SETTLE_MS)
        self.assertEqual(self.toasts, [])

    def test_an_empty_local_playlist_says_so_and_plays_nothing(self):
        path = self.write("Boş.m3u", "#EXTM3U\n#EXTINF:1,Yok\nyok.mkv\n")
        before = self.provider_names()
        self.handover(path)
        text = "None of the files in this playlist are here"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.provider_names(), before)
        runs: list[bool] = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [True])  # nothing held

    def test_an_hls_playlist_plays_as_one_film(self):
        text = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg1.ts\n"
        self.film("seg1.ts")
        path = self.write("canlı.m3u8", text)
        before = self.provider_names()
        self.handover(path)
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(path), path.stat().st_size)])
        self.assertEqual(self.lists(), [])
        self.assertEqual(self.provider_names(), before)

    def test_a_network_playlist_still_becomes_a_provider(self):
        # Any link makes a provider, beside local files too.
        film = self.film("film.mkv")
        path = self.write("Karışık.m3u", f"#EXTM3U\n{film}\n#EXTINF:-1,Bir\n{PANEL}/1.ts\n")
        self.handover(path)
        self.assertTrue(wait_until(lambda: "Karışık" in self.provider_names()))
        self.assertIn(("Added Karışık to your providers", "info"), self.toasts)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [])

    def test_a_link_playlist_leaves_a_film_being_looked_at(self):
        film = self.film("ep.mkv")
        playlist = self.write("Kanallar.m3u", f"#EXTM3U\n#EXTINF:-1,Bir\n{PANEL}/1.ts\n")
        gate = threading.Event()

        def slow(path):
            gate.wait(10)
            return 4

        with mock.patch("ui.tv_window._readable_size", side_effect=slow):
            self.handover(film)
            self.handover(playlist)  # handed over while the film is looked at
            self.assertTrue(wait_until(lambda: "Kanallar" in self.provider_names()))
            gate.set()
            self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(film), 4)])
        self.assertEqual(self.lists(), [])

    def test_a_film_opened_while_a_list_is_read_wins(self):
        film = self.film("ep.mkv")
        playlist = self.write("Liste.m3u", f"{film}\n")
        gate = threading.Event()
        with self.slow_reads(gate), self.assertLogs("ui.tv_window", "INFO") as logs:
            self.handover(playlist)
            drop(self.window, paths=[film])  # chosen while the list is read
            self.wait_for_plays(1)
            gate.set()
            self.assertTrue(wait_until(lambda: GAVE_WAY in logs.output), logs.output)
        self.assertEqual(self.lists(), [])
        self.assertEqual(self.plays(), [(str(film), 4)])

    def test_a_list_read_gives_way_to_a_channel_chosen_meanwhile(self):
        playlist = self.write("Liste.m3u", f"{self.film('ep.mkv')}\n")
        gate = threading.Event()
        with self.slow_reads(gate), self.assertLogs("ui.tv_window", "INFO") as logs:
            self.handover(playlist)
            self.window.file_gives_way()  # a channel chosen (TVController._file_gives_way)
            gate.set()
            self.assertTrue(wait_until(lambda: GAVE_WAY in logs.output), logs.output)
        self.assertEqual(self.calls, [])

    def test_a_drop_of_a_playlist_and_a_link_adds_both_at_once(self):
        # One toast for both, and the link's provider is the one shown, as before the read.
        path = self.write("Kanallar.m3u", f"#EXTM3U\n#EXTINF:-1,Bir\n{PANEL}/1.ts\n")
        link = f"{PANEL}/player_api.php?username=ann&password=pw1"
        self.assertEqual(drop(self.window, paths=[path], urls=[link], text=link), (True, True))
        names = self.provider_names
        self.assertTrue(wait_until(lambda: {"Kanallar", "127.0.0.1"} <= set(names())), names())
        elapse(SETTLE_MS)
        added = [text for text, _kind in self.toasts if text.startswith("Added ")]
        self.assertEqual(added, ["Added Kanallar, 127.0.0.1 to your providers"])
        self.assertEqual(self.window.config_manager.selected_provider_name, "127.0.0.1")

    def test_a_playlist_that_cant_be_read_says_so(self):
        gone = self.folder / "Gitti.m3u"  # handed over, then moved before it was read
        before = self.provider_names()
        self.handover(gone)
        text = "Couldn't open “Gitti.m3u”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.provider_names(), before)
        runs: list[bool] = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [True])  # nothing held


def gated_folder_reads(gate, calls=None):
    """Each folder is read once `gate` is set (a folder on a slow share); a file of no kind
    at once. Each read's (path, how) goes to `calls` too."""
    from services.local_playlist import read_folder

    def slow(path, **how):
        if calls is not None:
            calls.append((path, how))
        if os.path.isdir(path):
            gate.wait(10)
        return read_folder(path, **how)

    return mock.patch("ui.tv_window.read_folder", side_effect=slow)


def series(folder):
    """A season of a fictional show in `folder` (natural order is the other way round: the
    spaced name, the dotted, the underscored), with a subfolder's episode and a hidden file
    that never play: its episodes, in order."""
    folder.mkdir(parents=True, exist_ok=True)
    episodes = []
    for name, size in (
        ("Lantern_Coast_S01E01.mkv", 1),
        ("Lantern.Coast.S01E02.mkv", 2),
        ("Lantern Coast S01E03.mkv", 3),
    ):
        (folder / name).write_bytes(b"x" * size)
        episodes.append(folder / name)
    (folder / "Extras").mkdir()
    (folder / "Extras" / "Lantern Coast S01E00.mkv").write_bytes(b"x")
    (folder / "._Lantern Coast S01E01.mkv").write_bytes(b"x")
    return episodes


class FolderRouterTests(ListCase):
    """A folder opened in QiTV, and several files dropped together, play as a list (Task 11):
    read on the playlists' thread, in season and episode order, from the episode the viewer
    stopped part-way into. A launch or a hand-over of several files plays one, as before."""

    def setUp(self):
        super().setUp()
        self.season = self.folder / "Lantern Coast"
        self.episodes = series(self.season)
        self.entries = [str(path) for path in self.episodes]

    def assert_held(self, held=True):
        runs: list[bool] = []
        self.window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [] if held else [True])

    def test_a_dropped_series_folder_plays_in_episode_order(self):
        self.window.screens.open_library("movies")
        self.assertEqual(drop(self.window, paths=[self.season]), (True, True))
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])
        self.assertIs(self.window.pages.currentWidget(), self.window.player_page)
        self.assert_held()  # as a file opened in QiTV is
        elapse(SETTLE_MS)
        self.assertEqual((self.plays(), self.toasts), ([], []))
        self.assertEqual(self.fronts, 0)

    def test_a_dropped_folder_starts_where_it_stopped(self):
        watch = self.window.watch
        first, second, third = self.episodes
        watch.update_position(local_key(first.name, 1), 1_150_000, 1_200_000)  # past 90%: seen
        watch.update_position(local_key(second.name, 2), 600_000, 1_200_000)  # part-way
        watch.update_position(local_key(third.name, 3), 60_000, 1_200_000)  # part-way too
        drop(self.window, paths=[self.season])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        # Review I3: the whole list, from the second (the one before it stays in the list).
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])
        self.assertEqual(self.starts, [1])
        self.assertEqual(self.current.item["cmd"], second.absolute().as_uri())  # it plays
        # None part-way (two seen, one barely begun): the one after the last seen (Task 12's
        # ruling 3, which the Folders shelf's "next" says too).
        watch.update_position(local_key(second.name, 2), 1_190_000, 1_200_000)
        watch.update_position(local_key(third.name, 3), 3_000, 1_200_000)
        self.handover(self.season)
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        self.assertEqual(self.lists()[-1], ("Lantern Coast", self.entries, [1, 2, 3]))
        self.assertEqual(self.starts, [1, 2])
        # The last one seen too: as any list starts (its first).
        watch.update_position(local_key(third.name, 3), 1_190_000, 1_200_000)
        self.handover(self.season)
        self.assertTrue(wait_until(lambda: len(self.lists()) == 3), self.calls)
        self.assertEqual(self.starts, [1, 2, None])

    def test_a_handed_over_folder_of_songs_plays_as_its_album(self):
        album = self.folder / "Brannow Fell"
        album.mkdir()
        for name in ("Tide.mp3", "Harbour.flac"):
            (album / name).write_bytes(b"song")
        tags = {"Tide.mp3": {"track": 1}, "Harbour.flac": {"track": 2}}
        with mock.patch.object(local_playlist, "read_tracks", side_effect=fake_tracks(tags)):
            self.handover(album)
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        entries = [str(album / "Tide.mp3"), str(album / "Harbour.flac")]
        self.assertEqual(self.lists(), [("Brannow Fell", entries, [4, 4])])
        self.assertEqual(self.fronts, 1)

    def test_an_empty_folder_says_so(self):
        empty = self.folder / "Empty Shelf"
        (empty / "Inner").mkdir(parents=True)
        (empty / "Inner" / "Lantern Coast S01E04.mkv").write_bytes(b"x")  # a subfolder's
        (empty / "notes.txt").write_text("x", encoding="utf-8")
        self.assertEqual(drop(self.window, paths=[empty]), (True, True))
        text = "No videos or music in “Empty Shelf”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [])
        self.assert_held(False)

    def test_a_folder_is_read_off_the_gui_thread(self):
        from services.local_playlist import read_folder

        threads = []

        def spy(path, **how):
            threads.append(threading.current_thread())
            return read_folder(path, **how)

        with mock.patch("ui.tv_window.read_folder", side_effect=spy):
            self.handover(self.season)
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(len(threads), 1)
        self.assertIsNot(threads[0], threading.main_thread())

    def test_a_list_read_gives_way_to_a_film_chosen_meanwhile(self):
        film = self.film("ep.mkv")
        gate = threading.Event()
        with gated_folder_reads(gate), self.assertLogs("ui.tv_window", "INFO") as logs:
            drop(self.window, paths=[self.season])
            drop(self.window, paths=[film])  # chosen while the folder is read
            self.wait_for_plays(1)
            gate.set()
            self.assertTrue(wait_until(lambda: GAVE_WAY in logs.output), logs.output)
        self.assertEqual(self.lists(), [])
        self.assertEqual(self.plays(), [(str(film), 4)])

    def test_a_folder_dropped_with_a_film_leaves_the_film_alone(self):
        film = self.film("ep.mkv")
        reads: list = []
        gate = threading.Event()
        gate.set()  # a spy: no folder waits
        with gated_folder_reads(gate, reads):
            self.assertEqual(drop(self.window, paths=[self.season, film]), (True, True))
            self.wait_for_plays(1)
            self.assertTrue(wait_until(lambda: reads))
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [(str(film), 4)])
        self.assertEqual((self.lists(), self.toasts), ([], []))
        self.assertEqual(reads, [(str(self.season), {"cap": 0})])  # M7: no tags read for it

    def test_a_file_qitv_wont_play_leaves_a_read_alone(self):
        # Review M3: said as soon as its own look is back (never behind the read), and what is
        # read before it (a folder, a playlist) still plays.
        notes = self.folder / "Notlar.txt"
        notes.write_text("x", encoding="utf-8")
        text = "QiTV can't play “Notlar.txt”"
        gate = threading.Event()
        with gated_folder_reads(gate):
            drop(self.window, paths=[self.season])
            self.handover(notes)
            self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
            self.assertEqual(self.lists(), [])  # the folder is still read
            gate.set()
            self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])
        self.toasts.clear()
        playlist = self.folder / "Liste.m3u"
        playlist.write_text(f"{self.entries[0]}\n", encoding="utf-8")
        gate = threading.Event()
        from services.local_playlist import read_playlist

        def slow(path):
            gate.wait(10)
            return read_playlist(path)

        with mock.patch("ui.tv_window.read_playlist", side_effect=slow):
            self.handover(playlist)
            self.assertEqual(drop(self.window, paths=[notes]), (True, True))
            self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
            gate.set()
            self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        self.assertEqual(self.lists()[-1], ("Liste", self.entries[:1], [1]))

    def test_a_dropped_subtitle_loads_into_the_entry_it_is_for(self):
        # Review M4: by its name, not into whatever entry the list starts at.
        srt = self.season / "Lantern.Coast.S01E02.de.srt"
        srt.write_text(SUBTITLES, encoding="utf-8")
        drop(self.window, paths=[*self.episodes, srt])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertFalse(self.window.subtitles_due())  # not the first's
        self.state = PlaybackState.PLAYING
        elapse(SETTLE_MS)
        self.assertNotIn("subtitles", [call[0] for call in self.calls])
        self.state = PlaybackState.OPENING
        self.open_entry(self.entries[1])  # the list goes on to the second
        self.assertTrue(self.window.subtitles_due())
        self.state = PlaybackState.PLAYING
        self.assertTrue(wait_until(lambda: self.calls[-1] == ("subtitles", [str(srt)])))
        # Review N3: each time it opens again (Play again, back to it), until another list.
        loaded = len(self.subtitle_loads())
        self.state = PlaybackState.OPENING
        self.open_entry(self.entries[1])
        self.assertTrue(self.window.subtitles_due())
        self.state = PlaybackState.PLAYING
        self.assertTrue(wait_until(lambda: len(self.subtitle_loads()) == loaded + 1))
        drop(self.window, paths=list(self.episodes[:2]))  # another list, with no subtitle
        self.assertTrue(wait_until(lambda: len(self.lists()) == 2), self.calls)
        self.state = PlaybackState.OPENING
        self.open_entry(self.entries[1])
        self.assertFalse(self.window.subtitles_due())

    def subtitle_loads(self):
        return [call for call in self.calls if call[0] == "subtitles"]

    def test_links_dropped_with_a_folder_still_make_providers(self):
        # Review N2: a folder's read (tentative) adds the links dropped with it, as before.
        link = f"{PANEL}/player_api.php?username=ann&password=pw1"
        self.assertEqual(
            drop(self.window, paths=[self.season], urls=[link], text=link), (True, True)
        )
        providers = self.window.provider_manager.providers
        self.assertTrue(wait_until(lambda: "127.0.0.1" in [p.get("name") for p in providers]))
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])

    def test_a_dropped_file_of_no_kind_says_qitv_cant_play_it(self):
        notes = self.folder / "Notlar ğ.txt"
        notes.write_text("x", encoding="utf-8")
        self.assertEqual(drop(self.window, paths=[notes]), (True, True))
        text = "QiTV can't play “Notlar ğ.txt”"
        self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        elapse(SETTLE_MS)
        self.assertEqual(self.calls, [])
        self.assert_held(False)

    def test_several_dropped_files_play_as_a_list(self):
        srt = self.season / "Lantern Coast S01E01.srt"  # the first's, by its episode
        srt.write_text(SUBTITLES, encoding="utf-8")
        third, first, second = self.episodes[2], self.episodes[0], self.episodes[1]
        self.assertEqual(drop(self.window, paths=[third, srt, first, second]), (True, True))
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])
        self.assertEqual(self.plays(), [])
        self.assert_held()
        # The subtitle file dropped with them loads into its entry, once it opened.
        self.assertTrue(self.window.subtitles_due())
        self.state = PlaybackState.PLAYING
        self.assertTrue(wait_until(lambda: self.calls[-1] == ("subtitles", [str(srt)])))
        # One file dropped alone plays as a file (its folder names its next episode).
        drop(self.window, paths=[third])
        self.wait_for_plays(1)
        self.assertEqual(self.plays(), [(str(third), 3)])

    def test_several_dropped_files_start_where_they_stopped(self):
        first, second, third = self.episodes
        self.window.watch.update_position(local_key(third.name, 3), 600_000, 1_200_000)
        drop(self.window, paths=[third, second, first])
        self.assertTrue(wait_until(lambda: self.lists()), self.calls)
        self.assertEqual(self.lists(), [("Lantern Coast", self.entries, [1, 2, 3])])
        self.assertEqual(self.starts, [2])

    def test_a_launch_with_several_files_keeps_the_burst_rule(self):
        # Plan 1: a start plays the first by name; a hand-over in its burst that sorts later
        # is left out, and neither ever becomes a list.
        now = [100.0]
        self.window._clock = lambda: now[0]
        ep2, ep1, ep3 = self.film("Ep 2.mkv"), self.film("Ep 1.mkv"), self.film("Ep 3.mkv")
        self.window.open_paths([str(ep2), str(ep1), str(ep3)], source="launch")
        self.wait_for_plays(1)
        now[0] = 101.0
        self.handover(ep3, ep2)
        elapse(SETTLE_MS)
        self.assertEqual(self.plays(), [(str(ep1), 4)])
        self.assertEqual(self.lists(), [])


class FolderStartTests(LaunchCase, base.WindowCase):
    """A start with a folder, over no provider (Ruling P4-3): held while the folder is read and
    while its list plays; one with nothing to play says so and lets go."""

    def provider_list(self):
        return []

    def test_a_start_with_a_folder_is_held(self):
        show = Path(self.config_dir) / "Lantern Coast"
        episodes = [str(path) for path in series(show)]
        window, gate = self.window, threading.Event()
        card = window.overlays.welcome
        with (
            mock.patch.object(card, "show_card", wraps=card.show_card) as shown,
            mock.patch.object(window.controller, "play_list") as played,
            gated_folder_reads(gate),
        ):
            held, runs = self.launch(show)
            elapse(SETTLE_MS)
            self.assertTrue(held)  # from the first moment (main())
            self.assertFalse(card.isVisible())
            self.assertEqual(runs, [])  # the update question waits for the read
            gate.set()
            self.assertTrue(wait_until(lambda: played.called))
            elapse(SETTLE_MS)
            shown.assert_not_called()
        self.assertEqual(played.call_args.args, ("Lantern Coast", tuple(episodes), (1, 2, 3)))
        self.assertEqual(runs, [])  # the list holds as a film does
        window.controller.hold_for_file(False)  # it ended (the list is a fake here)
        self.assertEqual(runs, [True])
        self.assertTrue(card.isVisible())

    def test_a_folder_handed_over_before_the_start_ran_holds_it(self):
        # Review I2: main() held nothing (macOS's open event after the window was built); the
        # hand-over shows the window, whose start runs on the next pass of the event loop.
        show = Path(self.config_dir) / "Lantern Coast"
        series(show)
        window, gate, runs = self.window, threading.Event(), []
        card = window.overlays.welcome
        with (
            mock.patch.object(card, "show_card", wraps=card.show_card) as shown,
            mock.patch.object(window.controller, "play_list") as played,
            gated_folder_reads(gate),
        ):
            window.open_paths([str(show)], source="handover")
            window.when_idle(lambda: runs.append(True))
            self.show()  # its start runs now, held by the read
            elapse(SETTLE_MS)
            self.assertEqual(runs, [])
            self.assertFalse(card.isVisible())
            gate.set()
            self.assertTrue(wait_until(lambda: played.called))
            elapse(SETTLE_MS)
            shown.assert_not_called()
        self.assertEqual(runs, [])  # the list holds it now
        window.controller.hold_for_file(False)  # it ended (the list is a fake here)
        self.assertEqual(runs, [True])

    def test_a_missing_file_opened_while_a_handed_over_folder_is_read_lets_the_start_run(self):
        # Review N1: the file is a newer choice than the folder the start waits for; it is
        # gone, so nothing plays, and the start runs (the folder's late read gives way).
        show = Path(self.config_dir) / "Lantern Coast"
        series(show)
        gone = Path(self.config_dir) / "Gone S01E09.mkv"
        window, gate, runs = self.window, threading.Event(), []
        with (
            mock.patch.object(window.controller, "play_list") as played,
            gated_folder_reads(gate),
        ):
            window.open_paths([str(show)], source="handover")  # before the start ran
            window.when_idle(lambda: runs.append(True))
            self.show()
            elapse(SETTLE_MS)
            self.assertEqual(runs, [])
            window.open_paths([str(gone)], source="handover")
            text = "Couldn't open “Gone S01E09.mkv”"
            self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
            self.assertTrue(wait_until(lambda: runs == [True]), runs)
            self.assertTrue(wait_until(window.overlays.welcome.isVisible))
            self.assertFalse(window.controller._flow.reading())
            gate.set()
            elapse(SETTLE_MS)
        played.assert_not_called()

    def test_a_start_with_an_empty_folder_says_so_and_lets_go(self):
        empty = Path(self.config_dir) / "Empty Shelf"
        empty.mkdir()
        gate = threading.Event()
        with gated_folder_reads(gate):
            held, runs = self.launch(empty)
            elapse(SETTLE_MS)
            self.assertTrue(held)
            self.assertEqual(runs, [])  # not let go before the read says so
            gate.set()
            text = "No videos or music in “Empty Shelf”"
            self.assertTrue(wait_until(lambda: (text, "error") in self.toasts), self.toasts)
        self.assertTrue(wait_until(self.window.overlays.welcome.isVisible))
        self.assertEqual(runs, [True])

    def test_subtitles_handed_over_while_the_folder_is_read_keep_its_hold(self):
        # Ruling P4-3: a subtitle file's open never lets go of a start that waits for a read.
        show = Path(self.config_dir) / "Lantern Coast"
        series(show)
        srt = show / "Lantern Coast S01E01.srt"
        srt.write_text(SUBTITLES, encoding="utf-8")
        window, gate = self.window, threading.Event()
        card = window.overlays.welcome
        with mock.patch.object(window.controller, "play_list") as played, gated_folder_reads(gate):
            held, runs = self.launch(show)
            window.open_paths([str(srt)], source="handover")
            elapse(SETTLE_MS)
            self.assertTrue(held)
            self.assertEqual(runs, [])
            self.assertFalse(card.isVisible())
            gate.set()
            self.assertTrue(wait_until(lambda: played.called))
        self.assertEqual(runs, [])  # the list holds it now

    def test_a_folder_read_given_way_to_a_providers_list_lets_the_start_go(self):
        show = Path(self.config_dir) / "Lantern Coast"
        series(show)
        playlist = Path(self.config_dir) / "Kanallar.m3u"
        playlist.write_text(f"#EXTM3U\n#EXTINF:-1,Bir\n{PANEL}/1.ts\n", encoding="utf-8")
        window, gate = self.window, threading.Event()
        with (
            mock.patch.object(window.controller, "play_list") as played,
            mock.patch.object(window, "add_dropped_providers") as added,
            gated_folder_reads(gate),
        ):
            held, runs = self.launch(show)
            self.assertTrue(held)
            window.open_paths([str(playlist)], source="handover")  # a newer choice
            self.assertTrue(wait_until(lambda: added.called))
            self.assertTrue(wait_until(lambda: runs == [True]), runs)  # nothing holds it now
            gate.set()
            elapse(SETTLE_MS)
        played.assert_not_called()  # the folder gave way
        self.assertTrue(wait_until(window.overlays.welcome.isVisible))


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

    def test_a_local_playlist_plays_its_entries_in_order(self):
        # Spec §11, end to end: read on its thread, played by the real player in the list's
        # order (not by name), the transport's Next on to the list's next entry.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        first = Path(shutil.copyfile(SMOKE, Path(directory.name) / "İkinci.mp4"))
        second = Path(shutil.copyfile(SMOKE, first.with_name("Birinci.mp4")))
        playlist = first.with_name("Akşam.m3u")
        playlist.write_text("#EXTM3U\nİkinci.mp4\nyok.mkv\nBirinci.mp4\n", encoding="utf-8")
        window, controller = self.window, self.window.controller
        session = controller.session
        self.show()
        window.open_paths([str(playlist)], source="handover")
        self.assertTrue(wait_until(lambda: session.url == first.as_uri()))
        missing = ("1 file in the playlist is missing", "info")
        self.assertTrue(wait_until(lambda: missing in self.toasts), self.toasts)
        target = controller.now_playing()
        self.assertEqual(target.context[LOCAL_NEXT]["local_path"], str(second))
        window.overlays.transport.nextRequested.emit()  # the transport's Next
        self.assertTrue(wait_until(lambda: session.url == second.as_uri()))
        self.assertNotIn(LOCAL_NEXT, controller.now_playing().context or {})  # the last one
        controller.stop()

    def test_a_folder_episode_plays_on_through_the_card(self):
        # Spec §8: the folder's next episode is offered at the end and plays as a file; one gone
        # by the time Next is pressed fails as libVLC says, and the file's hold goes.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        first = Path(directory.name) / "Show.S01E01.mkv"
        shutil.copyfile(SMOKE, first)
        second = Path(shutil.copyfile(SMOKE, first.with_name("Show.S01E02.mkv")))
        gone = Path(shutil.copyfile(SMOKE, first.with_name("Show.S01E03.mkv")))
        window, controller = self.window, self.window.controller
        card, session = window.overlays.next_card, controller.session
        self.show()
        window.open_paths([str(first)], source="dialog")
        self.assertTrue(wait_until(lambda: session.url == first.as_uri()))
        self.assertTrue(wait_until(card.isVisible, TO_ITS_END_MS))  # it ended: the card
        card.activate()  # Play now
        self.assertTrue(wait_until(lambda: session.url == second.as_uri()))

        def offered():
            target = controller.now_playing()
            following = (target.context or {}).get(LOCAL_NEXT) if target is not None else None
            return isinstance(following, dict) and following["local_path"] == str(gone)

        self.assertTrue(wait_until(offered))
        gone.unlink()
        runs: list[bool] = []
        window.when_idle(lambda: runs.append(True))
        self.assertEqual(runs, [])  # the file plays: held
        window.overlays.transport.nextRequested.emit()  # the transport's Next
        self.assertTrue(wait_until(lambda: runs == [True], TO_ITS_END_MS), self.toasts)
        errors = [text for text, kind in self.toasts if kind == "error"]
        self.assertTrue(errors and gone.name in errors[-1], self.toasts)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg makes the songs")
    def test_an_album_shows_its_list_and_plays_on(self):
        # Plan 4, Task 5, end to end: two tagged tones with their covers (the second an .m4a
        # whose cover is an attached picture: no picture for libVLC). The first opens on the
        # Album view once its folder is listed, ends, and the second plays with no card; after
        # it, "Finished".
        from tests.test_tv_controller_audio import make_song

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        # Long enough on a loaded runner (the dummy sound output plays its first ~2 s at once):
        # the album is listed well before the first ends, the second is read while it plays.
        first = make_song(
            folder / "01 Arn.mp3", title="Arn", album="Thrum Days", number=1, seconds=10
        )
        second = make_song(
            folder / "02 Pell.m4a", title="Pell", album="Thrum Days", number=2, seconds=8
        )
        make_song(folder / "Elsewhere.mp3", title="Rook", album="Brannow Fell", number=1, seconds=3)
        window, controller = self.window, self.window.controller
        screen, card, session = window.audio_screen, window.overlays.next_card, controller.session
        self.show()
        window.open_paths([str(first)], source="dialog")
        self.assertTrue(wait_until(lambda: session.url == first.as_uri()))
        self.assertTrue(wait_until(lambda: screen.isVisible() and screen.shown_view() == "album"))
        names = [name for name in screen.controls() if name.startswith("track_")]
        self.assertEqual(names, ["track_0", "track_1"])  # the album's two, not the other one
        self.assertTrue(window.overlays.transport.audio())
        self.assertTrue(wait_until(lambda: session.url == second.as_uri(), TO_ITS_END_MS))
        self.assertTrue(wait_until(lambda: session.state() is PlaybackState.PLAYING))
        self.assertIs(session.has_video_track(), False)  # its attached cover is no picture
        self.assertTrue(screen.isVisible())
        self.assertFalse(card.isVisible())
        self.assertNotIn(("Finished", "info"), self.toasts)
        finished = ("Finished", "info")
        self.assertTrue(wait_until(lambda: finished in self.toasts, TO_ITS_END_MS), self.toasts)
        self.assertFalse(card.isVisible())
        self.assertEqual(session.url, second.as_uri())


if __name__ == "__main__":
    unittest.main()
