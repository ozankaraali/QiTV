"""The smoke's audio read-back rule, kept apart so a test can show it catches a wrong level."""

from __future__ import annotations

from collections.abc import Mapping
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


def should_mute(environ: Mapping[str, str]) -> bool:
    """Does the smoke play to libVLC's dummy audio output? Yes, unless CI set or asked not to.

    QITV_SMOKE_MUTE=1 mutes, 0 plays sound; unset mutes everywhere but CI (a runner has no
    one to hear it, and the dummy output keeps no volume or mute to read back).
    """
    asked = environ.get("QITV_SMOKE_MUTE")
    if asked in ("0", "1"):
        return asked == "1"
    return not environ.get("CI")
