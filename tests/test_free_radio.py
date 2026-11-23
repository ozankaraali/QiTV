"""Free radio (2.13): radio-browser.info's stations, added as an M3U provider whose list is the
directory's station JSON (local fixtures only: made-up stations, never the network)."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid as uuid_module

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import free_channels, free_radio as fr, i18n, provider_check, workers
from services.config_manager import get_app_version
from services.m3u import order_cached_groups, parse_stations, stations_in
from services.navigation import item_key
from services.parental import Parental
from services.provider_api import CLIENT_HEADERS
from services.provider_manager import ProviderManager
from services.watch_state import WatchState
from tests.test_parental import FakeClock, FakeConfig

COUNTRY_URL = (
    "https://all.api.radio-browser.info/json/stations/bycountrycodeexact/GB"
    "?hidebroken=true&order=clickcount&reverse=true&limit=2000"
)
ALL_URL = "https://all.api.radio-browser.info/json/stations/topclick/2000?hidebroken=true"


def station(name, link, *, tags="", country="TR", favicon="", url="", uuid=None):
    """One station as the directory lists it (the fields QiTV reads, and some it doesn't); its
    `stationuuid` is made from its name and link unless given ("" for none)."""
    if uuid is None:
        uuid = str(uuid_module.uuid5(uuid_module.NAMESPACE_URL, f"{name}|{link}|{url}"))
    return {
        "changeuuid": "00000000-0000-0000-0000-000000000000",
        "stationuuid": uuid,
        "name": name,
        "url": url or link,
        "url_resolved": link,
        "homepage": "https://home.test/",
        "favicon": favicon,
        "tags": tags,
        "countrycode": country,
        "country": "",
        "codec": "MP3",
        "bitrate": 128,
        "hls": 0,
        "lastcheckok": 1,
        "clickcount": 10,
    }


STATIONS = [  # most played first, as the directory answers
    station(
        "  Wexlo FM ",
        "http://stream.test/wexlo",
        tags="pop,turkish pop",
        favicon="https://logo.test/w.png",
    ),
    station("Pell Rock", "http://stream.test/pell", tags="pop rock,classic rock"),
    station("Quiet Corner", "http://stream.test/quiet"),
    station("Tide Hits", "http://stream.test/tide", tags="Top 40"),
    station("News Hour Test", "", url="http://stream.test/news.pls", tags="news,talk"),
]


class EntryTests(unittest.TestCase):
    def test_a_country(self):
        self.assertEqual(
            fr.provider_entry("uk"),  # iptv-org's code; the directory's is ISO 3166's
            {"type": "M3UPLAYLIST", "name": "Radio: United Kingdom", "url": COUNTRY_URL},
        )
        self.assertEqual(fr.iso_code("uk"), "GB")
        self.assertEqual(fr.iso_code("tr"), "TR")

    def test_all_countries(self):
        self.assertEqual(
            fr.provider_entry(None),
            {"type": "M3UPLAYLIST", "name": "Radio: all countries", "url": ALL_URL},
        )

    def test_the_country_list_is_free_channels_without_international(self):
        codes = [code for code, _name in free_channels.COUNTRIES]
        self.assertEqual(list(fr.COUNTRY_CODES), [code for code in codes if code != "int"])

    def test_the_same_list_is_the_same_address_whatever_its_query(self):
        entry = fr.provider_entry("tr")
        self.assertTrue(fr.same_list(dict(entry), entry))
        lighter = dict(entry, url=entry["url"].replace("limit=2000", "limit=500"))
        self.assertTrue(fr.same_list(lighter, entry))  # a later build's other limit
        self.assertFalse(fr.same_list(fr.provider_entry("de"), entry))
        self.assertFalse(fr.same_list(fr.provider_entry(None), entry))
        self.assertFalse(fr.same_list(free_channels.provider_entry("tr"), entry))
        self.assertTrue(fr.same_list(fr.provider_entry(None), fr.provider_entry(None)))

    def test_the_directory_is_known_by_its_host_and_path(self):
        for url in (ALL_URL, COUNTRY_URL, "https://de1.api.radio-browser.info/json/stations/x"):
            self.assertTrue(fr.is_directory_url(url), url)
        for url in (
            "https://all.api.radio-browser.info/m3u/stations/topclick/10",
            "https://api.radio-browser.info.evil.test/json/stations/x",
            "https://notapi.radio-browser.info/json/stations/x",
            "https://iptv-org.github.io/iptv/index.country.m3u",
            "/home/user/stations.json",
            "",
        ):
            self.assertFalse(fr.is_directory_url(url), url)

    def test_the_directory_hears_qitvs_own_name_and_the_rest_vlcs(self):
        self.assertEqual(fr.headers_for(ALL_URL), {"User-Agent": f"QiTV v{get_app_version()}"})
        self.assertEqual(fr.headers_for("http://panel.test/get.php"), CLIENT_HEADERS)


class GenreTests(unittest.TestCase):
    def test_the_groups_in_the_order_they_are_tried(self):
        self.assertEqual(
            [name for name, _words in fr.GENRES] + [fr.OTHER],
            [
                "News & talk",
                "Classical",
                "Jazz & blues",
                "Electronic & dance",
                "Rock",
                "Pop",
                "Folk & traditional",
                "Religious",
                "Kids",
                "Other",
            ],
        )

    def test_a_stations_tags_give_its_group(self):
        for tags, group in (
            ("pop,turkish pop", "Pop"),
            ("arabesk fantazi", "Folk & traditional"),
            ("fantezi", "Folk & traditional"),
            ("turkish music", "Folk & traditional"),
            ("turkish music,pop", "Pop"),  # the table's order
            ("türk sanat müziği", "Classical"),  # Turkish art music
            ("Türk Halk Müziği", "Folk & traditional"),
            ("pop rock", "Rock"),  # the first group in the table's order wins
            ("rock,news", "News & talk"),  # whatever the tags' order
            ("classic rock", "Rock"),  # whole words: no Classical
            ("classic hits", "Pop"),
            ("Top 40", "Pop"),
            ("80s,90s", "Pop"),
            ("sports", "News & talk"),
            ("Smooth Jazz", "Jazz & blues"),
            ("deep house", "Electronic & dance"),
            ("TÜRKÜ", "Folk & traditional"),
            ("halk müziği", "Folk & traditional"),
            ("ilahi،dini", "Religious"),  # an Arabic comma between them
            ("İlahi", "Religious"),  # Turkish capitals
            ("kids", "Kids"),
            ("operator", "Other"),  # no word of the table in it ("opera")
            ("local radio,fm", "Other"),
            ("", "Other"),
        ):
            self.assertEqual(fr.genre_of(tags), group, tags)

    def test_the_group_names_are_marked_for_translation(self):
        path = os.path.join(os.path.dirname(__file__), "..", "i18n", "template.json")
        with open(path, encoding="utf-8") as f:
            template = json.load(f)
        names = [name for name, _words in fr.GENRES] + [fr.OTHER, fr.HELP_TEXT, fr.ALL_STATIONS]
        for name in names:
            self.assertIn(name, template)


class StationsTests(unittest.TestCase):
    def test_the_directorys_json_is_known_by_its_content(self):
        raw = json.dumps(STATIONS).encode()
        self.assertEqual(len(stations_in(raw)), len(STATIONS))
        self.assertEqual(len(stations_in(" \n" + json.dumps(STATIONS))), len(STATIONS))
        self.assertIsNone(stations_in(b"#EXTM3U\n#EXTINF:-1,One\nhttp://s.test/1\n"))
        self.assertIsNone(stations_in(b'[{"name": "no stream here"}]'))
        self.assertIsNone(stations_in(b"[not json"))
        self.assertIsNone(stations_in(b'{"url_resolved": "http://s.test/1"}'))  # no list

    def test_an_empty_list_is_the_directorys_only_from_the_directory(self):
        self.assertEqual(stations_in(b"[]", directory=True), [])  # a country with no station
        self.assertIsNone(stations_in(b"[]"))
        self.assertIsNone(stations_in(b'{"error": "none"}', directory=True))  # no list

    def test_each_station_is_a_channel_as_an_m3u_line_makes_it(self):
        playlist = parse_stations(STATIONS)
        contents = playlist["contents"]
        self.assertEqual(
            contents[0],
            {
                "id": STATIONS[0]["stationuuid"],  # the directory's own: kept across refreshes
                "group": "Pop",
                "xmltv_id": None,
                "name": "Wexlo FM",  # its spaces gone
                "logo": "https://logo.test/w.png",
                "user_agent": None,
                "cmd": "http://stream.test/wexlo",
                "radio_station": True,  # its sound read as a station's (2.14)
                "radio_codec": "MP3",  # the directory's name for it (free_radio.plays_directly)
                "radio_codepage": "cp1254",  # its song names', when no UTF-8 (its country TR)
                "tv_genre_id": "Pop",
                "number": "1",
            },
        )
        self.assertIsNone(contents[1]["logo"])  # no favicon: no logo
        # Most played first, as the directory answered; one with no resolved link plays its
        # listed one.
        self.assertEqual(
            [(c["name"], c["cmd"]) for c in contents],
            [
                ("Wexlo FM", "http://stream.test/wexlo"),
                ("Pell Rock", "http://stream.test/pell"),
                ("Quiet Corner", "http://stream.test/quiet"),
                ("Tide Hits", "http://stream.test/tide"),
                ("News Hour Test", "http://stream.test/news.pls"),
            ],
        )

    def test_a_station_keeps_its_id_when_the_list_comes_sorted_anew(self):
        # The directory sorts by clicks: a refresh moves stations. Hidden stations, the
        # history, Recent and the last channel key on the id: it is the station's own.
        first = parse_stations(STATIONS)["contents"]
        again = parse_stations(list(reversed(STATIONS)))["contents"]
        by_name = {item["name"]: item for item in again}
        self.assertNotEqual([c["name"] for c in first], [c["name"] for c in again])
        for item in first:
            moved = by_name[item["name"]]
            self.assertEqual(item_key(moved), item_key(item))
            self.assertEqual(
                WatchState.content_id("Radio: Türkiye", moved, item_type="channel"),
                WatchState.content_id("Radio: Türkiye", item, item_type="channel"),
            )
        guard = Parental(FakeConfig(), clock=FakeClock())
        guard.set_pin("1234")
        self.assertTrue(guard.hide_item("live", first[1]))  # Pell Rock
        hidden = [item["name"] for item in again if guard.protects("live", item)]
        self.assertEqual(hidden, ["Pell Rock"])

    def test_a_station_without_its_uuid_is_numbered_by_its_place(self):
        stations = [station("Bare Test", "http://s.test/bare", uuid="")]
        self.assertEqual(parse_stations(stations)["contents"][0]["id"], 1)

    def test_one_countrys_stations_are_grouped_by_genre(self):
        playlist = parse_stations(STATIONS)
        self.assertEqual(
            playlist["categories"],
            [
                {"id": "*", "title": "All stations"},  # every station, most played first
                {"id": "News & talk", "title": "News & talk"},
                {"id": "Pop", "title": "Pop"},
                {"id": "Rock", "title": "Rock"},
                {"id": "Other", "title": "Other"},  # last
            ],
        )
        self.assertEqual(playlist["sorted_channels"]["Pop"], [0, 3])
        self.assertIs(playlist[fr.RADIO_KEY], True)

    def test_stations_of_many_countries_are_grouped_by_country(self):
        stations = [
            station("Alpha Test", "http://s.test/a", country="TR", tags="pop"),
            station("Bravo Test", "http://s.test/b", country="GB"),
            station("Charlie Test", "http://s.test/c", country="AQ"),  # no list of its own
            station("Delta Test", "http://s.test/d", country=""),
            station("Echo Test", "http://s.test/e", country="tr"),
        ]
        playlist = parse_stations(stations)
        self.assertEqual(
            [(c["id"], c["title"]) for c in playlist["categories"]],
            [
                ("*", "All stations"),
                ("Türkiye", "Türkiye"),
                ("United Kingdom", "United Kingdom"),
                ("Other", "Other"),  # after the countries
            ],
        )
        self.assertEqual(playlist["sorted_channels"]["Türkiye"], [0, 4])
        self.assertEqual(playlist["sorted_channels"]["Other"], [2, 3])

    def test_stations_without_a_link_are_left_out(self):
        stations = [
            station("Gone Test", "", url=""),
            station("Odd Test", "not a link"),
            station("", "http://s.test/unnamed/live.mp3"),  # named by its link, as M3U
            "not a station",
            station("Kept Test", "http://s.test/kept"),
        ]
        with self.assertLogs("services.m3u", "WARNING") as logged:
            playlist = parse_stations(stations)
        self.assertEqual([c["name"] for c in playlist["contents"]], ["live.mp3", "Kept Test"])
        self.assertNotIn("s.test", " ".join(logged.output))  # never a link in the log

    def test_a_station_plays_directly_only_in_a_sound_the_helper_cannot_read(self):  # 2.14
        station = {"cmd": "http://stream.test/a", fr.STATION_KEY: True}
        for codec, direct in (
            ("MP3", False),
            ("AAC+", False),
            ("aac", False),
            ("MP3,H.264", False),  # an HLS station's picture and sound
            ("UNKNOWN", False),  # tried: it falls back once the helper cannot read it
            ("", False),
            (None, False),  # a list 2.13 made names none
            ("OGG", True),
            ("FLAC", True),
            ("OPUS", True),
            ("WMA", True),
            ("AAC,VP9", True),
        ):
            with self.subTest(codec=codec):
                item = dict(station) if codec is None else {**station, fr.CODEC_KEY: codec}
                self.assertEqual(fr.plays_directly(item), direct)
        self.assertFalse(fr.plays_directly({"cmd": "x", fr.CODEC_KEY: "OGG"}))  # no station
        self.assertFalse(fr.plays_directly(None))
        codecs = [c[fr.CODEC_KEY] for c in parse_stations(STATIONS)["contents"]]
        self.assertEqual(set(codecs), {"MP3"})

    def test_a_stations_song_names_code_page_is_its_languages(self):  # 2.14
        for fields, page in (
            ({"languagecodes": "tr", "countrycode": "DE"}, "cp1254"),  # a Turkish station abroad
            ({"language": "turkish", "countrycode": "NL"}, "cp1254"),
            ({"countrycode": "TR"}, "cp1254"),
            ({"languagecodes": "en,ru", "countrycode": "GB"}, "cp1251"),  # the first that has one
            ({"languagecodes": "uk"}, "cp1251"),
            ({"countrycode": "BG"}, "cp1251"),
            ({"languagecodes": "el"}, "cp1253"),
            ({"languagecodes": "he"}, "cp1255"),
            ({"languagecodes": "ar"}, "cp1256"),
            ({"language": "persian"}, "cp1256"),
            ({"countrycode": "LT"}, "cp1257"),
            ({"languagecodes": "pl"}, "cp1250"),
            ({"countrycode": "CZ"}, "cp1250"),
            ({"languagecodes": "fr", "countrycode": "FR"}, "cp1252"),
            ({}, "cp1252"),
        ):
            with self.subTest(fields=fields):
                self.assertEqual(fr.codepage_of(fields), page)
        turkish = {**STATIONS[0], "languagecodes": "tr"}
        self.assertEqual(parse_stations([turkish])["contents"][0][fr.CODEPAGE_KEY], "cp1254")
        self.assertEqual(fr.codepage({"cmd": "x"}), "cp1252")  # a list 2.13 made names none
        self.assertEqual(fr.codepage({fr.CODEPAGE_KEY: "cp1254"}), "cp1254")
        self.assertEqual(fr.codepage(None), "cp1252")

    def test_no_station_is_an_empty_list(self):
        playlist = parse_stations([])
        self.assertEqual(playlist["categories"], [{"id": "*", "title": "All stations"}])
        self.assertEqual(playlist["contents"], [])


TURKISH = {
    "All stations": "Tüm istasyonlar",
    "News & talk": "Haberler ve sohbet",
    "Other": "Diğer",
    "Türkiye": "Türkiye",
}


class TranslatedGroupsTests(unittest.TestCase):
    """The groups show in the active language, in its alphabet, when the list loads and when
    its cache is read again (the language is chosen at the start)."""

    def turkish(self):
        before = i18n.language()
        self.addCleanup(i18n.set_language, before)
        i18n.set_language("tr")
        patcher = mock.patch.dict(i18n._catalog, TURKISH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_list_loaded_in_turkish(self):
        self.turkish()
        playlist = parse_stations(STATIONS)
        self.assertEqual(
            [(c["id"], c["title"]) for c in playlist["categories"]],
            [
                ("*", "Tüm istasyonlar"),
                ("News & talk", "Haberler ve sohbet"),  # by the shown names
                ("Pop", "Pop"),
                ("Rock", "Rock"),
                ("Other", "Diğer"),  # last, whatever its name
            ],
        )

    def test_a_cached_list_takes_the_language_of_the_start(self):
        payload = parse_stations(STATIONS)  # in English
        self.turkish()
        fr.retitle(payload)
        order_cached_groups(payload)
        self.assertEqual(
            [(c["id"], c["title"]) for c in payload["categories"]],
            [
                ("*", "Tüm istasyonlar"),
                ("News & talk", "Haberler ve sohbet"),
                ("Pop", "Pop"),
                ("Rock", "Rock"),
                ("Other", "Diğer"),
            ],
        )

    def test_another_playlists_groups_stay_as_they_are(self):
        payload = {
            "categories": [{"id": "*", "title": "All"}, {"id": "Other", "title": "Other"}],
            "contents": [],
            "sorted_channels": {},
        }
        self.turkish()
        fr.retitle(payload)
        self.assertEqual([c["title"] for c in payload["categories"]], ["All", "Other"])

    def test_the_provider_manager_retitles_a_radio_cache_as_it_reads_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SimpleNamespace(
            get_config_dir=lambda: directory.name,
            selected_provider_name="Radio: Türkiye",
            ssl_verify=True,
        )

        def manager():
            made = ProviderManager(config)
            self.addCleanup(made._cache_writer.shutdown, wait=True)
            made.providers = [fr.provider_entry("tr")]
            made.current_provider = made.providers[0]
            return made

        first = manager()
        first.current_provider_content = {"itv": parse_stations(STATIONS)}
        first.save_provider()
        first._cache_writer.submit(lambda: None).result(timeout=5)
        self.turkish()
        again = manager()
        loaded = again._load_provider_cache(again.current_provider)
        self.assertEqual(
            [c["title"] for c in loaded["itv"]["categories"]],
            ["Tüm istasyonlar", "Haberler ve sohbet", "Pop", "Rock", "Diğer"],
        )


class _Directory(BaseHTTPRequestHandler):
    """A stand-in for the directory: its station JSON at its path; a HEAD finds nothing, as the
    real one answers it."""

    def do_HEAD(self):
        self.server.requests.append(("HEAD", self.path, self.headers.get("User-Agent", "")))
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.server.requests.append(("GET", self.path, self.headers.get("User-Agent", "")))
        body = self.server.bodies.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LoaderTests(unittest.TestCase):
    """The M3U loader reads the directory's JSON: the same worker, the same list's shape."""

    def setUp(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Directory)
        server.requests, server.bodies = [], {
            "/json/stations/bycountrycodeexact/TR": json.dumps(STATIONS).encode(),
            "/json/stations/bycountrycodeexact/BQ": b"[]",
            "/list.json": b'[{"name": "no stream here"}]',
        }
        self.server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def run_worker(self, worker):
        results, errors = [], []
        worker.finished.connect(results.append)
        worker.error.connect(errors.append)
        worker.run()
        return results, errors

    def test_the_stations_load_as_a_playlist(self):
        url = f"{self.base}/json/stations/bycountrycodeexact/TR?hidebroken=true"
        [playlist], errors = self.run_worker(workers.M3ULoaderWorker(url))
        self.assertEqual(errors, [])
        self.assertEqual(playlist, parse_stations(STATIONS))
        self.assertEqual(self.server.requests[0][2], CLIENT_HEADERS["User-Agent"])  # no directory

    def test_the_directory_is_asked_with_qitvs_name(self):
        url = f"{self.base}/json/stations/bycountrycodeexact/TR?hidebroken=true"
        with mock.patch.object(fr, "is_directory_url", return_value=True):
            [playlist], errors = self.run_worker(workers.M3ULoaderWorker(url))
        self.assertEqual(errors, [])
        self.assertEqual(len(playlist["contents"]), len(STATIONS))
        self.assertEqual(self.server.requests[0][2], f"QiTV v{get_app_version()}")

    def test_an_empty_answer_keeps_the_saved_list(self):
        # As an empty playlist's (2.8.12): no list replaces the saved one with none; a list
        # added just now says so as an empty playlist does.
        url = f"{self.base}/json/stations/bycountrycodeexact/BQ"
        with mock.patch.object(fr, "is_directory_url", return_value=True):
            results, errors = self.run_worker(workers.M3ULoaderWorker(url))
        self.assertEqual(results, [])
        self.assertEqual(errors, [workers.UNEXPECTED_ANSWER_MESSAGE])

    def test_other_json_is_still_no_playlist(self):
        results, errors = self.run_worker(workers.M3ULoaderWorker(f"{self.base}/list.json"))
        self.assertEqual(results, [])
        self.assertEqual(errors, [workers.UNEXPECTED_ANSWER_MESSAGE])

    def test_a_file_of_stations_loads_too(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(STATIONS, f)
        self.addCleanup(os.unlink, f.name)
        [playlist], errors = self.run_worker(workers.M3ULoaderWorker(f.name))
        self.assertEqual(errors, [])
        self.assertEqual(len(playlist["contents"]), len(STATIONS))

    def test_settings_check_asks_the_directory_with_a_get(self):
        # The directory answers a HEAD with 404: the check asks for the list, and reads none
        # of it.
        provider = {"type": "M3UPLAYLIST", "name": "Radio: Türkiye"}
        provider["url"] = f"{self.base}/json/stations/bycountrycodeexact/TR?hidebroken=true"
        with mock.patch.object(fr, "is_directory_url", return_value=True):
            self.assertTrue(provider_check.check(provider, True).ok)
        self.assertEqual(
            [(method, agent) for method, _path, agent in self.server.requests],
            [("GET", f"QiTV v{get_app_version()}")],
        )
        provider["url"] = f"{self.base}/json/stations/bycountrycodeexact/ZZ"
        with mock.patch.object(fr, "is_directory_url", return_value=True):
            self.assertFalse(provider_check.check(provider, True).ok)


if __name__ == "__main__":
    unittest.main()
