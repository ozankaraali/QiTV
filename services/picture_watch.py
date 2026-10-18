"""Hardware decoding's frozen picture: no new picture while the sound plays (2.1).

libVLC 3.0.20's VideoToolbox decoder (the Mac's hardware decoder) skips every picture after
a flush until one made of I slices only. VLC flushes its decoders when its clock resets (a
PCR that came too late, after a stall: "ES_OUT_SET_(GROUP_)PCR is called too late (pts_delay
increased …)", then "ES_OUT_RESET_PCR called"), and a tune starts wherever the stream is. On a
channel whose pictures start again at recovery points (periodic intra refresh) or whose I
pictures are far apart, the picture then stands still while the sound plays. The lab
(headless libVLC 3.0.20 on an Intel Mac, muted, a synthetic 720p25 channel served in real
time on 127.0.0.1, one 3 s stall): with intra refresh the picture stood still for the 26 s
left of the run, in each of five runs (time-shift and direct profiles, deinterlacing auto,
bob or off alike), and a 25 s tune into the channel showed no picture at all; libVLC's
software decoder showed pictures again within 2.9 s of the stall, and a tune's first after
2.3 s. With an I picture every 20 s it stood still until the next one (5.4 s; software
4.2 s). Steady streams, timestamp jumps, bursts, losses, rate changes, reopens, interlaced
and spliced streams showed no difference between the two decoders. QiTV's fallback with
these rules (PlaybackSession, LiveSession and the helper, same lab) acted about 2 s after
the picture stood still, or 4.4-4.8 s after an open that showed none, and pictures came again
in software 2.0-4.0 s later (a direct play after the stall and at a tune, a time-shift
stream after a step onto a start without a keyframe); it never acted on the other channels.

`FrozenPicture` reads libVLC's media statistics (`Counters`) once a tick and says, once per
stream, that the picture froze when VLC plays, video keeps reaching the decoder and audio
keeps decoding (both within the last `RECENT_S`), and no picture was shown, nor dropped as
late, for `FROZEN_S` since the last one. VideoToolbox counts each picture it skips as
decoded, so its freeze shows as video decoding with nothing shown. Before a stream's first
picture, and once video reaches the decoder again after none did for over `RECENT_S` (after
a reset VLC holds back what comes before the next I picture, for both decoders), it waits
`FIRST_PICTURE_S` from then: with a 20 s GOP VideoToolbox showed the I picture 1.8 s after
it came, and a reopen then would have joined after it and waited for the next. A stalled
source (nothing decodes, sound included), an audio-only stream (no video reaches the
decoder), a pause or a wait for data (VLC not Playing), and a display that drops pictures
as late (lost pictures grow: the decoder sends them) are no frozen picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

FROZEN_S = 2.0  # no new picture this long after the last one ...
FIRST_PICTURE_S = 4.0  # ... or this long after a stream played without one
RECENT_S = 1.0  # while video blocks and audio decoded within this


@dataclass(frozen=True)
class Counters:
    """libVLC's media statistics for one stream (libvlc_media_stats_t), counted from its
    open."""

    shown: int  # displayed_pictures
    lost: int  # lost_pictures: decoded, then dropped (too late)
    video: int  # decoded_video: video blocks the decoder took
    audio: int  # decoded_audio


class FrozenPicture:
    """Says once per stream that the picture froze while the sound played (module docstring).

    `update()` each tick; `restart()` for each stream opened (its counters start at 0)."""

    def __init__(self) -> None:
        self.restart()

    def restart(self) -> None:
        self._still: Optional[tuple[float, Counters]] = None  # since when nothing was shown
        self._last: Optional[Counters] = None
        self._video_at: Optional[float] = None  # when video blocks last reached the decoder
        self._flow_since: Optional[float] = None  # since when they reach it without a pause
        self._audio_at: Optional[float] = None  # when audio last decoded
        self._fired = False

    def update(self, now: float, playing: bool, counters: Counters) -> bool:
        """True once: the picture froze while the sound played (`playing`: VLC plays)."""
        if self._fired:
            return False
        last, self._last = self._last, counters
        zero = Counters(0, 0, 0, 0)
        if counters.video > (last or zero).video:
            if self._video_at is None or now - self._video_at > RECENT_S:
                self._flow_since = now  # the decoder gets video (again)
            self._video_at = now
        if counters.audio > (last or zero).audio:
            self._audio_at = now
        still = self._still
        if (
            not playing
            or still is None
            or counters.shown != still[1].shown
            or counters.lost != still[1].lost
        ):
            self._still = (now, counters)  # a picture came (or was dropped), or VLC waits
            return False
        since = still[0]
        hold = FROZEN_S if counters.shown else FIRST_PICTURE_S
        flow = self._flow_since
        if flow is not None and flow > since:
            # VLC gave the decoder nothing for a while (it waited for a keyframe after a
            # reset): it starts again, as at a stream's first picture.
            since, hold = flow, FIRST_PICTURE_S
        if now - since < hold or not self._recent(self._video_at, now):
            return False
        if not self._recent(self._audio_at, now):
            return False
        self._fired = True
        return True

    @staticmethod
    def _recent(at: Optional[float], now: float) -> bool:
        return at is not None and now - at <= RECENT_S
