import os
import tempfile
import unittest
from unittest import mock

from services.local_episodes import list_folder, next_in_folder, parse_episode


def _listing(*names):
    return [(n, 100 + i) for i, n in enumerate(names)]


class ParseTests(unittest.TestCase):
    def test_season_and_episode_forms(self):
        first = parse_episode("Taşlı Dere Postası.S01E02.1080p.mkv")
        assert first is not None
        self.assertEqual((first.season, first.first, first.last), (1, 2, 2))
        self.assertEqual(first.show, "tasli dere postasi")
        self.assertEqual(first.title, "Taşlı Dere Postası")
        for name in (
            "tasli_dere_postasi_s01e02.mkv",
            "Taşlı Dere Postası - S01 E02.mp4",
            "Taşlı.Dere.Postası.S01.E02.mkv",
            "TAŞLI DERE POSTASI s1e2.avi",
            "Taşlı Dere Postası 1x02.mkv",
        ):
            with self.subTest(name=name):
                ep = parse_episode(name)
                assert ep is not None
                self.assertEqual((ep.season, ep.first, ep.last), (1, 2, 2))
                self.assertEqual(ep.show, "tasli dere postasi")

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
        a = parse_episode("İpek Şırıl Işığı.S01E01.mkv")
        b = parse_episode("İPEK ŞIRIL IŞIĞI - s01e02.mkv")
        assert a is not None and b is not None
        self.assertEqual(a.show, b.show)
        self.assertEqual(a.show, "ipek siril isigi")

    def test_bracketed_group_is_not_in_the_title(self):
        ep = parse_episode("[Grp] Show - S01E02.mkv")
        assert ep is not None
        self.assertEqual(ep.title, "Show")
        ep = parse_episode("(Grp) Show - S01E02.mkv")
        assert ep is not None
        self.assertEqual(ep.title, "Show")

    def test_dash_double_episode_only_when_consecutive(self):
        ep = parse_episode("Show.S01E01-02.mkv")
        assert ep is not None
        self.assertEqual((ep.first, ep.last), (1, 2))
        for name in ("Show.S01E02-720p.mkv", "Show.S01E02-2019.mkv", "Show.S01E02-03x.mkv"):
            with self.subTest(name=name):
                ep = parse_episode(name)
                assert ep is not None
                self.assertEqual((ep.first, ep.last), (2, 2))

    def test_bare_name_and_s00e00(self):
        ep = parse_episode("S01E02.mkv")
        assert ep is not None
        self.assertEqual((ep.show, ep.title, ep.season, ep.first), ("", "S01E02", 1, 2))
        ep = parse_episode("Show.S00E00.mkv")
        assert ep is not None
        self.assertEqual((ep.season, ep.first, ep.last), (0, 0, 0))


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

    def test_the_last_episode_of_the_last_season_has_none(self):
        listing = _listing("Show.S01E01.mkv", "Show.S02E01.mkv", "Show.S02E02.mkv")
        self.assertIsNone(next_in_folder("/m/Show.S02E02.mkv", listing))

    def test_next_across_spellings(self):
        nxt = next_in_folder("/m/Show.S01E02.mkv", _listing("show_s01e03.mkv"))
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/show_s01e03.mkv")

    def test_a_hidden_companion_is_never_the_next(self):
        listing = _listing("._Show.S01E02.mkv", ".Show.S01E02.mkv", "Show.S01E02.mkv")
        nxt = next_in_folder("/m/Show.S01E01.mkv", listing)
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/Show.S01E02.mkv")

    def test_bare_names_link_only_in_the_sxxeyy_form(self):
        # Was P3-3 (an empty key links only SxxEyy to SxxEyy). Now a bare SxxEyy takes its
        # folder's show and links under the same-show rule; a bare 1x02 still never links.
        nxt = next_in_folder("/m/S01E01.mkv", _listing("S01E02 - Title.mkv"))
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/S01E02 - Title.mkv")
        self.assertIsNone(next_in_folder("/m/S01E01.mkv", _listing("1x02 Other.mkv")))
        self.assertIsNone(next_in_folder("/m/1x01.mkv", _listing("1x02.mkv")))


class SameShowTests(unittest.TestCase):
    LONG = "The.Long.Show.Title.A.Name.Story.S04E08.1080p.SRC.WEB-DL.H.265.DDP5.1-GRP.mkv"
    SHORT = "The.Long.Show.Title.S04E09.720p.WEB-DL.x264-OTHER.mkv"

    def test_a_shorter_spelling_of_the_same_show_is_the_same_show(self):
        nxt = next_in_folder("/m/" + self.LONG, _listing(self.SHORT))
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/" + self.SHORT)
        short_first = "The.Long.Show.Title.S04E08.720p.mkv"
        longer_next = "The.Long.Show.Title.A.Name.Story.S04E09.1080p.mkv"
        nxt = next_in_folder("/m/" + short_first, _listing(longer_next))
        assert nxt is not None
        self.assertEqual(nxt["local_path"], "/m/" + longer_next)

    def test_one_shared_word_is_not_the_same_show(self):
        self.assertIsNone(next_in_folder("/m/Show.S01E01.mkv", _listing("Show.Name.S01E02.mkv")))
        self.assertIsNone(next_in_folder("/m/Show.Name.S01E01.mkv", _listing("Show.S01E02.mkv")))
        self.assertIsNone(next_in_folder("/m/The.Cat.S01E01.mkv", _listing("The.Dog.S01E02.mkv")))
        self.assertIsNone(next_in_folder("/m/The.S01E01.mkv", _listing("The.Cat.S01E02.mkv")))

    def test_bare_names_take_the_folders_show(self):
        for folder in ("/Shows/Long Show Title", "/Shows/Long Show Title/Season 4"):
            with self.subTest(folder=folder):
                cur = folder + "/S04E08.mkv"
                nxt = next_in_folder(cur, _listing("S04E09 - Title.mkv"))
                assert nxt is not None
                self.assertEqual(nxt["local_path"], folder + "/S04E09 - Title.mkv")
                named = "Long.Show.Title.S04E09.720p.mkv"
                nxt = next_in_folder(cur, _listing(named))
                assert nxt is not None
                self.assertEqual(nxt["local_path"], folder + "/" + named)
                nxt = next_in_folder(
                    folder + "/Long.Show.Title.A.Name.S04E08.mkv", _listing("S04E09.mkv")
                )
                assert nxt is not None
                self.assertEqual(nxt["local_path"], folder + "/S04E09.mkv")

    def test_a_bare_name_in_an_unrelated_folder_never_links_to_another_show(self):
        self.assertIsNone(
            next_in_folder("/Downloads/S01E01.mkv", _listing("Other.Show.S01E02.mkv"))
        )
        self.assertIsNone(
            next_in_folder("/Downloads/Other.Show.S01E01.mkv", _listing("S01E02.mkv"))
        )

    def test_bare_nxnn_still_never_links(self):
        folder = "/Shows/Long Show Title"
        self.assertIsNone(next_in_folder(folder + "/1x01.mkv", _listing("1x02.mkv")))
        self.assertIsNone(next_in_folder(folder + "/S01E01.mkv", _listing("1x02 Other.mkv")))
        self.assertIsNone(
            next_in_folder(folder + "/Long.Show.Title.S01E01.mkv", _listing("1x02.mkv"))
        )

    def test_specials_is_not_a_season_folder(self):
        # "Specials" is the show here, so the bare name does not take "Long Show Title".
        cur = "/Shows/Long Show Title/Specials/S00E01.mkv"
        self.assertIsNone(next_in_folder(cur, _listing("Long.Show.Title.S00E02.mkv")))
        nxt = next_in_folder(cur, _listing("S00E02.mkv"))
        assert nxt is not None

    def test_season_folder_spellings(self):
        for name in ("Season 4", "season 04", "S04", "Sezon 4", "Staffel 4", "SAISON 4", "Season"):
            with self.subTest(name=name):
                cur = "/Shows/Long Show Title/" + name + "/S04E08.mkv"
                nxt = next_in_folder(cur, _listing("Long.Show.Title.S04E09.mkv"))
                self.assertIsNotNone(nxt)


class ListFolderTests(unittest.TestCase):
    def test_only_videos_are_offered(self):
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("Show.S01E02.srt", "Show.S01E02.nfo", "Show.S01E02.jpg"):
                open(os.path.join(tmp, n), "wb").close()
            os.mkdir(os.path.join(tmp, "Show.S01E02.mkv"))
            for n in ("._Show.S01E02.mkv", ".Show.S01E02.mkv"):
                open(os.path.join(tmp, n), "wb").close()
            self.assertEqual(list_folder(tmp), [])
            os.rmdir(os.path.join(tmp, "Show.S01E02.mkv"))
            with open(os.path.join(tmp, "Show.S01E02.mkv"), "wb") as f:
                f.write(b"12345")
            self.assertEqual(list_folder(tmp), [("Show.S01E02.mkv", 5)])

    def test_an_unreadable_folder_is_empty_and_its_path_is_not_logged(self):
        with self.assertLogs("services.local_episodes", level="DEBUG") as logs:
            self.assertEqual(list_folder("/no/such/secret-folder"), [])
        self.assertNotIn("secret-folder", "\n".join(logs.output))
        self.assertNotIn("secret-folder", str([r.exc_text for r in logs.records]))

    def test_size_is_none_when_stat_fails(self):
        class Entry:
            name = "Show.S01E02.mkv"

            def is_file(self):
                return True

            def stat(self):
                raise OSError("gone")

        class Scan(list):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("os.scandir", return_value=Scan([Entry()])):
            self.assertEqual(list_folder("/m"), [("Show.S01E02.mkv", None)])


if __name__ == "__main__":
    unittest.main()
