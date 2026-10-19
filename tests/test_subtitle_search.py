"""Search OpenSubtitles… (2.2): the Subtitles panel's row opens the search page; without a key
it asks for one; a subtitle chosen is downloaded, shown and kept for the title; the page is
worked by the keys and the mouse. HTTP is mocked: no key, no network."""

import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QWheelEvent
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from opensubtitles_fixtures import (
    API,
    LINK,
    FakeHttp,
    FakeResponse,
    entry,
    login_answer,
    page,
    quota_answer,
    serving,
)
import requests
from test_track_panel import FakePlayer
from test_tv_controller import ControllerTestCase

from services import _build_keys
from services.jobs import ImmediateRunner
from services.next_episode import play_context
from services.opensubtitles import CONSUMERS_URL, SubtitleFile
from services.sound_sync import SoundSync
from services.subtitle_picks import Pick, SubtitlePicks, pick_key
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.subtitle_search import (
    KEY_EMPTY_TEXT,
    KEY_PAGE,
    RESULTS_PAGE,
    LanguageRow,
    SubtitleSearchPage,
    masked,
)
from ui.overlays.track_panel import TrackPanel
from ui.overlays.transport import Transport
from ui.subtitle_search import OnlineSetup, OnlineSubtitles
from ui.track_controls import REMEMBERED, SEARCH_ONLINE, SEARCH_ONLINE_TEXT, TrackControls

KEY = "fictional-api-key-0001"
FILM = Target(
    {"id": "m1", "name": "TR: Harbour Lights 4K", "year": "2019", "_provider": "Home"},
    "movies",
    "TR: Harbour Lights 4K",
)
EPISODE = Target(
    {"id": "e5", "name": "The Bridge", "episode_num": 5, "_provider": "Home"},
    "series",
    "The Bridge",
    subtitle="Salt Road",
    context={"series": {"id": "s1", "name": "Salt Road"}, "season": {"id": "2", "number": 2}},
)
CHANNEL = Target({"id": 1, "name": "Harbour News", "cmd": "http://tv/1"}, "live", "Harbour News")
FOUND = (
    entry(1, "en", "Harbour.Lights.2019.1080p", downloads=500),
    entry(2, "en", "Harbour.Lights.2019.HI", downloads=900, hearing_impaired=True),
    entry(3, "tr", "Harbour.Lights.2019.TR", downloads=50),
    entry(4, "de", "Harbour.Lights.2019.DE", downloads=70, trusted=True),
)


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.host.show()
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)

    def click(self, widget, point):
        QTest.mouseClick(
            widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point.toPoint()
        )


class PicksTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.folder = os.path.join(self.dir, "subtitles")
        self.path = os.path.join(self.dir, "subtitle_picks.json")

    def test_a_pick_is_kept_in_a_versioned_file_and_read_back(self):
        picks = SubtitlePicks(self.path, self.folder)
        key = pick_key("Home", FILM.item, episode=False)
        file = os.path.join(self.folder, "77", "Harbour.srt")
        picks.set(key, Pick(77, "Harbour.srt", file, "en"))
        picks.flush()
        again = SubtitlePicks(self.path, self.folder)
        self.assertEqual(again.get(key), Pick(77, "Harbour.srt", file, "en"))
        with open(self.path, "rb") as f:
            text = f.read().decode()
        self.assertIn('"version":1', text.replace(" ", ""))
        self.assertNotIn("Harbour Lights", text)  # titles are hashed

    def test_a_bad_file_or_a_path_outside_the_folder_counts_as_nothing(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertIsNone(SubtitlePicks(self.path, self.folder).get("x"))
        with open(self.path, "w") as f:
            f.write(
                '{"version": 1, "titles": {"k": {"file_id": 1, "name": "a.srt",'
                ' "path": "../../etc/a.srt", "set": 1}}}'
            )
        self.assertIsNone(SubtitlePicks(self.path, self.folder).get("k"))
        picks = SubtitlePicks(None, self.folder)
        picks.set("k", Pick(1, "a.srt", "/elsewhere/a.srt"))
        self.assertIsNone(picks.get("k"))

    def test_keys_tell_titles_episodes_and_providers_apart(self):
        film = pick_key("Home", FILM.item, episode=False)
        self.assertNotEqual(film, pick_key("Backup", FILM.item, episode=False))
        episode = pick_key("Home", EPISODE.item, episode=True, context=EPISODE.context)
        other = dict(EPISODE.item, episode_num=6)
        self.assertNotEqual(episode, pick_key("Home", other, episode=True, context=EPISODE.context))
        self.assertEqual(pick_key("Home", {}, episode=False), "")

    def test_the_oldest_go_beyond_the_cap(self):
        clock = iter(range(100))
        picks = SubtitlePicks(None, self.folder, max_entries=2, now=lambda: next(clock))
        for key in "abc":
            picks.set(key, Pick(1, "a.srt", os.path.join(self.folder, "1", "a.srt")))
        self.assertIsNone(picks.get("a"))
        self.assertIsNotNone(picks.get("c"))


def results_page(host) -> SubtitleSearchPage:
    page_ = SubtitleSearchPage(host)
    page_.show_search("Harbour Lights (2019)")
    english = [SubtitleFile(i, "en", f"English {i}", downloads=10 - i) for i in range(1, 13)]
    files = {"en": english, "tr": [SubtitleFile(20, "tr", "Turkish")]}
    rows = [LanguageRow("en", "English", 12), LanguageRow("tr", "Turkish", 1)]
    page_.set_results(rows, files, "13 subtitles in 2 languages")
    return page_


class KeyPageTests(Case):
    def setUp(self):
        super().setUp()
        self.page = SubtitleSearchPage(self.host)
        self.page.show_key_page()

    def test_typing_and_pasting_fill_the_key_shown_masked(self):
        page_ = self.page
        self.assertEqual((page_.page(), page_.focus()), (KEY_PAGE, "field"))
        for ch in "ab cd":
            page_.handle(Action.NAV_TEXT, ch)
        page_.handle(Action.NAV_BACKSPACE)
        self.assertEqual(page_.key_text(), "abc")
        QGuiApplication.clipboard().setText("  key-from-the-site-1234 \nsecond line")
        page_.handle(Action.PASTE)
        self.assertEqual(page_.key_text(), "key-from-the-site-1234")
        self.assertEqual(page_.focus(), "save")
        self.assertEqual(masked("key-from-the-site-1234"), "•" * 18 + "1234")
        saved = QSignalSpy(page_.saveKeyRequested)
        page_.handle(Action.NAV_ACTIVATE)
        self.assertEqual(saved.at(0), ["key-from-the-site-1234"])

    def test_save_without_a_key_says_so(self):
        saved = QSignalSpy(self.page.saveKeyRequested)
        self.page.handle(Action.NAV_ACTIVATE)
        self.assertEqual(saved.count(), 0)
        self.assertEqual(self.page.message(), KEY_EMPTY_TEXT)

    def test_the_keys_move_between_the_buttons(self):
        page_ = self.page
        page_.handle(Action.NAV_DOWN)
        self.assertEqual(page_.focus(), "get_key")  # no key yet
        page_.handle(Action.NAV_RIGHT)
        self.assertEqual(page_.focus(), "paste")
        page_.handle(Action.NAV_LEFT)
        asked = QSignalSpy(page_.getKeyRequested)
        page_.handle(Action.NAV_ACTIVATE)
        self.assertEqual(asked.count(), 1)
        page_.handle(Action.NAV_UP)
        self.assertEqual(page_.focus(), "field")
        closed = QSignalSpy(page_.closed)
        page_.handle(Action.ESCAPE)
        self.assertFalse(page_.isVisible())
        self.assertEqual(closed.count(), 1)

    def test_the_buttons_take_clicks(self):
        page_ = self.page
        asked = QSignalSpy(page_.getKeyRequested)
        self.click(page_, page_.control_rect("get_key").center())
        self.assertEqual(asked.count(), 1)
        QGuiApplication.clipboard().setText("pasted-key-9876")
        self.click(page_, page_.control_rect("paste").center())
        self.assertEqual(page_.key_text(), "pasted-key-9876")
        self.click(page_, page_.control_rect("clear").center())
        self.assertEqual(page_.key_text(), "")
        page_.type_text("typed")
        saved = QSignalSpy(page_.saveKeyRequested)
        self.click(page_, page_.control_rect("save").center())
        self.assertEqual(saved.at(0), ["typed"])
        tip = page_.tooltip_at(page_.control_rect("close").center())
        self.assertEqual(tip, key_hint("Close", "Esc"))
        self.click(page_, page_.control_rect("close").center())
        self.assertFalse(page_.isVisible())


class ResultsPageTests(Case):
    def setUp(self):
        super().setUp()
        self.page = results_page(self.host)

    def test_the_keys_walk_the_two_lists(self):
        page_ = self.page
        self.assertEqual((page_.page(), page_.focus()), (RESULTS_PAGE, "files"))
        self.assertEqual(page_.highlighted_file().release, "English 1")
        page_.handle(Action.NAV_DOWN)
        page_.handle(Action.NAV_DOWN)
        self.assertEqual(page_.highlighted_file().release, "English 3")
        page_.handle(Action.NAV_PAGE_DOWN)
        self.assertGreater(page_.highlighted_file().file_id, 3)
        page_.handle(Action.NAV_LEFT)
        self.assertEqual(page_.focus(), "languages")
        page_.handle(Action.NAV_DOWN)
        self.assertEqual(page_.selected_language(), "tr")
        self.assertEqual([f.release for f in page_.files_shown()], ["Turkish"])
        page_.handle(Action.NAV_RIGHT)
        chosen = QSignalSpy(page_.fileChosen)
        page_.handle(Action.NAV_ACTIVATE)
        self.assertEqual(chosen.at(0)[0].file_id, 20)

    def test_up_from_the_top_goes_to_the_field_and_typing_searches_again(self):
        page_ = self.page
        page_.handle(Action.NAV_UP)
        self.assertEqual(page_.focus(), "field")
        page_.handle(Action.NAV_BACKSPACE)
        page_.handle(Action.NAV_TEXT, ")")
        self.assertEqual(page_.query(), "Harbour Lights (2019)")
        searched = QSignalSpy(page_.searchRequested)
        page_.handle(Action.NAV_ACTIVATE)
        self.assertEqual(searched.at(0), ["Harbour Lights (2019)"])
        page_.handle(Action.NAV_RIGHT)
        self.assertEqual(page_.focus(), "search")
        page_.handle(Action.NAV_DOWN)
        self.assertEqual(page_.focus(), "files")

    def test_the_mouse_picks_a_language_and_a_subtitle(self):
        page_ = self.page
        self.click(page_, page_.row_rect("languages", 1).center())
        self.assertEqual(page_.selected_language(), "tr")
        chosen = QSignalSpy(page_.fileChosen)
        self.click(page_, page_.row_rect("files", 0).center())
        self.assertEqual(chosen.at(0)[0].file_id, 20)
        page_.set_busy("Downloading…")
        self.click(page_, page_.row_rect("files", 0).center())
        self.assertEqual(chosen.count(), 1)  # one download at a time

    def test_the_wheel_scrolls_a_long_list(self):
        page_ = self.page
        last = len(page_.files_shown()) - 1
        self.assertTrue(page_.row_rect("files", last).isEmpty())  # below what shows
        point = page_.row_rect("files", 0).center()
        for _ in range(12):
            event = QWheelEvent(
                point,
                page_.mapToGlobal(point),
                QPoint(),
                QPoint(0, -120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(page_, event)
        self.assertFalse(page_.row_rect("files", last).isEmpty())

    def test_a_click_beside_the_panel_closes_it_and_reaches_nothing_under_it(self):
        page_ = self.page
        self.click(page_, QPointF(5, 5))
        self.assertFalse(page_.isVisible())

    def test_search_and_the_fields_clear_take_clicks(self):
        page_ = self.page
        searched = QSignalSpy(page_.searchRequested)
        self.click(page_, page_.control_rect("search").center())
        self.assertEqual(searched.at(0), ["Harbour Lights (2019)"])
        self.click(page_, page_.control_rect("clear").center())
        self.assertEqual((page_.query(), page_.focus()), ("", "field"))
        self.click(page_, page_.control_rect("search").center())
        self.assertEqual(searched.count(), 1)  # nothing to search for


class FlowCase(Case):
    """The Subtitles panel with the OpenSubtitles search, over a fake player and HTTP."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = directory.name
        self.http = FakeHttp()
        self.opened = []
        self.saves = 0
        self.config = SimpleNamespace(
            subtitle_folder="",
            opensubtitles_api_key="",
            opensubtitles_username="",
            opensubtitles_password="",
            opensubtitles_language="",
            opensubtitles_hearing_impaired=False,
            save_config=self.saved,
        )
        self.picks = SubtitlePicks(
            os.path.join(self.dir, "subtitle_picks.json"), os.path.join(self.dir, "subtitles")
        )
        self.addCleanup(self.picks.flush)  # before the folder goes
        self.player = FakePlayer()
        self.current = FILM
        self.playing = True
        self.toasts = []
        self.transport = Transport(self.host, idle_ms=200)
        self.transport.set_pip_state(True, False)
        self.transport.show_vod("Harbour Lights", "")
        self.transport.update_position(0, 100_000)
        self.panel = TrackPanel(self.host)
        self.search_page = SubtitleSearchPage(self.host)
        self.online = OnlineSubtitles(
            page=self.search_page,
            setup=OnlineSetup(ImmediateRunner(), self.picks, self.opened.append, http=self.http),
            config=self.config,
            current=lambda: self.current,
            playing=lambda: self.playing,
            provider=lambda item: item.get("_provider", ""),
            load=lambda path: self.controls.load_subtitle_file(
                path, say=False, remember_folder=False
            ),
            toast=lambda text, *args: self.toasts.append(text),
        )
        self.controls = TrackControls(
            sound_panel=TrackPanel(self.host),
            subtitle_panel=self.panel,
            transport=self.transport,
            session=lambda: self.player,
            current=lambda: self.current,
            playing=lambda: self.playing,
            channel_key=lambda item: f"key-{item['id']}",
            store=SoundSync(None),
            toast=lambda text, *args: self.toasts.append(text),
            show_transport=self.transport.poke,
            pick_file=lambda folder: "",
            config=self.config,
            online=self.online,
        )
        self.controls.wire()
        no_built_in = mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", "")
        no_built_in.start()
        self.addCleanup(no_built_in.stop)
        english = mock.patch("ui.subtitle_search.system_language", return_value="en")
        english.start()
        self.addCleanup(english.stop)

    def saved(self):
        self.saves += 1

    def rows(self):
        return [row.text for row in self.panel.model().rows]

    def choose_row(self, key):
        self.controls.open_subtitles()
        keys = [row.key for row in self.panel.model().rows]
        self.click(self.panel, self.panel.row_rect(keys.index(key)).center())

    def search(self):
        self.choose_row(SEARCH_ONLINE)
        self.assertTrue(self.search_page.isVisible())


class FlowTests(FlowCase):
    def test_a_film_offers_the_search_and_a_live_channel_does_not(self):
        self.controls.open_subtitles()
        self.assertEqual(self.rows()[-1], SEARCH_ONLINE_TEXT)
        self.assertEqual(self.panel.model().rows[-1].icon, "search")
        self.controls.close()
        self.current = CHANNEL
        self.controls.open_subtitles()
        self.assertNotIn(SEARCH_ONLINE_TEXT, self.rows())

    def test_without_a_key_the_page_asks_for_one_and_save_keeps_it_and_searches(self):
        serving(self.http, *FOUND)
        self.search()
        page_ = self.search_page
        self.assertEqual(page_.page(), KEY_PAGE)
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.http.calls, [])
        self.click(page_, page_.control_rect("get_key").center())
        self.assertEqual(self.opened, [CONSUMERS_URL])
        QGuiApplication.clipboard().setText(KEY)
        page_.handle(Action.PASTE)
        page_.handle(Action.NAV_ACTIVATE)
        self.assertEqual(self.config.opensubtitles_api_key, KEY)
        self.assertEqual(self.saves, 1)
        self.assertEqual(page_.page(), RESULTS_PAGE)
        (request,) = self.http.made("GET", "/subtitles")
        self.assertEqual(request["headers"]["Api-Key"], KEY)
        self.assertEqual(dict(request["params"]), {"query": "harbour lights", "year": "2019"})
        self.assertEqual(page_.query(), "Harbour Lights (2019)")

    def test_a_built_in_key_searches_at_once_and_the_users_key_wins(self):
        serving(self.http, *FOUND)
        with mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", "built-in-key"):
            self.search()
            self.assertEqual(self.search_page.page(), RESULTS_PAGE)
            self.config.opensubtitles_api_key = KEY
            self.search_page.handle(Action.NAV_UP)
            self.search_page.handle(Action.NAV_ACTIVATE)  # search again
        keys = [r["headers"]["Api-Key"] for r in self.http.made("GET", "/subtitles")]
        self.assertEqual(keys, ["built-in-key", KEY])

    def test_results_show_the_languages_and_the_best_first(self):
        self.config.opensubtitles_api_key = KEY
        self.config.opensubtitles_language = "de"
        serving(self.http, *FOUND)
        self.search()
        page_ = self.search_page
        self.assertEqual(
            page_.languages(),
            [
                LanguageRow("de", "German", 1),  # the last one picked
                LanguageRow("en", "English", 2),  # this computer's
                LanguageRow("tr", "Turkish", 1),
            ],
        )
        self.assertEqual(page_.status(), "4 subtitles in 3 languages")
        page_.handle(Action.NAV_LEFT)
        page_.handle(Action.NAV_DOWN)
        self.assertEqual(
            [f.release for f in page_.files_shown()],
            ["Harbour.Lights.2019.1080p", "Harbour.Lights.2019.HI"],  # HI after the others
        )

    def test_a_subtitle_chosen_downloads_shows_and_is_kept_for_the_title(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        page_ = self.search_page
        self.assertEqual(page_.selected_language(), "en")  # this computer's
        page_.handle(Action.NAV_ACTIVATE)  # the best English one
        self.assertFalse(page_.isVisible())
        name = "Harbour.Lights.2019.en.srt"
        path = os.path.join(self.dir, "subtitles", "1", name)
        self.assertEqual(self.player.files, [path])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(
            self.toasts,
            [f"Subtitles: {name} · 4 downloads left today, more in 23 hours and 52 minutes"],
        )
        self.assertEqual(self.config.subtitle_folder, "")  # "Load a file…" keeps its folder
        self.assertEqual(self.config.opensubtitles_language, "en")
        self.controls.open_subtitles()
        rows, ticked = self.rows(), [r.text for r in self.panel.model().rows if r.checked]
        self.assertIn(name, rows)
        self.assertEqual(ticked, [name])
        self.assertFalse(any(r.key == REMEMBERED for r in self.panel.model().rows))  # it shows
        self.assertEqual(self.picks.get(self.online._key_for(FILM)).name, name)

    def test_next_time_the_title_plays_its_pick_is_offered_first(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        self.search_page.handle(Action.NAV_ACTIVATE)
        name = self.player.subtitles[-1][1]
        self.player = FakePlayer()  # the title again, later
        calls = len(self.http.calls)
        self.controls.open_subtitles()
        first = self.panel.model().rows[0]
        self.assertEqual((first.key, first.text), (REMEMBERED, f"{name} (OpenSubtitles)"))
        self.click(self.panel, self.panel.row_rect(0).center())
        self.assertEqual(len(self.http.calls), calls)  # from the folder, no download
        self.assertEqual([os.path.basename(p) for p in self.player.files], [name])
        self.assertEqual(self.toasts[-1], f"Subtitles: {name}")
        # Its file gone: downloaded again.
        os.remove(self.player.files[0])
        self.player = FakePlayer()
        self.choose_row(REMEMBERED)
        self.assertEqual(len(self.http.made("POST", "/download")), 2)
        self.assertEqual([os.path.basename(p) for p in self.player.files], [name])

    def test_an_episode_is_searched_by_its_series_and_numbers(self):
        self.current = EPISODE
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        (request,) = self.http.made("GET", "/subtitles")
        self.assertEqual(
            dict(request["params"]),
            {"query": "salt road", "season_number": "2", "episode_number": "5"},
        )
        self.assertEqual(self.search_page.query(), "Salt Road S02E05")

    def test_a_refused_key_brings_the_key_page_back(self):
        self.config.opensubtitles_api_key = KEY
        self.http.answer("GET", API + "/subtitles", FakeResponse(401, {"message": "invalid"}))
        self.search()
        self.assertEqual(self.search_page.page(), KEY_PAGE)
        self.assertIn("didn't accept the API key", self.search_page.message())
        self.assertEqual(self.search_page.key_text(), KEY)

    def test_problems_are_said_on_the_page(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.http.answer("POST", API + "/download", quota_answer())
        self.search()
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertTrue(self.search_page.isVisible())
        self.assertIn("5 a day per connection", self.search_page.status())
        self.assertEqual(self.player.files, [])
        self.http.answer("GET", API + "/subtitles", requests.ConnectionError("down"))
        self.search_page.handle(Action.NAV_UP)
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertIn("Couldn't reach OpenSubtitles", self.search_page.status())
        self.http.answer("GET", API + "/subtitles", FakeResponse(200, page()))
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertIn('No subtitles found for "Harbour Lights (2019)"', self.search_page.status())
        self.http.answer(
            "GET", API + "/subtitles", FakeResponse(429, {}, headers={"Retry-After": "3"})
        )
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertIn("Try again in 3 seconds", self.search_page.status())

    def test_another_title_closes_the_page_and_a_late_download_is_only_kept(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        self.controls.prepare(self.player, FILM, FILM)  # a retry of the film: it stays
        self.assertTrue(self.search_page.isVisible())
        # Another title opens (a live tune prepares before it becomes the current one).
        self.controls.prepare(self.player, EPISODE, FILM)
        self.assertFalse(self.search_page.isVisible())
        self.current = EPISODE
        # A download that ends after the film made way is kept for the film, not shown.
        self.online._download(FILM, SubtitleFile(1, "en", "Harbour"))
        self.assertEqual(self.player.files, [])
        self.assertIn("They show when it plays again", self.toasts[-1])
        self.assertIsNotNone(self.online.remembered(FILM))

    def test_signed_in_downloads_reuse_the_token_and_a_new_sign_in_signs_the_old_out(self):
        self.config.opensubtitles_api_key = KEY
        self.config.opensubtitles_username = "harbour_viewer"
        self.config.opensubtitles_password = "fictional-password"
        serving(self.http, *FOUND)
        self.http.answer("POST", API + "/login", FakeResponse(200, login_answer()))
        self.http.answer("DELETE", API + "/logout", FakeResponse(200, {}))
        self.search()
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.search()
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertEqual(len(self.http.made("POST", "/login")), 1)
        tokens = [r["headers"].get("Authorization") for r in self.http.made("POST", "/download")]
        self.assertEqual(tokens, ["Bearer token-1", "Bearer token-1"])
        # Settings → Sign out: the next request is anonymous, and the old sign-in ends.
        self.config.opensubtitles_username = self.config.opensubtitles_password = ""
        self.search()
        self.assertEqual(len(self.http.made("DELETE", "/logout")), 1)
        self.search_page.handle(Action.NAV_ACTIVATE)
        self.assertNotIn("Authorization", self.http.made("POST", "/download")[-1]["headers"])

    def test_the_keys_are_the_pages_while_it_shows(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        self.assertTrue(self.online.handle(Action.NAV_TEXT, "x"))
        self.assertTrue(self.online.handle(Action.OPEN_DRAWER))  # nothing opens over it
        self.assertFalse(self.online.handle(Action.PLAY_PAUSE))  # the player still pauses
        self.assertFalse(self.online.handle(Action.FULLSCREEN))
        self.online.handle(Action.ESCAPE)
        self.assertFalse(self.online.visible())
        self.assertFalse(self.online.handle(Action.NAV_TEXT, "x"))


class DownloadedLinkTests(FlowCase):
    def test_the_downloads_link_is_fetched_without_the_key(self):
        self.config.opensubtitles_api_key = KEY
        serving(self.http, *FOUND)
        self.search()
        self.search_page.handle(Action.NAV_ACTIVATE)
        (fetched,) = self.http.made("GET", LINK)
        self.assertNotIn("Api-Key", fetched["headers"])


class ControllerRoutingTests(ControllerTestCase):
    """The TV shell gives the page the keys while it shows, and closes it with the player."""

    MOVIE = {"id": "m7", "name": "Harbour Lights", "cmd": "http://vod/harbour.mkv"}

    def setUp(self):
        self.page_host = QWidget()
        self.page_host.resize(1280, 720)
        self.page_host.show()
        self.addCleanup(self.page_host.deleteLater)
        self.addCleanup(self.page_host.close)
        self.page = SubtitleSearchPage(self.page_host)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.picks = SubtitlePicks(None, os.path.join(directory.name, "subtitles"))
        self.http = FakeHttp()
        no_built_in = mock.patch.object(_build_keys, "OPENSUBTITLES_API_KEY", "")
        no_built_in.start()
        self.addCleanup(no_built_in.stop)
        super().setUp()
        self.controller.start()

    def make_controller(self, column, **extra):
        setup = OnlineSetup(ImmediateRunner(), self.picks, lambda url: None, http=self.http)
        extra.setdefault("online_subtitles", setup)
        return super().make_controller(column, **extra)

    def overlays(self, column):
        overlays = super().overlays(column)
        overlays.subtitle_search = self.page
        return overlays

    def open_page(self):
        self.controller.play_vod(self.MOVIE, "movie", play_context())
        self.catalog.linkReady.emit(self.catalog.links[-1][0], self.MOVIE["cmd"])
        self.controller._online.open()
        self.assertTrue(self.page.isVisible())

    def test_the_page_takes_the_keys_and_esc_gives_them_back(self):
        self.assertEqual(self.controller.key_context(), "player")
        self.open_page()
        self.assertEqual(self.page.page(), KEY_PAGE)  # no key yet
        self.assertEqual(self.controller.key_context(), "subtitles")
        self.assertTrue(self.controller.overlay_open())  # Esc never leaves for the Library
        self.assertFalse(self.controller.pointer_may_hide())
        self.controller.handle(Action.NAV_TEXT, "k")
        self.assertEqual(self.page.key_text(), "k")
        self.controller.handle(Action.OPEN_DRAWER)
        self.assertEqual(self.drawer.named("open_drawer"), [])  # nothing opens over it
        self.controller.handle(Action.ESCAPE)
        self.assertFalse(self.page.isVisible())
        self.assertEqual(self.controller.key_context(), "player")

    def test_the_library_closes_it(self):
        self.open_page()
        self.controller.stop_for_library()
        self.assertFalse(self.page.isVisible())


if __name__ == "__main__":
    unittest.main()
