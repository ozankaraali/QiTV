"""What a screen reader gets from the painted TV screens: each overlay is one element, named
after what it is ("Player controls") in the active language; a panel after its title; a
question, the sleep notice, the next card and the zap banner after what they say, which is
announced. The Library's top bar is described by the section it shows, the poster grid named
after the genre it lists."""

import importlib
import inspect
import json
import os
from pathlib import Path
import pkgutil
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QWidget

from services import i18n
from ui.library.genre_list import GenreList
from ui.library.poster_grid import PosterGrid
from ui.library.top_nav import TopNav
import ui.overlays
from ui.overlays.base import Overlay
from ui.overlays.mixer import Mixer
from ui.overlays.question_card import QuestionCard
from ui.overlays.track_panel import PanelModel, PanelRow, TrackPanel
from ui.overlays.transport import Transport
from ui.overlays.zap_banner import BannerInfo, ZapBanner

ROOT = Path(__file__).resolve().parent.parent
# Named as they show, after what they say (or no element of their own: TileHits draws nothing).
NAMED_AS_THEY_SHOW = {"Toast", "QuestionCard", "NextCard", "ZapBanner", "TrackPanel", "TileHits"}


def overlay_classes() -> list[type]:
    found = []
    for module in pkgutil.iter_modules(ui.overlays.__path__):
        loaded = importlib.import_module(f"ui.overlays.{module.name}")
        for _, cls in inspect.getmembers(loaded, inspect.isclass):
            if (
                issubclass(cls, Overlay)
                and cls is not Overlay
                and cls.__module__ == loaded.__name__
            ):
                found.append(cls)
    return found


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.deleteLater)
        self.addCleanup(self.host.close)
        self.host.show()
        patcher = mock.patch.object(QAccessible, "updateAccessibility")
        self.posted = patcher.start()
        self.addCleanup(patcher.stop)

    def said(self) -> list[str]:
        return [
            call.args[0].message()
            for call in self.posted.call_args_list
            if call.args[0].type() == QAccessible.Event.Announcement
        ]


class OverlayNameTests(_Case):
    def test_every_overlay_says_what_it_is_in_every_language(self):
        classes = overlay_classes()
        self.assertGreater(len(classes), 15)
        catalogs = {
            lang: json.loads((ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("tr", "de", "es", "fr")
        }
        for cls in classes:
            if cls.__name__ in NAMED_AS_THEY_SHOW:
                continue
            with self.subTest(cls.__name__):
                self.assertTrue(cls.accessible_name)
                for lang, catalog in catalogs.items():
                    self.assertTrue(catalog.get(cls.accessible_name), lang)

    def test_the_names_are_in_the_active_language(self):
        self.assertEqual(Transport(self.host).accessibleName(), "Player controls")
        self.assertEqual(i18n.set_language("tr"), "tr")
        self.addCleanup(i18n.set_language, "en")
        self.assertEqual(Transport(self.host).accessibleName(), "Oynatıcı denetimleri")
        self.assertEqual(Mixer(self.host).accessibleName(), "Ses mikseri")

    def test_a_panel_is_named_after_its_title(self):
        panel = TrackPanel(self.host)
        panel.show_panel(PanelModel("Subtitles", (PanelRow("off", "Off", checked=True),)))
        self.assertEqual(panel.accessibleName(), "Subtitles")


class SaidAsItShowsTests(_Case):
    def test_a_question_is_said_at_once(self):
        card = QuestionCard(self.host)
        card.ask("Stop the recording?", "Stop", "Keep recording")
        self.assertEqual(card.accessibleName(), "Stop the recording?")
        self.assertEqual(self.said(), ["Stop the recording?"])
        event = self.posted.call_args_list[-1].args[0]
        self.assertEqual(event.politeness(), QAccessible.AnnouncementPoliteness.Assertive)

    def test_the_zap_banner_says_a_new_channel_once(self):
        banner = ZapBanner(self.host)
        info = BannerInfo("104", "Harbour Nature HD", None, None, None)
        banner.show_info(info)
        banner.show_info(info)  # the same channel again (its guide came): not said again
        self.assertEqual(banner.accessibleName(), "104 Harbour Nature HD")
        self.assertEqual(self.said(), ["104 Harbour Nature HD"])


class LibraryNameTests(_Case):
    def test_the_top_bar_is_described_by_its_section(self):
        nav = TopNav(self.host)
        self.assertEqual(nav.accessibleName(), "QiTV")
        self.assertEqual(nav.accessibleDescription(), "Live TV")
        nav.set_active("movies")
        self.assertEqual(nav.accessibleDescription(), "Movies")
        nav.set_active("search")
        self.assertEqual(nav.accessibleDescription(), "Search")

    def test_the_genres_and_the_posters_are_named(self):
        self.assertEqual(GenreList(self.host).accessibleName(), "Genres")
        grid = PosterGrid(None, self.host)
        self.assertEqual(grid.accessibleName(), "Movies and series")
        grid.set_header("Comedy", "120 titles", [], "")
        self.assertEqual(grid.accessibleName(), "Comedy")
        self.assertEqual(grid.accessibleDescription(), "120 titles")


if __name__ == "__main__":
    unittest.main()
