"""Tests for LoopTrimmer (services/live/ts.py): a provider that replays its last seconds under
new timestamps while its own source is down, on synthetic streams.

Each picture carries a content number (an IDR picture every 25), and its two sound PES carry
the content's too, so a replay repeats both byte for byte, as the provider's does; "live" sound
is new every time. The pictures' PTS run on a frame apart however the content goes.
"""

from __future__ import annotations

from collections.abc import Iterable
import time
import unittest
from unittest import mock

from services.live import ts
from services.live.ts import PACKET, ClockJoiner, LoopTrimmer, pts_delta
from tests.ts_fixtures import AUDIO_PID, CLOCK, PMT_PID, VIDEO_PID, pat, pes_packets, pmt

FPS = 25
FRAME = CLOCK // FPS
SOUND = FRAME // 2  # two sound PES a picture
GOP = 25
START = 900000
_IDR = b"\x00\x00\x00\x01\x67" + b"\x55" * 15 + b"\x00\x00\x00\x01\x65\x55"
_P = b"\x00\x00\x00\x01\x41\x55"


def _tag(value: int, size: int) -> bytes:
    return bytes(0x80 | ((value >> (7 * k)) & 0x7F) for k in range(size))  # never a start code


def _untag(data: bytes) -> int:
    return sum((b & 0x7F) << (7 * k) for k, b in enumerate(data))


def picture(content: int) -> bytes:
    """An H.264 access unit only `content` has: an IDR picture every `GOP`, else P."""
    head = b"\x00\x00\x00\x01\x09\xf0" + (_IDR if content % GOP == 0 else _P)
    return head + _tag(content, 3) + b"\x55" * 300


class Writer:
    """A stream written picture by picture: PTS a frame apart, counters running on, the tables
    every 12 pictures, each picture's two sound PES after it."""

    def __init__(self, pts: int = START) -> None:
        self.pts = pts
        self.counters = {0: 0, PMT_PID: 0, VIDEO_PID: 0, AUDIO_PID: 0}
        self.written = 0
        self.live = 1 << 21  # the next live sound's number

    def pictures(self, contents: Iterable[int], *, live: bool = False, sound: bool = True) -> bytes:
        out: list[bytes] = []
        counters = self.counters
        for content in contents:
            if self.written % 12 == 0:
                out.append(pat(cc=counters[0]))
                out.append(pmt(audio_pids=(AUDIO_PID,) if sound else (), cc=counters[PMT_PID]))
                counters[0] = (counters[0] + 1) & 0x0F
                counters[PMT_PID] = (counters[PMT_PID] + 1) & 0x0F
            packets, counters[VIDEO_PID] = pes_packets(
                VIDEO_PID,
                0xE0,
                picture(content),
                self.pts,
                rai=content % GOP == 0,
                cc_start=counters[VIDEO_PID],
                pcr=self.pts,
            )
            out.extend(packets)
            for k in range(2 if sound else 0):
                number = self.live if live else 2 * content + k
                self.live += live
                packets, counters[AUDIO_PID] = pes_packets(
                    AUDIO_PID,
                    0xC0,
                    b"\xff\xf1" + _tag(number, 4) + b"\xaa" * 200,
                    self.pts + k * SOUND,
                    cc_start=counters[AUDIO_PID],
                )
                out.extend(packets)
            self.pts += FRAME
            self.written += 1
        return b"".join(out)

    def sound_only(self, seconds: float) -> bytes:
        """Live sound without pictures (the video stalled)."""
        out: list[bytes] = []
        for _ in range(round(seconds * CLOCK / SOUND)):
            packets, self.counters[AUDIO_PID] = pes_packets(
                AUDIO_PID,
                0xC0,
                b"\xff\xf1" + _tag(self.live, 4) + b"\xaa" * 200,
                self.pts,
                cc_start=self.counters[AUDIO_PID],
            )
            out.extend(packets)
            self.live += 1
            self.pts += SOUND
        return b"".join(out)


def packets(data: bytes) -> list[bytes]:
    return [data[at : at + PACKET] for at in range(0, len(data) - PACKET + 1, PACKET)]


def pid_of(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def starts(data: bytes, pid: int) -> list[tuple[int, bytes]]:
    """(PTS, ES bytes in the first packet) of each PES start on `pid`."""
    out = []
    for p in packets(data):
        if pid_of(p) == pid and p[1] & 0x40:
            _, pts, es = ts._pes_start(p)
            assert pts is not None
            out.append((pts, p[es:]))
    return out


def contents(data: bytes) -> list[int]:
    """The content of each picture in `data`, in order."""
    found = []
    for _, es in starts(data, VIDEO_PID):
        at = 6 + (len(_IDR) if es[6:11] == _IDR[:5] else len(_P))
        found.append(_untag(es[at : at + 3]))
    return found


def sounds(data: bytes) -> list[int]:
    return [_untag(es[2:6]) for _, es in starts(data, AUDIO_PID)]


def steps(data: bytes, pid: int) -> set[int]:
    times = [pts for pts, _ in starts(data, pid)]
    return {pts_delta(a, b) for a, b in zip(times, times[1:])}


def cc_breaks(data: bytes) -> int:
    breaks, last = 0, {}
    for p in packets(data):
        pid, b3 = pid_of(p), p[3]
        if not b3 & 0x10:
            continue
        if pid in last and (b3 & 0x0F) != (last[pid] + 1) & 0x0F:
            breaks += 1
        last[pid] = b3 & 0x0F
    return breaks


def trimmer(clock: list[float] | None = None) -> LoopTrimmer:
    now = clock if clock is not None else [0.0]
    return LoopTrimmer(program=lambda: (VIDEO_PID, 0x1B), clock=lambda: now[0])


def run(
    *connections: bytes, chunk: int | None = None, loops: LoopTrimmer | None = None
) -> tuple[list[bytes], LoopTrimmer]:
    """The connections through a loop trimmer (a reconnect between two), fed `chunk` bytes at a
    time: the pieces it passed on (each after the first starts a new run)."""
    loops = loops or trimmer()
    pieces = [b""]

    def take(parts: list[bytes]) -> None:
        pieces[-1] += parts[0]
        pieces.extend(parts[1:])

    for number, data in enumerate(connections):
        if number:
            take(loops.reconnect())
        step = chunk or len(data)
        for at in range(0, len(data), step):
            take(loops.feed([data[at : at + step]]))
    take(loops.finish())
    return pieces, loops


def rebased(pieces: list[bytes]) -> bytes:
    """The pieces through a ClockJoiner, as the helper passes them on."""
    joiner = ClockJoiner(program=lambda: (VIDEO_PID, 0x1B))
    out = []
    for k, piece in enumerate(pieces):
        out.extend(joiner.feed(piece, junction=k > 0))
    out.append(joiner.finish())
    return b"".join(out)


class LoopTrimmerTests(unittest.TestCase):
    def test_a_stretch_replayed_three_times_is_dropped_and_the_clock_runs_on(self):
        # The source dies after 20 s; the provider replays its last 9 s three times under new
        # timestamps, then new content comes from a keyframe.
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(list(range(275, 500)) * 3)
        data += w.pictures(range(500, 625))
        for chunk in (None, 1000, 187):
            with self.subTest(chunk=chunk):
                pieces, loops = run(data, chunk=chunk)
                self.assertEqual(len(pieces), 2)  # the new content is a run of its own
                out = rebased(pieces)
                self.assertEqual(contents(out), list(range(625)))
                self.assertEqual(sounds(out), list(range(1250)))
                self.assertEqual(steps(out, VIDEO_PID), {FRAME})  # no jump where it was cut
                self.assertEqual(steps(out, AUDIO_PID), {SOUND})
                self.assertEqual(cc_breaks(out), 0)
                (loop,) = loops.loops
                self.assertEqual(loop.ended_by, "keyframe")
                self.assertAlmostEqual(loop.back_s, 9.0)
                self.assertAlmostEqual(loop.dropped_s, 27.0)
                self.assertEqual(len(out), len(data) - loop.dropped_bytes)

    def test_the_live_edge_stands_still_from_the_loops_first_picture(self):
        # Nothing of the loop passes on: what passed before it ends with the last picture
        # before it, whenever it was recognised.
        w = Writer()
        before = w.pictures(range(500))
        data = before + w.pictures(list(range(275, 500)) * 2)
        loops = trimmer()
        passed = b"".join(loops.feed([data]))
        self.assertEqual(contents(passed), list(range(500)))
        self.assertEqual(len(loops.loops), 0)  # it goes on
        self.assertEqual(loops.finish(), [b""])
        self.assertEqual(loops.loops[0].ended_by, "end")

    def test_a_still_picture_an_encoder_repeats_is_kept(self):
        # The same picture over and over (its sound too), and a still's few pictures repeated
        # every 2.4 s with live sound: neither replays a sequence.
        w = Writer()
        still = w.pictures(range(100)) + w.pictures([107] * 300)
        pattern = Writer().pictures([k % 60 for k in range(600)], live=True)
        for data in (still, pattern):
            with self.subTest(len=len(data)):
                pieces, loops = run(data, chunk=1000)
                self.assertEqual(pieces, [data])
                self.assertEqual(list(loops.loops), [])

    def test_a_replay_shorter_than_two_seconds_is_kept(self):
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(range(250, 287)) + w.pictures(range(500, 600))
        pieces, loops = run(data, chunk=1000)
        self.assertEqual(pieces, [data])
        self.assertEqual(list(loops.loops), [])

    def test_a_replay_whose_sound_is_new_is_kept(self):
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(list(range(275, 500)) * 2, live=True)
        pieces, loops = run(data, chunk=1000)
        self.assertEqual(pieces, [data])
        self.assertEqual(list(loops.loops), [])

    def test_a_loop_whose_sound_is_new_for_two_seconds_ends_at_the_next_picture(self):
        # Pictures that go on repeating with live sound (a still picture whose silence made
        # it look like a loop): from the picture that comes with 2 s of new sound.
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(range(275, 400))
        data += w.pictures(range(400, 500), live=True)
        pieces, loops = run(data, chunk=1000)
        (loop,) = loops.loops
        self.assertEqual(loop.ended_by, "sound")
        shown = contents(rebased(pieces))
        self.assertEqual(shown, [*range(500), *range(450, 500)])

    def test_new_pictures_end_a_loop_at_their_first_keyframe(self):
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(list(range(275, 500)) * 2)
        data += w.pictures(range(1010, 1100))  # a keyframe 15 pictures in
        pieces, loops = run(data, chunk=1000)
        self.assertEqual(contents(rebased(pieces)), [*range(500), *range(1025, 1100)])
        self.assertEqual(loops.loops[0].ended_by, "keyframe")

    def test_new_pictures_without_a_keyframe_end_a_loop_after_two_seconds(self):
        # An intra-refresh stream: no keyframe ever comes.
        w = Writer()
        data = w.pictures(range(500)) + w.pictures(list(range(275, 500)) * 2)
        fresh = [c for c in range(2000, 2200) if c % GOP]
        data += w.pictures(fresh)
        pieces, loops = run(data, chunk=1000)
        self.assertEqual(contents(rebased(pieces)), [*range(500), *fresh[49:]])
        self.assertEqual(loops.loops[0].ended_by, "pictures")

    def test_a_loop_goes_on_across_a_reconnect(self):
        w = Writer()
        old = w.pictures(range(500)) + w.pictures(range(275, 400))
        new = w.pictures(range(400, 500)) + w.pictures(range(275, 500))
        new += w.pictures(range(500, 600))
        pieces, loops = run(old, new, chunk=1000)
        self.assertEqual(contents(b"".join(pieces)), list(range(600)))
        (loop,) = loops.loops
        self.assertAlmostEqual(loop.dropped_s, (125 + 325) / FPS)

    def test_a_stream_without_a_loop_passes_on_untouched_in_its_pieces(self):
        # A new run (a restart RepeatTrimmer passed on) keeps its place, and so does one that
        # comes while a run is being judged.
        w = Writer()
        first = w.pictures(range(300))
        second = w.pictures(range(100, 120))  # a replay being judged when the next run comes
        third = w.pictures(range(300, 400))
        loops = trimmer()
        pieces = loops.feed([first[:5000]])
        for parts in (
            loops.feed([first[5000:], second, third[:7000]]),
            loops.feed([third[7000:]]),
            loops.finish(),
        ):
            pieces[-1] += parts[0]
            pieces.extend(parts[1:])
        self.assertEqual(pieces, [first, second, third])
        self.assertEqual(list(loops.loops), [])

    def test_sound_goes_on_while_the_pictures_stall(self):
        # The video stops for 3 s while the sound goes on: the sound is not kept waiting for
        # the picture after the last one (nor dropped in a loop, which ends there).
        w = Writer()
        before = w.pictures(range(100))
        stall = w.sound_only(3.0)
        after = w.pictures(range(100, 150))
        loops = trimmer()
        passed = b"".join(loops.feed([before]) + loops.feed([stall]))
        self.assertGreater(len(passed), len(before + stall) - 4 * 1024)  # but the last packets
        self.assertEqual(
            b"".join([passed, *loops.feed([after]), *loops.finish()]), before + stall + after
        )

        w = Writer()
        data = w.pictures(range(500)) + w.pictures(range(275, 400))
        data += w.sound_only(3.0) + w.pictures(range(1000, 1050))
        pieces, loops = run(data, chunk=1000)
        (loop,) = loops.loops
        self.assertEqual(loop.ended_by, "stall")
        out = rebased(pieces)
        self.assertEqual(contents(out)[-50:], list(range(1000, 1050)))
        self.assertGreater(len(sounds(out)), 2 * 500 + 3 * CLOCK // SOUND - 64)

    def test_a_loop_ends_when_no_picture_comes_after_a_reconnect_while_the_sound_goes_on(self):
        # The next connection brings sound, but no picture on the video PID (its pictures
        # moved to another PID, say): nothing piles up, and the sound passes on.
        w = Writer()
        old = w.pictures(range(500)) + w.pictures(range(275, 400))
        new = w.sound_only(4.0)
        loops = trimmer()
        loops.feed([old])
        loops.reconnect()
        passed = b"".join(
            b"".join(loops.feed([new[at : at + 1000]])) for at in range(0, len(new), 1000)
        )
        self.assertLess(len(loops._held), 4 * 1024)
        self.assertEqual(loops.finish(), [b""])
        (loop,) = loops.loops
        self.assertEqual(loop.ended_by, "stall")
        self.assertGreater(len(sounds(passed)), 4 * CLOCK // SOUND - 2 * 64)

    def test_a_channel_that_is_a_30_s_loop_plays_again_after_three_minutes(self):
        # An off-air card: the same 30 s for ever. It is dropped as a replay, then after 3
        # minutes it plays again from a keyframe, its clock running on, and stays playing;
        # a different loop later is dropped again.
        now = [0.0]
        loops = trimmer(now)
        w = Writer()
        pieces = [b""]

        def send(contents: list[int]) -> None:
            for at in range(0, len(contents), FPS):  # a second at a time, at real time
                parts = loops.feed([w.pictures(contents[at : at + FPS])])
                pieces[-1] += parts[0]
                pieces.extend(parts[1:])
                now[0] += 1.0

        send(list(range(750)) * 9)  # 270 s
        send(list(range(5000, 5500)) + list(range(5275, 5500)) * 2)  # new, then its last 9 s
        pieces[-1] += loops.finish()[0]
        given, other = loops.loops
        self.assertEqual(given.ended_by, "given up")
        self.assertAlmostEqual(given.ended_at - given.found_at, ts._LOOP_GIVE_UP_S, delta=1.0)
        out = rebased(pieces)
        self.assertEqual(steps(out, VIDEO_PID), {FRAME})
        shown = contents(out)
        resumed = 750 + round(given.dropped_s * FPS)
        self.assertEqual(shown[:750], list(range(750)))
        self.assertEqual(shown[750], shown[750] // GOP * GOP)  # from a keyframe
        self.assertEqual(len(shown), 9 * 750 - resumed + 750 + 500)  # it stays playing
        self.assertEqual((other.ended_by, other.dropped_s), ("end", 18.0))

    def test_memory_stays_bounded(self):
        with (
            mock.patch.object(ts, "_LOOP_WINDOW_PICTURES", 64),
            mock.patch.object(ts, "_LOOP_WINDOW_SOUND", 96),
        ):
            loops = trimmer()
            w = Writer()
            biggest = 0
            for at in range(0, 2000, 50):
                loops.feed([w.pictures(range(at, at + 50))])
                biggest = max(biggest, len(loops._held))
            self.assertLessEqual(len(loops._picture_at), 64)
            self.assertLessEqual(len(loops._sound_at), 96)
            self.assertLess(biggest, 4 * PACKET * 4)  # a picture and its sound: a few packets

    def test_a_loop_longer_than_the_window_is_dropped_whole(self):
        # The pictures dropped stay in the window: the third replay is still found.
        with mock.patch.object(ts, "_LOOP_WINDOW_S", 12.0):
            w = Writer()
            data = w.pictures(range(500)) + w.pictures(list(range(275, 500)) * 3)
            data += w.pictures(range(500, 550))
            pieces, loops = run(data, chunk=1000)
        self.assertEqual(contents(rebased(pieces)), list(range(550)))

    def test_the_clock_says_when_a_loop_was_found_and_when_it_ended(self):
        now = [0.0]
        loops = trimmer(now)
        w = Writer()
        loops.feed([w.pictures(range(500))])
        for second in range(9):
            now[0] = 20.0 + second
            loops.feed([w.pictures(range(275 + 25 * second, 300 + 25 * second))])
        now[0] = 30.0
        loops.feed([w.pictures(range(500, 525))])
        loops.finish()
        (loop,) = loops.loops
        self.assertEqual((loop.found_at, loop.ended_at), (22.0, 30.0))

    def test_a_packet_takes_a_few_microseconds_at_most(self):
        w = Writer()
        data = w.pictures(range(1500))
        loops = trimmer()
        started = time.perf_counter()
        for at in range(0, len(data), 64 * 1024):
            loops.feed([data[at : at + 64 * 1024]])
        per_packet = (time.perf_counter() - started) / (len(data) / PACKET)
        self.assertLess(per_packet, 20e-6)  # about 1 µs measured; generous for a loaded runner


if __name__ == "__main__":
    unittest.main()
