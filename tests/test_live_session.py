import math
import os
import random
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import services.live.live_session as live_session
from services.live.live_session import LiveSession
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.live.stay_live import TARGETS
from services.live.timeline import Span
from services.playback import PlaybackState
from services.vlc_profile import PlayKind

app = QApplication.instance() or QApplication([])

URL = "http://provider.example/live/user/secret/42.ts"
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
BUFFERED = "Lost the connection; playing what's buffered"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
LOADING = "Still loading…"
DIRECT = (URL, PlayKind.LIVE, "normal")
MiB = 1024 * 1024


def segments(first_seq, count, *, start=0.0, length=2.0):
    """`count` closed spans of `length` seconds from `first_seq`, the first at `start`."""
    return [
        Span(first_seq + i, start + i * length, start + (i + 1) * length, False)
        for i in range(count)
    ]


class FakeEngine(QObject):
    """LiveEngine's GUI-side surface; the test drives its buffer and its signals."""

    available = Signal(bool)
    sessionReady = Signal(str)
    sessionUpdated = Signal(str, dict)
    sessionFailed = Signal(str, str, str)
    sessionEnded = Signal(str)
    sessionClosed = Signal(str)

    def __init__(self):
        super().__init__()
        self.up = True
        self.gave_up = False  # an engine that gave up before this LiveSession existed
        self.opened = []  # (sid, url, keyword arguments)
        self.closed = []
        self.events = []  # ("open", sid) as sessions open
        self._answered = set()
        self._spans = {}
        self._snapshots = {}

    @property
    def last_sid(self):
        return self.opened[-1][0]

    def is_available(self):
        return self.up

    def open_session(self, url, **kwargs):
        sid = f"s{len(self.opened) + 1}"
        self.opened.append((sid, url, kwargs))
        self.events.append(("open", sid))
        if self.gave_up:  # the engine's answer after its give-up, queued like a relay
            QTimer.singleShot(
                0, lambda: self.sessionFailed.emit(sid, "unavailable", "Not available.")
            )
        return sid

    def close_session(self, sid):
        self.closed.append(sid)
        if self.gave_up:
            QTimer.singleShot(0, self.answer)

    def answer(self):
        """The helper confirms every close so far."""
        for sid in self.closed:
            if sid not in self._answered:
                self._answered.add(sid)
                self.sessionClosed.emit(sid)

    def lose(self):
        """A second helper death: time-shift is off for the run."""
        self.up = False
        self.available.emit(False)

    def spans(self, sid):
        return list(self._spans.get(sid, []))

    def snapshot(self, sid):
        return dict(self._snapshots.get(sid, {}))

    def stream_url(self, sid, seq):
        return f"http://127.0.0.1:9/token/{sid}/stream/{seq}.ts" if self.up else ""

    def buffer(self, sid, spans, *, live_edge=None, open_seq=None, burst=0.0):
        """Closed `spans` plus an optional open segment, as the helper's state reports them."""
        end = spans[-1].end if spans else 0.0
        oldest = spans[0].seq if spans else open_seq
        self._spans[sid] = list(spans)
        self._snapshots[sid] = {
            "oldest": oldest,
            "newest": open_seq if open_seq is not None else (spans[-1].seq if spans else None),
            "open": open_seq,
            "start": spans[0].start if spans else end,
            "end": end,
            "live_edge": end if live_edge is None else live_edge,
            "bytes": 0,
            "ended": False,
            "burst_s": burst,
        }
        self.sessionUpdated.emit(sid, dict(self._snapshots[sid]))

    def receive(self, sid, count):
        """`count` bytes arrived, but nothing is playable yet: the helper's state before ready."""
        self._snapshots[sid] = {
            "oldest": None,
            "newest": None,
            "open": None,
            "start": 0.0,
            "end": 0.0,
            "live_edge": 0.0,
            "bytes": 0,
            "ended": False,
            "burst_s": 0.0,
            "received": count,
        }
        self.sessionUpdated.emit(sid, dict(self._snapshots[sid]))

    def reset(self, sid):
        """The helper holding `sid` died: its spans and snapshot are gone."""
        self._spans.pop(sid, None)
        self._snapshots.pop(sid, None)


class FakePlayback(QObject):
    """PlaybackSession's surface: records calls; `ms` and `current` are set by the test."""

    stateChanged = Signal(str)
    positionChanged = Signal(int, int)
    errorOccurred = Signal(str)
    ended = Signal()
    inputEnded = Signal()

    def __init__(self):
        super().__init__()
        self.holding = False  # a stop leaves libVLC's input open until the test ends it
        self.plays = []  # (url, kind, latency)
        self.stops = 0
        self.paused = []
        self.rates = []
        self.toggles = 0
        self.ms = 0
        self.current = PlaybackState.IDLE
        self.fail_on_play = False

    def play(self, url, *, kind, latency="normal", user_agent=None, start_ms=None):
        self.plays.append((url, kind, latency))
        self.ms = 0
        self.set_state(PlaybackState.OPENING)
        if self.fail_on_play:
            self.set_state(PlaybackState.ERROR)
            self.errorOccurred.emit("VLC could not start playback")

    def set_state(self, state):
        self.current = state
        self.stateChanged.emit(state.value)

    def stop(self):
        self.stops += 1
        self.set_state(PlaybackState.IDLE)

    def state(self):
        return self.current

    def holds_input(self):
        return self.holding

    def let_go(self):
        """The worker ended libVLC's input."""
        self.holding = False
        self.inputEnded.emit()

    def time_ms(self):
        return self.ms

    def set_paused(self, paused):
        self.paused.append(paused)
        if paused and self.current is PlaybackState.PLAYING:
            self.set_state(PlaybackState.PAUSED)
        elif not paused and self.current is PlaybackState.PAUSED:
            self.set_state(PlaybackState.PLAYING)

    def toggle_pause(self):
        self.toggles += 1

    def set_rate(self, rate):
        self.rates.append(rate)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Feed:
    """A live source and a VLC around a test's fakes, modelled on the review's simulator.

    Media arrives `burst` seconds at a time every `burst` seconds (raw TS when 0: a quarter
    second at a time), reported as `burst_s` from arrival `measured_after` on (the helper
    measures a raw TS source's bursts; HLS reports its own at once), each arrival optionally
    jittered, cut into `gop` second segments
    whose newest stays open until the next arrival, as in the real segmenter. VLC plays at
    the last rate set while it has data; at the edge it reports `starved` (BUFFERING; real
    libVLC 3 keeps reporting PLAYING with its clock standing still). While `stalled` the
    source sends nothing: the edge stands still and no state arrives. LiveSession ticks every
    0.25 s.
    """

    DT = 0.05

    def __init__(
        self,
        case,
        sid,
        *,
        burst,
        edge=60.0,
        gop=2.0,
        jitter=0.0,
        seed=1,
        first=None,
        starved=PlaybackState.BUFFERING,
        measured_after=0,
    ):
        self.case, self.sid, self.burst, self.gop = case, sid, burst, gop
        self.measured_after, self.arrivals = measured_after, 0
        self.edge, self.chunk = edge, (burst if burst > 0 else 0.25)
        self.jitter, self.rng = jitter, random.Random(seed)
        self.nominal = case.clock.now + (self.chunk if first is None else first)
        self.next_arrival = self.nominal
        self.next_tick = case.clock.now + 0.25
        self.position, self.media, self.plays_seen = None, 0.0, 0
        self.starved, self.stalled = starved, False
        self.starved_s = 0.0  # how long VLC had nothing to play
        self.before = []  # the lag right before each arrival
        self.push()

    def push(self):
        closed = math.ceil(self.edge / self.gop - 1e-9) - 1  # the newest segment is open
        spans = segments(0, closed, length=self.gop)
        burst = self.burst if self.arrivals >= self.measured_after else 0.0
        self.case.engine.buffer(self.sid, spans, live_edge=self.edge, open_seq=closed, burst=burst)

    def ready(self):
        self.case.engine.sessionReady.emit(self.sid)
        self.sync()
        return self

    def sync(self):
        """A new pipeline play opens at once (and is paused again if it should be)."""
        playback = self.case.playback
        if len(playback.plays) > self.plays_seen:
            self.plays_seen = len(playback.plays)
            if playback.plays[-1][1] is PlayKind.LIVE_PIPELINE:
                self.position, self.media = self.case.live.position(), 0.0
                playback.set_state(PlaybackState.PLAYING)

    def run(self, seconds):
        case, playback = self.case, self.case.playback
        for _ in range(round(seconds / self.DT)):
            case.clock.now = now = round(case.clock.now + self.DT, 6)
            if now >= self.next_arrival - 1e-9:
                if self.position is not None and not self.stalled:
                    self.before.append(self.edge - self.position)
                while now >= self.next_arrival - 1e-9:
                    if not self.stalled:
                        self.edge += self.chunk
                        self.arrivals += 1
                    self.nominal += self.chunk
                    self.next_arrival = self.nominal + self.rng.uniform(-self.jitter, self.jitter)
                if not self.stalled:
                    self.push()
            self.sync()
            if self.position is not None and playback.current in (
                PlaybackState.PLAYING,
                PlaybackState.BUFFERING,
            ):
                rate = (playback.rates or [1.0])[-1]
                advance = max(0.0, min(rate * self.DT, self.edge - self.position))
                state = PlaybackState.PLAYING
                if advance < rate * self.DT - 1e-9:
                    state = self.starved
                    self.starved_s += self.DT
                if state is not playback.current:
                    playback.set_state(state)
                self.position += advance
                self.media += advance
                playback.ms = round(self.media * 1000)
            if now >= self.next_tick - 1e-9:
                self.next_tick += 0.25
                case.live._tick()
                self.sync()
        return self


def reversals(rates):
    """How often the rate turns around (up after down, or down after up)."""
    rates = [1.0] + list(rates)
    return sum(1 for a, b, c in zip(rates, rates[1:], rates[2:]) if (b - a) * (c - b) < 0)


class LiveSessionCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.live = self.make()

    def make(self, **kwargs):
        """A LiveSession on fresh fakes: an earlier one stays wired only to its own."""
        self.engine = FakeEngine()
        self.playback = FakePlayback()
        kwargs.setdefault("tick_ms", 3_600_000)  # ticks are driven by the test
        kwargs.setdefault("clock", self.clock)
        live = LiveSession(self.engine, self.playback, **kwargs)
        self.modes, self.notices, self.failures = [], [], []
        self.releases, self.timelines = [], []
        live.modeChanged.connect(self.modes.append)
        live.notice.connect(self.notices.append)
        live.failed.connect(self.failures.append)
        live.released.connect(lambda: self.releases.append(True))
        live.released.connect(lambda: self.engine.events.append(("released",)))
        live.timelineChanged.connect(self.timelines.append)
        self.addCleanup(self.engine.answer)  # runs after live.stop (cleanups are LIFO)
        self.addCleanup(live.stop)
        return live

    def patch(self, name, value):
        self.enterContext(mock.patch.object(live_session, name, value))

    # ---- helpers ----------------------------------------------------------------------
    def start(self, latency="normal", disk=False, max_bytes=128 * MiB):
        self.live.start(
            URL,
            headers={"User-Agent": "QiTV-Test"},
            verify_ssl=True,
            latency=latency,
            user_agent="QiTV-Test",
            disk=disk,
            max_bytes=max_bytes,
        )
        return self.engine.last_sid if self.engine.opened else None

    def ready(self, spans, *, live_edge=None, open_seq=None, burst=0.0, latency="normal"):
        sid = self.start(latency=latency)
        self.engine.buffer(sid, spans, live_edge=live_edge, open_seq=open_seq, burst=burst)
        self.engine.sessionReady.emit(sid)
        return sid

    def stream(self, sid, seq):
        return (self.engine.stream_url(sid, seq), PlayKind.LIVE_PIPELINE, "normal")

    def step(self, seconds, media=None):
        """Let `seconds` of wall time pass while VLC advances by `media` (default: as much)."""
        self.clock.now += seconds
        self.playback.ms += round((seconds if media is None else media) * 1000)
        self.live._tick()

    def wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("timed out")
            QTest.qWait(10)

    def active_timers(self):
        return [timer for timer in self.live.findChildren(QTimer) if timer.isActive()]

    def assert_falls_back(self, sid, notice, plays_before):
        """Closed, nothing direct until the close is confirmed, then direct play."""
        self.assertIn(sid, self.engine.closed)
        self.assertEqual(len(self.playback.plays), plays_before)
        self.assertNotIn("direct", self.modes)
        self.engine.answer()
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices[-1], notice)
        self.assertEqual(self.modes[-1], "direct")
        self.assertFalse(self.live.is_pipeline())


class StartTests(LiveSessionCase):
    def test_a_disk_buffer_carries_its_memory_fallback_size(self):
        # The helper's disk-failure fallback follows timeshift_ram_mib, not 128 MiB (H12).
        self.live.start(
            URL,
            headers={},
            verify_ssl=True,
            latency="normal",
            user_agent="QiTV-Test",
            disk=True,
            max_bytes=2048 * MiB,
            ram_bytes=512 * MiB,
        )
        self.assertEqual(self.engine.opened[0][2]["ram_bytes"], 512 * MiB)

    def test_ready_plays_the_oldest_segment_through_the_pipeline(self):
        sid = self.start(disk=True, max_bytes=2048 * MiB)
        _, url, kwargs = self.engine.opened[0]
        self.assertEqual(url, URL)
        self.assertEqual(
            kwargs,
            {
                "headers": {"User-Agent": "QiTV-Test"},
                "verify_ssl": True,
                "disk": True,
                "max_bytes": 2048 * MiB,
                "live_delay": 6.0,
            },
        )
        self.assertEqual(self.playback.plays, [])  # nothing plays before ready

        modes_at_open = []
        self.playback.stateChanged.connect(
            lambda state: modes_at_open.append(self.live.mode) if state == "opening" else None
        )
        self.engine.buffer(sid, [], open_seq=0)  # the first segment is still open
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(modes_at_open, ["pipeline"])  # set before the first play
        self.assertEqual(self.modes, ["pipeline"])
        self.assertTrue(self.live.is_pipeline())
        self.assertEqual(self.live.mode, "pipeline")
        self.assertEqual(self.live.position(), 0.0)
        self.assertEqual(self.notices, [])

    def test_ready_bases_the_position_on_the_oldest_closed_segment(self):
        sid = self.ready(segments(7, 3, start=14.0), live_edge=20.5)
        self.assertEqual(self.playback.plays, [self.stream(sid, 7)])
        self.assertEqual(self.live.position(), 14.0)
        self.assertEqual(self.live.buffered_range(), (14.0, 20.5))

    def test_low_and_normal_play_the_oldest_segment_at_once(self):
        for latency in ("low", "normal"):
            with self.subTest(latency=latency):
                self.live = self.make()
                sid = self.ready(segments(0, 1), live_edge=2.5, open_seq=1, latency=latency)
                url = self.engine.stream_url(sid, 0)  # 2.5 s behind: no wait for zapping
                self.assertEqual(self.playback.plays, [(url, PlayKind.LIVE_PIPELINE, latency)])

    def test_stable_waits_until_the_buffer_holds_its_target(self):
        sid = self.start(latency="stable")
        self.engine.buffer(sid, segments(0, 1), live_edge=2.5, open_seq=1)
        self.engine.sessionReady.emit(sid)
        self.assertEqual((self.playback.plays, self.modes), ([], []))
        self.engine.buffer(sid, segments(0, 2), live_edge=5.0, open_seq=2)
        self.assertEqual(self.playback.plays, [])  # 5.5 s once VLC opened: under 5.75
        self.engine.buffer(sid, segments(0, 2), live_edge=5.5, open_seq=2)
        stable = (self.engine.stream_url(sid, 0), PlayKind.LIVE_PIPELINE, "stable")
        self.assertEqual(self.playback.plays, [stable])  # 6.0 s: on target
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.engine.closed, [])

    def test_stable_starts_at_its_target_when_the_buffer_holds_more(self):
        sid = self.ready(segments(0, 6), live_edge=12.0, burst=6.0, latency="stable")
        # A burst just landed: from 2.0 the mean lag is 7.5 s once playing, from 4.0 5.5 s.
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 1))

    def test_stables_wait_for_its_cushion_is_bounded_by_the_ready_timeout(self):
        self.live = self.make(ready_timeout_ms=50)
        sid = self.ready(segments(0, 1), live_edge=2.5, open_seq=1, latency="stable")
        self.assertEqual(self.playback.plays, [])
        self.wait_until(lambda: self.playback.plays)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual((self.engine.closed, self.notices), ([], []))

    def test_live_delay_follows_the_latency_setting(self):
        for latency, delay in (("low", 3.0), ("normal", 6.0), ("stable", 12.0)):
            with self.subTest(latency=latency):
                self.start(latency=latency)
                self.assertEqual(self.engine.opened[-1][2]["live_delay"], delay)
                self.live.stop()
                self.engine.answer()

    def test_opens_before_the_helper_says_hello(self):
        self.engine.up = False  # spawning: no hello yet, but no available(False) either
        sid = self.start()
        self.assertEqual([opened[0] for opened in self.engine.opened], [sid])
        self.assertEqual(self.playback.plays, [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_dead_engine_plays_directly_at_once(self):
        self.engine.lose()
        self.start(latency="low")
        self.assertEqual(self.engine.opened, [])
        self.assertEqual(self.playback.plays, [(URL, PlayKind.LIVE, "low")])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])
        self.assertFalse(self.live.is_pipeline())

    def test_an_unsupported_source_falls_back_once_the_session_closed(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.modes, ["direct"])

    def test_not_ready_in_time_falls_back(self):
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)  # too late
        self.assertEqual(len(self.playback.plays), 1)

    def test_a_source_that_sends_nothing_falls_back_at_the_ready_timeout(self):
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=60_000)
        sid = self.start()
        self.wait_until(lambda: self.engine.closed)  # not 60 s later
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [UNAVAILABLE])

    def test_bytes_without_anything_playable_extend_the_wait_with_a_hint(self):
        """An on-demand restream starting up (2.0.1, L2a): bytes flow, no keyframe yet."""
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=60_000)
        sid = self.start()
        self.engine.receive(sid, 188 * 20)
        self.wait_until(lambda: self.notices)
        self.assertEqual(self.notices, [LOADING])
        self.assertEqual(self.engine.closed, [])  # still waiting
        self.assertEqual(self.playback.plays, [])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.notices, [LOADING])

    def test_the_extended_wait_is_bounded(self):
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=250)
        sid = self.start()
        self.engine.receive(sid, 188 * 20)
        self.wait_until(lambda: self.notices)
        self.assertEqual(self.engine.closed, [])
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [LOADING, UNAVAILABLE])

    def test_the_next_channel_waits_only_the_ready_timeout_again(self):
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=250)
        sid = self.start()
        self.engine.receive(sid, 188 * 20)
        self.wait_until(lambda: self.engine.closed)
        self.engine.answer()
        started = time.monotonic()
        sid = self.start()  # the next channel sends nothing
        self.wait_until(lambda: sid in self.engine.closed)
        self.assertLess(time.monotonic() - started, 0.2)  # not the extended 0.25 s
        self.assertEqual(self.notices[-1], UNAVAILABLE)

    def test_a_fallback_plays_directly_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        sid = self.start()
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
            self.assertEqual(self.playback.plays, [])
            self.wait_until(lambda: self.playback.plays)
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.engine.answer()  # a late confirmation changes nothing
        self.assertEqual(len(self.playback.plays), 1)

    def test_a_disk_failure_before_ready_keeps_waiting(self):
        sid = self.start(disk=True)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.engine.closed, [])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_late_ready_after_stop_is_ignored(self):
        sid = self.start()
        self.live.stop()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.modes, [])

    def test_zapping_waits_for_the_old_session_to_close(self):
        first = self.start()
        self.start()
        self.assertEqual(self.engine.closed, [first])
        self.assertEqual(len(self.engine.opened), 1)  # the new one opens after the close
        self.engine.buffer(first, [], open_seq=0)
        self.engine.sessionReady.emit(first)
        self.engine.sessionFailed.emit(first, "network", "The stream could not be reached.")
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.releases, [])
        self.engine.answer()
        second = self.engine.last_sid
        self.assertEqual(self.engine.events, [("open", first), ("released",), ("open", second)])
        self.engine.buffer(second, [], open_seq=0)
        self.engine.sessionReady.emit(second)
        self.assertEqual(self.playback.plays, [self.stream(second, 0)])

    def test_zapping_opens_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        self.start()
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.start()
            self.wait_until(lambda: len(self.engine.opened) == 2)
        self.assertEqual(self.engine.events[1:], [("released",), ("open", "s2")])

    def test_start_resets_the_position(self):
        self.ready(segments(5, 3, start=10.0), live_edge=16.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.live.stop()
        self.engine.answer()
        self.start()
        self.assertEqual(self.live.position(), 0.0)


class ReleaseTests(LiveSessionCase):
    def test_released_follows_session_closed(self):
        sid = self.ready(segments(0, 2))
        self.live.stop()
        self.assertEqual(self.playback.stops, 1)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.releases, [])
        self.engine.sessionClosed.emit("other")
        self.assertEqual(self.releases, [])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.engine.sessionClosed.emit(sid)
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.live.mode, "")
        self.assertEqual(self.active_timers(), [])

    def test_released_after_the_close_cap(self):
        self.patch("_CLOSE_CAP_MS", 50)
        self.ready(segments(0, 2))
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.live.stop()
            self.wait_until(lambda: self.releases)
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])

    def test_direct_mode_releases_right_after_stopping_playback(self):
        self.engine.lose()
        self.start()
        stops = []
        self.live.released.connect(lambda: stops.append(self.playback.stops))
        self.live.stop()
        self.assertEqual(stops, [1])
        self.live.stop()  # idle: nothing more
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])

    def test_a_stop_during_the_fallback_wait_releases_on_that_close(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.live.stop()
        self.assertEqual(self.releases, [])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(len(self.playback.plays), 1)  # the fallback was cancelled
        self.assertEqual(self.notices, [])
        self.assertEqual(self.active_timers(), [])

    def test_after_a_completed_fallback_stop_releases_at_once(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.answer()
        self.assertEqual(self.active_timers(), [])
        self.live.stop()
        self.assertEqual(self.releases, [True])

    def test_timers_are_idle_after_a_fallback(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.engine.answer()
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.active_timers(), [])


class SeekTests(LiveSessionCase):
    def test_seek_maps_to_the_segment_and_keeps_the_pause(self):
        sid = self.ready(segments(0, 10), live_edge=20.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.assertEqual(self.playback.paused, [True])
        self.playback.set_state(PlaybackState.PAUSED)

        self.live.seek_to(5.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 2))
        self.assertEqual(self.live.position(), 4.0)
        self.playback.set_state(PlaybackState.PLAYING)  # the reopened stream starts
        self.assertEqual(self.playback.paused, [True, True])

        self.live.seek_to(9.0)  # a second seek is still paused
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True, True, True])

        self.step(0.25, media=0)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")
        self.assertTrue(self.timelines[-1]["paused"])

    def test_seek_relative_moves_from_the_position(self):
        sid = self.ready(segments(0, 10), live_edge=20.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.step(1.0)
        self.step(1.0)  # 3.0
        self.live.seek_relative(10.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 6))
        self.assertEqual(self.live.position(), 12.0)
        self.live.seek_relative(-10.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 1))

    def test_seek_clamps_to_the_closed_segments(self):
        # A 10 s open segment follows the closed ones: it is not addressable.
        sid = self.ready(segments(3, 3, start=6.0), live_edge=22.0)
        self.live.seek_to(-50.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 3))
        self.live.seek_to(15.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))

    def test_seek_with_nothing_closed_yet_is_ignored(self):
        self.ready([], open_seq=0, live_edge=10.0)
        self.live.seek_to(0.0)
        self.assertEqual(len(self.playback.plays), 1)

    def test_seeking_back_leaves_live(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")

    def test_a_seek_near_live_goes_live(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.live.seek_to(18.0)  # within the 2.5 s target of the edge
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")
        self.live.seek_to(4.0)
        self.live.seek_relative(30.0)  # past the edge
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")

    def test_go_live_picks_the_start_nearest_the_target_once_vlc_plays(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(3.0)
        self.live.set_speed(1.5)
        self.assertEqual(self.playback.rates, [1.5])
        self.live.go_live()
        # Normal (2.5 s): segment 9 starts 2.0 s behind 20.0, about 2.35 s once VLC opened
        # it (the instant open above brought its open time from 0.5 to 0.35 s); 8: 4.35 s.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))
        self.assertEqual(self.playback.rates, [1.5, 1.0])
        self.assertEqual(self.live.position(), 18.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # back in LIVE, on target: no nudge
        self.assertEqual(self.timelines[-1]["mode"], "live")
        self.assertEqual(self.playback.rates, [1.5, 1.0])

    def test_go_live_with_bursts_aims_the_mean_lag_at_the_target(self):
        sid = self.ready(segments(0, 10), live_edge=20.0, burst=6.0)
        # A burst just landed: the mean lag is the current one less half a burst. Starts
        # 10 and 12 give means of 7 and 5: 5 is under the 5.75 s floor, and 7 is the
        # nearest above stay-live's band (5.75-6.75): "behind live" for a moment.
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))
        self.clock.now += 3.0  # halfway to the next burst: the current lag is the mean
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 7))  # 6.0: at once

    def test_go_live_with_bursts_keeps_headroom_before_the_next_burst(self):
        sid = self.ready(segments(5, 2, start=10.0), live_edge=14.0, burst=6.0)
        self.live.go_live()  # neither start reaches the 5.75 s floor: the most buffer
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))

    def test_go_live_takes_the_most_buffer_when_none_is_far_enough_behind(self):
        sid = self.ready(segments(5, 2, start=10.0), live_edge=14.0)
        self.live.set_latency("stable")
        self.live.go_live()  # 4.5 s and 2.5 s once playing, both under 5.75 s: the oldest
        self.assertEqual(self.playback.plays[-1][0], self.engine.stream_url(sid, 5))

    def test_raw_ts_go_live_lands_within_stay_lives_band(self):
        # Normal: 2.25-3.25 s behind. 0.3 s short of the target still counts as at it.
        sid = self.ready(segments(0, 10), live_edge=20.3)
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))  # 2.3 s behind
        # The open segment is a start too: from 20.0, 2.5 s behind 22.5.
        self.engine.buffer(sid, segments(0, 10), live_edge=22.5, open_seq=10)
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))

    def test_go_live_counts_vlcs_open_time_seeded_at_half_a_second(self):
        # The edge moves on while VLC opens: from 18.0, 2.0 s behind 20.0 now, 2.5 s once
        # playing (on target); without the open time, 16.0 (4.5 s) would win.
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))

    def test_go_live_learns_how_long_vlc_takes_to_open(self):
        sid = self.ready(segments(0, 10), live_edge=21.2, open_seq=10)
        for _ in range(10):  # every open takes 1.5 s from play() to Playing
            self.live.go_live()
            self.clock.now += 1.5
            self.playback.set_state(PlaybackState.PLAYING)
        self.engine.buffer(sid, segments(0, 10), live_edge=21.2, open_seq=10)
        self.live.go_live()
        # From 20.0: 1.2 s behind now, about 2.7 s once it plays; from 18.0 it would be 4.7.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))

    def test_a_resume_is_no_open(self):
        sid = self.ready(segments(0, 10), live_edge=21.0, open_seq=10)
        self.clock.now += 0.5
        self.playback.set_state(PlaybackState.PLAYING)  # the open took the seed's 0.5 s
        for _ in range(10):  # Playing again 30 s after each pause: not an open
            self.live.toggle_pause()
            self.clock.now += 30.0
            self.live.toggle_pause()
        self.live.go_live()
        # From 20.0 (1.0 s): 1.5 s once playing, under the floor; from 18.0: 3.5 s.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))

    def test_a_slow_open_counts_for_two_seconds_at_most(self):
        sid = self.ready(segments(0, 10), live_edge=20.0, open_seq=10)
        for _ in range(10):  # a stalled source: Playing a minute after play()
            self.live.go_live()
            self.clock.now += 60.0
            self.playback.set_state(PlaybackState.PLAYING)
        self.engine.buffer(sid, segments(0, 10), live_edge=20.0, open_seq=10)
        self.live.go_live()  # 2.0 + 2 s: 18.0 lands 4 s behind, not 20.0 for a minute's open
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))

    def test_a_seek_near_live_keeps_the_pause(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.live.seek_to(19.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True, True])  # paused again at the edge
        self.step(0.25, media=0)
        self.assertTrue(self.timelines[-1]["paused"])
        self.assertEqual(self.timelines[-1]["mode"], "shifted")  # only Go live sets LIVE

    def test_go_live_resumes_a_paused_session(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.live.go_live()
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])  # not paused again
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 9))
        self.step(0.25)
        self.assertFalse(self.timelines[-1]["paused"])

    def test_speed_while_shifted_catches_up(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(2.0)
        self.live.set_speed(1.5)
        self.step(0.25, media=0.375)
        self.assertEqual(self.timelines[-1]["mode"], "catching_up")
        self.assertEqual(self.timelines[-1]["rate"], 1.5)
        self.assertEqual(self.playback.rates, [1.5])

    def test_a_pause_ends_a_catch_up_at_normal_speed(self):
        self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(2.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.set_speed(1.5)
        self.step(0.25, media=0.375)
        self.assertEqual(self.timelines[-1]["mode"], "catching_up")
        self.live.toggle_pause()
        self.assertEqual(self.playback.rates, [1.5, 1.0])
        self.step(0.25, media=0)
        self.assertEqual((self.timelines[-1]["mode"], self.timelines[-1]["rate"]), ("shifted", 1.0))
        self.live.toggle_pause()  # resumes at 1x, time-shifted
        self.step(0.25)
        self.assertEqual(self.playback.rates, [1.5, 1.0])
        self.assertEqual((self.timelines[-1]["mode"], self.timelines[-1]["rate"]), ("shifted", 1.0))

    def test_a_pause_while_waiting_applies_once_it_plays(self):
        sid = self.start()
        self.live.toggle_pause()
        self.engine.buffer(sid, segments(0, 3), live_edge=6.0)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])
        self.assertIs(self.playback.current, PlaybackState.PAUSED)
        self.step(0.25, media=0)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")
        self.assertTrue(self.timelines[-1]["paused"])

    def test_a_pause_and_resume_while_waiting_plays(self):
        sid = self.start()
        self.live.toggle_pause()
        self.live.toggle_pause()
        self.engine.buffer(sid, segments(0, 3), live_edge=6.0)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [])
        self.step(0.25)
        self.assertEqual(self.timelines[-1]["mode"], "live")  # nothing was time-shifted

    def test_a_pause_while_a_zap_waits_for_the_old_close_applies(self):
        self.start()
        sid = self.start()  # the old session is still closing: the new one waits
        self.assertEqual(len(self.engine.opened), 1)
        self.live.toggle_pause()
        self.engine.answer()
        sid = self.engine.last_sid
        self.engine.buffer(sid, segments(0, 3), live_edge=6.0)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertIs(self.playback.current, PlaybackState.PAUSED)
        self.step(0.25, media=0)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")

    def test_speed_at_live_is_ignored(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.live.set_latency("stable")  # 6 s: stay-live leaves the rate alone at 4 s
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.set_speed(1.5)
        self.assertEqual(self.playback.rates, [])
        self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["mode"], "live")

    def test_direct_mode_passes_pause_through_and_ignores_seeks(self):
        self.engine.lose()
        self.start()
        self.live.toggle_pause()
        self.live.seek_to(5.0)
        self.live.set_speed(2.0)
        self.live.go_live()
        self.assertEqual(self.playback.toggles, 1)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(self.playback.rates, [])


class StayLiveTests(LiveSessionCase):
    def test_lag_above_target_nudges_the_rate_then_settles(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # position 0.25, lag 3.75 > 2.5 + 0.75
        self.assertEqual(self.playback.rates, [1.05])
        self.step(0.5, media=0.75)  # lag 3.0: inside the hysteresis band, keep 1.05
        self.assertEqual(self.playback.rates, [1.05])
        self.step(0.5, media=0.6)  # lag 2.4: within 0.2 s of the target
        self.assertEqual(self.playback.rates, [1.05, 1.0])
        rates = [timeline["rate"] for timeline in self.timelines]
        self.assertEqual(rates, [1.05, 1.05, 1.0])
        last = self.timelines[-1]
        self.assertAlmostEqual(last["position"], 1.6)
        self.assertAlmostEqual(last["lag"], 2.4)
        self.assertEqual(last["live_edge"], 4.0)
        self.assertEqual((last["start"], last["end"]), (0.0, 4.0))
        self.assertEqual(last["mode"], "live")
        self.assertFalse(last["paused"])
        self.assertFalse(last["ended"])
        self.assertEqual(last["target"], 2.5)

    def test_hls_bursts_raise_the_target(self):
        self.ready(segments(0, 2), live_edge=4.0, burst=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # lag 3.75 is under the 6 s burst: no nudge
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["target"], 6.0)

    def test_hls_bursts_settle_without_rate_flapping(self):
        sid = self.start()
        feed = Feed(self, sid, burst=6.0, edge=12.0).ready().run(120.0)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        rates = self.playback.rates
        self.assertEqual(rates, sorted(rates, reverse=True))  # only ever down
        self.assertEqual(rates[-1], 1.0)
        self.assertGreater(min(feed.before[-4:]), 2.0)  # headroom before every burst
        reported = [timeline["lag"] for timeline in self.timelines[-24:]]
        self.assertLess(max(reported) - min(reported), 0.5)  # the mean, not the sawtooth

    def test_hls_bursts_settle_after_reopening_at_live(self):
        sid = self.start()
        feed = Feed(self, sid, burst=6.0, edge=60.0).ready().run(120.0)  # 60 s behind
        self.assertEqual(len(self.playback.plays), 2)  # one reopen at live, no loop
        rates = self.playback.rates
        self.assertEqual(rates, sorted(rates, reverse=True))  # only ever down, if at all
        self.assertGreater(min(feed.before[-4:]), 2.0)

    def go_live_at_every_phase(self, burst, latency, *, step=0.5, jitter=0.0, seconds=60.0):
        """Go live at points through a burst (GOPs as long as a burst, the newest open).

        Per phase: the seconds the transport would show "behind live" (lag at least the
        target plus 1 s), the least lag before a burst, the rates set, the plays after Go
        live, and the target.
        """
        rows = []
        for phase in [i * step for i in range(int(burst / step))]:
            self.live = self.make()
            sid = self.start(latency=latency)
            feed = Feed(self, sid, burst=burst, gop=burst, jitter=jitter).ready()
            feed.run(0.25)
            self.live.seek_to(20.0)
            feed.run(3 * burst + phase)
            plays, rates, timelines = (
                len(self.playback.plays),
                len(self.playback.rates),
                len(self.timelines),
            )
            arrivals = len(feed.before)
            self.live.go_live()
            feed.sync()
            feed.run(seconds)
            target = self.timelines[-1]["target"]
            after = self.timelines[timelines:]
            rows.append(
                (
                    phase,
                    0.25 * sum(1 for t in after if t["lag"] >= target + 1.0),
                    min(feed.before[arrivals:]),
                    self.playback.rates[rates:],
                    len(self.playback.plays) - plays,
                    target,
                )
            )
            self.live.stop()
            self.engine.answer()
        return rows

    def test_go_live_on_bursts_keeps_the_buffer_at_every_phase(self):
        """GOPs of 2-6 s arriving whole: never below the target less a quarter second."""
        for burst in (2.0, 3.0, 4.0, 6.0):
            for latency in ("low", "normal", "stable"):
                rows = self.go_live_at_every_phase(burst, latency)
                for phase, behind, before, rates, plays, target in rows:
                    with self.subTest(burst=burst, latency=latency, phase=phase):
                        self.assertGreaterEqual(before, max(0.5, target - burst / 2 - 0.25))
                        self.assertEqual(plays, 1)
                        self.assertLessEqual(reversals(rates), 1)

    def test_go_live_on_jittery_bursts_never_stalls(self):
        """Arrivals up to a second early or late: the landing leaves room for them."""
        for burst in (2.0, 3.0, 4.0, 6.0):
            for latency in ("low", "normal", "stable"):
                if max(TARGETS[latency], burst) - burst / 2 - 0.25 <= 1.0:
                    continue  # Low at 2 s bursts: its target leaves under a second
                rows = self.go_live_at_every_phase(
                    burst, latency, step=1.0, jitter=1.0, seconds=120.0
                )
                for phase, _, before, _, _, _ in rows:
                    with self.subTest(burst=burst, latency=latency, phase=phase):
                        self.assertGreater(before, 0.0)

    def test_go_live_can_start_at_the_open_segment(self):
        sid = self.ready(segments(0, 10), live_edge=23.0, open_seq=10, burst=2.0)
        self.clock.now += 0.5  # half a second after the last burst landed
        # The open segment (from 20.0) gives a mean lag of 3.0 + 0.5 - 1.0 = 2.5: the target.
        self.live.go_live()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))
        self.assertEqual(self.live.position(), 20.0)

    def test_paused_sessions_stay_time_shifted(self):
        """No stay-live while paused, whatever reopened the stream; no catch-up after."""
        for burst in (0.0, 6.0):
            for scenario in ("near-live seek", "recovery", "disk failure"):
                with self.subTest(burst=burst, scenario=scenario):
                    self.live = self.make()
                    sid = self.start()
                    feed = Feed(self, sid, burst=burst).ready()
                    feed.run(1.0)
                    self.live.seek_to(30.0)
                    feed.run(1.0)
                    self.live.toggle_pause()
                    feed.run(1.0)
                    if scenario == "near-live seek":
                        self.live.seek_to(feed.edge - 0.5)
                    elif scenario == "recovery":
                        self.engine.up = False
                        self.engine.reset(sid)
                        self.engine.sessionFailed.emit(sid, "internal", "The helper died.")
                        feed.run(1.0)
                        self.engine.up = True
                        self.engine.available.emit(True)
                        feed.push()  # the reopened session's first state
                        self.engine.sessionReady.emit(sid)
                    else:
                        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
                        feed.push()  # the RAM ring's first state
                    feed.sync()
                    plays, rates = len(self.playback.plays), len(self.playback.rates)
                    feed.run(90.0)
                    self.assertEqual(len(self.playback.plays), plays)  # no reopen at live
                    self.assertEqual(len(self.playback.rates), rates)  # no rate while paused
                    self.assertEqual(self.timelines[-1]["mode"], "shifted")
                    self.assertTrue(self.timelines[-1]["paused"])
                    lag = self.timelines[-1]["lag"]
                    self.live.toggle_pause()
                    feed.run(20.0)
                    self.assertEqual(len(self.playback.rates), rates)  # no catch-up
                    self.assertEqual(self.timelines[-1]["mode"], "shifted")
                    self.assertAlmostEqual(self.timelines[-1]["lag"], lag, delta=0.5)
                    self.live.go_live()
                    feed.sync()
                    feed.run(1.0)
                    self.assertEqual(self.timelines[-1]["mode"], "live")
                    self.assertFalse(self.timelines[-1]["paused"])

    def test_catching_up_on_bursts_never_chatters(self):
        """The review's chatter cases: from a far start, one way down to 1.0."""
        cases = [(6.0, 12.0, 5.0, 0.0, 1), (10.0, 40.0, 4.5, 0.0, 1)]  # burst, edge, first
        cases += [
            (burst, edge, first, 1.0, seed)  # arrivals jittered by up to a second
            for burst in (2.0, 6.0)
            for edge in (burst * 2 + 1, 16.0)
            for first in (0.5, burst - 0.25)
            for seed in (1, 2, 3)
        ]
        for burst, edge, first, jitter, seed in cases:
            with self.subTest(burst=burst, edge=edge, first=first, jitter=jitter, seed=seed):
                self.live = self.make()
                sid = self.start(latency="stable")
                feed = Feed(self, sid, burst=burst, edge=edge, first=first, jitter=jitter)
                feed.rng.seed(seed)
                feed.ready().run(120.0)
                self.assertLessEqual(reversals(self.playback.rates), 1)
                self.assertGreater(min(feed.before[5:]), 0.0)  # no underrun

    def test_bursty_lag_follows_a_pause_without_delay(self):
        sid = self.ready(segments(0, 30), live_edge=60.0, burst=6.0)
        self.live.seek_to(40.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.live.toggle_pause()
        self.playback.set_state(PlaybackState.PAUSED)
        before = self.timelines[-1]["lag"]
        edge = 60.0
        for tick in range(1, 49):  # 12 s paused, two bursts landing
            if tick % 24 == 0:
                edge += 6.0
                self.engine.buffer(sid, segments(0, int(edge // 2)), live_edge=edge, burst=6.0)
            self.step(0.25, media=0)
        # Paused, the mean lag grows with the wall clock, the transport's "behind live" too.
        self.assertAlmostEqual(self.timelines[-1]["lag"], before + 12.0, delta=0.3)

    def test_raw_ts_bursts_once_measured_are_handled_as_hls_bursts(self):
        """The owner's channel (2.0.1, L1): raw TS, 10 s at once every 10 s, measured by the
        helper from the second burst on. From then on: no stall shown between bursts, the
        burst is the target, no catch-up into the next gap, and Go live keeps a burst's room."""
        sid = self.start()
        feed = Feed(self, sid, burst=10.0, edge=12.0, measured_after=2).ready()
        feed.run(10.5)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"])  # not measured yet
        feed.run(10.0)  # the second burst landed: measured
        timelines, rates = len(self.timelines), len(self.playback.rates)
        feed.run(60.0)
        after = self.timelines[timelines:]
        self.assertTrue(all(state["target"] == 10.0 for state in after))
        self.assertFalse(any(state["stalled"] for state in after))
        self.assertTrue(all(rate <= 1.0 for rate in self.playback.rates[rates:]))
        self.assertEqual(len(self.playback.plays), 1)  # nothing reopened
        self.assertEqual(feed.starved_s, 0.0)
        arrivals = len(feed.before)
        self.live.go_live()
        feed.sync()
        feed.run(60.0)
        self.assertEqual(len(self.playback.plays), 2)
        # The mean lag lands at the target: about half a burst of room before every landing
        # (less the open time Go live allows for: this VLC opens at once).
        self.assertGreater(min(feed.before[arrivals + 1 :]), 10.0 / 2 - 0.25 - 0.5)
        self.assertFalse(any(state["stalled"] for state in self.timelines[timelines:]))
        self.assertEqual(feed.starved_s, 0.0)

    def test_a_source_bursty_again_after_steady_eases_afresh(self):
        """A raw TS source's measured bursts may stop and come back (2.0.1, L1)."""
        sid = self.ready(segments(0, 3), live_edge=6.0, burst=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.engine.buffer(sid, segments(0, 6), live_edge=12.0, burst=6.0)
        self.step(0.25)  # the landing's step is eased in: a correction builds up
        self.engine.buffer(sid, segments(0, 6), live_edge=12.0, burst=0.0)  # steady now
        for _ in range(8):
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen, which would start afresh
        self.engine.buffer(sid, segments(0, 9), live_edge=18.0, burst=6.0)  # bursty again
        self.step(0.25)
        position = self.timelines[-1]["position"]
        # The mean over a burst, with no correction left over from the earlier bursts.
        self.assertAlmostEqual(self.timelines[-1]["lag"], 18.0 - position + 0.25 - 3.0)

    def test_no_rate_change_while_buffering(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.BUFFERING)
        self.step(0.25, media=0)
        self.assertEqual(self.playback.rates, [])

    def test_far_behind_reopens_at_live_once(self):
        sid = self.ready(segments(0, 20), live_edge=40.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # lag 39.75 > 2.5 + 20
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 19))
        self.playback.set_state(PlaybackState.PLAYING)
        for _ in range(8):
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 2)

    def test_latency_change_moves_the_target(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.live.set_latency("stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["target"], 6.0)


class StallTests(LiveSessionCase):
    """The source stops sending at the live edge; libVLC 3 keeps reporting Playing meanwhile.

    The review's scenarios (6 s HLS bursts, Normal): stalls of 15, 30 and 60 s.
    """

    def settle(self, *, burst=6.0):
        sid = self.start()
        feed = Feed(self, sid, burst=burst, edge=12.0, starved=PlaybackState.PLAYING).ready()
        feed.run(60.0)
        self.assertEqual(self.playback.rates[-1:], [1.0] if self.playback.rates else [])
        return feed

    def test_a_stall_at_the_edge_reopens_nothing_and_does_not_catch_up_after(self):
        for seconds in (15.0, 30.0, 60.0):
            with self.subTest(stall=seconds):
                self.live = self.make()
                feed = self.settle()
                plays, timelines = len(self.playback.plays), len(self.timelines)
                feed.stalled = True
                feed.run(seconds)
                during = self.timelines[timelines:]
                self.assertEqual(len(self.playback.plays), plays)  # no reopen storm
                # Shown as a stall once the edge is a burst plus 2 s late, never before.
                late = [state["stalled"] for state in during[4 * 9 :]]
                self.assertTrue(all(late))
                self.assertFalse(any(state["stalled"] for state in during[: 4 * 8]))
                self.assertLess(max(state["lag"] for state in during), 12.0)
                feed.stalled = False
                rates, timelines = len(self.playback.rates), len(self.timelines)
                feed.run(6.5)  # the first burst after the stall has landed
                self.assertFalse(self.timelines[-1]["stalled"])
                starved = feed.starved_s
                feed.run(30.0)
                self.assertEqual(len(self.playback.plays), plays)
                after = self.playback.rates[rates:]
                self.assertTrue(all(rate <= 1.0 for rate in after), after)  # no catch-up
                self.assertEqual(self.timelines[-1]["rate"], 1.0)
                self.assertLess(feed.starved_s - starved, 0.5)  # no underrun at every burst
                reported = [state["lag"] for state in self.timelines[timelines + 4 * 7 :]]
                self.assertLess(max(reported), 7.0)  # not "59 s behind source" at the edge

    def test_a_raw_ts_stall_is_shown_without_nudging_or_reopening(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=30.0, starved=PlaybackState.PLAYING).ready()
        self.live.go_live()
        feed.sync()
        feed.run(20.0)
        plays, rates, timelines = (
            len(self.playback.plays),
            len(self.playback.rates),
            len(self.timelines),
        )
        feed.stalled = True
        feed.run(40.0)
        during = self.timelines[timelines:]
        self.assertFalse(any(state["stalled"] for state in during[:7]))  # up to 2 s: a hiccup
        self.assertTrue(all(state["stalled"] for state in during[9:]))
        self.assertEqual(len(self.playback.plays), plays)
        self.assertTrue(all(rate <= 1.0 for rate in self.playback.rates[rates:]))  # no nudge
        feed.stalled = False
        feed.run(1.0)
        self.assertFalse(self.timelines[-1]["stalled"])
        self.assertEqual(len(self.playback.plays), plays)

    def test_a_frozen_vlc_clock_counts_as_buffering(self):
        sid = self.ready(segments(0, 2), live_edge=4.0)
        self.live.set_latency("stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # 3.75 s behind a 6 s target: nothing to do
        self.step(0.25, media=0)
        self.step(0.25, media=0)  # VLC says Playing, but its clock stood still for 0.5 s
        self.engine.buffer(sid, segments(0, 20), live_edge=40.0)  # the edge moves on
        self.step(0.25, media=0)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(len(self.playback.plays), 1)  # no reopen at live either
        self.assertFalse(self.timelines[-1]["stalled"])  # the source is fine
        self.step(0.25)  # the clock moves again: 39 s behind, so back to live
        self.assertEqual(len(self.playback.plays), 2)

    def test_the_timeline_is_not_stalled_once_the_source_ended(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionEnded.emit(sid)
        for _ in range(12):
            self.step(0.25)
        self.assertFalse(self.timelines[-1]["stalled"])


class PositionTests(LiveSessionCase):
    def test_position_follows_vlc_time(self):
        self.ready(segments(5, 3, start=10.0), live_edge=16.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.step(0.25, media=0.3)
        self.assertAlmostEqual(self.live.position(), 10.55)

    def test_a_vlc_time_jump_falls_back_to_wall_clock(self):
        # Stable's 6 s target: stay-live leaves the rate alone at this lag.
        self.ready(segments(0, 3), live_edge=6.0, latency="stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.assertAlmostEqual(self.live.position(), 0.25)
        self.step(0.25, media=30.25)  # get_time jumps by 30 s
        self.assertAlmostEqual(self.live.position(), 0.5)
        self.step(0.25)
        self.assertAlmostEqual(self.live.position(), 0.75)
        self.assertAlmostEqual(self.timelines[-1]["position"], 0.75)

    def test_wall_clock_counts_the_rate(self):
        self.ready(segments(0, 30), live_edge=60.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(10.0)
        self.live.set_speed(2.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0, media=40.0)
        self.assertAlmostEqual(self.live.position(), 12.0)

    def test_a_paused_player_does_not_advance(self):
        self.ready(segments(0, 30), live_edge=60.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.playback.set_state(PlaybackState.PAUSED)
        self.step(30.0, media=0)
        self.assertEqual(self.live.position(), 0.0)


class InterruptionTests(LiveSessionCase):
    def pause_past_the_ring(self, sid):
        """Pause for 5 minutes, while the ring moves on to 300-312 s."""
        self.live.toggle_pause()
        self.playback.set_state(PlaybackState.PAUSED)
        self.step(300.0, media=0)
        self.engine.buffer(sid, segments(150, 6, start=300.0), live_edge=312.5)

    def test_a_long_pause_past_the_ring_rewinds_a_margin_past_the_oldest(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.pause_past_the_ring(sid)
        self.live.toggle_pause()
        # 5 s past the oldest point, so the next eviction does not beat VLC's request.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 152))
        self.assertEqual(self.notices, [REWOUND])
        self.assertEqual(self.live.position(), 304.0)
        self.assertEqual(self.playback.paused, [True])  # reopened, not unpaused in place
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])

    def test_a_resume_inside_the_buffer_unpauses_in_place(self):
        self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.live.toggle_pause()
        self.assertEqual(self.playback.paused, [True, False])
        self.assertEqual(len(self.playback.plays), 1)

    def test_an_ended_stream_past_the_ring_reopens_a_margin_past_the_oldest(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.engine.buffer(sid, segments(50, 6, start=100.0), live_edge=112.0)
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 52))
        self.assertEqual(self.notices, [REWOUND])

    def test_a_short_ring_rewinds_to_its_newest_closed_segment_at_most(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.engine.buffer(sid, segments(50, 2, start=100.0), live_edge=104.5)
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 51))

    def test_a_rewind_is_said_once_per_resume(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.pause_past_the_ring(sid)
        self.live.toggle_pause()
        # The ring moved on again before VLC's request: a 404, and the retry rewinds too.
        self.engine.buffer(sid, segments(153, 6, start=306.0), live_edge=318.5)
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 155))
        self.assertEqual(self.notices, [REWOUND])
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.live.toggle_pause()  # a second long pause is news again
        self.playback.set_state(PlaybackState.PAUSED)
        self.step(300.0, media=0)
        self.engine.buffer(sid, segments(300, 6, start=600.0), live_edge=612.5)
        self.live.toggle_pause()
        self.assertEqual(self.notices, [REWOUND, REWOUND])

    def test_an_error_inside_the_buffer_reopens_at_the_position(self):
        sid = self.ready(segments(0, 5), live_edge=10.5)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        self.step(1.0)
        self.step(1.0)  # 3.0: segment 1
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 1))
        self.assertEqual(self.notices, [])
        self.assertEqual(self.failures, [])

    def test_errors_without_playing_end_in_direct_play(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 5), live_edge=10.5)
        with self.assertLogs("services.live.live_session", "WARNING"):
            for _ in range(3):
                self.clock.now += 0.6
                self.playback.errorOccurred.emit("The stream could not be opened")
        self.assert_falls_back(sid, LOST, 3)  # ready, two reopens
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])

    def test_a_synchronous_open_error_retries_on_the_tick_without_recursion(self):
        self.live = self.make(ready_timeout_ms=1000)
        self.playback.fail_on_play = True
        sid = self.ready(segments(0, 5), live_edge=10.5)
        self.assertEqual(len(self.playback.plays), 1)
        self.step(0.5)
        self.assertEqual(len(self.playback.plays), 2)
        timelines = len(self.timelines)
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(1.1)  # over a second of failures since the first retry
        self.assertEqual(len(self.timelines), timelines)  # nothing after the fallback
        self.playback.fail_on_play = False
        self.assert_falls_back(sid, LOST, 2)

    def test_after_the_session_ended_an_error_does_not_reopen(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionEnded.emit(sid)
        self.engine.reset(sid)  # a helper death took the ended buffer
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(len(self.playback.plays), 1)
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.engine.closed, [])
        self.step(0.25, media=0)
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.timelines[-1]["live_edge"], 6.0)

    def test_a_disk_failure_after_ready_goes_live_on_the_ram_ring(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(2.0)
        # The failure arrives before any state of the swapped-in RAM ring.
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(len(self.playback.plays), 2)
        self.playback.errorOccurred.emit("The stream could not be played")  # cut off
        self.assertEqual(len(self.playback.plays), 2)  # not reopened on the old spans
        self.engine.buffer(sid, segments(10, 1, start=20.0), live_edge=23.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))
        self.assertEqual(self.notices, [DISK])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_disk_failure_without_a_new_state_falls_back(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.step(0.25)
        self.step(1.5)  # past the ready timeout, but under the 2 s floor
        self.assertEqual(self.engine.closed, [])
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(0.6)
        self.assert_falls_back(sid, LOST, 1)

    def test_a_pause_does_not_extend_the_disk_deferral(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.step(0.25)
        self.step(1.5)
        self.live.toggle_pause()  # VLC reports "paused"
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(0.5)  # 2.25 s since the failure: past the 2 s bound
        self.assert_falls_back(sid, LOST, 1)

    def test_an_interruption_during_the_disk_deferral_waits_for_its_bound(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.step(0.25)
        self.step(1.1)  # over the 1 s ready timeout, within the 2 s deferral bound
        self.playback.errorOccurred.emit("The stream could not be played")  # cut off
        self.assertEqual(self.engine.closed, [])
        self.engine.buffer(sid, segments(10, 3, start=20.0), live_edge=26.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 12))

    def test_the_disk_deferral_waits_a_burst_for_hls(self):
        self.live = self.make(ready_timeout_ms=1000)
        sid = self.ready(segments(0, 10), live_edge=20.0, burst=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.step(0.25)
        self.step(7.5)  # the next burst may take 6 s to bring the RAM ring's first state
        self.assertEqual(self.engine.closed, [])
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.step(0.6)
        self.assert_falls_back(sid, LOST, 1)

    def test_user_actions_during_the_disk_deferral_wait_for_the_fresh_state(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.toggle_pause()
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.live.seek_to(6.0)
        self.live.toggle_pause()  # resume
        self.assertEqual(self.playback.paused, [True])
        self.assertEqual(len(self.playback.plays), 1)  # nothing on the lost disk ring
        self.engine.buffer(sid, segments(10, 3, start=20.0), live_edge=26.0)
        # The seek runs on the RAM ring: 6.0 is gone, so its oldest point, playing.
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 10))
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertEqual(self.playback.paused, [True])

    def test_go_live_during_the_disk_deferral_waits_for_the_fresh_state(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.live.go_live()
        self.assertEqual(len(self.playback.plays), 2)
        self.engine.buffer(sid, segments(10, 3, start=20.0), live_edge=26.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 12))

    def test_the_source_giving_up_before_the_ram_ring_reports_falls_back(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assert_falls_back(sid, LOST, 2)  # no history is left to play
        self.assertEqual(self.notices, [DISK, LOST])

    def test_the_source_giving_up_at_live_falls_back(self):
        for kind in ("network", "timeout", "http", "tls", "unsupported", "crashed", "new-kind"):
            with self.subTest(kind=kind):
                self.notices.clear()
                self.modes.clear()
                sid = self.ready(segments(0, 3), live_edge=6.0)
                self.playback.set_state(PlaybackState.PLAYING)
                self.step(0.25)  # lag 5.75: stay-live runs at 1.10
                self.assertEqual(self.playback.rates[-1], 1.10)
                plays = len(self.playback.plays)
                self.engine.sessionFailed.emit(sid, kind, "The stream could not be reached.")
                self.assert_falls_back(sid, LOST, plays)
                self.assertEqual(self.playback.rates[-1], 1.0)
                self.assertEqual(self.notices, [LOST])
                self.assertEqual(self.modes, ["pipeline", "direct"])
                self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped.")
                self.assertEqual(self.notices, [LOST])  # one fallback only

    def test_the_source_giving_up_while_shifted_plays_the_buffer_to_its_end(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        plays = len(self.playback.plays)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped.")  # ignored
        self.assertEqual(self.notices, [BUFFERED])
        self.assertTrue(self.timelines[-1]["ended"])
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(len(self.playback.plays), plays)
        self.live.seek_to(8.0)  # the buffer is still there
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.step(0.25)
        self.assertTrue(self.timelines[-1]["ended"])
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()  # the end of what was buffered
        self.assert_falls_back(sid, LOST, plays + 1)
        self.assertEqual(self.notices, [BUFFERED, LOST])

    def test_go_live_after_the_source_gave_up_falls_back(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "http", "The provider refused the stream.")
        self.assertEqual(self.notices, [BUFFERED])
        self.live.go_live()
        self.assert_falls_back(sid, LOST, 2)

    def test_a_session_crash_while_shifted_plays_the_buffer(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.live.seek_to(4.0)
        self.engine.sessionFailed.emit(sid, "crashed", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.notices, [BUFFERED])
        self.assertTrue(self.timelines[-1]["ended"])
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assert_falls_back(sid, LOST, 2)

    def test_direct_play_errors_are_reported(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.engine.answer()
        plays = len(self.playback.plays)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.failures, ["The stream could not be played"])
        self.assertEqual(len(self.playback.plays), plays)


class InputReleaseTests(LiveSessionCase):
    """A provider stream VLC played directly counts as released once libVLC let go of it."""

    def direct(self):
        self.engine.lose()
        self.start()
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.playback.holding = True

    def test_a_direct_channel_is_released_once_vlc_let_go_of_it(self):
        self.direct()
        self.live.stop()
        self.assertEqual(self.releases, [])  # VLC may still hold the provider connection
        self.playback.let_go()
        self.assertEqual(self.releases, [True])
        self.playback.let_go()
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])

    def test_the_wait_for_vlc_is_capped(self):
        self.patch("_CLOSE_CAP_MS", 50)
        self.direct()
        with self.assertLogs("services.live.live_session", "WARNING"):
            self.live.stop()
            self.wait_until(lambda: self.releases)
        self.assertEqual(self.active_timers(), [])

    def test_a_start_after_a_direct_channel_waits_for_vlc_to_let_go(self):
        self.direct()
        self.engine.up = True
        self.live.use_engine(self.engine)  # time-shift is back (a restarted engine)
        self.live.stop()
        self.start()
        self.assertEqual(self.engine.opened, [])  # the old provider stream is still open
        self.playback.let_go()
        self.assertEqual(len(self.engine.opened), 1)
        self.assertEqual(self.releases, [True])

    def test_a_pipeline_stop_does_not_wait_for_vlc(self):
        self.ready(segments(0, 2))  # VLC plays the loopback stream, not the provider
        self.playback.holding = True
        self.live.stop()
        self.engine.answer()
        self.assertEqual(self.releases, [True])


class EngineShutdownTests(LiveSessionCase):
    def test_after_the_engine_shut_down_nothing_waits_for_its_close(self):
        self.ready(segments(0, 2))
        self.live.stop()
        self.assertEqual(self.releases, [])
        with self.assertNoLogs("services.live.live_session", "WARNING"):
            self.live.engine_shut_down()
            QTest.qWait(10)
        self.assertEqual(self.releases, [True])  # at once, not after the close cap
        self.assertEqual(self.active_timers(), [])

    def test_starts_play_directly_until_another_engine_is_used(self):
        old = self.engine
        self.live.engine_shut_down()
        self.start()
        self.assertEqual(old.opened, [])
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.live.stop()
        self.engine = FakeEngine()
        self.live.use_engine(self.engine)
        sid = self.ready(segments(0, 2))
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 0))
        old.available.emit(False)  # the old engine is no longer heard
        old.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assertTrue(self.live.is_pipeline())
        self.live.stop()
        self.engine.answer()  # the cleanup answers only the first engine
        self.assertEqual(self.active_timers(), [])


class StreamLimitTests(LiveSessionCase):
    """The provider refused for its connection limit: playing directly would be refused too."""

    def assert_refused(self, sid, plays, message=STREAM_LIMIT):
        self.assertIn(sid, self.engine.closed)
        self.engine.answer()
        self.assertEqual(len(self.playback.plays), plays)  # nothing direct
        self.assertEqual(self.failures, [message])
        self.assertNotIn("direct", self.modes)
        self.assertFalse(self.live.is_pipeline())
        self.assertEqual(self.active_timers(), [])
        self.live.stop()
        self.assertEqual(self.releases, [True])

    def test_before_ready(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "http", STREAM_LIMIT)
        self.assertEqual(self.notices, [])  # not "Time-shift isn't available"
        self.assert_refused(sid, 0)

    def test_at_the_live_edge(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        stops = self.playback.stops
        self.engine.sessionFailed.emit(sid, "http", STREAM_LIMIT)
        self.assertEqual(self.playback.stops, stops + 1)
        self.assertEqual(self.modes, ["pipeline", ""])  # nothing plays any more
        self.assert_refused(sid, 1)

    def test_a_plain_403_is_reported_as_such(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "http", STREAM_REFUSED)
        self.assertEqual(self.modes, [])  # it never played
        self.assert_refused(sid, 0, STREAM_REFUSED)

    def test_while_shifted_the_buffer_plays_to_its_end_first(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        plays = len(self.playback.plays)
        self.engine.sessionFailed.emit(sid, "http", STREAM_LIMIT)
        self.assertEqual(self.notices, [BUFFERED])
        self.assertEqual(self.failures, [])
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()
        self.assertEqual(self.notices, [BUFFERED])
        self.assert_refused(sid, plays)

    def test_another_http_failure_still_plays_directly(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "http", "The channel could not be reached over HTTP.")
        self.assert_falls_back(sid, UNAVAILABLE, 0)


class HelperDeathTests(LiveSessionCase):
    def die(self, sid):
        self.engine.up = False
        self.engine.reset(sid)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")

    def test_helper_death_reopens_at_live_then_a_second_death_plays_directly(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        ticks = len(self.timelines)

        self.die(sid)
        self.playback.errorOccurred.emit("The stream could not be played")  # the dead server
        self.step(0.25)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(len(self.timelines), ticks)

        self.engine.up = True
        self.engine.available.emit(True)
        self.assertEqual(len(self.playback.plays), 1)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)  # the reopened session
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.modes, ["pipeline"])

        self.die(sid)
        self.engine.lose()  # no helper is left, so neither is an upstream: at once
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.active_timers(), [])

        self.engine.up = True  # never again in this run
        self.start()
        self.assertEqual(len(self.engine.opened), 1)
        self.assertEqual(self.notices, [LOST, UNAVAILABLE])

    def test_internal_always_waits_for_the_reopen(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.engine.closed, [])  # no is_available() guess: it is a death
        self.assertEqual(self.notices, [])
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))

    def test_internal_before_ready_waits_for_the_reopen(self):
        sid = self.start()
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.assertEqual(self.engine.closed, [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, [], open_seq=0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.notices, [])

    def test_go_live_during_recovery_applies_after_the_reopen(self):
        for paused_before_death in (True, False):
            with self.subTest(paused_before_death=paused_before_death):
                self.live = self.make()
                sid = self.ready(segments(0, 10), live_edge=20.0)
                self.playback.set_state(PlaybackState.PLAYING)
                self.live.seek_to(2.0)
                self.playback.set_state(PlaybackState.PLAYING)
                if paused_before_death:
                    self.live.toggle_pause()
                self.die(sid)
                if not paused_before_death:
                    self.live.toggle_pause()  # remembered
                self.live.go_live()  # remembered too: playing, at live
                self.engine.up = True
                self.engine.available.emit(True)
                self.engine.buffer(sid, segments(0, 12), live_edge=24.0)
                self.engine.sessionReady.emit(sid)
                self.playback.set_state(PlaybackState.PLAYING)
                self.assertIs(self.playback.current, PlaybackState.PLAYING)
                self.step(0.25)
                self.assertEqual(self.timelines[-1]["mode"], "live")
                self.assertFalse(self.timelines[-1]["paused"])

    def test_a_helper_death_while_time_shifted_says_the_buffer_is_lost(self):
        for how in ("seeked back", "paused"):
            with self.subTest(how=how):
                self.live = self.make()
                sid = self.ready(segments(0, 10), live_edge=20.0)
                self.playback.set_state(PlaybackState.PLAYING)
                if how == "paused":
                    self.live.toggle_pause()
                else:
                    self.live.seek_to(4.0)
                self.die(sid)
                self.assertEqual(self.notices, [LOST])  # the reopen will play live
                self.engine.up = True
                self.engine.available.emit(True)
                self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
                self.engine.sessionReady.emit(sid)
                self.assertEqual(self.notices, [LOST])

    def test_a_helper_death_at_the_live_edge_says_nothing(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.die(sid)
        self.assertEqual(self.notices, [])

    def test_a_pause_during_recovery_is_kept_after_the_reopen(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.die(sid)
        self.live.toggle_pause()  # nothing plays: remembered
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))
        self.playback.set_state(PlaybackState.PLAYING)
        self.assertIs(self.playback.current, PlaybackState.PAUSED)
        self.step(0.25, media=0)
        self.assertEqual(self.timelines[-1]["mode"], "shifted")
        self.assertTrue(self.timelines[-1]["paused"])

    def test_recovery_is_bounded(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)  # but the reopened session never gets ready
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_recovery_is_bounded_without_a_new_helper(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_a_reopen_that_fails_falls_back(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.sessionFailed.emit(sid, "network", "The stream could not be reached.")
        self.assert_falls_back(sid, LOST, 1)

    def test_a_crash_of_the_reopened_session_falls_back(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.sessionFailed.emit(sid, "crashed", "The live buffer stopped unexpectedly.")
        self.assert_falls_back(sid, LOST, 1)

    def test_internal_while_recovering_keeps_waiting_until_the_cap(self):
        self.patch("_RECOVERY_MS", 200)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.engine.up = True
        self.engine.available.emit(True)
        self.die(sid)  # the restarted helper died too: still waiting, but bounded
        self.assertEqual(self.engine.closed, [])
        self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, LOST, 1)

    def test_stop_while_recovering(self):
        self.patch("_RECOVERY_MS", 50)
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.die(sid)
        self.live.stop()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.answer()
        self.assertEqual(self.releases, [True])
        self.assertEqual(self.active_timers(), [])
        self.engine.up = True
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 5), live_edge=10.0)
        self.engine.sessionReady.emit(sid)
        QTest.qWait(100)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(self.notices, [])

    def test_an_ended_session_falls_back_when_the_helper_is_gone(self):
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.engine.sessionEnded.emit(sid)
        self.engine.lose()
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.active_timers(), [])

    def test_a_session_made_after_the_engine_gave_up_falls_back_at_once(self):
        self.engine.gave_up = True  # this LiveSession never saw available(False)
        self.engine.up = False
        sid = self.start()
        started = time.monotonic()
        self.wait_until(lambda: self.playback.plays, timeout=1.0)  # not the 6 s ready wait
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.engine.closed, [sid])
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.notices, [UNAVAILABLE])

    def test_engine_lost_during_the_wait_plays_directly_at_once(self):
        sid = self.start()
        self.engine.up = False
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer is not available.")
        self.assertEqual(self.playback.plays, [])  # waiting for the reopen
        self.engine.lose()
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertEqual(self.playback.plays, [DIRECT])
        self.assertEqual(self.active_timers(), [])

    def test_available_outside_recovery_is_harmless(self):
        sid = self.start()
        self.engine.available.emit(True)  # waiting
        self.engine.buffer(sid, segments(0, 3), live_edge=6.0)
        self.engine.sessionReady.emit(sid)
        self.engine.available.emit(True)  # playing
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.engine.answer()
        self.engine.available.emit(True)  # direct
        self.assertEqual(len(self.playback.plays), 2)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])


if __name__ == "__main__":
    unittest.main()
