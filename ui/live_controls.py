"""The live channel's time-shift side of the TV shell (spec §6, §6.9, §9).

Which live links play through the time-shift pipeline, and with which budget slot; the
transport's time-shift controls (seek, speed, Go live, ⏮ to the programme start, the bar's
programme ticks); and LiveSession's reports: its mode and timeline, its notices (time-shift
unavailable, once per channel and run) and its failures.

A live channel that played in this tune is never given up on while it stays chosen
(`services.revive`), as a TV's: the time-shift helper waits for its stream itself, and a
direct play that ends or fails opens again in the same player, so in the same budget slot,
after a growing wait (RECONNECT_MS, then the revive's 10 s, 30 s, 60 s, 120 s and every
5 minutes; HEALTHY_S of play starts them anew): LiveSession's direct play by itself
(`revives()`), one the player plays itself (an on-demand restream may show a placeholder and
end) here. Meanwhile the pill over the picture (ui/overlays/retry_notice.py) says "The stream
ended · Trying again in 0:25", and its Try now (a click, or Enter) tries at once. A channel
that never played opens again twice at most, then "The stream ended", as before. An STB
channel that fails gets a fresh link instead (its play token may have expired): at once the
first time in a tune and each time it played HEALTHY_S since, then, once it played, after the
revive's waits. A refusal for the provider's connection limit is never retried and says so.

Kept buffers (2.6): coming back to a channel whose buffer was kept, it plays live and the
session offers where the user was (`LiveSession.backOffered`): the Resume pill
(ui/overlays/back_notice.py) says when for `BACK_NOTICE_MS`, until the channel changes; a click,
Enter or the phone's Resume plays on from there (`go_back`).

The live ⚙ (2.8): a channel whose HLS stream comes in several picture sizes (the helper names
them, `LiveSession`'s timeline carries them) offers them; the sizes are kept per channel for
the run, so the ⚙ stays while a new size opens. A size chosen there tunes the channel again
(TVController.change_live_quality); where the user was is then played on at once, not offered.

Recording (2.2): R and the record button record the time-shifted channel being watched from
where it plays (rewound or not) into the Recordings folder (`services.recording`), or stop
it; the record button is lit and the REC chip counts while the large picture's channel
records. A channel played directly, or one still opening, says why it cannot record.

Provider catch-up (2.3, `ui.catchup_controls.CatchUp`): on a channel with an archive the bar
reaches further back than the buffer; seeking there, stepping back past the buffer's start or
⏮ to a programme that began before it plays the provider's archive, and the transport's
controls then act on it (Go live returns to the live channel). An STB portal's archive plays
the recording of a programme of its guide, whose link the portal makes (asked in the side lane).

TVController owns the target and the LiveTuner and hands this unit what it needs. With
picture in picture it is the large picture's: a swap hands it the other player's tuner, live
session and player (`use_slot`); a multiview's focused tile is the large picture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from itertools import compress, count, repeat
import logging
import math
import time
from typing import Any, Callable, Optional, Sequence, cast
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer, SignalInstance
from PySide6.QtGui import QPixmap

from services import favorites, recording
from services.catchup_memory import CatchupMemory
from services.epg_view import Programme
from services.i18n import N_, tr, tr_n, translate
from services.live import parts as live_parts
from services.live.live_session import UNAVAILABLE, LiveSession
from services.live.refusals import REFUSALS, STREAM_LIMIT, STREAM_REFUSED
from services.live.tuning import channel_key
from services.network_streams import DOWN, failure_reason
from services.playback import PlaybackState
from services.provider_api import known_http_only, portal_http_only
from services.revive import revive_wait
from services.vlc_profile import PlayKind
from ui import item_provider
from ui.catchup_controls import NO_RECORDING_TEXT, NOT_AVAILABLE_TEXT, CatchUp
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, LiveTuner
from ui.overlays.back_notice import LEFT_OFF
from ui.overlays.painting import fmt_clock
from ui.overlays.retry_notice import ended_text
from ui.overlays.transport import NO_RECORDING, NOTHING_PLAYING, Transport

logger = logging.getLogger(__name__)

LIVE_SEEK_S = 10.0  # ←/→ while time-shifting (spec §6.7)
BACK_NOTICE_MS = 60_000  # 2.6: the Resume pill shows this long (the owner: "a minute or so")
_PAST_BUFFER_S = 1.0  # a point this far before the buffer's start is the archive's


_WINDOW_SEEK_S = 30.0  # 2.7: a guide's start waits this long for its channel's window


def _reach(state: dict) -> float:
    """How far back the live session plays (timeline seconds): its buffer's start, or (2.7)
    the start of the channel's own server window; a provider's archive is reached beyond,
    unless it plays through the session already (its steps and seeks are the session's)."""
    if state.get("archive_part"):
        return -math.inf
    return float(state.get("window_start", state["start"]))


_AT_START_S = 5.0  # ⏮ this near a programme's start goes to the one before (the archive)
TICK_PROGRAMMES = 6  # programme starts marked on the time-shift bar
# When its limit is not known (shown with translate()).
LIMITED_TEXT = N_("Your provider limits concurrent streams")  # i18n: error toast
ERROR_MS = 6000
# The waits before the first reopens of a live stream that ended (a stream that never played
# gets these only); one that played goes on with the revive's (`services.revive`).
RECONNECT_MS = (1000, 3000)
HEALTHY_S = 60.0  # a stream that played this long before it ended has its waits back
# Plan 4: a link opened with Open Link is tried again this often at most, until it plays well
# again (HEALTHY_S): one that keeps failing stops and says why, never reconnected forever.
LINK_TRIES = 1
# i18n: toast: a live channel's stream ended and is opened again
RECONNECTING_TEXT = N_("Reconnecting…")
RETRY_TICK_MS = 1000  # the pill's countdown


def reopen_wait_ms(tries: int) -> int:
    """The wait before reopen `tries` + 1 of an ended live stream: RECONNECT_MS, then the
    revive's longer ones (`services.revive.REVIVE_S`)."""
    if tries < len(RECONNECT_MS):
        return RECONNECT_MS[tries]
    return round(revive_wait(tries) * 1000)


# i18n: a toast: R or the record button was pressed while the channel is still opening
NOT_YET_TEXT = N_("Recording can start once the channel plays")
# i18n: a toast: R on a channel that already records on schedule (2.9.7)
ON_SCHEDULE_TEXT = N_("This channel is recording on schedule already.")
_PIPELINE_SCHEMES = ("http", "https")  # what the pipeline's source can read
# Never opened again: on-demand content, and a file listed as a channel ends for good.
NOT_REOPENED = (PlayKind.VOD_FILE, PlayKind.VOD_STREAM, PlayKind.LIVE_PIPELINE)


def stream_options(item: dict, catalog: Any = None) -> dict[str, Any]:
    """A channel's own HTTP options for VLC's direct play: its agent (with `catalog`, 2.8: an
    STB portal's box's when it names none), and the referrer its M3U names (2.1,
    `:http-referrer`); an item without them plays exactly as before."""
    agent = item_provider.stream_agent(catalog, item) if catalog is not None else None
    options: dict[str, Any] = {"user_agent": agent or item.get("user_agent")}
    if item.get("referrer"):
        options["referrer"] = item["referrer"]
    return options


def reopen_options(player, item: dict, catalog: Any = None) -> dict[str, Any]:
    """An ended stream opened again in `player`: the channel's options, software decoding
    when its open decoded in software (a channel whose picture froze before, or since), and the
    size cap its open asked VLC for (2.8, the live ⚙)."""
    options = stream_options(item, catalog)
    if player.decodes_in_software():
        options["software"] = True
    cap = player.max_height()
    if cap > 0:
        options["max_height"] = cap
    return options


@dataclass
class Reopen:
    """An ended direct stream on its way to open again in its player, as a swap hands it to
    the picture that has that player now (`take_reopen`, `adopt_reopen`)."""

    target: Target
    url: str
    kind: PlayKind
    remaining_ms: int  # of its back-off
    count: int  # its reopens so far
    played: bool = False  # its tune played: it comes back by itself (`services.revive`)


def _named(title: str, message: str) -> str:
    """A failure after the channel's name, when it has one."""
    # i18n: an error toast: a channel's, a film's or a provider's name, then what went wrong
    return tr("{name}: {message}", name=title, message=message) if title else message


def _identities(items: list[dict]) -> dict[str, set[str]]:
    """What `channel_key` tells each channel by, by field: its address, else its id, else its
    name (the provider aside)."""
    found: dict[str, set[str]] = {"cmd": set(), "id": set(), "name": set()}
    for item in items:
        cmd = str(item.get("cmd") or "").strip()
        if cmd:
            found["cmd"].add(cmd)
        elif item.get("id") is not None:
            found["id"].add(str(item["id"]))
        elif item.get("name"):
            found["name"].add(str(item["name"]))
    return found


class LiveControls(QObject):
    def __init__(
        self,
        *,
        tuner: LiveTuner,
        live: Optional[LiveSession],
        session,
        catalog,
        config,
        lanes: LinkLanes,
        transport: Transport,
        now: Callable[[], datetime],
        current: Callable[[], Optional[Target]],
        closed: Callable[[], bool],
        programmes: Callable[[dict, datetime, int], list[Programme]],
        now_title: Callable[[dict], Optional[tuple[str, float]]],
        noted: Optional[Callable[[str, dict, str, float], None]] = None,
        toast: Callable[..., None],
        error: Callable[[str], None],
        request: Callable[[Target], None],
        restore_channel: Callable[[], None],
        show_transport: Callable[[], None],
        logo: Callable[[dict], Optional[QPixmap]] = lambda channel: None,
        explain: Callable[[dict, str], None] = lambda item, title: None,
        may_reopen: Callable[[Target], bool] = lambda target: True,
        memory: Optional[CatchupMemory] = None,
        prepare: Callable[[Any, Target], None] = lambda session, target: None,
        archive_allowed: Callable[[], bool] = lambda: True,
        beside_recording: Optional[Callable[[Target, float, Optional[float]], None]] = None,
        gave_up: Callable[[], None] = lambda: None,
        runner: Any = None,
        back_notice: Callable[[], Any] = lambda: None,
        on_sizes: Callable[[], None] = lambda: None,
        borrow: Callable[[dict, str, str], Any] = lambda item, provider, purpose: None,
        retry_notice: Callable[[], Any] = lambda: None,
        link: Callable[[dict], bool] = lambda item: False,
        link_failed: Callable[[Target, str], None] = lambda target, why: None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`explain(item, title)` asks why the failure just toasted happened at the item's
        provider (TVController._explain_item). `may_reopen(target)`: whether an ended
        direct stream may open again (a multiview tile's checks its provider's slot
        first). Catch-up: `memory` keeps whether each channel's archive answered (None: for
        this run only), `prepare(session, target)` readies the player for a channel before
        its archive opens, `archive_allowed()` says whether archives may play now,
        `beside_recording(target, moment, until)` opens a rewind's archive while the channel
        records (CatchUp's), `runner` runs the archive probes (a JobRunner by default).
        `gave_up()`: the channel's live session failed for good (said; a multiview's focused
        tile then stops and shows its card). `on_sizes()`: the channel's picture
        sizes came, or an archive started or ended (2.8: the ⚙ may show or go).
        `retry_notice()`: the pill that counts down to a channel's next try (None: none).
        `link(item)`: whether the item is a link of Network streams (plan 4), tried again once
        at most (`LINK_TRIES`); `link_failed(target, why)` hears why one did not play or went
        (an English text, `services.network_streams.failure_reason`)."""
        super().__init__(parent)
        self._link = link
        self._link_failed = link_failed
        # A link's tries again since it last played well, and since when it plays (None: it
        # does not, or a try went since).
        self._link_tries = 0
        self._link_well_since: Optional[float] = None
        self._on_sizes = on_sizes
        self._borrow = borrow  # 2.9.7: a scheduled recording's session, to watch
        self._gave_up = gave_up
        # 2.8: each channel's picture sizes as last seen this run (by its key), and whether the
        # next place offered is played on at once (a size chosen in the ⚙).
        self._sizes: dict[str, dict] = {}
        self._chosen: dict[str, int] = {}  # the cap chosen per channel this run (it counts)
        self._back_at_once = False
        self._back_notice = back_notice  # 2.6: the Resume pill (None: none)
        self._back: Optional[float] = None  # where the user was, while it is offered
        self._back_label: Optional[str] = None
        self._back_timer = QTimer(self, singleShot=True)
        self._back_timer.timeout.connect(self.forget_back)
        self._wired_notices: set[int] = set()
        self._may_reopen = may_reopen
        self._tuner = tuner
        self.live = live
        self.session = session
        self.catalog = catalog
        self.config = config
        self._lanes = lanes
        self._transport = transport
        self._now = now
        self._current = current
        self._closed = closed
        self._programmes = programmes
        self._now_title = now_title
        # 2.9.7: a recording started with R: (its file, the channel, the programme on now, when)
        self._noted = noted
        self._toast = toast
        self._error = error
        self._request = request
        self._restore_channel = restore_channel
        self._show_transport = show_transport
        self._logo = logo
        self._explain = explain
        self._ticks: list[tuple[float, str]] = []
        # 2.7: how far back each channel's own server keeps it (seconds, as last seen this
        # run, by the channel's key), and a guide's start waiting for its window to show.
        self._windows: dict[str, float] = {}
        self._window_items: dict[str, dict] = {}  # the channel each window was seen on
        self._window_seek: Optional[tuple[str, float, float]] = None  # key, moment, asked at
        self._wired = False
        self._reconnects = 0  # reopens of the current tune's ended stream so far
        self._playing_since: Optional[float] = None  # the player's last "playing" (monotonic)
        self._played = False  # the current tune played (it comes back by itself then)
        self._reopen_due: Optional[tuple[Target, str, PlayKind]] = None  # after the back-off
        self._reopen_timer = QTimer(self, singleShot=True)
        self._reopen_timer.timeout.connect(self._reopen)
        # An STB channel's fresh link after the revive's wait (`relink`).
        self._relink_due: Optional[Target] = None
        self._relink_timer = QTimer(self, singleShot=True)
        self._relink_timer.timeout.connect(self._relink_now)
        # The pill counting down to the channel's next try: when it is due (monotonic), what
        # Try now does, and whether LiveSession's `retrying` drives it (else a wait of ours).
        self._retry_notice = retry_notice
        self._retry_due: Optional[float] = None
        self._retry_action: Optional[Callable[[], object]] = None
        self._retry_from_live = False
        self._retry_timer = QTimer(self, interval=RETRY_TICK_MS)
        self._retry_timer.timeout.connect(self._show_retry_text)
        self._wired_retry: set[int] = set()
        self.catchup = CatchUp(
            tuner=tuner,
            session=session,
            catalog=catalog,
            config=config,
            memory=memory if memory is not None else CatchupMemory(None),
            now=now,
            toast=toast,
            request=request,
            changed=self._on_archive_changed,
            limit=self.limit,
            prepare=prepare,
            allowed=archive_allowed,
            beside_recording=beside_recording,
            runner=runner,
            lanes=lanes,  # an STB portal makes its recordings' links
            parent=self,
        )

    def wire(self) -> None:
        transport = self._transport
        transport.seekRelativeRequested.connect(self._seek_relative_ms)
        transport.speedRequested.connect(self._set_speed)
        transport.goLiveRequested.connect(self.go_live)
        transport.programmeStartRequested.connect(self._programme_start)
        transport.seekToRequested.connect(self._seek_to)
        transport.recordRequested.connect(self.toggle_recording)
        self._wired = True
        self._connect(True)

    def _connect(self, connect: bool) -> None:
        pairs: list[tuple[SignalInstance, Callable[..., None]]] = [
            (self._tuner.startFailed, self._on_live_start_failed)
        ]
        live = self.live
        if live is not None:
            pairs += [
                (live.modeChanged, self._on_live_mode),
                (live.timelineChanged, self._on_live_timeline),
                (live.notice, self._on_live_notice),
                (live.failed, self._on_live_failed),
                (live.recordingChanged, self._on_recording_changed),
            ]
            offered = getattr(live, "backOffered", None)
            if offered is not None:
                pairs.append((offered, self._on_back_offered))
            retrying = getattr(live, "retrying", None)
            if retrying is not None:
                pairs.append((retrying, self._on_live_retrying))
        for signal, slot in pairs:
            if connect:
                signal.connect(slot)
            else:
                signal.disconnect(slot)

    # ---- 2.6: where a channel come back to was left ---------------------------------------
    def _on_back_offered(self, t: float, wall: float) -> None:
        if self._closed():
            return
        if self._back_at_once:  # 2.8: a new size of the channel goes on where the user was
            self._back_at_once = False
            self._back = t
            live = self.live
            if self.go_back() and live is not None and live.back_paused():
                live.toggle_pause()  # as it was left: paused
            return
        self._back = t
        self._back_label = fmt_clock(datetime.fromtimestamp(wall, timezone.utc).astimezone())
        notice = self._back_notice()
        if notice is not None:
            if id(notice) not in self._wired_notices:
                self._wired_notices.add(id(notice))
                notice.keepWatching.connect(self.go_back)
            notice.show_notice(translate(LEFT_OFF, time=self._back_label))
        self._back_timer.start(BACK_NOTICE_MS)

    def go_back(self) -> bool:
        """Resume (the pill, Enter, the phone): play on from where the user was; False when
        nothing is offered."""
        back = self._back
        if back is None or self.live is None:
            return False
        self.forget_back()
        self.live.go_back_to(back)
        self._show_transport()
        return True

    def back_label(self) -> Optional[str]:
        """When the channel was left ("20:10") while Resume is offered; else None."""
        return self._back_label if self._back is not None else None

    def forget_back(self) -> None:
        self._back = self._back_label = None
        self._back_timer.stop()
        notice = self._back_notice()
        if notice is not None:
            notice.hide()

    def use_slot(self, tuner: LiveTuner, live: Optional[LiveSession], session) -> None:
        """The large picture is another player's now (a PiP swap): act on its stream."""
        self.forget_back()
        if self._wired:
            self._connect(False)
        self._tuner, self.live, self.session = tuner, live, session
        if self._wired:
            self._connect(True)
        self._forget_channel()
        self.catchup.use_slot(tuner, session)  # the new large picture's archive, if any
        self._on_recording_changed()  # the REC chip is the new large picture's
        waiting = getattr(live, "retry_in", None)
        seconds = waiting() if callable(waiting) else None
        if seconds is not None:  # its stream was down, small: the pill says so large
            self._live_retrying(seconds, counted=False)

    # ---- tuning -------------------------------------------------------------------------------

    def new_target(self) -> None:
        """A new target plays: the old channel's ticks, reopen and archive are gone, and so is
        the Resume pill (2.6)."""
        self._back_at_once = False
        self.forget_back()
        self._forget_channel()
        self.catchup.reset()

    def _forget_channel(self) -> None:
        self._ticks = []
        self._reconnects = 0
        self._link_tries = 0
        self._link_well_since = None
        self._playing_since = None
        self._played = False
        self._cancel_reopen()
        self._cancel_relink()
        self._hide_retry()

    def window_reach(self, channel: dict) -> float:
        """How far back (seconds) the channel's own server keeps it, as seen this run (2.7);
        0.0 when unknown."""
        return self._windows.get(self.catchup.key(Target(channel, "live", "")), 0.0)

    def deepest_window(self, channels: Sequence[dict], beyond: float = 0.0) -> float:
        """The longest own server window (seconds, as seen this run: 2.7) of any of
        `channels` when longer than `beyond`, else 0.0. As the longest `window_reach`, without
        a key (a SHA-256) for each channel of a list (the guide's: up to 264,000): only one with
        the address, else id, else name of a channel whose window was seen gets one."""
        longer = {key: seconds for key, seconds in self._windows.items() if seconds > beyond}
        if not longer:
            return 0.0
        seen = [self._window_items.get(key) for key in longer]
        if any(item is None for item in seen):  # (each window is noted with its channel)
            return max((r for r in map(self.window_reach, channels) if r > beyond), default=0.0)
        rows: set[int] = set()
        for field, values in _identities(cast(list[dict], seen)).items():
            if values:  # (in C: 264,000 channels in a few tens of milliseconds)
                texts = map(str, map(dict.get, channels, repeat(field), repeat("")))
                if field == "cmd":
                    texts = map(str.strip, texts)
                rows.update(compress(count(), map(values.__contains__, texts)))
        best = 0.0
        for row in rows:
            key = self.catchup.key(Target(channels[row], "live", ""))
            best = max(best, longer.get(key, 0.0))
        return best

    def watch_from(self, channel: dict, moment: float) -> bool:
        """The guide's "Watch from the start" within the channel's own server window (2.7):
        the live session plays from `moment` (Unix seconds), at once when the channel plays,
        else once its window shows (the caller tunes it). False when its window, as seen
        this run, does not reach that far."""
        key = self.catchup.key(Target(channel, "live", ""))
        now = self._now().timestamp()
        if now - moment > self._windows.get(key, 0.0):
            return False
        self._window_seek = (key, moment, now)
        state = self._tuner.timeline
        if state is not None and "window_start" in state:
            self._seek_window(state)
        return True

    def _seek_window(self, state: dict) -> None:
        pending, current = self._window_seek, self._current()
        if pending is None or current is None or self.live is None or not self.pipeline_live():
            return
        key, moment, asked = pending
        if self._now().timestamp() - asked > _WINDOW_SEEK_S:
            self._window_seek = None  # the channel never came: forgotten
            return
        if self.catchup.key(current) != key:
            return  # still tuning it
        self._window_seek = None
        t = live_parts.t_in(state, moment, self._now().timestamp())
        if t is not None:
            self.catchup.cancel()
            self.live.seek_to(t)
            self._show_transport()

    def open_archive(self, target: Target, url: str) -> bool:
        """`target` (with `archive_at`) plays its archive instead of live: False when it
        offers none now (it plays live then)."""
        if target.archive_at is None:
            return False
        return self.catchup.open(
            target,
            target.archive_at,
            until=target.archive_until,
            link=url,
            live_playing=False,
        )

    def through_pipeline(self, url: str, kind: PlayKind) -> bool:
        """Whether a live link plays through the time-shift pipeline.

        Its source reads http(s) only; container files listed as channels (M3U) play as files.
        """
        if self.live is None or kind is PlayKind.VOD_FILE:
            return False
        return urlsplit(url).scheme.lower() in _PIPELINE_SCHEMES

    def limit(self, item: Optional[dict] = None) -> Optional[int]:
        """The simultaneous streams of the item's provider (the browsed one without an item):
        None when unlimited or unknown."""
        if item is None:
            provider, reported = self.catalog.current_provider(), self.catalog.max_connections()
        else:
            provider = item_provider.provider_dict(self.catalog, item)
            reported = item_provider.max_connections(self.catalog, item)
        return self._tuner.budget.limit_for(provider, reported)

    def limit_text(self, limit: Optional[int]) -> str:
        if not limit:
            return translate(LIMITED_TEXT)
        return tr_n(
            "Your provider allows {n} stream at a time",  # i18n: error toast
            "Your provider allows {n} streams at a time",
            limit,
        )

    def tune_pipeline(self, target: Target, url: str) -> bool:
        """Ask for a budget slot for `target` (spec §6.9); it starts once the old one is released.

        False when the provider's limit is taken by other streams (picture in picture, say):
        the toast says so and the running channel goes on.
        """
        name = item_provider.provider_name(self.catalog, target.item)
        # 2.9.7: a channel recording on schedule plays its recording's session: no stream of
        # its own, so no limit to refuse it.
        borrowed = self._borrow(target.item, name, self._tuner.purpose)
        if borrowed is None and self.refuse_over_limit(target):
            logger.info(
                "Not tuning %s (%s): its provider has no stream to spare.",
                _log_name(target.item),
                name,
            )
            return False
        # 2.9.7: each tune named in the log (no address): what was watched, and how.
        logger.info(
            "Tuning %s (%s) in the %s player: %s.",
            _log_name(target.item),
            name,
            self._tuner.purpose,
            "the scheduled recording's stream" if borrowed is not None else "a stream of its own",
        )
        try:
            start, limit = self.live_start(target, url), self.limit(target.item)
        except Exception:
            if borrowed is not None:
                borrowed.give_back()  # 2.9.7: never started: its owner has it back
            raise
        self._tuner.tune(start, name, limit, borrowed)
        return True

    def refuse_over_limit(self, target: Target) -> bool:
        """Other players' streams (picture in picture, tiles) take the whole limit of
        `target`'s provider: True, the toast says so and the running channel goes on. A
        closing picture's stream does not count: what opens waits for it."""
        name = item_provider.provider_name(self.catalog, target.item)
        limit = self.limit(target.item)
        if not self._tuner.refused(name, limit):
            return False
        logger.info("Not opening %s: the provider allows %s streams", target.title, limit)
        self._restore_channel()
        self._toast(self.limit_text(limit), "error", ERROR_MS)
        return True

    def live_start(self, target: Target, url: str) -> LiveStart:
        """How the pipeline asks for `target`'s link: its agent, the provider's TLS check and
        whether the helper asks it over HTTPS first, HTTP when that does not open (2.7.1:
        unless its host answered only over HTTP this run, or it is the HTTP address of an
        Xtream panel, or of an STB portal that answered only over HTTP (2.8.1): their sign-in
        found no HTTPS); and the channel's key for what it taught stay-live (Auto's target, a
        locked rate)."""
        provider = item_provider.provider_dict(self.catalog, target.item)
        user_agent = item_provider.user_agent(self.catalog, target.item)
        verify_ssl = bool(provider.get("ssl_verify", self.config.ssl_verify))
        channel = channel_key(str(provider.get("name", "")), target.item)
        referrer = str(target.item.get("referrer") or "")
        kind = item_provider.provider_type(self.catalog, target.item)
        plain = kind == "XTREAM" or portal_http_only(provider)  # its sign-in found no HTTPS
        https = not known_http_only(url) and not (plain and url.startswith("http://"))
        # An STB portal's channel that fails gets a fresh link (`relink`), never the old again;
        # a link of Network streams is tried again once (`once`), by the player's own retry.
        link = self._link(target.item)
        revive = kind != "STB" and not link
        return LiveStart(
            url,
            user_agent,
            verify_ssl,
            channel,
            referrer,
            https,
            target.max_height,
            revive,
            once=link,
        )

    def pipeline_live(self) -> bool:
        """The current channel plays from the time-shift buffer."""
        current = self._current()
        return current is not None and current.live and self._tuner.pipeline()

    def stop_tune(self) -> None:
        self._cancel_reopen()
        self._cancel_relink()
        self._hide_retry()
        self.catchup.reset()
        self._tuner.stop()
        self._ticks = []

    def _stb(self, current: Optional[Target]) -> bool:
        return (
            current is not None
            and current.live
            and item_provider.provider_type(self.catalog, current.item) == "STB"
        )

    def may_retry(self, current: Optional[Target]) -> bool:
        """An STB channel's link may be made again at once once per tune (its play token
        expires), and once more each time its fresh link played for `HEALTHY_S` (its token
        expired again), as an ended direct stream gets its reopens back."""
        return (
            self._stb(current)
            and current is not None
            and (not current.retried or self._played_well())
            and not self._lanes.busy()
        )

    def relink(self, current: Optional[Target]) -> bool:
        """`current`, an STB channel, failed or ended: its link is made again (`retry`), at
        once when `may_retry`, else, once it played since it was chosen, after the revive's
        next wait (`services.revive`; the pill counts down, Try now makes it at once), for as
        long as it stays chosen. False: not an STB channel's, or one that never played (the
        caller says why, as before)."""
        if self._relink_due is not None and self._relink_due is current:
            return True  # already on its way
        if self.may_retry(current):
            assert current is not None
            self.retry(current)  # STB play tokens expire: a fresh link before the error
            return True
        if not self._stb(current) or self._lanes.busy():
            return False
        assert current is not None
        if not (current.played or self._played):
            return False
        wait = revive_wait(current.relinks)
        logger.info("Making the link of %s again in %.0f s (it keeps failing)", current.title, wait)
        self._relink_due = current
        self._relink_timer.start(round(wait * 1000))
        self._show_retry(wait, self._relink_now)
        return True

    def _relink_now(self) -> None:
        target, self._relink_due = self._relink_due, None
        self._relink_timer.stop()
        if target is None or self._closed() or self._current() is not target:
            self._hide_retry()
            return  # something else plays now
        self.retry(target, relinks=target.relinks + 1)

    def _cancel_relink(self) -> None:
        self._relink_due = None
        self._relink_timer.stop()

    def _played_well(self) -> bool:
        """The player played for `HEALTHY_S` since it last started playing."""
        since = self._playing_since
        return since is not None and time.monotonic() - since >= HEALTHY_S

    def reconnect(self, current: Target, *, failed: bool = False) -> bool:
        """`current`'s live stream, played directly, ended (or `failed` to play): open it
        again after a back-off (`reopen_wait_ms`).

        The same player opens the same stream (its ended input ends before the new one
        opens), so it keeps its budget slot and the provider never sees two connections.
        False when nothing more is tried: a tune that never played twice at most (and never
        after a failure), never on-demand content or a file listed as a channel; the caller
        says "The stream ended" (or why it failed) then. One that played is never given up on.
        """
        if self._reopen_due is not None:
            return True  # already on its way
        url, kind = self.session.url, self.session.kind
        if not url or kind is None or kind in NOT_REOPENED:
            return False
        since, self._playing_since = self._playing_since, None
        if since is not None and time.monotonic() - since >= HEALTHY_S:
            self._reconnects = 0  # it played well since it last opened: the back-off anew
        if not self._played and (failed or self._reconnects >= len(RECONNECT_MS)):
            self._hide_retry()  # the caller says why
            return False
        if not self._link_may_try(current):
            self._hide_retry()  # a link had its try again: the caller says why
            return False
        wait_ms = reopen_wait_ms(self._reconnects)
        self._reopen_due = (current, url, kind)
        self._reopen_timer.start(wait_ms)
        self._reconnects += 1
        logger.info(
            "%s's stream %s; opening it again in %.0f s (try %d)",
            current.title,
            "failed" if failed else "ended",
            wait_ms / 1000,
            self._reconnects,
        )
        self._show_retry(wait_ms / 1000, self._reopen_now)
        return True

    def _reopen(self) -> None:
        due, self._reopen_due = self._reopen_due, None
        if due is None or self._closed():
            return
        target, url, kind = due
        session = self.session
        if self._current() is not target or session.url != url:
            self._hide_retry()
            return  # something else plays now
        if not self._may_reopen(target):
            self._hide_retry()
            return  # no stream of its provider to spare (the caller says so)
        logger.info("Opening %s again after its stream ended", target.title)
        if self._retry_action is not None and not self._retry_from_live:
            self._show_retry(0.0, self._reopen_now)  # "Reconnecting…" until it plays
        session.play(
            url,
            kind=kind,
            latency=self.config.live_latency,
            **reopen_options(session, target.item, self.catalog),
        )

    def _reopen_now(self) -> None:
        if self._reopen_due is not None:
            self._reopen_timer.stop()
            self._reopen()

    def _cancel_reopen(self) -> None:
        self._reopen_due = None
        self._reopen_timer.stop()

    # ---- the pill over a channel that comes back by itself (`services.revive`) ----------
    def retry_now(self) -> bool:
        """Try now (the pill's click, Enter): the channel waited for opens at once. False
        when nothing is waited for."""
        action = self._retry_action
        if action is None or self._closed():
            return False
        action()
        return True

    def retrying(self) -> bool:
        """The pill shows: the channel's next try is waited for."""
        return self._retry_action is not None

    def _on_live_retrying(self, seconds: float) -> None:
        """LiveSession's `retrying`: its channel's stream is down (the helper waits for it,
        or its direct play opens again by itself), or no longer (< 0)."""
        self._live_retrying(seconds, counted=True)

    def _live_retrying(self, seconds: float, *, counted: bool) -> None:
        """`counted`: a new try is waited for (a swap only shows the one waited for). A link
        whose stream went down again after its try stops instead (`_link_may_try`)."""
        current = self._current()
        if self._closed() or current is None or not current.live:
            return
        if seconds < 0.0:
            if self._link(current.item):
                self._link_well_since = time.monotonic()  # it came back: it plays again
            if self._retry_from_live:
                self._hide_retry()
            return
        live = self.live
        if live is None:
            return
        if counted and not self._link_may_try(current):
            self._give_up_link(current)
            return
        self._show_retry(seconds, live.retry_now, from_live=True)

    # ---- plan 4: a link of Network streams is tried again once ---------------------------
    def _link_may_try(self, current: Target) -> bool:
        """Whether `current` may be tried again now (counted): any channel; a link once
        (`LINK_TRIES`) until it played well again (`HEALTHY_S` since it last came back)."""
        if not self._link(current.item):
            return True
        since = self._link_well_since
        if since is not None and time.monotonic() - since >= HEALTHY_S:
            self._link_tries = 0
        if self._link_tries >= LINK_TRIES:
            return False
        self._link_tries += 1
        self._link_well_since = None
        return True

    def _give_up_link(self, current: Target) -> None:
        """A link went down again after its try: it stops (its stream goes back), and the
        player says why; nothing tries it again."""
        logger.info("Stopping %s: its link went down again after its try", current.title)
        self.stop_tune()
        self._link_failed(current, DOWN)
        self._gave_up()

    def _live_failure(self) -> tuple[str, Optional[int]]:
        """How the live session's buffer input last failed: (kind, HTTP status)."""
        failure = getattr(self.live, "failure", None)
        found = failure() if callable(failure) else None
        if not isinstance(found, tuple) or len(found) != 2:
            return "", None
        kind, status = found
        return str(kind or ""), status if isinstance(status, int) else None

    def _show_retry(
        self, seconds: float, action: Callable[[], object], *, from_live: bool = False
    ) -> None:
        self._retry_due = time.monotonic() + max(0.0, seconds)
        self._retry_action = action
        self._retry_from_live = from_live
        notice = self._retry_notice()
        if notice is not None and id(notice) not in self._wired_retry:
            self._wired_retry.add(id(notice))
            notice.keepWatching.connect(self.retry_now)
        self._show_retry_text()
        if not self._retry_timer.isActive():
            self._retry_timer.start()

    def _show_retry_text(self) -> None:
        due = self._retry_due
        if self._closed():  # the window closes: its overlays may be gone already
            self._retry_due = self._retry_action = None
            self._retry_timer.stop()
            return
        if due is None:
            self._hide_retry()
            return
        notice = self._retry_notice()
        if notice is not None:
            notice.show_notice(ended_text(due - time.monotonic()))

    def _hide_retry(self) -> None:
        self._retry_due = self._retry_action = None
        self._retry_from_live = False
        self._retry_timer.stop()
        notice = self._retry_notice()
        if notice is not None and not self._closed() and not notice.isHidden():
            notice.hide()

    def take_reopen(self) -> Optional[Reopen]:
        """A swap: the large picture's ended stream on its way to open again goes with its
        player, to the small picture (None: none waits). It no longer opens here."""
        due = self._reopen_due
        if due is None:
            return None
        remaining = max(0, self._reopen_timer.remainingTime())
        self._cancel_reopen()
        self._hide_retry()
        target, url, kind = due
        return Reopen(target, url, kind, remaining, self._reconnects, self._played)

    def adopt_reopen(self, reopen: Reopen) -> None:
        """A swap made the large picture a player whose ended stream was on its way to open
        again (small): it opens here once the rest of its back-off is over."""
        self._reopen_due = (reopen.target, reopen.url, reopen.kind)
        self._reconnects = reopen.count
        self._played = self._played or reopen.played
        self._reopen_timer.start(reopen.remaining_ms)
        self._show_retry(reopen.remaining_ms / 1000, self._reopen_now)

    def retry(self, current: Target, *, relinks: int = 0) -> None:
        """Make `current`'s link again (an STB portal's: its play token may have expired) and
        tune it anew; `relinks`: the links made again in a row after a wait (the revive's)."""
        logger.info("Making the link of %s again after a playback failure", current.title)
        played = current.played or self._played
        self.stop_tune()  # it failed: its slot goes back, also if the new link does not come
        item_provider.sign_in_again(self.catalog, current.item)  # its own portal's session
        self._request(replace(current, retried=True, relinks=relinks, played=played))

    # ---- LiveSession's reports ----------------------------------------------------------------

    def _on_live_start_failed(self, message: str) -> None:
        if not self._closed():
            message = translate(message)
            current = self._current()
            title = current.title if current is not None else ""
            self._error(_named(title, message))

    def _on_live_mode(self, _mode: str) -> None:
        current = self._current()
        if self._closed() or current is None or not current.live:
            return
        self._on_sizes()  # 2.8: played directly, the sizes the channel kept show the ⚙
        if self._transport.isVisible():
            self.show_live_transport(current)  # the time-shift bar, or plain live

    # ---- 2.8: the live ⚙ ----------------------------------------------------------------------
    def sizes(self, target: Target) -> Optional[dict]:
        """The channel's picture sizes when it has two or more: {"heights" (largest first),
        "height" (the playing one's, 0: not named), "max_height" (the cap, 0: none)}."""
        key = self.catchup.key(target)
        found = self._sizes.get(key)
        live = self.live
        if found is None and live is not None and live.channel_key() == key:
            found = live.known_sizes()  # what the channel kept (it plays directly, say)
        if found is None or len(found["heights"]) < 2:
            return None
        cap = self._chosen.get(key, found["max_height"])
        return dict(found, heights=list(found["heights"]), max_height=cap)

    def choose_size(self, target: Target, max_height: int) -> None:
        """The cap chosen for the channel: what its ⚙ ticks from now on, while its old stream
        still reports the one before (until it is parked)."""
        self._chosen[self.catchup.key(target)] = max_height

    def go_back_when_offered(self) -> None:
        """The channel opens again in another size: where the user was is played on at once."""
        self._back_at_once = True

    def _note_sizes(self, current: Target, state: dict) -> None:
        heights = state.get("heights")
        if not isinstance(heights, list):
            return
        self._sizes[self.catchup.key(current)] = {
            "heights": [int(h) for h in heights],
            "height": int(state.get("height") or 0),
            "max_height": int(state.get("max_height") or 0),
        }
        self._on_sizes()

    def _on_live_timeline(self, state: dict) -> None:
        current = self._current()
        if self._closed() or not self._tuner.running() or current is None or not current.live:
            return
        self._note_sizes(current, state)
        # 2.7: the channel's own server keeps more (a provider archive's part is not that)
        if "window_start" in state and not state.get("archive_part"):
            now = self._now().timestamp()
            wall = live_parts.wall_in(state, float(state["window_start"]), now)
            key = self.catchup.key(current)
            self._windows[key] = max(0.0, now - wall)
            self._window_items[key] = current.item
            self._seek_window(state)
        state = self.catchup.extend(state, current)  # the bar reaches into an archive
        transport = self._transport
        if transport.isVisible():
            self._ticks = self._programme_ticks(current.item, state)
        transport.update_timeline(state, self._ticks)

    def _on_archive_changed(self) -> None:
        """The archive's bar moved (it plays, opened, or was sought): the transport follows."""
        current = self._current()
        if self._closed() or current is None or not current.live:
            return
        self._on_sizes()  # 2.8: an archive has no sizes to choose
        state = self.catchup.timeline()
        if state is None:
            return
        transport = self._transport
        if transport.isVisible():
            self._ticks = self._programme_ticks(current.item, state)
        transport.update_timeline(state, self._ticks)

    def _programme_ticks(self, channel: dict, state: dict) -> list[tuple[float, str]]:
        """Programme starts in timeline seconds: the live edge is now (as the transport), and
        a kept buffer's older parts are placed by their own time (2.6; a programme that began
        where nothing was kept has no tick)."""
        stamp = self._now().timestamp()
        oldest = live_parts.wall_in(state, float(state["start"]), stamp)
        since = datetime.fromtimestamp(min(oldest, stamp), timezone.utc)
        ticks = []
        for p in self._programmes(channel, since, TICK_PROGRAMMES):
            t = live_parts.t_in(state, p.start.timestamp(), stamp)
            if t is not None:
                ticks.append((t, p.title))
        return ticks

    def _on_live_notice(self, text: str) -> None:
        if self._closed():
            return
        if text == UNAVAILABLE:
            # Said when it matters, not at every tune: a rewind on a channel that plays
            # directly says it (at each tune the toast also came on load noise on slow channels).
            return
        self._toast(translate(text))  # LiveSession's notices are English (N_) texts

    def on_playing(self) -> None:
        """The player plays: an ended stream counts how long it played since (HEALTHY_S), and
        a wait of ours for it is over (LiveSession's own says when its own is)."""
        self._playing_since = time.monotonic()
        self._played = True
        self._link_well_since = self._playing_since
        if not self._retry_from_live:
            self._hide_retry()

    def _on_live_failed(self, message: str) -> None:
        """Playback of the live channel failed for good (LiveSession recovered what it could)."""
        if self._closed():
            return
        self._hide_retry()  # (a fresh link below may count down again)
        current = self._current()
        if message == STREAM_LIMIT:
            # Never asked again (spec §9), not even with a fresh STB link: the limit is
            # reached, and a retry would only be refused or take another device's slot.
            self.stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            self._explain_failure(current, message)
            self._gave_up()
            return
        if current is not None and self.may_retry(current):
            self.retry(current)  # STB play tokens expire: a fresh link before the error
            return
        if message not in REFUSALS and self.relink(current):
            return  # it keeps failing, but played: a fresh link after the revive's wait
        title = current.title if current is not None else ""
        if message in REFUSALS:
            self.stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            self._explain_failure(current, message)
            self._gave_up()
            return
        self._error(_named(title, translate(message)))  # the player's, or LiveSession's N_
        self._explain_failure(current, message)
        self._gave_up()

    def _explain_failure(self, current: Optional[Target], message: str = "") -> None:
        """Why `current` failed for good: a link says it plainly first (`link_failed`, from
        `message` and how its buffer's input failed), then its provider's account is asked."""
        if current is None:
            return
        if self._link(current.item):
            kind, status = self._live_failure()
            self._link_failed(current, failure_reason(message, kind, status))
        self._explain(current.item, current.title)

    def _refusal_text(self, message: str) -> str:
        current = self._current()
        limit = self.limit(current.item if current is not None else None)
        if message != STREAM_REFUSED:
            return self.limit_text(limit)
        if limit:  # a plain 403: maybe the limit
            return tr_n(
                # i18n: error toast
                "The provider refused the stream — it may allow only {n} stream at a time",
                "The provider refused the stream — it may allow only {n} streams at a time",
                limit,
            )
        return tr("The provider refused the stream — it may limit concurrent streams")

    # ---- recording (2.2) ----------------------------------------------------------------------

    def toggle_recording(self) -> None:
        """R and the record button: record the channel being watched from where it plays, or
        stop recording (the file is kept; its toast follows once it is closed)."""
        live, current = self.live, self._current()
        if live is not None and live.recording():
            live.stop_recording()
            self._show_transport()
            return
        if current is None or not current.live:
            return  # a movie or nothing: there is no live channel to record
        if self.catchup.active():
            self._toast(translate(NO_RECORDING_TEXT))
            return
        lender = getattr(live, "lender_records", None)  # 2.9.7: it records already, on schedule
        if callable(lender) and lender():
            self._toast(translate(ON_SCHEDULE_TEXT))
            return
        if live is None or not live.recordable() or not self.pipeline_live():
            if live is not None and self._tuner.opening():
                self._toast(translate(NOT_YET_TEXT))
            elif self._nothing_plays():
                self._toast(translate(NOTHING_PLAYING))  # it failed: not time-shift's fault
            else:
                self._toast(translate(NO_RECORDING))
            return
        when = self._now().astimezone()
        path = recording.path_for(self.config, current.title, when)
        if live.start_recording(path):
            if self._noted is not None:
                on_now = self._now_title(current.item)
                self._noted(path, current.item, on_now[0] if on_now else "", when.timestamp())
            self._show_transport()

    def _nothing_plays(self) -> bool:
        """The channel's player failed or ended (2.8.12): nothing plays to record."""
        state = getattr(self.session, "state", None)
        return callable(state) and state() in (PlaybackState.ERROR, PlaybackState.ENDED)

    def _on_recording_changed(self, *_args: Any) -> None:
        live = self.live
        self._transport.set_recording(live.recording_since() if live is not None else None)

    # ---- the transport's time-shift controls --------------------------------------------------

    def seek_live(self, back: bool) -> bool:
        """←/→ on a time-shifted channel (or its archive): False when it is not one (the
        caller seeks)."""
        step = -LIVE_SEEK_S if back else LIVE_SEEK_S
        if self.catchup.active():
            self.catchup.seek_relative(step)
            self._show_transport()
            return True
        if not self.pipeline_live() or self.live is None:
            return False
        if not (back and self._rewind_to_archive(step)):
            self.catchup.cancel()  # within the buffer: a probe for the archive is dropped
            self.live.seek_relative(step)
        self._show_transport()
        return True

    def _seek_relative_ms(self, delta_ms: int) -> None:
        seconds = delta_ms / 1000.0
        if self.catchup.active():
            self.catchup.seek_relative(seconds)
        elif self.pipeline_live() and self.live is not None:
            if not (seconds < 0 and self._rewind_to_archive(seconds)):
                self.catchup.cancel()
                self.live.seek_relative(seconds)
        else:
            self.session.seek_relative(delta_ms)

    def _seek_to(self, seconds: float) -> None:
        if self.catchup.active():
            self.catchup.seek_to(seconds)  # the archive's bar is in Unix seconds
        elif self.pipeline_live() and self.live is not None:
            state = self._tuner.timeline
            if state is not None and seconds < _reach(state) - _PAST_BUFFER_S:
                if self._open_archive_at(state, seconds):
                    return
            self.catchup.cancel()  # within the buffer: a probe for the archive is dropped
            self.live.seek_to(seconds)

    def _rewind_to_archive(self, seconds: float) -> bool:
        """A step back from where the buffer plays that lands before its start opens the
        archive there, when the channel has one: True when it did."""
        state = self._tuner.timeline
        if state is None:
            return False
        t = float(state["position"]) + seconds
        return t < _reach(state) - _PAST_BUFFER_S and self._open_archive_at(state, t)

    def _open_archive_at(self, state: dict, t: float) -> bool:
        """The channel's archive at the time-shift timeline's `t` (its live edge is now; 2.6:
        a kept buffer's older parts by their own time)."""
        return self._open_archive_moment(live_parts.wall_in(state, t, self._now().timestamp()))

    def _open_archive_moment(self, moment: float) -> bool:
        """The channel's archive from `moment` (Unix seconds)."""
        current = self._current()
        if current is None or self.catchup.offered(current) is None:
            if self.catchup.known_failed(current):
                self._toast(translate(NOT_AVAILABLE_TEXT))
            return False
        return self.catchup.open(current, moment)

    def _set_speed(self, rate: float) -> None:
        if self.catchup.active():
            self.session.set_rate(rate)
            self.catchup.set_rate(rate)
        elif self.pipeline_live() and self.live is not None:
            self.live.set_speed(rate)
        else:
            self.session.set_rate(rate)

    def go_live(self) -> None:
        current = self._current()
        if self.catchup.active():
            self.catchup.go_live(chosen_now=True)  # the channel tunes again
            self._show_transport()
        elif current is not None and current.live and self._tuner.go_live():
            self.catchup.cancel()  # a probe for the archive is dropped
            self._show_transport()

    def _programme_start(self) -> None:
        """⏮: the start of the programme being watched if buffered, else the oldest point; on
        a channel with an archive, a programme that began before the buffer from the archive
        (and, playing the archive, the programme before when at a programme's start)."""
        current, state = self._current(), self._tuner.timeline
        if self.catchup.active():
            self._archive_programme_start()
            return
        if not self.pipeline_live() or self.live is None or current is None or state is None:
            return
        stamp = self._now().timestamp()
        oldest = _reach(state)  # 2.7: the channel's server window, past the bar too
        position = float(state["position"])
        moment = min(live_parts.wall_in(state, position, stamp), stamp)
        watching = datetime.fromtimestamp(moment, timezone.utc)
        # Without a programme: the buffer's oldest point (the bar's start before it, 2.7).
        buffer_start = float(state.get("buffer_start", state["start"]))
        target = buffer_start if position >= buffer_start else float(state["start"])
        for programme in self._programmes(current.item, watching, 1):
            if not programme.is_on(watching):
                continue
            # Where it began on the timeline (2.6: by its part; None: where nothing was kept).
            begin = programme.start.timestamp()
            start = live_parts.t_in(state, begin, stamp)
            if start is not None and start >= oldest:
                target = start
            elif self._open_archive_moment(begin):
                self._show_transport()
                return
        self.catchup.cancel()
        self.live.seek_to(target)
        self._show_transport()

    def _archive_programme_start(self) -> None:
        current, position = self._current(), self.catchup.position()
        if current is None or position is None:
            return
        watching = datetime.fromtimestamp(position, timezone.utc)
        programme = self._programme_at(current.item, watching)
        if programme is None:
            self.catchup.seek_relative(-LIVE_SEEK_S)
        else:
            if (watching - programme.start).total_seconds() < _AT_START_S:
                earlier = programme.start - timedelta(seconds=1)
                programme = self._programme_at(current.item, earlier) or programme
            self.catchup.seek_to(programme.start.timestamp())
        self._show_transport()

    def _programme_at(self, channel: dict, moment: datetime) -> Optional[Programme]:
        for programme in self._programmes(channel, moment, 1):
            if programme.is_on(moment):
                return programme
        return None

    def show_live_transport(self, target: Target) -> None:
        now = self._now_title(target.item)
        subtitle = now[0] if now else ""
        transport = self._transport
        transport.set_favorite(favorites.is_favorite(self.config, target.item))  # its star
        if self.catchup.active():
            transport.show_timeshift(target.title, subtitle, self._logo(target.item))
            self._on_archive_changed()
            return
        if not self.pipeline_live():
            transport.show_live(target.title, subtitle, self._logo(target.item))
            return
        transport.show_timeshift(target.title, subtitle, self._logo(target.item))
        timeline = self._tuner.timeline
        if timeline is not None:
            timeline = self.catchup.extend(timeline, target)  # into an archive, if any
            self._ticks = self._programme_ticks(target.item, timeline)
            transport.update_timeline(timeline, self._ticks)


def _log_name(item: Optional[dict]) -> str:
    """A channel's name for the log (2.9.7), quoted; never its address."""
    return f"“{str((item or {}).get('name') or '?')[:80]}”"
