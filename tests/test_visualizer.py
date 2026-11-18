"""The audio screen's Stage bars (ui/visualizer.py, services/visualizer/helper.py): read from the
song's own sound, by a helper process far faster than it plays, or by the AI captions' film run
when they read the same file (one decode), and shown at the player's position: a pause holds
them, a seek reads on, a live channel has none, and reduced motion shows the song's average,
still. The helper's own legs decode songs ffmpeg makes, with QiTV's libVLC; the visualizer's
run over fakes (the screen, the player, the captions, the helper)."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Optional
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from motion_fixtures import pin_motion
from test_tv_controller_audio import NO_PICTURE_S, AudioCase

from services.levels import BANDS, FRAME_MS, band_of, unpack
from services.playback import create_instance
from services.visualizer import helper as helper_module
from services.visualizer.helper import FileLevels, LevelsHelper
from ui.overlays.audio_screen import AudioScreen
from ui.visualizer import (
    ASK_MS,
    FEED_MS,
    HOLD_MS,
    KEEP_BEHIND_FRAMES,
    RESTING,
    STEER_MS,
    STILL_FRAMES,
    TRIM_FRAMES,
    FrameStore,
    PlayClock,
    Visualizer,
)

FFMPEG = shutil.which("ffmpeg")
INSTANCE = create_instance(["--vout=dummy", "--aout=dummy", "--quiet"])
PATH = "/music/Tidewick Isle/01 Low Tide.mp3"
LOCATION = "file:///music/Harbour%20Lights/01%20Low%20Tide.mp3"
OTHER = "file:///music/Harbour%20Lights/02%20Gulls.mp3"


def frames(first: int, count: int) -> bytes:
    """`count` frames from index `first`, each band holding its frame's index (mod 251)."""
    return b"".join(bytes([k % 251] * BANDS) for k in range(first, first + count))


def frame_at(ms: int) -> int:
    """What the bars show at `ms` of the song, read from `frames`."""
    return ms // FRAME_MS % 251


def ffmpeg(*args: str) -> None:
    subprocess.run([str(FFMPEG), "-v", "error", "-y", *args], check=True, timeout=120)


# ---- the helper process (libVLC, songs ffmpeg makes) ------------------------------------------


@unittest.skipIf(INSTANCE is None, "libVLC is not available")
@unittest.skipIf(FFMPEG is None, "ffmpeg makes the songs")
class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="qitv-levels-"))
        cls.tone = cls.folder / "tone.mp3"  # 60 s at 1 kHz
        ffmpeg(
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=60:sample_rate=44100",
            "-ac", "2", "-b:a", "192k", str(cls.tone),
        )  # fmt: skip
        # 20 s of silence, then 10 s of a 1 kHz tone: where a start lands shows
        steps = (
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=20",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=10:sample_rate=44100",
            "-filter_complex", "[1]aformat=channel_layouts=stereo[t];[0][t]concat=n=2:v=0:a=1",
        )  # fmt: skip
        cls.stepped = {
            "flac": cls.folder / "step.flac",
            "vbr mp3": cls.folder / "step.mp3",
            "ogg": cls.folder / "step.ogg",
        }
        ffmpeg(*steps, str(cls.stepped["flac"]))
        ffmpeg(*steps, "-q:a", "4", str(cls.stepped["vbr mp3"]))
        ffmpeg(*steps, "-c:a", "libvorbis", str(cls.stepped["ogg"]))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def read(self, path, from_ms=0, until_ms=None, timeout=60.0):
        """The helper's chunks of `path` from `from_ms`, until it says it ended; and the time
        it took from its start (its spawn and libVLC's too)."""
        heard: queue.Queue = queue.Queue()
        helper = LevelsHelper(str(path), heard.put)
        began = time.monotonic()
        helper.start()
        helper.decode(from_ms, until_ms)
        chunks, ended = [], None
        while ended is None and time.monotonic() - began < timeout:
            try:
                message = heard.get(timeout=0.5)
            except queue.Empty:
                continue
            if message[0] == "levels":
                chunks.append(message)
            elif message[0] == "ended":
                ended = message
            else:
                self.fail(message)
        took = time.monotonic() - began
        helper.stop()
        self.assertTrue(helper.wait(5.0))
        self.assertIsNotNone(ended, "it never said it ended")
        return chunks, ended, took

    @staticmethod
    def frames_of(chunks):
        out = []
        for _, start_ms, frame_ms, blob in chunks:
            for i in range(len(blob) // BANDS):
                out.append((start_ms + i * frame_ms, unpack(blob, i)))
        return out

    def test_the_helper_decodes_a_file_faster_than_real_time(self):
        chunks, ended, took = self.read(self.tone)
        self.assertEqual(ended[2], "end")
        read = self.frames_of(chunks)
        self.assertAlmostEqual(len(read) * FRAME_MS / 1000.0, 60.0, delta=0.5)
        self.assertTrue(all(frame == FRAME_MS for _, _, frame, _ in chunks))
        middle = read[len(read) // 2][1]
        self.assertEqual(max(range(BANDS), key=middle.__getitem__), band_of(1000.0))
        self.assertGreaterEqual(60.0 / took, 5.0)  # (40x on hel)

    def test_the_helper_reads_from_its_start_where_the_sound_is(self):
        # :start-time lands seconds off in libVLC's stream output for these: read from the
        # file's start, the levels from 17.5 s on
        for kind, path in self.stepped.items():
            with self.subTest(kind):
                chunks, ended, _ = self.read(path, from_ms=17_500)
                read = self.frames_of(chunks)
                self.assertAlmostEqual(read[0][0], 17_500, delta=FRAME_MS)
                onset = next(at for at, frame in read if frame[band_of(1000.0)] > 0.5)
                self.assertAlmostEqual(onset, 20_000, delta=2 * FRAME_MS)
                self.assertAlmostEqual(ended[1], 30_000, delta=150)

    def test_the_helper_reads_no_further_than_asked(self):
        # In this process (the helper's own reader): AHEAD_S past the player, then on as it
        # moves; to an `until`, which it says once, and no further
        sent: list = []
        levels = FileLevels(str(self.tone), sent.append)
        self.addCleanup(levels.close)

        def read_to():
            ends = [m[1] + len(m[3]) // BANDS * m[2] for m in sent if m[0] == "levels"]
            return max(ends, default=0)

        def settle(timeout=10.0):
            last, began = -1, time.monotonic()
            while not read_to() and time.monotonic() - began < 15.0:
                time.sleep(0.05)  # (libVLC's start: slower on a busy runner)
            while time.monotonic() - began < timeout:
                levels.poll()
                time.sleep(0.3)
                now = read_to()
                if now == last:
                    return now
                last = now
            return last

        with mock.patch.object(helper_module, "AHEAD_S", 5.0):
            levels.decode(0, None)
            self.assertLessEqual(settle(), 5_000 + FRAME_MS)
            self.assertGreater(read_to(), 3_000)
            levels.viewer(20_000)  # the player moved on
            self.assertGreater(settle(), 22_000)
            levels.decode(30_000, 32_000)
            settle()
            self.assertLessEqual(read_to(), 32_000 + FRAME_MS)
            self.assertGreaterEqual(read_to(), 32_000 - 2 * FRAME_MS)
            levels.poll()
            self.assertEqual([m for m in sent if m[0] == "ended"], [("ended", 32_000, "until")])
            levels.decode(5_000, None)  # behind where it read: from the file's start again
            settle()
            later = [m for m in sent if m[0] == "levels" and 5_000 <= m[1] < 6_000]
            self.assertTrue(later)

    def test_a_stretch_asked_after_one_ends_joins_it_frame_for_frame(self):
        # The stretch before the captions' levels begin is read in two steps (to their run's
        # start, then on to where their levels begin): no frame lost where they meet, nothing
        # read twice, whatever libVLC's block size (an mp3's 26 ms, a flac's 104 ms)
        for kind, path in (("mp3", self.tone), ("flac", self.stepped["flac"])):
            with self.subTest(kind):
                sent: list = []
                levels = FileLevels(str(path), sent.append)
                self.addCleanup(levels.close)

                def ended(count):
                    began = time.monotonic()
                    while time.monotonic() - began < 15.0:
                        levels.poll()
                        if len([m for m in sent if m[0] == "ended"]) >= count:
                            return True
                        time.sleep(0.05)
                    return False

                levels.decode(0, 6_000)
                self.assertTrue(ended(1))
                levels.decode(6_000, 8_220)
                self.assertTrue(ended(2))
                indices = [
                    start // FRAME_MS + i
                    for kind_, start, _, blob in (m for m in sent if m[0] == "levels")
                    for i in range(len(blob) // BANDS)
                ]
                self.assertEqual(indices, list(range(8_220 // FRAME_MS)))
                self.assertEqual(
                    [m for m in sent if m[0] == "ended"],
                    [("ended", 6_000, "until"), ("ended", 8_220, "until")],
                )

    def test_one_libvlc_for_the_helper_and_no_restart_for_what_it_holds(self):
        sent: list = []
        levels = FileLevels(str(self.tone), sent.append)
        self.addCleanup(levels.close)

        def sent_to():
            return max((m[1] for m in sent if m[0] == "levels"), default=0)

        with mock.patch.object(helper_module, "AHEAD_S", 5.0):
            levels.decode(0, None)
            began = time.monotonic()
            while sent_to() < 3_000 and time.monotonic() - began < 15.0:
                time.sleep(0.05)
            time.sleep(0.5)  # (it waits at 5 s, its last frames read but not sent)
            instance, player = levels._instance, levels._player
            held = levels._stream.unsent_from_s()
            self.assertIsNotNone(held)
            levels.decode(round(held * 1000) + FRAME_MS, None)  # what it holds covers it
            self.assertIs(levels._player, player)  # on from there, not from the start again
            levels.decode(1_000, None)  # behind it: from the start again, the same libVLC
            self.assertIsNot(levels._player, player)
            self.assertIs(levels._instance, instance)

    def test_the_helper_quits_when_its_pipe_closes(self):
        heard: queue.Queue = queue.Queue()
        helper = LevelsHelper(str(self.tone), heard.put)
        helper.start()
        helper.decode(0)
        self.assertEqual(heard.get(timeout=30)[0], "levels")
        pid = helper.pid
        began = time.monotonic()
        helper.stop()
        self.assertLess(time.monotonic() - began, 0.2)  # nothing waits for it here
        self.assertTrue(helper.wait(5.0))
        self.assertEqual(helper.exitcode, 0)  # it quit by itself, not killed
        self.assertNotIn(pid, [child.pid for child in multiprocessing.active_children()])


class HelperTroubleTests(unittest.TestCase):
    """A helper that dies or fails says so (its owner logs it), and a block's trouble is said
    once, not for every block."""

    def hear(self, target):
        import visualizer_fixtures

        heard: queue.Queue = queue.Queue()
        helper = LevelsHelper(
            "/music/none.mp3", heard.put, target=getattr(visualizer_fixtures, target)
        )
        self.addCleanup(helper.stop)
        with self.assertLogs("services.visualizer.helper", "WARNING") as logs:
            helper.start()
            helper.decode(0)
            message = heard.get(timeout=30)
            self.assertTrue(helper.wait(5.0))
        return message, " ".join(logs.output), helper

    def test_a_helper_that_dies_says_so(self):
        message, logged, helper = self.hear("die_at_once")
        self.assertEqual(message, ("failed", "gone"))
        self.assertEqual(helper.exitcode, 3)
        self.assertIn("ended by itself (exit code 3)", logged)

    def test_a_helper_that_fails_says_why(self):
        message, logged, _ = self.hear("fail_inside")
        self.assertEqual(message, ("failed", "RuntimeError"))
        self.assertIn("the helper failed: RuntimeError", logged)
        # Item 12: the error's name only; its text may name the file
        self.assertNotIn("no libVLC here", logged)
        self.assertNotIn("none.mp3", logged)

    def test_a_blocks_trouble_is_said_once(self):
        import ctypes

        levels = FileLevels("/music/none.mp3", lambda message: None, vlc_module=SimpleNamespace())
        _, _, prerender, postrender = levels._callbacks
        with self.assertLogs("services.visualizer.helper", "WARNING") as logs:
            for _ in range(50):  # stereo at 44.1 kHz: not what it asked for
                at = (ctypes.c_void_p * 1)()
                prerender(None, at, 8)
                postrender(None, at[0], 2, 44_100, 2, 16, 8, 0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("2 channels at 44100 Hz", logs.output[0])


# ---- the visualizer (fakes) -----------------------------------------------------------------


class FakeStage(QObject):
    """The audio screen as the visualizer uses it (checked against AudioScreen below)."""

    stageMoving = Signal(bool)
    stageShown = Signal(bool)

    def __init__(self):
        super().__init__()
        self.shown = self.moving = self.plays = False
        self.levels: list = []

    def takes_words(self):
        return self.shown

    def stage_moving(self):
        return self.moving

    def playing(self):
        return self.plays

    def set_levels(self, bands):
        self.levels.append(None if bands is None else list(bands))

    def play(self, playing=True):
        """The Stage shows, playing (or paused)."""
        self.shown, self.moving, self.plays = True, playing, playing
        self.stageShown.emit(True)
        self.stageMoving.emit(playing)

    def leave(self):
        self.shown = self.moving = False
        self.stageMoving.emit(False)
        self.stageShown.emit(False)

    def frame(self):
        """The frame the bars show (each band of a test frame holds its index), "rest" or None."""
        last = self.levels[-1] if self.levels else None
        if last is None:
            return None
        if last == RESTING:
            return "rest"
        return round(last[0] * 255)


class FakeCaptions(QObject):
    levels = Signal(str, int, int, object)
    soundRead = Signal()

    def __init__(self):
        super().__init__()
        self.reading: Optional[tuple[str, float]] = None  # (location, from second)
        self.wanted: list = []

    def reads(self, location, from_s, *, tell=True):
        self.reading = None if location is None else (location, from_s)
        if tell:
            self.soundRead.emit()

    def levels_from(self, location):
        if self.reading is not None and self.reading[0] == location:
            return self.reading[1]
        return None

    def want_levels(self, on):
        self.wanted.append(on)


class FakeHelper:
    def __init__(self, location, on_message):
        self.location, self.on_message = location, on_message
        self.started = self.stopped = False
        self.asks: list = []
        self.viewers: list = []

    def start(self):
        self.started = True

    def decode(self, from_ms, until_ms=None):
        self.asks.append((from_ms, until_ms))

    def viewer(self, ms):
        self.viewers.append(ms)

    def stop(self):
        self.stopped = True

    def say(self, *message):
        self.on_message(tuple(message))

    def chunk(self, first, count):
        self.say("levels", first * FRAME_MS, FRAME_MS, frames(first, count))


class VisualizerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        pin_motion(self, reduced=False)
        self.now = 1000.0
        self.screen = FakeStage()
        self.session = SimpleNamespace(ms=0, time_ms=lambda: self.session.ms)
        self.captions = FakeCaptions()
        self.helpers: list[FakeHelper] = []

        def make(location, on_message):
            made = FakeHelper(location, on_message)
            self.helpers.append(made)
            return made

        self.vis = Visualizer(
            self.screen, lambda: self.session, self.captions, helper=make, clock=lambda: self.now
        )
        self.addCleanup(self.vis.shutdown)

    def play_to(self, ms, step=33):
        """The player plays on to `ms`, the bars ticking as their timer would."""
        while self.session.ms < ms:
            self.session.ms = min(ms, self.session.ms + step)
            self.now += step / 1000.0
            self.vis._tick()

    def seek(self, ms):
        self.session.ms = ms
        self.now += STEER_MS / 1000.0
        self.vis._tick()

    def helper(self):
        self.assertEqual(len(self.helpers), 1)
        return self.helpers[0]


class OneDecodeTests(VisualizerCase):
    def test_no_helper_while_captions_decode_the_same_file(self):
        self.captions.reads(LOCATION, 4.0)  # their film run reads this file from 4 s on
        self.vis.follow(PATH, LOCATION)
        self.assertEqual(self.captions.wanted, [True])  # asked to send its levels
        self.captions.levels.emit(LOCATION, 4_000, FRAME_MS, frames(133, 400))  # 4–16 s
        self.captions.levels.emit(OTHER, 4_000, FRAME_MS, bytes([250] * BANDS * 400))
        self.session.ms = 10_000
        self.screen.play()
        self.play_to(12_000)
        self.assertEqual(self.helpers, [])
        self.assertEqual(self.screen.frame(), frame_at(12_000))  # theirs, not the other's
        self.play_to(20_000)  # past what they read so far: they read on, no helper
        self.assertEqual(self.helpers, [])
        self.assertEqual(self.screen.frame(), "rest")

    def test_the_stretch_before_the_captions_sound_is_read_up_to_where_they_start(self):
        # Their run plays its sound from 6 s past the picture, and its levels begin a little
        # later still (its player finds its clock: 2.2 s on hel). The song's start (and after
        # a seek, where it lands) up to there is read by a helper that stops there, so the bars
        # never wait for them and no sound is read twice.
        self.captions.reads(LOCATION, 6.0)
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        helper = self.helper()
        self.assertEqual(helper.asks, [(0, 6_000)])
        helper.chunk(0, 200)
        helper.say("ended", 6_000, "until")
        self.play_to(300)
        self.assertFalse(helper.stopped)  # it waits there: where do theirs begin?
        self.captions.levels.emit(LOCATION, 8_220, FRAME_MS, frames(274, 300))
        self.play_to(600)
        self.assertEqual(helper.asks[-1], (6_000, 8_220))  # on to there, no further
        helper.chunk(200, 74)
        helper.say("ended", 8_220, "until")
        self.play_to(900)
        self.assertTrue(helper.stopped)  # what it read joins theirs: it ends
        self.assertIsNone(self.vis.helper())
        self.play_to(5_000)
        self.assertEqual(self.screen.frame(), frame_at(5_000))
        self.play_to(9_000)
        self.assertEqual(self.screen.frame(), frame_at(9_000))
        self.assertEqual(len(self.helpers), 1)
        # a seek: they read again from 66 s; a helper reads from 60 s up to it
        self.seek(60_000)
        self.captions.reads(LOCATION, 66.0)
        second = self.helpers[-1]
        self.assertEqual(len(self.helpers), 2)
        self.assertEqual(second.asks[-1], (60_000, 66_000))
        for made in self.helpers:  # never into what they read
            for _, until in made.asks:
                self.assertIsNotNone(until)
        self.assertLessEqual(max(until for _, until in self.helpers[0].asks), 8_220)

    def test_a_captions_run_that_sends_no_levels_lets_the_helper_read_on(self):
        self.captions.reads(LOCATION, 6.0)
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        helper = self.helper()
        helper.chunk(0, 200)
        helper.say("ended", 6_000, "until")
        self.play_to(8_500)
        self.assertEqual(helper.asks, [(0, 6_000)])  # (theirs may still come)
        self.play_to(9_500)  # 3 s past their start, none came: it reads on, as without them
        from_ms, until = helper.asks[-1]
        self.assertIsNone(until)
        self.assertEqual(from_ms, 9_000 + 7 * FRAME_MS)  # (from where the bars are)
        self.assertFalse(helper.stopped)
        helper.chunk(from_ms // FRAME_MS, 200)
        self.play_to(10_000)
        self.assertEqual(self.screen.frame(), frame_at(10_000))

    def test_captions_turned_off_or_on_mid_song_hand_over_where_the_bars_are(self):
        self.captions.reads(LOCATION, 6.0)
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        first = self.helper()
        first.chunk(0, 200)
        first.say("ended", 6_000, "until")
        self.captions.levels.emit(LOCATION, 6_000, FRAME_MS, frames(200, 1134))  # to 40 s
        self.play_to(20_000)
        self.assertTrue(first.stopped)
        # off: a helper reads on from where they had read to, the bars uninterrupted
        self.captions.reads(None, 0.0)
        helper = self.helpers[-1]
        self.assertEqual(len(self.helpers), 2)
        self.assertEqual(helper.asks, [(40_020, None)])
        self.play_to(21_000)
        self.assertEqual(self.screen.frame(), frame_at(21_000))
        helper.chunk(1334, 2000)  # 40–100 s
        self.play_to(30_000)
        # on again: they read from 36 s; what the helper read reaches past it: it stops once
        # their levels come, and the bars go on uninterrupted
        self.captions.reads(LOCATION, 36.0)
        self.captions.levels.emit(LOCATION, 38_000, FRAME_MS, frames(1266, 64))
        self.play_to(30_300)
        self.assertTrue(helper.stopped)
        self.assertIsNone(self.vis.helper())
        self.play_to(31_000)
        self.assertEqual(self.screen.frame(), frame_at(31_000))
        self.assertEqual(len(self.helpers), 2)


class BarsTests(VisualizerCase):
    def test_pause_freezes_and_seek_reads_on(self):
        self.vis.follow(PATH, LOCATION)
        self.assertEqual(self.screen.frame(), "rest")  # resting bars until its sound is read
        self.assertEqual(self.helpers, [])  # nothing read before the Stage shows
        self.screen.play()
        helper = self.helper()
        self.assertEqual(helper.asks, [(0, None)])
        helper.chunk(0, 334)  # 0–10 s
        self.play_to(1_000)
        self.assertEqual(self.screen.frame(), frame_at(1_000))
        # paused: the feed stops, the bars stay at their frame however long it stays paused
        self.screen.play(False)
        self.assertFalse(self.vis._feed.isActive())
        shown = len(self.screen.levels)
        for _ in range(5):
            self.now += 1.0
            self.vis._slow_tick()
        QTest.qWait(2 * FEED_MS)
        self.assertEqual(len(self.screen.levels), shown)
        self.assertEqual(self.screen.frame(), frame_at(1_000))
        self.screen.play()
        self.assertTrue(self.vis._feed.isActive())
        self.play_to(2_000)
        self.assertEqual(self.screen.frame(), frame_at(2_000))
        # a seek past what is read: the same helper reads from there
        self.now += ASK_MS / 1000.0
        self.seek(50_000)
        self.assertEqual(helper.asks[-1], (50_000 // FRAME_MS * FRAME_MS, None))
        self.assertEqual(len(self.helpers), 1)
        self.assertEqual(self.screen.frame(), frame_at(2_000))  # held a moment ...
        self.play_to(50_000 + HOLD_MS)
        self.assertEqual(self.screen.frame(), "rest")  # ... then resting
        helper.chunk(50_000 // FRAME_MS, 64)
        self.play_to(51_200)
        self.assertEqual(self.screen.frame(), frame_at(51_200))
        # back into what is read: shown at once, and the helper reads on from where that ends
        self.now += ASK_MS / 1000.0
        self.seek(5_000)
        self.assertEqual(self.screen.frame(), frame_at(5_000))
        self.assertEqual(helper.asks[-1], (334 * FRAME_MS, None))
        self.assertEqual(len(self.helpers), 1)

    def test_a_seek_while_paused_shows_its_frame_once_read(self):
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        helper = self.helper()
        helper.chunk(0, 334)  # 0–10 s
        self.play_to(2_000)
        self.screen.play(False)
        self.assertTrue(self.vis._slow.isActive())  # (a look a quarter second, no feed)
        self.assertFalse(self.vis._feed.isActive())
        self.session.ms = 7_000  # dragged on the scrub bar
        self.vis._slow_tick()
        self.assertEqual(self.screen.frame(), frame_at(7_000))
        shown = len(self.screen.levels)
        self.vis._slow_tick()
        self.assertEqual(len(self.screen.levels), shown)  # one paint
        self.now += ASK_MS / 1000.0
        self.session.ms = 50_000  # past what is read: the helper is sent there
        self.vis._slow_tick()
        self.assertEqual(helper.asks[-1], (50_000 // FRAME_MS * FRAME_MS, None))
        self.assertEqual(self.screen.frame(), frame_at(7_000))
        helper.chunk(50_000 // FRAME_MS, 64)
        self.vis._slow_tick()
        self.assertEqual(self.screen.frame(), frame_at(50_000))

    def test_frames_long_behind_the_bars_are_let_go(self):
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        helper = self.helper()
        far = KEEP_BEHIND_FRAMES + TRIM_FRAMES + 600
        helper.chunk(0, far + 100)
        self.seek(far * FRAME_MS)
        self.assertEqual(self.vis._frames.first(), far - KEEP_BEHIND_FRAMES)
        self.assertEqual(self.screen.frame(), frame_at(far * FRAME_MS))
        self.seek((far - 600) * FRAME_MS)  # a short seek back: still held, at once
        self.assertEqual(self.screen.frame(), frame_at((far - 600) * FRAME_MS))

    def test_the_helper_hears_where_the_player_is(self):
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        helper = self.helper()
        helper.chunk(0, 1000)
        self.play_to(5_000)
        self.assertGreaterEqual(len(helper.viewers), 4)
        self.assertAlmostEqual(helper.viewers[-1], 5_000, delta=1_100)

    def test_no_bars_for_radio(self):
        self.vis.follow("", "")
        self.assertIsNone(self.screen.levels[-1])
        self.screen.play()
        self.assertEqual(self.helpers, [])
        self.assertFalse(self.vis._feed.isActive())
        self.assertEqual(self.captions.wanted, [False])
        # a song, then a radio channel: its bars go
        self.vis.follow(PATH, LOCATION)
        self.helper().chunk(0, 100)
        self.vis.follow("", "")
        self.assertTrue(self.helpers[0].stopped)
        self.assertIsNone(self.screen.levels[-1])

    def test_one_helper_at_most_and_a_new_track_ends_it(self):
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        first = self.helper()
        first.chunk(0, 100)
        self.play_to(1_000)
        self.vis.rest()  # the next song is opening on the same screen
        self.assertTrue(first.stopped)
        self.assertEqual(self.screen.frame(), "rest")
        first.chunk(100, 100)  # its late words reach nothing
        self.session.ms = 0
        self.vis.follow("/music/Tidewick Isle/02 Gulls.mp3", OTHER)
        self.assertEqual(len(self.helpers), 2)  # (the Stage shows: read at once)
        second = self.helpers[1]
        self.assertEqual(second.asks, [(0, None)])
        self.play_to(1_500)
        self.assertEqual(self.screen.frame(), "rest")  # not the first song's frames
        self.vis.shutdown()
        self.assertTrue(second.stopped)
        self.assertEqual([h.stopped for h in self.helpers], [True, True])

    def test_a_failed_helper_is_not_started_again_for_the_song(self):
        self.vis.follow(PATH, LOCATION)
        self.screen.play()
        self.helper().say("failed", "gone")
        self.play_to(3_000)
        self.assertEqual(len(self.helpers), 1)
        self.assertEqual(self.screen.frame(), "rest")

    def test_reduced_motion_shows_the_songs_average_still(self):
        pin_motion(self, reduced=True)
        self.vis.follow(PATH, LOCATION)
        self.screen.play(False)  # (the screen doesn't move under reduced motion)
        self.assertTrue(self.vis._slow.isActive())
        self.assertFalse(self.vis._feed.isActive())
        helper = self.helper()  # the song is read: its average needs it
        before = len(self.screen.levels)
        helper.say("levels", 0, FRAME_MS, bytes([51] * BANDS) * (STILL_FRAMES - 1))
        self.assertEqual(len(self.screen.levels), before)  # not enough of it yet
        helper.say("levels", (STILL_FRAMES - 1) * FRAME_MS, FRAME_MS, bytes([153] * BANDS))
        still = self.screen.levels[-1]
        self.assertEqual(len(still), BANDS)
        expected = ((STILL_FRAMES - 1) * 51 + 153) / STILL_FRAMES / 255
        self.assertAlmostEqual(still[0], expected, places=4)
        self.assertTrue(helper.stopped)  # the shape never changes: nothing more is read
        asks = list(helper.asks)
        for _ in range(3):
            self.now += 1.0
            self.session.ms += 1_000
            self.vis._slow_tick()
        self.assertEqual(self.screen.levels[-1], still)
        self.assertEqual((len(self.helpers), helper.asks), (1, asks))


class FrameStoreTests(unittest.TestCase):
    def test_frames_from_either_source_join_where_they_meet(self):
        store = FrameStore()
        store.put(10, frames(10, 5))
        store.put(15, frames(15, 5))  # on from it
        store.put(30, frames(30, 5))
        self.assertEqual(store.gap_from(10), 20)
        self.assertEqual(store.gap_from(25), 25)
        self.assertEqual(store.at(17), bytes([17] * BANDS))
        self.assertIsNone(store.at(22))
        store.put(18, frames(18, 13))  # over both, and between them
        self.assertEqual(store.gap_from(10), 35)
        self.assertEqual(store._starts, [10])
        self.assertEqual(store.count(), 25)
        self.assertEqual(store.at(31), bytes([31] * BANDS))

    def test_frames_before_the_start_are_dropped_and_the_average_is_each_bands(self):
        store = FrameStore()
        store.put(-3, frames(0, 5))  # (a start rounded just before 0)
        self.assertEqual(store._starts, [0])
        self.assertEqual(store.count(), 2)
        self.assertEqual(store.at(0), bytes([3] * BANDS))
        store.put(-9, frames(0, 4))  # wholly before it: nothing
        self.assertEqual(store.count(), 2)
        self.assertEqual(FrameStore().average(), RESTING)
        store.clear()
        store.put(0, bytes(range(BANDS)) + bytes(2 * b for b in range(BANDS)))
        store.put(10, bytes([255] * BANDS))
        average = store.average()
        self.assertEqual(len(average), BANDS)
        for band, value in enumerate(average):
            self.assertAlmostEqual(value, (band + 2 * band + 255) / 3 / 255)

    def test_a_trim_lets_go_of_what_is_before_it(self):
        store = FrameStore()
        store.put(0, frames(0, 100))
        store.put(200, frames(200, 100))
        store.trim(150)
        self.assertEqual((store.first(), store.count()), (200, 100))
        store.trim(250)
        self.assertEqual((store.first(), store.count()), (250, 50))
        self.assertEqual(store.at(250), bytes([250] * BANDS))
        store.trim(1000)
        self.assertIsNone(store.first())


class PlayClockTests(unittest.TestCase):
    def test_the_bars_follow_the_players_time_between_its_steps(self):
        now = [0.0]
        clock = PlayClock(lambda: now[0])
        read, seen = 0, []
        for step in range(60):  # libVLC's time moves every 260 ms; the bars every 33
            now[0] = step * 0.033
            if step * 33 >= read + 260:
                read += 260
            seen.append(clock.at(read))
        self.assertEqual(seen, sorted(seen))  # never back
        gaps = [b - a for a, b in zip(seen, seen[1:])]
        self.assertLess(max(gaps), 70)  # no quarter-second jumps
        self.assertAlmostEqual(seen[-1], 59 * 33, delta=300)
        now[0] += 0.033
        self.assertEqual(clock.at(0), seen[-1])  # a busy player's 0: no reading
        now[0] += 0.033
        self.assertEqual(clock.at(500), 500)  # a seek back
        now[0] += 0.033
        self.assertEqual(clock.at(90_000), 90_000)  # and on


class StageMovingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_the_stage_says_when_its_bars_move(self):
        pin_motion(self, reduced=False)
        host = QWidget()
        self.addCleanup(host.deleteLater)
        host.resize(1280, 720)
        screen = AudioScreen(host)
        screen.resize(1280, 720)
        seen: list = []
        screen.stageMoving.connect(seen.append)
        host.show()
        screen.set_view("stage")
        screen.show()
        self.assertEqual(seen, [])  # not playing
        screen.set_playing(True)
        self.assertEqual(seen, [True])
        self.assertTrue(screen.stage_moving())
        screen.set_playing(False)
        screen.set_playing(True)
        screen.set_view("cover")
        screen.set_view("stage")
        screen.hide()
        self.assertEqual(seen, [True, False, True, False, True, False])
        pin_motion(self, reduced=True)
        screen.show()
        self.assertFalse(screen.stage_moving())
        names = ("stage_moving", "takes_words", "playing", "set_levels", "stageMoving")
        for name in names + ("stageShown",):
            self.assertTrue(hasattr(screen, name), name)  # (FakeStage's API)


class CaptionsLevelsTests(unittest.TestCase):
    """The captions process sends a film run's band levels while asked; the controller passes
    on only its current run's, and says where that run reads from."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_a_films_run_sends_its_levels_while_asked(self):
        from array import array
        import math

        from services.captions import worker

        sent: list = []
        link: Any = SimpleNamespace(send=sent.append)
        model = SimpleNamespace(is_speech=lambda w: False, is_voiced=lambda w: False)
        levels_on = [False]
        run = worker._FilmRun(
            link, model, 7, "file:///m/a.mp3", 6.0, None, "", [0.0, 1.0], levels_on=levels_on
        )
        tone = array("f", [0.5 * math.sin(2 * math.pi * 1000 * n / 16000) for n in range(48000)])
        run._take(6.0, tone[:16000])
        self.assertEqual([m for m in sent if m[0] == "levels"], [])  # not asked
        levels_on[0] = True
        run._take(7.0, tone[16000:])
        chunks = [m for m in sent if m[0] == "levels"]
        self.assertTrue(chunks)
        self.assertEqual(chunks[0][:4], ("levels", 7, 7_000, FRAME_MS))
        run.stop()

    def test_the_process_turns_its_levels_on_and_off(self):
        from services.captions import worker

        made: list = []

        def film(*args, **kwargs):
            made.append(kwargs["levels_on"])
            return SimpleNamespace(start=lambda: None, stop=lambda: None)

        script = iter(
            [
                ("load", {}),
                ("levels", True),
                ("film", 3, "file:///m/a.mp3", 6.0, None, "", ()),
                ("quit",),
            ]
        )
        seen: list = []

        def recv():
            message = next(script)
            if message[0] == "quit":
                seen.append(list(made[0]))
            return message

        conn: Any = SimpleNamespace(recv=recv, send=lambda m: None)
        with (
            mock.patch.object(worker, "_Model", lambda spec: SimpleNamespace()),
            mock.patch.object(worker, "_FilmRun", film),
        ):
            worker._serve(worker._Link(conn))
        self.assertEqual(seen, [[True]])

    def test_the_gui_hears_a_films_levels(self):
        from services.captions.process import CaptionsProcess

        process = CaptionsProcess()
        heard: list = []
        process.levels.connect(lambda *a: heard.append(a))
        process._supervisor, process._serial = object(), 4  # (a running child's)
        process._on_message(4, ("levels", 2, 6_000, FRAME_MS, b"\x01" * BANDS))
        process._on_message(3, ("levels", 2, 9_000, FRAME_MS, b"\x01" * BANDS))  # a gone one's
        process._supervisor = None
        self.assertEqual(heard, [(2, 6_000, FRAME_MS, b"\x01" * BANDS)])

    def test_the_controller_passes_on_its_runs_levels_and_where_it_reads(self):
        from test_captions_pipeline import _Controller, _Session, _Target

        from ui.captions_controller import FILM_AHEAD_S, CaptionsController

        window = QWidget()
        self.addCleanup(window.deleteLater)
        window.controller = _Controller(_Target(live=False, local=True))
        window.session = _Session("file:///m/a.mp3", 30_000)
        captions = CaptionsController(window)
        process = captions._process
        sent: list = []
        process.send = lambda *m: sent.append(m)
        process.running = lambda: True
        captions._config_get = lambda name, default: default
        captions._enabled = captions._ready = True
        heard: list = []
        captions.levels.connect(lambda *a: heard.append(a))
        moved: list = []
        captions.soundRead.connect(lambda: moved.append(True))
        captions.want_levels(True)
        self.assertEqual(sent, [("levels", True)])
        self.assertIsNone(captions.levels_from("file:///m/a.mp3"))  # no run reads it yet
        captions._start_run()
        film = [m for m in sent if m[0] == "film"]
        self.assertEqual(len(film), 1)
        self.assertEqual(sent[sent.index(film[0]) - 1], ("levels", True))  # said first
        self.assertEqual(moved, [True])
        self.assertEqual(captions.levels_from("file:///m/a.mp3"), 30.0 + FILM_AHEAD_S)
        self.assertIsNone(captions.levels_from("file:///m/b.mp3"))
        run = film[0][1]
        captions._on_levels(run, 36_000, FRAME_MS, b"\x02" * BANDS)
        captions._on_levels(run - 1, 36_000, FRAME_MS, b"\x03" * BANDS)  # the run before's
        self.assertEqual(heard, [("file:///m/a.mp3", 36_000, FRAME_MS, b"\x02" * BANDS)])
        captions._disable()
        self.assertIsNone(captions.levels_from("file:///m/a.mp3"))
        self.assertEqual(moved, [True, True])


class ControllerTests(AudioCase):
    """The controller tells the bars what plays on the audio screen."""

    def setUp(self):
        super().setUp()
        self.window.visualizer = mock.Mock()
        self.followed = self.window.visualizer.follow

    def test_a_song_has_bars_and_a_radio_channel_none(self):
        song = self.folder / "01 Arn.mp3"
        self.open(song)
        path, location = self.followed.call_args.args
        self.assertEqual(path, str(song))
        self.assertEqual(location, self.session.url)
        self.open(self.folder / "02 Pell.mp3")  # a song after a song: its bars rest first
        self.window.visualizer.rest.assert_called()
        self.assertEqual(self.followed.call_args.args[0], str(self.folder / "02 Pell.mp3"))
        self.controller.stop()
        self.assertEqual(self.followed.call_args.args, ("", ""))
        self.play(0)
        self.session.video = False
        self.session.stateChanged.emit("playing")
        self.clock.now += NO_PICTURE_S + 0.1
        self.controller._sound_watch()
        self.assertTrue(self.screen.visible)
        self.assertEqual(self.followed.call_args.args, ("", ""))  # no bars for radio

    def test_a_local_hls_stream_on_the_screen_gets_no_helper(self):
        # A .m3u8 opened from disk plays as one stream: a helper would open it a second time
        self.open(self.folder / "concert.m3u8", video=False)
        self.assertTrue(self.screen.visible)
        self.assertEqual(self.followed.call_args.args, ("", ""))


if __name__ == "__main__":
    unittest.main()
