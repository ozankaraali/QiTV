"""Local files resume where they stopped (File → Open File…)."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from test_tv_controller import ControllerTestCase

from services.watch_state import WatchState, local_key


class LocalResumeTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.store = SimpleNamespace(playback_positions={}, save_config=lambda retry=True: None)
        self.store.save_playback_position = lambda cid, pos, dur: (
            self.store.playback_positions.__setitem__(
                cid, {"position": pos, "duration": dur, "timestamp": "2026-11-17T10:00:00"}
            )
        )
        self.controller.start()
        self.controller.watch = WatchState(self.store)

    def test_play_file_resumes_from_saved_point(self):
        key = local_key("ep.mkv", 1234)
        self.store.playback_positions[key] = {"position": 600_000, "duration": 2_400_000}
        self.controller.play_file("/videos/ep.mkv", size=1234)
        url, kwargs = self.session.plays[-1]
        self.assertEqual(url, Path("/videos/ep.mkv").absolute().as_uri())
        self.assertEqual(kwargs["start_ms"], 600_000)
        self.session.positionChanged.emit(700_000, 2_400_000)
        self.assertEqual(self.store.playback_positions[key]["position"], 700_000)

    def test_play_file_without_size_keeps_no_point(self):
        self.controller.play_file("/videos/ep.mkv")
        self.assertEqual(self.session.plays[-1][1]["start_ms"], 0)
        self.session.positionChanged.emit(700_000, 2_400_000)
        self.assertEqual(self.store.playback_positions, {})

    def test_a_short_song_keeps_no_point(self):
        self.controller.play_file("/music/song.mp3", size=99)
        self.session.positionChanged.emit(60_000, 180_000)
        self.assertEqual(self.store.playback_positions, {})


if __name__ == "__main__":
    unittest.main()
