"""A playlist opened in QiTV is read first (spec §1, §11): a network link makes a provider, an
HLS tag one stream, and only local files a list whose videos play in its order, with Next and
the Next card following it. Fictional files in a temporary folder."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

from test_tv_controller import ControllerTestCase

from services.local_episodes import list_folder
from services.local_playlist import PlaylistRead, read_playlist
from services.next_episode import LOCAL_NEXT, episode_label
from services.watch_state import local_key

PANEL = "http://127.0.0.1:9"  # a closed port on the loopback
FINISHED = ("Finished", "info")


class Folder:
    """A temporary folder for the files a playlist names."""

    folder: Path

    def make_folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)

    def file(self, relative, data=b"media"):
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def playlist(self, text, name="Akşam listesi.m3u", encoding="utf-8"):
        path = self.folder / name
        path.write_bytes(text.encode(encoding))
        return path


class ReadPlaylistTests(Folder, unittest.TestCase):
    def setUp(self):
        self.make_folder()

    def test_network_links_make_a_provider(self):
        film = self.file("Bölüm 1.mkv")
        for link in (
            f"{PANEL}/live/1.ts",
            "HTTPS://example.test/2.ts",
            "rtmp://127.0.0.1/live/3",
            "udp://@239.0.0.1:1234",
        ):
            with self.subTest(link=link):
                path = self.playlist(f"#EXTM3U\n#EXTINF:-1,Bir\n{film}\n#EXTINF:-1,İki\n{link}\n")
                self.assertEqual(
                    read_playlist(str(path)), PlaylistRead("provider", "Akşam listesi", (), 0)
                )

    def test_a_link_in_a_tag_is_no_provider(self):
        song = self.file("Şarkı.mp3", b"x" * 7)
        text = '#EXTM3U\n#EXTINF:-1 tvg-logo="http://example.test/a.png",Şarkı\nŞarkı.mp3\n'
        read = read_playlist(str(self.playlist(text)))
        self.assertEqual(read.kind, "local")
        self.assertEqual(read.entries, (str(song),))
        self.assertEqual(read.sizes, (7,))

    def test_ext_x_makes_one_stream(self):
        for text in (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg1.ts\n",
            f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n{PANEL}/low.m3u8\n",
            "#EXTM3U\n#EXTINF:6,\nseg1.ts\n#EXT-X-ENDLIST\n",  # anywhere
        ):
            with self.subTest(text=text):
                self.file("seg1.ts")
                path = self.playlist(text, name="canlı.m3u8")
                self.assertEqual(read_playlist(str(path)), PlaylistRead("hls", "canlı", (), 0))

    def test_local_entries_relative_absolute_and_file_urls(self):
        first = self.file("Bölüm 1.mkv", b"x" * 3)
        song = self.file("sub/b.mp3", b"x" * 5)
        lossless = self.file("Şarkı ü #1.flac", b"x" * 9)
        far = self.file("başka/klip.mp4", b"x" * 11)
        film = self.file("film.mkv", b"x" * 13)
        text = (
            "#EXTM3U\n"
            "#EXTINF:120,Bölüm 1\n"
            "Bölüm 1.mkv\n"
            "  sub\\b.mp3  \n"  # written on Windows
            f"{lossless.as_uri()}\n"  # percent-encoded
            f"file://localhost{quote(str(far))}\n"
            f"{film}\n"  # absolute
            "./sub/../Bölüm 1.mkv\n"  # the same file again plays again
        )
        read = read_playlist(str(self.playlist(text)))
        self.assertEqual(read.kind, "local")
        self.assertEqual(read.name, "Akşam listesi")
        self.assertEqual(
            read.entries, (str(first), str(song), str(lossless), str(far), str(film), str(first))
        )
        self.assertEqual(read.sizes, (3, 5, 9, 11, 13, 3))
        self.assertEqual(read.missing, 0)

    def test_missing_entries_are_counted_and_skipped(self):
        film = self.file("film.mkv")
        self.file("notlar.txt")  # there, but no media
        self.file("başka.m3u")  # a playlist in a playlist is not played
        (self.folder / "klasör.mkv").mkdir()  # a folder named like a film
        text = (
            "#EXTM3U\n"
            "\n"
            "film.mkv\n"
            "yok.mkv\n"
            "notlar.txt\n"
            "başka.m3u\n"
            "klasör.mkv\n"
            "/nowhere/şarkı.mp3\n"
            "file:///nowhere/klip.mp4\n"
        )
        read = read_playlist(str(self.playlist(text)))
        self.assertEqual(read, PlaylistRead("local", "Akşam listesi", (str(film),), 6, (5,)))

    def test_cp1252_playlist_is_read(self):
        song = self.file("Müzik çö.mp3")
        text = "#EXTM3U\r\n#EXTINF:1,Müzik\r\nMüzik çö.mp3\r\n"
        read = read_playlist(str(self.playlist(text, encoding="cp1252")))
        self.assertEqual((read.kind, read.entries), ("local", (str(song),)))
        with_bom = self.playlist(text, name="Liste.m3u8", encoding="utf-8-sig")
        read = read_playlist(str(with_bom))
        self.assertEqual((read.kind, read.name, read.entries), ("local", "Liste", (str(song),)))

    def test_a_playlist_with_nothing_left_is_empty(self):
        for text, missing in (("#EXTM3U\n", 0), ("", 0), ("#EXTM3U\nyok.mkv\nyok.mp3\n", 2)):
            with self.subTest(text=text):
                read = read_playlist(str(self.playlist(text)))
                self.assertEqual(read, PlaylistRead("empty", "Akşam listesi", (), missing))

    def test_a_playlist_that_cant_be_read_is_told_apart(self):
        # Gone or refused: "Couldn't open" it, never "none of its files are here".
        gone = self.folder / "Gitti.m3u"
        self.assertEqual(read_playlist(str(gone)), PlaylistRead("unreadable", "Gitti"))
        self.file("film.mkv")
        path = self.playlist("film.mkv\n")
        refused = mock.patch(
            "services.local_playlist.open", side_effect=PermissionError, create=True
        )
        with refused:
            read = read_playlist(str(path))
        self.assertEqual(read, PlaylistRead("unreadable", "Akşam listesi"))

    def test_a_list_entry_has_no_season_label(self):
        entry = {"name": "b", "season": None, "number": None, "local_path": "/x/b.mkv", "size": 4}
        self.assertEqual(episode_label(None, entry), "")


class VideoListTests(Folder, ControllerTestCase):
    """TVController.play_list: the folder listing is the test's (FolderListing.start is
    captured), so a list entry is seen never to ask for one."""

    def setUp(self):
        super().setUp()
        self.make_folder()
        patch = mock.patch.object(self.controller._listing, "start")
        self.listings = patch.start()
        self.addCleanup(patch.stop)

    def end(self):
        self.session.stateChanged.emit("ended")
        self.session.ended.emit()

    def context(self):
        return self.controller.now_playing().context or {}

    def next_states(self):
        return [available for (available,) in self.transport.named("set_next_available")]

    def test_a_video_list_follows_its_order_with_next(self):
        self.controller.start()
        # Episodes of one show, listed out of their order: the list's order wins.
        first = self.file("Dizi.S01E02.mkv", b"x" * 5)
        second = self.file("Dizi.S01E01.mkv", b"x" * 7)
        third = self.file("Tatil.mp4", b"x" * 9)
        self.file("Dizi.S01E03.mkv")  # the folder's next for S01E02, never the list's
        self.watch.resume[local_key(first.name, 5)] = 60_000  # its name and size: its key
        self.controller.hold_for_file(True)
        self.controller.play_file(str(second), 7)  # an episode before it: its folder is listed
        late = self.listings.call_args.args
        self.listings.reset_mock()
        self.controller.play_list("Akşam", [str(first), str(second), str(third)], [5, 7, 9])
        url, kwargs = self.session.plays[-1]
        self.assertEqual((url, kwargs["start_ms"]), (first.absolute().as_uri(), 60_000))
        self.session.stateChanged.emit("playing")
        following = {
            "name": "Dizi.S01E01",
            "season": None,
            "number": None,
            "local_path": str(second),
            "size": 7,
        }
        self.assertEqual(self.context()[LOCAL_NEXT], following)
        self.assertEqual(self.next_states()[-1], True)
        self.controller._on_folder_listed(late[0], list_folder(late[1]))  # a late listing
        self.assertEqual(self.context()[LOCAL_NEXT], following)
        # It ends: the card offers the list's next, with no season and number.
        self.end()
        self.assertEqual(self.next_card.named("show_next"), [("Dizi.S01E01", "", "episode")])
        self.assertNotIn(FINISHED, self.toast.messages())
        self.next_card.activate()  # Play now
        self.assertEqual(self.session.plays[-1][0], second.absolute().as_uri())
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(third))
        self.assertEqual(self.next_states()[-1], True)
        # The transport's Next plays the list's next at once; the last one offers nothing.
        self.transport.nextRequested.emit()
        self.assertEqual(self.session.plays[-1][0], third.absolute().as_uri())
        self.session.stateChanged.emit("playing")
        self.assertNotIn(LOCAL_NEXT, self.context())
        self.assertEqual(self.next_states()[-1], False)
        self.end()
        self.assertEqual(len(self.next_card.named("show_next")), 1)
        self.assertIn(FINISHED, self.toast.messages())
        self.listings.assert_not_called()  # no entry's folder was listed

    def test_the_same_file_twice_in_a_list_plays_on_in_order(self):
        self.controller.start()
        song, film = self.file("Şarkı.mp3", b"x" * 3), self.file("Film.mkv", b"x" * 4)
        entries = [str(song), str(film), str(song)]
        with mock.patch.object(self.controller.watch, "mark_audio", create=True) as marked:
            self.controller.play_list("Karışık", entries, [3, 4, 3])
            self.session.stateChanged.emit("playing")
            for expected in (film, song):
                self.transport.nextRequested.emit()
                self.assertEqual(self.session.plays[-1][0], expected.absolute().as_uri())
                self.session.stateChanged.emit("playing")
        song_key = local_key(song.name, 3)
        self.assertEqual(marked.call_args_list, [mock.call(song_key)] * 2)  # as play_file's
        self.assertNotIn(LOCAL_NEXT, self.context())  # the third, the last one
        self.listings.assert_not_called()

    def test_play_again_after_stop_keeps_the_list(self):
        self.controller.start()
        first, second = self.file("A.mkv", b"x" * 5), self.file("B.mkv")
        self.watch.resume[local_key(first.name, 5)] = 60_000
        self.controller.play_list("Liste", [str(first), str(second)], [5, 5])
        self.session.stateChanged.emit("playing")
        self.controller.stop()
        self.controller.play_again()
        url, kwargs = self.session.plays[-1]
        self.assertEqual((url, kwargs["start_ms"]), (first.absolute().as_uri(), 0))  # the start
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(second))
        self.assertEqual(self.next_states()[-1], True)
        self.listings.assert_not_called()

    def test_a_list_given_up_for_another_file_is_not_followed(self):
        self.controller.start()
        first, second = self.file("A.mkv"), self.file("B.mkv")
        episode, after = self.file("Dizi.S01E01.mkv"), self.file("Dizi.S01E02.mkv")
        self.controller.play_list("Liste", [str(first), str(second)])
        self.controller.play_file(str(episode), 5)  # another file: its own folder's next
        generation, folder = self.listings.call_args.args
        self.controller._on_folder_listed(generation, list_folder(folder))
        self.transport.nextRequested.emit()
        self.assertEqual(self.session.plays[-1][0], after.absolute().as_uri())


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    unittest.main()
