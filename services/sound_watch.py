"""Lost sound: VLC shows pictures but plays no sound (2.8.8).

A channel whose source left gaps every minute or two (VLC ran dry at each, and libVLC 3.0.20
flushes every decoder when media comes late after one) lost its sound for good while the
picture played on; the stream QiTV served still carried the sound, in step with the picture,
and tuning again brought it back. Android's player rebuilds its audio output when the sound
stops while frames still reach it, and Kodi flushes and seeks to where it plays.

`SoundWatch` reads libVLC's media statistics (`services.picture_watch.Counters`) once a tick.
Once VLC plays, has an audio track and played sound in this stream, a stretch of `SILENT_S`
in which pictures were shown (the last within `RECENT_S`) and no sound played says "cycle":
the caller turns the audio track off and on again (VLC makes a new decoder and audio output).
Still no sound `AGAIN_S` after that while sound keeps reaching the decoder (the stream carries
it: a source sending none is not lost sound) says "reopen", once per stream: the caller opens
the stream again where it plays. Sound that plays again starts it over. A pause, a wait for
data (VLC not Playing) and a stall (no picture shown) are no lost sound; muted sound plays
(libVLC counts its buffers as played).
"""

from __future__ import annotations

from typing import Optional

from services.picture_watch import Counters

SILENT_S = 3.0  # no sound played this long while pictures show: the audio track is cycled
AGAIN_S = 6.0  # still none this long after: the stream opens again
RECENT_S = 1.0  # a picture shown, or sound decoded, within this


class SoundWatch:
    """Says "cycle", then "reopen", when the sound stops while the picture plays (module
    docstring). `update()` each tick; `restart()` for each stream opened (its counters start
    at 0) and after a pause VLC was asked for."""

    def __init__(self) -> None:
        self.restart()

    def restart(self) -> None:
        self._last: Optional[Counters] = None
        self._played_ever = False
        self._shown_at: Optional[float] = None
        self._decoded_at: Optional[float] = None
        self._silent_since: Optional[float] = None
        self._cycled_at: Optional[float] = None
        self._reopened = False

    def update(
        self, now: float, playing: bool, counters: Counters, has_audio: bool
    ) -> Optional[str]:
        """The verdict for this tick: "cycle", "reopen" or None (`playing`: VLC plays;
        `has_audio`: an audio track is selected)."""
        last, self._last = self._last, counters
        if last is None:
            return None
        if counters.shown > last.shown:
            self._shown_at = now
        if counters.audio > last.audio:
            self._decoded_at = now
        if counters.played > last.played:
            self._played_ever = True
            self._silent_since = self._cycled_at = None  # it plays: a later loss starts over
            return None
        if not (playing and has_audio and self._played_ever and self._recent(self._shown_at, now)):
            self._silent_since = None
            return None
        if self._silent_since is None:
            self._silent_since = now
            return None
        if self._cycled_at is None:
            if now - self._silent_since >= SILENT_S:
                self._cycled_at = now
                return "cycle"
            return None
        if (
            not self._reopened
            and now - self._cycled_at >= AGAIN_S
            and self._recent(self._decoded_at, now)
        ):
            self._reopened = True
            return "reopen"
        return None

    @staticmethod
    def _recent(at: Optional[float], now: float) -> bool:
        return at is not None and now - at <= RECENT_S
