"""What the viewer chose for the picture (2.3): its aspect ratio, kept per channel, deinterlacing,
and the statistics shown over it (no widgets).

Aspect ratio (`ASPECTS`): Auto is the stream's own; 16:9 and 4:3 stretch the picture to that
shape; Fill crops it to the window's shape, no bars. `shape_args` gives libVLC an aspect ratio
or a crop: Fill is a crop to the window's ratio, which libVLC 3.0.20 takes in the picture's
display shape, its pixels' own aspect counted (a 720×576 PAL picture with 16:15 pixels cropped
to 1:1 is 540×576; measured), so it fills the window whatever the stream. Both apply at once,
without a reopen; a video output libVLC kept from the media before keeps the old ones until
they are set again (`services.playback`). A live channel's choice is kept for its next tune in
a small file of its own, `picture.json` beside the config (`ChannelPictures`, as
`services.sound_sync` keeps the sound's sync); a film's stays with its player while it plays.

The shape the picture shows in (`shown_ratio`: the stream's own from its size and its pixels'
aspect, `display_ratio`, or the 16:9 or 4:3 chosen) is the shape a box holding it small takes,
so it shows no bars: the picture in picture's, in the window and floating (`box_size`: as
large as the 16:9 box was).

Deinterlacing (`DEINTERLACING`, the config's `deinterlace`): Auto is libVLC's own: pictures the
decoder marks interlaced are deinterlaced (VLC's `deinterlace=-1`, its "auto" method, X); On
deinterlaces every picture (yadif) and Off none. libVLC 3 can set only on or off
(`libvlc_video_set_deinterlace` with a method or NULL; it ignores "auto"), and a player set
either way keeps it: its video outputs inherit the player's setting, which a media option
(":deinterlace=-1") does not reach. So a player Auto was left for comes back to it only when
QiTV starts again. Measured with 3.0.20: On and Off apply at once, without a reopen; with
hardware decoding (VideoToolbox) VLC has the graphics chip hand an interlaced stream's pictures
back as I420 and deinterlaces them on the CPU (its cvpx converter in the filter chain).

Statistics (`stats_rows`): the plain dict `PlaybackSession.video_stats()` gives, as rows to
show. libVLC 3 counts a stream's pictures from its open: decoded, shown, and lost, which are
the ones dropped for coming too late; its bitrates are bytes per microsecond.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Mapping, Optional

from services import i18n
from services.channel_store import ChannelStore
from services.i18n import N_, tr, translate

_LOG = logging.getLogger(__name__)

FILE_NAME = "picture.json"
MAX_ENTRIES = 5000
_VERSION = 1

AUTO, WIDE, STANDARD, FILL = "auto", "16:9", "4:3", "fill"
ASPECTS = (AUTO, WIDE, STANDARD, FILL)  # the order ‹ and › (and A) step through
ON, OFF = "on", "off"
DEINTERLACING = (AUTO, ON, OFF)
DEINTERLACE_METHOD = "yadif"  # On: libVLC's deinterlace method

# What a stream's source is, as the statistics name it.
SOURCE_TS, SOURCE_HLS, SOURCE_FMP4, SOURCE_DIRECT = "ts", "hls", "fmp4", "direct"
SOURCES = {
    # i18n: the statistics' source: the live channel's MPEG-TS, through QiTV's time-shift
    SOURCE_TS: N_("Time-shift, MPEG-TS"),
    SOURCE_HLS: N_("Time-shift, HLS"),  # i18n: the statistics' source
    # i18n: the statistics' source: HLS of fragmented MP4 segments, remuxed to MPEG-TS
    SOURCE_FMP4: N_("Time-shift, HLS (fragmented MP4 as TS)"),
    SOURCE_DIRECT: N_("Direct"),  # i18n: the statistics' source: VLC plays the stream itself
}
# libVLC's four-character codes, as people know the codecs.
_CODECS = {
    "h264": "H.264",
    "avc1": "H.264",
    "hevc": "HEVC",
    "hvc1": "HEVC",
    "mpgv": "MPEG-2",
    "mp2v": "MPEG-2",
    "mp1v": "MPEG-1",
    "mp4v": "MPEG-4",
    "av01": "AV1",
    "vp90": "VP9",
    "vp80": "VP8",
    "mp4a": "AAC",
    "a52 ": "AC-3",
    "eac3": "E-AC-3",
    "mpga": "MPEG audio",
    "mp3 ": "MP3",
    "opus": "Opus",
    "dts ": "DTS",
    "lpcm": "PCM",
}
KBPS_PER_UNIT = 8000  # libVLC's bitrates are bytes per microsecond
# A picture's shape (width / height), for the boxes that hold it small (the PiP's).
WIDE_RATIO, STANDARD_RATIO = 16 / 9, 4 / 3
MIN_RATIO, MAX_RATIO = 1 / 3, 3.0  # beyond these a box shows bars rather than a sliver
SAME_SHAPE = 0.01  # ratios this close (relatively) are one shape


def shape_args(aspect: str, width: int, height: int) -> tuple[Optional[str], Optional[str]]:
    """libVLC's (aspect ratio, crop) for `aspect` in a window `width` × `height`: None, None
    for the stream's own; Fill crops to the window's ratio (none while it has no size)."""
    if aspect in (WIDE, STANDARD):
        return aspect, None
    if aspect == FILL and width > 0 and height > 0:
        common = math.gcd(width, height)
        return None, f"{width // common}:{height // common}"
    return None, None


def display_ratio(
    width: int, height: int, sar: tuple[int, int] = (1, 1), rotated: bool = False
) -> Optional[float]:
    """The shape (width / height) a `width` × `height` picture shows in, its pixels' own aspect
    (`sar`, numerator and denominator; 0 for either: square pixels) counted, and turned a
    quarter for a `rotated` stream (libVLC turns it upright); None while its size is unknown.
    Kept between MIN_RATIO and MAX_RATIO: a box shaped like a sliver shows nothing."""
    if width <= 0 or height <= 0:
        return None
    num, den = sar
    ratio = width * num / (height * den) if num > 0 and den > 0 else width / height
    ratio = 1 / ratio if rotated else ratio
    return max(MIN_RATIO, min(MAX_RATIO, ratio))


def shown_ratio(aspect: str, stream_ratio: Optional[float]) -> Optional[float]:
    """The shape the picture shows in with `aspect` chosen: 16:9 or 4:3, which stretch it, the
    stream's own (`stream_ratio`; None while unknown) with Auto, and None with Fill, which crops
    it to whatever shape its surface has."""
    if aspect == WIDE:
        return WIDE_RATIO
    if aspect == STANDARD:
        return STANDARD_RATIO
    if aspect == FILL:
        return None
    return stream_ratio


def same_shape(a: float, b: float) -> bool:
    """Two ratios no box would tell apart (a 1% difference shows as a pixel or two of bar)."""
    return abs(a - b) <= SAME_SHAPE * max(a, b)


def box_ratio(ratio: Optional[float]) -> float:
    """The shape a box takes for a picture of `ratio`: 16:9 when it is unknown or as good as
    16:9, so a 16:9 box stays exactly the size it always was."""
    if ratio is None or ratio <= 0 or same_shape(ratio, WIDE_RATIO):
        return WIDE_RATIO
    return max(MIN_RATIO, min(MAX_RATIO, ratio))


def box_height(width: int, ratio: float) -> int:
    """The height of a box `width` wide in `ratio`'s shape (`box_ratio`'s)."""
    return round(width * 9 / 16) if ratio == WIDE_RATIO else round(width / ratio)


def box_size(wide_width: float, ratio: Optional[float]) -> tuple[int, int]:
    """A box (width, height) as large as a 16:9 one `wide_width` wide, in the shape of a
    picture of `ratio`: a 4:3 or an upright picture gets as much of the screen as a 16:9
    one, not a sliver or a tower of it."""
    shape = box_ratio(ratio)
    width = round(wide_width * math.sqrt(shape / WIDE_RATIO))
    return width, box_height(width, shape)


def aspect_name(aspect: str) -> str:
    """The Picture panel's name for `aspect`, in the active language."""
    if aspect == FILL:
        return tr("Fill the window")  # i18n: aspect ratio: cropped to the window's shape, no bars
    if aspect in (WIDE, STANDARD):
        return aspect
    return tr("Auto", ctx="aspect ratio")  # i18n: the aspect ratio: the stream's own


def deinterlace_name(choice: str) -> str:
    """The Picture panel's name for a deinterlacing `choice`, in the active language."""
    if choice == ON:
        return tr("On", ctx="deinterlacing")  # i18n: deinterlacing: every picture
    if choice == OFF:
        return tr("Off", ctx="deinterlacing")  # i18n: deinterlacing: none
    # i18n: deinterlacing: VLC's own, for the pictures marked interlaced
    return tr("Auto", ctx="deinterlacing")


def deinterlace_method(choice: str) -> Optional[str]:
    """libVLC's method for On, None for Off (Auto sets nothing: see the module docstring)."""
    return DEINTERLACE_METHOD if choice == ON else None


def stepped(choices: tuple[str, ...], current: str, delta: int) -> str:
    """The choice `delta` steps from `current`, round the ends."""
    index = choices.index(current) if current in choices else 0
    return choices[(index + delta) % len(choices)]


def _entry(value: Any) -> Optional[dict[str, Any]]:
    """A stored entry as `set_aspect()` writes it; None when it is not one."""
    if not isinstance(value, dict):
        return None
    aspect, when = value.get("aspect"), value.get("set")
    if aspect not in ASPECTS or aspect == AUTO:
        return None
    if not isinstance(when, (int, float)) or isinstance(when, bool):
        return None
    return {"aspect": aspect, "set": when}


class ChannelPictures(ChannelStore):
    """Each live channel's aspect ratio (`ChannelPictures(path)`; None: for this run only).

    Keyed as `services.sound_sync` keys its channels (`services.live.tuning.channel_key()`);
    Auto is never stored. Version 1: a later build adds only optional fields to an entry, so
    this one keeps reading the file; older builds never read it."""

    WHAT = "picture settings"
    VERSION = _VERSION
    MAX_ENTRIES = MAX_ENTRIES
    LOG = _LOG
    _entry = staticmethod(_entry)

    def aspect(self, key: str) -> str:
        entry = self._entries.get(key) if key else None
        return str(entry["aspect"]) if entry is not None else AUTO

    def set_aspect(self, key: str, aspect: str) -> None:
        """Keep the channel's aspect ratio (Auto forgets it); write it soon."""
        if not key or aspect not in ASPECTS:
            return
        if aspect == AUTO:
            if self._entries.pop(key, None) is None:
                return
        else:
            if self.aspect(key) == aspect:
                return
            self._entries[key] = {"aspect": aspect, "set": self._now()}
            self._trim()
        self._save()

    def _trim(self) -> None:
        """Beyond the cap, the choices made longest ago go first."""
        excess = len(self._entries) - self._max
        if excess > 0:
            for key in sorted(self._entries, key=lambda k: self._entries[k]["set"])[:excess]:
                del self._entries[key]


# ---- the statistics ------------------------------------------------------------------------


def codec_name(fourcc: str) -> str:
    """ "h264" → "H.264"; a code it does not know as it is, without its padding."""
    return _CODECS.get(fourcc.lower(), fourcc.strip().upper())


def bitrate_text(kbps: float) -> str:
    """ "850 kb/s", or from 1,000 "3.7 Mb/s"."""
    if not math.isfinite(kbps) or kbps <= 0:
        return tr("{kbps} kb/s", kbps=0)  # i18n: a bitrate in kilobits per second
    if kbps < 1000:
        return tr("{kbps} kb/s", kbps=i18n.number(round(kbps)))
    # i18n: a bitrate in megabits per second
    return tr("{mbps} Mb/s", mbps=i18n.decimal(f"{kbps / 1000:.1f}"))


def stats_rows(stats: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The statistics overlay's rows, (name, value), from `PlaybackSession.video_stats()`'s
    dict with what the shell adds ("source", "lag"); a row whose numbers are not known yet is
    left out."""
    rows: list[tuple[str, str]] = []
    video = []
    if stats.get("video_codec"):
        video.append(codec_name(str(stats["video_codec"])))
    width, height = int(stats.get("width") or 0), int(stats.get("height") or 0)
    if width and height:
        video.append(f"{width}×{height}")
    fps = float(stats.get("fps") or 0.0)
    if fps > 0:
        shown = f"{fps:.2f}".rstrip("0").rstrip(".")
        video.append(tr("{fps} fps", fps=i18n.decimal(shown)))  # i18n: frames per second
    if video:
        rows.append((tr("Video"), " · ".join(video)))  # i18n: the statistics' row: its codec…
    audio = []
    if stats.get("audio_codec"):
        audio.append(codec_name(str(stats["audio_codec"])))
    rate = int(stats.get("audio_rate") or 0)
    if rate > 0:
        khz = f"{rate / 1000:.1f}".rstrip("0").rstrip(".")
        audio.append(tr("{khz} kHz", khz=i18n.decimal(khz)))  # i18n: the sound's sample rate
    if audio:
        rows.append((tr("Audio"), " · ".join(audio)))  # i18n: the statistics' row
    decoding = stats.get("decoding")
    if decoding:
        texts = {
            # i18n: the statistics' decoding: Settings allow the graphics chip, VLC picks it
            "hardware": tr("Hardware where VLC can"),
            "software": tr("Software"),  # i18n: the statistics' decoding
            # i18n: the statistics' decoding: back to software after a frozen picture
            "froze": tr("Software (the picture froze in hardware)"),
        }
        rows.append((tr("Decoding"), texts.get(str(decoding), str(decoding))))  # i18n: a row
    if "decoded" in stats:
        rows.append(
            (
                tr("Pictures"),  # i18n: the statistics' row: the video's pictures (frames)
                # i18n: the statistics: pictures decoded, shown, and dropped for coming too late
                tr(
                    "{decoded} decoded · {shown} shown · {late} too late",
                    decoded=i18n.number(int(stats.get("decoded") or 0)),
                    shown=i18n.number(int(stats.get("shown") or 0)),
                    late=i18n.number(int(stats.get("lost") or 0)),
                ),
            )
        )
    if "input_kbps" in stats:
        rows.append(
            (
                tr("Bitrate"),  # i18n: the statistics' row
                # i18n: the statistics: the bitrate read from the source, and after demuxing
                tr(
                    "{read} read · {demuxed} demuxed",
                    read=bitrate_text(float(stats.get("input_kbps") or 0.0)),
                    demuxed=bitrate_text(float(stats.get("demux_kbps") or 0.0)),
                ),
            )
        )
    source = stats.get("source")
    if source in SOURCES:
        rows.append((tr("Source"), translate(SOURCES[str(source)])))  # i18n: the statistics' row
    lag = stats.get("lag")
    if isinstance(lag, (int, float)) and math.isfinite(lag) and lag >= 0:
        seconds = i18n.decimal(f"{lag:.1f}")
        # i18n: the statistics' row: how far the picture plays behind the channel's live edge
        rows.append((tr("Behind live"), tr("{seconds} s", seconds=seconds)))  # i18n: seconds
    return rows
