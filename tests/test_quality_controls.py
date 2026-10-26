"""2.8: the ⚙ quality menu: a media server's versions and conversions, and the question before
the first conversion on a server."""

from types import SimpleNamespace
import unittest

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtWidgets import QApplication

from services import media_server as ms
from ui.keymap import Action
from ui.quality_controls import QUALITY, LiveSize, QualityControls


class FakePanel(QObject):
    rowChosen = Signal(object)
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.shown = []  # (model, button)
        self.dismissed = 0
        self.moves = []

    def show_panel(self, model, anchor=None, floor=None, button=None):
        self.shown.append((model, button))

    def model(self):
        return self.shown[-1][0]

    def isVisible(self):
        return bool(self.shown) and not self.dismissed

    def dismiss(self):
        self.dismissed += 1

    def move_focus(self, step):
        self.moves.append(step)

    def focused(self):
        return None

    def focus_row(self, row):
        self.moves.append(("row", row))

    def activate(self):
        self.moves.append("activate")


class FakeTransport(QObject):
    qualityRequested = Signal()
    bandChanged = Signal()

    def __init__(self):
        super().__init__()
        self.holds = []

    def button_rect(self, name):
        return QRectF(900, 600, 28, 28)

    def pos(self):
        from PySide6.QtCore import QPoint

        return QPoint(0, 0)

    def controls_top(self):
        return 560

    def isVisible(self):
        return True

    def hold(self, button):
        self.holds.append(button)

    def held(self):
        return self.holds[-1] if self.holds else ""


class FakeCard(QObject):
    answered = Signal(bool)

    def __init__(self):
        super().__init__()
        self.asked = []
        self.keys = []
        self.hidden = True

    def ask(self, question, yes, no):
        self.asked.append((question, yes, no))
        self.hidden = False

    def isHidden(self):
        return self.hidden

    def escape(self):
        if self.hidden:
            return False
        self.hidden = True
        self.answered.emit(False)
        return True

    def handle(self, action):
        self.keys.append(action)
        return True


VERSIONS = [
    {"id": "uhd", "height": 2160, "bitrate": 25_000_000, "uhd": True, "name": ""},
    {"id": "hd", "height": 1080, "bitrate": 8_200_000, "uhd": False, "name": ""},
]


class QualityControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.panel, self.transport, self.card = FakePanel(), FakeTransport(), FakeCard()
        self.target = SimpleNamespace(item={"id": "f1", "_provider": "Home server"}, live=False)
        self.menu = {"versions": list(VERSIONS), "quality": ms.Quality(source="hd")}
        self.chosen = []
        self.sizes = {"heights": [1080, 720, 360], "height": 1080, "max_height": 0}
        self.capped = []
        self.controls = QualityControls(
            panel=self.panel,
            transport=self.transport,
            card=self.card,
            current=lambda: self.target,
            menu=lambda item: self.menu,
            choose=self.chosen.append,
            live_menu=lambda: self.sizes,
            choose_live=self.capped.append,
        )
        self.controls.wire()

    def rows(self):
        return [(row.text, row.checked) for row in self.panel.model().rows]

    def test_the_versions_and_the_conversions_below_the_one_playing(self):
        self.transport.qualityRequested.emit()
        self.assertEqual(self.panel.shown[-1][1], QUALITY)
        self.assertEqual(self.panel.model().title, "Quality")
        self.assertEqual(
            self.rows(),
            [
                ("4K · 25 Mbit/s", False),
                ("1080p · 8.2 Mbit/s", True),  # what plays, as its file is
                ("720p · 4 Mbit/s, converted", False),
                ("480p · 2 Mbit/s, converted", False),
            ],
        )

    def test_a_conversion_playing_is_ticked(self):
        self.menu["quality"] = ms.Quality("hd", 720, 4_000_000)  # of the 1080p version
        self.transport.qualityRequested.emit()
        self.assertEqual([checked for _text, checked in self.rows()], [False, False, True, False])

    def test_a_version_plays_as_its_file_is_without_a_question(self):
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.Quality(source="uhd"))
        self.assertEqual(self.chosen, [ms.Quality(source="uhd")])
        self.assertEqual(self.card.asked, [])
        self.assertEqual(self.panel.dismissed, 1)

    def test_the_first_conversion_on_a_server_asks_first(self):
        self.transport.qualityRequested.emit()
        converted = ms.CONVERSIONS[1]
        self.panel.rowChosen.emit(converted)
        self.assertEqual(self.chosen, [])
        ((question, yes, no),) = self.card.asked
        self.assertIn("server's processor", question)
        self.assertEqual((yes, no), ("Convert", "Keep the original"))
        self.card.answered.emit(True)
        self.assertEqual(self.chosen, [converted])
        self.panel.rowChosen.emit(ms.CONVERSIONS[2])  # that server again: no question
        self.assertEqual(self.chosen, [converted, ms.CONVERSIONS[2]])
        self.assertEqual(len(self.card.asked), 1)

    def test_an_answer_for_a_title_no_longer_playing_changes_nothing(self):  # review M10
        # The card asked about one title; another (on another server) plays when "Convert"
        # comes: that server was never asked, nothing converts.
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.CONVERSIONS[1])
        self.target = SimpleNamespace(item={"id": "f9", "_provider": "Their server"}, live=False)
        self.card.answered.emit(True)
        self.assertEqual(self.chosen, [])

    def test_a_new_title_drops_the_question(self):
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.CONVERSIONS[1])
        self.controls.drop_question()
        self.assertTrue(self.card.isHidden())
        self.assertEqual(self.chosen, [])
        self.controls.drop_question()  # nothing asked: nothing to do

    def test_no_to_the_question_changes_nothing(self):
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.CONVERSIONS[1])
        self.card.answered.emit(False)
        self.assertEqual(self.chosen, [])
        self.card.answered.emit(True)  # a later answer to another asker's question
        self.assertEqual(self.chosen, [])

    def test_the_quality_playing_chosen_again_changes_nothing(self):
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.Quality(source="hd"))
        self.assertEqual(self.chosen, [])

    def test_while_it_shows_the_arrows_enter_and_esc_are_its_own(self):
        self.assertFalse(self.controls.handle(Action.OK))  # nothing shows
        self.transport.qualityRequested.emit()
        self.assertTrue(self.controls.handle(Action.CH_NEXT, Qt.Key.Key_Down.value))
        self.assertTrue(self.controls.handle(Action.OK))
        self.assertEqual(self.panel.moves, [("row", 0), "activate"])
        self.assertTrue(self.controls.handle(Action.ESCAPE))
        self.assertEqual(self.panel.dismissed, 1)

    def test_the_question_takes_the_keys_while_it_asks(self):
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(ms.CONVERSIONS[1])
        self.assertTrue(self.controls.handle(Action.ESCAPE))
        self.assertEqual(self.card.keys, [Action.ESCAPE])

    def test_a_channel_lists_the_best_and_its_sizes(self):  # the live ⚙
        self.target = SimpleNamespace(item={"id": 7}, live=True)
        self.transport.qualityRequested.emit()
        self.assertEqual(
            self.rows(),
            [("Auto (best)", True), ("1080p", False), ("720p", False), ("360p", False)],
        )
        self.sizes = {"heights": [2160, 1080], "height": 1080, "max_height": 1080}
        self.transport.qualityRequested.emit()
        self.assertEqual(self.rows(), [("Auto (best)", False), ("4K", False), ("1080p", True)])

    def test_a_cap_between_sizes_ticks_the_size_playing(self):
        # A cap kept from another day (480) where the channel now offers 1080, 720, 360.
        self.target = SimpleNamespace(item={"id": 7}, live=True)
        self.sizes = {"heights": [1080, 720, 360], "height": 360, "max_height": 480}
        self.transport.qualityRequested.emit()
        self.assertEqual([checked for _text, checked in self.rows()], [False, False, False, True])

    def test_a_size_is_chosen_without_a_question(self):
        self.target = SimpleNamespace(item={"id": 7}, live=True)
        self.transport.qualityRequested.emit()
        self.panel.rowChosen.emit(LiveSize(720))
        self.assertEqual((self.capped, self.chosen, self.card.asked), ([720], [], []))
        self.panel.rowChosen.emit(LiveSize(0))  # the best again: what plays (nothing to do)
        self.assertEqual(self.capped, [720])

    def test_a_channel_with_one_size_offers_nothing(self):
        self.target = SimpleNamespace(item={"id": 7}, live=True)
        self.sizes = {"heights": [1080], "height": 1080, "max_height": 0}
        self.transport.qualityRequested.emit()
        self.sizes = None
        self.transport.qualityRequested.emit()
        self.assertEqual(self.panel.shown, [])

    def test_nothing_offered_shows_nothing(self):
        self.menu = None
        self.transport.qualityRequested.emit()
        self.assertEqual(self.panel.shown, [])
        self.target = None
        self.transport.qualityRequested.emit()
        self.assertEqual(self.panel.shown, [])


if __name__ == "__main__":
    unittest.main()
