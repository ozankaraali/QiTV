"""The sound's band levels for the audio screen's Stage (services/levels.py): 60 log-spaced bands
from 40 Hz to 8 kHz, a frame every 30 ms, each 0–1 under a slow running peak. Generated tones,
chords and silence; no libVLC."""

from array import array
import math
import random
import time
import unittest

from services.levels import (
    BANDS,
    CHUNK_FRAMES,
    FRAME_MS,
    Levels,
    LevelStream,
    band_edges,
    band_levels,
    band_of,
    pack,
    unpack,
)

RATE = 16_000


def tone(hz, seconds, amplitude=0.5, rate=RATE):
    return array(
        "f", [amplitude * math.sin(2 * math.pi * hz * n / rate) for n in range(int(seconds * rate))]
    )


def silence(seconds, rate=RATE):
    return array("f", bytes(4 * int(seconds * rate)))


def loudest(frame):
    return max(range(BANDS), key=frame.__getitem__)


class BandLevelsTests(unittest.TestCase):
    def test_the_bands_run_from_40_hz_to_8_khz_in_steps_of_one_ratio(self):
        edges = band_edges()
        self.assertEqual(len(edges), BANDS + 1)
        self.assertAlmostEqual(edges[0], 40.0)
        self.assertAlmostEqual(edges[-1], 8000.0)
        ratios = {round(b / a, 9) for a, b in zip(edges, edges[1:])}
        self.assertEqual(len(ratios), 1)

    def test_bands_of_a_sine_peak_in_its_band(self):
        # 62.5 and 125 Hz: bands narrower than a bin (read between two); 440 Hz off a bin
        for hz in (62.5, 125.0, 440.0, 1000.0, 2500.0, 4000.0, 7000.0):
            frame = band_levels(tone(hz, 1.0), RATE)[-5]  # held, away from the padded end
            peak = loudest(frame)
            self.assertEqual(peak, band_of(hz), hz)
            self.assertGreater(frame[peak], 0.9, hz)
            far = [frame[i] for i in range(BANDS) if abs(i - peak) > 6]
            self.assertLess(max(far), 0.5, hz)

    def test_a_chord_lights_each_of_its_notes(self):
        notes = (220.0, 277.18, 329.63)  # A major, an octave down
        sound = [sum(v) / 3 for v in zip(*(tone(hz, 1.0) for hz in notes))]
        frame = band_levels(array("f", sound), RATE)[-5]
        for hz in notes:
            self.assertGreater(max(frame[band_of(hz) - 1 : band_of(hz) + 2]), 0.85, hz)
        self.assertLess(max(frame[band_of(1500.0) :]), 0.4)  # nothing up high

    def test_silence_is_low(self):
        for frame in band_levels(silence(1.0), RATE):
            self.assertEqual(max(frame), 0.0)
        rng = random.Random(7)
        hiss = array("h", [rng.choice((-1, 0, 1)) for _ in range(RATE)])  # a 16-bit file's dither
        for frame in band_levels(hiss, RATE):
            self.assertLess(max(frame), 0.1)
        # after a loud tone, the bars fall within half a second, and stay down
        frames = band_levels(array("f", list(tone(1000.0, 1.0)) + list(silence(1.0))), RATE)
        after = frames[len(frames) // 2 + round(500 / FRAME_MS) :]
        self.assertLess(max(max(frame) for frame in after), 0.05)

    def test_a_quiet_song_still_moves(self):
        frame = band_levels(tone(1000.0, 1.0, amplitude=0.01), RATE)[-5]  # -40 dB
        self.assertGreater(frame[loudest(frame)], 0.9)
        quieter = band_levels(tone(1000.0, 1.0, amplitude=0.001), RATE)[-5]  # -60 dB
        self.assertGreater(quieter[loudest(quieter)], 0.4)
        # a loud stretch, then a quiet one: the peak comes down to it, slowly
        frames = band_levels(
            array("f", list(tone(1000.0, 1.0, 0.5)) + list(tone(1000.0, 12.0, 0.02))), RATE
        )
        soon, later = frames[40][band_of(1000.0)], frames[-5][band_of(1000.0)]
        self.assertLess(soon, later)
        self.assertGreater(later, 0.9)

    def test_frames_every_30_ms(self):
        self.assertEqual(FRAME_MS, 30)
        self.assertEqual(len(band_levels(tone(440.0, 3.0), RATE)), 100)
        self.assertEqual(len(band_levels(tone(440.0, 1.0), RATE)), 33)
        self.assertEqual(len(band_levels(silence(0.02), RATE)), 0)
        for frame in band_levels(tone(440.0, 0.5), RATE):
            self.assertEqual(len(frame), BANDS)
            self.assertTrue(all(0.0 <= v <= 1.0 for v in frame))
        # signed 16-bit samples read the same as floats
        ints = array("h", [round(v * 32767) for v in tone(1000.0, 0.5)])
        a, b = band_levels(ints, RATE), band_levels(tone(1000.0, 0.5), RATE)
        self.assertEqual(len(a), len(b))
        self.assertLess(max(abs(x - y) for fa, fb in zip(a, b) for x, y in zip(fa, fb)), 0.01)

    def test_a_stream_sends_its_frames_in_chunks_placed_in_time(self):
        sent = []
        stream = LevelStream(lambda start, frame, blob: sent.append((start, frame, blob)))
        sound = tone(1000.0, 5.0)
        for i in range(0, len(sound), 371):  # blocks as libVLC hands them over
            stream.push(10.0 + i / RATE, sound[i : i + 371])
        stream.flush()
        self.assertTrue(all(frame == FRAME_MS for _, frame, _ in sent))
        self.assertEqual(sent[0][0], 10_000)
        for (start, _, blob), (after, _, _) in zip(sent, sent[1:]):
            self.assertEqual(len(blob), CHUNK_FRAMES * BANDS)
            self.assertEqual(after, start + CHUNK_FRAMES * FRAME_MS)
        frames = [unpack(blob, i) for _, _, blob in sent for i in range(len(blob) // BANDS)]
        self.assertEqual(len(frames), 167)  # 5 s: the last 30 ms two-thirds sound, so a frame
        whole = [unpack(pack([frame])) for frame in band_levels(sound, RATE)]
        self.assertEqual(len(whole), 166)  # (read whole: only frames of a full 30 ms)
        self.assertEqual(frames[:166], whole)  # pushed in blocks, read as one: the same

    def test_sound_from_elsewhere_starts_a_chunk_where_it_is(self):
        sent = []
        stream = LevelStream(lambda start, frame, blob: sent.append((start, len(blob) // BANDS)))
        stream.push(0.0, tone(440.0, 1.0))
        stream.push(1.01, tone(440.0, 1.0))  # 10 ms late: the same stretch, the gap silent
        stream.push(40.0, tone(440.0, 1.0))  # a seek
        stream.flush()
        self.assertEqual(sent[0][0], 0)
        self.assertEqual(sum(n for start, n in sent if start < 40_000), 67)  # 2.01 s
        self.assertEqual([start for start, _ in sent if start >= 40_000][0], 40_000)

    def test_a_streams_peak_carries_across_its_pushes(self):
        levels = Levels(RATE)
        loud = levels.push(tone(1000.0, 1.0, 0.5))
        quiet = levels.push(tone(1000.0, 0.3, 0.005))  # just after: the peak is the loud one's
        self.assertGreater(loud[-1][band_of(1000.0)], 0.9)
        self.assertLess(quiet[-1][band_of(1000.0)], 0.5)

    def test_levels_keep_up_faster_than_real_time(self):
        # (57x on hel measured, unloaded; this floor only catches a reader gone wrong, on a
        # loaded runner too)
        rng = random.Random(3)
        sound = array("h", [rng.randint(-12000, 12000) for _ in range(3 * RATE)])
        began = time.process_time()
        band_levels(sound, RATE)
        speed = 3.0 / max(1e-6, time.process_time() - began)
        self.assertGreater(speed, 5.0)


if __name__ == "__main__":
    unittest.main()
