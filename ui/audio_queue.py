"""The album's play order on the audio screen (spec §10): its tracks in order or shuffled, repeat
off, all or one, the next track at a song's end (`auto`) or on a press, and the one before.

Plain data, no widgets and no Qt: the controller asks it which track plays next and plays it.

- Shuffled, the song playing comes first and the others follow once each in a new order;
  turned off, the album's order goes on from the song playing.
- Repeat off: nothing after the last. All: round again from the first (the same order).
  One: the same song again at its end; a press goes on to the next as repeat all does.
- Previous: past RESTART_MS into a song, the same song from its start; else the one before
  (None on the first with repeat off: the caller plays it from its start).
"""

from __future__ import annotations

from collections.abc import Sequence
import random
from typing import Optional

REPEAT_MODES = ("off", "all", "one")  # as the dock's repeat button cycles them
RESTART_MS = 3000  # previous past this far into a song: the same song from its start


class AudioQueue:
    def __init__(
        self,
        tracks: Sequence[dict],
        current: int,
        *,
        shuffle: bool = False,
        repeat: str = "off",
        rng: Optional[random.Random] = None,
    ) -> None:
        """`tracks`: the album's Tracks (services.song_facts) in its order; `current`: the one
        playing. A repeat that is none of REPEAT_MODES is off."""
        if not tracks:
            raise ValueError("An audio queue needs a track")
        if not 0 <= current < len(tracks):
            raise ValueError(f"No track {current} of {len(tracks)}")
        self._tracks = list(tracks)
        self._current = current
        self._repeat = repeat if repeat in REPEAT_MODES else "off"
        self._rng = rng or random.Random()
        self._shuffle = False
        self._order = list(range(len(self._tracks)))
        if shuffle:
            self.toggle_shuffle()

    def __len__(self) -> int:
        return len(self._tracks)

    @property
    def tracks(self) -> list[dict]:
        return list(self._tracks)

    @property
    def current(self) -> int:
        """The track playing (its index in `tracks`)."""
        return self._current

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def repeat(self) -> str:
        return self._repeat

    def track(self, index: int) -> dict:
        return self._tracks[index]

    def index_of(self, path: str) -> Optional[int]:
        """The track whose file is `path` (None: it is not on the album)."""
        for index, track in enumerate(self._tracks):
            if track.get("path") == path:
                return index
        return None

    def order(self) -> list[int]:
        """The tracks' indices in the order they play."""
        return list(self._order)

    def pick(self, index: int) -> None:
        """`index` plays now (a row picked, the same album's song opened): the order goes on
        from it."""
        if not 0 <= index < len(self._tracks):
            raise IndexError(index)
        self._current = index

    def next(self, auto: bool) -> Optional[int]:
        """The track after the current one, made current: at a song's end (`auto`) or on a
        press. None after the last with repeat off (nothing changes then)."""
        if auto and self._repeat == "one":
            return self._current
        at = self._order.index(self._current) + 1
        if at >= len(self._order):
            if self._repeat == "off":
                return None
            at = 0
        self._current = self._order[at]
        return self._current

    def previous(self, position_ms: int) -> Optional[int]:
        """Past RESTART_MS into the song, the same one (from its start); else the one before it,
        made current. None on the first with repeat off: nothing is before it."""
        if position_ms > RESTART_MS:
            return self._current
        at = self._order.index(self._current) - 1
        if at < 0:
            if self._repeat == "off":
                return None
            at = len(self._order) - 1
        self._current = self._order[at]
        return self._current

    def toggle_shuffle(self) -> bool:
        """Shuffle on (the song playing first, then the rest in a new order) or off (the
        album's order). Whether it is on now."""
        self._shuffle = not self._shuffle
        if self._shuffle:
            rest = [i for i in range(len(self._tracks)) if i != self._current]
            self._rng.shuffle(rest)
            self._order = [self._current, *rest]
        else:
            self._order = list(range(len(self._tracks)))
        return self._shuffle

    def cycle_repeat(self) -> str:
        """The next of off, all and one; the one now."""
        self._repeat = REPEAT_MODES[(REPEAT_MODES.index(self._repeat) + 1) % len(REPEAT_MODES)]
        return self._repeat
