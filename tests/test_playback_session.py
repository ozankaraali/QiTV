import os
from pathlib import Path
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from services.playback import PlaybackSession, PlaybackState, create_instance
from services.vlc_profile import PlayKind

FIXTURE = Path(__file__).parent / "fixtures" / "smoke.mp4"
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])


def wait_for(predicate, timeout_ms=8000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        QTest.qWait(50)
        waited += 50
    return predicate()


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
class PlaybackSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=3000)
        self.states = []
        self.session.stateChanged.connect(self.states.append)
        self.addCleanup(self.session.release)

    def test_stop_is_safe_before_play(self):
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)

    def test_plays_pauses_seeks_and_stops(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.assertTrue(wait_for(lambda: self.session.length_ms() > 0))
        self.session.set_paused(True)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PAUSED))
        self.session.set_paused(False)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.PLAYING))
        self.session.stop()
        self.assertIs(self.session.state(), PlaybackState.IDLE)
        self.assertIn("playing", self.states)

    def test_resume_position(self):
        self.session.play(FIXTURE.as_uri(), kind=PlayKind.VOD_FILE, start_ms=1000)
        self.assertTrue(wait_for(lambda: self.session.time_ms() >= 900))

    def test_bad_stream_reports_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        self.session.play("http://127.0.0.1:9/missing.ts", kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)

    def test_ended_before_playing_is_an_error(self):
        errors = []
        self.session.errorOccurred.connect(errors.append)
        missing = (FIXTURE.parent / "does-not-exist.ts").as_uri()
        self.session.play(missing, kind=PlayKind.LIVE)
        self.assertTrue(wait_for(lambda: self.session.state() is PlaybackState.ERROR, 6000))
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
