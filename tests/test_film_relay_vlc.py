"""The film relay with real libVLC (2.9): the window's player plays a provider's film through it
(muted, no picture shown) while AI captions' film reader (services.captions.film) reads the same
film through it, from a stand-in provider on 127.0.0.1 that notes any moment two of its
connections are open at once.

The films are made here with ffmpeg (skipped without it): 40 s at 6 Mbit/s, an MKV and an MP4
with its moov at the end, whose sound is silence with a short beep at known moments. The reader
must place each beep within 0.2 s of where it is in the film, before and after the picture seeks.
"""

import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import wave

from PySide6.QtWidgets import QApplication
from film_provider import FilmProvider
from timer_fixtures import wait_until

from services.film_relay import FilmRelay
from services.playback import PlaybackSession, PlaybackState, create_instance
from services.vlc_profile import classify

INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--no-video-title-show", "--quiet"])
FFMPEG = shutil.which("ffmpeg")
LENGTH_S = 40.0
RATE = 1_500_000  # the stand-in provider's bytes a second per connection (the film's: 0.76 MB)
TOLERANCE_S = 0.2
_GAPS = (0.7, 1.1, 0.9, 1.3, 0.8, 1.2)


def beeps() -> list[float]:
    found, at, i = [], 0.5, 0
    while at < LENGTH_S - 0.5:
        found.append(round(at, 3))
        at += _GAPS[i % len(_GAPS)]
        i += 1
    return found


def make_films(folder: Path) -> dict[str, Path]:
    rate = 48_000
    pcm = bytearray(int(rate * LENGTH_S) * 2)
    for onset in beeps():
        first = int(onset * rate)
        for n in range(int(0.04 * rate)):
            value = int(0.6 * 32767 * math.sin(2 * math.pi * 1000 * n / rate))
            struct.pack_into("<h", pcm, (first + n) * 2, value)
    sound = folder / "beeps.wav"
    with wave.open(str(sound), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(pcm))
    films = {}
    for extension in (".mkv", ".mp4"):
        film = folder / f"film{extension}"
        subprocess.run(
            [
                FFMPEG, "-v", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={LENGTH_S}",
                "-i", str(sound),
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
                "-b:v", "6M", "-minrate", "6M", "-maxrate", "6M", "-bufsize", "3M",
                "-x264-params", "nal-hrd=cbr", "-c:a", "aac", "-b:a", "96k", "-shortest",
                str(film),
            ],
            check=True,
        )  # fmt: skip
        films[extension] = film
    return films


class Listener:
    """What a film reader placed: (film second, 16 kHz samples) as libVLC plays them."""

    def __init__(self) -> None:
        self.blocks: list[tuple[float, object]] = []
        self._lock = threading.Lock()

    def __call__(self, at, samples) -> None:
        with self._lock:
            self.blocks.append((at, samples))

    def onsets(self) -> list[float]:
        with self._lock:
            blocks = list(self.blocks)
        found, loud_at = [], -10.0
        for at, samples in blocks:
            for i, value in enumerate(samples):
                if abs(value) > 0.25:
                    moment = at + i / 16_000
                    if moment - loud_at > 0.3:
                        found.append(moment)
                    loud_at = moment
        return found


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
@unittest.skipIf(FFMPEG is None, "ffmpeg makes the films")
class FilmThroughRelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.folder = tempfile.TemporaryDirectory()
        cls.films = make_films(Path(cls.folder.name))

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def setUp(self):
        self.provider = None

    def tearDown(self):
        if self.provider is not None:
            self.assertEqual(self.provider.overlaps, [], "two connections were open at once")

    def start(self, extension, content_type):
        self.provider = FilmProvider(
            self.films[extension].read_bytes(),
            extension=extension,
            content_type=content_type,
            rate=RATE,
        )
        self.addCleanup(self.provider.close)
        relay = FilmRelay(self.provider.url, user_agent="VLC/3.0.20")
        relay.start()
        self.addCleanup(relay.wait_closed, 3.0)
        self.addCleanup(relay.stop)
        session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=15_000)
        self.addCleanup(self.release, session)
        return relay, session

    def release(self, session):
        session.release()
        wait_until(lambda: session._runner.pending() == 0, 10_000)

    def read_captions(self, relay, start_s, seconds):
        """The captions' film reader through the relay from `start_s` for `seconds`: the beeps
        it placed and each one's distance from where it is in the film."""
        from services.captions.film import FilmReader

        listener = Listener()
        reader = FilmReader(relay.follower_url, start_s, listener)
        reader.start()
        wait_until(lambda: False, int(seconds * 1000))  # the Qt loop runs the picture meanwhile
        reader.stop()
        truth = beeps()
        heard = [o for o in listener.onsets() if o >= start_s + 0.2]
        errors = [min((o - t for t in truth), key=abs) for o in heard]
        self.placed = [
            round(at, 2) for at, _ in listener.blocks[:: max(1, len(listener.blocks) // 8)]
        ]
        return heard, errors

    def check_placed(self, heard, errors, label):
        why = f"{label}: too few beeps placed ({heard}; blocks at {self.placed})"
        self.assertGreaterEqual(len(heard), 3, why)
        worst = max(abs(e) for e in errors)
        self.assertLess(worst, TOLERANCE_S, f"{label}: placed {errors}")
        print(
            f"\n{self.id()} {label}: {len(heard)} beeps, worst {worst * 1000:.0f} ms, "
            f"median {sorted(abs(e) for e in errors)[len(errors) // 2] * 1000:.0f} ms"
        )

    def play(self, session, relay):
        url = self.provider.url
        session.play(url, kind=classify(url, False), relay=relay)
        self.assertEqual(session.url, url)  # the title stays the provider's address
        self.assertTrue(wait_until(lambda: session.state() is PlaybackState.PLAYING, 15_000))
        self.assertTrue(wait_until(lambda: session.time_ms() > 1000, 10_000))

    def stop_and_check(self, session, relay):
        ended = []
        session.inputEnded.connect(lambda: ended.append(relay.closed()))
        session.stop()
        self.assertTrue(session.holds_input())
        self.assertTrue(wait_until(lambda: bool(ended), 5_000))
        self.assertEqual(ended, [True])  # told once the relay's connection is closed
        self.assertTrue(wait_until(lambda: self.provider.open_now() == 0, 2_000))

    def test_an_mkv_plays_and_its_captions_read_it_over_one_connection(self):
        relay, session = self.start(".mkv", "video/x-matroska")
        self.play(session, relay)
        at = session.time_ms() / 1000
        heard, errors = self.read_captions(relay, at + 6.0, 6.0)
        self.check_placed(heard, errors, "from the start")
        self.assertTrue(wait_until(lambda: session.time_ms() > 9000, 10_000))
        session.seek(3000)  # back over what it played (libVLC's MKV seeks there exactly)
        self.assertTrue(wait_until(lambda: 2500 <= session.time_ms() <= 5000, 5_000))
        moved = session.time_ms()
        self.assertTrue(wait_until(lambda: session.time_ms() > moved + 1000, 5_000))
        heard, errors = self.read_captions(relay, session.time_ms() / 1000 + 6.0, 5.0)
        self.check_placed(heard, errors, "after a seek back")
        self.stop_and_check(session, relay)

    def test_an_mp4_with_its_moov_at_the_end_plays_seeks_and_is_read(self):
        relay, session = self.start(".mp4", "video/mp4")
        self.play(session, relay)
        at = session.time_ms() / 1000
        heard, errors = self.read_captions(relay, at + 6.0, 6.0)
        self.check_placed(heard, errors, "from the start")
        connections = self.provider.film_asks()
        read = relay.stats["bytes"]
        session.seek(25_000)  # far ahead, past what the relay read: the upstream moves
        self.assertTrue(wait_until(lambda: 24_000 <= session.time_ms() <= 28_000, 8_000))
        moved = session.time_ms()
        self.assertTrue(wait_until(lambda: session.time_ms() > moved + 1000, 5_000))
        heard, errors = self.read_captions(relay, session.time_ms() / 1000 + 6.0, 5.0)
        self.check_placed(heard, errors, "after a seek ahead")
        print(
            f"{self.id()}: the provider was asked {len(self.provider.film_asks())} times "
            f"({len(connections)} before the seek, {read} bytes read); relay: {relay.stats}"
        )
        self.stop_and_check(session, relay)

    def test_ai_captions_on_during_a_paused_film_open_it_again_where_it_is(self):
        self.provider = FilmProvider(
            self.films[".mp4"].read_bytes(), extension=".mp4", content_type="video/mp4", rate=RATE
        )
        self.addCleanup(self.provider.close)
        session = PlaybackSession(INSTANCE, poll_ms=50, open_timeout_ms=15_000)
        self.addCleanup(self.release, session)
        url = self.provider.url
        session.play(url, kind=classify(url, False))  # straight from the provider
        self.assertTrue(wait_until(lambda: session.time_ms() > 2000, 15_000))
        session.set_paused(True)
        self.assertTrue(wait_until(lambda: session.state() is PlaybackState.PAUSED, 5_000))
        where = session.time_ms()
        # libVLC's own open of an MP4 asks for its moov at the end while its first connection
        # is still open (measured with 3.0.20): only the relay's are counted from here.
        self.provider.overlaps.clear()
        relay = FilmRelay(url, user_agent="VLC/3.0.20")
        relay.start()
        self.addCleanup(relay.wait_closed, 3.0)
        self.addCleanup(relay.stop)
        self.assertTrue(session.play_through(relay))
        self.assertIs(session.relay(), relay)
        self.assertEqual(session.url, url)
        self.assertTrue(wait_until(lambda: session.placed(), 15_000))
        self.assertTrue(wait_until(lambda: session.state() is PlaybackState.PAUSED, 5_000))
        self.assertLess(abs(session.time_ms() - where), 1500)
        self.assertGreater(relay.stats["bytes"], 0)  # it reads through the relay now
        self.stop_and_check(session, relay)


if __name__ == "__main__":
    unittest.main()
