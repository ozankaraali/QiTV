"""The live channel's time-shift side of the TV shell (spec §6, §6.9, §9).

Which live links play through the time-shift pipeline, and with which budget slot; the
transport's time-shift controls (seek, speed, Go live, ⏮ to the programme start, the bar's
programme ticks); and LiveSession's reports: its mode and timeline, its notices (time-shift
unavailable, once per channel and run) and its failures. An STB channel that fails gets a
fresh link once per tune (its play token may have expired) before the error shows; a
refusal for the provider's connection limit is never retried and says so. A live channel
played directly whose stream ends (an on-demand restream may show a placeholder and end)
opens again in the same player, so in the same budget slot, twice per tune at most, after
RECONNECT_MS: "Reconnecting…", and only then "The stream ended".

Recording (2.3): R and the record button record the time-shifted channel being watched from
where it plays (rewound or not) into the Recordings folder (`services.recording`), or stop
it; the record button is lit and the REC chip counts while the large picture's channel
records. A channel played directly, or one still opening, says why it cannot record.

Provider catch-up (2.3, `ui.catchup_controls.CatchUp`): on a channel with an archive the bar
reaches further back than the buffer; seeking there, stepping back past the buffer's start or
⏮ to a programme that began before it plays the provider's archive, and the transport's
controls then act on it (Go live returns to the live channel).

TVController owns the target and the LiveTuner and hands this unit what it needs. With
picture in picture it is the large picture's: a swap hands it the other player's tuner, live
session and player (`use_slot`); a multiview's focused tile is the large picture.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QTimer, SignalInstance
from PySide6.QtGui import QPixmap

from services import favorites, recording
from services.catchup_memory import CatchupMemory
from services.epg_view import Programme
from services.i18n import N_, tr, tr_n, translate
from services.live.live_session import UNAVAILABLE, LiveSession
from services.live.refusals import REFUSALS, STREAM_LIMIT, STREAM_REFUSED
from services.live.tuning import channel_key
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind
from ui import item_provider
from ui.catchup_controls import NO_RECORDING_TEXT, NOT_AVAILABLE_TEXT, CatchUp
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, LiveTuner
from ui.overlays.transport import NO_RECORDING, Transport

logger = logging.getLogger(__name__)

LIVE_SEEK_S = 10.0  # ←/→ while time-shifting (spec §6.7)
_PAST_BUFFER_S = 1.0  # a point this far before the buffer's start is the archive's
_AT_START_S = 5.0  # ⏮ this near a programme's start goes to the one before (the archive)
TICK_PROGRAMMES = 6  # programme starts marked on the time-shift bar
# When its limit is not known (shown with translate()).
LIMITED_TEXT = N_("Your provider limits concurrent streams")  # i18n: error toast
ERROR_MS = 6000
RECONNECT_MS = (1000, 3000)  # the back-off before each reopen of a live stream that ended
# i18n: toast: a live channel's stream ended and is opened again
RECONNECTING_TEXT = N_("Reconnecting…")
# i18n: a toast: R or the record button was pressed while the channel is still opening
NOT_YET_TEXT = N_("Recording can start once the channel plays")
_PIPELINE_SCHEMES = ("http", "https")  # what the pipeline's source can read
# Never opened again: on-demand content, and a file listed as a channel ends for good.
_NOT_LIVE = (PlayKind.VOD_FILE, PlayKind.VOD_STREAM, PlayKind.LIVE_PIPELINE)


def stream_options(item: dict) -> dict[str, Any]:
    """A channel's own HTTP options for VLC's direct play: its agent, and the referrer its M3U
    names (2.1, `:http-referrer`); an item without one plays exactly as before."""
    options: dict[str, Any] = {"user_agent": item.get("user_agent")}
    if item.get("referrer"):
        options["referrer"] = item["referrer"]
    return options


def _named(title: str, message: str) -> str:
    """A failure after the channel's name, when it has one."""
    # i18n: an error toast: a channel's, a film's or a provider's name, then what went wrong
    return tr("{name}: {message}", name=title, message=message) if title else message


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
        runner: Any = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`explain(item, title)` asks why the failure just toasted happened at the item's
        provider (TVController._explain_item). `may_reopen(target)`: whether an ended
        direct stream may open again (a multiview tile's checks its provider's slot
        first). Catch-up: `memory` keeps whether each channel's archive answered (None: for
        this run only), `prepare(session, target)` readies the player for a channel before
        its archive opens, `archive_allowed()` says whether archives may play now, `runner`
        runs the archive probes (a JobRunner by default)."""
        super().__init__(parent)
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
        self._toast = toast
        self._error = error
        self._request = request
        self._restore_channel = restore_channel
        self._show_transport = show_transport
        self._logo = logo
        self._explain = explain
        self._ticks: list[tuple[float, str]] = []
        self._wired = False
        self._reconnects = 0  # reopens of the current tune's ended stream so far
        self._reopen_due: Optional[tuple[Target, str, PlayKind]] = None  # after the back-off
        self._reopen_timer = QTimer(self, singleShot=True)
        self._reopen_timer.timeout.connect(self._reopen)
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
            runner=runner,
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
        for signal, slot in pairs:
            if connect:
                signal.connect(slot)
            else:
                signal.disconnect(slot)

    def use_slot(self, tuner: LiveTuner, live: Optional[LiveSession], session) -> None:
        """The large picture is another player's now (a PiP swap): act on its stream."""
        if self._wired:
            self._connect(False)
        self._tuner, self.live, self.session = tuner, live, session
        if self._wired:
            self._connect(True)
        self._forget_channel()
        self.catchup.use_slot(tuner, session)  # the new large picture's archive, if any
        self._on_recording_changed()  # the REC chip is the new large picture's

    # ---- tuning -------------------------------------------------------------------------------

    def new_target(self) -> None:
        """A new target plays: the old channel's ticks, reopen and archive are gone."""
        self._forget_channel()
        self.catchup.reset()

    def _forget_channel(self) -> None:
        self._ticks = []
        self._reconnects = 0
        self._cancel_reopen()

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

        False when the provider's limit is taken by other streams (Phase 4: picture in
        picture): the toast says so and the running channel goes on.
        """
        name = item_provider.provider_name(self.catalog, target.item)
        limit = self.limit(target.item)
        if self._tuner.refused(name, limit):
            logger.info("Not opening %s: the provider allows %s streams", target.title, limit)
            self._restore_channel()
            self._toast(self.limit_text(limit), "error", ERROR_MS)
            return False
        self._tuner.tune(self.live_start(target, url), name, limit)
        return True

    def live_start(self, target: Target, url: str) -> LiveStart:
        """How the pipeline asks for `target`'s link: its agent and the provider's TLS check;
        and the channel's key for what it taught stay-live (Auto's target, a locked rate)."""
        provider = item_provider.provider_dict(self.catalog, target.item)
        user_agent = target.item.get("user_agent") or DEFAULT_USER_AGENT
        verify_ssl = bool(provider.get("ssl_verify", self.config.ssl_verify))
        channel = channel_key(str(provider.get("name", "")), target.item)
        referrer = str(target.item.get("referrer") or "")
        return LiveStart(url, user_agent, verify_ssl, channel, referrer)

    def pipeline_live(self) -> bool:
        """The current channel plays from the time-shift buffer."""
        current = self._current()
        return current is not None and current.live and self._tuner.pipeline()

    def stop_tune(self) -> None:
        self._cancel_reopen()
        self.catchup.reset()
        self._tuner.stop()
        self._ticks = []

    def may_retry(self, current: Optional[Target]) -> bool:
        """An STB channel's link may be made again once per tune (its play token expires)."""
        return (
            current is not None
            and current.live
            and not current.retried
            and item_provider.provider_type(self.catalog, current.item) == "STB"
            and not self._lanes.busy()
        )

    def reconnect(self, current: Target) -> bool:
        """`current`'s live stream, played directly, ended: open it again after a back-off.

        The same player opens the same stream (its ended input ends before the new one
        opens), so it keeps its budget slot and the provider never sees two connections.
        False when nothing more is tried: twice per tune, never on-demand content or a file
        listed as a channel; the caller says "The stream ended" then.
        """
        if self._reopen_due is not None:
            return True  # already on its way
        url, kind = self.session.url, self.session.kind
        if not url or kind is None or kind in _NOT_LIVE:
            return False
        if self._reconnects >= len(RECONNECT_MS):
            return False
        self._reopen_due = (current, url, kind)
        self._reopen_timer.start(RECONNECT_MS[self._reconnects])
        self._reconnects += 1
        self._toast(translate(RECONNECTING_TEXT))
        return True

    def _reopen(self) -> None:
        due, self._reopen_due = self._reopen_due, None
        if due is None or self._closed():
            return
        target, url, kind = due
        session = self.session
        if self._current() is not target or session.url != url:
            return  # something else plays now
        if not self._may_reopen(target):
            return  # no stream of its provider to spare (the caller says so)
        logger.info("Opening %s again after its stream ended", target.title)
        session.play(
            url, kind=kind, latency=self.config.live_latency, **stream_options(target.item)
        )

    def _cancel_reopen(self) -> None:
        self._reopen_due = None
        self._reopen_timer.stop()

    def retry(self, current: Target) -> None:
        logger.info("Making the link of %s again after a playback failure", current.title)
        self.stop_tune()  # it failed: its slot goes back, also if the new link does not come
        item_provider.sign_in_again(self.catalog, current.item)  # its own portal's session
        self._request(replace(current, retried=True))

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
        if self._transport.isVisible():
            self.show_live_transport(current)  # the time-shift bar, or plain live

    def _on_live_timeline(self, state: dict) -> None:
        current = self._current()
        if self._closed() or not self._tuner.running() or current is None or not current.live:
            return
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
        state = self.catchup.timeline()
        if state is None:
            return
        transport = self._transport
        if transport.isVisible():
            self._ticks = self._programme_ticks(current.item, state)
        transport.update_timeline(state, self._ticks)

    def _programme_ticks(self, channel: dict, state: dict) -> list[tuple[float, str]]:
        """Programme starts in timeline seconds: the live edge is now (as the transport)."""
        now = self._now()
        edge = float(state["live_edge"])
        since = now - timedelta(seconds=max(0.0, edge - float(state["start"])))
        return [
            (edge - (now - p.start).total_seconds(), p.title)
            for p in self._programmes(channel, since, TICK_PROGRAMMES)
        ]

    def _on_live_notice(self, text: str) -> None:
        if self._closed():
            return
        if text == UNAVAILABLE:
            # Said when it matters, not at every tune: a rewind on a channel that plays
            # directly says it (the owner found the toast on load noise on slow channels).
            return
        self._toast(translate(text))  # LiveSession's notices are English (N_) texts

    def on_playing(self) -> None:
        """The player plays (kept for the controller's call; nothing is held back now)."""

    def _on_live_failed(self, message: str) -> None:
        """Playback of the live channel failed for good (LiveSession recovered what it could)."""
        if self._closed():
            return
        current = self._current()
        if message == STREAM_LIMIT:
            # Never asked again (spec §9), not even with a fresh STB link: the limit is
            # reached, and a retry would only be refused or take another device's slot.
            self.stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            self._explain_failure(current)
            return
        if current is not None and self.may_retry(current):
            self.retry(current)  # STB play tokens expire: a fresh link before the error
            return
        title = current.title if current is not None else ""
        if message in REFUSALS:
            self.stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            self._explain_failure(current)
            return
        message = translate(message)  # the player's, or LiveSession's N_ texts
        self._error(_named(title, message))
        self._explain_failure(current)

    def _explain_failure(self, current: Optional[Target]) -> None:
        if current is not None:
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

    # ---- recording (2.3) ----------------------------------------------------------------------

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
        if live is None or not live.recordable() or not self.pipeline_live():
            opening = live is not None and self._tuner.opening()
            self._toast(translate(NOT_YET_TEXT if opening else NO_RECORDING))
            return
        when = self._now().astimezone()
        if live.start_recording(recording.path_for(self.config, current.title, when)):
            self._show_transport()

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
            self.live.seek_relative(step)
        self._show_transport()
        return True

    def _seek_relative_ms(self, delta_ms: int) -> None:
        seconds = delta_ms / 1000.0
        if self.catchup.active():
            self.catchup.seek_relative(seconds)
        elif self.pipeline_live() and self.live is not None:
            if not (seconds < 0 and self._rewind_to_archive(seconds)):
                self.live.seek_relative(seconds)
        else:
            self.session.seek_relative(delta_ms)

    def _seek_to(self, seconds: float) -> None:
        if self.catchup.active():
            self.catchup.seek_to(seconds)  # the archive's bar is in Unix seconds
        elif self.pipeline_live() and self.live is not None:
            state = self._tuner.timeline
            if state is None or seconds >= float(state["start"]) - _PAST_BUFFER_S:
                self.live.seek_to(seconds)
            elif not self._open_archive_at(state, seconds):
                self.live.seek_to(seconds)

    def _rewind_to_archive(self, seconds: float) -> bool:
        """A step back from where the buffer plays that lands before its start opens the
        archive there, when the channel has one: True when it did."""
        state = self._tuner.timeline
        if state is None:
            return False
        t = float(state["position"]) + seconds
        return t < float(state["start"]) - _PAST_BUFFER_S and self._open_archive_at(state, t)

    def _open_archive_at(self, state: dict, t: float) -> bool:
        """The channel's archive at the time-shift timeline's `t` (its live edge is now)."""
        current = self._current()
        if current is None or self.catchup.offered(current) is None:
            if self.catchup.known_failed(current):
                self._toast(translate(NOT_AVAILABLE_TEXT))
            return False
        moment = self._now().timestamp() - (float(state["live_edge"]) - t)
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
            self.catchup.go_live()  # the channel tunes again
            self._show_transport()
        elif current is not None and current.live and self._tuner.go_live():
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
        now = self._now()
        edge, oldest = float(state["live_edge"]), float(state["start"])
        watching = now - timedelta(seconds=max(0.0, edge - float(state["position"])))
        target = oldest
        for programme in self._programmes(current.item, watching, 1):
            start = edge - (now - programme.start).total_seconds()
            if programme.is_on(watching) and start >= oldest:
                target = start
            elif programme.is_on(watching) and self._open_archive_at(state, start):
                self._show_transport()
                return
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
