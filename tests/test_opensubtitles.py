"""OpenSubtitles (2.2): what is searched for, what is found and how it ranks, downloads, the
sign-in, and every problem said in plain words. HTTP is mocked: no key, no network."""

import logging
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QLocale
from opensubtitles_fixtures import (
    API,
    LINK,
    SRT,
    FakeHttp,
    FakeResponse,
    download_answer,
    entry,
    feature,
    login_answer,
    page,
    quota_answer,
    serving,
)
import requests

from services import _build_keys, opensubtitles
from services.opensubtitles import (
    BAD_KEY,
    BAD_LOGIN,
    BUSY,
    GONE,
    NETWORK,
    NO_KEY,
    QUOTA,
    SERVER,
    Credentials,
    Downloaded,
    DownloadWorker,
    Feature,
    OpenSubtitlesClient,
    OpenSubtitlesError,
    Problem,
    SearchQuery,
    SearchResult,
    SearchWorker,
    SubtitleFile,
    api_key,
    clean_title,
    feature_matches,
    found_for,
    group_languages,
    language_name,
    original_title,
    parse_query,
    parse_search,
    provider_ids,
    ranked,
    safe_file_name,
    same_title,
    search_query,
    split_matches,
    system_language,
)

KEY = "fictional-api-key-0001"
USER, PASSWORD = "harbour_viewer", "fictional-password"


def client(http, *, username="", password="", key=KEY):
    return OpenSubtitlesClient(Credentials(key, username, password), http=http)


class TitleTests(unittest.TestCase):
    def test_a_providers_title_loses_its_prefixes_tags_and_quality(self):
        cases = {
            "TR: Harbour Lights (2019) 4K": ("Harbour Lights", 2019),
            "|EN| Night Ferry 1080p": ("Night Ferry", None),
            "[DE] Der Leuchtturm - 2015": ("Der Leuchtturm", 2015),
            "FR| Le Phare Blanc": ("Le Phare Blanc", None),
            "TR: Lighthouse": ("Lighthouse", None),  # one word, long enough
            "4K-TR: Salt Road": ("Salt Road", None),
            "(US) Salt Road": ("Salt Road", None),
            "Harbour Lights (2019) [MULTI-SUB]": ("Harbour Lights", 2019),
            "(500) Lanterns": ("(500) Lanterns", None),  # a number in brackets is the title's
            "Tower Clock 2049": ("Tower Clock 2049", None),  # not a year: the future's
            "Wonder Harbour 1984": ("Wonder Harbour 1984", None),  # a provider's: kept whole
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), expected)

    def test_only_known_codes_in_their_forms_are_taken_for_a_providers_tag(self):
        cases = {
            "[REC] 2": "[REC] 2",  # brackets that are the title's own
            "Harbour (Director's Cut)": "Harbour (Director's Cut)",
            "IT: Up": "IT: Up",  # never down to a single short word
            "TR - Up": "TR - Up",
            "NF: Salt Road": "NF: Salt Road",  # not a country or language code
            "NF - Salt Road": "NF - Salt Road",
            "CSI: Harbour Nights": "CSI: Harbour Nights",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), (expected, None))

    def test_a_language_tag_goes_in_every_shape_before_or_after_the_title(self):
        cases = {
            "TR - Süper Liman Bekçileri (2014)": ("Süper Liman Bekçileri", 2014),
            "TR – Harbour Lights": ("Harbour Lights", None),
            "TR — Harbour Lights": ("Harbour Lights", None),
            "TR- Harbour Lights": ("Harbour Lights", None),
            "TR | Harbour Lights": ("Harbour Lights", None),
            "TR:Harbour Lights": ("Harbour Lights", None),
            "TR▸ Harbour Lights": ("Harbour Lights", None),
            "TR ▸ Harbour Lights": ("Harbour Lights", None),
            "[TR] Harbour Lights": ("Harbour Lights", None),
            "[TR] - Harbour Lights": ("Harbour Lights", None),
            "(TR) Harbour Lights": ("Harbour Lights", None),
            "|TR| Harbour Lights": ("Harbour Lights", None),
            "EN - Quiet Valley": ("Quiet Valley", None),
            "TR - 4K - Harbour Lights (2019)": ("Harbour Lights", 2019),
            "Harbour Lights - TR": ("Harbour Lights", None),
            "Harbour Lights [TR]": ("Harbour Lights", None),
            "Harbour Lights | EN": ("Harbour Lights", None),
            "Harbour Lights (2019) - TR": ("Harbour Lights", 2019),
            "Harbour Lights - TR - 2019": ("Harbour Lights", 2019),
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), expected)

    def test_titles_that_start_or_end_with_short_words_keep_them(self):
        cases = (
            "Up",
            "It",
            "Us",
            "ET",
            "Z3RO",
            "It - Harbour Two",  # a title's own words are not all capitals
            "Us - The Return",
            "DE-FROST",  # a word's hyphen, not a tag's dash
            "Harbour - IV",  # not a language
            "Harbour Lights TR",  # no separator: may be the title's
            "Salt Road - It",
        )
        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(clean_title(title), (title, None))

    def test_a_files_name_loses_its_release_tags(self):
        self.assertEqual(
            clean_title("Harbour.Lights.2019.1080p.WEB-DL.x264-GROUP.mkv"), ("Harbour Lights", 2019)
        )
        self.assertEqual(clean_title("Tower.Clock.2049.2017.1080p.mkv"), ("Tower Clock 2049", 2017))

    def test_a_film_is_searched_by_its_title_and_year(self):
        query = search_query("movies", "TR: Harbour Lights 4K", item={"year": "2019"})
        self.assertEqual(query, SearchQuery("Harbour Lights", 2019))
        self.assertEqual(query.label(), "Harbour Lights (2019)")
        self.assertEqual(
            search_query("movies", "Harbour Lights", item={}).label(), "Harbour Lights"
        )

    def test_a_film_is_searched_by_the_id_its_provider_gives(self):
        item = {"name": "TR - Süper Liman Bekçileri (2014)", "tmdb": "4242", "year": "2014"}
        query = search_query("movies", item["name"], item=item)
        self.assertEqual(query, SearchQuery("Süper Liman Bekçileri", 2014, tmdb_id=4242))
        self.assertEqual(query.label(), "Süper Liman Bekçileri (2014)")  # the field: cleaned
        self.assertTrue(query.by_id)
        # In an item's info, and an IMDb id with or without its "tt".
        self.assertEqual(provider_ids({"info": {"tmdb_id": 17}}), (17, None))
        self.assertEqual(provider_ids({"imdb_id": "tt0123456"}), (None, 123456))
        self.assertEqual(provider_ids({"imdb": "0123456"}), (None, 123456))
        for nothing in ({"tmdb": "0"}, {"tmdb": "n/a"}, {"imdb": "7.5"}, {"imdb": "8"}, {}):
            with self.subTest(item=nothing):
                self.assertEqual(provider_ids(nothing), (None, None))  # a rating is no id

    def test_a_films_original_title_in_latin_letters_is_searched_for(self):
        item = {"name": "TR: Liman Işıkları", "o_name": "Harbour Lights (2019)"}
        self.assertEqual(
            search_query("movies", item["name"], item=item), SearchQuery("Harbour Lights", 2019)
        )
        foreign = {"name": "TR: Liman Işıkları", "o_name": "港の灯り"}
        self.assertEqual(original_title(foreign), "")
        self.assertEqual(
            search_query("movies", foreign["name"], item=foreign).text, "Liman Işıkları"
        )

    def test_an_episode_is_searched_by_its_series_id_and_numbers(self):
        context = {"series": {"name": "TR - Tuz Yolu", "tmdb": "77"}, "season": {"number": "2"}}
        item = {"episode_num": "5", "description": {"tmdb_id": 9001}}  # an Xtream episode's info
        query = search_query(
            "series", "Köprü", series_title="TR - Tuz Yolu", item=item, context=context
        )
        self.assertEqual(
            query, SearchQuery("Tuz Yolu", None, 2, 5, tmdb_id=9001, parent_tmdb_id=77)
        )
        self.assertEqual(query.label(), "Tuz Yolu S02E05")
        # The series' id from its info (services.workers puts it on each season).
        context = {"series": {"name": "Salt Road"}, "season": {"number": 2, "series_tmdb": "77"}}
        query = search_query("series", "x", item={"episode_num": 5}, context=context)
        self.assertEqual((query.parent_tmdb_id, query.tmdb_id), (77, None))
        # A provider that gives each episode its series' id: it is the series'.
        context = {"series": {"name": "Salt Road", "tmdb": "77"}, "season": {"number": 2}}
        query = search_query("series", "x", item={"episode_num": 5, "tmdb": 77}, context=context)
        self.assertEqual((query.parent_tmdb_id, query.tmdb_id), (77, None))
        # Its own id without its series or numbers could be a film's: its text is searched.
        query = search_query("series", "Salt Road", item={"info": {"tmdb_id": 9001}})
        self.assertFalse(query.by_id)

    def test_an_episode_is_searched_by_its_series_season_and_number(self):
        context = {"series": {"name": "|EN| Salt Road"}, "season": {"number": "2"}}
        query = search_query(
            "series",
            "The Bridge",
            series_title="|EN| Salt Road (2020)",
            item={"episode_num": "5"},
            context=context,
        )
        self.assertEqual(query, SearchQuery("Salt Road", None, 2, 5))
        self.assertEqual(query.label(), "Salt Road S02E05")

    def test_an_episode_without_its_series_reads_its_own_title(self):
        query = search_query("series", "Salt Road - S03E04 - The Bridge", item={})
        self.assertEqual(query.label(), "Salt Road S03E04")
        # A portal's episode: the series' own fields, its number under "series".
        stb = search_query("series", "Episode 7", series_title="Salt Road", item={"series": 7})
        self.assertEqual((stb.text, stb.episode), ("Salt Road", 7))
        named = search_query("series", "x", series_title="Salt Road Season 2", item={"number": 3})
        self.assertEqual(named.label(), "Salt Road S02E03")

    def test_a_file_named_after_an_episode_is_searched_as_one(self):
        query = search_query("movies", "Salt.Road.S01E02.Pilot.720p.mkv")
        self.assertEqual(query, SearchQuery("Salt Road", None, 1, 2))

    def test_the_field_is_read_back(self):
        self.assertEqual(parse_query("Salt Road S01E02"), SearchQuery("Salt Road", None, 1, 2))
        self.assertEqual(parse_query("Harbour Lights (2019)"), SearchQuery("Harbour Lights", 2019))
        self.assertEqual(parse_query("  harbour   lights "), SearchQuery("harbour lights"))

    def test_an_id_is_searched_for_instead_of_the_text(self):
        film = SearchQuery("Süper Liman Bekçileri", 2014, tmdb_id=4242)
        self.assertEqual(
            film.params(("tr",)), [("languages", "tr"), ("tmdb_id", "4242"), ("type", "movie")]
        )
        both = SearchQuery("Harbour Lights", 2019, tmdb_id=4242, imdb_id=123456)
        self.assertEqual(both.params(), [("imdb_id", "123456"), ("type", "movie")])
        episode = SearchQuery("Salt Road", None, 2, 5, tmdb_id=9001, parent_tmdb_id=77)
        self.assertEqual(
            episode.params(),
            [
                ("episode_number", "5"),
                ("parent_tmdb_id", "77"),
                ("season_number", "2"),
                ("type", "episode"),
            ],
        )
        # Without its number, an episode's own id names it.
        unnumbered = SearchQuery("Salt Road", None, 2, None, tmdb_id=9001, parent_tmdb_id=77)
        self.assertEqual(unnumbered.params(), [("tmdb_id", "9001"), ("type", "episode")])
        self.assertEqual(episode.without_ids(), SearchQuery("Salt Road", None, 2, 5))

    def test_the_year_is_never_in_the_text_too(self):
        self.assertEqual(
            SearchQuery("Harbour Lights 2019", 2019).params(),
            [("query", "harbour lights"), ("year", "2019")],
        )
        self.assertEqual(SearchQuery("1984", 1984).params(), [("query", "1984"), ("year", "1984")])
        self.assertEqual(
            search_query("movies", "TR - Süper Liman Bekçileri (2014)").params(),
            [("query", "süper liman bekçileri"), ("year", "2014")],
        )

    def test_parameters_are_sorted_and_lower_case(self):
        query = SearchQuery("Salt Road", None, 2, 5)
        self.assertEqual(
            query.params(("tr", "EN")),
            [
                ("episode_number", "5"),
                ("languages", "en,tr"),
                ("query", "salt road"),
                ("season_number", "2"),
            ],
        )


class FoundTests(unittest.TestCase):
    def test_a_search_answer_is_read(self):
        data = page(
            entry(11, "en", "Harbour.Lights.2019", downloads=900, trusted=True, rank="Trusted"),
            entry(12, "tr", "", downloads=5, ai=True, file_name="harbour.tr.srt"),
            {"attributes": {"language": "de", "files": []}},  # no file: left out
        )
        found = parse_search(data)
        self.assertEqual([f.file_id for f in found], [11, 12])
        first, second = found
        self.assertEqual(
            (first.language, first.release, first.downloads), ("en", "Harbour.Lights.2019", 900)
        )
        self.assertTrue(first.trusted)
        self.assertEqual(second.release, "harbour.tr.srt")  # no release: its file's name
        self.assertTrue(second.machine_translated)  # AI counts as a machine
        self.assertEqual((first.order, second.order), (0, 1))

    def test_each_subtitle_names_what_it_was_made_for(self):
        episode = feature(
            "The Bridge", 2020, tmdb=9001, series="Salt Road", season=2, episode=5, series_tmdb=77
        )
        data = page(
            entry(1, "en", "a", made_for=feature("Harbour Lights", 2019, tmdb=4242, imdb=1234)),
            entry(2, "en", "b", made_for=episode),
            entry(3, "en", "c"),
        )
        first, second, third = parse_search(data)
        self.assertEqual(
            first.feature,
            Feature("movie", "Harbour Lights", "2019 - Harbour Lights", "", 2019, 4242, 1234),
        )
        self.assertEqual(first.feature.label(), "Harbour Lights (2019)")
        self.assertEqual(
            (second.feature.parent_tmdb_id, second.feature.season, second.feature.episode),
            (77, 2, 5),
        )
        self.assertEqual(second.feature.label(), "Salt Road S02E05")
        self.assertIsNone(third.feature)

    def test_a_title_is_matched_by_its_words_without_case_or_accents(self):
        self.assertTrue(same_title("Süper Liman Bekçileri", "Super Liman Bekcileri"))
        self.assertTrue(same_title("ISTANBUL ışıkları", "İstanbul Işıkları"))
        self.assertTrue(same_title("Harbour Lights", "2019 - Harbour Lights"))
        self.assertTrue(same_title("The Harbour Lights", "Harbour Lights: The Return"))
        self.assertFalse(same_title("6 Süper Kahraman", "Six Brave Lanterns 6"))
        self.assertFalse(same_title("The Harbour", "The Lighthouse"))  # "the" tells nothing
        self.assertFalse(same_title("Up", "Up and Away Over the Northern Harbour Lights"))
        self.assertFalse(same_title("", "Harbour"))

    def test_only_the_subtitles_for_the_title_asked_for_are_kept(self):
        """A translated title and its year find other films of that year (reported in 2.2.0:
        64 subtitles, none for the film): none of them is taken for it."""
        query = search_query("movies", "TR - Süper Liman Bekçileri (2014)")
        data = page(
            entry(1, "tr", "grown.beginners.2014.tur", made_for=feature("Grown Beginners", 2014)),
            entry(2, "tr", "Star Corridor (2014) TR", made_for=feature("Star Corridor", 2014)),
            entry(3, "en", "Night.Ferry.2014", made_for=feature("Night Ferry", 2014)),
            entry(4, "tr", "WhoIsIt tr", made_for=feature("Who Is It", 2014)),
            entry(5, "tr", "liman.2014", made_for=feature("Super Liman Bekcileri", 2014)),
            entry(6, "tr", "liman.2009", made_for=feature("Süper Liman Bekçileri", 2009)),
        )
        matching, others = split_matches(parse_search(data), query)
        self.assertEqual([f.file_id for f in matching], [5])
        self.assertEqual([f.file_id for f in others], [1, 2, 3, 4, 6])  # 6: another year
        self.assertEqual(found_for(matching), "Super Liman Bekcileri (2014)")
        # Not one of them: nothing is for it.
        matching, others = split_matches(parse_search(page(*data["data"][:4])), query)
        self.assertEqual((matching, len(others)), ([], 4))
        # Without the features OpenSubtitles names, nothing tells them apart: all are kept.
        plain = parse_search(page(entry(7, "en", "a"), entry(8, "tr", "b")))
        self.assertEqual(split_matches(plain, query), (plain, []))

    def test_by_id_the_features_ids_count_and_the_year_checks_them(self):
        query = SearchQuery("Süper Liman Bekçileri", 2014, tmdb_id=4242)
        film = Feature("movie", "Brave Harbour Six", year=2014, tmdb_id=4242)
        self.assertTrue(feature_matches(film, query))  # its English title: the id says
        other = Feature("movie", "Other", year=2014, tmdb_id=5)
        self.assertFalse(feature_matches(other, query))
        wrong = Feature("movie", "Old", year=1990, tmdb_id=4242)  # a provider's wrong id
        self.assertFalse(feature_matches(wrong, query))
        untold = Feature("movie", "Süper Liman Bekçileri", year=2015)  # no ids: its title
        self.assertTrue(feature_matches(untold, query))
        episode = SearchQuery("Salt Road", None, 2, 5, parent_tmdb_id=77)
        bridge = Feature(
            "episode",
            "The Bridge",
            parent_title="Salt Road",
            parent_tmdb_id=77,
            season=2,
            episode=5,
        )
        pier = Feature(
            "episode", "The Pier", parent_title="Salt Road", parent_tmdb_id=77, season=2, episode=6
        )
        self.assertTrue(feature_matches(bridge, episode))
        self.assertFalse(feature_matches(pier, episode))
        # By text, an episode's series is its title.
        self.assertTrue(feature_matches(bridge, episode.without_ids()))

    def test_languages_group_with_the_preferred_first(self):
        files = [
            SubtitleFile(i, lang, "r") for i, lang in enumerate("en en en tr tr de pt-BR".split())
        ]
        self.assertEqual(
            group_languages(files, ["de", "fr", "tr"]),
            [("de", 1), ("tr", 2), ("en", 3), ("pt-BR", 1)],
        )

    def test_ranking_within_a_language(self):
        files = [
            SubtitleFile(1, "en", "machine", downloads=9000, machine_translated=True, order=0),
            SubtitleFile(2, "en", "hi", downloads=8000, hearing_impaired=True, order=1),
            SubtitleFile(3, "en", "anonymous", downloads=7000, uploader_rank="anonymous", order=2),
            SubtitleFile(4, "en", "ranked", downloads=10, uploader_rank="Gold member", order=3),
            SubtitleFile(5, "en", "trusted", downloads=5, trusted=True, order=4),
            SubtitleFile(6, "en", "anonymous too", downloads=7000, order=5),
        ]
        names = [f.release for f in ranked(files)]
        self.assertEqual(
            names, ["trusted", "ranked", "anonymous", "anonymous too", "hi", "machine"]
        )
        with_hi = [f.release for f in ranked(files, prefer_hi=True)]
        self.assertEqual(with_hi[0], "hi")  # the viewer picked such subtitles before
        self.assertEqual(with_hi[-1], "machine")

    def test_language_names(self):
        self.assertEqual(language_name("en"), "English")
        self.assertEqual(language_name("pt-BR"), "Portuguese (Brazil)")
        self.assertEqual(language_name("ze"), "Chinese (bilingual)")
        self.assertEqual(language_name("xx"), "XX")

    def test_this_computers_language_as_opensubtitles_writes_it(self):
        self.assertEqual(system_language(QLocale("tr_TR")), "tr")
        self.assertEqual(system_language(QLocale("pt_BR")), "pt-BR")
        self.assertEqual(system_language(QLocale("pt_PT")), "pt-PT")
        self.assertEqual(system_language(QLocale("zh_TW")), "zh-TW")

    def test_a_saved_file_has_a_safe_name(self):
        self.assertEqual(safe_file_name('a/b\\c:"d".srt', "1.srt"), "c d .srt")
        self.assertEqual(safe_file_name("Harbour", "1.srt"), "Harbour.srt")
        self.assertEqual(safe_file_name("", "1.srt"), "1.srt")
        # Windows' device names never name a file.
        self.assertEqual(safe_file_name("con.srt", "1.srt"), "_con.srt")
        self.assertEqual(safe_file_name("NUL", "1.srt"), "_NUL.srt")
        self.assertEqual(safe_file_name("COM1.en.srt", "1.srt"), "_COM1.en.srt")
        self.assertEqual(safe_file_name("Console.srt", "1.srt"), "Console.srt")


class KeyTests(unittest.TestCase):
    def test_the_users_key_else_the_builds(self):
        with mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", " built-in "):
            self.assertEqual(api_key(""), "built-in")
            self.assertEqual(api_key(" mine "), "mine")
        with mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", ""):
            self.assertEqual(api_key(""), "")

    def test_the_repository_carries_no_key(self):
        self.assertEqual(_build_keys.OPENSUBTITLES_API_KEY, "")


class SearchTests(unittest.TestCase):
    def test_a_search_sends_the_key_and_the_apps_user_agent(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", FakeResponse(200, page(entry(1, "en", "Harbour"))))
        found = client(http).search(SearchQuery("Harbour Lights", 2019))
        self.assertEqual([f.file_id for f in found], [1])
        (request,) = http.made("GET", "/subtitles")
        headers = request["headers"]
        self.assertEqual(headers["Api-Key"], KEY)
        self.assertRegex(headers["User-Agent"], r"^QiTV v\d")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(request["params"], [("query", "harbour lights"), ("year", "2019")])
        self.assertTrue(request["allow_redirects"])
        self.assertTrue(request["timeout"])

    def test_more_pages_are_read_then_the_preferred_languages(self):
        http = FakeHttp()
        http.answer(
            "GET",
            API + "/subtitles",
            FakeResponse(200, page(entry(1, "en", "a"), total_pages=5)),
            FakeResponse(200, page(entry(2, "de", "b"), total_pages=5, number=2)),
            FakeResponse(200, page(entry(2, "de", "b"), entry(3, "tr", "c"), total_pages=1)),
        )
        found = client(http).search(SearchQuery("Harbour"), preferred=["tr"], pages=2)
        self.assertEqual([f.file_id for f in found], [1, 2, 3])  # each once
        params = [dict(r["params"]) for r in http.made("GET", "/subtitles")]
        self.assertEqual([p.get("page") for p in params], [None, "2", None])
        self.assertEqual(params[2].get("languages"), "tr")

    def test_an_id_that_finds_nothing_is_searched_again_by_the_title(self):
        http = FakeHttp()
        made_for = feature("Harbour Lights", 2019)
        http.answer(
            "GET",
            API + "/subtitles",
            FakeResponse(200, page()),  # the id: nothing
            FakeResponse(200, page(entry(1, "en", "a", made_for=made_for))),
        )
        results = []
        query = SearchQuery("Harbour Lights", 2019, tmdb_id=4242)
        worker = SearchWorker(client(http), query)
        worker.finished.connect(results.append)
        worker.run()
        params = [dict(r["params"]) for r in http.made("GET", "/subtitles")]
        self.assertEqual(
            params,
            [{"tmdb_id": "4242", "type": "movie"}, {"query": "harbour lights", "year": "2019"}],
        )
        (result,) = results
        self.assertEqual(result.query, SearchQuery("Harbour Lights", 2019))
        self.assertEqual([f.file_id for f in result.files], [1])

    def test_an_id_that_finds_its_film_is_searched_once(self):
        http = FakeHttp()
        made_for = feature("Brave Harbour Six", 2014, tmdb=4242)
        answer = page(
            entry(1, "en", "a", made_for=made_for), entry(2, "tr", "b", made_for=made_for)
        )
        http.answer("GET", API + "/subtitles", FakeResponse(200, answer))
        results = []
        query = SearchQuery("Süper Liman Bekçileri", 2014, tmdb_id=4242)
        worker = SearchWorker(client(http), query)
        worker.finished.connect(results.append)
        worker.run()
        self.assertEqual(len(http.made("GET", "/subtitles")), 1)
        self.assertEqual([f.file_id for f in results[0].files], [1, 2])
        self.assertEqual(results[0].others, ())

    def test_the_worker_keeps_other_titles_apart(self):
        http = FakeHttp()
        answer = page(
            entry(1, "tr", "a", made_for=feature("Grown Beginners", 2014)),
            entry(2, "en", "b", made_for=feature("Night Ferry", 2014)),
        )
        http.answer("GET", API + "/subtitles", FakeResponse(200, answer))
        results = []
        worker = SearchWorker(client(http), SearchQuery("Süper Liman Bekçileri", 2014))
        worker.finished.connect(results.append)
        worker.run()
        self.assertEqual(results[0].files, ())
        self.assertEqual([f.file_id for f in results[0].others], [1, 2])

    def test_a_worker_emits_the_result_or_the_problem(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", FakeResponse(200, page(entry(1, "en", "a"))))
        results = []
        worker = SearchWorker(client(http), SearchQuery("Harbour"))
        worker.finished.connect(results.append)
        worker.run()
        self.assertIsInstance(results[0], SearchResult)
        http.answer("GET", API + "/subtitles", FakeResponse(401, {"message": "invalid key"}))
        worker.run()
        self.assertEqual(results[1].kind, BAD_KEY)


class ProblemTests(unittest.TestCase):
    def problem(self, http, call=lambda c: c.search(SearchQuery("Harbour"))) -> Problem:
        with self.assertRaises(OpenSubtitlesError) as caught:
            call(client(http))
        return caught.exception.problem

    def test_a_key_opensubtitles_refuses(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", FakeResponse(401, {"message": "Invalid API key"}))
        problem = self.problem(http)
        self.assertEqual(problem.kind, BAD_KEY)
        self.assertIn("didn't accept the API key", problem.text)

    def test_no_key_is_said_without_asking(self):
        http = FakeHttp()
        with self.assertRaises(OpenSubtitlesError) as caught:
            client(http, key="").search(SearchQuery("Harbour"))
        self.assertEqual(caught.exception.problem.kind, NO_KEY)
        self.assertEqual(http.calls, [])

    def test_todays_downloads_used_says_when_they_come_back(self):
        http = FakeHttp()
        http.answer("POST", API + "/download", quota_answer())
        problem = self.problem(http, lambda c: c.download(1))
        self.assertEqual(problem.kind, QUOTA)
        self.assertIn("5 a day per connection", problem.text)
        self.assertIn("8 hours and 38 minutes", problem.text)
        self.assertIn("Signing in", problem.text)
        self.assertIn("Settings → Subtitles", problem.text)

    def test_too_many_requests_says_how_long_to_wait(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", FakeResponse(429, {}, headers={"Retry-After": "4"}))
        problem = self.problem(http)
        self.assertEqual(problem.kind, BUSY)
        self.assertIn("Try again in 4 seconds", problem.text)

    def test_no_connection(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", requests.ConnectionError("down"))
        problem = self.problem(http)
        self.assertEqual(problem.kind, NETWORK)
        self.assertIn("Check your internet connection", problem.text)
        http.answer("GET", API + "/subtitles", requests.Timeout("slow"))
        self.assertEqual(self.problem(http).kind, NETWORK)

    def test_the_server_in_trouble_and_answers_not_json(self):
        http = FakeHttp()
        http.answer("GET", API + "/subtitles", FakeResponse(503, {}))
        self.assertEqual(self.problem(http).kind, SERVER)
        http.answer("GET", API + "/subtitles", FakeResponse(200, None))
        self.assertEqual(self.problem(http).kind, SERVER)

    def test_an_anonymous_download_refused_is_the_key(self):
        http = FakeHttp()
        http.answer("POST", API + "/download", FakeResponse(401, {"message": "invalid key"}))
        self.assertEqual(self.problem(http, lambda c: c.download(1)).kind, BAD_KEY)
        self.assertEqual(http.made("POST", "/login"), [])

    def test_a_file_that_is_gone(self):
        http = FakeHttp()
        http.answer("POST", API + "/download", FakeResponse(404, {"message": "gone"}))
        self.assertEqual(self.problem(http, lambda c: c.download(1)).kind, GONE)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = directory.name

    def test_a_download_is_saved_under_its_own_name(self):
        http = FakeHttp()
        serving(http, entry(77, "en", "Harbour.Lights.2019"))
        results = []
        file = SubtitleFile(77, "en", "Harbour.Lights.2019")
        worker = DownloadWorker(client(http), file, self.folder)
        worker.finished.connect(results.append)
        worker.run()
        (result,) = results
        self.assertIsInstance(result, Downloaded)
        self.assertEqual(result.name, "Harbour.Lights.2019.en.srt")
        self.assertEqual(result.path, os.path.join(self.folder, "77", result.name))
        with open(result.path, "rb") as f:
            self.assertEqual(f.read(), SRT)
        self.assertEqual((result.remaining, result.reset), (4, "23 hours and 52 minutes"))
        (asked,) = http.made("POST", "/download")
        self.assertEqual(asked["json"], {"file_id": 77})
        (fetched,) = http.made("GET", LINK)
        self.assertNotIn("Api-Key", fetched["headers"])  # the key stays with the API
        self.assertEqual([n for n in os.listdir(os.path.join(self.folder, "77"))], [result.name])

    def test_the_link_is_read_in_pieces_and_its_redirects_stay_https(self):
        http = FakeHttp()
        http.answer("POST", API + "/download", FakeResponse(200, download_answer()))
        cdn = "https://cdn.opensubtitles.example/f/1.srt"
        http.answer("GET", LINK, FakeResponse(302, headers={"Location": cdn}))
        http.answer("GET", cdn, FakeResponse(200, content=SRT))
        content, _info = client(http).download(1)
        self.assertEqual(content, SRT)
        for url in (LINK, cdn):
            (request,) = http.made("GET", url)
            self.assertTrue(request["stream"])
            self.assertFalse(request["allow_redirects"])  # each hop is checked
        http.answer("GET", LINK, FakeResponse(302, headers={"Location": "http://plain.example/1"}))
        with self.assertRaises(OpenSubtitlesError) as caught:
            client(http).download(1)
        self.assertEqual(caught.exception.problem.kind, SERVER)
        self.assertEqual(http.made("GET", "http://plain.example/1"), [])  # never followed

    def test_a_file_too_large_is_refused(self):
        http = FakeHttp()
        serving(http, entry(1, "en", "Harbour"))
        with mock.patch.object(opensubtitles, "MAX_FILE_BYTES", len(SRT) - 1):
            with self.assertRaises(OpenSubtitlesError) as caught:
                client(http).download(1)
        self.assertEqual(caught.exception.problem.kind, SERVER)

    def test_a_saved_file_is_not_downloaded_again(self):
        path = os.path.join(self.folder, "77", "Harbour.srt")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(SRT)
        http = FakeHttp()
        results = []
        worker = DownloadWorker(
            client(http), SubtitleFile(77, "en", "Harbour"), self.folder, saved=path
        )
        worker.finished.connect(results.append)
        worker.run()
        self.assertTrue(results[0].reused)
        self.assertEqual(http.calls, [])

    def test_a_problem_downloading_is_emitted(self):
        http = FakeHttp()
        http.answer("POST", API + "/download", quota_answer())
        results = []
        worker = DownloadWorker(client(http), SubtitleFile(77, "en", "Harbour"), self.folder)
        worker.finished.connect(results.append)
        worker.run()
        self.assertEqual(results[0].kind, QUOTA)
        self.assertEqual(os.listdir(self.folder), [])


class SignInTests(unittest.TestCase):
    def signed(self, http):
        http.answer("POST", API + "/login", FakeResponse(200, login_answer()))
        http.answer("POST", API + "/download", FakeResponse(200, download_answer()))
        http.answer("GET", LINK, FakeResponse(200, content=SRT))
        return client(http, username=USER, password=PASSWORD)

    def test_a_download_signs_in_and_reuses_the_token(self):
        http = FakeHttp()
        signed = self.signed(http)
        signed.download(1)
        signed.download(2)
        (login,) = http.made("POST", "/login")
        self.assertEqual(login["json"], {"username": USER, "password": PASSWORD})
        downloads = http.made("POST", "/download")
        self.assertEqual(len(downloads), 2)
        for request in downloads:
            self.assertEqual(request["headers"]["Authorization"], "Bearer token-1")
            self.assertEqual(request["headers"]["Api-Key"], KEY)
        self.assertTrue(signed.signed_in())

    def test_an_expired_token_signs_in_again_once(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer(
            "POST",
            API + "/login",
            FakeResponse(200, login_answer("token-1")),
            FakeResponse(200, login_answer("token-2")),
        )
        http.answer(
            "POST",
            API + "/download",
            FakeResponse(401, {"message": "token expired"}),
            FakeResponse(200, download_answer()),
        )
        content, info = signed.download(1)
        self.assertEqual(content, SRT)
        tokens = [r["headers"]["Authorization"] for r in http.made("POST", "/download")]
        self.assertEqual(tokens, ["Bearer token-1", "Bearer token-2"])
        self.assertEqual(len(http.made("POST", "/login")), 2)

    def test_a_wrong_password_is_said(self):
        http = FakeHttp()
        http.answer(
            "POST", API + "/login", FakeResponse(401, {"message": "Invalid username/password"})
        )
        with self.assertRaises(OpenSubtitlesError) as caught:
            client(http, username=USER, password="wrong").download(1)
        self.assertEqual(caught.exception.problem.kind, BAD_LOGIN)
        self.assertEqual(http.made("POST", "/download"), [])

    def test_a_sign_in_refused_for_the_key_is_the_key(self):
        http = FakeHttp()
        http.answer(
            "POST",
            API + "/login",
            FakeResponse(403, {"message": "You cannot consume this service"}),
        )
        with self.assertRaises(OpenSubtitlesError) as caught:
            client(http, username=USER, password=PASSWORD).download(1)
        self.assertEqual(caught.exception.problem.kind, BAD_KEY)

    def test_refused_again_after_a_new_sign_in_asks_to_sign_in_again(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer("POST", API + "/download", FakeResponse(401, {"message": "token"}))
        with self.assertRaises(OpenSubtitlesError) as caught:
            signed.download(1)
        problem = caught.exception.problem
        self.assertEqual(problem.kind, BAD_LOGIN)  # never the key page for a sign-in
        self.assertIn("Sign in to OpenSubtitles again", problem.text)
        self.assertEqual(len(http.made("POST", "/login")), 2)  # one new sign-in, no loop
        self.assertEqual(len(http.made("POST", "/download")), 2)

    def test_jobs_at_once_sign_in_once(self):
        http = FakeHttp()
        signed = self.signed(http)
        started = threading.Event()

        def slow_login(_kwargs):
            started.set()
            time.sleep(0.05)
            return FakeResponse(200, login_answer())

        http.answer("POST", API + "/login", slow_login)
        tokens = []
        jobs = [threading.Thread(target=lambda: tokens.append(signed._token_now())) for _ in "ab"]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join(5)
        self.assertEqual(tokens, ["token-1", "token-1"])
        self.assertEqual(len(http.made("POST", "/login")), 1)

    def test_a_signed_in_users_requests_go_to_their_host_but_only_opensubtitles(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer(
            "POST",
            API + "/login",
            FakeResponse(200, login_answer(base_url="vip-api.opensubtitles.com")),
        )
        http.answer(
            "POST",
            "https://vip-api.opensubtitles.com/api/v1/download",
            FakeResponse(200, download_answer()),
        )
        signed.download(1)
        self.assertEqual(
            len(http.made("POST", "https://vip-api.opensubtitles.com/api/v1/download")), 1
        )
        other = FakeHttp()
        elsewhere = self.signed(other)
        other.answer(
            "POST", API + "/login", FakeResponse(200, login_answer(base_url="evil.example"))
        )
        elsewhere.download(1)
        self.assertEqual(len(other.made("POST", "/download")), 1)  # the API's own host

    def test_sign_out_ends_the_sign_in(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer(
            "DELETE",
            API + "/logout",
            FakeResponse(200, {"message": "token successfully destroyed"}),
        )
        signed.download(1)
        signed.logout()
        (logout,) = http.made("DELETE", "/logout")
        self.assertEqual(logout["headers"]["Authorization"], "Bearer token-1")
        self.assertFalse(signed.signed_in())
        signed.logout()  # nothing more to end
        self.assertEqual(len(http.made("DELETE", "/logout")), 1)

    def test_signed_in_quota_says_so(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer("POST", API + "/download", quota_answer())
        with self.assertRaises(OpenSubtitlesError) as caught:
            signed.download(1)
        self.assertIn("on your OpenSubtitles account", caught.exception.problem.text)

    def test_secrets_never_reach_the_log(self):
        http = FakeHttp()
        signed = self.signed(http)
        http.answer("POST", API + "/download", FakeResponse(401, {}), quota_answer())
        logger = logging.getLogger(opensubtitles.__name__)
        with self.assertLogs(logger, level="DEBUG") as logs:
            logger.debug("start")
            with self.assertRaises(OpenSubtitlesError):
                signed.download(1)
            http.answer("POST", API + "/download", requests.ConnectionError(f"{LINK}?key={KEY}"))
            with self.assertRaises(OpenSubtitlesError):
                signed.download(1)
        text = "\n".join(logs.output)
        for secret in (KEY, PASSWORD, "token-1", LINK):
            self.assertNotIn(secret, text)
        self.assertNotIn(KEY, repr(signed.credentials))
        self.assertNotIn(PASSWORD, repr(signed.credentials))


if __name__ == "__main__":
    unittest.main()
