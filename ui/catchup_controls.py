"""Provider catch-up on the large picture (2.3): further back than QiTV's buffer, the
provider's archive plays.

A channel whose provider keeps an archive of it (`services.catchup.archive_of`) offers it
(`offered`) unless its archive did not answer lately (`services.catchup_memory`), the
multiview's grid shows or an external player plays the channels. Then:
- the time-shift bar reaches `EXTEND_S` further back than the buffer (`extend`: its start moves
  back, `buffer_start` says where the buffer starts; the transport draws that part dashed);
  a seek there, a step back past the buffer's start (←, ◀10) and ⏮ to a programme that began
  before it open the archive at that moment (`open`);
- the guide's "Watch from the start" opens it at a past programme's start (the controller
  sends that channel's target with `archive_at`).

Opening: the archive's addresses are built for the moment (an Xtream panel's in its time
zone; a playlist's xc channel's in that of an Xtream provider of the user's on its server;
without one a UTC guess, whose answers teach nothing), from the asked moment to now (a
programme: to its end, plus `AFTER_S`), at most `MAX_MINUTES` (the next stretch opens after
it). An address form
that answered within the day opens at once. Else, when the provider leaves a stream to spare
beside the one playing and one more (its limit less what is open is two or more, or it has
none: a closed connection may stay counted for a while), a probe reads its first bytes on a
worker, holding a "probe" slot of the connection budget meanwhile, while the channel plays
on; a form that plays opens, else the toast says why and the channel plays on ("nothing
there" from every form is a strike, `CatchupMemory.strike`: two for different moments make
the archive failed). Go live, and a seek within the buffer, drop a probe still running
(`cancel`: its slot goes back when it ends). Without a stream to spare it opens unprobed;
VLC failing within `QUICK_S` of that open (a timeout, or anything later, teaches nothing)
has the same address probed (`_diagnose`: VLC does not say why) when the provider's limit
leaves a "probe" slot beside the archive's, else the channel goes live: "nothing there" is a
strike, a refusal is not (a 1-stream account may still count the live stream closed a
moment ago), and an archive that plays opens again once.

The archive plays in the large picture's player as an on-demand stream through the tuner's
`open(direct=True)`: the live stream is stopped first and its connection released before
the archive's opens (the budget's slot passes from one to the other), so a 1-stream account
never has both. Go live (L, the chip) tunes the channel again (`request`), which opens its
live stream once the archive's is let go (the tuner stops the archive first, an archive
still waiting to open included). While it plays, the transport shows its own bar
(`timeline`): timeline seconds are Unix seconds, from `EXTEND_S` before the moment asked (at
most the archive's depth) to now; the bubble over the knob is the moment playing. A seek on
it (or ← → ◀10 ⏮) moves within the opened address when VLC can seek it, else opens the
archive anew there; within `LIVE_S` of now it goes live. The archive's own end (its address
covers a stretch) opens the next stretch, or goes live near now. A failure after it played
opens it again once where it was, then goes live.

Each large picture keeps its own: a swap (`use_slot`) shows the other picture's (none for a
live one); the small picture plays on what it had.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.catchup import (
    MAX_MINUTES,
    Answer,
    Archive,
    address_start,
    archive_of,
    archive_urls,
    wall_clock,
)
from services.catchup_memory import CatchupMemory
from services.connection_budget import DENIED
from services.i18n import N_, translate
from services.jobs import JobRunner
from services.live.tuning import channel_key
from services.vlc_profile import DEFAULT_USER_AGENT, PlayKind
from services.workers import ArchiveProbeWorker
from ui import item_provider
from ui.link_lanes import Target

logger = logging.getLogger(__name__)

# The toasts (shown with translate()).
# i18n: a toast: the channel now plays from its provider's archive (catch-up), not live
ARCHIVE_TEXT = N_("From the provider's archive")
# i18n: a toast: the provider's archive of the channel gave nothing; the channel plays on
NO_ANSWER_TEXT = N_("The provider's archive didn't answer")
# i18n: a toast: the provider's archive of this channel failed lately, so it isn't asked
NOT_AVAILABLE_TEXT = N_("This channel's archive isn't available right now")
# i18n: a toast: the provider refused its archive (maybe its limit of streams at a time)
REFUSED_TEXT = N_("The provider refused its archive — it may limit concurrent streams")
# i18n: a toast: R (record) while the channel plays from the provider's archive
NO_RECORDING_TEXT = N_("Can't record from the provider's archive")
EXTEND_S = 30 * 60  # the bar reaches this far back beyond the buffer, or the moment asked
LIVE_S = 30.0  # a seek this near now goes live
MIN_AGE_S = 120.0  # an archive moment is at least this old (newer: the archive may not have it)
EDGE_S = 60.0  # nor older than the archive's depth less this
AFTER_S = 5 * 60.0  # a programme's address runs on this long after its end
QUICK_S = 6.0  # VLC failing within this of its open, unprobed, is the archive's failure
SHORT_S = 30.0  # a stretch that ends sooner than this after its moment has nothing more
NO_LEARN_S = 15 * 60.0  # a failure at a moment newer than this teaches nothing
TICK_MS = 1000  # the bar's refresh while it opens or is paused (positions come while it plays)
PURPOSE = "probe"


@dataclass
class _Ask:
    """An archive asked for: the channel, the moment and what plays it."""

    target: Target
    archive: Archive
    key: str  # the channel's key in the memory
    moment: float  # Unix seconds asked for
    begins: float  # where its addresses start (an Xtream one at the whole minute)
    ends: float  # where its addresses end
    urls: list[str]
    live_playing: bool  # the channel plays live meanwhile (a rewind): it plays on if it fails
    guessed: bool = False  # its addresses' time zone is a UTC guess: their answers teach nothing


@dataclass
class _Playing:
    ask: _Ask
    url: str
    form: int
    probed: bool  # its address answered (a probe, or the memory)
    opened: float  # the clock's when it was opened
    bar_start: float
    played: bool = False
    paused: bool = False
    rate: float = 1.0
    retried: bool = False  # opened again once after a failure


class CatchUp(QObject):
    def __init__(
        self,
        *,
        tuner,
        session,
        catalog,
        config,
        memory: CatchupMemory,
        now: Callable[[], datetime],
        toast: Callable[..., None],
        request: Callable[[Target], None],
        changed: Callable[[], None],
        limit: Callable[[dict], Optional[int]],
        prepare: Callable[[Any, Target], None] = lambda session, target: None,
        allowed: Callable[[], bool] = lambda: True,
        runner: Any = None,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional[QObject] = None,
    ) -> None:
        """`request(target)` plays a target (the channel live again); `changed()` says the
        archive's bar changed (it plays, moved, or ended); `limit(item)` is the item's
        provider's streams at a time (None: no limit); `prepare(session, target)` readies the
        player for the channel (its sound sync) before the archive opens there."""
        super().__init__(parent)
        self._tuner, self._session = tuner, session
        self.catalog = catalog
        self.config = config
        self.memory = memory
        self._now = now
        self._toast = toast
        self._request = request
        self._changed = changed
        self._limit = limit
        self._prepare = prepare
        self._allowed = allowed
        self._runner = runner if runner is not None else JobRunner(self)
        self._clock = clock
        self._slots: dict[int, _Playing] = {}  # by tuner: each picture's archive
        self._generation = 0  # asks and probes before a newer one are dropped
        self._probing = False
        self._asked: Optional[_Ask] = None  # the newest ask a running probe answers
        self._key: tuple[Optional[dict], str, str] = (None, "", "")  # item, provider, its key
        self._timer = QTimer(self, interval=TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._connect(session, True)

    # ---- the large picture ----------------------------------------------------------------

    def _connect(self, session, connect: bool) -> None:
        signal = getattr(session, "positionChanged", None)
        if signal is None:
            return
        if connect:
            signal.connect(self._on_position)
        else:
            signal.disconnect(self._on_position)

    def use_slot(self, tuner, session) -> None:
        """The large picture is another player's now (a swap): its archive, if it has one."""
        if session is not self._session:
            self._connect(self._session, False)
            self._connect(session, True)
        self._tuner, self._session = tuner, session
        self._generation += 1  # a probe for the picture that went small is dropped
        self._probing, self._asked = False, None
        self._run_timer()

    @property
    def _playing(self) -> Optional[_Playing]:
        return self._slots.get(id(self._tuner))

    def _set_playing(self, playing: Optional[_Playing]) -> None:
        if playing is None:
            self._slots.pop(id(self._tuner), None)
        else:
            self._slots[id(self._tuner)] = playing
        self._run_timer()

    def active(self) -> bool:
        """The large picture plays a channel's archive (or opens it)."""
        return self._playing is not None

    def probing(self) -> bool:
        return self._probing

    def reset(self) -> None:
        """Another title plays large (a zap, a film): its archive and a probe are forgotten."""
        self._generation += 1
        self._probing, self._asked = False, None
        self._set_playing(None)

    # ---- what a channel offers ------------------------------------------------------------

    def key(self, target: Target) -> str:
        """The channel's key in the memory (the last one is kept: the bar asks at each tick)."""
        provider = item_provider.provider_name(self.catalog, target.item)
        item, name, key = self._key
        if item is target.item and name == provider:
            return key
        key = channel_key(provider, target.item)
        self._key = (target.item, provider, key)
        return key

    def cancel(self) -> None:
        """A probe still running is dropped (Go live, a seek within the buffer); its slot
        goes back when it ends."""
        if self._probing:
            self._generation += 1
            self._probing, self._asked = False, None

    def offered(self, target: Optional[Target]) -> Optional[Archive]:
        """The channel's archive when it may be played now; else None."""
        if target is None or not target.live or not self._allowed():
            return None
        archive = archive_of(target.item)
        if archive is None or self.memory.failed(self.key(target)):
            return None
        return archive

    def known_failed(self, target: Optional[Target]) -> bool:
        """The channel claims an archive that did not answer lately."""
        return (
            target is not None
            and archive_of(target.item) is not None
            and self.memory.failed(self.key(target))
        )

    def extend(self, state: dict, target: Optional[Target]) -> dict:
        """A time-shift state whose bar reaches into the channel's archive (see the module
        docstring); the state as it is when the archive is not offered."""
        archive = self.offered(target)
        if archive is None:
            return state
        start, edge = float(state["start"]), float(state["live_edge"])
        now = self._now().timestamp()
        ring_wall = now - (edge - start)
        bar_wall = max(ring_wall - EXTEND_S, now - archive.seconds + EDGE_S)
        if bar_wall >= ring_wall - 1.0:
            return state
        return dict(state, start=edge - (now - bar_wall), buffer_start=start)

    # ---- opening --------------------------------------------------------------------------

    def open(
        self,
        target: Target,
        moment: float,
        *,
        until: Optional[float] = None,
        link: str = "",
        live_playing: bool = True,
    ) -> bool:
        """Play `target`'s archive from `moment` (Unix seconds; to `until`, a programme's end):
        False, doing nothing, when it has none to offer or its addresses can't be made. A
        moment newer than `MIN_AGE_S` ago plays from then (the archive may not have it
        yet), one older than its depth from its oldest."""
        archive = self.offered(target)
        if archive is None:
            return False
        item = target.item
        if archive.kind in ("default", "append") and not archive.source:
            source = item_provider.playlist_source(self.catalog, item)  # its #EXTM3U line's
            archive = replace(archive, source=source)
        now = self._now().timestamp()
        moment = min(max(moment, now - archive.seconds + EDGE_S), now - MIN_AGE_S)
        when = datetime.fromtimestamp(moment, timezone.utc)
        cmd = str(item.get("cmd") or link)
        begins = address_start(archive, when, cmd).timestamp()
        ends = now if until is None or until <= moment else min(until + AFTER_S, now)
        ends = min(max(ends, moment + 60.0), begins + MAX_MINUTES * 60.0)
        zone = item_provider.archive_zone(self.catalog, item)
        if zone is None and archive.kind in ("xc", "auto"):
            zone = item_provider.xtream_zone_at(self.catalog, cmd)  # an Xtream provider's
        guessed = zone is None and wall_clock(archive, cmd)
        provider = item_provider.provider_dict(self.catalog, item)
        urls = archive_urls(
            archive,
            cmd,
            datetime.fromtimestamp(begins, timezone.utc),
            ends - begins,
            self._now(),
            zone=zone or timezone.utc,
            username=str(provider.get("username") or ""),
            password=str(provider.get("password") or ""),
        )
        if not urls:
            logger.info("No archive address for %s", target.title)
            return False
        key = self.key(target)
        ask = _Ask(target, archive, key, moment, begins, ends, urls, live_playing, guessed)
        asked = self._asked
        if self._probing and asked is not None and asked.key == key:
            # A probe of this channel's archive runs: its answer serves this newer ask (← held
            # down opens one archive, not one per press).
            self._asked = replace(ask, live_playing=asked.live_playing and live_playing)
            return True
        self._generation += 1
        self._probing = False
        form = self.memory.answered(key)
        if form is not None and form < len(urls):
            self._play(ask, form, probed=True)
        elif not self._probe(ask):
            self._play(ask, 0, probed=False)
        return True

    def _probe_room(self, target: Target) -> Optional[str]:
        """The provider's name when a probe may open a connection beside what plays."""
        name = item_provider.provider_name(self.catalog, target.item)
        limit = self._limit(target.item)
        if limit is None:
            return name
        return name if limit - self._tuner.budget.in_use(name) >= 2 else None

    def _probe(self, ask: _Ask) -> bool:
        """Probe the addresses on a worker beside what plays; False when there is no room."""
        name = self._probe_room(ask.target)
        if name is None:
            return False
        budget = self._tuner.budget
        ticket = budget.request(name, self._limit(ask.target.item), purpose=PURPOSE, queue=False)
        if ticket == DENIED:
            return False
        self._probing, self._asked = True, ask
        worker = ArchiveProbeWorker(ask.urls, self._headers(ask.target), self._verify(ask.target))
        context = (self._generation, ticket, list(range(len(ask.urls))))
        self._runner.run_worker(worker, self._on_probed, self._on_probe_failed, context=context)
        return True

    def _headers(self, target: Target) -> dict[str, str]:
        headers = {"User-Agent": target.item.get("user_agent") or DEFAULT_USER_AGENT}
        if target.item.get("referrer"):
            headers["Referer"] = str(target.item["referrer"])
        return headers

    def _verify(self, target: Target) -> bool:
        provider = item_provider.provider_dict(self.catalog, target.item)
        return bool(provider.get("ssl_verify", getattr(self.config, "ssl_verify", True)))

    def _probed(self, context: tuple[int, int, list[int]]) -> Optional[_Ask]:
        """A probe ended: its slot goes back (its connection is closed by now); the ask it
        answers, unless a zap, a swap or another channel's ask came meanwhile."""
        generation, ticket, _forms = context
        self._tuner.budget.release(ticket)
        if generation != self._generation or self._asked is None:
            return None
        ask, self._asked = self._asked, None
        self._probing = False
        return ask

    def _on_probed(self, context: tuple[int, int, list[int]], result: Any) -> None:
        ask = self._probed(context)
        if ask is None:
            return
        forms = context[2]  # the probed addresses' forms, in the order probed
        index, answer = result if isinstance(result, tuple) else (-1, Answer.UNKNOWN.value)
        if isinstance(index, int) and 0 <= index < len(forms):
            self.memory.remember(ask.key, True, forms[index])
            self._play(ask, forms[index], probed=True)
            return
        if answer == Answer.NONE.value:
            self._learn_failure(ask)  # every form had nothing (a guessed zone's teaches nothing)
            self._failed(ask, NO_ANSWER_TEXT)
        elif answer == Answer.REFUSED.value:
            self._failed(ask, REFUSED_TEXT)
        else:
            self._failed(ask, NO_ANSWER_TEXT)

    def _on_probe_failed(self, context: tuple[int, int, list[int]], _message: str) -> None:
        ask = self._probed(context)
        if ask is not None:
            self._failed(ask, NO_ANSWER_TEXT)

    def _learn_failure(self, ask: _Ask) -> None:
        """Every form had nothing for the ask's moment: a strike (two make it failed), unless
        the moment is too recent for the archive to have it, or its time zone was a guess."""
        if ask.guessed or self._now().timestamp() - ask.begins < NO_LEARN_S:
            return
        if self.memory.strike(ask.key, ask.moment):
            logger.info(
                "The archive of %s did not answer twice: not asked for a while", ask.target.title
            )

    def _failed(self, ask: _Ask, text: str) -> None:
        """The archive did not open: say why; the channel plays live (it does, or it opens)."""
        self._toast(translate(text))
        if self._playing is not None:
            self.go_live()  # an archive opened before (it failed, or it plays an older stretch)
        elif not ask.live_playing:
            self._request(replace(ask.target, archive_at=None, archive_until=None, zapped=False))

    def _play(self, ask: _Ask, form: int, *, probed: bool) -> None:
        """Open the archive in the large picture once the stream before it is let go."""
        url = ask.urls[form]
        target, session = ask.target, self._session
        offset_ms = int(round((ask.moment - ask.begins) * 1000))
        item = target.item

        def play() -> None:
            self._prepare(session, target)
            session.play(
                url,
                kind=PlayKind.VOD_STREAM,
                start_ms=offset_ms or None,
                user_agent=item.get("user_agent") or DEFAULT_USER_AGENT,
                referrer=item.get("referrer") or None,
            )
            session.set_rate(1.0)

        now = self._now().timestamp()
        bar_start = max(ask.moment - EXTEND_S, now - ask.archive.seconds + EDGE_S)
        old = self._playing
        self._set_playing(
            _Playing(ask, url, form, probed, self._clock(), min(bar_start, ask.moment))
        )
        name = item_provider.provider_name(self.catalog, item)
        logger.info("Opening the archive of %s", target.title)
        self._tuner.open(play, direct=True, provider=name)
        if old is None:
            self._toast(translate(ARCHIVE_TEXT))
        self._changed()

    # ---- while it plays -------------------------------------------------------------------

    def position(self) -> Optional[float]:
        """The moment playing (Unix seconds); None without an archive."""
        playing = self._playing
        if playing is None:
            return None
        if not playing.played:
            return playing.ask.moment
        ms = self._session.time_ms() if hasattr(self._session, "time_ms") else 0
        return playing.ask.begins + max(0, ms) / 1000.0

    def timeline(self) -> Optional[dict]:
        """The archive's bar as a time-shift state, in Unix seconds (None without one)."""
        playing = self._playing
        position = self.position()
        if playing is None or position is None:
            return None
        now = self._now().timestamp()
        position = min(position, now)
        return {
            "start": min(playing.bar_start, position),
            "end": now,
            "position": position,
            "live_edge": now,
            "lag": now - position,
            "shown": now - position,
            "mode": "shifted",
            "rate": playing.rate,
            "nudge": 1.0,
            "paused": playing.paused,
            "target": 0.0,
            "archive": True,
        }

    def seek_to(self, moment: float) -> None:
        """Play the archive from `moment` (Unix seconds): within the opened address when VLC
        can seek it, else opened anew; near now, live."""
        playing = self._playing
        if playing is None:
            return
        now = self._now().timestamp()
        if moment >= now - LIVE_S:
            self.go_live()
            return
        ask = playing.ask
        moment = max(moment, now - ask.archive.seconds + EDGE_S)
        session = self._session
        if playing.played and ask.begins <= moment < ask.ends - 5.0 and session.is_seekable():
            session.seek(int((moment - ask.begins) * 1000))
            self._changed()
            return
        self._reopen(playing, moment)

    def seek_relative(self, seconds: float) -> None:
        position = self.position()
        if position is not None:
            self.seek_to(position + seconds)

    def set_rate(self, rate: float) -> None:
        playing = self._playing
        if playing is not None:
            playing.rate = rate
            self._changed()

    def go_live(self) -> None:
        """Back to the channel live: tuned again, its live stream opens once the archive's is
        let go."""
        playing = self._playing
        if playing is None:
            return
        self._generation += 1
        self._probing, self._asked = False, None
        self._set_playing(None)
        target = playing.ask.target
        logger.info("Back to live on %s", target.title)
        # The archive stops now, one still waiting for the live stream's release included: a
        # slow live link can't let it open meanwhile.
        self._tuner.stop()
        self._request(replace(target, archive_at=None, archive_until=None, zapped=False))

    def _reopen(self, playing: _Playing, moment: float) -> None:
        ask = playing.ask
        until = None
        if ask.ends < self._now().timestamp() - LIVE_S:
            until = ask.ends - AFTER_S  # a programme's stretch: the next one to its end
        if not self.open(ask.target, moment, until=until, live_playing=False):
            self.go_live()

    # ---- the player's reports (the controller asks first) ---------------------------------

    def on_state(self, state: str) -> None:
        playing = self._playing
        if playing is None:
            return
        if state in ("playing", "paused"):
            if not playing.played and not playing.probed:
                self.memory.remember(playing.ask.key, True, playing.form)
            playing.played = True
            playing.paused = state == "paused"
            self._changed()

    def on_error(self, _message: str) -> bool:
        """The archive's player failed: True when that was the archive's (handled here)."""
        playing = self._playing
        if playing is None:
            return False
        if playing.played and not playing.retried:
            self._again(playing)
            return True
        quick = self._clock() - playing.opened <= QUICK_S
        if not playing.played and not playing.probed and quick:
            self._diagnose(playing)  # what the provider says, now that VLC let go
            return True
        self._toast(translate(NO_ANSWER_TEXT))
        self.go_live()
        return True

    def _diagnose(self, playing: _Playing) -> None:
        """VLC could not open an archive it opened unprobed, and does not say why: its address
        is probed on a "probe" slot when the provider's limit leaves one beside the archive's
        (still held), else the channel goes live and nothing is learned."""
        target = playing.ask.target
        name = item_provider.provider_name(self.catalog, target.item)
        limit = self._limit(target.item)
        ticket = self._tuner.budget.request(name, limit, purpose=PURPOSE, queue=False)
        if ticket == DENIED:
            self._toast(translate(NO_ANSWER_TEXT))
            self.go_live()
            return
        self._generation += 1
        self._probing, self._asked = True, playing.ask
        worker = ArchiveProbeWorker([playing.url], self._headers(target), self._verify(target))
        context = (self._generation, ticket, [playing.form])
        self._runner.run_worker(worker, self._on_probed, self._on_probe_failed, context=context)

    def on_ended(self) -> bool:
        """The archive's stretch ended: the next one opens, or live near now. True when it
        was the archive's."""
        playing = self._playing
        if playing is None:
            return False
        if not playing.played:
            return self.on_error("")
        position = self.position() or playing.ask.ends
        if position >= self._now().timestamp() - LIVE_S - 60.0:
            self.go_live()
        elif position - playing.ask.moment < SHORT_S:
            self._toast(translate(NO_ANSWER_TEXT))  # it ended at once: no stretch to go on with
            self.go_live()
        else:
            self._reopen(playing, position)
        return True

    def _again(self, playing: _Playing) -> None:
        position = self.position() or playing.ask.moment
        self._reopen(playing, position)
        again = self._playing
        if again is not None:
            again.retried = True

    def _on_position(self, *_args: Any) -> None:
        if self._playing is not None:
            self._changed()

    def _run_timer(self) -> None:
        if self._playing is not None:
            self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        playing = self._playing
        if playing is not None and (playing.paused or not playing.played):
            self._changed()  # now moves on: the lag grows
