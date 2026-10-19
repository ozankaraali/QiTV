import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.watch_state import WatchState


def make_config():
    cfg = SimpleNamespace(
        playback_positions={},
        last_watched=None,
        last_live_channel=None,
        history=[],
        saves=0,
    )
    cfg.add_to_watch_history = lambda item, t, link, p, content_id=None: cfg.history.append(
        (item, t, link, p)
    )
    cfg.save_config = lambda retry=True: setattr(cfg, "saves", cfg.saves + 1)
    cfg.save_playback_position = lambda cid, pos, dur: cfg.playback_positions.__setitem__(
        cid, {"position": pos, "duration": dur}
    )
    return cfg


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class WatchStateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config()
        self.clock = Clock()
        self.ws = WatchState(self.cfg, clock=self.clock)

    def test_movie_id_format(self):
        self.assertEqual(
            WatchState.content_id("P", {"id": 42, "name": "Film"}, item_type="movie"), "P:42"
        )

    def test_episode_ids_differ_by_season_and_episode(self):
        a = WatchState.content_id("P", {"id": 7, "season": 1, "series": 2}, item_type="episode")
        b = WatchState.content_id("P", {"id": 7, "season": 2, "series": 2}, item_type="episode")
        c = WatchState.content_id("P", {"id": 7, "season": 1, "series": 3}, item_type="episode")
        self.assertEqual(a, "P:7:s1:e2")
        self.assertEqual(len({a, b, c}), 3)

    def test_fallback_id_is_stable(self):
        item = {"title": "x", "n": [1, 2]}
        a = WatchState.content_id("P", item, item_type="movie")
        b = WatchState.content_id("P", dict(reversed(list(item.items()))), item_type="movie")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("P:"))

    def test_record_play_channel_sets_both_and_saves_once(self):
        item = {"id": 1, "name": "News"}
        self.ws.record_play("P", item, item_type="channel", link="http://x", group_id=5)
        self.assertEqual(self.cfg.last_watched["item_type"], "channel")
        self.assertEqual(
            self.cfg.last_live_channel, {"provider_name": "P", "group_id": "5", "item": item}
        )
        self.assertEqual(self.cfg.saves, 1)
        self.assertEqual(len(self.cfg.history), 1)

    def test_vod_play_leaves_last_live_channel(self):
        self.ws.record_play("P", {"id": 42, "name": "Film"}, item_type="movie", link="http://x")
        self.assertIsNone(self.cfg.last_live_channel)
        self.assertEqual(self.cfg.last_watched["item_type"], "movie")

    def test_resume_below_threshold_is_none(self):
        self.cfg.save_playback_position("c", 4000, 100000)
        self.assertIsNone(self.ws.resume_position("c"))

    def test_resume_midway_returns_position(self):
        self.cfg.save_playback_position("c", 50000, 100000)
        self.assertEqual(self.ws.resume_position("c"), 50000)

    def test_resume_near_end_is_none(self):
        self.cfg.save_playback_position("c", 95000, 100000)
        self.assertIsNone(self.ws.resume_position("c"))

    def test_resume_unknown_is_none(self):
        self.assertIsNone(self.ws.resume_position("nope"))

    def test_update_position_throttles_saves(self):
        self.ws.update_position("c", 10000, 100000)
        self.clock.t += 10
        self.ws.update_position("c", 20000, 100000)
        self.assertEqual(self.cfg.saves, 0)
        self.clock.t += 30
        self.ws.update_position("c", 30000, 100000)
        self.assertEqual(self.cfg.saves, 1)
        self.assertEqual(self.cfg.playback_positions["c"]["position"], 30000)

    def test_forget_drops_the_entries_it_picks_and_saves_the_rest_as_they_were(self):
        one = {"provider_name": "P", "item_type": "channel", "item_data": {"id": 1}}
        two = {"provider_name": "P", "item_type": "channel", "item_data": {"id": 2}}
        film = {"provider_name": "P", "item_type": "movie", "item_data": {"id": 1}}
        self.cfg.watch_history = [one, film, dict(one), two]
        removed = self.ws.forget(
            lambda e: e["item_type"] == "channel" and e["item_data"]["id"] == 1
        )
        self.assertEqual(removed, 2)  # both of its plays
        self.assertEqual(self.cfg.watch_history, [film, two])  # the same entries, the same order
        self.assertIs(self.cfg.watch_history[0], film)
        self.assertEqual(self.cfg.saves, 1)

    def test_forget_without_a_match_changes_and_saves_nothing(self):
        history = [{"provider_name": "P", "item_type": "movie", "item_data": {"id": 1}}]
        self.cfg.watch_history = history
        self.assertEqual(self.ws.forget(lambda entry: False), 0)
        self.assertIs(self.cfg.watch_history, history)
        self.assertEqual(self.cfg.saves, 0)

    def test_flush_always_saves(self):
        self.ws.flush()
        self.ws.flush()
        self.assertEqual(self.cfg.saves, 2)


def wait_until(predicate, timeout_ms=2000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(10)
        waited += 10
    return predicate()


class DeferredSaveTests(unittest.TestCase):
    """The shell saves plays on a timer, not on the GUI thread at every tune (Minor 7)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = make_config()
        self.ws = WatchState(self.cfg, save_delay_ms=40)

    def test_a_burst_of_plays_is_saved_once_after_it_settles(self):
        for i in range(5):
            self.ws.record_play("P", {"id": i}, item_type="channel", link="u", group_id=1)
        self.assertEqual(self.cfg.saves, 0)
        self.assertEqual(self.cfg.last_live_channel["item"], {"id": 4})  # in memory at once
        self.assertTrue(wait_until(lambda: self.cfg.saves == 1))
        QTest.qWait(100)
        self.assertEqual(self.cfg.saves, 1)

    def test_flush_saves_a_pending_play_at_once(self):
        self.ws.record_play("P", {"id": 1}, item_type="channel", link="u")
        self.ws.flush()  # e.g. the window closes
        self.assertEqual(self.cfg.saves, 1)
        QTest.qWait(100)
        self.assertEqual(self.cfg.saves, 1)  # the timer no longer runs

    def test_a_channel_taken_out_of_recents_is_saved_once_it_settles(self):
        self.cfg.watch_history = [{"item_type": "channel", "item_data": {"id": 1}}]
        self.assertEqual(self.ws.forget(lambda entry: True), 1)
        self.assertEqual(self.cfg.saves, 0)  # never on the GUI thread at once
        self.assertTrue(wait_until(lambda: self.cfg.saves == 1))

    def test_without_a_delay_a_play_is_saved_at_once(self):
        WatchState(self.cfg).record_play("P", {"id": 1}, item_type="movie", link="u")
        self.assertEqual(self.cfg.saves, 1)

    def test_a_settled_zap_never_waits_for_a_locked_file(self):
        # A locked config.json is retried for 250 ms: never on the save after a zap.
        retries = []
        self.cfg.save_config = lambda retry=True: retries.append(retry)
        self.ws.record_play("P", {"id": 1}, item_type="channel", link="u")
        self.assertTrue(wait_until(lambda: retries))
        self.ws.flush()  # a close may wait out a transient lock
        self.assertEqual(retries, [False, True])


class LockedConfigTests(unittest.TestCase):
    """config.json can stay locked on Windows: a zap or a close must not raise."""

    def setUp(self):
        self.cfg = make_config()
        self.attempts = 0

        def locked():
            self.attempts += 1
            raise PermissionError(13, "Access is denied")

        self.cfg.save_config = locked
        self.clock = Clock()
        self.ws = WatchState(self.cfg, flush_interval_s=30.0, clock=self.clock)

    def test_record_play_and_flush_log_instead_of_raising(self):
        with self.assertLogs("services.watch_state", "WARNING"):
            self.ws.record_play("P", {"id": 1}, item_type="channel", link="u")
            self.ws.flush()
        self.assertEqual(self.cfg.last_live_channel["item"], {"id": 1})
        self.assertEqual(self.attempts, 2)

    def test_a_failed_save_logs_a_message_without_the_path(self):
        def locked():
            raise PermissionError(13, "Access is denied", "/home/alice/config.json")

        self.cfg.save_config = locked
        with self.assertLogs("services.watch_state", "WARNING") as logs:
            self.ws.flush()
        output = "\n".join(logs.output)
        self.assertIn("PermissionError", output)
        self.assertIn("Access is denied", output)
        self.assertNotIn("alice", output)

    def test_a_failed_save_is_retried_after_the_flush_interval(self):
        with self.assertLogs("services.watch_state", "WARNING"):
            self.ws.record_play("P", {"id": 1}, item_type="movie", link="u")
            self.ws.update_position("cid", 60000, 600000)  # within the interval: no retry
            self.assertEqual(self.attempts, 1)
            self.clock.t += 31
            self.ws.update_position("cid", 61000, 600000)
        self.assertEqual(self.attempts, 2)


if __name__ == "__main__":
    unittest.main()
