"""Jellyfin and Emby (2.8): sign-in, the libraries' films and series as Library titles, the
version a title plays in (never converted, no 4K unless asked) and its stream's address."""

import json
import unittest

from services import media_server as ms
from services.catalog import ALL_GROUP_ID

BASE = "http://server.test:8096"


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    """requests.Session as the client uses it: answers by (method, path), records the asks."""

    def __init__(self, answers):
        self.answers = answers  # (method, path) -> (status, body)
        self.asked = []
        self.headers = {}

    def request(self, method, url, params=None, json=None, timeout=None, verify=True):
        path = url[len(BASE) :]
        self.asked.append((method, path, dict(params or {}), json))
        status, body = self.answers.get((method, path), (404, None))
        return _Response(status, body)

    def close(self):
        pass


def client(answers, kind=ms.JELLYFIN, **kwargs):
    http = FakeHttp(answers)
    defaults = {"device_id": "dev-1", "version": "2.8.0", "token": "tok", "user_id": "u1"}
    defaults.update(kwargs)
    return ms.MediaServerClient(BASE, kind, session=http, **defaults), http


FILM = {
    "Id": "f1",
    "Name": "Salt Roads",
    "Type": "Movie",
    "ProductionYear": 2021,
    "CommunityRating": 7.4,
    "Overview": "A long road.",
    "Genres": ["Drama", "Road"],
    "RunTimeTicks": 72_000_000_000,
    "ProviderIds": {"Tmdb": "4242", "Imdb": "tt1"},
    "DateCreated": "2026-10-20T08:00:00.0000000Z",
    "ImageTags": {"Primary": "tag1"},
    "UserData": {"PlaybackPositionTicks": 6_000_000_000, "Played": False},
    "Container": "mkv",
}


class HeaderTests(unittest.TestCase):
    def test_the_client_names_qitv_and_no_computer(self):
        header = ms.auth_header("dev-1", "2.8.0", token="tok", system="Darwin")
        self.assertEqual(
            header,
            'MediaBrowser Client="QiTV", Device="QiTV (macOS)", DeviceId="dev-1", '
            'Version="2.8.0", Token="tok"',
        )
        self.assertNotIn("Token", ms.auth_header("dev-1", "2.8.0", system="Linux"))


class SignInTests(unittest.TestCase):
    def test_a_sign_in_keeps_the_token_and_the_user(self):
        answers = {
            ("POST", "/Users/AuthenticateByName"): (
                200,
                {"AccessToken": "T", "User": {"Id": "U", "Name": "me"}},
            )
        }
        c, http = client(answers, token="", user_id="")
        self.assertEqual(c.sign_in("me", "pw"), ("T", "U"))
        method, path, _params, body = http.asked[-1]
        self.assertEqual(body, {"Username": "me", "Pw": "pw"})

    def test_a_refused_sign_in_says_so(self):
        c, _http = client({("POST", "/Users/AuthenticateByName"): (401, None)})
        with self.assertRaises(ms.SignInNeeded):
            c.sign_in("me", "wrong")


class RouteTests(unittest.TestCase):
    def test_jellyfin_asks_the_new_routes_first_and_old_ones_when_missing(self):
        views = {"Items": [{"Id": "L1", "Name": "Movies", "CollectionType": "movies"}]}
        c, http = client({("GET", "/Users/u1/Views"): (200, views)})  # an older Jellyfin
        self.assertEqual(c.libraries(), [{"id": "L1", "name": "Movies", "type": "movies"}])
        self.assertEqual([a[1] for a in http.asked], ["/UserViews", "/Users/u1/Views"])

    def test_emby_asks_the_classic_routes(self):
        views = {"Items": [{"Id": "L1", "Name": "TV", "CollectionType": "tvshows"}]}
        c, http = client({("GET", "/Users/u1/Views"): (200, views)}, kind=ms.EMBY)
        self.assertEqual(c.libraries()[0]["type"], "tvshows")
        self.assertEqual([a[1] for a in http.asked], ["/Users/u1/Views"])

    def test_libraries_without_films_or_series_are_left_out(self):
        views = {
            "Items": [
                {"Id": "L1", "Name": "Music", "CollectionType": "music"},
                {"Id": "L2", "Name": "Films", "CollectionType": "movies"},
                {"Id": "L3", "Name": "Mixed"},
            ]
        }
        c, _http = client({("GET", "/UserViews"): (200, views)})
        self.assertEqual([lib["id"] for lib in c.libraries()], ["L2", "L3"])

    def test_items_come_page_by_page(self):
        first = {"Items": [{"Id": str(i)} for i in range(3)], "TotalRecordCount": 5}
        c, http = client({})
        pages = [first, {"Items": [{"Id": "3"}, {"Id": "4"}], "TotalRecordCount": 5}]
        http.request = lambda method, url, params=None, **kw: (
            http.asked.append((method, url, dict(params))) or _Response(200, pages.pop(0))
        )
        got = c.items("Movie", "L1", page=3)
        self.assertEqual([i["Id"] for i in got], ["0", "1", "2", "3", "4"])
        self.assertEqual([a[2]["StartIndex"] for a in http.asked], [0, 3])
        self.assertEqual(http.asked[0][2]["ParentId"], "L1")


class ItemTests(unittest.TestCase):
    def test_a_film_becomes_a_library_title(self):
        item = ms.title_item(FILM, BASE, "L1")
        self.assertEqual(item["id"], "f1")
        self.assertEqual(item["name"], "Salt Roads")
        self.assertEqual(item["tv_genre_id"], "L1")
        self.assertEqual(item["year"], "2021")
        self.assertEqual(item["rating"], "7.4")
        self.assertEqual(item["description"], "A long road.")
        self.assertEqual(item["genre"], "Drama, Road")
        self.assertEqual(item["duration_secs"], 7200)
        self.assertEqual(item["tmdb"], "4242")
        self.assertEqual(item["added"], "1792483200")
        self.assertEqual(item["server_position_ms"], 600_000)
        self.assertEqual(item["container_extension"], "mkv")
        self.assertEqual(item["cmd"], "")
        self.assertEqual(item["logo"], f"{BASE}/Items/f1/Images/Primary?maxHeight=360&tag=tag1")

    def test_a_long_description_is_kept_short(self):
        long = {"Id": "x", "Name": "Long", "Overview": "word " * 200}
        text = ms.title_item(long, BASE, "L1")["description"]
        self.assertLessEqual(len(text), ms.OVERVIEW_CHARS + 1)
        self.assertTrue(text.endswith("word…"))

    def test_an_error_never_names_the_address(self):
        c, _http = client(
            {("GET", "/UserViews"): (500, None), ("GET", "/Users/u1/Views"): (500, None)}
        )
        with self.assertRaises(ms.MediaServerError) as caught:
            c.libraries()
        self.assertEqual(str(caught.exception), "The server answered 500")

    def test_a_title_without_a_picture_or_a_date_has_none(self):
        item = ms.title_item({"Id": "x", "Name": "Bare"}, BASE, "L1")
        self.assertEqual((item["logo"], item.get("added")), ("", None))
        self.assertNotIn("server_position_ms", item)

    def test_the_section_lists_libraries_with_all_and_continue_first(self):
        payload = ms.section_payload(
            [{"id": "L1", "name": "Movies", "type": "movies"}],
            {"L1": [FILM, {"Id": "f2", "Name": "Tide"}]},
            BASE,
        )
        ids = [c["id"] for c in payload["categories"]]
        self.assertEqual(ids, [ALL_GROUP_ID, ms.CONTINUE_ID, "L1"])
        self.assertEqual(payload["sorted_channels"][ms.CONTINUE_ID], [0])  # it has a place
        self.assertEqual(payload["sorted_channels"]["L1"], [0, 1])

    def test_a_series_episodes_are_grouped_into_its_seasons(self):
        seasons = [{"Id": "s1", "Name": "Season 1", "IndexNumber": 1}]
        episodes = [
            {"Id": "e2", "Name": "Two", "IndexNumber": 2, "SeasonId": "s1", "SeriesId": "show"},
            {"Id": "e1", "Name": "One", "IndexNumber": 1, "SeasonId": "s1", "SeriesId": "show"},
        ]
        (season,) = ms.seasons_payload(seasons, episodes, BASE)["seasons"]
        self.assertEqual((season["id"], season["season_number"]), ("s1", "1"))
        self.assertEqual([e["ename"] for e in season["episodes"]], ["One", "Two"])
        first = season["episodes"][0]
        self.assertEqual(
            (first["series_id"], first["season_id"], first["season"]), ("show", "s1", "1")
        )


class SourceTests(unittest.TestCase):
    def source(self, sid, width, height, bitrate=10_000_000):
        video = {"Type": "Video", "Width": width, "Height": height}
        return {"Id": sid, "Container": "mkv", "Bitrate": bitrate, "MediaStreams": [video]}

    def test_below_a_4k_screen_the_best_version_that_is_not_4k_plays(self):
        sources = [self.source("uhd", 3840, 2160), self.source("hd", 1920, 1080)]
        source, why = ms.choose_source(sources, screen_4k=False, setting=ms.FOUR_K_SCREEN)
        self.assertEqual((source["Id"], why), ("hd", None))

    def test_a_4k_screen_gets_the_4k_version(self):
        sources = [self.source("uhd", 3840, 2160), self.source("hd", 1920, 1080)]
        source, _ = ms.choose_source(sources, screen_4k=True, setting=ms.FOUR_K_SCREEN)
        self.assertEqual(source["Id"], "uhd")

    def test_a_title_only_in_4k_plays_as_it_is_below_a_4k_screen(self):
        # Nothing is converted: the computer scales the picture down (the owner, 2.8).
        sources = [self.source("uhd", 3840, 2160)]
        source, why = ms.choose_source(sources, screen_4k=False, setting=ms.FOUR_K_SCREEN)
        self.assertEqual((source["Id"], why), ("uhd", None))

    def test_always_takes_the_4k_version_below_a_4k_screen(self):
        sources = [self.source("uhd", 3840, 2160), self.source("hd", 1920, 1080)]
        source, _ = ms.choose_source(sources, screen_4k=False, setting=ms.FOUR_K_ALWAYS)
        self.assertEqual(source["Id"], "uhd")

    def test_never_refuses_a_title_only_in_4k(self):
        sources = [self.source("uhd", 3840, 2160)]
        self.assertEqual(
            ms.choose_source(sources, screen_4k=True, setting=ms.FOUR_K_NEVER), (None, ms.ONLY_4K)
        )


class StreamUrlTests(unittest.TestCase):
    def test_the_file_plays_as_it_is_with_the_token(self):
        url = ms.stream_url(BASE, "f1", {"Id": "ms1", "Container": "mkv,webm"}, "tok", "ps1")
        self.assertEqual(
            url,
            f"{BASE}/Videos/f1/stream.mkv?static=true&MediaSourceId=ms1&PlaySessionId=ps1"
            "&api_key=tok",
        )

    def test_a_stream_address_tells_what_it_plays(self):
        url = ms.stream_url(BASE, "f1", {"Id": "ms1", "Container": "mp4"}, "tok", "ps1")
        ref = ms.parse_stream_url(url, [BASE])
        self.assertEqual(
            (ref.base, ref.item_id, ref.source_id, ref.token, ref.play_session),
            (BASE, "f1", "ms1", "tok", "ps1"),
        )
        self.assertIsNone(ms.parse_stream_url("http://other.test/Videos/f1/stream.mkv", [BASE]))

    def test_a_server_on_this_computer_is_reached_by_its_home_address_from_a_phone(self):
        url = "http://localhost:8096/Videos/f1/stream.mkv?static=true&api_key=t"
        self.assertEqual(
            ms.phone_reachable(url, "192.168.1.20"),
            "http://192.168.1.20:8096/Videos/f1/stream.mkv?static=true&api_key=t",
        )
        other = "https://media.example.test/Videos/f1/stream.mkv"
        self.assertEqual(ms.phone_reachable(other, "192.168.1.20"), other)
        self.assertEqual(ms.phone_reachable(url, None), url)

    def test_a_converted_stream_is_capped_and_still_known(self):
        url = ms.converted_url(BASE, "f1", {"Id": "ms1"}, "tok", "ps1", 8_000_000, "dev-1")
        self.assertIn("/Videos/f1/master.m3u8?", url)
        self.assertIn("MaxStreamingBitrate=8000000", url)
        self.assertIn("VideoCodec=h264", url)
        self.assertEqual(ms.parse_stream_url(url, [BASE]).item_id, "f1")

    def test_a_conversion_asks_for_its_height_too(self):  # 2.8: the quality menu
        url = ms.converted_url(
            BASE, "f1", {"Id": "ms1"}, "tok", "ps1", 4_000_000, "dev-1", max_height=720
        )
        self.assertIn("MaxStreamingBitrate=4000000", url)
        self.assertIn("MaxHeight=720", url)
        self.assertNotIn("MaxHeight", ms.converted_url(BASE, "f1", {}, "t", "p", 1, "d"))


class QualityTests(unittest.TestCase):  # 2.8: the quality menu's choices
    def test_a_version_is_known_by_its_size_and_rate(self):
        uhd = SourceTests.source(None, "uhd", 3840, 2160, 25_000_000)
        hd = SourceTests.source(None, "hd", 1920, 1080, 8_200_000)
        self.assertEqual(
            [ms.version_of(s) for s in (uhd, hd)],
            [
                {"id": "uhd", "height": 2160, "bitrate": 25_000_000, "uhd": True, "name": ""},
                {"id": "hd", "height": 1080, "bitrate": 8_200_000, "uhd": False, "name": ""},
            ],
        )

    def test_the_conversions_offered_step_down(self):
        self.assertEqual(
            [(q.height, q.bitrate) for q in ms.CONVERSIONS],
            [(1080, 8_000_000), (720, 4_000_000), (480, 2_000_000)],
        )
        self.assertTrue(all(q.converted for q in ms.CONVERSIONS))
        self.assertFalse(ms.Quality(source="hd").converted)
        self.assertFalse(ms.Quality().converted)


if __name__ == "__main__":
    unittest.main()
