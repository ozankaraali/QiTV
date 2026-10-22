"""Kept buffers (2.6): the time-shift buffers of channels the user left.

Leaving a channel parks its session (`LiveSession.park`): the helper closes the provider's
stream and keeps the buffer, served as it stands. What comes back to the player is a
`KeptRing`: the session's id, the channel's key (`services.live.tuning.channel_key`), whether
the buffer is on disk, and where the user was (`Where`). Tuning the channel again resumes that
session there (`LiveSession.start(kept=...)`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Where:
    """Where the user was in a buffer they left: a timeline position, paused or playing;
    `position` None: at the live edge."""

    position: Optional[float]
    paused: bool


@dataclass(frozen=True)
class KeptRing:
    sid: str  # the parked session at the engine
    channel: str  # the channel's key
    disk: bool  # its buffer is on disk (else in memory)
    where: Where
