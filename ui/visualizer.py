"""The audio screen's Stage bars, moved by the sound of the local file playing (Plan 4).

The bars are read ahead from the file itself, as band levels (services.levels: 60 bands, a frame
every 30 ms), and shown at the player's position: `set_levels` about 30 times a second while
the Stage shows and the song plays (`stageMoving`), so a pause holds them at their frame and a
seek reads on from where it lands. A live channel has none (`set_levels(None)`); under reduced
motion the Stage shows one still shape instead, the song's average once some of it is read.

One decode. While the AI captions read the same file (CaptionsController.levels_from: their
film run, which plays its sound from FILM_AHEAD_S past the picture), their process sends its
levels (`levels`) and no helper reads what they read. The stretch before their levels begin
(after a song's start or a seek: their run's start, and a couple of seconds more while its
player finds its clock) is read by the helper, which stops there (`until`), waits until their
first levels say where theirs begin, and ends: the two never read the same sound. (A ruling's
"no helper while they read" taken as "none for what they read": without it the bars would rest
6 to 11 s at every song's start and seek with Words on.) Otherwise the helper process
(services.visualizer.helper) reads the file, from where the bars are, up to AHEAD_S past the
player; a seek outside what is read sends it there (in the same process). Captions turned on
or off mid-song hand over at the bars' position: what either read stays (`FrameStore`), and
the other reads on from where it ends. One helper at most; a new track, a stop or something
not local ends it.

The helper is started the first time the Stage shows for a song (a song never seen on the
Stage costs nothing), and reads on while the song plays. Nothing here waits: the helper's
thread starts, joins and kills its process.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
import logging
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.levels import BANDS, FRAME_MS
from services.visualizer.helper import LevelsHelper
from ui.opening_mark import OpeningMark

logger = logging.getLogger(__name__)

FEED_MS = 33  # the bars' frames (about 30 a second)
STEER_MS = 250  # where the sound is read is looked at this often while they move
ASK_MS = 500  # the helper is sent elsewhere at most this often (a drag on the scrub bar)
STILL_MS = 1000  # under reduced motion: the still shape is looked for this often
TELL_MS = 1000  # the helper hears where the player is this often (it reads AHEAD_S past it)
HOLD_MS = 600  # where nothing is read yet the bars hold their frame this long, then rest
SLACK_FRAMES = round(2000 / FRAME_MS)  # the helper reads this close behind them: it gets there
STILL_FRAMES = round(6000 / FRAME_MS)  # the still shape is the average of this much at least
MAX_LEAD_MS = 600  # the player's time is carried on at most this far past its last reading
BACK_MS = 1000  # a reading this much behind is a seek back (less: the clock waits for it)
RESTING = [0.0] * BANDS


class FrameStore:
    """Frames by index (a FRAME_MS each from the song's start), BANDS bytes each (0–255), kept as
    runs of frames in a row: any source's frame of the song is good wherever it falls."""

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._runs: list[bytearray] = []

    def clear(self) -> None:
        self._starts, self._runs = [], []

    def count(self) -> int:
        return sum(len(run) for run in self._runs) // BANDS

    def _end(self, i: int) -> int:
        return self._starts[i] + len(self._runs[i]) // BANDS

    def put(self, first: int, blob: bytes) -> None:
        """Frames from index `first` (newer over older where they meet)."""
        count = len(blob) // BANDS
        if count <= 0:
            return
        if first < 0:
            blob, count, first = blob[-first * BANDS :], count + first, 0
            if count <= 0:
                return
        blob = blob[: count * BANDS]
        last = first + count
        i = bisect.bisect_right(self._starts, first) - 1
        if (
            i >= 0
            and self._end(i) == first
            and (i + 1 == len(self._starts) or self._starts[i + 1] > last)
        ):
            self._runs[i].extend(blob)  # on from the run it follows (the usual chunk)
            return
        lo = i if i >= 0 and self._end(i) >= first else i + 1
        hi = lo
        while hi < len(self._starts) and self._starts[hi] <= last:
            hi += 1
        start = min([first] + self._starts[lo:hi])
        end = max([last] + [self._end(k) for k in range(lo, hi)])
        merged = bytearray((end - start) * BANDS)
        for k in range(lo, hi):
            at = (self._starts[k] - start) * BANDS
            merged[at : at + len(self._runs[k])] = self._runs[k]
        merged[(first - start) * BANDS : (last - start) * BANDS] = blob
        self._starts[lo:hi] = [start]
        self._runs[lo:hi] = [merged]

    def at(self, index: int) -> Optional[bytes]:
        i = bisect.bisect_right(self._starts, index) - 1
        if i < 0 or index >= self._end(i):
            return None
        at = (index - self._starts[i]) * BANDS
        return bytes(self._runs[i][at : at + BANDS])

    def gap_from(self, index: int) -> int:
        """The first frame from `index` on that isn't held."""
        i = bisect.bisect_right(self._starts, index) - 1
        if i >= 0 and index < self._end(i):
            return self._end(i)
        return index

    def average(self) -> list[float]:
        """Each band's average over the frames held (0–1)."""
        count = self.count()
        if not count:
            return list(RESTING)
        totals = [0] * BANDS
        for run in self._runs:
            for band in range(BANDS):
                totals[band] += sum(run[band::BANDS])
        return [total / (count * 255.0) for total in totals]


class PlayClock:
    """The player's time between libVLC's steps (it moves a quarter second at a time): carried
    on from each new reading, never back by a little, at most MAX_LEAD_MS past its reading."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._read: Optional[int] = None
        self._since = (0.0, 0.0)
        self._last: Optional[float] = None

    def at(self, read_ms: int) -> float:
        now = self._clock()
        last = self._last
        if read_ms <= 0 and last is not None and last > BACK_MS:
            return last  # a busy player's 0: no reading
        if read_ms != self._read:
            self._read, self._since = read_ms, (float(read_ms), now)
        base, since = self._since
        ms = min(base + (now - since) * 1000.0, read_ms + MAX_LEAD_MS)
        if last is not None and last - BACK_MS < ms < last:
            ms = last  # a reading a little behind what was shown: it waits for it
        self._last = ms
        return ms


class Visualizer(QObject):
    """Feeds the audio screen's Stage bars (`screen.set_levels`) for the local file playing."""

    _heard = Signal(int, object)  # the helper's serial, what it said (from its thread)

    def __init__(
        self,
        screen: Any,
        session: Callable[[], Any],
        captions: Any = None,
        *,
        helper: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._screen = screen
        self._session = session
        self._captions = captions
        self._make_helper = helper or LevelsHelper
        self._clock = clock
        self._path = ""  # the file playing on the audio screen ("": none local)
        self._location = ""  # ... as the player and the captions name it (its URL)
        self._frames = FrameStore()
        self._play = PlayClock(clock)
        self._helper: Any = None
        self._serial = 0
        self._helper_failed = False  # this song's helper failed: no other for it
        self._asked: Optional[tuple[int, Optional[int]]] = None  # (from, until) frames asked
        self._reach = 0  # the frame the helper has read to since it was asked
        self._end: Optional[int] = None  # the song's last frame + 1, once read to its end
        self._captions_first: Optional[int] = None  # the captions' run's first frame, once come
        self._asked_at = float("-inf")
        self._told_at = float("-inf")
        self._steered_at = float("-inf")
        self._shown: Optional[int] = None  # the frame the bars show
        self._frame: Optional[bytes] = None  # ... its levels
        self._missing_since: Optional[float] = None
        self._still = False  # the still shape is up (reduced motion)
        self._feed = QTimer(self)
        self._feed.setInterval(FEED_MS)
        self._feed.timeout.connect(self._tick)
        self._slow = QTimer(self)
        self._slow.setInterval(STILL_MS)
        self._slow.timeout.connect(self._still_tick)
        self._heard.connect(self._on_heard)
        screen.stageMoving.connect(self._update)
        screen.stageShown.connect(self._update)
        levels = getattr(captions, "levels", None)
        if levels is not None:
            levels.connect(self._on_captions_levels)
        reads = getattr(captions, "soundRead", None)
        if reads is not None:
            reads.connect(self._captions_moved)

    # ---- what plays (the controller) ---------------------------------------------------------

    def follow(self, path: str, location: str = "") -> None:
        """A local file plays on the audio screen (`path`; `location`, its URL as the player
        and the captions name it); "": nothing local (a radio channel, a stop): no bars."""
        if path and (path, location) == (self._path, self._location):
            return  # (its own sound read already)
        self._forget()
        self._path, self._location = path, location if path else ""
        self._want_levels(bool(path))
        self._screen.set_levels(RESTING if path else None)
        self._update()

    def rest(self) -> None:
        """A new track comes on the same screen: the bars rest until its own sound is read."""
        self._forget()
        self._path = self._location = ""
        self._screen.set_levels(RESTING)
        self._update()

    def shutdown(self) -> None:
        """QiTV closes: no helper left reading."""
        self._forget()
        self._path = self._location = ""
        self._want_levels(False)
        self._feed.stop()
        self._slow.stop()

    def helper(self) -> Any:
        """The helper reading the song (None: none)."""
        return self._helper

    def _forget(self) -> None:
        self._end_helper()
        self._frames.clear()
        self._helper_failed = False
        self._end = self._captions_first = None
        self._shown = self._missing_since = self._frame = None
        self._still = False
        self._steered_at = float("-inf")  # (a new song's first frame steers at once)
        self._play.reset()

    def _want_levels(self, on: bool) -> None:
        want = getattr(self._captions, "want_levels", None)
        if callable(want):
            want(on)

    # ---- when the bars move ------------------------------------------------------------------

    def _update(self, *_: Any) -> None:
        screen = self._screen
        following = bool(self._path)
        moving = following and bool(screen.stage_moving())
        if moving and not self._feed.isActive():
            self._play.reset()
            self._shown = self._frame = None
            self._feed.start()
            self._tick()
        elif not moving:
            self._feed.stop()  # (a pause: the bars stay at their frame)
        still = following and not moving and bool(screen.takes_words())
        if still and OpeningMark.reduced_motion():
            if not self._slow.isActive():
                self._slow.start()
                self._still_tick()
        else:
            self._slow.stop()

    def _position_ms(self) -> float:
        session = self._session()
        try:
            read = int(session.time_ms()) if session is not None else 0
        except Exception:
            read = 0
        return self._play.at(read)

    def _tick(self) -> None:
        if not self._path:
            self._feed.stop()
            return
        index = int(self._position_ms() // FRAME_MS)
        now = self._clock()
        if index != self._shown:
            frame = self._frames.at(index)
            if frame is not None:
                if frame != self._frame:  # (silence: nothing to draw again)
                    self._screen.set_levels([v / 255.0 for v in frame])
                    self._frame = frame
                self._shown, self._missing_since = index, None
            elif self._missing_since is None:
                self._missing_since = now  # held a moment: it may come at once
            elif now - self._missing_since >= HOLD_MS / 1000.0 and self._shown != -1:
                self._screen.set_levels(RESTING)
                self._shown, self._frame = -1, None
        if now - self._steered_at >= STEER_MS / 1000.0:
            self._steer(index)

    def _still_tick(self) -> None:
        """Under reduced motion while the Stage shows: the song is read on, and once enough of
        it is, its average stays up, still."""
        if not self._path or not self._screen.takes_words() or not OpeningMark.reduced_motion():
            self._slow.stop()
            return
        self._steer(int(self._position_ms() // FRAME_MS))
        self._show_still()

    def _show_still(self) -> None:
        if self._still or not self._slow.isActive():
            return
        count = self._frames.count()
        if count >= STILL_FRAMES or (count and self._end is not None):
            self._still = True
            self._screen.set_levels(self._frames.average())

    # ---- where the sound is read ------------------------------------------------------------

    def _captions_until(self) -> Optional[int]:
        """The frame the captions' levels of this file begin at: where their film run reads
        from, and once their first levels came, where those begin (its first two seconds or
        so are the player finding its clock: measured on hel, 2.2 s). None: they read none."""
        read = getattr(self._captions, "levels_from", None)
        start = read(self._location) if callable(read) and self._location else None
        if start is None:
            return None
        begins = int(float(start) * 1000 // FRAME_MS)
        first = self._captions_first
        return begins if first is None else max(begins, first)

    def _captions_moved(self) -> None:
        """The captions' run began (or ended): at once, so the helper stops where they read."""
        self._captions_first = None
        if self._path and (self._feed.isActive() or self._slow.isActive()):
            self._steer(int(self._position_ms() // FRAME_MS), at_once=True)

    def _steer(self, index: int, *, at_once: bool = False) -> None:
        now = self._clock()
        self._steered_at = now
        gap = self._frames.gap_from(index)
        until = self._captions_until()
        if until is not None and gap >= until:
            if self._captions_first is not None:
                self._end_helper()  # one decode: what is read joins theirs, they read on
            return  # (else their first levels say where theirs begin: the helper waits)
        if until is None and self._end is not None and gap >= self._end:
            return  # read to the song's end
        helper = self._start_helper()
        if helper is None:
            return
        if now - self._told_at >= TELL_MS / 1000.0:
            self._told_at = now
            helper.viewer(index * FRAME_MS)
        asked = self._asked
        if (
            asked is not None
            and asked[1] == until
            and asked[0] <= gap <= self._reach + SLACK_FRAMES
        ):
            return  # it reads towards it
        if asked is not None and asked[1] == until and not at_once:
            if now - self._asked_at < ASK_MS / 1000.0:
                return  # (a drag on the scrub bar: once it rests)
        self._asked, self._reach, self._asked_at = (gap, until), gap, now
        self._told_at = now
        helper.viewer(index * FRAME_MS)
        helper.decode(gap * FRAME_MS, None if until is None else until * FRAME_MS)

    def _start_helper(self) -> Any:
        if self._helper is None and not self._helper_failed and self._path:
            self._serial += 1
            serial = self._serial
            heard = self._heard

            def hear(message: tuple) -> None:  # (the helper's thread)
                try:
                    heard.emit(serial, message)
                except RuntimeError:
                    pass  # QiTV closed meanwhile

            self._helper = self._make_helper(self._path, hear)
            self._asked, self._reach = None, 0
            self._told_at = self._asked_at = float("-inf")
            self._helper.start()
        return self._helper

    def _end_helper(self) -> None:
        helper, self._helper = self._helper, None
        self._asked = None
        if helper is not None:
            self._serial += 1  # its last words reach nothing
            helper.stop()

    # ---- what is read ------------------------------------------------------------------------

    def _on_heard(self, serial: int, message: Any) -> None:
        if serial != self._serial or not self._path:
            return
        kind = message[0]
        if kind == "levels":
            _, start_ms, frame_ms, blob = message
            first = self._store(int(start_ms), int(frame_ms), blob)
            asked = self._asked
            if first is not None and asked is not None and first >= asked[0] - 1:
                self._reach = max(self._reach, first + len(blob) // BANDS)
        elif kind == "ended":
            if message[2] == "end":  # ("until": it waits there, for the captions' levels)
                self._end = int(message[1]) // FRAME_MS
                self._show_still()
        elif kind == "failed":
            logger.info("Visualizer: the helper ended (%s): no bars for this song", message[1])
            self._helper_failed = True
            self._end_helper()

    def _on_captions_levels(self, location: str, start_ms: int, frame_ms: int, blob: Any) -> None:
        if self._path and location == self._location:
            first = self._store(int(start_ms), int(frame_ms), blob)
            if first is not None and (self._captions_first is None or first < self._captions_first):
                self._captions_first = first

    def _store(self, start_ms: int, frame_ms: int, blob: Any) -> Optional[int]:
        if frame_ms != FRAME_MS:
            return None
        first = round(start_ms / FRAME_MS)
        self._frames.put(first, bytes(blob))
        self._show_still()
        return first
