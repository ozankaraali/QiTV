"""Autoplay follows a local file's next episode (the play context's "local_next")."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from services.next_episode import play_context
from ui.autoplay import Autoplay
from ui.link_lanes import Target

NEXT = {"name": "Show", "season": 1, "number": 2, "local_path": "/v/Show.S01E02.mkv", "size": 9}


class FakeCard(QObject):
    playNow = Signal()
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.show_next = mock.Mock()
        self.escape = mock.Mock()

    def isVisible(self):
        return False


class FakeCatalog(QObject):
    episodesReady = Signal(str, str)
    episodesFailed = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.episodes = mock.Mock()
        self.seasons = mock.Mock()
        self.groups = mock.Mock()
        self.request_episodes = mock.Mock()


def local_target(context=None):
    item = {"name": "Show", "season": 1, "number": 1, "local_path": "/v/Show.S01E01.mkv"}
    return Target(item, "movies", "Show", local=True, context=context)


class LocalNextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.card, self.catalog = FakeCard(), FakeCatalog()
        self.config = mock.Mock(auto_play_episodes=True, auto_play_movies=False)
        self.play, self.declined, self.transport = mock.Mock(), mock.Mock(), mock.Mock()
        self.autoplay = Autoplay(
            catalog=self.catalog,
            config=self.config,
            card=self.card,
            play=self.play,
            is_current=lambda _t: True,
            external_mode=lambda: False,
            notify=mock.Mock(),
            set_hint=mock.Mock(),
            show_transport=self.transport,
            declined=self.declined,
        )
        self.target = local_target(play_context({"local_next": NEXT}))

    def test_local_next_offers_the_card_with_its_episode(self):
        self.assertTrue(self.autoplay.on_ended(self.target))
        self.card.show_next.assert_called_once_with("Show", "S1 · E2", kind="episode")
        self.catalog.episodes.assert_not_called()
        self.catalog.seasons.assert_not_called()

    def test_local_next_follows_the_episode_setting(self):
        self.config.auto_play_episodes, self.config.auto_play_movies = False, True
        self.assertFalse(self.autoplay.on_ended(self.target))
        self.card.show_next.assert_not_called()
        self.autoplay.next_requested(self.target)
        self.assertEqual(self.play.call_args.args[0], NEXT)
        self.catalog.episodes.assert_not_called()

    def test_playing_the_card_plays_the_item(self):
        self.autoplay.on_ended(self.target)
        self.card.playNow.emit()
        item, _kind, context = self.play.call_args.args
        self.assertEqual(item, NEXT)
        self.assertFalse(context["start_over"])

    def test_esc_on_a_local_card_says_declined(self):
        self.autoplay.on_ended(self.target)
        self.card.cancelled.emit()
        self.declined.assert_called_once_with()
        self.transport.assert_called_once_with()

    def test_a_drop_is_not_a_decline(self):
        self.autoplay.on_ended(self.target)
        self.autoplay.drop()
        self.card.cancelled.emit()
        self.declined.assert_not_called()

    def test_without_local_next_nothing_changes(self):
        target = local_target()
        self.assertFalse(Autoplay.has_next(target))
        self.assertFalse(self.autoplay.on_ended(target))
        self.card.show_next.assert_not_called()


if __name__ == "__main__":
    unittest.main()
