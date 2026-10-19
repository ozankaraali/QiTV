"""The painted overlays in Turkish (i18n/tr.json) and French (i18n/fr.json) at the default
window size: their texts are in the language, and QiTV's own words are never cut short (the
player controls' layout checks run in each language too)."""

from datetime import datetime, timedelta, timezone
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetrics, QFontMetricsF, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from services import failure_reasons, i18n
from services.epg_view import Programme
from tests import test_drawer, test_track_panel, test_transport
from ui.overlays import guide
from ui.overlays.welcome import WelcomeCard
from ui.overlays.zap_banner import BannerInfo, ZapBanner


def turkish(case: unittest.TestCase) -> None:
    case.assertEqual(i18n.set_language("tr"), "tr")
    case.addCleanup(i18n.set_language, "en")


def french(case: unittest.TestCase) -> None:
    case.assertEqual(i18n.set_language("fr"), "fr")
    case.addCleanup(i18n.set_language, "en")


class Elisions:
    """The texts cut with "…" while painting (a provider's names may be; QiTV's words not)."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.cut: list[str] = []
        for cls in (QFontMetrics, QFontMetricsF):
            original = cls.elidedText

            def elided(metrics, text, *args, _original=original, **kwargs):
                shown = _original(metrics, text, *args, **kwargs)
                if shown != text:
                    self.cut.append(text)
                return shown

            cls.elidedText = elided
            case.addCleanup(setattr, cls, "elidedText", original)


class Painted:
    """The texts painted (QPainter.drawText) while it is installed."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.texts: list[str] = []
        original = QPainter.drawText

        def draw(painter, *args, **kwargs):
            self.texts += [a for a in args if isinstance(a, str)]
            return original(painter, *args, **kwargs)

        QPainter.drawText = draw
        case.addCleanup(setattr, QPainter, "drawText", original)


class TurkishTransportLayoutTests(test_transport.SmallWidthLayoutTests):
    """The player controls' layout checks (no overlaps at any width, the whole Stay live
    indicator at 1280), in Turkish."""

    def setUp(self):
        turkish(self)

    def test_the_stay_live_indicator_reads_in_turkish(self):
        t = test_transport.Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.setGeometry(0, 0, 1280, 720)
        t.set_pip_state(True, False)
        t.show_timeshift("Liman Haber", "")
        t.update_timeline(self.state("live", 12.5, 1.05), [])
        self.assertEqual(
            t.indicator_text(), "Canlıda kal ● ≈ kaynağın 12,5 sn gerisinde · yetişiyor 1,05×"
        )
        cut = Elisions(self)
        t.grab()
        self.assertEqual(cut.cut, [])
        t.update_timeline(self.state("shifted", 300.0, 1.0), [])
        start, behind = t.texts()
        self.assertRegex(start, r"^Arabellek başı \d\d:\d\d$")
        self.assertEqual(behind, "canlının −05:00 gerisinde")


class TurkishFindTests(unittest.TestCase):
    """The channel list's find in Turkish: its rows and headings are whole at the default
    size and in a small window."""

    setUpClass = classmethod(lambda cls: setattr(cls, "app", QApplication.instance()))
    type = test_drawer.FindTests.type
    rows = test_drawer.FindTests.rows
    drawn = test_drawer.FindTests.drawn
    right = test_drawer.FindTests.right

    def setUp(self):
        turkish(self)
        test_drawer.FindTests.setUp(self)

    def test_the_rows_read_in_turkish_and_whole(self):
        for width, height in ((1280, 720), (640, 360)):
            with self.subTest(size=(width, height)):
                self.host.resize(width, height)
                self.drawer.open_drawer()
                self.type("kid")
                self.assertEqual(self.rows()[-1], ("found_library", "Film ve dizilerde ara ›"))
                drawn = self.drawn()
                self.assertIn(("Film ve dizilerde ara ›", "Film ve dizilerde ara ›"), drawn)
                self.assertIn(("KANALLAR", "KANALLAR"), drawn)


class TurkishSoundPanelTests(test_track_panel.PanelCase):
    def setUp(self):
        turkish(self)
        super().setUp()

    def test_the_sound_panel_reads_in_turkish_and_whole(self):
        self.click_button("audio")
        self.assertEqual(self.panel.model().title, "Ses")
        self.assertEqual(self.shown(), (["*English", "Deutsch"], "Senkronize"))
        cut = Elisions(self)
        self.panel.grab()
        self.assertEqual(cut.cut, [])
        self.assertEqual(test_track_panel.sync_text("Sound", 150), "Ses 150 ms geç")
        self.assertEqual(test_track_panel.sync_text("Sound", -1050), "Ses 1.050 ms erken")


class TurkishCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        turkish(self)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.close)
        self.host.show()

    def test_the_welcome_card(self):
        card = WelcomeCard(self.host)
        card.show_card()
        cut, painted = Elisions(self), Painted(self)
        self.assertFalse(card.grab().isNull())
        self.assertEqual(cut.cut, [])
        self.assertIn("QiTV'ye hoş geldiniz", painted.texts)
        self.assertIn("Sağlayıcı ekle", painted.texts)

    def test_the_guide_names_its_days_in_turkish(self):
        now = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc).astimezone()
        self.assertEqual(guide.day_label(now, now), "Bugün")
        self.assertEqual(guide.day_label(now + timedelta(days=1), now), "Yarın")
        self.assertEqual(guide.day_label(now + timedelta(days=2), now), "Çarşamba")

    def test_the_zapping_banner(self):
        now = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)

        def programme(title, minutes):
            start = now + timedelta(minutes=minutes)
            return Programme(title=title, start=start, stop=start + timedelta(minutes=60))

        banner = ZapBanner(self.host)
        info = BannerInfo("104", "Liman Doğa HD", None, programme("Kıyılar", -42), None)
        banner.show_info(BannerInfo(**{**info.__dict__, "next": programme("Dev Balinalar", 18)}))
        cut, painted = Elisions(self), Painted(self)
        banner.grab()
        self.assertEqual(cut.cut, [])
        self.assertIn("CANLI", painted.texts)
        self.assertIn("Şimdi", painted.texts)
        self.assertIn("Sonra  22:00 Dev Balinalar", painted.texts)

    def test_dates_and_numbers(self):
        when = datetime(2026, 10, 3)
        self.assertEqual(
            failure_reasons.expired_text(when), "Aboneliğiniz 3 Eki 2026 tarihinde sona erdi"
        )
        self.assertEqual(i18n.number(1234567), "1.234.567")
        self.assertEqual(i18n.decimal("1.25"), "1,25")


NNBSP = "\u202f"  # French digit grouping (Qt's)


class FrenchTransportLayoutTests(test_transport.SmallWidthLayoutTests):
    """The player controls' layout checks in French, and the whole Stay live indicator at
    1280 in each of its states."""

    def setUp(self):
        french(self)

    def test_the_stay_live_indicator_reads_in_french_and_whole(self):
        t = test_transport.Transport(idle_ms=3000)
        self.addCleanup(t.close)
        t.setGeometry(0, 0, 1280, 720)
        t.set_pip_state(True, False)
        t.show_timeshift("Liman Haber", "")
        auto = dict(target=10.0, auto=True, adjusted=True)
        states = (
            (
                self.state("live", 12.5, 1.05),
                "Rester en direct ● ≈ 12,5 s derrière la source · rattrapage 1,05×",
            ),
            (
                dict(self.state("live", 12.5, 1.05), **auto),
                "Rester en direct ● ≈ 12,5 s de retard · ajusté · rattrapage 1,05×",
            ),
            (
                dict(self.state("live", 12.5, 0.95), **auto, nudge=0.95),
                "Rester en direct ● ≈ 12,5 s de retard · ajusté · ralenti 0,95×",
            ),
        )
        for state, text in states:
            with self.subTest(text):
                t.update_timeline(state, [])
                self.assertEqual(t.indicator_text(), text)
                cut = Elisions(self)
                t.grab()
                self.assertEqual(cut.cut, [])
        t.update_timeline(self.state("shifted", 300.0, 1.0), [])
        start, behind = t.texts()
        self.assertRegex(start, r"^Tampon depuis \d\d:\d\d$")
        self.assertEqual(behind, "−05:00 derrière le direct")


class FrenchFindTests(unittest.TestCase):
    """The channel list's find in French: its rows and headings are whole at the default
    size and in a small window."""

    setUpClass = classmethod(lambda cls: setattr(cls, "app", QApplication.instance()))
    type = test_drawer.FindTests.type
    rows = test_drawer.FindTests.rows
    drawn = test_drawer.FindTests.drawn
    right = test_drawer.FindTests.right

    def setUp(self):
        french(self)
        test_drawer.FindTests.setUp(self)

    def test_the_rows_read_in_french_and_whole(self):
        for width, height in ((1280, 720), (640, 360)):
            with self.subTest(size=(width, height)):
                self.host.resize(width, height)
                self.drawer.open_drawer()
                self.type("kid")
                self.assertEqual(self.rows()[-1], ("found_library", "Rechercher films et séries ›"))
                drawn = self.drawn()
                self.assertIn(
                    ("Rechercher films et séries ›", "Rechercher films et séries ›"), drawn
                )
                self.assertIn(("CHAÎNES", "CHAÎNES"), drawn)


class FrenchSoundPanelTests(test_track_panel.PanelCase):
    def setUp(self):
        french(self)
        super().setUp()

    def test_the_sound_panel_reads_in_french_and_whole(self):
        self.click_button("audio")
        self.assertEqual(self.panel.model().title, "Son")
        self.assertEqual(self.shown(), (["*English", "Deutsch"], "Synchronisé"))
        cut = Elisions(self)
        self.panel.grab()
        self.assertEqual(cut.cut, [])
        self.assertEqual(test_track_panel.sync_text("Sound", 150), "Son retardé de 150 ms")
        self.assertEqual(
            test_track_panel.sync_text("Sound", -1050), f"Son avancé de 1{NNBSP}050 ms"
        )


class FrenchCardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        french(self)
        self.host = QWidget()
        self.host.resize(1280, 720)
        self.addCleanup(self.host.close)
        self.host.show()

    def test_the_welcome_card(self):
        card = WelcomeCard(self.host)
        card.show_card()
        cut, painted = Elisions(self), Painted(self)
        self.assertFalse(card.grab().isNull())
        self.assertEqual(cut.cut, [])
        self.assertIn("Bienvenue dans QiTV", painted.texts)
        self.assertIn("Ajouter un fournisseur", painted.texts)

    def test_the_guide_names_its_days_in_french(self):
        now = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc).astimezone()
        self.assertEqual(guide.day_label(now, now), "Aujourd’hui")
        self.assertEqual(guide.day_label(now + timedelta(days=1), now), "Demain")
        self.assertEqual(guide.day_label(now + timedelta(days=2), now), "mercredi")

    def test_the_zapping_banner(self):
        now = datetime(2026, 10, 15, 19, 42, tzinfo=timezone.utc)

        def programme(title, minutes):
            start = now + timedelta(minutes=minutes)
            return Programme(title=title, start=start, stop=start + timedelta(minutes=60))

        banner = ZapBanner(self.host)
        info = BannerInfo("104", "Liman Nature HD", None, programme("Les côtes", -42), None)
        banner.show_info(
            BannerInfo(**{**info.__dict__, "next": programme("Les baleines géantes", 18)})
        )
        cut, painted = Elisions(self), Painted(self)
        banner.grab()
        self.assertEqual(cut.cut, [])
        self.assertIn("DIRECT", painted.texts)
        self.assertIn("En cours", painted.texts)
        self.assertIn("À suivre  22:00 Les baleines géantes", painted.texts)

    def test_dates_and_numbers(self):
        when = datetime(2026, 10, 3)
        self.assertEqual(
            failure_reasons.expired_text(when), "Votre abonnement a expiré le 3 oct. 2026"
        )
        self.assertEqual(i18n.number(1234567), f"1{NNBSP}234{NNBSP}567")
        self.assertEqual(i18n.decimal("1.25"), "1,25")


if __name__ == "__main__":
    unittest.main()
