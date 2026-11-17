import os
import tempfile
import unittest

from services.local_episodes import list_folder, next_in_folder, parse_episode


def _listing(*names):
    return [(n, 100 + i) for i, n in enumerate(names)]


class ParseTests(unittest.TestCase):
    def test_season_and_episode_forms(self):
        first = parse_episode("Kuzey Yolu.S01E02.1080p.mkv")
        assert first is not None
        self.assertEqual((first.season, first.first, first.last), (1, 2, 2))
        self.assertEqual(first.show, "kuzey yolu")
        self.assertEqual(first.title, "Kuzey Yolu")
        for name in (
            "kuzey_yolu_s01e02.mkv",
            "Kuzey Yolu - S01 E02.mp4",
            "Kuzey.Yolu.S01.E02.mkv",
            "KUZEY YOLU s1e2.avi",
            "Kuzey Yolu 1x02.mkv",
        ):
            with self.subTest(name=name):
                ep = parse_episode(name)
                assert ep is not None
                self.assertEqual((ep.season, ep.first, ep.last), (1, 2, 2))
                self.assertEqual(ep.show, "kuzey yolu")

    def test_double_episode(self):
        for name in ("Show.S02E05E06.mkv", "Show.S02E05-E06.mkv"):
            with self.subTest(name=name):
                ep = parse_episode(name)
                assert ep is not None
                self.assertEqual((ep.season, ep.first, ep.last), (2, 5, 6))

    def test_never_an_episode(self):
        for name in (
            "Film.1920x1080.mkv",
            "Film 1280x720.mp4",
            "Film.720x576.avi",
            "Film.x264.mkv",
            "Film.x265.mkv",
            "Film.h264.mkv",
            "Film (2019).mkv",
            "Holiday 2019-06-01.mp4",
            "BOSS01E02.mkv",
            "Film.5x3.mkv",
        ):
            with self.subTest(name=name):
                self.assertIsNone(parse_episode(name))

    def test_show_key_ignores_case_accents_and_separators(self):
        a = parse_episode("Kızılcık.Şerbeti.S01E01.mkv")
        b = parse_episode("KIZILCIK ŞERBETİ - s01e02.mkv")
        assert a is not None and b is not None
        self.assertEqual(a.show, b.show)


class NextTests(unittest.TestCase):
    def test_next_is_the_following_episode(self):
        listing = _listing("Show.S01E01.mkv", "Show.S01E02.mkv", "Other.S01E02.mkv")
        nxt = next_in_folder("/media/Show.S01E01.mkv", listing)
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/media/Show.S01E02.mkv")
        self.assertEqual((nxt["season"], nxt["number"]), (1, 2))
        self.assertEqual(nxt["name"], "Show")
        self.assertEqual(nxt["size"], 101)

    def test_next_season_only_after_the_seasons_last(self):
        listing = _listing("Show.S01E10.mkv", "Show.S02E01.mkv")
        nxt = next_in_folder("/m/Show.S01E10.mkv", listing)
        assert nxt is not None
        self.assertEqual((nxt["season"], nxt["number"]), (2, 1))
        gap = _listing("Show.S01E02.mkv", "Show.S01E04.mkv", "Show.S02E01.mkv")
        self.assertIsNone(next_in_folder("/m/Show.S01E02.mkv", gap))

    def test_a_mixed_folder_never_jumps_shows(self):
        listing = _listing("Show.S01E02.mkv", "Other.S01E03.mkv")
        self.assertIsNone(next_in_folder("/m/Show.S01E02.mkv", listing))

    def test_two_copies_of_the_next_episode(self):
        listing = _listing("Show.S01E01.mkv", "Show.S01E02.1080p.mkv", "Show.S01E02.720p.mkv")
        nxt = next_in_folder("/m/Show.S01E01.mkv", listing)
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/Show.S01E02.720p.mkv")

    def test_not_an_episode_has_no_next(self):
        self.assertIsNone(next_in_folder("/m/Holiday.mkv", _listing("Show.S01E02.mkv")))

    def test_double_episode_continues_after_its_last(self):
        listing = _listing("Show.S01E01E02.mkv", "Show.S01E03.mkv")
        nxt = next_in_folder("/m/Show.S01E01E02.mkv", listing)
        assert nxt is not None
        self.assertEqual(nxt["number"], 3)


class ListFolderTests(unittest.TestCase):
    def test_only_videos_are_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("Show.S01E02.srt", "Show.S01E02.nfo", "Show.S01E02.jpg"):
                open(os.path.join(tmp, n), "wb").close()
            os.mkdir(os.path.join(tmp, "Show.S01E02.mkv"))
            self.assertEqual(list_folder(tmp), [])
            os.rmdir(os.path.join(tmp, "Show.S01E02.mkv"))
            with open(os.path.join(tmp, "Show.S01E02.mkv"), "wb") as f:
                f.write(b"12345")
            self.assertEqual(list_folder(tmp), [("Show.S01E02.mkv", 5)])

    def test_an_unreadable_folder_is_empty(self):
        self.assertEqual(list_folder("/no/such/folder"), [])


if __name__ == "__main__":
    unittest.main()
