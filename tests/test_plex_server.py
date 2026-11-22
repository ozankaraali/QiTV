"""Plex (2.14): a server's libraries, films and series as Library titles, a title's versions
(never converted, no 4K unless the setting allows it), its stream's address and where it plays
told back (a stop near the end is marked watched by the server itself, as a 1.43 server was
seen to). The fixtures are a local server's answers with made-up titles (its series library
made up in the shapes Plex documents)."""

import unittest
from urllib.parse import parse_qs, urlsplit

from services import media_server as ms, plex
from services.catalog import ALL_GROUP_ID
from tests.test_plex import FakeHttp, fixture

BASE = "https://10-0-0-5.example.plex.direct:32400"
TOKEN = "server-token"


def client(answers):
    http = FakeHttp(BASE, answers)
    c = plex.PlexClient(BASE, token=TOKEN, client_id="client-1", version="2.14.0", session=http)
    return c, http


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


class LibrariesTest(unittest.TestCase):
    def test_film_and_series_libraries_only_with_the_token_in_a_header(self):
        c, http = client({("GET", "/library/sections/all"): (200, fixture("sections.json"))})
        self.assertEqual(
            c.libraries(),
            [
                {"id": "1", "name": "Films", "type": "movie"},
                {"id": "2", "name": "Series", "type": "show"},
            ],
        )
        asked = http.asked[0]
        self.assertEqual(asked["headers"]["X-Plex-Token"], TOKEN)
        self.assertEqual(asked["headers"]["X-Plex-Product"], "QiTV")
        self.assertEqual(asked["headers"]["Accept"], "application/json")
        self.assertNotIn("X-Plex-Token", asked["params"])  # never in a logged address

    def test_a_refused_token_asks_to_sign_in_again_and_an_error_names_its_status(self):
        c, _ = client({("GET", "/library/sections/all"): (401, None)})
        with self.assertRaises(ms.SignInNeeded):
            c.libraries()
        c, _ = client({("GET", "/library/sections/all"): (500, {"x": 1})})
        with self.assertRaises(ms.MediaServerError) as caught:
            c.libraries()
        self.assertEqual(str(caught.exception), "The server answered 500")


class ItemsTest(unittest.TestCase):
    def test_a_librarys_titles_come_in_pages(self):
        films = fixture("movies_all.json")["MediaContainer"]["Metadata"]
        pages = []

        class Paged(FakeHttp):
            def request(self, method, url, params=None, headers=None, **kw):
                start = int(headers["X-Plex-Container-Start"])
                size = int(headers["X-Plex-Container-Size"])
                pages.append((start, size, dict(params)))
                batch = films[start : start + size]
                body = {"MediaContainer": {"size": len(batch), "totalSize": 3, "offset": start}}
                body["MediaContainer"]["Metadata"] = batch
                from tests.test_plex import _Response

                return _Response(200, body)

        http = Paged(BASE, {})
        c = plex.PlexClient(BASE, token=TOKEN, client_id="c", version="v", session=http)
        found = c.items("1", plex.MOVIE, page=2)
        self.assertEqual([f["ratingKey"] for f in found], ["101", "102", "103"])
        self.assertEqual([(s, n) for s, n, _ in pages], [(0, 2), (2, 2)])
        self.assertEqual(pages[0][2], {"type": 1, "includeGuids": 1})

    def test_a_server_that_ignores_paging_is_asked_no_further(self):  # review M1
        films = fixture("movies_all.json")  # three, whatever the start asked, and no total
        c, http = client({("GET", "/library/sections/1/all"): (200, films)})
        found = c.items("1", plex.MOVIE, page=2)
        self.assertEqual([f["ratingKey"] for f in found], ["101", "102", "103"])
        self.assertLessEqual(len(http.asked), 2)

    def test_an_answer_from_another_start_than_asked_ends_the_list(self):  # review M1
        films = fixture("movies_all.json")
        films["MediaContainer"].update(offset=0, totalSize=9)  # always the first page
        c, http = client({("GET", "/library/sections/1/all"): (200, films)})
        self.assertEqual(len(c.items("1", plex.MOVIE, page=3)), 3)
        self.assertEqual(len(http.asked), 2)

    def test_an_answer_without_its_total_ends_with_a_short_page(self):
        films = fixture("movies_all.json")
        c, http = client({("GET", "/library/sections/1/all"): (200, films)})
        self.assertEqual(len(c.items("1", plex.MOVIE)), 3)
        self.assertEqual(len(http.asked), 1)


class TitlesTest(unittest.TestCase):
    def setUp(self):
        self.films = fixture("movies_all.json")["MediaContainer"]["Metadata"]

    def test_a_film_as_a_library_title(self):
        item = plex.title_item(self.films[0], BASE, TOKEN, "1")
        self.assertEqual(item["id"], "101")
        self.assertEqual(item["name"], "Salt Roads")
        self.assertEqual(item["tv_genre_id"], "1")
        self.assertEqual(item["cmd"], "")  # its stream is asked for when it plays
        self.assertEqual(item["year"], "2021")
        self.assertEqual(item["rating"], "7.9")
        self.assertEqual(item["genre"], "Drama, Road")
        self.assertEqual(item["duration_secs"], 103)
        self.assertEqual(item["tmdb"], "90101")
        self.assertEqual(item["added"], "1732399531")
        self.assertEqual(item["server_position_ms"], 40_000)
        self.assertEqual(item["container_extension"], "mov")
        self.assertTrue(item["description"].endswith("…"))  # cut at a word
        self.assertLessEqual(len(item["description"]), ms.OVERVIEW_CHARS + 1)
        logo = item["logo"]
        self.assertTrue(logo.startswith(BASE + "/library/metadata/101/thumb/1795376703?"))
        self.assertEqual(query(logo), {"X-Plex-Token": TOKEN})

    def test_a_critics_rating_stands_in_and_what_is_missing_stays_empty(self):
        lantern = plex.title_item(self.films[1], BASE, TOKEN, "1")
        self.assertEqual(lantern["rating"], "9.2")
        self.assertNotIn("tmdb", lantern)
        self.assertNotIn("server_position_ms", lantern)
        harbour = plex.title_item(self.films[2], BASE, TOKEN, "1")
        self.assertEqual(harbour["year"], "")
        self.assertEqual(harbour["container_extension"], "mpegts")

    def test_the_section_all_continue_watching_then_each_library(self):
        libraries = [{"id": "1", "name": "Films", "type": "movie"}]
        payload = plex.section_payload(libraries, {"1": self.films}, BASE, TOKEN)
        self.assertEqual(
            [c["id"] for c in payload["categories"]], [ALL_GROUP_ID, ms.CONTINUE_ID, "1"]
        )
        self.assertEqual([i["name"] for i in payload["contents"]][0], "Salt Roads")
        self.assertEqual(payload["sorted_channels"][ms.CONTINUE_ID], [0])
        self.assertEqual(payload["sorted_channels"]["1"], [0, 1, 2])
        self.assertIsNone(ms.saved_summary(payload))  # a refresh loads it whole again


class SeriesTest(unittest.TestCase):
    def test_a_series_seasons_each_with_its_episodes_in_order(self):
        shows = fixture("shows_all.json")["MediaContainer"]["Metadata"]
        show = plex.title_item(shows[0], BASE, TOKEN, "2")
        self.assertEqual((show["id"], show["name"], show["tmdb"]), ("201", "Quiet Street", "8201"))
        c, http = client(
            {
                ("GET", "/library/metadata/201/children"): (200, fixture("show_children.json")),
                ("GET", "/library/metadata/201/allLeaves"): (200, fixture("show_allLeaves.json")),
            }
        )
        seasons, episodes = c.seasons_and_episodes("201")
        payload = plex.seasons_payload(seasons, episodes, BASE, TOKEN)
        first, second = payload["seasons"]
        self.assertEqual(
            (first["id"], first["name"], first["season_number"], first["episode_count"]),
            ("211", "Season 1", "1", "2"),
        )
        self.assertTrue(first["cover"].startswith(BASE + "/library/metadata/211/thumb/"))
        self.assertEqual(
            [e["name"] for e in first["episodes"]], ["The First Door", "The Second Door"]
        )
        episode = first["episodes"][1]
        self.assertEqual(
            {k: episode[k] for k in ("id", "number", "episode_num", "season", "series_id")},
            {"id": "222", "number": "2", "episode_num": "2", "season": "1", "series_id": "201"},
        )
        self.assertEqual(episode["season_id"], "211")
        self.assertEqual(episode["server_position_ms"], 600_000)
        self.assertEqual(episode["ename"], "The Second Door")
        self.assertEqual([e["name"] for e in second["episodes"]], ["A New Year"])
        self.assertEqual(second["overview"], "Later.")


class SeasonsGuardTest(unittest.TestCase):  # review M11
    def test_only_seasons_are_seasons_and_no_episode_is_left_out(self):
        seasons = fixture("show_children.json")["MediaContainer"]["Metadata"]
        episodes = fixture("show_allLeaves.json")["MediaContainer"]["Metadata"]
        everything = {"key": "/library/metadata/201/allLeaves", "type": "episode", "title": "All"}
        stray = {
            **episodes[2],
            "ratingKey": "241",
            "parentRatingKey": "214",
            "parentIndex": 4,
            "parentTitle": "Season 4",
            "index": 1,
            "title": "A Stray One",
        }
        payload = plex.seasons_payload([everything, *seasons], [*episodes, stray], BASE, TOKEN)
        names = [s["name"] for s in payload["seasons"]]
        self.assertEqual(names, ["Season 1", "Season 2", "Season 4"])
        fourth = payload["seasons"][2]
        self.assertEqual((fourth["id"], fourth["season_number"]), ("214", "4"))
        self.assertEqual([e["name"] for e in fourth["episodes"]], ["A Stray One"])

    def test_episodes_of_a_series_without_seasons_still_show(self):
        episodes = fixture("show_allLeaves.json")["MediaContainer"]["Metadata"]
        payload = plex.seasons_payload([], episodes, BASE, TOKEN)
        self.assertEqual(sum(len(s["episodes"]) for s in payload["seasons"]), 3)


class SeriesPagesTest(unittest.TestCase):
    def test_a_long_series_episodes_come_in_pages_too(self):
        leaves = fixture("show_allLeaves.json")["MediaContainer"]["Metadata"]
        seasons = fixture("show_children.json")["MediaContainer"]["Metadata"]
        asked = []

        class Paged(FakeHttp):
            def request(self, method, url, params=None, headers=None, **kw):
                from tests.test_plex import _Response

                path = url[len(BASE) :]
                start = int(headers["X-Plex-Container-Start"])
                size = int(headers["X-Plex-Container-Size"])
                asked.append((path, start))
                listed = leaves if path.endswith("allLeaves") else seasons
                batch = listed[start : start + min(size, 2)]  # the server cuts at 2
                container = {"size": len(batch), "totalSize": len(listed), "offset": start}
                return _Response(200, {"MediaContainer": {**container, "Metadata": batch}})

        c = plex.PlexClient(BASE, token=TOKEN, client_id="c", version="v", session=Paged(BASE, {}))
        found_seasons, episodes = c.seasons_and_episodes("201")
        self.assertEqual(len(found_seasons), 2)
        self.assertEqual(sorted(e["ratingKey"] for e in episodes), ["221", "222", "231"])
        self.assertIn(("/library/metadata/201/allLeaves", 2), asked)


class VersionsTest(unittest.TestCase):
    def setUp(self):
        meta = fixture("movie_metadata.json")
        self.c, self.http = client({("GET", "/library/metadata/102"): (200, meta)})
        self.sources = plex.sources(self.c.media("102"))

    def test_each_version_as_the_quality_menu_lists_it(self):
        self.assertEqual(
            [ms.version_of(s) for s in self.sources],
            [
                {"id": "2", "height": 2160, "bitrate": 30_425_000, "uhd": True, "name": ""},
                {"id": "902", "height": 1080, "bitrate": 8_200_000, "uhd": False, "name": ""},
            ],
        )

    def test_the_settings_rules_pick_the_version(self):
        small, _ = ms.choose_source(self.sources, screen_4k=False, setting=ms.FOUR_K_SCREEN)
        self.assertEqual(small["Id"], "902")
        large, _ = ms.choose_source(self.sources, screen_4k=True, setting=ms.FOUR_K_SCREEN)
        self.assertEqual(large["Id"], "2")

    def test_the_stream_is_the_file_itself_never_a_conversion(self):
        source = self.sources[1]
        url = plex.stream_url(BASE, source, TOKEN, "client-1", "102")
        self.assertTrue(url.startswith(BASE + "/library/parts/902/1792861808/file.mkv?"))
        self.assertNotIn("transcode", url)
        asked = query(url)
        self.assertEqual(asked["X-Plex-Token"], TOKEN)
        self.assertEqual(asked["X-Plex-Client-Identifier"], "client-1")
        self.assertEqual(asked["X-Plex-Product"], "QiTV")
        session = asked["X-Plex-Session-Identifier"]
        self.assertTrue(session.startswith("102-"))
        self.assertNotEqual(
            session,
            query(plex.stream_url(BASE, source, TOKEN, "c", "102"))["X-Plex-Session-Identifier"],
        )

    def test_the_reporter_reads_what_plays_back_from_the_address(self):
        url = plex.stream_url(BASE, self.sources[0], TOKEN, "client-1", "102")
        ref = plex.parse_stream_url(url, ["http://other.test:32400", BASE + "/"])
        self.assertEqual(
            (ref.base, ref.item_id, ref.source_id, ref.token, ref.converted),
            (BASE, "102", "2", TOKEN, False),
        )
        self.assertEqual(ref.play_session, query(url)["X-Plex-Session-Identifier"])
        self.assertIsNone(plex.parse_stream_url(url, ["http://other.test:32400"]))
        self.assertIsNone(plex.parse_stream_url(BASE + "/library/parts/2/1/file.mov", [BASE]))
        jellyfin = "http://server.test:8096/Videos/f1/stream.mkv?api_key=t"
        self.assertIsNone(plex.parse_stream_url(jellyfin, ["http://server.test:8096"]))


class ReportTest(unittest.TestCase):
    def setUp(self):
        plex.forget_durations()
        meta = fixture("movie_metadata.json")  # 46,699 ms
        self.c, self.http = client(
            {
                ("GET", "/library/metadata/102"): (200, meta),
                ("POST", "/:/timeline"): (200, None),
            }
        )
        source = plex.sources(self.c.media("102"))[0]
        url = plex.stream_url(BASE, source, TOKEN, "client-1", "102")
        self.ref = plex.parse_stream_url(url, [BASE])
        self.http.asked.clear()
        plex.forget_durations()  # (the link's ask learnt its length: asked again here)

    def timelines(self):
        return [a for a in self.http.asked if a["path"] == "/:/timeline"]

    def test_start_progress_and_stop_go_through_the_timeline(self):
        self.c.report("start", self.ref, 0)
        self.c.report("progress", self.ref, 10_000, paused=True)
        self.c.report("stopped", self.ref, 20_000)
        states = [(a["params"]["state"], a["params"]["time"]) for a in self.timelines()]
        self.assertEqual(states, [("playing", 0), ("paused", 10_000), ("stopped", 20_000)])
        first = self.timelines()[0]
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["params"]["ratingKey"], "102")
        self.assertEqual(first["params"]["key"], "/library/metadata/102")
        self.assertEqual(first["params"]["duration"], 46_699)
        self.assertEqual(first["headers"]["X-Plex-Token"], TOKEN)
        self.assertEqual(first["headers"]["X-Plex-Session-Identifier"], self.ref.play_session)
        # The title's length was asked once (the start's), then remembered.
        self.assertEqual([a["path"] for a in self.http.asked].count("/library/metadata/102"), 1)

    def test_the_length_the_links_ask_learnt_is_not_asked_again(self):
        self.c.media("102")
        self.http.asked.clear()
        self.c.report("start", self.ref, 0)
        self.assertEqual([a["path"] for a in self.http.asked], ["/:/timeline"])
        self.assertEqual(self.timelines()[0]["params"]["duration"], 46_699)

    def test_a_stop_near_the_end_is_only_a_stop_the_server_marks_watched_itself(self):
        self.c.report("stopped", self.ref, 45_000)
        self.assertEqual(
            [a["path"] for a in self.http.asked], ["/library/metadata/102", "/:/timeline"]
        )
        self.assertEqual(self.timelines()[0]["params"]["duration"], 46_699)

    def test_a_length_unknown_is_left_out(self):
        self.http.answers[("GET", "/library/metadata/102")] = (500, None)
        with self.assertLogs("services.plex", "INFO"):
            self.c.report("stopped", self.ref, 45_000)
        self.assertNotIn("duration", self.timelines()[0]["params"])


if __name__ == "__main__":
    unittest.main()
