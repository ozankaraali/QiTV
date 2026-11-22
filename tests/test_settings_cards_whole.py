"""Settings' cards show whole in the TV window (2.14): the overlay host shows only its overlays
(its mask), and a card Settings makes as it is first needed joins them, so nothing of it is cut
off at the drawer's edge, at 1920 × 1080, 1280 × 720 or 800 × 450, and in every language: the
Add card, its other ways each whole, and Plex's sign-in with its code. The real window,
offscreen."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFontMetricsF, QRegion
from PySide6.QtWidgets import QApplication
from timer_fixtures import wait_until

from scripts import i18n_extract as extract
from services import i18n
from services.plex_workers import PinJob
from services.settings_model import PROVIDERS
from tests.test_tv_window import WindowCase
from ui.overlays import settings_card as sc

SIZES = ((1920, 1080), (1280, 720), (800, 450))
LANGUAGES = ["en"] + [path.stem for path in extract.catalog_paths()]


class SettingsCardsWholeTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(i18n.set_language, "en")
        self.show()

    def sized(self, width, height):
        self.window.resize(width, height)
        host = self.window.host
        self.assertTrue(wait_until(lambda: host.width() == self.window.surface.width()))
        QApplication.processEvents()
        return host

    def assert_whole(self, card):
        """`card` is one of the host's overlays, and its panel lies inside what the host shows
        (its mask) and inside the window."""
        host = self.window.host
        self.assertIn(card, host.visible_overlays())
        self.assertEqual(card.geometry(), host.rect())
        panel = card.card_rect().toAlignedRect()
        self.assertTrue(host.rect().contains(panel), (panel, host.rect()))
        self.assertTrue(QRegion(panel).subtracted(host.mask()).isEmpty(), panel)
        return QRectF(panel)

    def test_the_add_card_and_each_other_way_show_whole_in_every_language(self):
        cards = self.window.provider_cards
        form = cards.form
        for width, height in SIZES:
            self.sized(width, height)
            for code in LANGUAGES:
                with self.subTest(size=width, language=code):
                    i18n.set_language(code)
                    self.window.settings.open(PROVIDERS)
                    cards._close()
                    cards.add()  # in this language
                    panel = self.assert_whole(form)
                    fonts = form._fonts(form._units())
                    pad = form._units().px(sc._ALT_PAD_U)
                    for alt in form._alternates:
                        rect = form.control_rect("alt", alt.key)
                        self.assertTrue(panel.contains(rect), (alt.text, rect, panel))
                        whole = QFontMetricsF(fonts["alt"]).horizontalAdvance(alt.text) + 2 * pad
                        self.assertGreaterEqual(rect.width() + 0.5, whole, alt.text)
                    for key in form.values():  # the fields keep room to type in
                        field = form.control_rect("field", key)
                        self.assertGreater(field.width(), panel.width() / 3, key)

    def test_plexs_code_shows_whole(self):
        signin = self.window.provider_cards.plex
        held = []

        def run_worker(worker, finished, error=None, context=None):  # never plex.tv
            held.append((worker, finished, context))
            return worker

        for width, height in SIZES:
            self.sized(width, height)
            with self.subTest(size=width):
                i18n.set_language("tr")
                self.window.settings.open(PROVIDERS)
                with mock.patch.object(self.window.jobs, "run_worker", side_effect=run_worker):
                    signin.start()
                    worker, finished, context = held.pop()
                    self.assertIsInstance(worker, PinJob)
                    finished(context, {"id": 7, "code": "H56G", "expires_in": 900})
                panel = self.assert_whole(signin.card)
                for zone in ("code", "link", "body"):
                    self.assertTrue(panel.contains(signin.card.control_rect(zone)), zone)
                signin._reset()

    def test_a_card_made_over_the_host_later_is_one_of_its_overlays(self):
        host = self.window.host
        card = sc.ChoiceCard(host)  # as Settings' sections make theirs, when first needed
        self.addCleanup(card.deleteLater)
        self.window.settings.open(PROVIDERS)
        card.ask("Remove?", "Words", [sc.Button(sc.CANCEL, "Cancel")], sc.CANCEL)
        self.window.settings.show_card(card)
        self.assert_whole(card)
        host.add_overlay(card)  # added again: still once
        self.assertEqual(host._overlays.count(card), 1)


if __name__ == "__main__":
    unittest.main()
