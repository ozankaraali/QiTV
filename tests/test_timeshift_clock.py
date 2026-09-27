"""Real compressed packets exercise clock recovery, not a particular clock helper."""

from collections import Counter, defaultdict
from contextlib import contextmanager
from fractions import Fraction
import io
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch

import av

from services.timeshift import _RecordingEngine


def make_clock_stream(seconds=24):
    """MPEG-2 B-frames and two native MP2 tracks, with roughly two-second GOPs."""
    destination = io.BytesIO()
    with av.open(destination, "w", format="mpegts") as output:
        video = output.add_stream("mpeg2video", rate=25)
        video.width = 96
        video.height = 64
        video.pix_fmt = "yuv420p"
        video.codec_context.gop_size = 54
        video.codec_context.max_b_frames = 2
        video.codec_context.options = {"sc_threshold": "1000000000"}
        audio = []
        for language in ("eng", "fra"):
            stream = output.add_stream("mp2", rate=48000)
            stream.layout = "mono"
            stream.bit_rate = 64000
            stream.metadata["language"] = language
            audio.append(stream)
        sample = 0
        for index in range(seconds * 25):
            frame = av.VideoFrame(96, 64, "yuv420p")
            for plane, value in zip(frame.planes, (16 + index % 200, 100, 140)):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            output.mux(video.encode(frame))
            while sample < (index + 1) * 48000 // 25:
                for stream in audio:
                    sound = av.AudioFrame(format="s16", layout="mono", samples=1152)
                    sound.planes[0].update(bytes(sound.planes[0].buffer_size))
                    sound.sample_rate = 48000
                    sound.pts = sample
                    sound.time_base = Fraction(1, 48000)
                    output.mux(stream.encode(sound))
                sample += 1152
        output.mux(video.encode(None))
        for stream in audio:
            output.mux(stream.encode(None))
    return destination.getvalue()


@contextmanager
def clock_source(payload, reset_gops=(), early_language="eng"):
    """Only timestamps change; native packet contents and demux order are intact.

    Each reset returns the video decode clock to zero, below its initial +30s
    clock. The two audio tracks enter that same epoch on opposite sides of the
    video packet in demux order. This models normal transport-stream interleave
    without synthesizing packets, changing codecs, or decoding/re-encoding.
    """
    with av.open(io.BytesIO(payload)) as media:
        packets = [packet for packet in media.demux() if packet.size]
        keyframes = [
            index
            for index, packet in enumerate(packets)
            if packet.stream.type == "video" and packet.is_keyframe
        ]
        transitions = defaultdict(list)
        for gop in reset_gops:
            boundary = keyframes[gop]
            video = packets[boundary]
            shift = 30 + video.dts * video.time_base
            transitions[video.stream.index].append((boundary, shift))
            for stream in media.streams.audio:
                positions = [
                    index
                    for index, packet in enumerate(packets)
                    if packet.stream.index == stream.index
                ]
                if stream.metadata.get("language") == early_language:
                    cutoff = max(index for index in positions if index < boundary)
                else:
                    cutoff = min(index for index in positions if index > boundary)
                transitions[stream.index].append((cutoff, shift))

        class TimestampSource:
            streams = media.streams

            def demux(self, streams):
                for position, packet in enumerate(packets):
                    offset = Fraction(30)
                    for cutoff, shift in transitions[packet.stream.index]:
                        if position >= cutoff:
                            offset = 30 - shift
                    ticks = offset / packet.time_base
                    assert ticks.denominator == 1
                    if packet.pts is not None:
                        packet.pts += int(ticks)
                    if packet.dts is not None:
                        packet.dts += int(ticks)
                    yield packet

        yield TimestampSource()


def inspect_recording(content):
    counts = Counter()
    timestamps = defaultdict(list)
    with av.open(io.BytesIO(content)) as media:
        codecs = {
            (stream.type, stream.metadata.get("language")): stream.codec_context.name
            for stream in media.streams
        }
        for packet in media.demux():
            key = packet.stream.type, packet.stream.metadata.get("language")
            if packet.size:
                timestamps[key].append(
                    (packet.pts * packet.time_base, packet.dts * packet.time_base)
                )
            for frame in packet.decode():
                counts[key] += frame.samples if isinstance(frame, av.AudioFrame) else 1
    return {"counts": counts, "timestamps": timestamps, "codecs": codecs}


class TimeshiftClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = make_clock_stream()

    def record(self, *, reset_gops=(), early_language="eng", cap=2 * 1024 * 1024):
        messages = []
        with tempfile.TemporaryDirectory(prefix="qitv-clock-test-") as temporary:
            engine = _RecordingEngine(
                "unused",
                session_directory=Path(temporary) / (".qitv-timeshift-" + secrets.token_hex(16)),
                max_bytes=cap,
                stop=threading.Event(),
                send=messages.append,
            )
            engine._prepare_directory()
            try:
                with patch(
                    "services.timeshift.open_source",
                    return_value=clock_source(self.payload, reset_gops, early_language),
                ):
                    engine._record(av, [])
                state = engine._snapshot()
                segments = list(engine._segments.values())
                ranges = [(segment.start, segment.end) for segment in segments]
                manifest = engine._manifest(segments[0].sequence).decode("ascii")
                content = b"".join(segment.path.read_bytes() for segment in segments)
                self.assertTrue(state["ended"])
                self.assertLessEqual(state["bytes"], cap)
                self.assertLessEqual(sum(segment.allocated for segment in segments), cap)
                self.assertEqual(state["bytes"], len(content))
                self.assertEqual((state["start"], state["end"]), (ranges[0][0], ranges[-1][1]))
                for start, end in ranges:
                    self.assertGreater(end, start)
                    self.assertLessEqual(end - start, 2.2)
                for (_, end), (start, _) in zip(ranges, ranges[1:]):
                    self.assertAlmostEqual(end, start, delta=0.08)
                durations = [
                    float(line.split(":", 1)[1].rstrip(","))
                    for line in manifest.splitlines()
                    if line.startswith("#EXTINF:")
                ]
                self.assertEqual(len(durations), len(ranges))
                for duration, (start, end) in zip(durations, ranges):
                    self.assertAlmostEqual(duration, end - start, places=5)
                self.assertIn("#EXT-X-ENDLIST", manifest)
                listed = [line for line in manifest.splitlines() if line.endswith(".ts")]
                self.assertEqual(
                    listed, [f"../segment/{segment.sequence}.ts" for segment in segments]
                )
                states = [message[1] for message in messages if message[0] == "state"]
                for before, after in zip(states, states[1:]):
                    self.assertGreaterEqual(after["start"], before["start"])
                    self.assertGreaterEqual(after["end"], before["end"])
                self.assertTrue(all(item["bytes"] <= cap for item in states))
                result = inspect_recording(content)
                result.update(
                    state=state,
                    ranges=ranges,
                    allocations=[segment.allocated for segment in segments],
                )
                return result
            finally:
                engine._stop.set()
                engine.run()

    def assert_native_timestamps(self, recording):
        self.assertEqual(
            recording["codecs"],
            {
                ("video", None): "mpeg2video",
                ("audio", "eng"): "mp2",
                ("audio", "fra"): "mp2",
            },
        )
        for key, packets in recording["timestamps"].items():
            dts = [stamp[1] for stamp in packets]
            self.assertTrue(all(right > left for left, right in zip(dts, dts[1:])), key)
        video = recording["timestamps"][("video", None)]
        self.assertTrue(any(right[0] < left[0] for left, right in zip(video, video[1:])))
        self.assertGreater(max(pts - dts for pts, dts in video), Fraction(1, 25))

    def assert_same_media_clock(self, expected, actual):
        self.assertEqual(actual["counts"], expected["counts"])
        video_key = ("video", None)
        expected_origin = expected["timestamps"][video_key][0][0]
        actual_origin = actual["timestamps"][video_key][0][0]
        for key, original in expected["timestamps"].items():
            recovered = actual["timestamps"][key]
            self.assertEqual(len(recovered), len(original), key)
            for (pts, dts), (old_pts, old_dts) in zip(recovered, original):
                # One shared origin, not one per track: independent audio reset
                # offsets would pass a per-track comparison but fail this one.
                self.assertAlmostEqual(
                    float(pts - actual_origin),
                    float(old_pts - expected_origin),
                    delta=0.08,
                    msg=str(key),
                )
                self.assertEqual(pts - dts, old_pts - old_dts, key)

    def test_backward_reset_preserves_video_and_both_audio_arrival_orders(self):
        baseline = self.record()
        self.assertEqual(baseline["counts"][("video", None)], 600)
        for language in ("eng", "fra"):
            self.assertGreater(baseline["counts"][("audio", language)], 23 * 48000)
            with self.subTest(early_language=language):
                recovered = self.record(reset_gops=(3,), early_language=language)
                self.assert_native_timestamps(recovered)
                self.assert_same_media_clock(baseline, recovered)
                self.assertAlmostEqual(recovered["state"]["start"], 0)
                self.assertAlmostEqual(recovered["state"]["end"], 24, delta=0.15)

    def test_repeated_resets_keep_elapsed_duration_and_evict_old_history(self):
        complete = self.record()
        cap = sum(complete["allocations"][-3:])
        baseline = self.record(cap=cap)
        recovered = self.record(reset_gops=(3, 6, 9), cap=cap)
        self.assert_native_timestamps(recovered)
        self.assertAlmostEqual(recovered["state"]["end"], 24, delta=0.2)
        self.assertGreater(recovered["state"]["start"], 16)
        self.assertAlmostEqual(recovered["state"]["start"], baseline["state"]["start"], delta=0.08)
        # Native decode may discard at most the leading open-GOP B-frames.
        self.assertAlmostEqual(
            recovered["counts"][("video", None)],
            baseline["counts"][("video", None)],
            delta=3,
        )
        for language in ("eng", "fra"):
            self.assertAlmostEqual(
                recovered["counts"][("audio", language)],
                baseline["counts"][("audio", language)],
                delta=2 * 1152,
            )
        video_origin = recovered["timestamps"][("video", None)][0][0]
        baseline_origin = baseline["timestamps"][("video", None)][0][0]
        for key in (("audio", "eng"), ("audio", "fra")):
            for position in (0, -1):
                self.assertAlmostEqual(
                    float(recovered["timestamps"][key][position][0] - video_origin),
                    float(baseline["timestamps"][key][position][0] - baseline_origin),
                    delta=0.08,
                )

    def test_native_b_frame_reordering_does_not_create_clock_resets(self):
        recorded = self.record()
        self.assert_native_timestamps(recorded)
        original = inspect_recording(self.payload)
        self.assertEqual(recorded["counts"][("video", None)], original["counts"][("video", None)])
        video = recorded["timestamps"][("video", None)]
        original_video = original["timestamps"][("video", None)]
        self.assertEqual(
            [(pts - video[0][0], pts - dts) for pts, dts in video],
            [(pts - original_video[0][0], pts - dts) for pts, dts in original_video],
        )
        self.assertAlmostEqual(recorded["state"]["end"], 24, delta=0.08)
        for language in ("eng", "fra"):
            # Initial audio before the first video keyframe is intentionally not
            # retained. No reset/reorder handling may discard later samples.
            self.assertAlmostEqual(
                recorded["counts"][("audio", language)],
                original["counts"][("audio", language)],
                delta=4800,
            )


if __name__ == "__main__":
    unittest.main()
