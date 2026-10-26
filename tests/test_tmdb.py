"""TMDB: what a title is looked up by, how the answer is matched, cached and filled in.

Every title and id here is made up; the HTTP stand-in answers like TMDB's API.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from opensubtitles_fixtures import FakeHttp, FakeResponse

from services import tmdb
from services.tmdb import (
    MOVIE,
    TV,
    Info,
    LookupWorker,
    TmdbCache,
    TmdbClient,
    TmdbError,
    Wanted,
    fill_gaps,
    look_up,
    pick,
    wanted_for,
)

API = tmdb.API_URL


def film(tmdb_id, title, date, *, original="", overview="", votes=500, rating=7.4, poster="/p.jpg"):
    """A film as TMDB's search or details answer it (no credits)."""
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original or title,
        "release_date": date,
        "overview": overview,
        "vote_average": rating,
        "vote_count": votes,
        "poster_path": poster,
    }


def details(tmdb_id, title, date, **kw):
    """A film's details with its runtime, genres and credits (append_to_response=credits)."""
    data = film(tmdb_id, title, date, **kw)
    data.update(
        runtime=118,
        genres=[{"id": 1, "name": "Drama"}, {"id": 2, "name": "Mystery"}],
        credits={
            "cast": [{"name": f"Actor {n}", "order": n} for n in (3, 1, 2, 6, 5, 4, 7)],
            "crew": [
                {"name": "Some Writer", "job": "Screenplay"},
                {"name": "Ada Director", "job": "Director"},
            ],
        },
    )
    return data


def ok(data):
    return FakeResponse(200, data)


class WantedForTests(unittest.TestCase):
    def test_a_provider_id_is_looked_up_by_it(self):
        wanted = wanted_for({"name": "TR: The Harbour (2019) 4K", "tmdb": "4242"}, MOVIE)
        self.assertEqual(wanted.tmdb_id, 4242)
        self.assertEqual((wanted.text, wanted.year), ("The Harbour", 2019))
        self.assertEqual(wanted.key(), "movie:id:4242")

    def test_a_title_is_cleaned_and_keeps_its_year(self):
        wanted = wanted_for({"name": "The Harbour | EN", "year": "2019-05-01"}, MOVIE)
        self.assertEqual((wanted.text, wanted.year, wanted.tmdb_id), ("The Harbour", 2019, None))
        self.assertEqual(wanted.key(), "movie:q:harbour:2019")

    def test_the_key_ignores_case_accents_and_little_words(self):
        a = wanted_for({"name": "The Harbour Café"}, MOVIE)
        b = wanted_for({"name": "harbour cafe"}, MOVIE)
        self.assertEqual(a.key(), b.key())

    def test_an_original_latin_title_is_searched_for(self):
        item = {"name": "Liman Kafesi", "o_name": "The Harbour Café", "year": "2019"}
        self.assertEqual(wanted_for(item, MOVIE).text, "The Harbour Café")

    def test_an_imdb_id_is_kept(self):
        wanted = wanted_for({"name": "The Harbour", "imdb_id": "tt0012345"}, MOVIE)
        self.assertEqual(wanted.imdb_id, 12345)
        self.assertEqual(wanted.key(), "movie:imdb:12345")

    def test_a_series_is_looked_up_as_tv(self):
        wanted = wanted_for({"name": "Harbour Nights", "tmdb": "77"}, TV)
        self.assertEqual((wanted.kind, wanted.tmdb_id), (TV, 77))

    def test_a_provider_s_own_tag_before_the_title_is_dropped(self):
        for name, text in (
            ("SO-IN - Harbour Apna Apna (1989)", "Harbour Apna Apna"),
            ("ZONE+ - Harbour The Debut (2023)", "Harbour The Debut"),
            ("IN-MM - Harbour Pratiksha", "Harbour Pratiksha"),
            ("QQV -  The Harbour War (2021)", "The Harbour War"),
        ):
            with self.subTest(name=name):
                self.assertEqual(wanted_for({"name": name}, MOVIE).text, text)

    def test_what_may_be_the_title_s_own_stays(self):
        for name in ("AB - Up", "Harbour - Lights", "M - Harbour", "JFK"):
            with self.subTest(name=name):
                self.assertEqual(wanted_for({"name": name}, MOVIE).text, name)

    def test_dated_events_are_looked_up_only_by_an_id(self):
        event = {"name": "SOC - Harbour United - Bay Rovers 23.09.2024"}
        self.assertIsNone(wanted_for(event, MOVIE))
        self.assertEqual(wanted_for({**event, "tmdb": "9"}, MOVIE).tmdb_id, 9)
        self.assertEqual(wanted_for({"name": "11.22.63"}, TV).text, "11 22 63")  # no date

    def test_titles_tagged_adult_are_never_sent(self):
        for name in ("XXX - Harbour Nights", "[XXX] Harbour Nights", "XXX: Harbour Nights"):
            with self.subTest(name=name):
                self.assertIsNone(wanted_for({"name": name, "tmdb": "9"}, MOVIE))
        self.assertEqual(wanted_for({"name": "XXX (2002)"}, MOVIE).text, "XXX")  # a title

    def test_recordings_and_wordless_titles_are_not_looked_up(self):
        self.assertIsNone(wanted_for({"name": "The Harbour", "_recording": True}, MOVIE))
        self.assertIsNone(wanted_for({"name": "!!! ---"}, MOVIE))
        self.assertIsNone(wanted_for({"name": "The Harbour"}, "episode"))


class PickTests(unittest.TestCase):
    RESULTS = [
        film(1, "Dune Sea", "2021-09-15"),
        film(2, "Dune Sea: Part Two", "2024-02-27"),
        film(3, "Dune Sea", "1984-12-14"),
        film(4, "Dune Sea", "2020-01-01"),
    ]

    def test_the_year_tells_films_of_one_name_apart(self):
        chosen, unsure = pick(self.RESULTS, Wanted(MOVIE, "Dune Sea", 1984))
        self.assertEqual((chosen["id"], unsure), (3, []))

    def test_a_year_one_off_still_matches(self):
        chosen, _ = pick(self.RESULTS, Wanted(MOVIE, "Dune Sea", 1985))
        self.assertEqual(chosen["id"], 3)

    def test_the_closest_year_wins(self):
        chosen, _ = pick(self.RESULTS, Wanted(MOVIE, "Dune Sea", 2021))
        self.assertEqual(chosen["id"], 1)

    def test_without_a_year_several_films_of_that_name_match_none(self):
        self.assertEqual(pick(self.RESULTS, Wanted(MOVIE, "Dune Sea")), (None, []))

    def test_a_longer_title_is_another_film(self):
        chosen, unsure = pick(
            [film(2, "Dune Sea: Part Two", "2024-02-27")], Wanted(MOVIE, "Dune Sea")
        )
        self.assertIsNone(chosen)
        self.assertEqual([r["id"] for r in unsure], [2])  # its translations may say so

    def test_a_title_of_that_name_in_another_year_is_not_it(self):
        self.assertEqual(pick(self.RESULTS, Wanted(MOVIE, "Dune Sea", 1999)), (None, []))

    def test_a_unique_title_matches_without_a_year(self):
        chosen, _ = pick([film(9, "The Harbour", "2019-01-01")], Wanted(MOVIE, "Harbour"))
        self.assertEqual(chosen["id"], 9)

    def test_the_original_title_matches_too(self):
        results = [film(9, "Liman", "2019-01-01", original="The Harbour")]
        chosen, _ = pick(results, Wanted(MOVIE, "The Harbour", 2019))
        self.assertEqual(chosen["id"], 9)

    def test_a_translated_title_leaves_the_years_results_to_check(self):
        results = [
            film(5, "The Harbour", "2019-01-01"),
            film(6, "The Harbour Two", "2010-01-01"),
            film(7, "Harbour Three", "2020-01-01"),
        ]
        chosen, unsure = pick(results, Wanted(MOVIE, "Liman", 2019))
        self.assertIsNone(chosen)
        self.assertEqual([r["id"] for r in unsure], [5, 7])

    def test_a_translated_title_without_a_year_checks_only_the_first(self):
        results = [film(5, "The Harbour", "2019-01-01"), film(7, "Harbour Three", "2020-01-01")]
        self.assertEqual([r["id"] for r in pick(results, Wanted(MOVIE, "Liman"))[1]], [5])

    def test_a_tv_result_uses_its_name_and_first_air_date(self):
        results = [
            {"id": 8, "name": "Harbour Nights", "original_name": "", "first_air_date": "2016-03-01"}
        ]
        chosen, _ = pick(results, Wanted(TV, "Harbour Nights", 2016))
        self.assertEqual(chosen["id"], 8)


class ClientTests(unittest.TestCase):
    def test_requests_carry_the_token_language_and_no_one_s_name(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", ok(details(7, "The Harbour", "2019-01-01")))
        TmdbClient("tok", "tr-TR", http=http).details(MOVIE, 7)
        _method, url, kwargs = http.calls[0]
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertTrue(kwargs["headers"]["User-Agent"].startswith("QiTV v"))
        self.assertEqual(kwargs["params"]["language"], "tr-TR")
        self.assertEqual(kwargs["params"]["append_to_response"], "credits")
        self.assertEqual(set(kwargs["headers"]), {"Authorization", "User-Agent", "Accept"})

    def test_a_missing_title_is_none(self):
        http = FakeHttp()  # answers 404
        self.assertIsNone(TmdbClient("tok", http=http).details(MOVIE, 7))

    def test_a_refused_token_says_so(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", FakeResponse(401, {"status_code": 7}))
        with self.assertRaises(TmdbError) as caught:
            TmdbClient("bad", http=http).details(MOVIE, 7)
        self.assertEqual(caught.exception.kind, tmdb.BAD_TOKEN)

    def test_too_many_requests_says_how_long_to_wait(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", FakeResponse(429, {}, headers={"Retry-After": "4"}))
        with self.assertRaises(TmdbError) as caught:
            TmdbClient("tok", http=http).details(MOVIE, 7)
        self.assertEqual((caught.exception.kind, caught.exception.retry_after), (tmdb.BUSY, 4.0))

    def test_an_unreachable_server_is_a_network_problem(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", OSError("no route"))
        with self.assertRaises(TmdbError) as caught:
            TmdbClient("tok", http=http).details(MOVIE, 7)
        self.assertEqual(caught.exception.kind, tmdb.NETWORK)

    def test_no_request_starts_once_qitv_is_closing(self):
        http = FakeHttp()
        with mock.patch.object(tmdb, "cancel_requested", return_value=True):
            with self.assertRaises(TmdbError):
                TmdbClient("tok", http=http).details(MOVIE, 7)
        self.assertEqual(http.calls, [])

    def test_no_token_asks_nothing(self):
        http = FakeHttp()
        with self.assertRaises(TmdbError) as caught:
            TmdbClient("", http=http).details(MOVIE, 7)
        self.assertEqual((caught.exception.kind, http.calls), (tmdb.BAD_TOKEN, []))


class LookUpTests(unittest.TestCase):
    def client(self, http, language="en-US"):
        return TmdbClient("tok", language, http=http)

    def test_by_the_provider_s_id(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/movie/7", ok(details(7, "The Harbour", "2019-03-01", overview="Boats."))
        )
        info = look_up(self.client(http), Wanted(MOVIE, "The Harbour", 2019, tmdb_id=7))
        self.assertEqual(
            info,
            Info(
                tmdb_id=7,
                kind=MOVIE,
                title="The Harbour",
                year=2019,
                plot="Boats.",
                minutes=118,
                genres=("Drama", "Mystery"),
                rating=7.4,
                cast=("Actor 1", "Actor 2", "Actor 3", "Actor 4", "Actor 5"),
                director="Ada Director",
                poster="/p.jpg",
            ),
        )
        self.assertEqual(len(http.calls), 1)

    def test_an_id_tmdb_does_not_know_falls_back_to_the_title(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/search/movie", ok({"results": [film(9, "The Harbour", "2019-01-01")]})
        )
        http.answer("GET", f"{API}/movie/9", ok(details(9, "The Harbour", "2019-01-01")))
        info = look_up(self.client(http), Wanted(MOVIE, "The Harbour", 2019, tmdb_id=7))
        self.assertEqual(info.tmdb_id, 9)
        search = [kw for m, u, kw in http.calls if u.endswith("/search/movie")][0]
        self.assertEqual(search["params"]["query"], "The Harbour")
        self.assertNotIn("year", search["params"])  # years differ by country: matched here

    def test_by_an_imdb_id(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/find/tt0012345", ok({"movie_results": [{"id": 9}], "tv_results": []})
        )
        http.answer("GET", f"{API}/movie/9", ok(details(9, "The Harbour", "2019-01-01")))
        info = look_up(self.client(http), Wanted(MOVIE, "The Harbour", imdb_id=12345))
        self.assertEqual(info.tmdb_id, 9)
        find = [kw for m, u, kw in http.calls if "/find/" in u][0]
        self.assertEqual(find["params"]["external_source"], "imdb_id")

    def test_a_series_by_its_name(self):
        http = FakeHttp()
        show = {
            "id": 8,
            "name": "Harbour Nights",
            "original_name": "Harbour Nights",
            "first_air_date": "2016-03-01",
        }
        http.answer("GET", f"{API}/search/tv", ok({"results": [show]}))
        http.answer(
            "GET",
            f"{API}/tv/8",
            ok(
                {
                    **show,
                    "overview": "Night shifts.",
                    "episode_run_time": [42],
                    "genres": [{"name": "Crime"}],
                    "vote_average": 8.12,
                    "vote_count": 90,
                    "poster_path": "/n.jpg",
                    "created_by": [{"name": "Bo Maker"}, {"name": "Cy Maker"}],
                    "credits": {"cast": [{"name": "Lead", "order": 0}]},
                }
            ),
        )
        info = look_up(self.client(http), Wanted(TV, "Harbour Nights", 2016))
        self.assertEqual(
            (info.title, info.year, info.minutes, info.rating, info.director, info.cast),
            ("Harbour Nights", 2016, 42, 8.1, "Bo Maker, Cy Maker", ("Lead",)),
        )

    def test_a_translated_title_is_checked_against_tmdb_s_translations(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/search/movie", ok({"results": [film(9, "The Harbour", "2019-01-01")]})
        )
        data = details(9, "The Harbour", "2019-01-01")
        data["translations"] = {
            "translations": [{"iso_639_1": "tr", "data": {"title": "Liman", "overview": ""}}]
        }
        data["alternative_titles"] = {"titles": []}
        http.answer("GET", f"{API}/movie/9", ok(data))
        info = look_up(self.client(http), Wanted(MOVIE, "Liman", 2019))
        self.assertEqual(info.tmdb_id, 9)
        params = [kw for m, u, kw in http.calls if u.endswith("/movie/9")][0]["params"]
        self.assertEqual(params["append_to_response"], "credits,translations,alternative_titles")

    def test_an_alternative_title_counts(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/search/movie", ok({"results": [film(9, "The Harbour", "2019-01-01")]})
        )
        data = details(9, "The Harbour", "2019-01-01")
        data["translations"] = {"translations": []}
        data["alternative_titles"] = {"titles": [{"title": "Harbour Lights"}]}
        http.answer("GET", f"{API}/movie/9", ok(data))
        self.assertEqual(
            look_up(self.client(http), Wanted(MOVIE, "Harbour Lights", 2019)).tmdb_id, 9
        )

    def test_a_result_known_by_no_such_title_is_not_it(self):
        http = FakeHttp()
        http.answer(
            "GET", f"{API}/search/movie", ok({"results": [film(9, "The Harbour", "2019-01-01")]})
        )
        data = details(9, "The Harbour", "2019-01-01")
        data["translations"] = {"translations": [{"data": {"title": "Liman"}}]}
        http.answer("GET", f"{API}/movie/9", ok(data))
        self.assertIsNone(look_up(self.client(http), Wanted(MOVIE, "Deniz Feneri", 2019)))

    def test_nothing_found_is_none(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/search/movie", ok({"results": []}))
        self.assertIsNone(look_up(self.client(http), Wanted(MOVIE, "Nowhere At All", 2019)))

    def test_a_story_missing_in_the_language_is_taken_in_english(self):
        http = FakeHttp()
        http.answer(
            "GET",
            f"{API}/movie/7",
            ok(details(7, "Liman", "2019-01-01", overview="")),
            ok(details(7, "The Harbour", "2019-01-01", overview="Boats.")),
        )
        info = look_up(self.client(http, "tr-TR"), Wanted(MOVIE, "Liman", tmdb_id=7))
        self.assertEqual((info.title, info.plot), ("Liman", "Boats."))
        self.assertEqual(http.calls[1][2]["params"], {"language": "en-US"})

    def test_a_rating_of_few_votes_is_left_out(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", ok(details(7, "The Harbour", "2019-01-01", votes=3)))
        self.assertIsNone(look_up(self.client(http), Wanted(MOVIE, "x", tmdb_id=7)).rating)


class FillGapsTests(unittest.TestCase):
    INFO = Info(
        tmdb_id=7,
        kind=MOVIE,
        title="The Harbour",
        year=2019,
        plot="Boats.",
        minutes=118,
        genres=("Drama", "Mystery", "Crime", "War"),
        rating=7.4,
        cast=("Actor 1", "Actor 2"),
        director="Ada Director",
        poster="/p.jpg",
    )

    def test_the_provider_s_own_fields_stay(self):
        item = {"name": "Liman", "description": "Tekneler.", "year": "2018", "rating": "6"}
        filled = fill_gaps(item, self.INFO)
        self.assertEqual(
            (filled["description"], filled["year"], filled["rating"]), ("Tekneler.", "2018", "6")
        )
        self.assertNotIn("plot", filled)

    def test_its_gaps_are_filled(self):
        filled = fill_gaps({"name": "Liman", "description": "", "rating": "0"}, self.INFO)
        self.assertEqual(filled["plot"], "Boats.")
        self.assertEqual(filled["year"], "2019")
        self.assertEqual(filled["duration_secs"], 118 * 60)
        self.assertEqual(filled["genre"], "Drama, Mystery, Crime")
        self.assertEqual(filled["rating"], "7.4")
        self.assertEqual(filled["cast"], "Actor 1, Actor 2")
        self.assertEqual(filled["director"], "Ada Director")

    def test_the_item_itself_is_not_changed(self):
        item = {"name": "Liman"}
        self.assertIsNot(fill_gaps(item, self.INFO), item)
        self.assertEqual(item, {"name": "Liman"})

    def test_nothing_known_is_the_item_as_it_is(self):
        item = {"name": "Liman"}
        self.assertIs(fill_gaps(item, None), item)

    def test_a_poster_url(self):
        self.assertEqual(self.INFO.poster_url(), "https://image.tmdb.org/t/p/w342/p.jpg")
        self.assertEqual(Info(1, MOVIE, "x").poster_url(), "")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "tmdb.json")
        self.now = 1_000_000.0

    def tearDown(self):
        self.dir.cleanup()

    def cache(self):
        return TmdbCache(self.path, now=lambda: self.now)

    def test_terms_keep_nothing_longer_than_six_months(self):
        self.assertLessEqual(max(tmdb.FOUND_S, tmdb.MISSING_S), 183 * 24 * 3600)

    def test_found_and_not_found_are_kept_across_starts(self):
        cache = self.cache()
        cache.load()
        cache.put("en-US|movie:id:7", FillGapsTests.INFO)
        cache.put("en-US|movie:q:nowhere:", None)
        self.assertTrue(cache.save())
        again = self.cache()
        again.load()
        self.assertEqual(again.get("en-US|movie:id:7"), (True, FillGapsTests.INFO))
        self.assertEqual(again.get("en-US|movie:q:nowhere:"), (True, None))
        self.assertEqual(again.get("en-US|movie:id:8"), (False, None))

    def test_entries_expire(self):
        cache = self.cache()
        cache.put("found", FillGapsTests.INFO)
        cache.put("missing", None)
        self.now += tmdb.MISSING_S + 1
        self.assertEqual(cache.get("missing"), (False, None))
        self.assertEqual(cache.get("found"), (True, FillGapsTests.INFO))
        self.now += tmdb.FOUND_S
        self.assertEqual(cache.get("found"), (False, None))

    def test_expired_entries_are_not_saved(self):
        cache = self.cache()
        cache.put("found", FillGapsTests.INFO)
        cache.save()
        self.now += tmdb.FOUND_S + 1
        later = self.cache()
        later.load()
        later.put("other", None)
        later.save()
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)["titles"]), ["other"])

    def test_the_oldest_go_beyond_the_limit(self):
        cache = self.cache()
        with mock.patch.object(tmdb, "MAX_ENTRIES", 2):
            for n in range(3):
                self.now += 1
                cache.put(f"k{n}", None)
            cache.save()
        later = self.cache()
        later.load()
        self.assertEqual([later.get(f"k{n}")[0] for n in range(3)], [False, True, True])

    def test_an_unreadable_or_other_version_file_is_an_empty_cache(self):
        for text in ("{not json", json.dumps({"version": 99, "titles": {"k": {}}}), "[]"):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            cache = self.cache()
            cache.load()
            self.assertEqual(cache.get("k"), (False, None))

    def test_saving_nothing_new_writes_nothing(self):
        cache = self.cache()
        cache.load()
        self.assertFalse(cache.save())
        self.assertFalse(os.path.exists(self.path))


class LookupWorkerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = TmdbCache(os.path.join(self.dir.name, "tmdb.json"))

    def tearDown(self):
        self.dir.cleanup()

    def run_worker(self, http, wanted):
        worker = LookupWorker(TmdbClient("tok", http=http), self.cache, wanted)
        results = []
        worker.finished.connect(results.append)
        worker.run()
        return results[0]

    def test_looks_up_caches_and_saves(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", ok(details(7, "The Harbour", "2019-01-01")))
        wanted = Wanted(MOVIE, "The Harbour", tmdb_id=7)
        looked = self.run_worker(http, [wanted])
        self.assertEqual(looked.found[wanted.key()].tmdb_id, 7)
        again = self.run_worker(http, [wanted])
        self.assertEqual(again.found[wanted.key()].tmdb_id, 7)
        self.assertEqual(len(http.calls), 1)  # the second time from the cache
        self.assertTrue(os.path.exists(self.cache.path))

    def test_a_busy_server_stops_the_batch(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", FakeResponse(429, {}, headers={"Retry-After": "3"}))
        looked = self.run_worker(
            http, [Wanted(MOVIE, "a", tmdb_id=7), Wanted(MOVIE, "b", tmdb_id=8)]
        )
        self.assertEqual((looked.found, looked.busy_s), ({}, 3.0))
        self.assertEqual(len(http.calls), 1)

    def test_a_refused_token_stops_the_batch(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", FakeResponse(401, {}))
        looked = self.run_worker(http, [Wanted(MOVIE, "a", tmdb_id=7)])
        self.assertTrue(looked.bad_token)

    def test_a_network_failure_is_not_remembered(self):
        http = FakeHttp()
        http.answer("GET", f"{API}/movie/7", OSError("down"), ok(details(7, "A", "2019-01-01")))
        wanted = Wanted(MOVIE, "a", tmdb_id=7)
        self.assertEqual(self.run_worker(http, [wanted]).found, {})
        self.assertEqual(self.run_worker(http, [wanted]).found[wanted.key()].tmdb_id, 7)


class TokenTests(unittest.TestCase):
    def test_the_user_s_own_token_wins(self):
        with mock.patch.object(tmdb._build_keys, "TMDB_API_TOKEN", "built"):
            self.assertEqual(tmdb.api_token(" mine "), "mine")
            self.assertEqual(tmdb.api_token(""), "built")

    def test_languages(self):
        self.assertEqual(tmdb.tmdb_language("tr"), "tr-TR")
        self.assertEqual(tmdb.tmdb_language(None), "en-US")
        self.assertEqual(tmdb.tmdb_language("pt"), "pt")


if __name__ == "__main__":
    unittest.main()
