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

from services import local_playlist
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

    def test_a_legacy_playlist_is_read_in_the_code_page_its_files_are_named_in(self):
        # Ruling P4-31: past UTF-8, the code page under which the most entries are there.
        turkish = [self.file(name) for name in ("Gölgeli Şehir.mp3", "Dağ İzi.mp3", "Işık.flac")]
        text = "#EXTM3U\r\n" + "".join(f"{path.name}\r\n" for path in turkish)
        read = read_playlist(str(self.playlist(text, name="Türkçe.m3u", encoding="cp1254")))
        self.assertEqual((read.kind, read.entries), ("local", tuple(map(str, turkish))))
        cyrillic = self.file("Ночной берег.mp3")
        read = read_playlist(str(self.playlist(f"{cyrillic.name}\n", encoding="cp1251")))
        self.assertEqual(read.entries, (str(cyrillic),))
        # cp1254 reads these bytes too, as other letters (Ş, ğ): cp1252's are the files'
        western = self.file("Þoka við sjó.mp3")
        read = read_playlist(str(self.playlist(f"{western.name}\n", encoding="cp1252")))
        self.assertEqual(read.entries, (str(western),))

    def test_a_tie_or_nothing_there_keeps_the_first_code_page(self):
        self.enterContext(
            mock.patch.object(local_playlist, "_code_pages", return_value=["cp1254", "cp1252"])
        )  # (a Windows runner tries its own first)
        sehir = self.file("Şehir.mp3")  # cp1254's 0xDE; cp1252 reads it as Þ
        self.file("Þoka.mp3")  # cp1252's 0xDE again: cp1254 reads it as Ş
        data = "Şehir.mp3\n".encode("cp1254") + "Þoka.mp3\n".encode("cp1252")
        (self.folder / "Liste.m3u").write_bytes(data)
        read = read_playlist(str(self.folder / "Liste.m3u"))
        self.assertEqual((read.entries, read.missing), ((str(sehir),), 1))  # one each: cp1254
        (self.folder / "Yok.m3u").write_bytes("Yok ğ.mp3\n".encode("cp1254"))
        read = read_playlist(str(self.folder / "Yok.m3u"))
        self.assertEqual(read, PlaylistRead("empty", "Yok", (), 1))

    def test_a_utf8_playlist_is_read_as_utf8_only(self):
        # Its entry is gone; its cp1252 misreading's twin is there, and stays unread.
        self.file("GÃ¶lge.mp3")
        read = read_playlist(str(self.playlist("Gölge.mp3\n")))
        self.assertEqual(read, PlaylistRead("empty", "Akşam listesi", (), 1))

    def test_each_file_is_looked_at_once_and_the_read_stops_once_all_are_there(self):
        pages = list(local_playlist.LEGACY_CODE_PAGES)  # (a Windows runner tries its own first)
        self.enterContext(mock.patch.object(local_playlist, "_code_pages", return_value=pages))
        plain = self.file("film.mkv")
        song = self.file("Şarkı.mp3")
        data = b"film.mkv\n" + "Şarkı.mp3\n".encode("cp1254") + b"yok.mp3\n"
        (self.folder / "Liste.m3u").write_bytes(data)
        stat = os.stat
        looked: list[str] = []

        def spy(path, *args, **kwargs):
            looked.append(str(path))
            return stat(path, *args, **kwargs)

        with mock.patch.object(local_playlist.os, "stat", side_effect=spy):
            read = read_playlist(str(self.folder / "Liste.m3u"))
        self.assertEqual((read.entries, read.missing), ((str(plain), str(song)), 1))
        self.assertEqual(len(looked), len(set(looked)))  # each path once, whatever the code page
        self.assertEqual(looked.count(str(plain)), 1)
        (self.folder / "Tamam.m3u").write_bytes(b"film.mkv\n" + "Şarkı.mp3\n".encode("cp1254"))
        looked.clear()
        with mock.patch.object(local_playlist.os, "stat", side_effect=spy):
            read_playlist(str(self.folder / "Tamam.m3u"))
        self.assertEqual(looked, [str(plain), str(song)])  # all there: no other code page

    def test_the_code_pages_tried_start_with_windows_own(self):
        later = ["cp1254", "cp1252", "cp1251", "cp1253", "cp1250"]
        with mock.patch.object(local_playlist.sys, "platform", "linux"):
            self.assertEqual(local_playlist._code_pages(), later)
        for system, pages in (
            ("cp1251", ["cp1251", "cp1254", "cp1252", "cp1253", "cp1250"]),
            ("cp932", ["cp932", *later]),
            ("cp65001", later),  # the system's UTF-8: read first already
            ("cp1254", later),
        ):
            with self.subTest(system=system):
                with (
                    mock.patch.object(local_playlist.sys, "platform", "win32"),
                    mock.patch.object(local_playlist.locale, "getencoding", return_value=system),
                ):
                    self.assertEqual(local_playlist._code_pages(), pages)

    def test_entries_on_another_computer_are_never_looked_at(self):
        # Item 9: a share's file is checked as it plays, never while the list is read.
        film = self.file("film.mkv", b"x" * 4)
        text = (
            "\\\\nas\\share\\a.mp3\n"
            "//nas/share/b%20c.mp3\n"
            "file://nas/share/d%20e.mp3\n"
            "\\\\nas\\share\\notes.txt\n"
            "film.mkv\n"
        )
        path = self.playlist(text)
        stat = os.stat
        looked: list[str] = []

        def spy(path, *args, **kwargs):
            looked.append(str(path))
            return stat(path, *args, **kwargs)

        for platform in ("win32", "linux"):
            looked.clear()
            with (
                self.subTest(platform=platform),
                mock.patch.object(local_playlist.sys, "platform", platform),
                mock.patch.object(local_playlist.os, "stat", side_effect=spy),
            ):
                read = read_playlist(str(path))
                self.assertEqual(looked, [str(film)])  # the share's never
                if platform == "win32":  # there, as far as the read can tell
                    shared = ("\\\\nas\\share\\a.mp3", "\\\\nas\\share\\b%20c.mp3")
                    shared += ("\\\\nas\\share\\d e.mp3",)
                    self.assertEqual(read.entries, (*shared, str(film)))
                    self.assertEqual((read.sizes, read.missing), ((None, None, None, 4), 1))
                else:  # a Windows share's path names nothing this system opens
                    self.assertEqual((read.entries, read.missing), ((str(film),), 4))

    def test_a_playlist_on_a_share_looks_at_its_own_shares_files(self):
        # That computer was asked for the list already: its share's files are looked at as
        # any list's are (sizes, resume keys, the vote); another share's or computer's aren't.
        home = "\\\\nas\\share\\Lists"
        for path, other in (
            ("\\\\nas\\share\\Music\\a.mp3", False),
            ("\\\\NAS\\Share\\b.mp3", False),
            ("//nas/share/c.mp3", False),
            ("\\\\nas\\other\\a.mp3", True),
            ("\\\\box\\share\\a.mp3", True),
        ):
            with self.subTest(path=path):
                self.assertIs(local_playlist._other_host(path, home), other)
        self.assertTrue(local_playlist._other_host("\\\\nas\\share\\a.mp3", "C:\\Lists"))

    def test_which_paths_are_another_computers(self):
        for path, other in (
            ("\\\\nas\\share\\a.mp3", True),
            ("//nas/share/a.mp3", True),
            ("\\\\?\\UNC\\nas\\share\\a.mp3", True),
            ("\\\\?\\C:\\Music\\a.mp3", False),  # extended: this computer's drive
            ("\\\\.\\C:\\Music\\a.mp3", False),
            ("C:\\Music\\a.mp3", False),
            ("/home/ayse/a.mp3", False),
        ):
            with self.subTest(path=path):
                self.assertIs(local_playlist._other_host(path), other)

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


def fake_tracks(tags):
    """A stand-in for album_listing.read_tracks: each file's `tags` by its name (a name not in
    `tags` is a file libVLC could not read); its calls are kept in `.calls`."""
    calls = []

    def read(files, *, budget_s, cap, stop=None):
        calls.append(([path for path, _size in files], budget_s, cap))
        tracks = []
        for path, size in list(files)[:cap]:
            name = os.path.basename(path)
            known = tags.get(name)
            track = {"album": "", "disc": None, "track": None, **(known or {})}
            track.update(path=path, name=name, size=size, parsed=known is not None)
            tracks.append(track)
        return tracks

    read.calls = calls
    return read


class _Clock:
    """local_playlist's clock in a test (its `time`): each look at it is a second later."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now


class ReadFolderTests(Folder, unittest.TestCase):
    """read_folder and read_files: a folder opened in QiTV, or several files dropped together,
    as a list (Task 11). The tags are a fake's (read_tracks)."""

    def setUp(self):
        self.make_folder()

    def read(self, relative, **how):
        return local_playlist.read_folder(str(self.folder / relative), **how)

    def names(self, read):
        return [os.path.basename(entry) for entry in read.entries]

    def songs(self, tags):
        """read_tracks answers with `tags` (fake_tracks)."""
        return mock.patch.object(local_playlist, "read_tracks", side_effect=fake_tracks(tags))

    def test_one_shows_episodes_go_by_season_and_episode(self):
        # Spelt four ways: natural order would put the spaced ones first, S02E01 among them.
        for name, size in (
            ("Lantern.Coast.S01E02.720p.mkv", 2),
            ("lantern_coast_s01e10.mkv", 10),
            ("Lantern Coast - S01E01 - Pilot.mkv", 1),
            ("LANTERN COAST S02E01.mp4", 21),
            ("Lantern Coast S01E03E04.mkv", 3),
        ):
            self.file(f"Lantern Coast/{name}", b"x" * size)
        read = self.read("Lantern Coast")
        self.assertEqual((read.kind, read.name, read.missing), ("local", "Lantern Coast", 0))
        self.assertEqual(
            self.names(read),
            [
                "Lantern Coast - S01E01 - Pilot.mkv",
                "Lantern.Coast.S01E02.720p.mkv",
                "Lantern Coast S01E03E04.mkv",
                "lantern_coast_s01e10.mkv",
                "LANTERN COAST S02E01.mp4",
            ],
        )
        self.assertEqual(read.entries[0], str(self.folder / "Lantern Coast" / self.names(read)[0]))
        self.assertEqual(read.sizes, (1, 2, 3, 10, 21))

    def test_bare_names_in_a_season_folder_are_its_shows(self):
        # Plan 3's rule: a bare S01E02 takes its show from the folder (above "Season 1").
        for name in ("S01E10.mkv", "Lantern Coast S01E02.mkv", "S01E01.mkv"):
            self.file(f"Lantern Coast/Season 1/{name}")
        read = self.read("Lantern Coast/Season 1")
        self.assertEqual(read.name, "Season 1")
        self.assertEqual(self.names(read), ["S01E01.mkv", "Lantern Coast S01E02.mkv", "S01E10.mkv"])

    def test_two_shows_or_an_extra_keep_natural_order(self):
        # Ruling 3: season and episode order only when every video is one show's episode.
        for name in (
            "Lantern Coast S01E01.mkv",
            "Brannow Fell S01E10.mkv",
            "Brannow Fell S01E2.mkv",
        ):
            self.file(f"Two/{name}")
        self.assertEqual(
            self.names(self.read("Two")),
            ["Brannow Fell S01E2.mkv", "Brannow Fell S01E10.mkv", "Lantern Coast S01E01.mkv"],
        )
        for name in ("Making of.mkv", "Lantern Coast S01E02.mkv", "Lantern.Coast.S01E01.mkv"):
            self.file(f"Extra/{name}")
        self.assertEqual(
            self.names(self.read("Extra")),
            ["Lantern Coast S01E02.mkv", "Lantern.Coast.S01E01.mkv", "Making of.mkv"],
        )

    def test_songs_in_a_mixed_folder_keep_their_natural_places(self):
        # One order for the whole list: the episodes take the places natural order gives the
        # videos, in season and episode order; the songs stay where natural order puts them.
        for name in (
            "Lantern.Coast.S01E01.mkv",
            "Lantern Coast Theme.mp3",
            "Lantern Coast S01E02.mkv",
        ):
            self.file(f"Mixed/{name}")
        tags = fake_tracks({})
        with mock.patch.object(local_playlist, "read_tracks", side_effect=tags):
            read = self.read("Mixed")
        self.assertEqual(  # natural order: S01E02, the theme, S01E01
            self.names(read),
            ["Lantern.Coast.S01E01.mkv", "Lantern Coast Theme.mp3", "Lantern Coast S01E02.mkv"],
        )
        self.assertEqual(tags.calls, [])  # no tags are read for a list with videos

    def test_a_folder_of_songs_is_an_album_named_after_it(self):
        for name in ("Harbour.flac", "Tide.mp3", "Lanterns.ogg", "Quay.opus"):
            self.file(f"Brannow Fell/{name}")
        tags = fake_tracks(
            {
                "Tide.mp3": {"album": "Brannow Fell", "track": 1},
                "Harbour.flac": {"album": "Brannow Fell", "disc": 2, "track": 1},
                "Lanterns.ogg": {"album": "Brannow Fell", "track": 2},
                "Quay.opus": {"album": "brannow  fell", "disc": 1, "track": 3},
            }
        )
        with mock.patch.object(local_playlist, "read_tracks", side_effect=tags):
            read = self.read("Brannow Fell")
        self.assertEqual((read.kind, read.name), ("local", "Brannow Fell"))
        self.assertEqual(  # by disc (none is the first), then track
            self.names(read), ["Tide.mp3", "Lanterns.ogg", "Quay.opus", "Harbour.flac"]
        )
        self.assertEqual(read.sizes, (5, 5, 5, 5))
        [(files, budget_s, cap)] = tags.calls
        self.assertEqual(len(files), 4)
        self.assertGreater(budget_s, 0)
        self.assertLessEqual(budget_s, local_playlist.READ_BUDGET_S)
        self.assertEqual(cap, local_playlist.TAGS_CAP)

    def test_songs_go_by_name_unless_each_is_read_numbered_and_of_one_album(self):
        names = ("10 Quay.mp3", "2 Tide.mp3", "1 Harbour.mp3")
        for name in names:
            self.file(f"Songs/{name}")
        by_name = ["1 Harbour.mp3", "2 Tide.mp3", "10 Quay.mp3"]
        numbered = {"10 Quay.mp3": {"track": 1}, "2 Tide.mp3": {"track": 2}}
        for case, tags in (
            ("untagged", {name: {} for name in names}),
            ("one not read", numbered),
            ("one with no number", {**numbered, "1 Harbour.mp3": {}}),
            ("two albums", {n: {"album": n[-8:], "track": i} for i, n in enumerate(names)}),
        ):
            with self.subTest(case=case), self.songs(tags):
                self.assertEqual(self.names(self.read("Songs")), by_name)
        with self.songs({**numbered, "1 Harbour.mp3": {"track": 3}}):
            read = self.read("Songs")
        self.assertEqual(self.names(read), ["10 Quay.mp3", "2 Tide.mp3", "1 Harbour.mp3"])

    def test_past_the_cap_songs_are_not_read_and_go_by_name(self):
        for number in (3, 1, 2):
            self.file(f"Many/{number} Song.mp3")
        tags = fake_tracks({f"{n} Song.mp3": {"track": 4 - n} for n in (1, 2, 3)})
        with mock.patch.object(local_playlist, "read_tracks", side_effect=tags):
            read = self.read("Many", cap=2)
        self.assertEqual(tags.calls, [])
        self.assertEqual(self.names(read), ["1 Song.mp3", "2 Song.mp3", "3 Song.mp3"])

    def test_only_the_tags_have_a_time(self):
        # The listing never looks at the clock: the tags get the whole budget, from the moment
        # the folder was listed (one tick of the fake clock later).
        for number in (2, 1):
            self.file(f"Slow/{number} Song.mp3")
        tags = fake_tracks({"1 Song.mp3": {"track": 2}, "2 Song.mp3": {"track": 1}})
        with (
            mock.patch.object(local_playlist, "time", _Clock()),
            mock.patch.object(local_playlist, "read_tracks", side_effect=tags),
        ):
            read = self.read("Slow", budget_s=10.0)
        [(_files, budget_s, _cap)] = tags.calls
        self.assertEqual(budget_s, 9.0)
        self.assertEqual(self.names(read), ["2 Song.mp3", "1 Song.mp3"])  # by their tags

    def test_subfolders_are_never_read(self):
        self.file("Lantern Coast/Lantern Coast S01E02.mkv")
        self.file("Lantern Coast/Season 2/Lantern Coast S02E01.mkv")  # a subfolder's
        self.file("Lantern Coast/Extras/Lantern Coast S01E01.mkv")
        (self.folder / "Lantern Coast" / "Lantern Coast S01E01.mkv").mkdir()  # named like one
        self.file("Lantern Coast/._Lantern Coast S01E03.mkv")  # macOS's companion: hidden
        self.file("Lantern Coast/.Lantern Coast S01E04.mkv")
        self.file("Lantern Coast/notes.txt")
        self.file("Lantern Coast/Lantern Coast S01E02.srt")
        self.file("Lantern Coast/Lantern Coast.m3u")
        read = self.read("Lantern Coast")
        self.assertEqual(
            read.entries, (str(self.folder / "Lantern Coast/Lantern Coast S01E02.mkv"),)
        )

    def test_an_empty_folder_says_so(self):
        self.file("Empty Shelf/notes.txt")
        self.file("Empty Shelf/Inner/Lantern Coast S01E01.mkv")
        self.assertEqual(self.read("Empty Shelf"), PlaylistRead("empty", "Empty Shelf"))
        (self.folder / "Bare").mkdir()
        self.assertEqual(self.read("Bare"), PlaylistRead("empty", "Bare"))

    def test_a_trailing_separator_still_names_the_folder(self):
        # macOS's Finder hands a dropped folder over as ".../Lantern Coast/".
        film = self.file("Lantern Coast/Lantern Coast S01E01.mkv")
        read = local_playlist.read_folder(str(self.folder / "Lantern Coast") + os.sep)
        self.assertEqual((read.name, read.entries), ("Lantern Coast", (str(film),)))

    def test_a_gone_or_refused_folder_is_unreadable_and_a_file_is_other(self):
        self.assertEqual(self.read("Gone Shelf"), PlaylistRead("unreadable", "Gone Shelf"))
        self.file("notes.txt")
        self.assertEqual(self.read("notes.txt"), PlaylistRead("other", "notes.txt"))
        self.file("Refused/Lantern Coast S01E01.mkv")
        with mock.patch.object(local_playlist.os, "scandir", side_effect=PermissionError):
            self.assertEqual(self.read("Refused"), PlaylistRead("unreadable", "Refused"))

    def test_a_slow_share_is_listed_whole_and_only_its_tags_are_cut(self):
        # Review I1: a share waking up (each look at the clock a second later, past a half
        # second's budget) said a full folder had nothing to play, or listed it with holes.
        for number in (4, 1, 3, 2):
            self.file(f"Slow show/Lantern Coast S01E0{number}.mkv")
            self.file(f"Slow songs/{number} Song.mp3")
        with mock.patch.object(local_playlist, "time", _Clock()):
            read = self.read("Slow show", budget_s=0.5)
        self.assertEqual(read.kind, "local")
        self.assertEqual(self.names(read), [f"Lantern Coast S01E0{n}.mkv" for n in (1, 2, 3, 4)])
        tags = fake_tracks({f"{n} Song.mp3": {"track": 5 - n} for n in (1, 2, 3, 4)})
        with (
            mock.patch.object(local_playlist, "time", _Clock()),
            mock.patch.object(local_playlist, "read_tracks", side_effect=tags),
        ):
            read = self.read("Slow songs", budget_s=0.5)
        self.assertEqual(tags.calls, [])  # no time left for the tags: by name
        self.assertEqual(self.names(read), [f"{n} Song.mp3" for n in (1, 2, 3, 4)])

    def test_a_file_whose_size_cant_be_read_plays_without_one(self):
        # M6: kept, as album_listing.music_files keeps it (it plays from its start).
        film = self.file("Odd/Lantern Coast S01E01.mkv")
        real = os.scandir

        class Entry:
            def __init__(self, entry):
                self.name, self._entry = entry.name, entry

            def is_file(self):
                return self._entry.is_file()

            def stat(self):
                raise PermissionError

        class Listing:
            def __init__(self, path):
                with real(path) as entries:
                    self._entries = [Entry(entry) for entry in entries]

            def __enter__(self):
                return iter(self._entries)

            def __exit__(self, *_exc):
                return False

        with mock.patch.object(local_playlist.os, "scandir", side_effect=Listing):
            read = self.read("Odd")
        self.assertEqual(read, PlaylistRead("local", "Odd", (str(film),), 0, (None,)))

    def test_an_ordering_error_falls_back_to_names_and_says_so(self):
        # M5: a fault in the order is no "Couldn't open": by name, and a warning in the log.
        first, second = self.file("Bugs/Lantern Coast S01E01.mkv"), self.file("Bugs/B.mkv")
        for read in (
            lambda: self.read("Bugs"),
            lambda: local_playlist.read_files([str(first), str(second)]),
        ):
            with (
                mock.patch.object(local_playlist, "_by_episode", side_effect=RuntimeError),
                self.assertLogs("services.local_playlist", "WARNING") as logs,
            ):
                got = read()
            self.assertEqual((got.kind, self.names(got)), ("local", ["B.mkv", first.name]))
            self.assertIn("could not be put in order (RuntimeError)", logs.output[0])

    def test_subtitles_go_to_the_entries_they_are_for(self):
        # M4: by the entry's name (the longest that leads the subtitle's), else its episode,
        # else the entry the list starts at.
        entries = ["/s/Ep 1.mkv", "/s/Ep 10.mkv", "/s/Lantern.Coast.S01E03.mkv", "/s/Other.mkv"]
        subtitles = [
            "/d/Ep 10.de.srt",
            "/d/ep 1.srt",  # in any case
            "/d/Lantern Coast S01E03.tr.srt",
            "/d/Notes.srt",
        ]
        self.assertEqual(
            local_playlist.subtitles_for(entries, subtitles, start=3),
            {
                "/s/Ep 10.mkv": ["/d/Ep 10.de.srt"],
                "/s/Ep 1.mkv": ["/d/ep 1.srt"],
                "/s/Lantern.Coast.S01E03.mkv": ["/d/Lantern Coast S01E03.tr.srt"],
                "/s/Other.mkv": ["/d/Notes.srt"],
            },
        )
        self.assertEqual(local_playlist.subtitles_for([], ["/d/a.srt"]), {})

    def test_several_files_read_as_a_list(self):
        # Natural order is the other way round: the spaced name, the dotted, the underscored.
        third = self.file("Lantern Coast/Lantern Coast S01E03.mkv", b"x" * 3)
        first = self.file("Lantern Coast/Lantern_Coast_S01E01.mkv", b"x" * 1)
        second = self.file("Lantern Coast/Lantern.Coast.S01E02.mkv", b"x" * 2)
        gone = self.folder / "Lantern Coast" / "Lantern Coast S01E04.mkv"
        read = local_playlist.read_files([str(third), str(gone), str(second), str(first)])
        entries = (str(first), str(second), str(third))
        self.assertEqual(read, PlaylistRead("local", "Lantern Coast", entries, 1, (1, 2, 3)))
        self.assertEqual(
            local_playlist.read_files([str(gone)]),
            PlaylistRead("unreadable", "Lantern Coast", missing=1),
        )


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

    def test_a_series_folder_resumed_mid_way_keeps_its_earlier_episodes(self):
        # Review I3: a folder's list starts at the episode stopped part-way into (it resumes
        # there), and the list still holds the ones before it, which play on in its order.
        self.controller.start()
        first, second, third = (self.file(f"Fell.S01E0{n}.mkv", b"x" * n) for n in (1, 2, 3))
        self.watch.resume[local_key(second.name, 2)] = 600_000
        entries = [str(first), str(second), str(third)]
        self.controller.hold_for_file(True)
        self.controller.play_list("Brannow Fell", entries, [1, 2, 3], start=1)
        url, kwargs = self.session.plays[-1]
        self.assertEqual((url, kwargs["start_ms"]), (second.absolute().as_uri(), 600_000))
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(third))
        self.assertEqual(self.controller._list.entries, tuple(entries))  # none left out
        self.controller._play_entry(0)  # the list's first (its own entry, as a list row plays)
        self.assertEqual(self.session.plays[-1][0], first.absolute().as_uri())
        self.session.stateChanged.emit("playing")
        self.assertEqual(self.context()[LOCAL_NEXT]["local_path"], str(second))
        # A start out of the list's range is its last (or first) entry.
        self.controller.play_list("Brannow Fell", entries, [1, 2, 3], start=7)
        self.assertEqual(self.session.plays[-1][0], third.absolute().as_uri())

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
