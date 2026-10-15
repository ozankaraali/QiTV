"""The live channel's time-shift side of the TV shell (spec §6, §6.9, §9).

Which live links play through the time-shift pipeline, and with which budget slot; the
transport's time-shift controls (seek, speed, Go live, ⏮ to the programme start, the bar's
programme ticks); and LiveSession's reports: its mode and timeline, its notices (time-shift
unavailable, once per channel and run) and its failures. An STB channel that fails gets a
fresh link once per tune (its play token may have expired) before the error shows; a
refusal for the provider's connection limit is never retried and says so.

TVController owns the target and the LiveTuner and hands this unit what it needs. With
picture in picture it is the large picture's: a swap hands it the other player's tuner, live
session and player (`use_slot`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import logging
from typing import Callable, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, SignalInstance
from PySide6.QtGui import QPixmap

from services.epg_view import Programme
from services.live.live_session import UNAVAILABLE, LiveSession
from services.live.source import REFUSALS, STREAM_LIMIT, STREAM_REFUSED
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind
from ui.link_lanes import LinkLanes, Target
from ui.live_tuner import LiveStart, LiveTuner
from ui.overlays.transport import Transport

logger = logging.getLogger(__name__)

LIVE_SEEK_S = 10.0  # ←/→ while time-shifting (spec §6.7)
TICK_PROGRAMMES = 6  # programme starts marked on the time-shift bar
LIMITED_TEXT = "Your provider limits concurrent streams"  # when its limit is not known
REFUSED_TEXT = "The provider refused the stream — it may"  # a plain 403: maybe the limit
ERROR_MS = 6000
_PIPELINE_SCHEMES = ("http", "https")  # what the pipeline's source can read


def _streams(count: int) -> str:
    return f"{count} stream" if count == 1 else f"{count} streams"


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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
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
        self._ticks: list[tuple[float, str]] = []
        self._unavailable_noticed: set[tuple[str, str]] = set()  # (provider, channel) this run
        self._held_notice: Optional[tuple[str, str]] = None  # shown once the channel plays
        self._wired = False

    def wire(self) -> None:
        transport = self._transport
        transport.seekRelativeRequested.connect(self._seek_relative_ms)
        transport.speedRequested.connect(self._set_speed)
        transport.goLiveRequested.connect(self.go_live)
        transport.programmeStartRequested.connect(self._programme_start)
        transport.seekToRequested.connect(self._seek_to)
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
        self.new_target()

    # ---- tuning -------------------------------------------------------------------------------

    def new_target(self) -> None:
        """A new target plays: the old channel's ticks and held notice are gone."""
        self._held_notice = None
        self._ticks = []

    def through_pipeline(self, url: str, kind: PlayKind) -> bool:
        """Whether a live link plays through the time-shift pipeline.

        Its source reads http(s) only; container files listed as channels (M3U) play as files.
        """
        if self.live is None or kind is PlayKind.VOD_FILE:
            return False
        return urlsplit(url).scheme.lower() in _PIPELINE_SCHEMES

    def limit(self) -> Optional[int]:
        """The provider's simultaneous streams: None when unlimited or unknown."""
        return self._tuner.budget.limit_for(
            self.catalog.current_provider(), self.catalog.max_connections()
        )

    def limit_text(self, limit: Optional[int]) -> str:
        return f"Your provider allows {_streams(limit)} at a time" if limit else LIMITED_TEXT

    def tune_pipeline(self, target: Target, url: str) -> bool:
        """Ask for a budget slot for `target` (spec §6.9); it starts once the old one is released.

        False when the provider's limit is taken by other streams (Phase 4: picture in
        picture): the toast says so and the running channel goes on.
        """
        name = self.catalog.current_provider_name()
        limit = self.limit()
        if self._tuner.refused(name, limit):
            logger.info("Not opening %s: the provider allows %s streams", target.title, limit)
            self._restore_channel()
            self._toast(self.limit_text(limit), "error", ERROR_MS)
            return False
        self._tuner.tune(self.live_start(target, url), name, limit)
        return True

    def live_start(self, target: Target, url: str) -> LiveStart:
        """How the pipeline asks for `target`'s link: its agent and the provider's TLS check."""
        provider = self.catalog.current_provider()
        user_agent = target.item.get("user_agent") or DEFAULT_USER_AGENT
        verify_ssl = bool(provider.get("ssl_verify", self.config.ssl_verify))
        return LiveStart(url, user_agent, verify_ssl)

    def pipeline_live(self) -> bool:
        """The current channel plays from the time-shift buffer."""
        current = self._current()
        return current is not None and current.live and self._tuner.pipeline()

    def stop_tune(self) -> None:
        self._tuner.stop()
        self._ticks = []

    def may_retry(self, current: Optional[Target]) -> bool:
        """An STB channel's link may be made again once per tune (its play token expires)."""
        return (
            current is not None
            and current.live
            and not current.retried
            and self.catalog.provider_type() == "STB"
            and not self._lanes.busy()
        )

    def retry(self, current: Target) -> None:
        logger.info("Making the link of %s again after a playback failure", current.title)
        self._held_notice = None
        self.stop_tune()  # it failed: its slot goes back, also if the new link does not come
        self._request(replace(current, retried=True))

    # ---- LiveSession's reports ----------------------------------------------------------------

    def _on_live_start_failed(self, message: str) -> None:
        if not self._closed():
            current = self._current()
            title = current.title if current is not None else ""
            self._error(f"{title}: {message}" if title else message)

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
        if text == UNAVAILABLE:  # once per channel and run
            current = self._current()
            item = current.item if current is not None else {}
            channel = str(item.get("cmd") or item.get("id") or item.get("name") or "")
            key = (self.catalog.current_provider_name(), channel)
            if key in self._unavailable_noticed:
                return
            if self.may_retry(current):
                # An expired STB token fails the pipeline too: say it only once the direct
                # play works, so a link made again does not use up the notice.
                self._held_notice = key
                return
            self._unavailable_noticed.add(key)
        self._toast(text)

    def on_playing(self) -> None:
        """The player plays: a notice held back while a fresh link was made shows now."""
        key, self._held_notice = self._held_notice, None
        if key is not None and key not in self._unavailable_noticed:
            self._unavailable_noticed.add(key)
            self._toast(UNAVAILABLE)

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
            return
        if current is not None and self.may_retry(current):
            self.retry(current)  # STB play tokens expire: a fresh link before the error
            return
        title = current.title if current is not None else ""
        if message in REFUSALS:
            self.stop_tune()  # nothing plays: its slot goes back
            self._toast(self._refusal_text(message), "error", ERROR_MS)
            return
        self._error(f"{title}: {message}" if title else message)

    def _refusal_text(self, message: str) -> str:
        limit = self.limit()
        if message != STREAM_REFUSED:
            return self.limit_text(limit)
        if limit:
            return f"{REFUSED_TEXT} allow only {_streams(limit)} at a time"
        return f"{REFUSED_TEXT} limit concurrent streams"

    # ---- the transport's time-shift controls --------------------------------------------------

    def seek_live(self, back: bool) -> bool:
        """←/→ on a time-shifted channel: False when it is not one (the caller seeks)."""
        if not self.pipeline_live() or self.live is None:
            return False
        self.live.seek_relative(-LIVE_SEEK_S if back else LIVE_SEEK_S)
        self._show_transport()
        return True

    def _seek_relative_ms(self, delta_ms: int) -> None:
        if self.pipeline_live() and self.live is not None:
            self.live.seek_relative(delta_ms / 1000.0)
        else:
            self.session.seek_relative(delta_ms)

    def _seek_to(self, seconds: float) -> None:
        if self.pipeline_live() and self.live is not None:
            self.live.seek_to(seconds)

    def _set_speed(self, rate: float) -> None:
        if self.pipeline_live() and self.live is not None:
            self.live.set_speed(rate)
        else:
            self.session.set_rate(rate)

    def go_live(self) -> None:
        current = self._current()
        if current is not None and current.live and self._tuner.go_live():
            self._show_transport()

    def _programme_start(self) -> None:
        """⏮: the start of the programme being watched if buffered, else the oldest point."""
        current, state = self._current(), self._tuner.timeline
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
        self.live.seek_to(target)
        self._show_transport()

    def show_live_transport(self, target: Target) -> None:
        now = self._now_title(target.item)
        subtitle = now[0] if now else ""
        transport = self._transport
        if not self.pipeline_live():
            transport.show_live(target.title, subtitle, self._logo(target.item))
            return
        transport.show_timeshift(target.title, subtitle, self._logo(target.item))
        timeline = self._tuner.timeline
        if timeline is not None:
            self._ticks = self._programme_ticks(target.item, timeline)
            transport.update_timeline(timeline, self._ticks)
