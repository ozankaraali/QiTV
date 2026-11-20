"""Texts Settings shows from more than one place (no Qt): the question it asks before the
image cache is cleared, what it says of OpenSubtitles' key, and the time-shift buffer's sizes
and how far back they reach (the example streams' bitrates)."""

from __future__ import annotations

from typing import Any

from services.i18n import N_, decimal, tr

HD_MBPS = 8  # the example HD stream the estimates are for: megabits per second
UHD_MBPS = 25  # and the example 4K stream

# i18n: asked by Settings' Clear Image Cache before it clears it (2.9.3)
CLEAR_CACHE_QUESTION = N_("Clear the image cache? Posters and logos download again as they show.")
# i18n: Settings → Subtitles, in a build that has an OpenSubtitles key of its own
OPENSUBTITLES_BUILT_IN_HINT = N_(
    "This copy of QiTV has a key of its own: yours is optional, and is used instead when set."
)


def mib_text(size: Any) -> str:
    return tr("{size} MiB", size=size)  # i18n: a size in mebibytes (MiB)


def gib_text(size: Any) -> str:
    return tr("{size} GiB", size=size)  # i18n: a size in gibibytes (GiB), as "1.5"


def cache_size_text(mib: int) -> str:
    """The disk cache's size as Settings says it: "512 MiB", from 1 GiB on "2.25 GiB"."""
    if mib < 1024:
        return mib_text(mib)
    return gib_text(decimal(f"{mib / 1024:.2f}".rstrip("0").rstrip(".")))


def rewind_minutes(mib: int, mbps: int) -> int:
    """The minutes `mib` MiB hold of a stream at `mbps` megabits per second (at least one)."""
    return max(1, round(mib * 1024 * 1024 * 8 / (mbps * 1_000_000 * 60)))


def rewind_text(minutes: int) -> str:
    """An estimated rewind: "About 36 min", "About 4 h 28 min"."""
    hours, rest = divmod(minutes, 60)
    if hours:
        # i18n: an estimated rewind: h hours and m minutes (abbreviated)
        return tr("About {h} h {m} min", h=hours, m=rest)
    # i18n: an estimated rewind: m minutes (abbreviated)
    return tr("About {m} min", m=minutes)


def memory_rewind_text(mib: int) -> str:
    """How far back the memory per stream reaches: "≈ 4 min of HD at 8 Mb/s"."""
    # i18n: how far back the memory reaches: n minutes (abbreviated) of HD video at 8 Mb/s
    return tr("≈ {n} min of HD at 8 Mb/s", n=rewind_minutes(mib, HD_MBPS))


def cache_rewind_text(mib: int) -> str:
    """How far back a disk cache of `mib` MiB reaches, a line per example stream."""
    hd = rewind_text(rewind_minutes(mib, HD_MBPS))
    uhd = rewind_text(rewind_minutes(mib, UHD_MBPS))
    return "\n".join(
        (
            # i18n: an example video stream: its resolution and bitrate (megabits per second)
            " ".join((tr("1080p at 8 Mb/s:"), hd)),
            " ".join((tr("4K at 25 Mb/s:"), uhd)),
        )
    )
