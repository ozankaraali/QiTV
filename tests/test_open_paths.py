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


if __name__ == "__main__":
    unittest.main()
