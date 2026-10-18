import itertools
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

from services.live import learned_target, worker
from services.live.learned_target import ArrivalModel, Learned, LearnedTarget
import services.live.live_session as live_session
from services.live.live_session import LiveSession
from services.live.source import STREAM_LIMIT, STREAM_REFUSED
from services.live.stay_live import AUTO, TARGETS, Decision, StayLive
from services.live.timeline import Span
from services.live.tuning import LiveTuning, Tuning
from services.playback import PlaybackState
from services.vlc_profile import PlayKind
from ui.overlays.transport import Transport

app = QApplication.instance() or QApplication([])

URL = "http://provider.example/live/user/secret/42.ts"
UNAVAILABLE = "Time-shift isn't available for this channel"
LOST = "Lost the time-shift buffer; playing live"
BUFFERED = "Lost the connection; playing what's buffered"
DISK = "Disk time-shift isn't available; using memory"
REWOUND = "Rewound past the buffer"
LOADING = "Still loading…"
RECONNECTING = "Reconnecting…"
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

    def buffer(
        self,
        sid,
        spans,
        *,
        live_edge=None,
        open_seq=None,
        burst=0.0,
        hls=False,
        breaks=(),
        caught_up=None,
        end=None,
        backlog=None,
        finite=False,
        open_quality=None,
        video=None,
    ):
        """Closed `spans` plus an optional open segment, as the helper's state reports them;
        `breaks`: (timeline position, seconds) of timestamp jumps it left out. `caught_up`
        (HLS: every listed segment arrived) defaults to `hls`; `end` (where the open segment
        starts) to the closed spans' end, or 0.0; `backlog`, the listed media an HLS source
        has still to deliver, is left out when None; `finite`: a finished (ENDLIST) playlist;
        `open_quality`: how cleanly the open segment starts; `video`: whether the stream has
        a video PID (left out when None, as an older helper does)."""
        end = spans[-1].end if spans else (end or 0.0)
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
            "hls": hls,
            "breaks": list(breaks),
            "caught_up": hls if caught_up is None else caught_up,
            "finite": finite,
            "open_quality": open_quality,
        }
        if backlog is not None:
            self._snapshots[sid]["backlog_s"] = backlog
        if video is not None:
            self._snapshots[sid]["video"] = video
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

    def answered(self, sid, hls=False):
        """The source answered, but no byte came yet: the helper's state before them (2.1);
        with `hls`, its open found a playlist."""
        self.receive(sid, 0)
        self._snapshots[sid]["answered"] = True
        self._snapshots[sid]["hls"] = hls
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
    pictureFroze = Signal()

    def __init__(self):
        super().__init__()
        self.holding = False  # a stop leaves libVLC's input open until the test ends it
        self.plays = []  # (url, kind, latency)
        self.software = []  # each play's software decoding
        self.stops = 0
        self.paused = []
        self.rates = []
        self.toggles = 0
        self.ms = 0
        self.current = PlaybackState.IDLE
        self.fail_on_play = False
        self.http = []  # (user_agent, referrer) of each play

    def play(
        self,
        url,
        *,
        kind,
        latency="normal",
        user_agent=None,
        start_ms=None,
        referrer=None,
        software=False,
    ):
        self.plays.append((url, kind, latency))
        self.http.append((user_agent, referrer))
        self.software.append(software)
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
    second at a time), each arrival optionally jittered, cut into `gop` second segments
    whose newest stays open until the next arrival, as in the real segmenter. The state
    reports `burst` as `burst_s` (HLS: its target duration; with `hls` the state says the
    source is HLS, as the helper does, 2.0.2), or with `metered` what the
    helper's BurstMeter measures from the arrivals (raw TS; `edge` arrived at connect). VLC plays at
    the last rate set while it has data; at the edge it reports `starved` (BUFFERING; real
    libVLC 3 keeps reporting PLAYING with its clock standing still). With `crawl` it cannot
    play at any rate but 1x, as on a real channel (2.0.2): at another rate its clock stands
    still and moves a quarter second every two seconds, while it reports PLAYING; with
    `crawl_1x` it crawls so at 1x too (measured on an HLS channel through the pipeline). With
    `recover`, VLC needs that long to restart after it ran dry (it refills its caching: its
    clock stands still while it says Playing; the 2.0.2 review's model). While
    `stalled` the source sends nothing: the edge stands still and no state arrives. With
    `sizes` (an iterator) each arrival brings the next size instead of `burst` seconds, at
    the same rhythm (a public HLS channel: 6 s every 7 s, every sixth time 12 s), and with
    `gaps` (an iterator) each next one comes that many seconds later (two sparse-PCR channels:
    10 s batches 12, 12 and 6 s apart). With `pcr`
    VLC's get_time() moves only at the stream's timestamps, one each `pcr` seconds of media
    (2.1 stage 2, measured on sparse-PCR HLS channels: 2000 ms steps every 2 s); with
    `speed` VLC plays at that share of the rate set when it is not 1x, with `speed_1x` at 1x
    (a real crawl, on that clock). On that clock `missing(k)` drops the k-th stamp (a step
    twice as long now and then). Once VLC plays past `jump_at` its stamps jump by `jump_by`:
    an HLS discontinuity or a reconnect whose timestamps jump, so VLC's clock moves while what
    it plays does not; with `media_step` the timeline kept the step as media (under 3 s), so
    VLC's place on it moves too. `reported` is the helper's list of the jumps it left out of
    its timeline: (position, seconds). LiveSession ticks every 0.25 s.
    """

    DT = 0.05
    CRAWL = (2.0, 0.25)  # at a rate other than 1x: this much media every this many seconds

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
        metered=False,
        crawl=False,
        crawl_1x=False,
        hls=False,
        recover=0.0,
        pcr=0.0,
        speed=1.0,
        speed_1x=1.0,
    ):
        self.case, self.sid, self.burst, self.gop = case, sid, burst, gop
        self.crawl, self.crawled = crawl, 0.0  # wall seconds at another rate since a step
        self.crawl_1x = crawl_1x
        self.hls = hls
        self.recover, self.frozen_until, self.dry, self.recoveries = recover, -1.0, False, 0
        self.pcr, self.speed, self.speed_1x = pcr, speed, speed_1x
        self.missing, self.reported = None, []
        self.lead = 0.0  # after each open, VLC's clock reads this far ahead (`sync`)
        self.jump_at, self.jump_by, self.media_step = math.inf, 0.0, False
        self.stamp = self.stamp_offset = self.pending_jump = 0.0
        self.meter = worker.BurstMeter() if metered else None
        self.edge, self.chunk = edge, (burst if burst > 0 else 0.25)
        self.jitter, self.rng = jitter, random.Random(seed)
        self.nominal = case.clock.now + (self.chunk if first is None else first)
        self.next_arrival = self.nominal
        self.next_tick = case.clock.now + 0.25
        self.position, self.media, self.plays_seen = None, 0.0, 0
        self.starved, self.stalled = starved, False
        self.sizes = None  # an iterator of arrival sizes (None: `chunk` each)
        self.gaps = None  # an iterator of the seconds to each next arrival (None: `chunk`)
        self.starved_s = 0.0  # how long VLC had nothing to play
        self.before = []  # the lag right before each arrival
        self.push()

    def push(self):
        closed = math.ceil(self.edge / self.gop - 1e-9) - 1  # the newest segment is open
        spans = segments(0, closed, length=self.gop)
        burst = self.burst
        if self.meter is not None:
            self.meter.add(self.case.clock.now, self.edge)
            burst = self.meter.burst_s
        self.case.engine.buffer(
            self.sid,
            spans,
            live_edge=self.edge,
            open_seq=closed,
            burst=burst,
            hls=self.hls,
            breaks=self.reported,
        )

    def ready(self, backlog_after_start=True):
        """`backlog_after_start`: the session starts at the oldest segment and the rest of
        `edge` lands after it, as drift (see `LiveSessionCase.ready`); else all of it is there
        at ready."""
        burst = self.burst if self.meter is None else self.meter.burst_s
        case = self.case
        if backlog_after_start and case.ready_at_oldest(
            self.sid, 0, 0.0, self.edge, burst, self.hls
        ):
            self.push()  # the rest lands after the start
        else:
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
                self.stamp = self.pending_jump = 0.0
                self.stamp_offset = self.lead  # VLC's clock may start ahead of its picture
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
                        self.edge += self.chunk if self.sizes is None else next(self.sizes)
                    self.nominal += self.chunk if self.gaps is None else next(self.gaps)
                    self.next_arrival = self.nominal + self.rng.uniform(-self.jitter, self.jitter)
                if not self.stalled:
                    self.push()
            self.sync()
            if self.position is not None and playback.current in (
                PlaybackState.PLAYING,
                PlaybackState.BUFFERING,
            ):
                rate = (playback.rates or [1.0])[-1]
                wanted = rate * self.DT * (self.speed_1x if rate == 1.0 else self.speed)
                if (self.crawl and rate != 1.0) or self.crawl_1x:
                    every, media = self.CRAWL
                    self.crawled += self.DT
                    wanted = 0.0
                    if self.crawled >= every - 1e-9:
                        self.crawled -= every
                        wanted = media
                if self.dry and self.edge - self.position > 1e-6:  # data again: restart
                    self.dry, self.frozen_until = False, now + self.recover
                    self.recoveries += 1
                if now < self.frozen_until:
                    wanted = 0.0
                advance = max(0.0, min(wanted, self.edge - self.position))
                state = PlaybackState.PLAYING
                if advance < wanted - 1e-9:
                    state = self.starved
                    self.starved_s += self.DT
                    self.dry = True
                if state is not playback.current:
                    playback.set_state(state)
                if self.position < self.jump_at <= self.position + advance:
                    self.pending_jump += self.jump_by  # the next stamp VLC reads carries it
                    if self.media_step:  # and its place on the timeline, which kept it
                        self.position += self.jump_by
                self.position += advance
                self.media += advance
                stamp = self.media
                if self.pcr > 0:
                    k = math.floor(self.media / self.pcr + 1e-9)
                    while k > 0 and self.missing is not None and self.missing(k):
                        k -= 1
                    stamp = max(self.stamp, k * self.pcr)
                if stamp != self.stamp and self.pending_jump:
                    self.stamp_offset += self.pending_jump
                    self.pending_jump = 0.0
                self.stamp = stamp
                playback.ms = round((stamp + self.stamp_offset) * 1000)
            if now >= self.next_tick - 1e-9:
                self.next_tick += 0.25
                case.live._tick()
                self.sync()
        return self


def reversals(rates):
    """How often the rate turns around (up after down, or down after up)."""
    rates = [1.0] + list(rates)
    return sum(1 for a, b, c in zip(rates, rates[1:], rates[2:]) if (b - a) * (c - b) < 0)


def changes(rates):
    """Rate changes between 1x and another rate (2.1: a nudge ramps in steps, counted once
    each way)."""
    rates = [1.0] + list(rates)
    return sum(1 for a, b in zip(rates, rates[1:]) if (a == 1.0) != (b == 1.0))


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
    def start(self, latency="normal", disk=False, max_bytes=128 * MiB, channel=None):
        extra = {} if channel is None else {"channel": channel}
        self.live.start(
            URL,
            headers={"User-Agent": "QiTV-Test"},
            verify_ssl=True,
            latency=latency,
            user_agent="QiTV-Test",
            disk=disk,
            max_bytes=max_bytes,
            **extra,
        )
        return self.engine.last_sid if self.engine.opened else None

    def ready(
        self,
        spans,
        *,
        live_edge=None,
        open_seq=None,
        burst=0.0,
        latency="normal",
        backlog_after_start=True,
    ):
        """Start, and get ready with `spans` buffered (and an open segment after them).

        `backlog_after_start` (the start most tests assume: they test stay-live from a lag,
        not the start): the session starts at the oldest segment, and the rest lands after
        it as drift (a reconnect's backlog, say), not as part of the start's landing. Ready
        comes with only that segment buffered, as far back as the target (2.1 stage 3 starts
        at the target), and the rest follows. Without it, or when the oldest segment cannot
        be that far back, all of it is buffered at ready and the start policy picks."""
        sid = self.start(latency=latency)
        first = (spans[0].seq, spans[0].start) if spans else (open_seq, 0.0)
        limit = live_edge if live_edge is not None else (spans[-1].end if spans else 0.0)
        early = backlog_after_start and first[0] is not None
        if early and self.ready_at_oldest(sid, *first, limit, burst):
            self.engine.buffer(sid, spans, live_edge=live_edge, open_seq=open_seq, burst=burst)
            return sid
        self.engine.buffer(sid, spans, live_edge=live_edge, open_seq=open_seq, burst=burst)
        self.engine.sessionReady.emit(sid)
        return sid

    def ready_at_oldest(self, sid, seq, start, limit, burst=0.0, hls=False):
        """Get ready with only segment `seq` (from `start`) buffered, as far behind the live
        edge as the target: the session starts there. False (nothing sent) when that edge
        would be past `limit`, the edge the test buffers next."""
        live = self.live
        target = live._target({"burst_s": burst})
        edge = start + target + burst / 2 - live._open_s  # the mean lag once VLC plays it
        if edge > limit + 1e-9:
            return False
        self.engine.buffer(sid, [], open_seq=seq, live_edge=edge, end=start, burst=burst, hls=hls)
        self.engine.sessionReady.emit(sid)
        # What lands next is drift: the start's re-landing window (a backlog step within
        # 10 s of it is part of the landing, within 5 s re-picks the start) is over.
        live._reland_until = live._repick_until = -math.inf
        return True

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


class NoRewindReasonTests(LiveSessionCase):
    def test_a_channel_that_cannot_be_time_shifted_says_so(self):
        sid = self.start()
        self.engine.sessionFailed.emit(sid, "unsupported", "Not MPEG-TS.")
        self.engine.answer()
        self.assertEqual(self.modes[-1], "direct")
        self.assertEqual(self.live.no_rewind_reason, UNAVAILABLE)


class StartTests(LiveSessionCase):
    def test_a_channels_referrer_goes_with_its_direct_play_only(self):
        # 2.1: the M3U's http-referrer reaches the provider; the loopback stream needs none.
        for referrer in ("https://site.test/", None):
            with self.subTest(referrer=referrer):
                self.live = self.make(ready_timeout_ms=50)
                extra = {} if referrer is None else {"referrer": referrer}
                self.live.start(
                    URL,
                    headers={"User-Agent": "QiTV-Test"},
                    verify_ssl=True,
                    latency="normal",
                    user_agent="QiTV-Test",
                    disk=False,
                    max_bytes=128 * MiB,
                    **extra,
                )
                sid = self.engine.last_sid
                self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
                self.engine.sessionReady.emit(sid)
                self.engine.sessionFailed.emit(sid, "network", "x")  # at the edge: direct
                self.wait_until(lambda: sid in self.engine.closed)
                self.engine.answer()
                self.assertEqual(self.playback.plays[-1], DIRECT)
                self.assertEqual(self.playback.http[0], ("QiTV-Test", None))  # the loopback
                self.assertEqual(self.playback.http[-1], ("QiTV-Test", referrer))

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

    def test_ready_plays_the_loopback_stream_through_the_pipeline(self):
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
                "target": 2.5,  # 2.1 stage 3: an HLS source starts far enough back for it
                "extra": 0.0,
            },
        )
        self.assertEqual(self.playback.plays, [])  # nothing plays before ready

        modes_at_open = []
        self.playback.stateChanged.connect(
            lambda state: modes_at_open.append(self.live.mode) if state == "opening" else None
        )
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)  # the first segment, still open
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

    def test_low_and_normal_start_at_their_targets_at_once(self):
        # 2.1 stage 3 (before: the oldest segment under any latency but Stable).
        for latency, seq in (("low", 1), ("normal", 0)):
            with self.subTest(latency=latency):
                self.live = self.make()
                spans = segments(0, 1)
                sid = self.ready(
                    spans, live_edge=2.5, open_seq=1, latency=latency, backlog_after_start=False
                )
                url = self.engine.stream_url(sid, seq)  # 1 s and 3 s behind once playing
                self.assertEqual(self.playback.plays, [(url, PlayKind.LIVE_PIPELINE, latency)])

    def test_stable_waits_until_the_buffer_holds_its_target(self):
        sid = self.start(latency="stable")
        self.engine.buffer(sid, segments(0, 1), live_edge=2.5, open_seq=1)
        self.engine.sessionReady.emit(sid)
        self.assertEqual((self.playback.plays, self.modes), ([], []))
        for edge in (3.0, 3.5, 4.0, 4.5, 5.0):  # at real time
            self.clock.now += 0.5
            self.engine.buffer(sid, segments(0, 2), live_edge=edge, open_seq=2)
        self.assertEqual(self.playback.plays, [])  # 5.5 s once VLC opened: under 5.75
        self.clock.now += 0.5
        self.engine.buffer(sid, segments(0, 2), live_edge=5.5, open_seq=2)
        stable = (self.engine.stream_url(sid, 0), PlayKind.LIVE_PIPELINE, "stable")
        self.assertEqual(self.playback.plays, [stable])  # 6.0 s: on target
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.engine.closed, [])

    def test_stable_starts_at_its_target_when_the_buffer_holds_more(self):
        sid = self.ready(
            segments(0, 6), live_edge=12.0, burst=6.0, latency="stable", backlog_after_start=False
        )
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
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
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
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
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
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
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

    def test_an_answer_without_bytes_yet_extends_the_wait_with_a_hint(self):
        """An on-demand restream answers at once and starts its body seconds later (2.1, a
        raw-TS channel VLC played at once right after the fallback)."""
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=60_000)
        sid = self.start()
        self.engine.answered(sid)
        self.wait_until(lambda: self.notices)
        self.assertEqual(self.notices, [LOADING])
        self.assertEqual(self.engine.closed, [])  # still waiting
        self.engine.receive(sid, 188 * 20)
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.notices, [LOADING])

    def test_an_answer_alone_is_waited_for_a_bounded_time(self):
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=250)
        sid = self.start()
        self.engine.answered(sid)
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            self.wait_until(lambda: self.engine.closed)
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [LOADING, UNAVAILABLE])
        self.assertTrue(any("answered but sent nothing" in line for line in logs.output))

    def test_an_hls_playlist_that_answered_without_segment_bytes_waits_only_the_timeout(self):
        """The longer wait is raw TS's (cold restreams): an HLS channel gets it only once
        segment bytes flow, as 2.0.1's not-playable-yet rule."""
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=60_000)
        sid = self.start()
        self.engine.answered(sid, hls=True)
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            self.wait_until(lambda: self.engine.closed)  # not 60 s later
        self.assert_falls_back(sid, UNAVAILABLE, 0)
        self.assertEqual(self.notices, [UNAVAILABLE])
        self.assertTrue(any("no segment" in line for line in logs.output), logs.output)
        # Once segment bytes flow, an HLS channel waits as before.
        self.engine.answer()
        sid = self.start()
        self.engine.answered(sid, hls=True)
        self.engine.receive(sid, 188 * 20)
        self.wait_until(lambda: self.notices[-1] == LOADING)
        self.assertNotIn(sid, self.engine.closed)

    def test_a_fallback_before_ready_says_why_in_the_log(self):
        cases = (
            ("nothing", None, "did not answer"),
            ("bytes", lambda sid: self.engine.receive(sid, 188 * 20), "nothing playable"),
            ("failure", lambda sid: self.engine.sessionFailed.emit(sid, "http", "x"), "(http)"),
        )
        for name, step, reason in cases:
            with self.subTest(name):
                self.live = self.make(ready_timeout_ms=50, ready_max_ms=150)
                with self.assertLogs("services.live.live_session", "INFO") as logs:
                    sid = self.start()
                    if step is not None:
                        step(sid)
                    self.wait_until(lambda sid=sid: sid in self.engine.closed)
                self.engine.answer()
                lines = [line for line in logs.output if "directly" in line]
                self.assertEqual(len(lines), 1, logs.output)
                self.assertIn(reason, lines[0])
                self.assertNotIn("://", lines[0])

    def test_the_next_channel_waits_only_the_ready_timeout_again(self):
        self.live = self.make(ready_timeout_ms=50, ready_max_ms=250)
        sid = self.start()
        self.engine.receive(sid, 188 * 20)
        self.wait_until(lambda: self.engine.closed)
        self.engine.answer()
        started = time.monotonic()
        sid = self.start()  # the next channel sends nothing
        self.wait_until(lambda: sid in self.engine.closed)
        self.assertLess(time.monotonic() - started, 0.24)  # not the extended 0.25 s
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
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])
        self.assertEqual(self.modes, ["pipeline"])

    def test_a_late_ready_after_stop_is_ignored(self):
        sid = self.start()
        self.live.stop()
        self.assertEqual(self.engine.closed, [sid])
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.modes, [])

    def test_zapping_waits_for_the_old_session_to_close(self):
        first = self.start()
        self.start()
        self.assertEqual(self.engine.closed, [first])
        self.assertEqual(len(self.engine.opened), 1)  # the new one opens after the close
        self.engine.buffer(first, [], open_seq=0, live_edge=2.0)
        self.engine.sessionReady.emit(first)
        self.engine.sessionFailed.emit(first, "network", "The stream could not be reached.")
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.releases, [])
        self.engine.answer()
        second = self.engine.last_sid
        self.assertEqual(self.engine.events, [("open", first), ("released",), ("open", second)])
        self.engine.buffer(second, [], open_seq=0, live_edge=2.0)
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


class StartPolicyTests(LiveSessionCase):
    """2.1 stage 3: a tune starts where Go live would land (the start nearest the target from
    above), once the backlog a source sends at connect has landed, never from the oldest
    backlog when a start nearer the target exists."""

    def setUp(self):
        super().setUp()
        # What the session tells stay-live about each start (its lag above the target); the
        # real method runs.
        real = StayLive.landed
        spy = mock.patch.object(StayLive, "landed", autospec=True, side_effect=real)
        self.landed = self.enterContext(spy)

    def test_the_start_is_the_one_go_live_would_pick(self):
        # Once VLC opened them (0.5 s): 19, 12, 5 and 2.4 s behind, the last one 0.1 s
        # short of the 2.5 s target, which counts as at it.
        spans = [Span(0, 1.5, 8.5, False), Span(1, 8.5, 15.5, False), Span(2, 15.5, 18.1, False)]
        sid = self.ready(spans, live_edge=20.0, open_seq=3, backlog_after_start=False)
        self.assertEqual(self.playback.plays, [self.stream(sid, 3)])
        self.assertEqual(self.live.position(), 18.1)
        self.assertAlmostEqual(self.landed.call_args.args[1], -0.1)

    def test_never_the_oldest_backlog_when_a_start_nearer_the_target_exists(self):
        sid = self.ready(
            [Span(0, 1.5, 11.5, False)], live_edge=20.0, open_seq=1, backlog_after_start=False
        )
        self.assertEqual(self.playback.plays, [self.stream(sid, 1)])
        self.assertAlmostEqual(self.landed.call_args.args[1], 9.0 - 2.5)

    def test_a_backlog_that_lands_in_pieces_is_waited_for(self):
        """The 1-stream raw-TS provider: ~20 s at connect, two keyframe intervals of 10 s,
        once a second apart (measured), then real time."""
        sid = self.start()
        self.engine.buffer(sid, [], open_seq=0, live_edge=0.0)  # the first keyframe
        self.engine.sessionReady.emit(sid)
        self.clock.now += 0.1
        self.engine.buffer(sid, segments(0, 1, length=10.0), live_edge=10.0)
        for _ in range(3):  # the first 10 s alone: a start 10.5 s behind, not yet
            self.step(0.25)
        self.clock.now += 0.2
        self.engine.buffer(sid, segments(0, 2, length=10.0), live_edge=20.0)
        self.assertEqual(self.playback.plays, [])
        for _ in range(4):
            self.step(0.25)
        # Held at most a second and a half: from the newest keyframe at or above the target.
        self.assertEqual(self.playback.plays, [self.stream(sid, 1)])
        self.assertAlmostEqual(self.landed.call_args.args[1], 10.5 - 2.5)

    def test_a_backlog_that_landed_is_started_from_at_once(self):
        sid = self.start()
        self.engine.buffer(sid, [], open_seq=0, live_edge=0.0)  # the first keyframe
        self.engine.sessionReady.emit(sid)
        self.clock.now += 0.1
        self.engine.buffer(sid, segments(0, 2, length=10.0), live_edge=20.0, open_seq=2)
        self.step(0.5)  # then about real time
        self.engine.buffer(sid, segments(0, 2, length=10.0), live_edge=20.6, open_seq=2)
        self.step(0.25)
        self.assertEqual(self.playback.plays, [])
        self.step(0.25)  # 0.6 s more in the last second: it landed
        self.assertEqual(self.playback.plays, [self.stream(sid, 1)])  # 1.1 s from 20.0: short

    def test_a_steady_source_with_nothing_far_enough_back_starts_within_a_second(self):
        sid = self.start()
        self.engine.buffer(sid, [], open_seq=0, live_edge=0.4)
        self.engine.sessionReady.emit(sid)
        self.step(0.75)
        self.assertEqual(self.playback.plays, [])
        self.step(0.25)
        self.assertEqual(self.playback.plays, [self.stream(sid, 0)])  # from the oldest
        self.assertLess(self.landed.call_args.args[1], 0)  # short of the target
        self.assertEqual(self.notices, [])

    def test_hls_starts_on_the_edge_its_backlog_will_reach_once_that_start_is_final(self):
        # A sparse-PCR channel's 10 s segments (2 s keyframe intervals), 36 s fetched: the
        # start need not wait for all of it (a slow segment of a public HLS channel held
        # the picture back 4 s): the helper says what is still to come.
        # (Under Normal: the segments' target. Auto aims a second higher at first, its
        # learned need: LearnedTargetTests.)
        sid = self.start(latency="normal")
        hls = {"burst": 10.0, "hls": True, "caught_up": False}
        self.engine.buffer(sid, [], open_seq=0, backlog=36.0, **hls)
        self.engine.sessionReady.emit(sid)
        self.engine.buffer(sid, segments(0, 10), live_edge=20.0, open_seq=10, backlog=16.0, **hls)
        self.step(0.25)
        # From 20.0 (the open segment): 11.5 s behind 36.0 once playing; but a newer start
        # may yet be nearer the 10 s target.
        self.assertEqual(self.playback.plays, [])
        self.engine.buffer(sid, segments(0, 12), live_edge=24.0, open_seq=12, backlog=12.0, **hls)
        # From 22.0: 9.5 s, under the floor: 20.0 is final, the rest of the backlog on its way.
        self.assertEqual(self.playback.plays[-1][0], self.engine.stream_url(sid, 10))
        self.assertAlmostEqual(self.landed.call_args.args[1], 1.5)

    def test_hls_is_held_past_a_second_and_a_half_until_its_start_is_final(self):
        # Seen on a public HLS channel: held at most 1.5 s like raw TS, it started with 18 s
        # of 24 buffered, the rest landed after it, and stay-live ran 1.10x for 2 minutes.
        # (Under Normal: the segments' target. Auto aims a second higher at first, its
        # learned need: LearnedTargetTests.)
        sid = self.start(latency="normal")
        hls = {"burst": 7.0, "hls": True, "caught_up": False}
        self.engine.buffer(sid, [], open_seq=0, backlog=24.0, **hls)
        self.engine.sessionReady.emit(sid)
        self.engine.buffer(sid, segments(0, 4), live_edge=8.0, open_seq=4, backlog=16.0, **hls)
        for _ in range(12):  # 3 s: the open segment (8.0) is 13.5 s behind 24.0, the newest
            self.step(0.25)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.notices, [LOADING])  # after a second
        # 14.0 would be 7 s behind 24.0, but a segment or less is still to come: a wrong
        # EXTINF or a missing segment could move it (review I3), so it waits for it.
        self.engine.buffer(sid, segments(0, 9), live_edge=18.0, open_seq=9, backlog=6.0, **hls)
        self.assertEqual(self.playback.plays, [])
        hls["caught_up"] = True
        self.engine.buffer(sid, segments(0, 11), live_edge=24.0, open_seq=11, **hls)
        self.assertEqual(self.playback.plays[-1][0], self.engine.stream_url(sid, 7))  # 14.0: 7 s
        self.assertEqual(self.notices, [LOADING])

    def test_hls_takes_a_start_inside_stay_lives_band_at_once(self):
        # Review M9: a start already within the band is not held for a nearer one.
        # (Under Normal: the segments' target. Auto aims a second higher at first, its
        # learned need: LearnedTargetTests.)
        sid = self.start(latency="normal")
        hls = {"burst": 7.0, "hls": True, "caught_up": False}
        self.engine.buffer(sid, [], open_seq=0, backlog=24.0, **hls)
        self.engine.sessionReady.emit(sid)
        # From 14.0 (the open segment, the newest): 7 s behind 24.0, the target.
        self.engine.buffer(sid, segments(0, 7), live_edge=16.0, open_seq=7, backlog=8.0, **hls)
        self.assertEqual(self.playback.plays[-1][0], self.engine.stream_url(sid, 7))
        self.assertAlmostEqual(self.landed.call_args.args[1], 0.0)

    def test_a_finished_playlist_fetched_at_once_plays_from_its_beginning(self):
        # Review I1: a 60 s clip (ENDLIST) played its last 4 s.
        sid = self.start(latency=AUTO)
        clip = {"burst": 2.0, "hls": True, "finite": True}
        self.engine.buffer(sid, segments(0, 30), live_edge=60.0, **clip)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))
        self.assertFalse(self.landed.called)  # nothing live to land near

    def test_a_finished_playlist_still_downloading_plays_from_its_beginning_at_once(self):
        # Review I1: a long one waited the whole ready timeout, then started at the newest
        # segment fetched, with a landing an hour above the target.
        sid = self.start(latency=AUTO)
        clip = {"burst": 2.0, "hls": True, "finite": True, "caught_up": False, "backlog": 0.0}
        self.engine.buffer(sid, segments(0, 3), live_edge=6.5, open_seq=3, **clip)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))
        self.assertFalse(self.landed.called)
        self.assertEqual(self.notices, [])

    def test_a_clip_still_downloading_is_not_steered_as_live(self):
        # Re-review I1: a finished playlist downloading at ~8x started at 0, then ramped to
        # 1.067x and reopened at its newest segment after the 5 s hold.
        sid = self.start(latency=AUTO)
        clip = {"burst": 2.0, "hls": True, "finite": True, "caught_up": False, "backlog": 0.0}
        self.engine.buffer(sid, segments(0, 3), live_edge=6.5, open_seq=3, **clip)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        edge = 6.5
        for _ in range(80):  # 20 s, 2 s of the clip every 0.25 s
            edge += 2.0
            count = int(edge // 2)
            self.engine.buffer(sid, segments(0, count), live_edge=edge, open_seq=count, **clip)
            self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(len(self.playback.plays), 1)

    def test_an_input_that_ends_mid_catch_up_plays_on_at_normal_speed(self):
        # Re-review I1: a download that ended mid-ramp kept 1.033x for the rest of the clip.
        for how in ("ended", "finished"):
            with self.subTest(how=how):
                self.live = self.make()
                sid = self.ready(segments(0, 7), live_edge=14.0)
                self.playback.set_state(PlaybackState.PLAYING)
                for k in range(1, 16):  # the source sends on, 11.5 s more than the target
                    self.engine.buffer(sid, segments(0, 7), live_edge=14.0 + 0.25 * k)
                    self.step(0.25)
                self.assertGreater(self.playback.rates[-1], 1.0)  # far behind: catching up
                if how == "ended":
                    self.engine.sessionEnded.emit(sid)
                else:  # the playlist turned finished (ENDLIST)
                    self.engine.buffer(sid, segments(0, 7), live_edge=18.0, finite=True)
                self.step(0.25)
                self.assertEqual(self.playback.rates[-1], 1.0)
                self.step(5.0)
                self.assertEqual(self.playback.rates[-1], 1.0)
                # And it shows so: no nudge left, no "catching up" (re-review round 3).
                self.assertEqual(self.timelines[-1]["nudge"], 1.0)
                transport = Transport(None, idle_ms=3000)
                self.addCleanup(transport.deleteLater)
                transport.update_timeline(self.timelines[-1], [])
                self.assertNotIn("catching up", transport.indicator_text())

    def test_a_clip_at_a_chosen_speed_plays_on_at_1x_near_its_download_edge(self):
        # Re-review round 3: a finished playlist still downloading, played faster after a
        # seek back, never came back to 1x (stay-live no longer runs for it) and could run
        # dry on a slow download. As a video player does near its download, it hands back
        # at the target behind the download edge.
        sid = self.start()
        clip = {"burst": 2.0, "hls": True, "finite": True, "caught_up": False, "backlog": 0.0}
        self.engine.buffer(sid, segments(0, 10), live_edge=20.0, open_seq=10, **clip)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.seek_to(2.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.set_speed(2.0)
        self.assertEqual(self.playback.rates, [2.0])
        edge = 20.0
        lags = []
        for _ in range(120):  # 30 s; the download brings 1.2 s a second
            edge += 0.3
            count = int(edge // 2)
            self.engine.buffer(sid, segments(0, count), live_edge=edge, open_seq=count, **clip)
            self.step(0.25, media=0.5 if self.playback.rates[-1] == 2.0 else 0.25)
            lags.append(edge - self.live.position())
        self.assertEqual(self.playback.rates, [2.0, 1.0])
        handed = next(i for i, rate in enumerate(self.timelines) if rate["rate"] == 1.0)
        at = next(i for i, lag in enumerate(lags) if lag <= TARGETS["normal"] + 0.5)
        self.assertGreater(lags[at], TARGETS["normal"] - 0.5)  # handed back at the target
        self.assertGreater(min(lags), 1.0)  # never run dry
        self.assertEqual(self.timelines[-1]["mode"], "live")  # the transport: no chosen speed
        self.assertEqual(self.timelines[handed]["nudge"], 1.0)

    def test_an_ended_input_plays_from_the_oldest_segment(self):
        sid = self.start()
        self.engine.sessionEnded.emit(sid)
        self.engine.buffer(sid, segments(0, 10), live_edge=20.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))

    def test_the_tune_prefers_a_keyframe_start(self):
        # Review M6 (5a's start qualities): a cut without a keyframe (a forced one) starts
        # broken; a keyframe 2 s further back first, an intra picture after it.
        cases = (
            (("none", "key"), 8),  # 16.0 (4.5 s behind) over 18.0 (2.5 s), a forced cut
            (("none", "open"), 8),  # an intra picture before no keyframe at all
            (("open", "key"), 9),  # a keyframe 2 s further back is beyond stay-live's band
            (("none", "none"), 9),  # nothing better far enough back: the target's
        )
        for (newest, older), seq in cases:
            with self.subTest(newest=newest, older=older):
                self.live = self.make()
                spans = [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "none") for k in range(8)]
                spans += [Span(8, 16.0, 18.0, False, older), Span(9, 18.0, 20.0, False, newest)]
                sid = self.ready(spans, live_edge=20.0, backlog_after_start=False)
                self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, seq))
        # The open segment's quality as the helper knows it.
        self.live = self.make()
        sid = self.start()
        spans = [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "none") for k in range(8)]
        spans.append(Span(8, 16.0, 18.0, False, "key"))
        self.engine.buffer(sid, spans, live_edge=20.0, open_seq=9, open_quality="none")
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 8))

    def test_a_keyframe_is_preferred_only_within_stay_lives_band(self):
        # Re-review I2: one IDR at the oldest segment, intra-picture starts every 2 s
        # (HEVC CRA, an open GOP): the tune started there, 18 s above its 2.5 s target.
        spans = [Span(0, 0.0, 2.0, False, "key")]
        spans += [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "open") for k in range(1, 10)]
        sid = self.ready(spans, live_edge=20.0, backlog_after_start=False)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 9))  # 2.5 s
        self.assertAlmostEqual(self.landed.call_args.args[1], 0.0)
        # A keyframe within the band (3 s behind, the band reaching 3.25 s) is taken over the
        # intra picture nearer the target: it costs no steering.
        self.live = self.make()
        spans = [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "none") for k in range(8)]
        spans += [Span(8, 16.0, 17.5, False, "none"), Span(9, 17.5, 18.0, False, "key")]
        spans.append(Span(10, 18.0, 20.0, False, "open"))
        sid = self.ready(spans, live_edge=20.0, backlog_after_start=False)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 9))

    def test_a_go_live_merged_into_stay_lives_reopen_keeps_its_landing(self):
        # Review M4: the merged Go live opened nothing but wiped the reopen's landing (9 s
        # above the target on 10 s keyframe intervals): 1.10x came back.
        spans = segments(0, 3, length=10.0)
        sid = self.ready(spans, live_edge=31.0, backlog_after_start=False)
        self.live._go_live(unpause=False, reopen=True)  # stay-live's own, far behind
        self.live.go_live()  # the user's, a moment later: merged
        self.assertEqual(len(self.playback.plays), 2)
        self.playback.set_state(PlaybackState.PLAYING)
        edge = 31.0
        for _ in range(40):  # 10 s, the lag held 9 s above the target
            edge += 0.25
            self.engine.buffer(sid, spans, live_edge=edge)
            self.step(0.25)
        self.assertEqual(self.playback.rates, [])

    def test_a_first_open_without_a_helper_picks_its_start_again_once_one_serves(self):
        # Review M5: it reopened at the position (the oldest segment), with no landing.
        sid = self.start()
        self.engine.up = False  # the helper is not serving yet: no stream URL
        self.engine.buffer(sid, segments(0, 10), live_edge=20.0)
        self.engine.sessionReady.emit(sid)
        self.assertEqual(self.playback.plays, [])
        self.engine.up = True
        self.engine.buffer(sid, segments(0, 10), live_edge=20.5)
        self.step(0.25)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 9))
        self.assertTrue(self.landed.called)

    def test_a_backlog_step_right_after_the_start_is_part_of_the_landing(self):
        # Review M8: raw TS whose backlog came after the 1.5 s hold ran 1.10x for ~100 s;
        # stage 3 takes it in as part of the landing (a gentle nudge). With 10 s keyframes a
        # start picked again once the source flowed on gains nothing on it (`RepickTests`).
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=13.5, gop=10.0).ready(backlog_after_start=False)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 1))  # 4 s
        feed.run(3.0)
        feed.edge += 9.0  # the rest of the backlog
        feed.push()
        feed.run(45.0)  # within the landing's window (after it a backlog is drift: R32)
        self.assertLessEqual(max(self.playback.rates or [1.0]), 1.03)
        self.assertEqual(len(self.playback.plays), 1)

    def test_hls_that_does_not_say_what_is_to_come_waits_until_every_segment_arrived(self):
        # A sparse-PCR channel's 10 s segments (2 s keyframe intervals), 36 s fetched.
        # (Under Normal: the segments' target. Auto aims a second higher at first, its
        # learned need: LearnedTargetTests.)
        sid = self.start(latency="normal")
        self.engine.buffer(sid, [], open_seq=0, burst=10.0, hls=True, caught_up=False)
        self.engine.sessionReady.emit(sid)
        self.engine.buffer(
            sid, segments(0, 10), live_edge=20.0, open_seq=10, burst=10.0, hls=True, caught_up=False
        )
        self.step(0.25)
        self.assertEqual(self.playback.plays, [])  # more of the backlog is on its way
        self.engine.buffer(sid, segments(0, 17), live_edge=36.0, open_seq=17, burst=10.0, hls=True)
        # The mean over a 10 s batch: from 20.0, 11.5 s behind once playing; from 22.0, 9.5.
        self.assertEqual(self.playback.plays[-1][0], self.engine.stream_url(sid, 10))
        self.assertAlmostEqual(self.landed.call_args.args[1], 1.5)

    def test_hls_with_nothing_far_enough_back_waits_and_says_so(self):
        self.live = self.make(ready_timeout_ms=400)
        sid = self.start(latency=AUTO)
        self.engine.buffer(sid, segments(0, 1), live_edge=4.0, open_seq=1, burst=6.0, hls=True)
        self.engine.sessionReady.emit(sid)
        self.step(0.75)
        self.assertEqual((self.playback.plays, self.notices), ([], []))
        self.step(0.5)
        self.assertEqual(self.notices, [LOADING])  # after a second
        self.wait_until(lambda: self.playback.plays)  # bounded by the ready timeout
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))
        self.assertEqual(self.notices, [LOADING])

    def test_the_source_is_told_the_target_it_steers_to(self):
        # Under Auto the learned one (here a 2.0.2 extra of 3 s read as its prior); nothing
        # extra on top: the learned target holds what was added (stage 4b review M1).
        tuning = LiveTuning(None)
        tuning.remember("ch", Tuning(3.0, False, False))
        for latency, target, extra in ((AUTO, 5.5, 0.0), ("stable", 6.0, 0.0), ("low", 1.0, 0.0)):
            with self.subTest(latency=latency):
                self.live = self.make(tuning=tuning)
                self.start(latency=latency, channel="ch")
                kwargs = self.engine.opened[-1][2]
                self.assertEqual((kwargs["target"], kwargs["extra"]), (target, extra))
                self.assertEqual(kwargs["live_delay"], live_session.LIVE_DELAY[latency])

    def test_no_catch_up_or_reopen_after_a_start_from_a_20_s_backlog(self):
        """The 1-stream raw-TS provider: a 20 s backlog, one keyframe every 10 s. 2.1 stage 1
        started 19 s behind, ran 1.10x for a minute and reopened at live 5 s in (R15). Now:
        from the newest keyframe at or above the target, 9 s above it (8 s is far behind),
        nudged gently."""
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=0.0, edge=21.0, gop=10.0).ready(backlog_after_start=False)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 1))  # not 21 s
        self.assertAlmostEqual(self.landed.call_args.args[1], 11.5 - 2.5)
        feed.run(60.0)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        self.assertLessEqual(max(self.playback.rates or [1.0]), 1.03)  # gently, if at all
        self.assertEqual(feed.starved_s, 0.0)

    def test_a_start_short_of_the_target_builds_at_0_97_for_20_s_at_most(self):
        # 6 s bursts and 4 s buffered: nothing as far back as the burst came within the
        # ready timeout, so it starts from the oldest, short (2.0.1: 0.95x for minutes).
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        feed = Feed(self, sid, burst=6.0, edge=4.0).ready()
        self.wait_until(lambda: self.playback.plays)
        self.assertLess(self.landed.call_args.args[1], -1.0)
        rates = []
        for _ in range(100):  # 25 s
            feed.run(0.25)
            rates.append(self.timelines[-1]["rate"])
        self.assertEqual(min(rates), 0.97)
        slow = sum(1 for rate in rates if rate < 1.0) * 0.25
        self.assertLessEqual(slow, 20.0 + feed.starved_s)  # 20 s of playing (VLC ran dry)
        self.assertEqual(rates[-1], 1.0)


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
        sid = self.ready(segments(5, 2, start=10.0), live_edge=14.0)
        self.engine.buffer(sid, segments(5, 2, start=10.0), live_edge=14.0, burst=6.0)
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
    def test_the_timeline_says_where_it_plays_and_how_it_steers(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # position 0.25, lag 3.75: 1.25 s above the target, no nudge yet
        self.assertEqual(self.playback.rates, [])
        last = self.timelines[-1]
        self.assertAlmostEqual(last["position"], 0.25)
        self.assertAlmostEqual(last["lag"], 3.75)
        self.assertEqual(last["shown"], 4.0)  # as the Stay live indicator shows it
        self.assertEqual((last["rate"], last["nudge"]), (1.0, 1.0))
        self.assertEqual(last["live_edge"], 4.0)
        self.assertEqual((last["start"], last["end"]), (0.0, 4.0))
        self.assertEqual(last["mode"], "live")
        self.assertFalse(last["paused"])
        self.assertFalse(last["ended"])
        self.assertEqual(last["target"], 2.5)

    def test_a_lasting_lag_above_the_band_gets_one_gentle_nudge_then_settles(self):
        # 2.1: not within a tick (2.0 did, and flapped on a public HLS channel): after 15 s outside
        # the band, one nudge ramped to 1.03x and back, sized to end at the target.
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready()
        feed.run(14.0)
        self.assertEqual(self.playback.rates, [])
        feed.run(200.0)
        rates = self.playback.rates
        self.assertEqual(rates[:3], [1.01, 1.02, 1.03])
        self.assertEqual(max(rates), 1.03)
        self.assertEqual((changes(rates), reversals(rates)), (2, 1))
        self.assertEqual(rates[-1], 1.0)
        nudging = [state for state in self.timelines if state["rate"] != 1.0]
        self.assertTrue(all(state["nudge"] == 1.03 for state in nudging))
        self.assertAlmostEqual(self.timelines[-1]["lag"], TARGETS["normal"], delta=0.3)
        self.assertEqual(self.timelines[-1]["shown"], 2.5)
        self.assertEqual(len(self.playback.plays), 1)

    def test_segmented_hls_arrivals_change_the_rate_once_at_most(self):
        """The owner's report (a public HLS channel): every 7 s a batch of 6 s, every sixth
        12 s, so the lag saws by 6-12 s around a mean that wanders by itself. 2.0.x changed the
        rate 18 times in 5 minutes there. Now: at its target, the rate never changes; a second
        under it, one gentle nudge; and the indicator's text barely moves."""
        for edge, nudges in ((13.0, 0), (12.0, 1)):  # mean lags of 7.0 and 6.0 s (7 s target)
            with self.subTest(edge=edge):
                self.live = self.make()
                # Under Normal (the 7 s target 2.0.x had): Auto's learned target moves with
                # what the channel needs (LearnedTargetTests: one gentle build at most).
                sid = self.start(latency="normal")
                feed = Feed(self, sid, burst=7.0, edge=edge, hls=True).ready()
                feed.sizes = iter([12.0 if k % 6 == 5 else 6.0 for k in range(1000)])
                feed.run(300.0)
                rates = self.playback.rates
                self.assertEqual((changes(rates), reversals(rates)), (2 * nudges, nudges))
                self.assertTrue(all(0.97 <= rate <= 1.0 for rate in rates))
                shown = [state["shown"] for state in self.timelines[4 * 30 :]]
                text = sum(1 for a, b in zip(shown, shown[1:]) if a != b)
                self.assertLessEqual(text, 270 / 10)  # at most one change per 10 s
                self.assertEqual(len(self.playback.plays), 1)
                self.live.stop()
                self.engine.answer()

    def test_bursts_raise_the_target(self):
        # A source that reports its burst without being HLS (raw TS the helper measured).
        # Nothing as far back as the burst came within the ready timeout: from the oldest.
        self.live = self.make(ready_timeout_ms=50)
        self.ready(segments(0, 2), live_edge=4.0, burst=6.0)
        self.wait_until(lambda: self.playback.plays)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)  # lag 3.75 is under the 6 s burst: no catch-up
        self.assertEqual(self.timelines[-1]["target"], 6.0)
        self.assertEqual(self.playback.rates, [])
        # A mean lag of 1 s is under half a burst of room: after 3 s the start's room is
        # built, at 0.97x for 20 s at most (2.1 stage 3; 2.0.1 built it at 0.95x until half
        # a burst, for minutes).
        for _ in range(25):
            self.step(0.25)
        self.assertEqual(self.playback.rates[:3], [0.99, 0.98, 0.97])  # three steps
        self.assertEqual(self.timelines[-1]["nudge"], 0.97)

    def test_a_frame_more_at_a_segments_close_is_no_burst_landing(self):
        # 2.1 stage 3: the helper reports a batch's end at once, so the next batch's first
        # state (its last segment closing) moves the edge by one frame, 0.04 s, a moment
        # before the batch. Taken for the batch, it dropped the mean lag by a burst and, on a
        # public sparse-PCR channel, made the lag filter start afresh.
        sid = self.ready(segments(0, 7), live_edge=14.5, burst=10.0)
        self.playback.set_state(PlaybackState.PLAYING)
        for _ in range(39):  # 9.75 s: the mean lag holds while the next burst is due
            self.step(0.25)
        before = self.timelines[-1]["lag"]
        self.engine.buffer(sid, segments(0, 7), live_edge=14.54, burst=10.0)
        self.step(0.25)
        self.assertAlmostEqual(self.timelines[-1]["lag"], before, delta=0.1)
        self.engine.buffer(sid, segments(0, 12), live_edge=24.5, burst=10.0)  # the burst
        self.step(0.25)
        self.assertAlmostEqual(self.timelines[-1]["lag"], before, delta=0.3)

    def test_bursts_settle_without_rate_flapping(self):
        sid = self.start()
        feed = Feed(self, sid, burst=6.0, edge=12.0).ready().run(120.0)
        self.assertEqual(len(self.playback.plays), 1)  # no reopen
        rates = self.playback.rates
        self.assertLessEqual(changes(rates), 2)  # one nudge at most, ramped in and out
        self.assertLessEqual(reversals(rates), 1)
        self.assertEqual(rates[-1], 1.0)
        self.assertGreater(min(feed.before[-4:]), 2.0)  # headroom before every burst
        reported = [timeline["lag"] for timeline in self.timelines[-24:]]
        self.assertLess(max(reported) - min(reported), 0.5)  # the mean, not the sawtooth

    def test_bursts_settle_after_reopening_at_live(self):
        sid = self.start()
        feed = Feed(self, sid, burst=6.0, edge=60.0).ready().run(120.0)  # 60 s behind
        self.assertEqual(len(self.playback.plays), 2)  # one reopen at live, no loop
        rates = self.playback.rates
        self.assertLessEqual(changes(rates), 2)
        self.assertLessEqual(reversals(rates), 1)
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

    def test_raw_ts_bursts_are_handled_as_hls_bursts_from_the_second_burst(self):
        """The owner's channel (2.0.1, L1): raw TS, 12 s at connect, then 10 s at once every
        10 s, measured as the helper does. From the second burst on: no stall shown between
        bursts, the burst is the target (never the 12 s backlog), no catch-up into the next
        gap and nothing starves; Go live keeps a burst's room."""
        sid = self.start()
        feed = Feed(self, sid, burst=10.0, edge=12.0, metered=True).ready()
        feed.run(10.0)  # the second burst lands at its end
        self.assertEqual(self.timelines[-2]["target"], TARGETS["normal"])  # not known yet
        self.assertEqual(self.timelines[-1]["target"], 10.0)
        timelines = len(self.timelines) - 1
        feed.run(60.0)
        after = self.timelines[timelines:]
        self.assertTrue(all(state["target"] == 10.0 for state in after))
        self.assertFalse(any(state["stalled"] for state in after))
        self.assertTrue(all(state["rate"] <= 1.05 for state in after))
        self.assertEqual(max(state["target"] for state in self.timelines), 10.0)
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

    def test_a_steady_raw_ts_source_shows_a_hiccup_only_while_it_lasts(self):
        """30 s at connect, then real time with one 3 s hiccup: never taken for bursts."""
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=30.0, metered=True).ready()
        self.live.go_live()
        feed.sync()
        feed.run(20.0)
        feed.stalled = True
        feed.run(3.0)
        self.assertTrue(self.timelines[-1]["stalled"])  # "Reconnecting…" while it lasts
        feed.stalled = False
        feed.run(0.25)  # the source sends again
        self.assertFalse(self.timelines[-1]["stalled"])
        timelines = len(self.timelines)
        feed.run(40.0)
        self.assertFalse(any(state["stalled"] for state in self.timelines[timelines:]))
        self.assertEqual({state["target"] for state in self.timelines}, {TARGETS["normal"]})
        self.assertEqual(feed.meter.burst_s, 0.0)

    def test_a_bursty_source_slows_a_little_to_build_its_room(self):
        """The owner's channel re-measured (2.0.1): 3.84 s at connect (in the helper's view),
        the first 10 s burst 8.5 s later, then 10 s every 10 s. Once the bursts are measured
        the position met the edge just before each burst; now it slows (2.1: once the burst
        counts, gently, then at 0.95x as it leaves under half a second of room) until half a
        burst of room is built, then plays at 1x, without flapping, and nothing starves after."""
        sid = self.start()
        feed = Feed(self, sid, burst=10.0, edge=3.84, first=8.5, metered=True).ready()
        feed.run(8.5)  # the first 10 s burst landed: its 8.5 s gap is the provisional burst
        first = len(self.timelines) - 1
        self.assertEqual(self.timelines[first]["target"], 8.5)
        feed.run(20.0)  # the third burst landed: two alike 10 s bursts
        self.assertEqual(self.timelines[-1]["target"], 10.0)
        timelines = len(self.timelines) - 1
        feed.run(150.0)
        after = self.timelines[timelines:]
        self.assertEqual(min(state["rate"] for state in after), 0.95)
        self.assertTrue(all(0.95 <= state["rate"] <= 1.0 for state in after))
        # From the first burst on: slowed once, then normal speed for good, no flapping, no
        # catch-up and no stall shown in the gaps.
        rates = self.playback.rates
        self.assertEqual((changes(rates), reversals(rates)), (2, 1))
        early = self.timelines[first:]
        self.assertTrue(all(0.95 <= state["rate"] <= 1.0 for state in early))
        self.assertFalse(any(state["stalled"] for state in early))
        self.assertGreaterEqual(min(state["target"] for state in early), 8.5)
        built = feed.before[-3:]  # the room when each of the last bursts landed
        self.assertGreaterEqual(min(built), 10.0 / 2 - 0.5)
        starved, arrivals = feed.starved_s, len(feed.before)
        feed.run(60.0)
        self.assertEqual(feed.starved_s, starved)  # the edge is never met again
        self.assertGreater(min(feed.before[arrivals:]), 4.0)
        self.assertEqual(self.timelines[-1]["rate"], 1.0)
        self.assertEqual(len(self.playback.plays), 1)

    def test_a_steady_source_never_slows(self):
        for edge in (3.0, 30.0):  # at its target, and behind it
            with self.subTest(edge=edge):
                self.live = self.make()
                sid = self.start()
                feed = Feed(self, sid, burst=0.0, edge=edge, metered=True).ready()
                feed.run(120.0)
                self.assertTrue(all(rate >= 1.0 for rate in self.playback.rates))
                self.assertTrue(all(state["rate"] >= 1.0 for state in self.timelines))

    def test_a_steady_source_that_paused_after_connect_is_not_bursty_for_long(self):
        """The 2.0.1 review: a steady source sends 4 s at connect, nothing for 12 s while it
        starts up, then real time. Its connect burst counts for its gap, at most 10 s, until a
        second burst confirms it, and none comes: steady again after 10 s (it was 12 s for
        36 s), so it never builds a burst's room (2.1: the burst does not count for stay-live
        before two bursts' worth; VLC, which played the connect burst out, builds Normal's
        room once), then a stall shows within the steady bound and Go live lands at the
        target, not ~17 s behind."""
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=4.0, first=12.0, metered=True).ready()
        feed.run(12.0)  # the source sends again: its connect burst counts for 10 s
        self.assertEqual(feed.meter.burst_s, 10.0)
        start = len(self.timelines)
        feed.run(10.5)
        self.assertEqual(feed.meter.burst_s, 0.0)
        self.assertTrue(all(state["target"] <= 10.0 for state in self.timelines[start:]))
        timelines = len(self.timelines)
        feed.run(60.0)
        after = self.timelines[timelines:]
        self.assertEqual({state["target"] for state in after}, {TARGETS["normal"]})
        self.assertFalse(any(state["stalled"] for state in after))
        # Normal's room, built once, never a 10 s burst's.
        self.assertEqual(changes(self.playback.rates), 2)
        self.assertEqual(self.timelines[-1]["rate"], 1.0)
        self.assertAlmostEqual(self.timelines[-1]["lag"], TARGETS["normal"], delta=0.5)
        feed.stalled = True
        feed.run(2.5)  # "Reconnecting…" after the steady bound (2 s), not a 10 s burst's 12 s
        self.assertTrue(self.timelines[-1]["stalled"])
        feed.stalled = False
        feed.run(5.0)
        self.live.go_live()
        feed.sync()
        feed.run(5.0)
        self.assertEqual(len(self.playback.plays), 2)
        # A segment start within a GOP (2 s) of the target, not ~17 s behind.
        self.assertLess(self.timelines[-1]["lag"], TARGETS["normal"] + 2.0)

    def test_bursts_measured_for_moments_move_neither_the_lag_nor_the_rate(self):
        """A raw TS source in ~1 s chunks that the helper measures as bursty for a few
        seconds at a time (2.1 findings, one provider: 15 times in 5 minutes). 2.0.x moved
        the target and the lag formula with each, and changed the rate 40 times."""
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=3.0).ready()
        feed.run(30.0)
        lags, rates = [], len(self.playback.rates)
        for k in range(20):
            feed.burst = 3.2 if k % 2 else 0.0  # reported for 7 s, then not for 7 s
            feed.run(7.0)
            lags += [state["lag"] for state in self.timelines[-28:]]
        self.assertEqual(self.playback.rates[rates:], [])
        self.assertLess(max(lags) - min(lags), 0.5)
        self.assertEqual(len({state["shown"] for state in self.timelines[-4 * 140 :]}), 1)

    def test_no_rate_change_while_buffering(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.playback.set_state(PlaybackState.BUFFERING)
        self.step(0.25, media=0)
        self.assertEqual(self.playback.rates, [])

    def test_far_behind_reopens_at_live_once(self):
        sid = self.ready(segments(0, 20), live_edge=40.0)
        self.playback.set_state(PlaybackState.PLAYING)
        for k in range(1, 20):  # 40 s behind (> 2.5 + 20), the source sending on
            self.engine.buffer(sid, segments(0, 20), live_edge=40.0 + 0.25 * k)
            self.step(0.25)
        # Not within 5 s of the stream's open (its lag is not known before): then once.
        self.assertEqual(len(self.playback.plays), 1)
        self.engine.buffer(sid, segments(0, 20), live_edge=45.0)
        self.step(0.25)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 19))
        self.playback.set_state(PlaybackState.PLAYING)
        self.live.go_live()  # the viewer's Go live a moment after it: merged, one open
        for _ in range(8):
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 2)

    def test_a_reopen_the_tick_retried_is_merged_with_a_go_live(self):
        """Stage 1's last review: stay-live's reopen at live could not open (no stream from
        the helper for a moment) and the tick made it later, but not as a reopen: a Go live
        right after it opened a second time."""
        sid = self.ready(segments(0, 20), live_edge=40.0)
        self.playback.set_state(PlaybackState.PLAYING)
        for k in range(1, 20):  # 40 s behind; not within 5 s of the open
            self.engine.buffer(sid, segments(0, 20), live_edge=40.0 + 0.25 * k)
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 1)
        self.engine.up = False  # the helper serves no stream for a moment
        self.engine.buffer(sid, segments(0, 20), live_edge=45.0)
        self.step(0.25)  # stay-live reopens at live: it cannot yet
        self.assertEqual(len(self.playback.plays), 1)
        self.engine.up = True
        self.step(0.25)  # the tick makes it
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 19))
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.live.go_live()  # the viewer's Go live a moment after it: merged, one open
        for _ in range(8):
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), 2)

    def test_a_recovery_or_the_ram_ring_opens_even_just_after_a_reopen(self):
        # The merge (one open, not two) is for the Go live button and a seek to the edge:
        # a helper's restart and the RAM ring after a disk failure always open (review 2).
        for case in ("recovery", "disk"):
            with self.subTest(case=case):
                self.live = self.make()
                sid = self.ready(segments(0, 20), live_edge=40.0)
                self.playback.set_state(PlaybackState.PLAYING)
                for k in range(1, 22):  # far behind: stay-live reopens at live after 5 s
                    self.engine.buffer(sid, segments(0, 20), live_edge=40.0 + 0.25 * k)
                    self.step(0.25)
                self.assertEqual(len(self.playback.plays), 2)
                self.playback.set_state(PlaybackState.PLAYING)
                if case == "recovery":
                    self.engine.sessionFailed.emit(sid, "internal", "The helper died.")
                    self.engine.buffer(sid, segments(0, 20), live_edge=46.0)
                    self.engine.sessionReady.emit(sid)
                else:
                    self.engine.sessionFailed.emit(sid, "disk", "The disk buffer failed.")
                    self.engine.buffer(sid, segments(10, 13, start=20.0), live_edge=46.0)
                self.assertEqual(len(self.playback.plays), 3)

    def test_a_catch_up_from_far_behind_goes_on_through_stalls(self):
        """Review round 2: a stall while catching up from far behind ended the catch-up (it
        counted as trouble, the cushion having existed), and it started again 3 s later, at
        every stall; a gentle one spent the budget and left the lag behind. A steady source
        20 s behind with three 3 s stalls: one catch-up, as in 4577a9e."""
        for latency in ("normal", AUTO):
            with self.subTest(latency=latency):
                self.live = self.make()
                sid = self.start(latency=latency)
                feed = Feed(self, sid, burst=0.0, edge=20.0).ready()
                for _ in range(3):  # each one while still far behind (Auto raises its target)
                    feed.run(10.0)
                    feed.stalled = True
                    feed.run(3.0)
                    feed.stalled = False
                feed.run(240.0)
                rates = self.playback.rates
                self.assertLessEqual(changes(rates), 2)
                self.assertTrue(all(rate >= 1.0 for rate in rates))  # never slowed down
                self.assertEqual(len(self.playback.plays), 1)
                self.live.stop()
                self.engine.answer()

    def test_go_live_then_a_backlog_is_one_open_not_two(self):
        """One provider (2.1 review): after a stall it sent 21 s at once just after the
        viewer's Go live, and stay-live reopened at live 1.2 s later, measuring the new lag
        against the old position. Now an open waits for VLC to play it and 5 s before another
        reopen at live, so a Go live and stay-live's reopen are never two opens in a row."""
        sid = self.ready(segments(0, 30), live_edge=60.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.live.seek_to(16.0)  # time-shifted, 44 s behind
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        plays = len(self.playback.plays)
        self.live.go_live()  # the viewer's Go live
        self.assertEqual(len(self.playback.plays), plays + 1)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.buffer(sid, segments(0, 40), live_edge=81.0)  # a backlog lands at once
        for k in range(1, 19):  # 4.5 s, the source sending on
            self.engine.buffer(sid, segments(0, 40), live_edge=81.0 + 0.25 * k)
            self.step(0.25)
        self.assertEqual(len(self.playback.plays), plays + 1)  # no second open
        for k in range(19, 41):
            self.engine.buffer(sid, segments(0, 40), live_edge=81.0 + 0.25 * k)
            self.step(0.25)
        self.assertLessEqual(len(self.playback.plays), plays + 2)  # then at most one more

    def test_latency_change_moves_the_target(self):
        self.ready(segments(0, 2), live_edge=4.0)
        self.live.set_latency("stable")
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        self.assertEqual(self.playback.rates, [])
        self.assertEqual(self.timelines[-1]["target"], 6.0)


def spans_at(*starts, end=20.0):
    """Closed spans from (start, start quality) pairs, oldest first, the last up to `end`."""
    ends = [start for start, _ in starts[1:]] + [end]
    return [
        Span(k, start, stop, False, quality)
        for k, ((start, quality), stop) in enumerate(zip(starts, ends))
    ]


class SafeJumpTests(LiveSessionCase):
    """2.1 stage 5 (safe jumps): Go live, stay-live's reopen at live and seeks open where
    playback starts cleanly (a keyframe, else an intra picture; a start without either only
    when nothing else is in reach, which a debug log says), and far behind one such jump
    replaces a long 1.10x catch-up, never two within a minute."""

    def go_live_over(self, *starts, video=None):
        """Go live over closed spans starting at `starts` ((start, quality), oldest first) up
        to a live edge at 20.0 (each lag once VLC plays it: 20.5 less its start); the URL
        opened and what the session logged at debug level."""
        sid = self.ready(segments(0, 10), live_edge=20.0)
        self.engine.buffer(sid, spans_at(*starts), live_edge=20.0, video=video)
        log = mock.Mock()
        with mock.patch.object(live_session, "_LOG", log):
            self.live.go_live()
        debug = " ".join(str(call.args[0]) for call in log.debug.call_args_list)
        return self.playback.plays[-1][0], self.engine.stream_url(sid, 0)[:-4], debug

    def test_go_live_prefers_a_keyframe_then_an_intra_picture_never_a_start_without(self):
        cases = (
            # A keyframe within the band (3.0 s) over an intra picture at the target.
            (((0.0, "none"), (17.5, "key"), (18.0, "open"), (18.4, "none")), 1),
            # An intra picture at the target (3.0) over a keyframe beyond the band (4.1).
            (((0.0, "none"), (16.4, "key"), (17.5, "open"), (17.9, "none")), 2),
            # The keyframe 2 s further back (4.5) over a start without one at the target.
            (((0.0, "none"), (16.0, "key"), (18.0, "none")), 1),
        )
        for starts, seq in cases:
            with self.subTest(starts=starts):
                self.live = self.make()
                url, base, debug = self.go_live_over(*starts)
                self.assertEqual(url, f"{base}{seq}.ts")
                self.assertNotIn("keyframe", debug)

    def test_with_only_starts_without_a_keyframe_go_live_takes_the_targets_and_says_so(self):
        starts = ((0.0, "none"), (16.0, "none"), (18.0, "none"))
        url, base, debug = self.go_live_over(*starts)
        self.assertEqual(url, f"{base}2.ts")  # 2.5 s: the target's
        self.assertIn("keyframe", debug)
        # An audio-only stream has nothing else to start at: nothing to say.
        self.live = self.make()
        url, base, debug = self.go_live_over(*starts, video=False)
        self.assertEqual(url, f"{base}2.ts")
        self.assertNotIn("keyframe", debug)

    def test_a_seek_opens_where_playback_starts_cleanly(self):
        sid = self.ready(segments(0, 10), live_edge=20.0)
        kinds = ("key", "none", "none", "open", "key", "none", "none", "none", "none", "none")
        spans = [Span(k, 2.0 * k, 2.0 * k + 2.0, False, kind) for k, kind in enumerate(kinds)]
        self.engine.buffer(sid, spans, live_edge=20.0)
        for t, seq in (
            (0.5, 0),  # a keyframe start holds it
            (5.0, 0),  # no clean start holds it: the one before it (review I1: never after)
            (6.5, 3),  # an intra picture holds it (the keyframe start after it waits)
            (7.9, 3),
            (13.0, 4),  # the clean start before it, 5 s back
        ):
            with self.subTest(t=t):
                self.live.seek_to(t)
                self.assertEqual(self.playback.plays[-1], self.stream(sid, seq))
        # Nothing clean at or before it within 10 s: a clean start just after it (2 s).
        spans = [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "none") for k in range(10)]
        spans[6] = Span(6, 12.0, 14.0, False, "key")
        self.engine.buffer(sid, spans, live_edge=20.0)
        self.live.seek_to(10.5)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 6))

    def gop20(self):
        """A keyframe every 20 s, a forced cut ("none") 10 s after each, to 100 s."""
        sid = self.ready(segments(0, 10, length=10.0), live_edge=100.0)
        kinds = ("key", "none")
        spans = [Span(k, 10.0 * k, 10.0 * k + 10.0, False, kinds[k % 2]) for k in range(10)]
        self.engine.buffer(sid, spans, live_edge=100.0)
        return sid

    def play(self, seconds):
        self.playback.set_state(PlaybackState.PLAYING)
        for _ in range(round(seconds / 0.25)):
            self.clock.now += 0.25
            self.playback.ms += 250
            self.live._tick()

    def test_back_steps_on_long_keyframe_intervals_always_move_back(self):
        # Review I1: 20 s GOPs, playing at 42: ← asked for 32 (no clean start there), and the
        # keyframe at 40, 8 s on, won every time: four presses, four opens at 40.
        sid = self.gop20()
        self.live.seek_to(45.0)
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 4))  # from 40
        landed = []
        for _ in range(4):
            self.play(2.0)
            before = self.live.position()
            self.live.seek_relative(-10.0)
            landed.append(self.live.position())
            self.assertLess(landed[-1], before - 5.0)  # back, roughly 10 s each
        self.assertEqual(landed, [30.0, 20.0, 10.0, 0.0])

    def test_a_forward_step_never_lands_at_or_behind_where_it_plays(self):
        sid = self.gop20()
        self.live.seek_to(41.0)
        self.play(1.0)  # at 42
        self.live.seek_relative(10.0)  # 52: the start after it is a forced cut
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 5))  # not back at 40
        self.assertEqual(self.live.position(), 50.0)

    def test_an_evicted_position_reopens_no_earlier_than_the_margin(self):
        # Review: a keyframe at the oldest point, starts without one after it: the reopen 5 s
        # past the oldest point went back to it, which the next eviction may beat.
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(1.0)
        spans = [Span(50, 100.0, 102.0, False, "key")]
        spans += [Span(50 + k, 100.0 + 2 * k, 102.0 + 2 * k, False, "none") for k in range(1, 6)]
        self.engine.buffer(sid, spans, live_edge=112.0)
        self.playback.set_state(PlaybackState.ENDED)
        self.playback.ended.emit()
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 52))  # holding 105
        self.assertEqual(self.notices, [REWOUND])

    def test_on_an_audio_only_stream_far_behind_jumps(self):
        # Review: every start is "none" there, so it never jumped (1.10x every time).
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        none = [Span(s.seq, s.start, s.end, False, "none") for s in self.engine.spans(sid)]
        self.engine.buffer(sid, none, live_edge=feed.edge, open_seq=2, video=False)
        feed.push = lambda: self.engine.buffer(  # every state: audio only
            sid,
            [Span(k, 2.0 * k, 2.0 * k + 2.0, False, "none") for k in range(int(feed.edge // 2))],
            live_edge=feed.edge,
            open_seq=int(feed.edge // 2),
            open_quality="none",
            video=False,
        )
        feed.run(65.0)
        self.assertIsNotNone(self.far_behind(feed, 10.0))
        feed.run(20.0)
        self.assertEqual(self.playback.rates, [])

    def test_go_live_lands_where_stay_live_then_measures_it(self):
        # Review I3's measurement (a sparse-PCR channel, 8 landings): Go live and jumps landed
        # 1.7-2.3 s off their pick either way. The pick read the mean lag from the arrival
        # now (the helper's 12 s target duration; the batches were 10 s, some 6 s apart),
        # stay-live from the lag filter's mean over a dozen of them. The pick reads the
        # filter's mean edge too, once it has one.
        real = StayLive.landed
        spy = mock.patch.object(StayLive, "landed", autospec=True, side_effect=real)
        landed = self.enterContext(spy)
        for phase in (0.0, 1.5, 3.0, 4.5, 6.0, 7.5, 9.0):
            with self.subTest(phase=phase):
                self.live = self.make()
                sid = self.start(latency=AUTO)
                # Batches 7-13 s apart (no stall: one would restart the filter's mean).
                feed = Feed(self, sid, burst=10.0, edge=40.0, hls=True, jitter=3.0, seed=3)
                feed.ready(backlog_after_start=False)
                feed.burst = 12.0  # reported: the playlist's target duration
                feed.run(60.0 + phase)
                self.live._open_s = 0.0  # VLC opens at once here
                self.live.go_live()
                feed.sync()
                predicted = landed.call_args.args[1] + self.live._target(self.live._snapshot())
                feed.run(8.0)
                # The mean wanders a little as batches land (before: up to 2.2 s off).
                self.assertAlmostEqual(self.timelines[-1]["lag"], predicted, delta=0.6)
                self.live.stop()
                self.engine.answer()

    def test_what_landings_miss_by_is_learned_for_the_tune(self):
        # Review I3's measurement after the pick read the filter's mean: 8 landings on a
        # sparse-PCR channel, every one 0.1-0.7 s short (mean 0.3). Learned from each clean
        # landing (picked while playing, then 8 s at 1x), half of it at a time, bounded; and
        # afresh at each tune (ruling R38: not kept with the channel).
        real = StayLive.landed
        landed = self.enterContext(
            mock.patch.object(StayLive, "landed", autospec=True, side_effect=real)
        )
        for lead, latency, settled in ((0.4, "normal", 0.1), (3.0, "stable", 1.6)):
            with self.subTest(lead=lead):
                self.live = self.make()
                sid = self.start(latency=latency)
                feed = Feed(self, sid, burst=0.0, edge=12.0).ready(backlog_after_start=False)
                feed.lead = lead  # VLC's clock reads this much ahead after each open
                feed.run(20.0)
                errors = []
                for _ in range(5):
                    self.live.go_live()
                    feed.sync()
                    target = self.live._target(self.live._snapshot())
                    predicted = landed.call_args.args[1] + target
                    feed.run(15.0)
                    errors.append(self.timelines[-1]["lag"] - predicted)
                self.assertLess(errors[0], -lead + 0.15)  # the first misses by all of it
                self.assertLess(abs(errors[-1]), settled)  # later ones by less (bounded)
                self.assertLessEqual(abs(self.live._land_offset), live_session._LAND_OFFSET_MAX_S)
                self.assertLess(self.live._land_offset, -0.1)  # learned
                self.start(latency=latency)  # a re-tune of the same channel, the same session
                self.assertEqual(self.live._land_offset, 0.0)
                self.live.stop()
                self.engine.answer()

    def test_only_the_picks_own_error_is_learned(self):
        # Re-review N2: a 5 s backlog 5.5 s after a Go live (no stall) was learned as a miss
        # (+1.5 s, the cap), and picks right after the lag filter started afresh (a stall's
        # end, a step, a burst reclassified: when jumps happen) taught it its own wander.
        for case in ("a step inside the check", "a pick right after a restart"):
            with self.subTest(case=case):
                self.live = self.make()
                sid = self.start()
                feed = Feed(self, sid, burst=0.0, edge=12.0).ready(backlog_after_start=False)
                feed.run(30.0)
                if case == "a pick right after a restart":
                    feed.stalled = True  # the filter starts afresh once it sends again
                    feed.run(3.0)
                    feed.stalled = False
                    feed.run(3.0)
                self.live.go_live()
                feed.sync()
                if case == "a step inside the check":
                    feed.run(5.5)
                    feed.edge += 5.0  # a reconnect's backlog, at once
                    feed.push()
                feed.run(15.0)
                self.assertEqual(self.live._land_offset, 0.0)
                self.live.stop()
                self.engine.answer()

    def test_a_tune_then_a_higher_latency_is_built_to_it(self):
        # The start's build and what it accepts are for the start's own shortfall: Normal to
        # Stable 30 s after a tune at the target was built for 20 s, then 3.0 s behind a 6 s
        # target was accepted for good.
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=12.0).ready(backlog_after_start=False)
        feed.run(30.0)
        self.live.set_latency("stable")
        feed.run(300.0)
        self.assertEqual((min(self.playback.rates), changes(self.playback.rates)), (0.97, 2))
        self.assertAlmostEqual(self.timelines[-1]["lag"], TARGETS["stable"], delta=0.5)

    def test_a_backlog_inside_the_check_on_a_bursty_source_teaches_nothing(self):
        # Stage 5b's last review: on a bursty source a backlog under its burst moves the lag
        # filter's level without starting it afresh (a step there is beyond the burst); 5 s
        # 5.5 s into the check taught +1.78. The check is dropped on such a step, and what the
        # filter's mean edge moved meanwhile is no miss of the pick's either.
        cases = (
            (7.0, True, 5.0, 5.5, False),
            (7.0, True, 6.5, 5.5, False),
            (10.0, False, 6.0, 5.5, False),
            (7.0, True, 5.0, 1.0, True),  # on a filter that settled a moment ago
            (6.0, True, 6.0, 3.0, True),
        )
        for burst, hls, step, at, fresh in cases:
            with self.subTest(burst=burst, step=step, at=at, fresh=fresh):
                self.live = self.make()
                sid = self.start()
                feed = Feed(self, sid, burst=burst, edge=40.0, hls=hls)
                feed.ready(backlog_after_start=False)
                self.live._open_s = 0.0
                feed.run(40.0)
                if fresh:  # the filter starts afresh after a stall, and has just settled
                    feed.stalled = True
                    feed.run(burst + 3.0)
                    feed.stalled = False
                    feed.run(20.0)
                else:
                    feed.run(20.0)
                self.live.go_live()
                feed.sync()
                feed.run(at)
                feed.edge += step
                feed.push()
                feed.run(15.0)
                self.assertLess(abs(self.live._land_offset), 0.05)
                self.live.stop()
                self.engine.answer()

    def test_go_live_then_a_higher_latency_is_built_as_without_it(self):
        # Re-review N1: after a Go live the shortfall a higher target left was tolerated for
        # good (2.0 s behind a 6 s target for 5 minutes).
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=12.0).ready(backlog_after_start=False)
        feed.run(30.0)
        self.live.go_live()
        feed.sync()
        feed.run(20.0)
        self.live.set_latency("stable")
        feed.run(300.0)
        self.assertEqual(min(self.playback.rates), 0.97)
        self.assertAlmostEqual(self.timelines[-1]["lag"], TARGETS["stable"], delta=1.0)

    def test_stay_live_hears_when_a_stall_ends(self):
        # Seen on a public sparse-PCR channel: Go live during a stall that went on after it
        # left the lag 7 s short; that is the source's step, not the pick's (no tolerance).
        stepped = self.enterContext(mock.patch.object(StayLive, "source_stepped", autospec=True))
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        feed.run(10.0)
        feed.stalled = True
        self.live.go_live()
        feed.sync()
        feed.run(5.0)
        self.assertFalse(stepped.called)
        feed.stalled = False
        feed.run(1.0)
        self.assertEqual(stepped.call_count, 1)

    def test_a_jump_that_lands_short_changes_no_rate(self):
        # Review I3: each jump on a sparse-PCR channel read 1.2-1.4 s short once VLC played,
        # and 0.97x for 20 s followed (six rate changes).
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        feed.run(66.0)  # (the phase where the pick is at the target: 2.65 s)
        self.live._open_s += 1.3  # VLC opens at once: the jump lands 1.1-1.3 s short of it
        self.assertIsNotNone(self.far_behind(feed, 10.0))
        feed.run(120.0)
        self.assertEqual(self.playback.rates, [])  # before: 0.97x for 20 s
        lag = self.timelines[-1]["lag"]
        self.assertLess(lag, TARGETS["normal"] - 0.9)  # short, as it landed
        self.assertGreater(lag, 0.5)  # with room: no 0.95x

    def test_a_seek_with_no_clean_start_near_opens_where_it_was_asked_and_says_so(self):
        sid = self.ready(segments(0, 20), live_edge=40.0)
        spans = [
            Span(k, 2.0 * k, 2.0 * k + 2.0, False, "key" if k == 0 else "none") for k in range(20)
        ]
        self.engine.buffer(sid, spans, live_edge=40.0)
        log = mock.Mock()
        with mock.patch.object(live_session, "_LOG", log):
            self.live.seek_to(25.0)  # the keyframe at 0 is 25 s away
        self.assertEqual(self.playback.plays[-1], self.stream(sid, 12))
        debug = " ".join(str(call.args[0]) for call in log.debug.call_args_list)
        self.assertIn("keyframe", debug)

    def far_behind(self, feed, step):
        """`step` seconds more land at once (a backlog: drift, not the start's); then 0.25 s
        at a time until a new open, at most 30 s. Returns when it opened (None: it did not)."""
        plays = len(self.playback.plays)
        feed.edge += step
        feed.push()
        for _ in range(120):
            feed.run(0.25)
            if len(self.playback.plays) > plays:
                return self.clock.now
        return None

    def test_far_behind_one_jump_replaces_the_catch_up(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        self.assertEqual(self.playback.plays, [self.stream(sid, 2)])  # at the target
        feed.run(65.0)  # past the tune's minute
        at = self.far_behind(feed, 10.0)
        self.assertIsNotNone(at)
        feed.run(30.0)
        self.assertEqual(len(self.playback.plays), 2)
        self.assertEqual(self.playback.rates, [])  # no catch-up: never another rate
        self.assertAlmostEqual(self.timelines[-1]["lag"], TARGETS["normal"], delta=0.5)

    def test_again_within_a_minute_it_catches_up_then_jumps_once_the_minute_passed(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        feed.run(65.0)
        first = self.far_behind(feed, 10.0)
        feed.run(20.0)
        self.assertIsNone(self.far_behind(feed, 14.0))  # within the minute: no open ...
        self.assertEqual(max(self.playback.rates), 1.10)  # ... but a catch-up
        for _ in range(4 * 15):
            feed.run(0.25)
            if len(self.playback.plays) == 3:
                break
        # The catch-up's rest: one jump, a minute after the first.
        self.assertEqual(len(self.playback.plays), 3)
        self.assertGreaterEqual(self.clock.now - first, 60.0)
        self.assertEqual(self.playback.rates[-1], 1.0)
        feed.run(30.0)
        self.assertEqual(len(self.playback.plays), 3)

    def test_a_landing_a_long_keyframe_interval_above_the_target_never_gets_1_10(self):
        """Stage 3's open concern: one keyframe every 10 s (the 1-stream raw-TS provider's 4K
        channel), the tune 9.7 s above the target (the newer keyframe just short of the
        floor): nudged gently for its minute, then 1.10x. Now gently, then one jump on a
        keyframe that helps."""
        self.live._open_s = 0.0  # VLC opens at once here: the landing is the lag picked
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=22.2, gop=10.0).ready(backlog_after_start=False)
        self.assertEqual(self.playback.plays, [self.stream(sid, 1)])  # 12.2 s behind
        times = []
        for _ in range(4 * 150):
            feed.run(0.25)
            if len(self.playback.plays) > len(times) + 1:
                times.append(self.clock.now)
        self.assertLessEqual(max(self.playback.rates), 1.03)
        self.assertEqual(len(times), 1)
        self.assertGreaterEqual(times[0] - 1000.0, 60.0)
        self.assertLess(self.timelines[-1]["lag"], TARGETS["normal"] + 4.0)
        self.assertEqual(feed.starved_s, 0.0)

    def test_go_live_right_after_stay_lives_jump_opens_nothing_more(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=6.0).ready(backlog_after_start=False)
        feed.run(65.0)
        self.assertIsNotNone(self.far_behind(feed, 10.0))
        feed.run(1.0)
        self.live.go_live()  # the viewer's, a second after it: merged
        feed.run(30.0)
        self.assertEqual(len(self.playback.plays), 2)


class RateHealthTests(LiveSessionCase):
    """2.0.2: on some channels VLC cannot play at any rate but 1x; its position crawls (4.5 s
    of media in 37 s, measured). Stay-live sees it within its window (3 s counted, from two
    seconds after VLC plays) and keeps that channel at 1x: no rate change and no jump after.
    2.1 stage 2: the window runs from VLC's first step after counting starts to a step at
    least 3 s on and two steps later; this simulated crawl steps every 2 s, so it is seen 6 s
    after the rate changed (2.0.2 judged each tick: 3 s)."""

    def test_a_bursty_channel_that_cannot_slow_down_plays_at_normal_speed(self):
        # The measured channel: 10 s segments and 8 s buffered at the start, so stay-live
        # slows down (after 3 s); in 2.0.1 VLC crawled and stay-live jumped to live twice.
        # (2.1 stage 3: such a start comes only after the ready timeout, its room built at
        # 0.97x.)
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            feed = Feed(self, sid, burst=10.0, edge=8.0, crawl=True).ready()
            self.wait_until(lambda: self.playback.plays)
            feed.run(10.0)
            rates = list(self.playback.rates)
            self.assertEqual((rates[0], rates[-1], changes(rates)), (0.99, 1.0, 2))
            slowed = [state for state in self.timelines if state["rate"] != 1.0]
            self.assertLessEqual(len(slowed) * 0.25, 6.0 + 0.25)  # its first window
            feed.run(120.0)
        self.assertEqual(self.playback.rates, rates)  # never changed again
        self.assertEqual(len(self.playback.plays), 1)  # and no jump to live
        self.assertEqual(self.timelines[-1]["rate"], 1.0)
        self.assertEqual(len(logs.records), 1)  # said once
        self.assertNotIn("provider.example", logs.output[0])
        self.assertNotIn("://", logs.output[0])

    def test_a_steady_channel_that_cannot_catch_up_plays_at_normal_speed(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=12.0, crawl=True).ready().run(10.0)
        rates = list(self.playback.rates)
        self.assertEqual((rates[0], rates[-1], changes(rates)), (1.0333, 1.0, 2))  # far: 1.10x
        self.assertGreater(self.timelines[-1]["lag"], TARGETS["normal"] + 8.0)  # far behind
        feed.run(120.0)
        self.assertEqual(self.playback.rates, rates)
        # 2.1 stage 5 (review I2): far behind, once a minute passed since the tune, one jump
        # to live, which changes no rate (before, it stayed far behind until 20 s beyond).
        self.assertEqual(len(self.playback.plays), 2)
        self.assertLess(abs(self.timelines[-1]["lag"] - TARGETS["normal"]), 1.0)

    def test_a_channel_that_plays_any_rate_keeps_stay_live_as_before(self):
        sid = self.start()
        feed = Feed(self, sid, burst=0.0, edge=12.0).ready().run(120.0)
        rates = list(self.playback.rates)
        self.assertEqual((max(rates), rates[-1], changes(rates)), (1.10, 1.0, 2))  # far behind
        feed.edge += 6.0  # a backlog arrives: behind again, and caught up again (gently)
        feed.push()
        feed.run(240.0)
        after = self.playback.rates[len(rates) :]
        self.assertEqual((max(after), after[-1], changes(after)), (1.03, 1.0, 2))
        self.assertFalse(self.live._stay.rate_locked)
        self.assertEqual(len(self.playback.plays), 1)

    def test_vlc_waiting_at_the_live_edge_is_not_taken_for_a_crawl(self):
        """VLC meets the edge before the next burst and waits there, its clock standing still
        while it reports Playing (libVLC 3), as stay-live builds the room: no crawl. (2.1
        stage 3: a start this short comes only after the ready timeout.)"""
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start()
        feed = Feed(self, sid, burst=10.0, edge=3.0, starved=PlaybackState.PLAYING).ready()
        self.wait_until(lambda: self.playback.plays)
        feed.run(30.0)
        self.assertGreater(feed.starved_s, 5.0)  # it waited at the edge for the first burst
        self.assertLess(min(self.playback.rates), 1.0)  # building the room
        self.assertFalse(self.live._stay.rate_locked)


class HlsRateTests(LiveSessionCase):
    """2.0.2: an HLS source changes speed as any other (the measured crawl was not the rate;
    rate health still guards it), and a speed the user chooses applies."""

    def test_behind_the_target_it_catches_up(self):
        sid = self.start()
        Feed(self, sid, burst=6.0, edge=12.0, hls=True).ready().run(120.0)
        self.assertEqual(max(self.playback.rates), 1.03)
        self.assertEqual(self.playback.rates[-1], 1.0)
        self.assertEqual(len(self.playback.plays), 1)

    def test_short_of_its_room_it_waits_until_a_start_is_far_enough_back(self):
        # The measured channel's shape: 10 s segments and 8 s buffered at the start. 2.0.1
        # started there and slowed to 0.95x. 2.1 stage 3 waits for a start as far back as
        # the 10 s target (its mean over a batch); the ready timeout comes first: from the
        # oldest then, 9.5 s behind once playing, 8.5 s of room to the next batch.
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=10.0, edge=8.0, hls=True).ready()
        feed.run(5.75)
        self.assertEqual(self.playback.plays, [])
        self.assertEqual(self.notices, [LOADING])
        self.live._on_ready_timeout()  # 6 s after the start (a QTimer in the app)
        self.assertEqual(self.playback.plays[0][0], self.engine.stream_url(sid, 0))
        feed.run(60.0)
        self.assertEqual(feed.starved_s, 0.0)
        self.assertTrue(all(rate >= 0.97 for rate in self.playback.rates))

    def test_a_crawl_at_another_rate_still_locks_1x(self):
        sid = self.start()
        Feed(self, sid, burst=6.0, edge=12.0, hls=True, crawl=True).ready().run(30.0)
        self.assertEqual(self.playback.rates[-1], 1.0)
        self.assertTrue(self.live._stay.rate_locked)


class RestartTests(LiveSessionCase):
    """2.0.2 review (I1): after VLC ran dry at the live edge it needs a moment to restart: it
    refills its caching while its clock stands still and libVLC 3 says Playing. That restart
    is no crawl: it never locks 1x or plays directly, on jittery raw TS or HLS."""

    def test_a_frozen_restart_after_running_dry_is_no_crawl(self):
        sources = ((False, 2.0, 1.0), (True, 6.0, 3.5), (True, 4.0, 2.5))
        for hls, burst, jitter in sources:
            for recover in (0.8, 1.6):
                for seed in (1, 2):
                    with self.subTest(hls=hls, burst=burst, recover=recover, seed=seed):
                        self.live = self.make(tuning=LiveTuning(None))
                        sid = self.start(channel="A")
                        feed = Feed(
                            self,
                            sid,
                            burst=burst,
                            edge=1.0,
                            jitter=jitter,
                            seed=seed,
                            hls=hls,
                            starved=PlaybackState.PLAYING,
                            recover=recover,
                        ).ready()
                        feed.run(300.0)
                        self.assertGreaterEqual(feed.recoveries, 1)  # it ran dry, restarted
                        self.assertFalse(self.live._stay.rate_locked)
                        self.assertNotIn("direct", self.modes)


class CrawlFallbackTests(LiveSessionCase):
    """2.0.2: on some HLS channels VLC crawls through the pipeline even at 1x (measured: about
    0.8x with a jump every ~23 s), while VLC's own HLS plays them at 1x. Under 70% progress
    over 8 s at 1x with at least 3 s buffered ahead, the channel plays directly, with one
    notice, and is remembered for a day: its next tune plays directly at once."""

    CRAWLING = "Plays without rewind for smoother playback"

    def setUp(self):
        self.now = [1_000_000.0]
        self.tuning = LiveTuning(None, now=lambda: self.now[0])
        super().setUp()

    def make(self, **kwargs):
        kwargs.setdefault("tuning", self.tuning)
        return super().make(**kwargs)

    def tune(self, channel="A", *, hls=True, edge=12.0, **kwargs):
        plays = len(self.playback.plays)
        self.start(channel=channel)
        self.engine.answer()  # the channel before closes
        if len(self.playback.plays) > plays:
            return None  # played directly at once
        return Feed(self, self.engine.last_sid, burst=6.0, edge=edge, hls=hls, **kwargs).ready()

    def test_an_hls_channel_that_crawls_at_1x_plays_directly_once(self):
        feed = self.tune(crawl_1x=True)
        sid = self.engine.last_sid
        # 2.1 stage 2: each 8 s window ends at a step of VLC's clock (2 s apart in this
        # simulated crawl): direct play at 32 s (2.0.2, judged each tick: 30 s).
        feed.run(33.0)
        # Its nudge crawled first: 1x for good (rate health), and it still crawled at 1x.
        self.assertEqual(self.playback.rates[-1], 1.0)
        self.assertIn(sid, self.engine.closed)  # the pipeline's upstream closes first
        self.assertNotIn("direct", self.modes)
        self.engine.answer()
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.modes[-1], "direct")
        self.assertEqual(self.notices, [self.CRAWLING])  # said once
        self.assertEqual(self.tuning.get("A"), Tuning(rate_locked=True, direct=True))
        self.assertEqual(self.live.no_rewind_reason, self.CRAWLING)  # why, on a rewind
        feed.run(30.0)
        self.assertEqual(self.notices, [self.CRAWLING])
        self.assertEqual(self.playback.plays[-1], DIRECT)

    def test_a_single_freeze_does_not_send_it_to_direct_play(self):
        # A healthy HLS channel whose VLC stands still for 5 s once, with plenty buffered (a
        # decoder or audio restart): one bad window, never two in a row (2.0.2 review, I3).
        feed = self.tune(edge=20.0)
        feed.run(30.0)
        feed.frozen_until = self.clock.now + 5.0
        feed.run(60.0)
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.modes, ["pipeline"])
        self.assertNotIn(self.CRAWLING, self.notices)
        self.assertFalse(self.tuning.get("A").direct)

    def test_its_next_tune_plays_directly_at_once_and_a_day_later_tries_again(self):
        self.tuning.remember("A", Tuning(direct=True))
        opened = len(self.engine.opened)
        self.assertIsNone(self.tune())
        self.assertEqual(len(self.engine.opened), opened)  # no pipeline session at all
        self.assertEqual(self.playback.plays[-1], DIRECT)
        self.assertEqual(self.notices[-1], UNAVAILABLE)  # not toasted at tune
        self.assertNotIn(self.CRAWLING, self.notices)
        self.assertEqual(self.live.no_rewind_reason, self.CRAWLING)  # a rewind says why
        self.now[0] += 24 * 60 * 60
        feed = self.tune()
        self.assertIsNotNone(feed)  # the pipeline again
        feed.run(20.0)
        self.assertEqual(self.modes[-1], "pipeline")

    def test_a_healthy_hls_channel_stays_on_the_pipeline_with_its_nudges(self):
        feed = self.tune()
        feed.run(120.0)
        self.assertIn(1.03, self.playback.rates)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.engine.closed, [])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.tuning.get("A"), Tuning())

    def test_raw_ts_is_left_as_it_was(self):
        feed = self.tune(hls=False, crawl_1x=True)
        feed.run(60.0)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.engine.closed, [])
        self.assertFalse(self.tuning.get("A").direct)

    def test_waiting_at_the_live_edge_is_no_crawl(self):
        # 1x for good, a short buffer and a source that keeps stalling: VLC waits at the edge
        # with under 3 s ahead, and plays at 1x in between.
        self.tuning.remember("A", Tuning(rate_locked=True))
        feed = self.tune(edge=3.0, starved=PlaybackState.PLAYING)
        for _ in range(6):
            feed.stalled = True
            feed.run(9.0)
            feed.stalled = False
            feed.run(6.0)
        self.assertGreater(feed.starved_s, 10.0)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertNotIn(self.CRAWLING, self.notices)


class SoftwareFallbackTests(LiveSessionCase):
    """2.1: hardware decoding froze the channel's picture while its sound played
    (`PlaybackSession.pictureFroze`, tests/test_video_decoding.py): the channel decodes in
    software from then on, and for a day (`LiveTuning`): a time-shift stream opens again where
    it plays; a direct play opened itself again."""

    def setUp(self):
        self.now = [1_000_000.0]
        self.tuning = LiveTuning(None, now=lambda: self.now[0])
        super().setUp()

    def make(self, **kwargs):
        kwargs.setdefault("tuning", self.tuning)
        return super().make(**kwargs)

    def tune(self, channel="A", *, edge=20.0):
        self.start(channel=channel)
        self.engine.answer()  # the channel before closes
        return Feed(self, self.engine.last_sid, burst=0.0, edge=edge).ready()

    def test_a_time_shift_stream_opens_again_where_it_plays_in_software(self):
        feed = self.tune()
        feed.run(10.0)
        self.assertEqual(set(self.playback.software), {False})  # hardware first
        plays, before = len(self.playback.plays), self.live.position()
        self.playback.pictureFroze.emit()
        self.assertEqual(len(self.playback.plays), plays + 1)
        url, kind, _ = self.playback.plays[-1]
        self.assertIs(kind, PlayKind.LIVE_PIPELINE)
        self.assertTrue(self.playback.software[-1])
        # Where it played: the segment holding the position (a keyframe start).
        self.assertLessEqual(before - self.live.position(), 2.0)
        self.assertGreaterEqual(before - self.live.position(), 0.0)
        self.assertEqual(self.tuning.get("A"), Tuning(software=True))
        feed.run(5.0)
        self.live.go_live()  # every later open of the tune too
        self.assertTrue(all(self.playback.software[plays:]))
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.notices, [])

    def test_its_next_tune_decodes_in_software_and_a_day_later_tries_hardware(self):
        self.tuning.remember("A", Tuning(software=True))
        self.tune().run(5.0)
        self.assertEqual(set(self.playback.software), {True})
        self.tune("B").run(5.0)  # another channel: hardware
        self.assertFalse(self.playback.software[-1])
        self.now[0] += 24 * 60 * 60
        plays = len(self.playback.plays)
        self.tune().run(5.0)
        self.assertEqual(set(self.playback.software[plays:]), {False})

    def test_a_direct_play_that_froze_stays_in_software(self):
        self.engine.lose()  # no helper: the channel plays directly
        self.start(channel="A")
        self.assertEqual((self.playback.plays[-1], self.playback.software[-1]), (DIRECT, False))
        plays = len(self.playback.plays)
        self.playback.pictureFroze.emit()
        self.assertEqual(len(self.playback.plays), plays)  # it opened itself again
        self.assertEqual(self.tuning.get("A"), Tuning(software=True))
        self.start(channel="A")  # its next tune
        self.assertEqual((self.playback.plays[-1], self.playback.software[-1]), (DIRECT, True))

    def test_a_direct_reopen_refused_at_once_is_tried_again(self):
        # Review M3: the reopen in software closes the stream first, and a provider allowing
        # one stream may still count it: refused at once, it is tried again, as after a
        # fallback, and only then reported.
        self.live = self.make(retry_ms=(20, 30, 40))
        self.engine.lose()
        self.start(channel="A")
        self.playback.set_state(PlaybackState.PLAYING)  # it played: errors reported at once
        self.playback.pictureFroze.emit()  # PlaybackSession opened it again in software
        plays = len(self.playback.plays)
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.failures, [])
        self.assertEqual(self.notices[-1], RECONNECTING)
        self.wait_until(lambda: len(self.playback.plays) == plays + 1)
        self.assertEqual((self.playback.plays[-1], self.playback.software[-1]), (DIRECT, True))

    def test_what_the_channel_taught_before_is_kept(self):
        self.tuning.remember("A", Tuning(extra=1.5, rate_locked=True))
        feed = self.tune()
        feed.run(5.0)
        self.playback.pictureFroze.emit()
        self.assertEqual(self.tuning.get("A"), Tuning(1.5, True, software=True))

    def test_auto_s_learned_target_and_software_decoding_are_both_kept(self):
        # Stage 4b saves the learned target on its own (trouble, a move, the tune's end):
        # the flag the store holds goes with it, as the lock and direct play do.
        self.start(latency=AUTO, channel="A")
        feed = Feed(self, self.engine.last_sid, burst=0.0, edge=20.0).ready()
        feed.run(10.0)
        self.playback.pictureFroze.emit()
        feed.run(5.0)
        self.live.stop()  # the tune's end saves the learned target (Auto's own save)
        self.engine.answer()
        self.assertTrue(self.tuning.get("A").software)

    def test_a_later_lesson_keeps_software_decoding(self):
        # A channel in software whose VLC then crawls at another rate: the lock's save keeps
        # the software flag (MemoryTests' crawl).
        self.tuning.remember("A", Tuning(software=True))
        self.start(latency=AUTO, channel="A")
        self.engine.answer()
        Feed(self, self.engine.last_sid, burst=0.0, edge=12.0, crawl=True).ready().run(10.0)
        kept = self.tuning.get("A")
        self.assertEqual((kept.rate_locked, kept.software), (True, True))

    def test_a_freeze_heard_before_the_stream_plays_changes_nothing(self):
        self.start(channel="A")
        plays = len(self.playback.plays)
        self.playback.pictureFroze.emit()  # a stale signal: nothing plays yet
        self.assertEqual((len(self.playback.plays), self.tuning.get("A")), (plays, Tuning()))


class StartCushionTests(LiveSessionCase):
    """Ruling R33: a tune started at a pick chosen for the target has its cushion, so VLC
    running dry right after it is trouble (Auto adds its step, the owed slow-down runs). A
    start that deliberately took less (a steady source's oldest segment after a second, the
    ready timeout's) has not."""

    def test_running_dry_after_a_start_chosen_for_the_target_is_trouble(self):
        # Measured on a public HLS channel: started for the 7 s target, the lag read 6.2 s
        # once its rhythm was learned (never the 6.75 s the cushion needed); VLC ran dry
        # before a 12 s batch, and Auto learned nothing. (Here Auto's target as the channel
        # left it, 8 s.)
        tuning = LiveTuning(None)
        tuning.remember("ch", Tuning(learned=Learned(need=8.0)))
        self.live = self.make(tuning=tuning)
        sid = self.start(latency=AUTO, channel="ch")
        feed = Feed(self, sid, burst=7.0, edge=25.0, hls=True).ready(backlog_after_start=False)
        target = self.live._target(self.live._snapshot())  # (Auto: 7 s segments, a second more)
        feed.sizes = iter([4.0, 4.0, 4.0, 12.0] + [7.0] * 100)  # short batches, then 12 s
        feed.run(20.0)
        lags = [state["lag"] for state in self.timelines]
        self.assertLess(max(lags), target - 0.25)  # no cushion read
        self.assertGreater(feed.starved_s, 0.5)  # VLC ran dry before the 12 s batch
        self.assertGreater(self.timelines[-1]["target"], target + 1.0)  # the learned rise
        feed.run(30.0)
        self.assertEqual(min(self.playback.rates), 0.95)  # the owed slow-down

    def test_a_timeout_start_that_took_less_has_no_cushion(self):
        # Nothing as far back as the 7 s target came within the ready timeout: from the
        # oldest, 0 s behind once playing; VLC runs dry before the next batch.
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=7.0, edge=3.0, hls=True).ready()
        target = self.live._target(self.live._snapshot())
        self.wait_until(lambda: self.playback.plays)
        feed.run(12.0)
        self.assertGreater(feed.starved_s, 0.5)
        self.assertEqual(self.timelines[-1]["target"], target)  # nothing ran out


class BackOffTests(LiveSessionCase):
    """Auto latency's trouble (2.0.2; since 2.1 stage 4b the learned target's,
    `LearnedTarget`): VLC running dry once a cushion existed raises the channel's target at
    once (its starvation prior's margin, about 1.6 s for the first), up to today's cap
    (Stable's plus 4 s; a segmented source: its burst plus 2 s); a steady source's stall is
    noted, a segmented one's is trouble at once (R37); quiet takes it down slowly, after it
    held for a while. A rise never jumps back: the room grows at 0.95x where the rate may
    change, else as VLC waits at 1x."""

    def stall(self, feed, seconds=4.0, after=20.0):
        """The source sends nothing for `seconds`, then sends on; the target after."""
        feed.stalled = True
        feed.run(seconds)
        feed.stalled = False
        feed.run(after)
        return self.timelines[-1]["target"]

    def dry_stall(self, feed, after=120.0):
        """A stall that runs VLC dry for a second and a half (its lag and that much more)."""
        return self.stall(feed, seconds=self.timelines[-1]["lag"] + 1.5, after=after)

    def settled(self, *, latency=AUTO, **kwargs):
        """A steady source, played 20 s from 3 s behind (within Normal's band: 1x)."""
        sid = self.start(latency=latency)
        feed = Feed(self, sid, burst=0.0, edge=3.0, **kwargs).ready().run(20.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS.get(latency, TARGETS["normal"]))
        return feed

    def test_each_dry_run_raises_the_target_up_to_the_cap(self):
        feed = self.settled()
        plays, rates = len(self.playback.plays), len(self.playback.rates)
        # Each stall runs VLC dry once its cushion was built again at 0.95x (3 minutes): the
        # slow-down owed starts as soon as VLC plays, beyond the budget.
        targets = [self.dry_stall(feed, after=180.0) for _ in range(8)]
        top = next(k for k, target in enumerate(targets) if target >= 9.9)
        self.assertEqual(targets[: top + 1], sorted(set(targets[: top + 1])))  # each higher ...
        self.assertGreater(top, 2)
        self.assertGreaterEqual(targets[-1], 9.9)  # ... up to Stable + 4 s
        self.assertLessEqual(max(state["target"] for state in self.timelines), 10.0)
        self.assertEqual(len(self.playback.plays), plays)  # never a jump back for room
        built = self.playback.rates[rates:]
        self.assertTrue(all(0.95 <= rate <= 1.0 for rate in built))  # room built slowly
        self.assertEqual(min(built), 0.95)

    def test_a_starvation_that_runs_out_a_full_cushion_raises_the_target(self):
        # At the target, the source sends nothing for 1.6 s three times with a quarter second
        # between: never a stall (under 2 s), but VLC runs dry and waits, its clock standing
        # still while it says Playing (libVLC 3): one dry run.
        feed = self.settled(starved=PlaybackState.PLAYING)
        timelines = len(self.timelines)
        for _ in range(3):
            feed.stalled = True
            feed.run(1.6)
            feed.stalled = False
            feed.run(0.25)
        feed.run(10.0)
        self.assertGreater(feed.starved_s, 0.5)
        self.assertFalse(any(state["stalled"] for state in self.timelines[timelines:]))
        target = self.timelines[-1]["target"]
        self.assertGreater(target, TARGETS["normal"] + 1.0)  # a margin, at once ...
        self.assertLess(target, TARGETS["normal"] + 3.0)  # ... once

    def test_starvations_before_the_first_cushion_do_not_raise_it(self):
        # Played from a second behind the edge, under the target (silent for the second the
        # start waits); the source is 1.9 s late at once, so VLC runs dry before any cushion
        # existed: nothing ran one out.
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=0.0, edge=1.0, starved=PlaybackState.PLAYING).ready()
        self.stall(feed, seconds=1.0, after=0.25)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertEqual(self.stall(feed, seconds=1.9, after=10.0), TARGETS["normal"])
        self.assertGreater(feed.starved_s, 0.5)
        self.assertFalse(any(state["stalled"] for state in self.timelines))

    def test_a_long_stall_is_one_rise(self):
        feed = self.settled()
        target = self.stall(feed, seconds=60.0)
        self.assertGreater(target, TARGETS["normal"] + 1.0)
        self.assertLess(target, TARGETS["normal"] + 3.0)

    def quick_quiet(self):
        """The learned target's hold and half-life cut to two minutes each."""
        self.enterContext(mock.patch.object(learned_target, "HOLD_S", 120.0))
        self.enterContext(mock.patch.object(learned_target, "HALF_LIFE_S", 120.0))

    def test_quiet_playing_takes_it_down_slowly_once_it_held(self):
        self.quick_quiet()
        feed = self.settled()
        raised = self.dry_stall(feed, after=60.0)
        self.assertGreater(raised, TARGETS["normal"] + 1.0)
        feed.run(30.0)  # within the hold: as it was
        self.assertEqual(self.timelines[-1]["target"], raised)
        targets = []
        for _ in range(4 * 1200):  # 20 minutes of quiet playing
            feed.run(0.25)
            targets.append(self.timelines[-1]["target"])
        self.assertTrue(all(a >= b - 1e-9 for a, b in zip(targets, targets[1:])))  # never up
        steps = [a - b for a, b in zip(targets, targets[1:])]
        self.assertLess(max(steps), 0.01)  # never a step down: Auto's quiet pace at most
        self.assertLess(targets[-1], raised - 1.0)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertNotIn(1.10, self.playback.rates)

    def test_paused_minutes_are_not_quiet_ones(self):
        self.quick_quiet()
        feed = self.settled()
        raised = self.dry_stall(feed, after=10.0)
        self.live.toggle_pause()
        feed.run(600.0)
        self.live.go_live()
        feed.sync()
        feed.run(20.0)  # 30 s of playing in all since: still within the hold
        self.assertEqual(self.timelines[-1]["target"], raised)

    def test_a_bursty_source_goes_up_to_two_seconds_above_what_it_needs(self):
        # 10 s segments (the helper's burst): a batch 13 s late runs VLC dry, trouble; the
        # margin takes the target 2 s above the larger of the burst and what its batches need
        # at most (2.0.2: the burst plus 2 s). Batches 23 s apart now and then are part of the
        # rhythm the need learns (not 3 cycles: no stall to the arrival model).
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=10.0, edge=20.0).ready().run(60.0)
        self.assertEqual(self.timelines[-1]["target"], 10.0)  # its burst: a regular rhythm
        targets = []
        for _ in range(3):  # 18 s late: what it owes comes after (lost, it would be a need)
            missed = feed.nominal
            feed.stalled = True
            feed.run(18.0)
            feed.stalled = False
            feed.edge += round((feed.nominal - missed) / feed.chunk) * feed.chunk
            feed.push()
            feed.run(150.0)
            targets.append(self.timelines[-1]["target"])
        learned = self.live._learned
        need = max(10.0, learned.need(10.0))
        self.assertAlmostEqual(learned.target(10.0), need + 2.0, delta=0.01)  # at the cap
        self.assertGreater(targets[0], 11.0)

    def test_a_channel_that_cannot_change_speed_builds_its_room_by_waiting(self):
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=0.0, edge=4.0, crawl=True).ready().run(24.0)
        rates = list(self.playback.rates)
        self.assertEqual(rates[-1], 1.0)  # rate health: 1x for good
        self.assertGreater(self.dry_stall(feed, after=60.0), TARGETS["normal"] + 1.0)
        self.assertEqual(self.playback.rates, rates)  # no 0.95x to build the room
        self.assertEqual(len(self.playback.plays), 1)  # and no jump back

    def test_fixed_latencies_never_back_off(self):
        for latency in ("low", "normal", "stable"):
            with self.subTest(latency=latency):
                self.live = self.make()
                feed = self.settled(latency=latency)
                for _ in range(3):
                    self.stall(feed)
                self.assertEqual({state["target"] for state in self.timelines}, {TARGETS[latency]})

    def test_auto_can_be_chosen_while_a_channel_plays_and_left_again(self):
        feed = self.settled(latency="normal")
        self.live.set_latency(AUTO)
        self.assertGreater(self.dry_stall(feed), TARGETS["normal"] + 1.0)
        self.live.set_latency("stable")
        feed.run(1.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["stable"])

    def test_the_timeline_says_auto_and_whether_trouble_raised_its_target(self):
        feed = self.settled()
        self.assertEqual(
            (self.timelines[-1]["auto"], self.timelines[-1]["adjusted"]), (True, False)
        )
        self.assertGreater(self.dry_stall(feed), TARGETS["normal"] + 1.0)
        self.assertTrue(self.timelines[-1]["adjusted"])
        self.live.set_latency("normal")
        feed.run(0.25)
        self.assertEqual(
            (self.timelines[-1]["auto"], self.timelines[-1]["adjusted"]), (False, False)
        )

    def test_adjusted_means_trouble_raised_the_target_not_its_segments(self):
        # Most HLS channels' segments are longer than Normal's 2.5 s: their target is the
        # burst from the start under any latency, and that is no adjustment. A stall there
        # that runs VLC dry is trouble.
        sid = self.start(latency=AUTO)
        feed = Feed(self, sid, burst=6.0, edge=20.0).ready(backlog_after_start=False)
        feed.run(30.0)
        self.assertEqual(self.timelines[-1]["target"], 6.0)
        self.assertFalse(self.timelines[-1]["adjusted"])
        feed.stalled = True
        feed.run(9.0)  # a stall (the burst plus 2 s) after a full cushion: VLC runs dry
        feed.stalled = False
        feed.run(10.0)
        self.assertGreater(self.timelines[-1]["target"], 7.5)
        self.assertTrue(self.timelines[-1]["adjusted"])

    def test_auto_opens_the_buffer_as_normal_does(self):
        self.start(latency=AUTO)
        self.assertEqual(self.engine.opened[-1][2]["live_delay"], live_session.LIVE_DELAY["normal"])


class MemoryTests(LiveSessionCase):
    """2.0.2: what a channel taught (Auto's target, "can't change speed") is kept per channel
    (`LiveTuning`, shared by the players): the channel's next tune starts there."""

    def setUp(self):
        self.tuning = LiveTuning(None)  # in memory; tests/test_live_tuning.py has the file
        super().setUp()

    def make(self, **kwargs):
        kwargs.setdefault("tuning", self.tuning)
        return super().make(**kwargs)

    def tune(self, channel, *, latency=AUTO, edge=4.0, **kwargs):
        """Tune `channel` (a zap: the channel before closes first) and play it."""
        self.start(latency=latency, channel=channel)
        self.engine.answer()
        return Feed(self, self.engine.last_sid, burst=0.0, edge=edge, **kwargs).ready()

    def stall(self, feed):
        feed.stalled = True
        feed.run(4.0)
        feed.stalled = False
        feed.run(5.0)

    def test_tunes_that_starve_right_after_the_start_leave_the_target_where_it_was(self):
        # Each tune plays from the oldest buffered point, a second behind the edge, and the
        # source is 1.9 s late at once: VLC runs dry before any cushion was built (2.0.2).
        self.tuning.remember("A", Tuning(extra=1.5))
        for channel, target in (("A", 4.0), ("B", TARGETS["normal"])):
            for _ in range(5):
                with self.subTest(channel=channel, tune=_):
                    feed = self.tune(channel, edge=1.0, starved=PlaybackState.PLAYING)
                    feed.stalled = True  # silent for the second the start waits (2.1 stage 3)
                    feed.run(1.0)
                    feed.stalled = False
                    feed.run(0.25)
                    feed.stalled = True
                    feed.run(1.9)
                    feed.stalled = False
                    feed.run(5.0)
                    self.assertGreater(feed.starved_s, 0.5)
                    self.assertAlmostEqual(self.timelines[-1]["target"], target, delta=0.05)
        # A's 2.0.2 extra became what it learned (the prior it was read as), B learned nothing.
        kept = self.tuning.get("A")
        self.assertIsNone(kept.extra)
        self.assertAlmostEqual(LearnedTarget(kept.learned).target(), 4.0, delta=0.05)
        self.assertEqual(self.tuning.get("B"), Tuning())

    def test_a_channel_that_cannot_change_speed_plays_at_1x_from_its_next_tune(self):
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            self.tune("A", edge=12.0, crawl=True).run(10.0)
            tried = list(self.playback.rates)
            self.assertEqual((tried[0], tried[-1], changes(tried)), (1.0333, 1.0, 2))
            self.assertEqual(self.tuning.get("A"), Tuning(rate_locked=True))
            rates = len(self.playback.rates)
            self.tune("A", edge=12.0, crawl=True).run(30.0)
            self.assertEqual(self.playback.rates[rates:], [])  # no other rate asked for
            self.assertGreater(self.timelines[-1]["lag"], TARGETS["normal"] + 3.0)
            self.tune("B", edge=12.0).run(5.0)
            self.assertEqual(self.playback.rates[rates], 1.0333)  # another channel: as before
        self.assertEqual(len(logs.records), 1)  # said once for the channel

    def test_a_channel_that_could_not_change_speed_is_tried_again_after_a_day(self):
        now = [1_000_000.0]
        self.tuning = LiveTuning(None, now=lambda: now[0])
        self.live = self.make()
        self.tune("A", edge=12.0, crawl=True).run(10.0)
        tried = list(self.playback.rates)
        self.assertEqual((tried[-1], changes(tried)), (1.0, 2))
        now[0] += 23 * 3600
        self.tune("A", edge=12.0, crawl=True).run(10.0)
        self.assertEqual(self.playback.rates, tried)  # still 1x only
        now[0] += 3600
        # A day on: rate changes are allowed again, and the crawl is seen and locked again.
        self.tune("A", edge=12.0, crawl=True).run(10.0)
        self.assertEqual(self.playback.rates, tried * 2)
        now[0] += 23 * 3600
        self.assertTrue(self.tuning.get("A").rate_locked)  # a day from the new lock
        now[0] += 3600
        # A day on again, and VLC now plays the channel at any rate: as before, no lock.
        rates = len(self.playback.rates)
        self.tune("A", edge=12.0).run(120.0)
        after = self.playback.rates[rates:]
        self.assertEqual((max(after), after[-1], changes(after)), (1.10, 1.0, 2))

    def test_auto_starts_a_channel_where_its_last_tune_left_it(self):
        feed = self.tune("A").run(20.0)
        feed.stalled = True  # VLC runs dry
        feed.run(self.timelines[-1]["lag"] + 1.5)
        feed.stalled = False
        feed.run(5.0)
        target = self.timelines[-1]["target"]
        self.assertGreater(target, TARGETS["normal"] + 1.0)
        self.assertIsNotNone(self.tuning.get("A").learned)
        self.tune("B").run(1.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"])
        kept = self.tuning.get("A").learned  # as A's tune left it when B's began
        self.tune("A").run(1.0)
        self.assertGreater(self.timelines[-1]["target"], TARGETS["normal"] + 1.0)
        self.assertAlmostEqual(
            self.timelines[-1]["target"], LearnedTarget(kept).target(), delta=0.05
        )
        self.assertEqual(self.tuning.get("B"), Tuning())  # nothing learned, nothing kept

    def test_a_fixed_latency_keeps_its_own_target_and_the_channels_locked_rate(self):
        self.tuning.remember("A", Tuning(extra=4.5, rate_locked=True))
        self.tune("A", latency="normal", edge=12.0).run(10.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"])
        self.assertEqual(self.playback.rates, [])  # far behind, and still 1x
        self.tune("A").run(1.0)  # Auto
        self.assertEqual(self.timelines[-1]["target"], 7.0)
        self.assertEqual(self.tuning.get("A"), Tuning(extra=4.5, rate_locked=True))

    def test_auto_chosen_while_a_channel_plays_starts_where_it_left_it(self):
        self.tuning.remember("A", Tuning(extra=3.0))
        feed = self.tune("A", latency="normal").run(1.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"])
        self.live.set_latency(AUTO)
        feed.run(1.0)
        self.assertAlmostEqual(self.timelines[-1]["target"], 5.5, delta=0.01)

    def test_a_target_back_where_auto_starts_is_forgotten(self):
        self.tuning.remember("A", Tuning(extra=0.5))
        self.tune("A").run(301.0)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"])
        self.assertEqual(self.tuning.get("A"), Tuning())

    def test_a_channel_without_a_key_teaches_nothing(self):
        feed = self.tune("").run(20.0)
        feed.stalled = True  # VLC runs dry
        feed.run(self.timelines[-1]["lag"] + 1.5)
        feed.stalled = False
        feed.run(5.0)
        self.assertGreater(self.timelines[-1]["target"], TARGETS["normal"] + 1.0)  # this tune's
        self.assertEqual(self.tuning.get(""), Tuning())


class StallTests(LiveSessionCase):
    """The source stops sending at the live edge; libVLC 3 keeps reporting Playing meanwhile.

    The review's scenarios (6 s HLS bursts, Normal): stalls of 15, 30 and 60 s.
    """

    def settle(self, *, burst=6.0):
        sid = self.start()
        feed = Feed(self, sid, burst=burst, edge=12.0, starved=PlaybackState.PLAYING).ready()
        feed.run(150.0)  # 3 s behind the target: one gentle nudge to it (2.1)
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
                # The stall drained the room before each burst: built again at 0.95x.
                self.assertEqual(self.timelines[-1]["rate"], 0.95)
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
        for k in range(1, 21):  # the clock moves again: 39 s behind, so back to live, once
            self.engine.buffer(sid, segments(0, 20), live_edge=40.0 + 0.25 * k)
            self.step(0.25)  # (5 s after the open: before, its lag was not known)
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
        # 2.1 stage 2: a clock at the rate, moved half way to each new reading of VLC's.
        self.assertAlmostEqual(self.live.position(), 10.525)
        for _ in range(8):
            self.step(0.25)
        self.assertAlmostEqual(self.live.position(), 10.55 + 2.0, delta=0.001)

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


class SparseClockTests(LiveSessionCase):
    """2.1 stage 2, measured on two public sparse-PCR HLS channels: VLC's get_time() stands
    still and steps by 2000 ms every 2 s. The old position rule took each step for a jump and
    counted a tick's wall time instead (0.09-0.16 of what VLC played): the lag read far too
    high (false catch-ups, a reopen at live every ~22 s), rate health saw a crawl and locked
    1x within seconds, the crawl check played the channel directly, and VLC's clock counted as
    frozen (starved) between its steps."""

    def setUp(self):
        super().setUp()
        self.live = self.make(tuning=LiveTuning(None))

    def tune(self, *, edge=12.0, burst=6.0, **kwargs):
        # Under Normal (the 6 s segments' target): the clock is the subject here; Auto's
        # learned target on such channels is tested in LearnedTargetTests and on the air.
        sid = self.start(latency="normal", channel="A")
        return Feed(self, sid, burst=burst, edge=edge, hls=True, pcr=2.0, **kwargs).ready()

    def errors(self, feed, seconds):
        """The position less VLC's real one at every tick for `seconds`."""
        out = []
        for _ in range(round(seconds / 0.25)):
            feed.run(0.25)
            out.append(self.live.position() - feed.position)
        return out

    def test_the_position_follows_vlc_between_its_steps(self):
        feed = self.tune(edge=8.0)  # within its band of a 6 s target: nothing to steer
        feed.run(10.0)
        errors = self.errors(feed, 120.0)
        self.assertLess(max(abs(e) for e in errors), 0.5)
        self.assertLess(abs(sum(errors) / len(errors)), 0.25)
        self.assertEqual(self.playback.rates, [])  # no false catch-up
        self.assertEqual(len(self.playback.plays), 1)  # and no reopen at live

    def test_a_channel_at_its_target_is_left_alone_for_minutes(self):
        feed = self.tune(edge=8.0)
        feed.run(300.0)
        self.assertLessEqual(changes(self.playback.rates), 2)
        self.assertEqual(len(self.playback.plays), 1)
        self.assertFalse(self.live._stay.rate_locked)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertEqual(self.notices, [])
        self.assertEqual(self.live._tuning.get("A"), Tuning())

    def test_a_seek_back_and_go_live_start_the_clock_at_their_segment(self):
        feed = self.tune(edge=30.0, burst=2.0)
        feed.run(20.0)
        for action in ("back", "live"):
            with self.subTest(action=action):
                plays = len(self.playback.plays)
                if action == "back":
                    self.live.seek_relative(-20.0)
                else:
                    self.live.go_live()
                feed.sync()
                self.assertEqual(len(self.playback.plays), plays + 1)
                errors = self.errors(feed, 20.0)
                self.assertLess(max(abs(e) for e in errors), 0.5)
                self.assertEqual(len(self.playback.plays), plays + 1)  # nothing more opened

    def test_a_late_or_missing_step_never_locks_a_healthy_channels_rate(self):
        # Stage 2 review: one 4 s step closed a window with nothing played at the clock's
        # hold, and a healthy channel at 1.10x was locked at 1x for a day. Here stay-live
        # holds 1.10x for 5 minutes while every 5th-10th of VLC's 2 s stamps is missing.
        rng, gaps, k = random.Random(5), set(), 0
        while k < 400:
            k += rng.randint(5, 10)
            gaps.add(k)
        feed = self.tune(edge=60.0, burst=0.0)
        feed.missing = gaps.__contains__
        with mock.patch.object(self.live._stay, "update", return_value=Decision(1.10)):
            feed.run(300.0)
        self.assertFalse(self.live._stay.rate_locked)
        self.assertEqual(self.playback.rates[-1], 1.10)

    def test_a_cadence_turning_coarse_at_another_rate_locks_nothing(self):
        # Stage 2 review (parked): fine quanta, then 2 s stamps mid-channel while at 1.10x or
        # 0.95x, one of the first three new stamps missing: its clock stood still longer than
        # the old cadence's holds, a window counted nothing played, and rate health locked a
        # healthy channel at 1x for a day.
        for rate in (1.10, 0.95):
            for missing in (1, 2, 3):
                with self.subTest(rate=rate, missing=missing):
                    self.live = self.make(tuning=LiveTuning(None))
                    sid = self.start(latency="normal", channel="A")
                    feed = Feed(self, sid, burst=0.0, edge=60.0, hls=True).ready()
                    decision = Decision(rate)
                    with mock.patch.object(self.live._stay, "update", return_value=decision):
                        feed.run(30.0)
                        feed.pcr = 2.0
                        first = math.floor(feed.media / 2.0 + 1e-9) + missing
                        feed.missing = {first}.__contains__
                        feed.run(60.0)
                    self.assertFalse(self.live._stay.rate_locked)
                    self.assertEqual(self.live._tuning.get("A"), Tuning())

    def test_a_real_crawl_with_missing_steps_still_locks_1x(self):
        feed = self.tune(edge=60.0, burst=0.0, speed=0.6)
        feed.missing = lambda k: k % 7 == 0
        with mock.patch.object(self.live._stay, "update", return_value=Decision(1.10)):
            feed.run(15.0)
        self.assertTrue(self.live._stay.rate_locked)

    def stamps_jump(
        self, feed, by, *, ahead=4.0, reported=None, late=0.0, media=False, newer=0, every=30.0
    ):
        """A few seconds ahead of VLC its stamps jump by `by`; the helper reports `reported`
        seconds left out there, `late` seconds further on (or nothing), and `newer` more
        breaks `every` seconds after it (a flaky source: VLC far behind them). With `media` the
        timeline kept the step (under 3 s; or VLC really skipped): the media after it, already
        buffered, is that much later on the timeline. The position's errors from then on."""
        at = feed.position + ahead
        feed.jump_at, feed.jump_by, feed.media_step = at, by, media
        if media:
            feed.edge += by
        if reported is not None:
            feed.reported = [(at + late + every * k, reported) for k in range(newer + 1)]
        feed.push()
        errors = self.errors(feed, 30.0)
        self.assertEqual(feed.stamp_offset, by)  # VLC's clock did jump
        return errors

    def test_a_dropped_ten_second_jump_the_helper_reports_leaves_the_lag(self):
        # Stage 2 review: an HLS discontinuity whose PCR jumps forward 10 s moves VLC's
        # get_time with it, but neither the helper's timeline nor what VLC plays: taken for a
        # skip, the lag would read 10 s short (a false 0.95x until the next open).
        feed = self.tune(edge=8.0)
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        errors = self.stamps_jump(feed, 10.0, reported=10.0)
        self.assertLess(max(abs(e) for e in errors), 0.5)
        self.assertAlmostEqual(self.timelines[-1]["lag"], lag, delta=1.0)
        self.assertTrue(all(rate >= 1.0 for rate in self.playback.rates))

    def test_a_viewer_far_behind_still_matches_its_break_among_many(self):
        # Stage 2 re-review: the helper kept the last eight breaks only, so a viewer minutes
        # behind on a flaky reconnecting source took older jumps for skips.
        feed = self.tune(edge=8.0)
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        errors = self.stamps_jump(feed, 10.0, reported=10.0, newer=19)
        self.assertLess(max(abs(e) for e in errors), 0.5)
        self.assertAlmostEqual(self.timelines[-1]["lag"], lag, delta=1.0)

    def test_a_two_second_step_the_timeline_kept_is_followed(self):
        # Stage 2 re-review: the segmenter keeps a step under 3 s as media (a quick
        # reconnect, an HLS sequence gap on 2 s segments): VLC's clock and the timeline agree,
        # so the position moves with it and the lag stays the timeline's.
        feed = self.tune(edge=8.0)
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        errors = self.stamps_jump(feed, 2.0, media=True)
        # (VLC's clock shows the step with its next stamp, up to 2 s after VLC is past it.)
        self.assertLess(max(abs(e) for e in errors[16 + 12 :]), 0.5)
        self.assertAlmostEqual(self.timelines[-1]["lag"], lag, delta=1.0)

    def test_a_reconnect_reported_a_gop_late_still_matches(self):
        # A steady raw-TS channel reconnects after an outage: its timestamps jump 10 s and the
        # segmenter's report may sit up to a GOP (here 1 s) from where VLC's clock jumps.
        sid = self.start(latency=AUTO, channel="A")
        feed = Feed(self, sid, burst=0.0, edge=3.0).ready()  # within its band: no steering
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        errors = self.stamps_jump(feed, 10.0, reported=10.0, late=1.0)
        self.assertLess(max(abs(e) for e in errors), 0.5)
        self.assertAlmostEqual(self.timelines[-1]["lag"], lag, delta=1.0)

    def test_a_real_skip_unlike_a_reported_break_is_taken(self):
        feed = self.tune(edge=8.0)
        feed.run(30.0)
        feed.reported = [(feed.position + 4.0, 10.0)]  # a 10 s break ahead ...
        errors = self.stamps_jump(feed, 5.0, media=True)  # ... and VLC skips 5 s there
        self.assertLess(max(abs(e) for e in errors[16 + 16 :]), 0.5)

    def test_vlcs_clock_is_frozen_only_well_past_its_steps(self):
        feed = self.tune(edge=8.5)  # from the oldest segment, 6 s behind: the target
        feed.run(10.0)
        frozen = []
        for _ in range(80):
            feed.run(0.25)
            frozen.append(self.live._clock_frozen())
        self.assertFalse(any(frozen))  # never between its 2 s steps
        while self.playback.ms == round(feed.media * 1000):  # until just after a step
            feed.run(0.25)
        for _ in range(8):
            if self.playback.ms != round(feed.media * 1000):
                break
            feed.run(0.25)
        feed.speed_1x = 0.0  # VLC stands still while it says Playing
        waited = 0.0
        while not self.live._clock_frozen() and waited < 10.0:
            feed.run(0.25)
            waited += 0.25
        self.assertGreater(waited, 2.0)  # not at its own interval ...
        self.assertLessEqual(waited, 1.5 * 2.0 + 0.25 + 0.5)  # ... but soon after it

    def test_a_catch_up_on_a_two_second_clock_is_not_taken_for_a_crawl(self):
        feed = self.tune(edge=20.0, burst=2.0)  # 18 s behind a 2.5 s target: 1.10x
        feed.run(200.0)
        self.assertEqual(max(self.playback.rates), 1.10)
        self.assertFalse(self.live._stay.rate_locked)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertLess(self.timelines[-1]["lag"], TARGETS["normal"] + 1.5)

    def test_a_real_crawl_on_a_two_second_clock_still_locks_1x(self):
        feed = self.tune(edge=20.0, burst=2.0, speed=0.6)
        feed.run(20.0)
        self.assertTrue(self.live._stay.rate_locked)
        self.assertEqual(self.playback.rates[-1], 1.0)
        self.assertEqual(self.modes, ["pipeline"])  # at 1x it plays as it should

    def test_the_crawl_check_leaves_a_healthy_two_second_clock_alone(self):
        feed = self.tune()
        feed.run(120.0)
        self.assertEqual(self.modes, ["pipeline"])
        self.assertNotIn(CrawlFallbackTests.CRAWLING, self.notices)
        self.assertFalse(self.live._tuning.get("A").direct)

    def test_a_real_crawl_at_1x_on_a_two_second_clock_still_plays_directly(self):
        feed = self.tune(edge=8.0, speed=0.6, speed_1x=0.6)
        feed.run(60.0)
        self.engine.answer()
        self.assertEqual(self.modes[-1], "direct")
        self.assertEqual(self.notices, [CrawlFallbackTests.CRAWLING])
        self.assertTrue(self.live._tuning.get("A").direct)


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
        self.step(0.25)  # a tick: the session knows the buffer up to 6 s
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
                sid = self.ready(segments(0, 7), live_edge=14.0)
                self.playback.set_state(PlaybackState.PLAYING)
                for k in range(1, 16):  # the source sends on, 11.5 s more than the target
                    self.engine.buffer(sid, segments(0, 7), live_edge=14.0 + 0.25 * k)
                    self.step(0.25)
                self.assertGreater(self.playback.rates[-1], 1.0)  # far behind: after 3 s
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
        # Without a buffer before it, a direct play's error is reported at once.
        self.engine.lose()
        self.start()
        plays = len(self.playback.plays)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.failures, ["The stream could not be played"])
        self.assertEqual(len(self.playback.plays), plays)

    def fall_back_on_one_stream(self, kind="network"):
        """Plays from the buffer until the source gives up at the live edge; the direct play
        opens once the buffer's upstream is closed. The plays before it."""
        self.live = self.make(retry_ms=(20, 30, 40))
        self.failures.clear()
        self.notices.clear()
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        plays = len(self.playback.plays)
        self.engine.sessionFailed.emit(sid, kind, "The stream could not be reached.")
        self.assertIn(sid, self.engine.closed)
        self.assertEqual(len(self.playback.plays), plays)  # the buffer still holds it
        self.engine.answer()  # its upstream is closed
        self.assertEqual(self.playback.plays[-1], DIRECT)
        return plays

    def test_a_fallback_on_a_one_stream_provider_tries_its_direct_play_again(self):
        """The owner's provider allows one stream (2.1 review): its source gave up at the
        live edge, the direct play failed at once ("The stream could not be opened": the
        provider still counted the buffer's connection) and nothing played for minutes. The
        direct play opens only once the buffer's upstream is closed (never two connections),
        and a failure right after is tried again up to three times (3, 5 and 10 s apart),
        "Reconnecting…" each time, and only then reported (ruling R19)."""
        for kind in ("network", "crashed"):
            with self.subTest(kind=kind):
                plays = self.fall_back_on_one_stream(kind)
                self.playback.set_state(PlaybackState.ERROR)
                for tries in range(1, 4):
                    self.playback.errorOccurred.emit("The stream could not be opened")
                    self.assertEqual(self.failures, [])  # not yet: again after a while
                    self.assertEqual(self.notices[-1], RECONNECTING)
                    self.wait_until(lambda: len(self.playback.plays) == plays + 1 + tries)
                    self.assertEqual(self.playback.plays[-1], DIRECT)
                self.playback.errorOccurred.emit("The stream could not be opened")
                self.assertEqual(self.failures, ["The stream could not be opened"])
                QTest.qWait(80)
                self.assertEqual(len(self.playback.plays), plays + 4)  # four tries, no more
                self.assertEqual(self.notices.count(RECONNECTING), 3)

    def test_a_direct_play_that_timed_out_is_reported_at_once(self):
        """Stage 1's last review: a provider that does not answer times out after VLC's open
        timeout (20 s), and retrying that showed "Reconnecting…" for a minute and a half.
        Only a failure within a few seconds of its play call is tried again."""
        plays = self.fall_back_on_one_stream()
        self.clock.now += 20.0  # VLC gave up opening it
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("Timed out opening the stream")
        self.assertEqual(self.failures, ["Timed out opening the stream"])
        self.assertNotIn(RECONNECTING, self.notices)
        QTest.qWait(80)
        self.assertEqual(len(self.playback.plays), plays + 1)

    def test_a_retry_that_times_out_is_reported_at_once(self):
        plays = self.fall_back_on_one_stream()
        self.clock.now += 4.0  # refused within a few seconds: tried again
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be opened")
        self.assertEqual(self.failures, [])
        self.wait_until(lambda: len(self.playback.plays) == plays + 2)
        self.clock.now += 20.0  # the retry timed out
        self.playback.errorOccurred.emit("Timed out opening the stream")
        self.assertEqual(self.failures, ["Timed out opening the stream"])
        QTest.qWait(80)
        self.assertEqual(len(self.playback.plays), plays + 2)
        self.assertEqual(self.notices.count(RECONNECTING), 1)

    def test_a_fallback_that_played_reports_a_later_error_at_once(self):
        plays = self.fall_back_on_one_stream()
        self.playback.set_state(PlaybackState.PLAYING)  # it played
        self.playback.set_state(PlaybackState.ERROR)
        self.playback.errorOccurred.emit("The stream could not be played")
        self.assertEqual(self.failures, ["The stream could not be played"])
        QTest.qWait(80)
        self.assertEqual(len(self.playback.plays), plays + 1)


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

    def test_a_backlog_step_right_after_a_recovery_is_part_of_its_landing(self):
        # Re-review nit 4: the restarted helper's session went live at the target, then its
        # raw-TS backlog landed and was caught up at 1.10x as drift.
        sid = self.ready(segments(0, 3), live_edge=6.0)
        self.playback.set_state(PlaybackState.PLAYING)
        self.engine.sessionFailed.emit(sid, "internal", "The live buffer stopped unexpectedly.")
        self.engine.available.emit(True)
        self.engine.buffer(sid, segments(0, 2), live_edge=4.0)
        self.engine.sessionReady.emit(sid)
        self.playback.set_state(PlaybackState.PLAYING)
        self.step(0.25)
        edge = 14.0  # 10 s more at once: the backlog
        for _ in range(80):
            self.engine.buffer(sid, segments(0, 7), live_edge=edge)
            self.step(0.25)
            edge += 0.25
        self.assertLessEqual(max(self.playback.rates or [1.0]), 1.03)

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
        self.engine.buffer(sid, [], open_seq=0, live_edge=2.0)
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
        self.assertEqual(self.playback.plays, [self.stream(sid, 2)])  # at the target
        self.engine.sessionFailed.emit(sid, "unsupported", "This stream format isn't supported.")
        self.engine.answer()
        self.engine.available.emit(True)  # direct
        self.assertEqual(len(self.playback.plays), 2)
        self.assertEqual(self.notices, [LOST])
        self.assertEqual(self.modes, ["pipeline", "direct"])


class RepickTests(LiveSessionCase):
    """A tune's start picked again (`REPICK`, ruling R46): only a dump right after the open
    that the source then flows on from at about real time, never a burst (which flows at 3x
    and more, then stands still), however it lands or is spread, never long after the open or
    when VLC is slow to play it. Each case against 4bb2d7c's (stage 4b re-reviews 1-2): one
    open and VLC never dry, where that ran dry 4-7 s."""

    def tune(self, latency, **kwargs):
        self.live = self.make(tuning=LiveTuning(None))
        sid = self.start(latency=latency, channel="A")
        return Feed(self, sid, metered=True, starved=PlaybackState.PLAYING, **kwargs)

    def opened(self, feed):
        for _ in range(40):
            if self.playback.plays:
                return
            feed.run(0.05)
        self.fail("the tune never opened")

    def outcome(self, feed, seconds=60.0):
        feed.run(seconds)
        out = (len(self.playback.plays), round(feed.starved_s, 2))
        self.live.stop()
        self.engine.answer()
        return out

    def unchanged(self, play):
        """`play()` (a case: its plays and dry seconds) as it goes without the re-pick: one
        open, and VLC as dry (its start's own, 4bb2d7c's) as there."""
        with mock.patch.object(live_session, "REPICK", False):
            without = play()
        self.assertEqual(without[0], 1)
        self.assertEqual(play(), without)

    @staticmethod
    def segmented(late, split=1):
        """10 s bursts every 10 s as five 2 s segments within 0.2 s, the last `split` of them
        `late` s after the rest: a tick can hold the step and the next one a 2.0 s segment
        (the 1-stream raw-TS provider's recordings: [6.08, 2.0] and [2.08, 4.0, 2.0])."""
        sizes, gaps = [], []
        for _ in range(100):
            first = 5 - split
            parts = [0.05] * (first - 1) + [late] + [0.05] * (split - 1)
            sizes += [2.0] * 5
            gaps += parts + [10.0 - sum(parts)]
        return iter(sizes), iter(gaps)

    @staticmethod
    def spread(rate, seed, every=0.1):
        """10 s bursts every 10 s arriving at `rate` x real time in jittered pieces."""
        rng = random.Random(seed)
        sizes, gaps = [], []
        for _ in range(100):
            count = max(1, math.ceil(10.0 / rate / every - 1e-9))
            parts = [rng.uniform(0.5, 1.5) for _ in range(count)]
            sizes += [10.0 * part / sum(parts) for part in parts]
            gaps += [every] * (count - 1) + [10.0 - (count - 1) * every]
        return iter(sizes), iter(gaps)

    def test_a_bursts_late_tail_at_every_phase_picks_nothing_again(self):
        for latency, phases in (
            (AUTO, range(25)),
            ("normal", range(25)),
            ("low", range(0, 25, 4)),
            ("stable", range(0, 25, 4)),
        ):
            for k in phases:
                for late, split in ((0.25, 1), (0.15, 1), (0.25, 2)):
                    first = 0.5 + 0.17 * k
                    with self.subTest(latency=latency, first=first, late=late, split=split):

                        def play(first=first, late=late, split=split):
                            feed = self.tune(latency, burst=10.0, edge=2.0, first=first)
                            feed.sizes, feed.gaps = self.segmented(late, split)
                            feed.ready(backlog_after_start=False)
                            return self.outcome(feed)

                        self.unchanged(play)

    def test_spread_bursts_pick_nothing_again(self):
        for latency in (AUTO, "normal"):
            for rate in (7.0, 8.0, 10.0):
                for seed in range(3):
                    for first in (0.5, 1.0, 2.0):
                        with self.subTest(latency=latency, rate=rate, seed=seed, first=first):

                            def play(rate=rate, seed=seed, first=first):
                                feed = self.tune(latency, burst=10.0, edge=12.0, first=first)
                                feed.sizes, feed.gaps = self.spread(rate, seed)
                                feed.ready(backlog_after_start=False)
                                return self.outcome(feed)

                            self.unchanged(play)

    def test_spread_bursts_behind_a_dump_pick_nothing_again(self):
        for latency in (AUTO, "normal"):
            for rate in (7.0, 10.0):
                for seed in range(3):
                    with self.subTest(latency=latency, rate=rate, seed=seed):

                        def play(rate=rate, seed=seed):
                            feed = self.tune(latency, burst=10.0, edge=2.0, first=1.7)
                            feed.sizes, feed.gaps = self.spread(rate, seed)
                            feed.ready(backlog_after_start=False)
                            self.opened(feed)
                            feed.run(0.7)
                            feed.edge += 20.0
                            feed.push()
                            return self.outcome(feed, 50.0)

                        self.unchanged(play)

    def test_a_vlc_slow_to_play_its_first_open_gets_no_late_re_pick(self):
        for latency in (AUTO, "normal"):
            for burst in (4.0, 6.0, 10.0):
                for slow in (5.0, 8.0, 15.0):
                    for first in (1.0, 2.0, 3.0):
                        with self.subTest(latency=latency, burst=burst, slow=slow, first=first):

                            def play(burst=burst, slow=slow, first=first):
                                feed = self.tune(
                                    latency, burst=burst, edge=burst + 2.0, first=first
                                )
                                feed.ready(backlog_after_start=False)
                                self.opened(feed)
                                feed.frozen_until = self.clock.now + slow  # VLC's clock still
                                return self.outcome(feed, 50.0)

                            self.unchanged(play)

    def test_a_dump_then_a_flow_is_still_picked_again_once_early(self):
        # The re-pick's own case: 2 s at the open, a 20 s dump 0.7 s later, then 1x (0.25 s
        # pieces, or 2 s every 2 s as the provider's random channel sends it).
        for latency in (AUTO, "normal", "low"):
            for chunk in (0.0, 2.0):
                with self.subTest(latency=latency, chunk=chunk):
                    feed = self.tune(latency, burst=chunk, edge=2.0)
                    feed.ready(backlog_after_start=False)
                    self.opened(feed)
                    opened = self.clock.now
                    feed.run(0.7)
                    feed.edge += 20.0
                    feed.push()
                    for _ in range(80):
                        feed.run(0.25)
                        if len(self.playback.plays) > 1:
                            break
                    self.assertLess(self.clock.now - opened, 10.0)  # early
                    lag = self.timelines[-1]["lag"]
                    self.assertEqual(self.outcome(feed), (2, 0.0))
                    self.assertLess(lag, 22.0)

    def test_a_reopen_in_software_and_the_re_pick_are_never_two_opens_in_a_row(self):
        # Hardware decoding froze the picture right after the dump: the stream opened again
        # in software where it played; the re-pick still comes, but not within the reopen
        # hold of that open (never two opens in a row).
        feed = self.tune("normal", burst=0.0, edge=2.0)
        feed.ready(backlog_after_start=False)
        self.opened(feed)
        opens = [self.clock.now]
        feed.run(0.7)
        feed.edge += 20.0
        feed.push()
        feed.run(2.3)
        self.playback.pictureFroze.emit()
        self.assertEqual(len(self.playback.plays), 2)
        opens.append(self.clock.now)
        for _ in range(80):
            feed.run(0.25)
            if len(self.playback.plays) > len(opens):
                opens.append(self.clock.now)
        self.assertEqual(len(opens), 3)  # the re-pick came
        self.assertTrue(all(self.playback.software[1:]))
        self.assertGreaterEqual(opens[2] - opens[1], live_session._REOPEN_HOLD_S)

    def test_an_hls_start_is_never_picked_again(self):
        # Round 3 of re-review 2: a public HLS channel (6 s segments 7 s apart, every sixth
        # batch 12 s) whose 12 s batch landed 2 s after the open and the next 3.24 s later
        # read as a dump, then a flow: picked again 7 s behind, VLC ran dry twice. HLS never.
        for latency in ("low", "normal", "stable", AUTO):
            with self.subTest(latency=latency):

                def play():
                    feed = self.tune(latency, burst=7.0, edge=20.0, first=2.0, hls=True)
                    sizes = [12.0, 6.0] + [12.0 if k % 6 == 5 else 6.0 for k in range(200)]
                    feed.sizes = iter(sizes)
                    feed.gaps = iter([3.24] + [7.0] * 201)
                    feed.ready(backlog_after_start=False)
                    return self.outcome(feed)

                self.unchanged(play)

    def test_a_flow_that_stands_still_three_seconds_is_no_flow(self):
        # Raw TS: a dump, then batches 2.95 s apart (about real time over the window): the
        # edge stands still about 3 s between them, which ticks a quarter second apart read
        # as 3 s at some phases and 2.75 s at others: either way no flow (tick granularity
        # works against qualifying).
        for phase in (0.0, 0.05, 0.1, 0.15, 0.2):
            with self.subTest(phase=phase):

                def play(phase=phase):
                    feed = self.tune("normal", burst=0.0, edge=2.0)
                    feed.ready(backlog_after_start=False)
                    self.opened(feed)
                    feed.run(0.7)
                    feed.edge += 20.0
                    feed.push()
                    feed.sizes = itertools.repeat(2.95)
                    feed.gaps = itertools.repeat(2.95)
                    feed.nominal = feed.next_arrival = self.clock.now + 2.95 + phase
                    return self.outcome(feed, 30.0)

                self.unchanged(play)

    def test_a_reopen_aims_at_least_at_the_measured_burst(self):
        # Re-review 2's M-q: Auto, bursty raw TS (a dump, then 10 s bursts), VLC's clock
        # still 25 s after the open: stay-live's reopen at live aimed 4.7 s behind though
        # the helper had measured a 10 s burst, and VLC ran dry 3.7-4.7 s. The timeline's
        # target (Go live's, a reopen's, a jump's) is never under the burst floor.
        for first in (1.0, 2.0, 3.0):
            with self.subTest(first=first):
                feed = self.tune(AUTO, burst=10.0, edge=2.0, first=first + 0.7)
                feed.ready(backlog_after_start=False)
                self.opened(feed)
                feed.frozen_until = self.clock.now + 25.0
                feed.run(0.7)
                feed.edge += 20.0
                feed.push()
                lowest = []
                for _ in range(240):
                    feed.run(0.25)
                    snapshot = self.live._snapshot()
                    if snapshot.get("burst_s", 0.0) > 0:
                        lowest.append(self.live._target(snapshot) - snapshot["burst_s"])
                self.assertGreaterEqual(min(lowest), 0.0)
                self.assertEqual(feed.starved_s, 0.0)
                self.live.stop()
                self.engine.answer()

    def test_the_re_pick_can_be_switched_off(self):
        self.enterContext(mock.patch.object(live_session, "REPICK", False))
        feed = self.tune("normal", burst=0.0, edge=2.0)
        feed.ready(backlog_after_start=False)
        self.opened(feed)
        feed.run(0.7)
        feed.edge += 20.0
        feed.push()
        self.assertEqual(self.outcome(feed, 30.0)[0], 1)  # (stay-live's jump: after a minute)


class LearnedTargetTests(LiveSessionCase):
    """2.1 stage 4b: under Auto the channel's learned target (`LearnedTarget`, stage 4a) in
    place of 2.0.2's `AutoTarget`, wired by the integration contract of 4a's final review;
    Low, Normal and Stable stay as they were."""

    def setUp(self):
        self.tuning = LiveTuning(None)
        super().setUp()

    def make(self, **kwargs):
        kwargs.setdefault("tuning", self.tuning)
        return super().make(**kwargs)

    def spy(self, cls, name):
        real = getattr(cls, name)
        return self.enterContext(mock.patch.object(cls, name, autospec=True, side_effect=real))

    def tune(self, *, burst=0.0, edge=6.0, latency=AUTO, channel="ch", **kwargs):
        """Tune `channel` (a zap: the channel before closes first) and play it from the start
        Go live would pick."""
        self.start(latency=latency, channel=channel)
        self.engine.answer()
        feed = Feed(self, self.engine.last_sid, burst=burst, edge=edge, **kwargs)
        return feed.ready(backlog_after_start=False)

    def late(self, feed, seconds):
        """The source sends nothing for `seconds`, then all it owes at once (late, not lost)."""
        feed.stalled = True
        feed.run(seconds)
        feed.stalled = False
        feed.edge += seconds
        feed.push()

    def played(self, feed, plays):
        """Run `feed` until the tune opened (more than `plays` plays), at most 3 s."""
        for _ in range(12):
            if len(self.playback.plays) > plays:
                return
            feed.run(0.25)
        self.fail("the tune never opened")

    def starve(self, feed, times=3):
        """VLC runs dry and waits (its clock still while it says Playing): the source sends
        nothing for 1.6 s `times` times, a quarter second apart (never a stall)."""
        for _ in range(times):
            feed.stalled = True
            feed.run(1.6)
            feed.stalled = False
            feed.run(0.25)

    # ---- contract 1, 9: one per tune, from what the channel taught; kept for the next ----
    def test_one_learned_target_per_tune_from_what_the_channel_taught(self):
        self.tuning.remember("ch", Tuning(learned=Learned(need=6.0)))
        self.tune(edge=10.0).run(0.25)
        first = self.live._learned
        self.assertIsInstance(first, LearnedTarget)
        self.assertEqual((first.floor, first.arrivals.remembered), (TARGETS["normal"], 6.0))
        self.assertEqual(first.segment_floor, live_session.SEGMENT_FLOOR)
        self.assertEqual(self.timelines[-1]["target"], 6.0)  # its next tune starts there
        self.tune(edge=10.0)
        self.assertIsNot(self.live._learned, first)  # one per tune
        for latency in ("low", "normal", "stable"):
            with self.subTest(latency=latency):
                self.tune(latency=latency, edge=10.0).run(2.0)
                self.assertIsNone(self.live._learned)
                self.assertEqual(self.timelines[-1]["target"], TARGETS[latency])

    def test_the_burst_floor_switch_is_read_once_per_run(self):
        self.enterContext(mock.patch.object(live_session, "SEGMENT_FLOOR", False))
        self.tune()
        self.assertFalse(self.live._learned.segment_floor)

    def test_a_2_0_2_extra_is_a_prior_and_is_kept_as_what_was_learned(self):
        self.tuning.remember("ch", Tuning(extra=3.0))
        self.tune(edge=8.0).run(0.25)
        self.assertEqual(self.timelines[-1]["target"], TARGETS["normal"] + 3.0)
        self.live.stop()
        kept = self.tuning.get("ch")
        self.assertIsNone(kept.extra)  # read as the prior: never read so again
        self.assertIsNotNone(kept.learned)
        self.assertEqual(LearnedTarget(kept.learned).target(), TARGETS["normal"] + 3.0)

    def test_a_tune_that_learned_nothing_leaves_nothing(self):
        self.tuning.remember("ch", Tuning(extra=0.4))  # under half a second: no margin
        feed = self.tune()
        feed.run(30.0)
        self.live.stop()
        self.assertEqual(self.tuning.get("ch"), Tuning())

    # ---- contract 2: the edge every tick; arrivals start afresh only with a new stream ----
    def test_the_edge_every_tick_and_a_vlc_reopen_restarts_no_arrivals(self):
        add = self.spy(ArrivalModel, "add")
        reset = self.spy(ArrivalModel, "reset")
        feed = self.tune(edge=40.0)
        feed.run(0.25)
        calls, resets = add.call_count, reset.call_count  # (the first edge: one of its own)
        feed.run(10.0)
        self.assertEqual(add.call_count - calls, 40)  # every tick
        self.live.toggle_pause()  # paused: the source goes on arriving
        feed.run(5.0)
        self.live.toggle_pause()
        self.live.seek_relative(-20.0)  # VLC reopens
        feed.sync()
        feed.run(5.0)
        self.live.go_live()
        feed.sync()
        feed.run(5.0)
        self.assertEqual(add.call_count - calls, 40 + 20 + 20 + 20)
        self.assertEqual(reset.call_count, resets)
        self.engine.sessionFailed.emit(self.engine.last_sid, "internal", "The helper died.")
        self.assertEqual(reset.call_count, resets + 1)  # the helper's stream starts afresh

    # ---- contract 3: the steering target from the filter's burst ----
    def test_a_burst_the_helper_reports_for_a_moment_steers_nothing(self):
        feed = self.tune(edge=6.0)
        feed.run(60.0)
        target = self.timelines[-1]["target"]
        steered = []
        # 15 s of a measured "burst" (a backlog's), then none again: longer than a rise must
        # hold to be steered by, shorter than the filter takes a larger burst in.
        feed.burst = 10.0  # what the helper reports with each state (it arrives as before)
        for _ in range(60):
            feed.run(0.25)
            steered.append(self.live._stay.target)
        # Go live, the start and the timeline count with the helper's burst at once (stage
        # 1), so stay-live never speeds up towards a target it may be about to raise.
        self.assertGreaterEqual(self.timelines[-1]["target"], 10.0)
        feed.burst = 0.0
        for _ in range(120):
            feed.run(0.25)
            steered.append(self.live._stay.target)
        self.assertEqual(set(steered), {target})
        self.assertEqual(self.timelines[-1]["target"], target)

    # ---- contract 4, 7: a stall once when it begins; absorbed when it ends with room ----
    def test_a_stall_is_told_once_when_it_begins_with_the_bounds_burst(self):
        stalled = self.spy(LearnedTarget, "stalled")
        absorbed = self.spy(LearnedTarget, "absorbed")
        self.tuning.remember("ch", Tuning(extra=3.0))  # a 5.5 s target: room for a stall
        feed = self.tune(edge=12.0)
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        feed.stalled = True
        feed.run(1.75)
        self.assertFalse(stalled.called)  # under the bound (a steady source: 2 s)
        feed.run(1.0)
        self.assertEqual(stalled.call_count, 1)
        self.assertEqual(stalled.call_args.args[1:], (0.0,))  # the 2 s bound's burst
        feed.run(1.0)  # still the same stall
        self.late(feed, 0.0)
        feed.run(1.0)
        self.assertEqual(stalled.call_count, 1)
        # It ended with VLC still playing: the least headroom it had meanwhile, the lag less
        # the 3.75 s it lasted.
        self.assertEqual(absorbed.call_count, 1)
        self.assertAlmostEqual(absorbed.call_args.args[1], lag - 3.75, delta=0.5)

    def test_a_stall_is_not_told_while_paused_or_before_the_cushion(self):
        stalled = self.spy(LearnedTarget, "stalled")
        feed = self.tune(edge=8.0)
        feed.run(30.0)
        self.live.toggle_pause()
        feed.stalled = True
        feed.run(5.0)
        feed.stalled = False
        feed.run(1.0)
        self.assertFalse(stalled.called)
        self.live = self.make()
        sid = self.start(latency=AUTO, channel="ch")  # from the oldest, short of the target
        feed = Feed(self, sid, burst=0.0, edge=1.0, starved=PlaybackState.PLAYING).ready()
        feed.stalled = True
        feed.run(3.0)
        self.assertFalse(stalled.called)

    def test_a_stall_the_2_s_bound_found_is_told_without_a_burst(self):
        # The helper's burst went (a raw TS meter's estimate) while the filter still holds it.
        stalled = self.spy(LearnedTarget, "stalled")
        feed = self.tune(burst=6.0, edge=20.0)
        feed.run(60.0)
        feed.burst = 0.0  # the helper reports none now (the filter keeps 6 for 30 s)
        feed.push()
        feed.run(0.25)
        self.assertEqual(self.live._filter.burst, 6.0)
        feed.stalled = True
        feed.run(8.0)
        self.assertEqual(stalled.call_count, 1)
        self.assertEqual(stalled.call_args.args[1:], (0.0,))

    # ---- contract 5: VLC running dry, once per dry run ----
    def test_running_dry_is_told_once_per_dry_run(self):
        starved = self.spy(LearnedTarget, "starved")
        feed = self.tune(edge=8.0, starved=PlaybackState.PLAYING)
        feed.run(30.0)
        self.starve(feed)  # three dry spells a quarter second apart: VLC refilling
        self.assertEqual(starved.call_count, 1)
        feed = self.tune(edge=8.0, channel="other", starved=PlaybackState.PLAYING)
        feed.run(30.0)
        calls = starved.call_count
        for _ in range(2):  # dry runs with the cushion built back between them
            self.late(feed, self.timelines[-1]["lag"] + 1.0)  # a second dry, then all of it
            feed.run(240.0)  # the owed slow-down builds the room back (a cushion again)
        self.assertEqual(starved.call_count, calls + 2)

    def test_a_refills_flicker_is_one_dry_run(self):
        # At the cap (a 2.0.2 extra of 7.5 s: 10 s), so running dry raises nothing and every
        # dry spell would count were they not one.
        starved = self.spy(LearnedTarget, "starved")
        self.tuning.remember("ch", Tuning(extra=7.5))
        feed = self.tune(edge=14.0)
        feed.run(30.0)
        lag = self.timelines[-1]["lag"]
        feed.stalled = True
        feed.run(lag + 0.25)  # VLC runs dry ...
        for _ in range(3):  # ... and refills in pieces: dry, playing, dry
            feed.edge += 0.25
            feed.push()
            feed.run(0.25)
            feed.run(0.5)
        feed.stalled = False
        feed.edge += lag
        feed.push()
        feed.run(5.0)
        self.assertEqual(starved.call_count, 1)

    # ---- contract 6: near-misses ----
    def test_a_near_miss_is_the_headroom_before_a_batch_less_vlcs_caching(self):
        near = self.spy(LearnedTarget, "near_miss")
        feed = self.tune(burst=6.0, edge=20.0)
        feed.run(60.0)
        self.assertGreater(near.call_count, 5)  # one per batch
        before = feed.before[-1]  # the headroom just before the last batch
        # On the tick before it (a quarter second earlier, when VLC had a little more).
        self.assertAlmostEqual(near.call_args.args[1], before - 0.3, delta=0.3)

    def test_no_near_miss_while_the_start_fills_its_cushion(self):
        near = self.spy(LearnedTarget, "near_miss")
        self.live = self.make(ready_timeout_ms=50)
        sid = self.start(latency=AUTO, channel="ch")
        feed = Feed(self, sid, burst=6.0, edge=4.0, first=6.0).ready(backlog_after_start=False)
        self.wait_until(lambda: self.playback.plays)
        feed.run(20.0)  # the lag short of the target the whole time
        self.assertLess(self.timelines[-1]["lag"], self.timelines[-1]["target"])
        self.assertFalse(near.called)

    # ---- contract 8: playing at the live edge only ----
    def test_only_playing_at_the_live_edge_counts(self):
        played = self.spy(LearnedTarget, "played")
        feed = self.tune(edge=40.0)
        feed.run(20.0)
        self.assertAlmostEqual(sum(c.args[1] for c in played.call_args_list), 20.0, delta=0.5)
        for step in ("paused", "time-shifted"):
            calls = played.call_count
            if step == "paused":
                self.live.toggle_pause()
                feed.run(10.0)
                self.live.toggle_pause()
            else:
                self.live.seek_relative(-20.0)
                feed.sync()
                feed.run(10.0)
            self.assertEqual(played.call_count, calls, step)
            self.live.go_live()
            feed.sync()
            feed.run(1.0)

    # ---- contract 10: "· adjusted" means trouble raised it ----
    def test_adjusted_says_trouble_raised_the_target_not_a_burst(self):
        feed = self.tune(burst=6.0, edge=20.0, hls=True, starved=PlaybackState.PLAYING)
        feed.run(60.0)
        self.assertEqual(self.timelines[-1]["target"], 6.0)  # at its burst: no adjustment
        self.assertEqual(
            (self.timelines[-1]["auto"], self.timelines[-1]["adjusted"]), (True, False)
        )
        feed.stalled = True
        feed.run(10.0)  # a stall of a segmented source, VLC dry before its end: trouble
        feed.stalled = False
        feed.run(1.0)
        self.assertTrue(self.timelines[-1]["adjusted"])

    # ---- review round 1 ----
    def test_a_burst_the_helper_measures_late_reaches_the_target_at_once(self):
        # Raw TS a meter measures (10 s bursts every 10 s), its first clear burst well after
        # the start, as on the 1-stream raw-TS provider (C1): the timeline, Go live and the
        # start count with it at once, and stay-live never speeds up towards a target the
        # burst is about to raise (2.0.1's bursty handling, stage 1's slack).
        sid = self.start(latency=AUTO, channel="ch")
        feed = Feed(
            self, sid, burst=10.0, edge=12.0, metered=True, starved=PlaybackState.PLAYING
        ).ready()
        measured = None
        for _ in range(240 * 4):
            feed.run(0.25)
            if measured is None and feed.meter.burst_s > 0:
                measured = feed.meter.burst_s
                self.assertGreaterEqual(self.timelines[-1]["target"], measured)
        self.assertIsNotNone(measured)
        self.assertLessEqual(max(self.playback.rates or [1.0]), 1.0)
        self.assertEqual(feed.starved_s, 0.0)
        self.assertGreaterEqual(self.live._stay.target, measured)  # steered by it since

    def test_a_one_off_first_burst_comes_back_down_within_a_minute(self):
        # A steady source whose helper measured its backlog as a "burst" in its first states,
        # dropped by its next estimate (I1): down as the lag filter drops it, not at Auto's
        # quiet pace (it held ~10.5 s for over an hour).
        sid = self.start(latency=AUTO, channel="ch")
        feed = Feed(self, sid, burst=0.0, edge=12.0, starved=PlaybackState.PLAYING)
        feed.burst = 10.0
        feed.push()
        feed.ready(backlog_after_start=False)
        feed.run(8.0)
        feed.burst = 0.0
        feed.run(60.0)
        self.assertLessEqual(self.timelines[-1]["target"], 3.5)

    def test_the_batch_that_ends_a_dry_run_is_no_near_miss(self):
        # (I2) Its headroom on the tick before was the dry run's: one starvation, told once.
        near = self.spy(LearnedTarget, "near_miss")
        starved = self.spy(LearnedTarget, "starved")
        feed = self.tune(edge=8.0, starved=PlaybackState.PLAYING)
        feed.run(60.0)
        calls = near.call_count
        self.late(feed, self.timelines[-1]["lag"] + 3.0)  # VLC dry 3 s, then all of it
        feed.run(0.5)
        self.assertEqual(starved.call_count, 1)
        self.assertEqual([c for c in near.call_args_list[calls:] if c.args[1] < 0.5], [])
        self.assertAlmostEqual(self.live._learned.prior.margin(), 1.58, delta=0.05)

    def test_running_dry_inside_a_counted_stall_is_told(self):
        # (I3) A segmented source stalls beyond its bound, then VLC runs dry: both told.
        stalled = self.spy(LearnedTarget, "stalled")
        starved = self.spy(LearnedTarget, "starved")
        self.tuning.remember("ch", Tuning(learned=Learned(need=10.0)))
        feed = self.tune(burst=6.0, edge=30.0, hls=True, starved=PlaybackState.PLAYING)
        feed.run(60.0)
        feed.stalled = True
        feed.run(18.0)  # the bound at 8 s, VLC dry from about 10 s
        feed.stalled = False
        feed.run(2.0)
        self.assertEqual((stalled.call_count, starved.call_count), (1, 1))

    def test_a_stall_vlc_got_through_is_no_trouble(self):
        # A source late by a stall the target covered (VLC never ran dry): nothing holds the
        # target up for it (ruling R37 had counted every segmented source's stall at once,
        # 2.0.2 every stall: a raw TS stall 23 s behind took the target from 2.5 to 5.5;
        # part A's backlog idea: it is absorbed, a near-miss at most).
        for burst, late, channel in ((6.0, 9.0, "hls"), (0.0, 5.0, "raw")):
            with self.subTest(burst=burst):
                self.tuning.remember(channel, Tuning(learned=Learned(need=10.0)))
                feed = self.tune(
                    burst=burst,
                    edge=30.0,
                    hls=burst > 0,
                    starved=PlaybackState.PLAYING,
                    channel=channel,
                )
                feed.run(60.0)
                target = self.timelines[-1]["target"]
                self.late(feed, late)  # the bound passed; VLC had about 10 s
                feed.run(1.0)
                self.assertEqual(feed.starved_s, 0.0)
                self.assertEqual(self.live._learned.prior.margin(), 0.0)
                self.assertFalse(self.timelines[-1]["adjusted"])
                self.assertLessEqual(self.timelines[-1]["target"], target)

    def test_one_late_reload_is_a_near_miss_at_most(self):
        # 4a's final review: a reload missed on a 6 s HLS channel at its burst (9 s silent,
        # beyond the 8 s stall bound, VLC never dry) raised the target 1.3 s for about an
        # hour. A stall the target held out is no starvation: a near-miss at most.
        starved = self.spy(LearnedTarget, "starved")
        feed = self.tune(burst=6.0, edge=20.0, hls=True, starved=PlaybackState.PLAYING)
        feed.run(60.0)
        target = self.timelines[-1]["target"]
        arrivals = len(feed.before)
        while len(feed.before) == arrivals:  # right after a batch: the most room
            feed.run(0.25)
        self.late(feed, 8.5)
        feed.run(1.0)
        self.assertEqual(feed.starved_s, 0.0)
        self.assertFalse(starved.called)
        self.assertLess(self.live._learned.prior.margin(), 1.0)  # a starvation's is 1.6 s
        self.assertLess(self.timelines[-1]["target"], target + 1.0)

    def test_a_backlog_right_after_the_open_picks_the_start_again(self):
        # The 1-stream raw-TS provider's random channel: the tune opened with 2 s buffered,
        # then 20 s came at once 0.7 s later and the source flowed on at 1x; stay-live nudged
        # from 17 s and jumped only once the tune's 60 s had passed, a minute some 23 s
        # behind. Now the start is picked again, once, after 2.5 s of flow.
        for latency in (AUTO, "normal"):
            with self.subTest(latency=latency):
                plays = len(self.playback.plays)
                feed = self.tune(edge=2.0, latency=latency, channel=latency)
                self.played(feed, plays)
                feed.run(0.75)
                feed.edge += 20.0
                feed.push()
                feed.run(10.0)
                self.assertEqual(len(self.playback.plays), plays + 2)  # the open, once more
                target = self.timelines[-1]["target"]
                self.assertLess(self.timelines[-1]["lag"], target + 2.5)
                rates = len(self.playback.rates)
                feed.run(60.0)
                self.assertEqual(len(self.playback.plays), plays + 2)
                # A gentle nudge at most (the pick's keyframe grain): no catch-up.
                self.assertTrue(all(r <= 1.03 for r in self.playback.rates[rates:]))
                self.assertEqual(feed.starved_s, 0.0)

    def test_a_bursty_sources_first_burst_right_after_the_open_picks_nothing_again(self):
        # Re-review C1: raw TS the meter has not measured yet (it can at the second burst),
        # its first regular burst 1-4 s after the open, looked like a late backlog: the start
        # was picked again 2.5 s behind and VLC ran dry 4-7 s until the next burst. Under any
        # latency it plays on as before: no second open, no longer dry than before the re-pick.
        for latency in ("normal", AUTO):
            for burst, edge, first, most in (
                (10.0, 12.0, 1.0, 0.0),
                (10.0, 12.0, 2.0, 0.0),
                (10.0, 12.0, 3.0, 0.95),
                (10.0, 12.0, 4.0, 1.95),
                (6.0, 8.0, 1.0, 0.0),
            ):
                with self.subTest(latency=latency, burst=burst, first=first):
                    plays = len(self.playback.plays)
                    self.start(latency=latency, channel="b%s%g" % (latency, first))
                    self.engine.answer()
                    feed = Feed(
                        self,
                        self.engine.last_sid,
                        burst=burst,
                        edge=edge,
                        first=first,
                        metered=True,
                        starved=PlaybackState.PLAYING,
                    ).ready(backlog_after_start=False)
                    feed.run(60.0)
                    self.assertEqual(len(self.playback.plays), plays + 1)
                    self.assertLessEqual(feed.starved_s, most + 0.05)

    def test_a_dump_then_a_bursty_source_is_left_as_it_landed(self):
        # The re-pick's own shape, but bursty: 2 s at the open, 20 s 0.7 s later, then 10 s
        # every 10 s. Nothing flows after the step: no re-pick (it ran VLC dry 6.2 s).
        for latency in ("normal", AUTO):
            with self.subTest(latency=latency):
                plays = len(self.playback.plays)
                self.start(latency=latency, channel="d" + latency)
                self.engine.answer()
                feed = Feed(
                    self,
                    self.engine.last_sid,
                    burst=10.0,
                    edge=2.0,
                    metered=True,
                    starved=PlaybackState.PLAYING,
                ).ready(backlog_after_start=False)
                self.played(feed, plays)
                feed.run(0.7)
                feed.edge += 20.0
                feed.push()
                feed.run(60.0)
                self.assertEqual(len(self.playback.plays), plays + 1)
                self.assertEqual(feed.starved_s, 0.0)

    def test_a_bursty_channels_second_tune_does_not_run_dry_either(self):
        # Auto, the same channel again after two minutes (re-review C1: 0.95-4.95 s dry; M-c:
        # a first tune too short to measure the need kept nothing of the burst, and the next
        # started at the floor and a starvation's margin, 1.95 s dry).
        for first in (1.0, 2.0, 3.0, 6.0):
            with self.subTest(first=first):
                for tune in (1, 2):
                    self.start(latency=AUTO, channel="t%g" % first)
                    self.engine.answer()
                    feed = Feed(
                        self,
                        self.engine.last_sid,
                        burst=10.0,
                        edge=12.0,
                        first=first,
                        metered=True,
                        starved=PlaybackState.PLAYING,
                    ).ready(backlog_after_start=False)
                    feed.run(120.0 if tune == 1 else 60.0)
                self.assertEqual(feed.starved_s, 0.0)

    def test_a_backlog_a_while_after_the_open_is_part_of_the_landing(self):
        # Later than the re-pick's few seconds: as stage 3 had it, steered gently.
        plays = len(self.playback.plays)
        feed = self.tune(edge=2.0)
        self.played(feed, plays)
        feed.run(6.0)
        feed.edge += 20.0
        feed.push()
        feed.run(10.0)
        self.assertEqual(len(self.playback.plays), plays + 1)

    def test_an_hls_start_fetches_for_the_learned_target_once(self):
        # (M1) The learned target already holds the source's segments: passed as the
        # target, nothing extra on top (the helper adds a segment and a half itself).
        self.tuning.remember("ch", Tuning(learned=Learned(need=13.0)))
        self.start(latency=AUTO, channel="ch")
        kwargs = self.engine.opened[-1][2]
        self.assertEqual((kwargs["target"], kwargs["extra"]), (13.0, 0.0))
        self.start(latency="normal", channel="other")
        self.engine.answer()
        kwargs = self.engine.opened[-1][2]
        self.assertEqual((kwargs["target"], kwargs["extra"]), (TARGETS["normal"], 0.0))

    def test_a_learned_rise_left_in_the_band_still_counts_the_next_dry_run(self):
        # (M2) A rise within the band is drift: stay-live builds nothing for it, so the lag
        # never reaches it; VLC running dry then still ran out the cushion.
        starved = self.spy(LearnedTarget, "starved")
        feed = self.tune(edge=8.0, starved=PlaybackState.PLAYING)
        feed.run(60.0)
        self.live._learned.prior.at_least(0.8)  # the learned target 0.8 s higher
        feed.run(15.0)
        self.assertAlmostEqual(self.live._stay.target, TARGETS["normal"] + 0.8, delta=0.05)
        self.assertEqual(self.playback.rates, [])  # nothing built
        self.late(feed, self.timelines[-1]["lag"] + 1.0)
        feed.run(0.5)
        self.assertEqual(starved.call_count, 1)

    def test_the_burst_floor_switch_reads_the_environment(self):
        # (M5) QITV_SEGMENT_FLOOR=0 (or off, false, no) turns it off; anything else is on.
        for value, on in (
            ("", True),
            ("1", True),
            ("on", True),
            ("yes", True),
            ("0", False),
            ("off", False),
            ("False", False),
            (" no ", False),
        ):
            with self.subTest(value=value):
                self.assertEqual(live_session.segment_floor_from(value), on)

    def test_a_move_of_the_sources_burst_is_said_as_such(self):
        # (M4) Not "arrivals" or "quiet": the lag filter took a larger burst in.
        sid = self.start(latency=AUTO, channel="ch")
        feed = Feed(
            self, sid, burst=10.0, edge=12.0, metered=True, starved=PlaybackState.PLAYING
        ).ready()
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            feed.run(60.0)
        self.assertTrue(any("burst" in line for line in logs.output), logs.output)

    # ---- the learned target in play ----
    def test_a_steady_source_learns_its_need_and_the_next_tune_starts_there(self):
        feed = self.tune(burst=2.0, edge=8.0)  # 2 s of media every 2 s
        feed.run(120.0)
        target = self.timelines[-1]["target"]
        self.assertGreaterEqual(target, 2.5)
        self.assertLessEqual(target, 3.5)
        self.assertEqual(changes(self.playback.rates), 0)
        self.tune(burst=2.0, edge=8.0)
        self.assertAlmostEqual(self.timelines[-1]["target"], target, delta=0.1)

    def test_an_uneven_batch_hls_source_holds_what_its_deepest_dip_needs(self):
        # 6 s every 7 s, every sixth 12 s (a public HLS channel, measured; its playlist says 7
        # s): VLC needs about 7 s of mean lag there (the edge falls 6 s below its mean before
        # each 12 s batch). With the 12 s batch at each place in the rhythm: never under its
        # burst, at most a margin above it; a start whose pick read the 12 s batch's dip short
        # may run dry once before that batch, as 2.0.2 did, and is then built at 0.95x; no
        # catch-up ever.
        for phase in range(6):
            with self.subTest(phase=phase):
                feed = self.tune(burst=7.0, edge=20.0, hls=True, channel="ch%d" % phase)
                feed.sizes = iter([12.0 if k % 6 == phase else 6.0 for k in range(1000)])
                rates = len(self.playback.rates)
                feed.run(60.0)
                dry = feed.starved_s
                feed.run(240.0)
                learned = self.live._learned.target(7.0)
                self.assertGreaterEqual(learned, 7.0)
                self.assertLessEqual(learned, 9.5)
                self.assertLessEqual(dry, 1.5)
                self.assertEqual(feed.starved_s, dry)  # only in the first minute
                self.assertTrue(all(r <= 1.0 for r in self.playback.rates[rates:]))

    def test_a_sparse_pcr_rhythm_learns_what_keeps_vlc_from_running_dry(self):
        # Two sparse-PCR channels, measured: 10 s batches 12, 12 and 6 s apart, their
        # playlists' target duration 12 s. A 12 s target kept 4.9 s of headroom before every
        # batch; the learned target had read the regular 12-12-6 rhythm as lateness (16 s).
        feed = self.tune(burst=12.0, edge=30.0, hls=True, pcr=2.0, starved=PlaybackState.PLAYING)
        feed.sizes = itertools.repeat(10.0)
        feed.gaps = itertools.cycle([12.0, 12.0, 6.0])
        feed.run(300.0)
        self.assertLessEqual(self.live._learned.need(12.0), 12.0)
        self.assertLessEqual(self.timelines[-1]["target"], 13.0)
        self.assertEqual(feed.starved_s, 0.0)
        self.assertLessEqual(changes(self.playback.rates), 2)
        self.assertGreater(min(feed.before[3:]), 2.0)  # headroom before every batch

    def test_a_dry_run_raises_the_target_at_once_and_the_slow_down_runs_once(self):
        feed = self.tune(edge=8.0, starved=PlaybackState.PLAYING)
        feed.run(30.0)
        before, rates = self.timelines[-1]["target"], len(self.playback.rates)
        with self.assertLogs("services.live.live_session", "INFO") as logs:
            self.late(feed, 3.0)  # VLC runs dry a second, then all of it arrives
            feed.run(0.25)
        self.assertGreaterEqual(self.timelines[-1]["target"], before + 1.0)
        self.assertTrue(any("trouble" in line for line in logs.output))
        self.assertFalse(any("://" in line for line in logs.output))
        feed.run(120.0)
        built = self.playback.rates[rates:]
        self.assertEqual((min(built), changes(built)), (0.95, 2))

    def test_quiet_brings_it_down_slowly(self):
        self.enterContext(mock.patch.object(learned_target, "HOLD_S", 60.0))
        self.enterContext(mock.patch.object(learned_target, "HALF_LIFE_S", 60.0))
        feed = self.tune(edge=8.0, starved=PlaybackState.PLAYING)
        feed.run(30.0)
        self.late(feed, 3.0)
        feed.run(0.25)
        raised = self.timelines[-1]["target"]
        feed.run(1200.0)
        targets = [t["target"] for t in self.timelines[-4800:]]
        self.assertLess(targets[-1], raised - 1.0)
        self.assertTrue(all(a >= b - 1e-9 for a, b in zip(targets, targets[1:])))  # never up
        self.assertNotIn(1.10, self.playback.rates)


if __name__ == "__main__":
    unittest.main()
