"""The smoke's audio read-back rule, kept apart so a test can show it catches a wrong level."""

from __future__ import annotations

from typing import Optional


def reading_matches(
    muted: bool, level: int, got: Optional[tuple[int, int]], check_level: bool = True
) -> bool:
    """Does libVLC's (mute, volume) read-back agree with what the manager says?

    `got` is None while a worker holds the player. A player the manager has at `level` must
    not play at another one (review I1: VLC 3's volume-save made a new output start at the
    PiP's 30 while the mixer said 100).
    """
    if got is None or got[0] != int(muted):
        return False
    return not check_level or got[1] == level
