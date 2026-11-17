"""Paths and their kinds: launch arguments, sorting, media types."""

import os
from pathlib import Path
import tempfile
import unittest

from services import open_paths


class LaunchPaths(unittest.TestCase):
    def test_skips_flags_and_replace_value(self):
        with tempfile.TemporaryDirectory() as d:
            film = Path(d, "Dizi Bölüm 1.mkv")
            film.write_bytes(b"x")
            old = Path(d, "old.exe")
            old.write_bytes(b"x")
            argv = ["qitv", "--replace", str(old), "-psn_0_12345", str(film), "missing.mkv"]
            self.assertEqual(open_paths.launch_paths(argv), [str(film.resolve())])

    def test_relative_paths_become_absolute(self):
        with tempfile.TemporaryDirectory() as d:
            film = Path(d, "a.mp4")
            film.write_bytes(b"x")
            before = os.getcwd()
            os.chdir(d)
            try:
                self.assertEqual(open_paths.launch_paths(["qitv", "a.mp4"]), [str(film.resolve())])
            finally:
                os.chdir(before)

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

    def test_kinds(self):
        self.assertTrue(open_paths.is_video("/x/A.MKV"))
        self.assertTrue(open_paths.is_music("/x/b.flac"))
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
