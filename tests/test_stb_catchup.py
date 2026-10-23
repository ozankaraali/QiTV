"""Catch-up on STB portals: which channels the portal marks archived, the guide's programme ids
and recording marks, the portal's links to a programme's recording, and the link lanes that
ask for them. A fake portal on 127.0.0.1 (tests/fixtures/local_stb.py) answers; nothing here
reaches a network, and everything is fictional.
"""

from datetime import datetime, timedelta, timezone
import os
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from job_fixtures import ImmediateRunner
from mixed_fixtures import mixed_config, shut_hub
import orjson
from timer_fixtures import wait_until

from services.catalog import EMPTY_LINK_MESSAGE, NO_STREAM_MESSAGE, CatalogService
from services.catalogs import Catalogs
from services.catchup import (
    ARCHIVE_KEY,
    STB,
    TYPE_KEY,
    TYPES,
    Archive,
    archive_of,
    archive_urls,
    mark_stb_archives,
    programme_in_archive,
    recording_at,
    stb_archive_fields,
)
from services.epg_manager import EpgManager
from services.epg_view import (
    Programme,
    Recording,
    normalize,
    parse_programme,
    programmes_for_channel,
    shifted,
    timeline,
)
from services.multikeydict import MultiKeyDict
from services.provider_api import stb_archive_link_url
from services.provider_manager import ProviderManager, StbHandshake
from services.workers import LinkCreatorWorker, STBCategoriesWorker, stb_archive_link
from tests.fixtures.local_stb import ARCHIVED_ID, MAC, PLAIN_ID, StbFixture, channels
from ui.link_lanes import LinkLanes, Target

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 18, 0, tzinfo=UTC)
HALF = timedelta(minutes=30)


def run(worker):
    """Run a worker on this thread; its one result: ("finished", payload) or ("error", text)."""
    results = []
    worker.finished.connect(lambda payload: results.append(("finished", payload)))
    worker.error.connect(lambda message: results.append(("error", message)))
    worker.run()
    [result] = results
    return result


class ArchiveFieldsTests(unittest.TestCase):
    def test_a_channel_the_portal_marks_archived_has_an_archive(self):
        for mark in ({"tv_archive": "1"}, {"tv_archive": 1}, {"enable_tv_archive": "1"}):
            with self.subTest(mark=mark):
                fields = stb_archive_fields(dict(mark, tv_archive_duration="3"))
                self.assertEqual(fields, {ARCHIVE_KEY: 3, TYPE_KEY: STB})
        self.assertEqual(stb_archive_fields({"archive": 1})[TYPE_KEY], STB)

    def test_a_channel_not_marked_has_none(self):
        for channel in (
            {},
            {"tv_archive": "0", "tv_archive_duration": "7"},
            {"tv_archive": 0},
            {"allow_pvr": "1"},  # the portal's own recorder, not an archive
            {"tv_archive_duration": "168"},
        ):
            with self.subTest(channel=channel):
                self.assertEqual(stb_archive_fields(channel), {})

    def test_its_depth_is_hours_in_whole_days_or_above_31_else_days(self):
        depths = {
            "7": 7,
            "30": 30,
            "31": 31,
            "24": 1,  # a day in hours, not 24 days
            "24.0": 1,
            "168": 7,
            "48": 2,
            "40": 2,
            "0": 1,
            "": 1,
            "x": 1,
            "9999": 31,
        }
        for given, days in depths.items():
            with self.subTest(given=given):
                fields = stb_archive_fields({"tv_archive": "1", "tv_archive_duration": given})
                self.assertEqual(fields[ARCHIVE_KEY], days)
        self.assertEqual(stb_archive_fields({"tv_archive": "1"})[ARCHIVE_KEY], 1)

    def test_its_archive_is_the_portals_whose_links_the_portal_makes(self):
        item = dict(channels()[0], **stb_archive_fields(channels()[0]))
        archive = archive_of(item)
        self.assertEqual(archive, Archive(7, STB))
        self.assertEqual(archive_urls(archive, item["cmd"], T0, 600.0, T0 + HALF), [])

    def test_older_builds_read_no_archive_from_the_cache(self):
        channel = channels()[0]
        mark_stb_archives([channel])
        cached = orjson.loads(orjson.dumps([channel]))[0]
        self.assertEqual(cached[ARCHIVE_KEY], 7)
        # Builds before STB catch-up give a channel with a type they do not know no archive.
        self.assertNotIn(cached[TYPE_KEY], TYPES)
        self.assertEqual(cached["tv_archive"], "1")  # the portal's own fields stay as they came

    def test_marking_follows_the_portal_and_leaves_other_archives_alone(self):
        marked, plain = channels()
        playlist = {"name": "Lighthouse", ARCHIVE_KEY: 2, TYPE_KEY: "shift"}
        mark_stb_archives({"1": [marked, plain], "2": [playlist]})
        self.assertEqual((marked[ARCHIVE_KEY], marked[TYPE_KEY]), (7, STB))
        self.assertNotIn(ARCHIVE_KEY, plain)
        self.assertEqual((playlist[ARCHIVE_KEY], playlist[TYPE_KEY]), (2, "shift"))
        marked["tv_archive"] = "0"  # the portal no longer marks it
        mark_stb_archives([marked])
        self.assertNotIn(ARCHIVE_KEY, marked)
        self.assertNotIn(TYPE_KEY, marked)
        self.assertIsNone(archive_of(marked))


def entry(ident, start, stop, mark=None, name="Show"):
    raw = {
        "id": ident,
        "name": name,
        "time": start.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "time_to": stop.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "start_timestamp": int(start.timestamp()),
        "stop_timestamp": int(stop.timestamp()),
    }
    if mark is not None:
        raw["mark_archive"] = mark
    return raw


class GuideRecordingTests(unittest.TestCase):
    def test_a_portal_guides_programme_keeps_its_id_and_mark(self):
        programme = parse_programme(entry("5001", T0, T0 + HALF, mark=1))
        self.assertEqual(programme.recording, Recording("5001", T0, T0 + HALF, True))
        self.assertEqual(programme.start, T0)
        self.assertFalse(parse_programme(entry(5002, T0, T0 + HALF, mark="0")).recording.marked)
        self.assertIsNone(parse_programme(entry("5003", T0, T0 + HALF)).recording.marked)
        self.assertIsNone(parse_programme(entry("", T0, T0 + HALF)).recording)
        self.assertIsNone(parse_programme(entry("x1", T0, T0 + HALF)).recording)

    def test_an_xmltv_programme_has_no_recording(self):
        raw = {"@start": "20261015180000 +0000", "@stop": "20261015183000 +0000", "title": "News"}
        self.assertIsNone(parse_programme(raw).recording)

    def test_the_guide_offset_moves_the_recording_too(self):
        moved = shifted(parse_programme(entry("5001", T0, T0 + HALF, mark=1)), timedelta(hours=1))
        self.assertEqual(moved.recording.start, T0 + timedelta(hours=1))
        self.assertEqual(moved.recording.stop, moved.stop)

    def test_a_programme_shown_in_pieces_is_recorded_whole(self):
        film = entry("7", T0, T0 + 4 * HALF, mark=1, name="Film")
        news = entry("8", T0 + HALF, T0 + 2 * HALF, mark=1, name="News")
        pieces = normalize([film, news])
        self.assertEqual([p.title for p in pieces], ["Film", "News", "Film"])
        self.assertEqual(pieces[2].start, T0 + 2 * HALF)
        self.assertEqual(pieces[2].recording.start, T0)  # its recording starts at its start

    def test_a_moment_finds_the_recording_on_then(self):
        programmes = timeline(
            parse_programme(raw)
            for raw in (
                entry("1", T0, T0 + HALF, mark=1),
                entry("2", T0 + HALF, T0 + 2 * HALF, mark=0),
                entry("3", T0 + 3 * HALF, T0 + 4 * HALF),
            )
        )
        found = recording_at(programmes, T0 + timedelta(minutes=10))
        self.assertEqual((found.id, found.start), ("1", T0))
        self.assertIsNone(recording_at(programmes, T0 + timedelta(minutes=40)))  # not recorded
        self.assertIsNone(recording_at(programmes, T0 + timedelta(minutes=70)))  # nothing on
        self.assertEqual(recording_at(programmes, T0 + timedelta(minutes=100)).id, "3")

    def test_an_stb_channel_offers_only_programmes_its_guide_can_name(self):
        stb = {ARCHIVE_KEY: 7, TYPE_KEY: STB}
        listed = parse_programme(entry("1", T0, T0 + HALF, mark=1))
        unsaid = parse_programme(entry("2", T0, T0 + HALF))
        not_recorded = parse_programme(entry("3", T0, T0 + HALF, mark=0))
        xmltv = Programme("From a file", T0, T0 + HALF)
        self.assertTrue(programme_in_archive(stb, listed))
        self.assertTrue(programme_in_archive(stb, unsaid))
        self.assertFalse(programme_in_archive(stb, not_recorded))
        self.assertFalse(programme_in_archive(stb, xmltv))
        self.assertTrue(programme_in_archive({ARCHIVE_KEY: 3}, xmltv))  # an Xtream panel's


class LinkTests(unittest.TestCase):
    def test_the_archive_link_request_names_the_programme_as_a_set_top_box_does(self):
        url = stb_archive_link_url("http://portal.test", "5012")
        parts = urlsplit(url)
        self.assertEqual(parts.path, "/server/load.php")
        self.assertIn("cmd=auto%20/media/5012.mpg", parts.query)
        self.assertEqual(
            parse_qs(parts.query, keep_blank_values=True),
            {
                "type": ["tv_archive"],
                "action": ["create_link"],
                "cmd": ["auto /media/5012.mpg"],
                "series": [""],
                "forced_storage": [""],
                "disable_ad": ["0"],
                "download": ["0"],
                "JsHttpRequest": ["1-xml"],
            },
        )

    def test_the_link_is_the_last_word_of_an_address(self):
        cases = {
            "ffmpeg http://s.test/a/1.ts?t=1": "http://s.test/a/1.ts?t=1",
            "http://s.test/a/1.ts": "http://s.test/a/1.ts",
            "": "",  # no recording
        }
        for cmd, link in cases.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(stb_archive_link({"js": {"cmd": cmd}}), link)
        self.assertEqual(stb_archive_link({"js": {"cmd": "", "error": "nothing_to_play"}}), "")

    def test_any_other_answer_is_the_portals_error_not_a_missing_recording(self):
        for answer in (
            {"js": {"cmd": "", "error": "limit"}},  # its limit of streams
            {"js": {"cmd": "", "error": "link_fault"}},
            {"js": {"cmd": "auto /media/5012.mpg"}},  # what was asked, unmade
            {"js": []},  # a portal without an archive to ask
            {"js": False},
            None,
        ):
            with self.subTest(answer=answer):
                with self.assertRaises(ValueError):
                    stb_archive_link(answer)


class PortalCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.portal = StbFixture()
        self.addCleanup(self.portal.close)
        directory = tempfile.TemporaryDirectory(prefix="qitv-stb-catchup-")
        self.addCleanup(directory.cleanup)
        self.dir = directory.name

    def signed_in_headers(self):
        """A session's headers after a handshake with the portal (its token)."""
        handshake = StbHandshake()
        self.assertTrue(handshake.run(self.portal.url, MAC))
        return handshake.headers


class PortalLoadTests(PortalCase):
    def test_the_live_channels_carry_the_archive_the_portal_marks(self):
        worker = STBCategoriesWorker(self.portal.url, self.signed_in_headers(), "itv")
        kind, payload = run(worker)
        self.assertEqual(kind, "finished")
        by_id = {c["id"]: c for c in payload["contents"]}
        self.assertEqual(archive_of(by_id[ARCHIVED_ID]), Archive(7, STB))
        self.assertIsNone(archive_of(by_id[PLAIN_ID]))
        self.assertNotIn(ARCHIVE_KEY, by_id[PLAIN_ID])

    def test_a_portal_cached_before_gets_its_archives_as_the_cache_loads(self):
        _config, manager = mixed_config(
            self.dir,
            [self.portal.provider()],
            {
                self.portal.provider()["name"]: {
                    "categories": [{"id": "*", "title": "All"}],
                    "contents": channels(),  # as an older build saved them
                    "sorted_channels": {"1": [0, 1]},
                }
            },
        )
        self.addCleanup(manager._cache_writer.shutdown, wait=True)
        loaded = manager._load_provider_cache(self.portal.provider())
        contents = loaded["itv"]["contents"]
        self.assertEqual(archive_of(contents[0]), Archive(7, STB))
        self.assertIsNone(archive_of(contents[1]))

    def test_the_portals_guide_keeps_programme_ids_and_marks_through_its_file(self):
        config = SimpleNamespace(
            channel_epg=True,
            epg_source="STB",
            epg_expiration=3600,
            epg_stb_period_hours=24,
            epg_list_window_hours=0,
            prefer_https=False,
            ssl_verify=True,
            xmltv_channel_map=MultiKeyDict(),
            get_config_dir=lambda: self.dir,
        )
        providers = SimpleNamespace(
            current_provider=self.portal.provider(), headers=self.signed_in_headers()
        )
        EpgManager(config, providers).set_current_epg()
        restarted = EpgManager(config, providers)  # the guide comes from its file now
        restarted.set_current_epg()
        self.assertEqual(len(self.portal.asked("itv", "get_epg_info")), 1)
        past = datetime.fromtimestamp(self.portal.now - 3 * 3600, UTC)
        found = programmes_for_channel(restarted, {"id": ARCHIVED_ID}, past, limit=40)
        listed = self.portal.programme(ARCHIVED_ID, past.timestamp())
        self.assertEqual(found[0].recording.id, listed["id"])
        self.assertTrue(found[0].recording.marked)
        on_now = [p for p in found if p.is_on(datetime.now(UTC))]
        self.assertFalse(on_now[0].recording.marked)  # not over: not recorded yet
        plain = programmes_for_channel(restarted, {"id": PLAIN_ID}, past, limit=2)
        self.assertIsNone(plain[0].recording.marked)  # the portal says nothing of it


class PortalLinkTests(PortalCase):
    def worker(self, programme, headers):
        return LinkCreatorWorker(
            base_url=self.portal.url,
            headers=headers,
            content_type="tv_archive",
            cmd="",
            programme=programme,
        )

    def test_a_recorded_programmes_link_comes_from_the_signed_in_portal(self):
        headers = self.signed_in_headers()
        programme = self.portal.programme(ARCHIVED_ID, self.portal.now - 3600)["id"]
        self.assertEqual(
            run(self.worker(programme, headers)),
            ("finished", {"link": self.portal.archive_link(programme)}),
        )
        [(query, bearer)] = self.portal.asked("tv_archive", "create_link")
        self.assertEqual(query["cmd"], f"auto /media/{programme}.mpg")
        self.assertEqual(bearer, f"Bearer {self.portal.token}")

    def test_a_programme_not_recorded_gets_no_link(self):
        headers = self.signed_in_headers()
        programme = self.portal.programme(ARCHIVED_ID, self.portal.now + 3600)["id"]
        self.assertEqual(run(self.worker(programme, headers)), ("finished", {"link": ""}))

    def test_a_stale_session_is_refused(self):
        headers = self.signed_in_headers()
        self.portal.expire()
        programme = self.portal.programme(ARCHIVED_ID, self.portal.now - 3600)["id"]
        self.assertEqual(run(self.worker(programme, headers))[0], "error")


class CatalogLinkTests(PortalCase):
    """The catalogs ask the portal of the item's provider, signed in, and sign in again."""

    def setUp(self):
        super().setUp()
        provider = self.portal.provider()
        self.name = provider["name"]
        self.config, self.registry = mixed_config(self.dir, [provider], {})
        self.addCleanup(self.registry._cache_writer.shutdown, wait=True)
        self.catalogs = Catalogs(self.config, self.registry, runner=ImmediateRunner())
        self.addCleanup(shut_hub, self.catalogs)
        self.ready, self.failed = [], []
        self.catalogs.linkReady.connect(lambda rid, url: self.ready.append((rid, url)))
        self.catalogs.linkFailed.connect(lambda rid, message: self.failed.append((rid, message)))
        self.item = dict(channels()[0], _provider=self.name)
        self.recorded = self.portal.programme(ARCHIVED_ID, self.portal.now - 3600)["id"]

    def test_a_link_asked_before_the_sign_in_waits_for_it(self):
        request = self.catalogs.request_archive_link(self.item, self.recorded)
        self.assertTrue(wait_until(lambda: self.ready or self.failed))
        self.assertEqual(self.ready, [(request, self.portal.archive_link(self.recorded))])
        self.assertEqual(self.portal.handshakes, 1)

    def test_a_programme_not_recorded_fails_plainly(self):
        self.catalogs.ensure(self.name)
        self.assertTrue(wait_until(lambda: self.catalogs.is_ready(self.name)))
        later = self.portal.programme(ARCHIVED_ID, self.portal.now + 3600)["id"]
        request = self.catalogs.request_archive_link(self.item, later)
        self.assertTrue(wait_until(lambda: self.failed))
        self.assertEqual(self.failed, [(request, EMPTY_LINK_MESSAGE)])

    def test_the_portals_other_errors_are_no_missing_recording(self):
        self.catalogs.ensure(self.name)
        self.assertTrue(wait_until(lambda: self.catalogs.is_ready(self.name)))
        for error in ("limit", ""):  # "": a portal without an archive to ask: {"js": []}
            with self.subTest(error=error):
                self.portal.errors[self.recorded] = error
                self.failed.clear()
                request = self.catalogs.request_archive_link(self.item, self.recorded)
                self.assertTrue(wait_until(lambda: self.failed))
                [(failed, message)] = self.failed
                self.assertEqual(failed, request)
                self.assertNotEqual(message, EMPTY_LINK_MESSAGE)

    def test_an_expired_session_signs_in_again_and_the_link_comes(self):
        self.catalogs.ensure(self.name)
        self.assertTrue(wait_until(lambda: self.catalogs.is_ready(self.name)))
        self.portal.expire()
        first = self.catalogs.request_archive_link(self.item, self.recorded)
        self.assertTrue(wait_until(lambda: self.failed))
        self.assertEqual(self.failed[0][0], first)
        self.catalogs.sign_in_again(self.name)  # what catch-up asks after a failed link
        again = self.catalogs.request_archive_link(self.item, self.recorded)
        self.assertTrue(wait_until(lambda: self.ready))
        self.assertEqual(self.ready, [(again, self.portal.archive_link(self.recorded))])
        self.assertEqual(self.portal.handshakes, 2)

    def test_another_kind_of_provider_makes_no_archive_link(self):
        catalog = CatalogService(
            self.config,
            ProviderManager(self.config),
            None,
            runner=ImmediateRunner(),
        )
        catalog.provider_manager.current_provider = {"name": "Playlist", "type": "M3UPLAYLIST"}
        failed = []
        catalog.linkFailed.connect(lambda rid, message: failed.append(message))
        catalog.request_archive_link({"name": "One"}, "5000")
        self.assertTrue(wait_until(lambda: failed))
        self.assertEqual(failed, [NO_STREAM_MESSAGE])


class FakeCatalog(QObject):
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

    def __init__(self):
        super().__init__()
        self.requests = []  # (request id, item name, what: a section or ("archive", id))

    def request_link(self, item, *, section, is_episode=False):
        self.requests.append((len(self.requests) + 1, item["name"], section))
        return len(self.requests)

    def request_archive_link(self, item, programme):
        self.requests.append((len(self.requests) + 1, item["name"], ("archive", programme)))
        return len(self.requests)


def target(name):
    return Target({"name": name, "_provider": name}, "live", name)


class ArchiveLaneTests(unittest.TestCase):
    """The side lane asks for a recording's link one request at a time, as it asks others."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.catalog = FakeCatalog()
        self.lanes = LinkLanes(self.catalog)
        self.links, self.failures, self.side_failed = [], [], []
        self.lanes.sideFailed.connect(lambda t, message: self.side_failed.append(t.title))

    def ask(self, name="Portal", programme="5000"):
        self.lanes.request_archive(target(name), programme, self.links.append, self.failures.append)

    def test_it_waits_for_the_side_request_before_it_and_hands_over_the_link(self):
        copied = []
        self.lanes.request_side(target("Other"), copied.append, play=False)
        self.ask()
        self.assertEqual(len(self.catalog.requests), 1)  # one at a time
        self.catalog.linkReady.emit(1, "http://other")
        self.assertEqual(self.catalog.requests[1], (2, "Portal", ("archive", "5000")))
        self.catalog.linkReady.emit(2, "http://archive")
        self.assertEqual((copied, self.links), (["http://other"], ["http://archive"]))

    def test_its_failure_goes_to_its_asker_not_the_shell(self):
        self.ask()
        self.catalog.linkFailed.emit(1, EMPTY_LINK_MESSAGE)
        self.assertEqual((self.failures, self.side_failed), ([EMPTY_LINK_MESSAGE], []))

    def test_a_newer_choice_does_not_drop_it(self):
        self.ask()
        self.lanes.supersede()  # a zap: the asker drops a link that comes too late itself
        self.catalog.linkReady.emit(1, "http://archive")
        self.assertEqual(self.links, ["http://archive"])

    def test_its_providers_failed_setup_fails_it_and_a_setup_asks_it_again(self):
        self.ask()
        self.ask(programme="5001")
        _dropped, lost = self.lanes.provider_failed("Portal", lambda item: item["_provider"])
        self.assertFalse(lost)  # the asker says it, not the shell
        self.assertEqual(self.failures, ["", ""])
        self.ask(programme="5002")
        self.assertEqual(self.lanes.provider_ready("Portal", lambda item: item["_provider"]), None)
        asked = [what for _id, _name, what in self.catalog.requests]
        # 5001 was never asked; 5002, asked before the setup, is asked again after it.
        self.assertEqual(asked, [("archive", "5000"), ("archive", "5002"), ("archive", "5002")])


if __name__ == "__main__":
    unittest.main()
