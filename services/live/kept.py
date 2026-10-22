"""Kept buffers (2.6): the time-shift buffers of channels the user left.

Leaving a channel parks its session (`LiveSession.park`): the helper closes the provider's
stream and keeps the buffer, served as it stands. What comes back to the player is a
`KeptRing`: the session's id, the channel's key (`services.live.tuning.channel_key`), whether
the buffer is on disk, and where the user was (`Where`). Tuning the channel again resumes that
session there (`LiveSession.start(kept=...)`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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


class KeptBuffers:
    """The parked sessions of the channels left, one per channel (GUI thread).

    Every player's stream slot shares one (`BudgetedStream.kept`): a stop parks its channel
    here (`put`), a start of a channel takes its own back (`take`). A memory buffer costs
    memory as long as it is kept, so one is kept at most: the channel left last. Disk buffers
    are all kept: their total is the user's disk amount, which the helper keeps them under.
    Letting one go closes its session at the engine; one the helper emptied (the disk amount,
    the day's limit, its files gone) goes by itself once its state says so.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._rings: dict[str, KeptRing] = {}  # by channel key, the oldest put first
        self._listen(engine)

    def _listen(self, engine: Any) -> None:
        updated = getattr(engine, "sessionUpdated", None)
        if updated is not None:
            updated.connect(self._on_updated)

    def _on_updated(self, sid: str, snapshot: dict) -> None:
        if snapshot.get("oldest") is None and any(r.sid == sid for r in self._rings.values()):
            self.drop(sid)  # nothing of it is left

    def put(self, ring: KeptRing) -> None:
        old = self._rings.pop(ring.channel, None)
        if old is not None and old.sid != ring.sid:
            self._engine.close_session(old.sid)
        if not ring.disk:
            for other in [r for r in self._rings.values() if not r.disk]:
                self.drop(other.sid)
        self._rings[ring.channel] = ring

    def take(self, channel: str) -> Optional[KeptRing]:
        """The channel's kept buffer, no longer kept here (its player takes it back)."""
        return self._rings.pop(channel, None)

    def drop(self, sid: str) -> None:
        """Let session `sid` go (it is closed)."""
        for channel, ring in list(self._rings.items()):
            if ring.sid == sid:
                del self._rings[channel]
                self._engine.close_session(sid)

    def drop_disk(self) -> None:
        """The disk buffer was turned off: every disk buffer kept goes."""
        for ring in [r for r in self._rings.values() if r.disk]:
            self.drop(ring.sid)

    def use_engine(self, engine: Any) -> None:
        """A new engine (a restart after a self-update): the old one's sessions went with it."""
        self._engine = engine
        self._rings.clear()
        self._listen(engine)

    def rings(self) -> list[KeptRing]:
        return list(self._rings.values())
