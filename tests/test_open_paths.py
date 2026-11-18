"""Paths and their kinds: launch arguments, sorting, media types."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from services import open_paths


class LaunchPaths(unittest.TestCase):
    def test_skips_flags_and_replace_value(self):
        with tempfile.TemporaryDirectory() as d:
            film = Path(d, "Dizi Bölüm 1.mkv")
            film.write_bytes(b"x")
            old = Path(d, "old.exe")
            old.write_bytes(b"x")
            argv = ["qitv", "--replace", str(old), "-psn_0_12345", str(film), "missing.mkv"]
            self.assertEqual(open_paths.launch_paths(argv), [str(film)])

    def test_relative_paths_become_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            film = Path(d, "a.mp4")
            film.write_bytes(b"x")
            before = os.getcwd()
            os.chdir(d)
            try:
                expected = os.path.join(os.getcwd(), "a.mp4")  # the folder as the system says
                self.assertEqual(open_paths.launch_paths(["qitv", "a.mp4"]), [expected])
            finally:
                os.chdir(before)

    def test_paths_are_kept_as_given_never_resolved(self):
        # Review Focus 1: a launch names the file as a hand-over or DDE does. resolve() turned
        # a link into its target (a mapped drive into its share on Windows).
        with tempfile.TemporaryDirectory() as d:
            film = Path(d, "Dizi Bölüm 1.mkv")
            film.write_bytes(b"x")
            given = os.path.join(d, ".", "Dizi Bölüm 1.mkv")
            self.assertEqual(open_paths.launch_paths(["qitv", given]), [str(film)])
            if os.name == "nt":
                return  # a link takes a privilege there
            folder = Path(d, "Kısayol")
            folder.mkdir()
            link = folder / "Bölüm 1.mkv"
            link.symlink_to(film)
            self.assertEqual(open_paths.launch_paths(["qitv", str(link)]), [str(link)])
            broken = folder / "gone.mkv"
            broken.symlink_to(Path(d, "deleted.mkv"))
            self.assertEqual(open_paths.launch_paths(["qitv", str(broken)]), [])

    def test_a_long_windows_path_is_looked_at_in_its_extended_spelling(self):
        long = "C:\\" + "\\".join(["Sezon 1 uzun klasör"] * 14) + "\\Dizi Bölüm 1.mkv"
        seen = []

        def isfile(path):
            seen.append(path)
            return path.startswith("\\\\?\\")  # only the extended spelling is found

        for system, expected, looked in (
            ("win32", [long], [long, open_paths.extended_path(long)]),  # kept plain
            ("linux", [], [long]),  # no second spelling elsewhere
        ):
            with (
                self.subTest(system=system),
                mock.patch.object(open_paths.sys, "platform", system),
                mock.patch.object(open_paths.os.path, "abspath", side_effect=lambda p: p),
                mock.patch.object(open_paths.os.path, "isfile", side_effect=isfile),
            ):
                seen.clear()
                self.assertEqual(open_paths.launch_paths(["qitv", long]), expected)
                self.assertEqual(seen, looked)

    def test_folders_and_first_argument_are_not_paths(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(open_paths.launch_paths([d, d]), [])


class SortPaths(unittest.TestCase):
    def test_playlist_first_then_media_natural_order(self):
        s = open_paths.sort_paths(
            ["/x/Ep 10.mkv", "/x/list.m3u8", "/x/Ep 2.mkv", "/x/a.srt", "/x/notes.txt"]
        )
        self.assertEqual(s.media, ("/x/Ep 2.mkv", "/x/Ep 10.mkv"))
        self.assertEqual(s.playlists, ("/x/list.m3u8",))
        self.assertEqual(s.subtitles, ("/x/a.srt",))

    def test_natural_key_ignores_case_and_folder(self):
        self.assertLess(
            open_paths.natural_key("/z/ep 2.mkv"), open_paths.natural_key("/a/EP 10.mkv")
        )

    def test_superscript_and_circled_digits_sort_as_text(self):
        # '²' and '①' are digits to str.isdigit but no decimal ones: int() refused them, and a
        # launch with such a file ended QiTV before its window showed (holds_the_start).
        odd = ["/x/1²2.mkv", "/x/²1 Film.mkv", "/x/①1.mp4", "C:\\Filmler\\Bölüm ③.mkv"]
        for path in odd:
            with self.subTest(path=path):
                open_paths.natural_key(path)  # no ValueError
        kinds = open_paths.sort_paths([*odd, "/x/1.mkv"])
        self.assertEqual(sorted(kinds.media), sorted([*odd, "/x/1.mkv"]))
        self.assertTrue(open_paths.holds_the_start(odd))
        # 1²2: the 1, then "²" as text, then the 2
        key = open_paths.natural_key("/x/1²2.mkv")
        self.assertEqual(key[:3], ((0, 1, ""), (1, 0, "²"), (0, 2, "")))

    def test_decimal_digits_of_other_scripts_count_as_numbers(self):
        # Arabic-Indic and fullwidth digits are decimal (\d matches them, int() reads them).
        key = open_paths.natural_key
        self.assertLess(key("/x/Bölüm ٣.mkv"), key("/x/Bölüm ١٠.mkv"))
        self.assertLess(key("/x/Ep 2.mkv"), key("/x/Ep １２.mkv"))
        mixed = open_paths.sort_paths(["/x/Ep １٠.mkv", "/x/Ep 9.mkv"])  # one run: ten
        self.assertEqual(mixed.media, ("/x/Ep 9.mkv", "/x/Ep １٠.mkv"))

    def test_kinds(self):
        self.assertTrue(open_paths.is_video("/x/A.MKV"))
        self.assertTrue(open_paths.is_music("/x/b.flac"))
        # A song (a playlist of these is an album): MUSIC_EXTENSIONS only, in any case.
        self.assertTrue(open_paths.is_song("/x/C.FLAC"))
        self.assertTrue(open_paths.is_song("C:\\Müzik\\d.Opus"))
        for path in ("/x/a.f4a", "/x/b.mp2", "/x/c.mkv", "/x/mp3"):
            with self.subTest(path=path):
                self.assertFalse(open_paths.is_song(path))
        self.assertFalse(open_paths.is_video("/x/b.flac"))
        self.assertTrue(open_paths.is_video("/x/c.ts"))

    def test_media_is_both_kinds(self):
        self.assertEqual(
            open_paths.MEDIA_EXTENSIONS, open_paths.VIDEO_EXTENSIONS | open_paths.MUSIC_EXTENSIONS
        )

    def test_what_the_claimed_types_bring_plays_too(self):
        # A type QiTV registers for covers these as well (macOS's QuickTime movie is .mov and
        # .qt; its FLV video .flv, .f4v, .f4p, .f4a and .f4b): the system may hand them over.
        videos = ["/x/a.qt", "/x/b.MPG4", "/x/c.vfw", "/x/d.mpe", "/x/e.m75", "/x/f.m15"]
        videos += ["/x/Dizi Bölüm 1.f4v", "/x/h.f4p"]
        for path in videos:
            with self.subTest(path=path):
                self.assertTrue(open_paths.is_video(path))
                self.assertFalse(open_paths.is_music(path))
        for path in ("/x/a.f4a", "/x/b.F4B"):
            with self.subTest(path=path):
                self.assertTrue(open_paths.is_music(path))
                self.assertFalse(open_paths.is_video(path))
        kinds = open_paths.sort_paths([*videos, "/x/a.f4a", "/x/b.F4B"])
        self.assertEqual(len(kinds.media), 10)
        self.assertEqual(kinds.others, ())
        # what QiTV registers for, and the Open File filter, stay as they were
        self.assertNotIn("qt", open_paths.VIDEO_EXTENSIONS)
        self.assertNotIn("f4a", open_paths.MEDIA_EXTENSIONS)

    def test_what_linuxs_mime_types_and_macos_ogg_bring_plays_too(self):
        # shared-mime-info's globs for the MIME types QiTV registers on Linux: video/mpeg
        # (.vob, .mp2), video/vnd.avi (.avf, .divx), video/quicktime (.moov, .qtvr), video/mp4
        # (.lrv), video/mp2t (.m2t); and macOS's Ogg video (.ogm). They got "QiTV can't play".
        videos = ["/x/DVD Bölüm 1.VOB", "/x/b.avf", "/x/c.divx", "/x/d.moov", "/x/e.qtvr"]
        videos += ["/x/f.lrv", "/x/g.m2t", "/x/h.ogm"]
        for path in videos:
            with self.subTest(path=path):
                self.assertTrue(open_paths.is_video(path))
                self.assertFalse(open_paths.is_music(path))
        # .mp2 is MPEG audio layer 2 (audio/mp2 too): music
        self.assertTrue(open_paths.is_music("/x/Şarkı.mp2"))
        self.assertFalse(open_paths.is_video("/x/Şarkı.mp2"))
        kinds = open_paths.sort_paths([*videos, "/x/Şarkı.mp2"])
        self.assertEqual((len(kinds.media), kinds.others), (9, ()))
        self.assertTrue(open_paths.holds_the_start(["/x/f.lrv"]))
        # what QiTV registers for (services/file_types.py) and the Open File filter stay
        self.assertEqual(
            open_paths.VIDEO_EXTENSIONS,
            {"mp4", "m4v", "mkv", "avi", "mov", "webm", "ts", "m2ts", "mts", "mpg", "mpeg"}
            | {"flv", "wmv", "ogv"},
        )
        self.assertEqual(
            open_paths.MUSIC_EXTENSIONS, {"mp3", "m4a", "aac", "flac", "ogg", "opus", "wav"}
        )

    def test_others_are_the_files_qitv_wont_play(self):
        s = open_paths.sort_paths(["/x/notes.txt", "/x/a.srt", "/x/b.mkv", "/x/c", "/x/l.m3u"])
        self.assertEqual(s.others, ("/x/notes.txt", "/x/c"))
        self.assertEqual(s.media, ("/x/b.mkv",))

    def test_a_start_holds_for_a_film_or_a_file_qitv_wont_play(self):
        # held: a film plays, or the message about the file comes, before anything else;
        # subtitle and playlist files alone start as they always did
        self.assertTrue(open_paths.holds_the_start(["/x/film.qt"]))
        self.assertTrue(open_paths.holds_the_start(["/x/notes.txt"]))
        self.assertTrue(open_paths.holds_the_start(["/x/a.srt", "/x/notes.txt"]))
        self.assertFalse(open_paths.holds_the_start(["/x/a.srt"]))
        self.assertFalse(open_paths.holds_the_start(["/x/list.m3u", "/x/b.m3u8"]))
        self.assertFalse(open_paths.holds_the_start([]))

    def test_a_start_waits_for_its_playlists_when_it_carries_no_film(self):
        # Ruling P4-3: read first (a list of files holds the start, a provider's doesn't);
        # with a film the film plays, and its playlists only add their providers.
        self.assertTrue(open_paths.reads_the_start(["/x/list.m3u"]))
        self.assertTrue(open_paths.reads_the_start(["/x/a.srt", "/x/B.M3U8"]))
        self.assertTrue(open_paths.reads_the_start(["/x/notes.txt", "/x/list.m3u"]))
        self.assertFalse(open_paths.reads_the_start(["/x/film.mkv", "/x/list.m3u"]))
        self.assertFalse(open_paths.reads_the_start(["/x/a.srt", "/x/notes.txt"]))
        self.assertFalse(open_paths.reads_the_start([]))


class Filter(unittest.TestCase):
    def test_open_file_patterns_are_unchanged(self):
        from ui.tv_pages import MEDIA_PATTERNS

        today = (
            "*.mp4 *.m4v *.mkv *.avi *.mov *.webm *.ts *.m2ts *.mts *.mpg *.mpeg *.flv"
            " *.wmv *.ogv *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wav *.m3u8"
        )
        self.assertEqual(set(MEDIA_PATTERNS.split()), set(today.split()))

    def test_subtitle_extensions_still_exported_by_the_ui(self):
        from ui import track_controls

        self.assertIs(track_controls.SUBTITLE_EXTENSIONS, open_paths.SUBTITLE_EXTENSIONS)


class ExtendedPath(unittest.TestCase):
    r"""Windows' extended-length spelling (`\\?\`), for a path past 260 characters in a QiTV
    that isn't long-path aware; worked out with ntpath, so on any system."""

    def test_a_drive_path(self):
        self.assertEqual(
            open_paths.extended_path(r"C:\Films\Dizi Bölüm 1.mkv"), r"\\?\C:\Films\Dizi Bölüm 1.mkv"
        )

    def test_forward_slashes_and_dots_are_written_out(self):
        # The prefix turns Windows' own clean-up off: the path is spelled out first.
        self.assertEqual(
            open_paths.extended_path("C:/Films/Sezon 1/../Ep 1.mkv"), r"\\?\C:\Films\Ep 1.mkv"
        )

    def test_a_unc_path(self):
        expected = r"\\?\UNC\server\share\Dizi\Ep 1.mkv"
        self.assertEqual(open_paths.extended_path(r"\\server\share\Dizi\Ep 1.mkv"), expected)
        self.assertEqual(open_paths.extended_path("//server/share/Dizi/Ep 1.mkv"), expected)

    def test_a_prefixed_path_stays(self):
        for path in (r"\\?\C:\Films\Ep 1.mkv", r"\\?\UNC\server\share\Ep 1.mkv"):
            with self.subTest(path=path):
                self.assertEqual(open_paths.extended_path(path), path)


if __name__ == "__main__":
    unittest.main()
