"""Jellyfin and Emby workers (2.8): what each asks of the server and what it hands back."""

import unittest

from services import media_server as ms
from services.media_workers import (
    NO_VERSION_TEXT,
    MediaLinkWorker,
    MediaSectionWorker,
    MediaSignInWorker,
)

BASE = "http://server.test:8096"
PROVIDER = {"type": "JELLYFIN", "url": BASE, "token": "T", "user_id": "U", "device_id": "D"}


class FakeClient:
    def __init__(self, libraries=(), items=None, sources=(), fail=None):
        self.base, self.token, self.device_id = BASE, "T", "D"
        self._libraries, self._items, self._sources, self._fail = (
            libraries,
            items or {},
            sources,
            fail,
        )
        self.asked, self.closed = [], False

    def libraries(self):
        if self._fail:
            raise self._fail
        return list(self._libraries)

    def items(self, item_type, parent_id, *, since="", resumable=False, ids=()):
        asked = (item_type, parent_id)
        more = since or ids or resumable
        self.asked.append(asked + (since, resumable, tuple(ids)) if more else asked)
        if ids:
            return [i for i in self.everything.get(parent_id, []) if i["Id"] in ids]
        if resumable:
            return list(self.resumable.get(parent_id, []))
        if since:
            return list(self.changed.get(parent_id, []))
        return list(self._items.get(parent_id, []))

    everything: dict = {}  # a library's every title now (by id asks and the count)
    changed: dict = {}  # library -> the titles saved since
    resumable: dict = {}  # library -> its titles started (the server's resume list)
    server_time = None  # the server's clock at its last answer (Unix seconds), if it said

    def count(self, item_type, parent_id):
        self.asked.append(("count", parent_id))
        return len(self.everything.get(parent_id, []))

    def item_ids(self, item_type, parent_id):
        self.asked.append(("ids", parent_id))
        return [i["Id"] for i in self.everything.get(parent_id, [])]

    def media_sources(self, item_id):
        if self._fail:
            raise self._fail
        return list(self._sources)

    def sign_in(self, username, password):
        if self._fail:
            raise self._fail
        return "T2", "U2"

    def close(self):
        self.closed = True


def outcome(worker):
    got = []
    worker.finished.connect(lambda payload: got.append(("ok", payload)))
    worker.error.connect(lambda message: got.append(("error", message)))
    worker.run()
    return got[0]


def source(sid, width, height, bitrate=5_000_000):
    return {
        "Id": sid,
        "Container": "mkv",
        "Bitrate": bitrate,
        "MediaStreams": [{"Type": "Video", "Width": width, "Height": height}],
    }


class SectionWorkerTests(unittest.TestCase):
    def test_films_come_from_the_libraries_that_hold_films(self):
        client = FakeClient(
            libraries=[
                {"id": "L1", "name": "Movies", "type": "movies"},
                {"id": "L2", "name": "TV", "type": "tvshows"},
                {"id": "L3", "name": "Both", "type": "mixed"},
            ],
            items={"L1": [{"Id": "f1", "Name": "Salt"}], "L3": [{"Id": "f2", "Name": "Tide"}]},
        )
        worker = MediaSectionWorker(PROVIDER, "vod", True, client=lambda p, v: client)
        kind, payload = outcome(worker)
        self.assertEqual(kind, "ok")
        self.assertEqual(client.asked, [("Movie", "L1"), ("Movie", "L3")])
        self.assertEqual([i["name"] for i in payload["contents"]], ["Salt", "Tide"])
        self.assertTrue(client.closed)

    def test_a_first_load_is_whole_and_notes_when_it_looked(self):  # 2.8: refresh
        client = FakeClient(
            libraries=[{"id": "L1", "name": "Movies", "type": "movies"}],
            items={"L1": [{"Id": "f1", "Name": "Salt"}]},
        )
        worker = MediaSectionWorker(
            PROVIDER, "vod", True, client=lambda p, v: client, clock=lambda: 1_800_000_000.0
        )
        _kind, payload = outcome(worker)
        self.assertEqual(payload["server_full"], 1_800_000_000.0)
        self.assertEqual(payload["server_since"], "2027-01-15T07:50:00Z")  # ten minutes back
        self.assertNotIn("delta", payload)

    def test_a_later_load_asks_only_what_changed(self):
        lib = {"id": "L1", "name": "Movies", "type": "movies"}
        client = FakeClient(libraries=[lib])
        client.everything = {"L1": [{"Id": "f1", "Name": "Salt"}, {"Id": "f2", "Name": "Tide"}]}
        client.changed = {"L1": [{"Id": "f2", "Name": "Tide", "UserData": {}}]}
        saved = {
            "since": "2027-01-15T07:50:00Z",
            "full": 1_800_000_000.0,
            "members": {"L1": ["f1", "f2"]},
        }
        worker = MediaSectionWorker(
            PROVIDER,
            "vod",
            True,
            client=lambda p, v: client,
            saved=saved,
            clock=lambda: 1_800_003_600.0,
        )
        _kind, delta = outcome(worker)
        self.assertTrue(delta["delta"])
        self.assertEqual(list(delta["changed"]), ["f2"])
        self.assertEqual(delta["order"], {"L1": ["f1", "f2"]})  # nothing came or went
        self.assertNotIn(("ids", "L1"), client.asked)  # the count told: no list of ids
        self.assertEqual(delta["server_since"], "2027-01-15T08:50:00Z")

    def test_a_title_added_or_gone_reads_the_library_s_ids(self):
        lib = {"id": "L1", "name": "Movies", "type": "movies"}
        f0, f1, f2 = ({"Id": i, "Name": i} for i in ("f0", "f1", "f2"))
        saved = {"since": "x", "full": 1_800_000_000.0, "members": {"L1": ["f1", "f2"]}}
        cases = (
            ("one went", [f1], {}, ["f1"], []),
            (
                "one came, saved since",
                [f0, f1, f2],
                {"L1": [f0]},
                ["f0", "f1", "f2"],
                ["f0"],
            ),
            ("one came, the count tells", [f0, f1, f2], {}, ["f0", "f1", "f2"], ["f0"]),
        )
        for name, now, changed, order, came in cases:
            with self.subTest(name):
                client = FakeClient(libraries=[lib])
                client.everything, client.changed = {"L1": now}, changed
                worker = MediaSectionWorker(
                    PROVIDER,
                    "vod",
                    True,
                    client=lambda p, v: client,
                    saved=saved,
                    clock=lambda: 1_800_003_600.0,
                )
                _kind, delta = outcome(worker)
                self.assertEqual(delta["order"], {"L1": order})
                self.assertEqual(list(delta["changed"]), came)

    def later(self, client, saved, clock=1_800_003_600.0):
        worker = MediaSectionWorker(
            PROVIDER, "vod", True, client=lambda p, v: client, saved=saved, clock=lambda: clock
        )
        return outcome(worker)[1]

    def test_positions_come_from_the_servers_resume_list(self):  # review I1
        # Played in the server's own app: the title itself was not saved since, its user data
        # was; the server's resume list says where each title stopped, the others started none.
        lib = {"id": "L1", "name": "Movies", "type": "movies"}
        client = FakeClient(libraries=[lib])
        client.everything = {"L1": [{"Id": "f1"}, {"Id": "f2"}]}
        ticks = 600_000 * ms.TICKS_PER_MS
        client.resumable = {"L1": [{"Id": "f2", "UserData": {"PlaybackPositionTicks": ticks}}]}
        saved = {"since": "x", "full": 1_800_000_000.0, "members": {"L1": ["f1", "f2"]}}
        delta = self.later(client, saved)
        self.assertEqual(delta["positions"], {"f2": 600_000})
        self.assertIn(("Movie", "L1", "", True, ()), client.asked)

    def test_a_title_in_two_libraries_keeps_the_first(self):  # review M1
        libs = [
            {"id": "LA", "name": "A", "type": "movies"},
            {"id": "LB", "name": "B", "type": "movies"},
        ]
        client = FakeClient(libraries=libs)
        both = {"Id": "f1", "Name": "Salt"}
        client.everything = {"LA": [both], "LB": [both]}
        client.changed = {"LA": [both], "LB": [both]}
        saved = {"since": "x", "full": 1_800_000_000.0, "members": {"LA": ["f1"], "LB": ["f1"]}}
        delta = self.later(client, saved)
        self.assertEqual(delta["changed"]["f1"]["tv_genre_id"], "LA")  # as a whole load has it

    def test_a_look_is_dated_by_the_servers_clock(self):
        # A server whose clock is behind this computer's: the next look asks from its time.
        lib = {"id": "L1", "name": "Movies", "type": "movies"}
        client = FakeClient(libraries=[lib], items={"L1": [{"Id": "f1"}]})
        client.server_time = 1_800_000_000.0 - 3600  # an hour behind
        worker = MediaSectionWorker(
            PROVIDER, "vod", True, client=lambda p, v: client, clock=lambda: 1_800_000_000.0
        )
        payload = outcome(worker)[1]
        self.assertEqual(payload["server_since"], "2027-01-15T06:50:00Z")
        self.assertEqual(payload["server_libraries"], ["L1"])

    def test_after_a_week_or_with_a_new_library_it_loads_whole(self):
        libs = [{"id": "L1", "name": "Movies", "type": "movies"}]
        client = FakeClient(libraries=libs, items={"L1": [{"Id": "f1", "Name": "Salt"}]})
        week_old = {"since": "x", "full": 1_800_000_000.0 - 8 * 86400, "members": {"L1": []}}
        other = {"since": "x", "full": 1_800_000_000.0, "members": {"L9": []}}
        for saved in (week_old, other):
            worker = MediaSectionWorker(
                PROVIDER,
                "vod",
                True,
                client=lambda p, v: client,
                saved=saved,
                clock=lambda: 1_800_000_000.0,
            )
            _kind, payload = outcome(worker)
            self.assertNotIn("delta", payload)
            self.assertEqual([i["name"] for i in payload["contents"]], ["Salt"])

    def test_a_refused_token_asks_to_sign_in_again(self):
        client = FakeClient(fail=ms.SignInNeeded("401"))
        kind, message = outcome(
            MediaSectionWorker(PROVIDER, "series", True, client=lambda p, v: client)
        )
        self.assertEqual((kind, message), ("error", ms.SIGN_IN_AGAIN))


class LinkWorkerTests(unittest.TestCase):
    def link(self, sources, **choices):
        client = FakeClient(sources=sources)
        settings = {"screen_4k": False, "four_k": ms.FOUR_K_SCREEN}
        settings.update(choices)
        return outcome(
            MediaLinkWorker(PROVIDER, "f1", True, client=lambda p, v: client, **settings)
        )

    def test_the_file_plays_as_it_is(self):
        kind, payload = self.link([source("hd", 1920, 1080)])
        self.assertEqual(kind, "ok")
        self.assertIn("/Videos/f1/stream.mkv?static=true&MediaSourceId=hd", payload["link"])

    def test_without_a_choice_nothing_is_converted(self):  # 2.8: the quality menu converts
        kind, payload = self.link([source("hd", 1920, 1080, 60_000_000)])
        self.assertIn("/stream.mkv?static=true", payload["link"])
        self.assertEqual(payload["quality"], ms.Quality(source="hd"))

    def test_the_versions_come_with_the_link(self):
        sources = [source("uhd", 3840, 2160, 25_000_000), source("hd", 1920, 1080, 8_000_000)]
        kind, payload = self.link(sources)
        self.assertEqual([v["id"] for v in payload["versions"]], ["uhd", "hd"])
        self.assertEqual(payload["quality"], ms.Quality(source="hd"))  # the settings' pick

    def test_a_version_chosen_plays_as_its_file_is(self):
        sources = [source("uhd", 3840, 2160, 25_000_000), source("hd", 1920, 1080, 8_000_000)]
        kind, payload = self.link(sources, quality=ms.Quality(source="uhd"))
        self.assertIn("/stream.mkv?static=true&MediaSourceId=uhd", payload["link"])
        self.assertEqual(payload["quality"], ms.Quality(source="uhd"))

    def test_a_version_gone_is_the_settings_pick(self):
        kind, payload = self.link([source("hd", 1920, 1080)], quality=ms.Quality(source="old"))
        self.assertIn("MediaSourceId=hd", payload["link"])

    def test_a_conversion_chosen_is_the_servers(self):
        chosen = ms.Quality(height=720, bitrate=4_000_000)
        kind, payload = self.link([source("hd", 1920, 1080, 20_000_000)], quality=chosen)
        self.assertIn("/Videos/f1/master.m3u8?", payload["link"])
        self.assertIn("MaxHeight=720", payload["link"])
        self.assertIn("MediaSourceId=hd", payload["link"])
        self.assertEqual(payload["quality"], ms.Quality("hd", 720, 4_000_000))  # of "hd"

    def test_a_conversion_a_file_already_fits_plays_it_as_it_is(self):  # review M9
        # A conversion carried to a server's next titles: a 480p file within 720p and 4 Mbit/s
        # gains nothing from it (the server's processor would work for nothing).
        chosen = ms.Quality(height=720, bitrate=4_000_000)
        kind, payload = self.link([source("sd", 854, 480, 1_500_000)], quality=chosen)
        self.assertIn("/stream.mkv?static=true&MediaSourceId=sd", payload["link"])
        self.assertEqual(payload["quality"], ms.Quality(source="sd"))
        # Larger than the rate (or of a size not known): converted.
        for big in (source("sd", 854, 480, 9_000_000), source("x", 0, 0, 1_000_000)):
            kind, payload = self.link([big], quality=chosen)
            self.assertIn("/master.m3u8?", payload["link"])

    def test_only_4k_plays_as_it_is_below_a_4k_screen(self):
        kind, payload = self.link([source("uhd", 3840, 2160)])
        self.assertEqual(kind, "ok")
        self.assertIn("/stream.mkv?static=true&MediaSourceId=uhd", payload["link"])

    def test_only_4k_with_never_says_how_to_play_it(self):
        refused = self.link([source("uhd", 3840, 2160)], four_k=ms.FOUR_K_NEVER)
        self.assertEqual(refused, ("error", ms.ONLY_4K_TEXT))

    def test_no_file_says_so(self):
        self.assertEqual(self.link([]), ("error", NO_VERSION_TEXT))


class SignInWorkerTests(unittest.TestCase):
    def test_a_sign_in_gives_the_token_user_and_device(self):
        worker = MediaSignInWorker(
            BASE, "EMBY", "me", "pw", True, device_id="D", client=FakeClient()
        )
        self.assertEqual(
            outcome(worker), ("ok", {"token": "T2", "user_id": "U2", "device_id": "D"})
        )

    def test_a_refused_password_says_so(self):
        worker = MediaSignInWorker(
            BASE, "EMBY", "me", "no", True, client=FakeClient(fail=ms.SignInNeeded("401"))
        )
        self.assertEqual(outcome(worker), ("error", "The user name or the password is not right."))


if __name__ == "__main__":
    unittest.main()
