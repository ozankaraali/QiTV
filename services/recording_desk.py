"""The Library's Recordings page (2.9.7), its content from plain data (no widgets).

The page lists, in sections:
- NOW: what records now (a player's recording, a scheduled one) and the scheduled recordings
  that are due but wait for a free stream;
- COMING: the schedule: one-time programmes, timers and series, soonest first, each with its
  next airing and a warning when it clashes (`clashes`);
- ATTENTION: the recordings that were missed or failed, with why and what helps
  (`problem_reason`), until they are dismissed;
- RECORDED: the recordings' files, newest first;
- PLACES: the folders recordings are listed from: the one new recordings go to and the others
  added, each with its disk's free space.

`ui.recordings_desk` gathers the data (the scheduler, the players, the providers' streams, the
folders) and `ui.library.recordings_view` paints the entries. The texts are the active
language's.

A clash: the recordings due at once on one provider need more streams than it allows. Two on
one channel that meet or overlap share a stream (the scheduler hands one over to the next, and
records one within a longer one in its file), so they count once. Airings are looked for
DAYS ahead; a series' come from its channel's guide, so only as far as the guide goes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import os
from typing import Callable, Iterable, Mapping, Optional, Sequence

from services import recording_schedule as rs
from services.epg_view import Programme
from services.i18n import N_, number, tr_n, translate
from services.recordings_list import short_when, size_text, timer_text

NOW, COMING, ATTENTION, RECORDED, PLACES = "now", "coming", "attention", "recorded", "places"
RULES = "rules"  # 2.9.7: the series and the timers, each once
SETTINGS = PLACES  # 2.9.7: the folders and the streams (the places' entries)
SECTIONS = (NOW, COMING, RULES, ATTENTION, RECORDED, PLACES)
ALL = "all"  # the page's first chip: every section
DAYS = 7  # how far ahead airings are looked for (clashes, a timer's next)
LOW_SPACE = 4 * 1024**3  # less free on a place's disk: said (the recorder stops under 2 GiB)
SECTION_TITLES = {
    # i18n: a section of the Recordings page: what records at this moment
    NOW: N_("Recording now"),
    # i18n: a section of the Recordings page: the scheduled recordings, soonest first
    COMING: N_("Coming up"),
    # i18n: a Recordings tab row: the repeating recordings (every airing, timers on days)
    RULES: N_("Series & timers"),
    # i18n: a section of the Recordings page: recordings that were missed or failed
    ATTENTION: N_("Needs attention"),
    # i18n: a section of the Recordings page: the recordings' files
    RECORDED: N_("Recorded"),
    # i18n: a section of the Recordings page: the folders recordings are kept in
    PLACES: N_("Folders & streams"),
}
# i18n: the Recordings page's first chip: every section at once
ALL_TEXT = N_("All")

# The buttons' names (what the page's controller does for each).
WATCH, STOP, TRY_NOW, SKIP = "watch", "stop", "try_now", "skip"
EDIT, CANCEL = "edit", "cancel"
DISMISS, DISMISS_ALL = "dismiss", "dismiss_all"
PLAY, FOLDER, DELETE = "play", "folder", "delete"
OPEN, DEFAULT, REMOVE = "open", "default", "remove"
ADD_TIMER, OPEN_GUIDE, ADD_PLACE = "add_timer", "open_guide", "add_place"
REMOVE_RULE = "remove_rule"  # 2.9.7: a series or a timer goes off the schedule
ACCENT, RAISED, DANGER = "accent", "raised", "danger"  # a button's look
# A status pill's (and a note's) tone.
REC, OK, WARN, ERROR, QUIET = "rec", "ok", "warn", "error", "quiet"

BUTTON_TEXTS = {
    # i18n: a Recordings page button: the recording channel plays (verb)
    WATCH: N_("Watch"),
    # i18n: a Recordings page button: the recording stops, what it recorded is kept (verb)
    STOP: N_("Stop"),
    # i18n: a Recordings page button: a recording waiting for a free stream tries at once
    TRY_NOW: N_("Try now"),
    # i18n: a Recordings page button: this airing is not recorded (a repeating one goes on)
    SKIP: N_("Skip"),
    # i18n: a Recordings page button: change a scheduled recording's time, days or name
    EDIT: N_("Edit…"),
    # i18n: a Recordings page button: the scheduled recording is taken off the schedule
    CANCEL: N_("Cancel Recording"),
    # i18n: a Recordings page button: the problem is no longer listed (verb)
    DISMISS: N_("Dismiss"),
    # i18n: a Recordings page button: every problem listed goes from the list
    DISMISS_ALL: N_("Dismiss all"),
    # i18n: a Recordings page button: the recording plays (verb)
    PLAY: N_("Play"),
    # i18n: a Recordings page button: opens the folder that holds the recording's file
    FOLDER: N_("Show in Folder"),
    # i18n: a Recordings page button: deletes the recording's file (asks first)
    DELETE: N_("Delete…"),
    # i18n: a Recordings page button: the folder opens in the system's file manager (verb)
    OPEN: N_("Open"),
    # i18n: a Recordings page button: new recordings go to this folder from now on
    DEFAULT: N_("Use for new recordings"),
    # i18n: a Recordings page button: the folder is listed no more (its files stay on disk)
    REMOVE: N_("Remove from list"),
    # i18n: a Recordings page button: a channel recorded at a time of the user's choosing
    ADD_TIMER: N_("Record at a time…"),
    # i18n: a Recordings page button: the programme guide opens, to record from it
    OPEN_GUIDE: N_("Record from the guide"),
    # i18n: a Recordings page button: another folder recordings are listed from (a picker)
    ADD_PLACE: N_("Add a place…"),
    # i18n: a Recordings tab button: a series or a timer is taken off the schedule (verb)
    REMOVE_RULE: N_("Remove"),
}
STATUS_TEXTS = {
    # i18n: a Recordings page status pill: it records now (as the REC sign)
    "rec": N_("REC"),
    # i18n: a Recordings page status pill: due, but no stream of its provider is free yet
    "waiting": N_("Waiting"),
    # i18n: a Recordings page status pill: a programme recorded once
    "once": N_("Once"),
    # i18n: a Recordings page status pill: a channel recorded at a time on chosen days
    "repeats": N_("Repeats"),
    # i18n: a Recordings page status pill: every airing of a programme is recorded
    "series": N_("Series"),
    # i18n: a Recordings page status pill: the recording was missed
    "missed": N_("Missed"),
    # i18n: a Recordings page status pill: the recording failed
    "failed": N_("Failed"),
    # i18n: a Recordings page status pill: a recording's file still being written
    "writing": N_("Writing"),
    # i18n: a Recordings page status pill: the folder new recordings go to
    "default": N_("New recordings"),
}
# i18n: a recording's line on the Recordings page: a player records the channel it shows
WATCHING_TEXT = N_("Recording the channel you're watching")
# i18n: a recording's line on the Recordings page: a player records it out of sight
BACKGROUND_TEXT = N_("Recording in the background")
# i18n: a recording's line on the Recordings page: scheduled ({until}: when it ends, 21:00)
SCHEDULED_UNTIL = N_("Scheduled · until {until}")
# i18n: on the Recordings page: a due recording waits for a stream ({provider}: whose)
WAITING_TEXT = N_("No stream of {provider} is free. QiTV tries again every minute until {until}.")
# i18n: on the Recordings page: a due recording could not start ({why}: the reason) and is
# tried again ({until}: when its time ends, 21:00)
RETRYING_TEXT = N_("Couldn't start: {why}. QiTV tries again every minute until {until}.")
# i18n: a scheduled recording's next airing on the Recordings page ({when}: "Tue 5 Nov 21:00")
NEXT_TEXT = N_("Next: {when}")
# i18n: on the Recordings page: a scheduled recording records now ({until}: when it ends)
ON_NOW_TEXT = N_("Recording now, until {until}")
# i18n: on the Recordings page: a series' channel's guide lists no airing of it yet
NO_AIRING_TEXT = N_("No airing in the guide yet")
# i18n: a series recording's line on the Recordings page ({channel}: its channel)
EVERY_AIRING_TEXT = N_("Every airing on {channel}")
# i18n: a clash on the Recordings page: more recordings at once than the provider's streams
# ({when}: "Tue 5 Nov 21:00", {n}: recordings, {provider}: its name, {limit}: its streams)
CLASH_TEXT = N_(
    "Clashes at {when}: {n} recordings, but {provider} allows {limit} at once. "
    "The longest records first."
)
# i18n: why a recording was missed, on the Recordings page: QiTV was not running
CLOSED_TEXT = N_("QiTV was closed. It records only while it runs.")
# i18n: why a recording was missed, on the Recordings page: the computer slept
ASLEEP_TEXT = N_(
    "The computer was asleep. QiTV keeps it awake while it runs, but not with the lid closed."
)
# i18n: why a recording was missed, on the Recordings page: every stream was in use
NO_STREAM_TEXT = N_(
    "No stream of its provider was free. Stop what else plays then, or raise Max simultaneous "
    "streams in Settings → Providers if your plan allows more."
)
# i18n: on the Recordings page: what failed ({why}: the reason), then what was kept
FAILED_KEPT = N_("{why}. What was recorded before is kept.")
# i18n: a place's line on the Recordings page ({free}: "120 GB")
FREE_TEXT = N_("{free} free")
# i18n: on the Recordings page: a place's disk is almost full ({free}: what is left)
LOW_SPACE_TEXT = N_("Almost full: {free} left. Recording stops when less than 2 GB is free.")
# i18n: on the Recordings page: a place that can't be read (a drive not connected)
UNREACHABLE_TEXT = N_("Can't be reached. Is its drive connected?")
# i18n: a place's line on the Recordings page: where QiTV puts recordings by default
DEFAULT_PLACE_TEXT = N_("QiTV's own folder")
# i18n: what a provider's stream is used for, on the Recordings page
USE_TEXTS = {
    "watch": N_("Watching"),  # i18n: what a provider's stream is used for (the big picture)
    "pip": N_("Picture in picture"),  # i18n: what a provider's stream is used for
    "tile": N_("Multiview"),  # i18n: what a provider's stream is used for (a tile)
    "background": N_("Recording"),  # i18n: what a provider's stream is used for
    "probe": N_("Checking the archive"),  # i18n: a stream used to look into a catch-up archive
    "other": N_("In use"),  # i18n: a provider's stream is used for something else
}


@dataclass(frozen=True)
class Button:
    name: str
    label: str
    style: str = RAISED
    enabled: bool = True


@dataclass(frozen=True)
class Entry:
    """One row of the page: `key` names it to the controller ("rec:<name>", "item:<id>",
    "out:<at>", "file:<path>", "place:<path>", "add:<section>" …)."""

    key: str
    section: str
    title: str
    detail: str = ""
    note: str = ""
    tone: str = QUIET  # the status pill's and the note's
    status: str = ""
    buttons: tuple[Button, ...] = ()
    since: Optional[float] = None  # a recording's start (time.monotonic): the page counts on
    sort: float = 0.0


@dataclass(frozen=True)
class Recording:
    """What records now: `name` (its REC sign name: a player's slot, or a scheduled one's),
    `role` ("large", "small", "background", "tile" or "scheduled")."""

    name: str
    channel: str
    since: float
    role: str
    title: str = ""
    until: float = 0.0  # a scheduled one's end (unix)


@dataclass(frozen=True)
class Waiting:
    """A scheduled recording due now, waiting for one of its provider's streams."""

    key: str  # its job's key
    item_id: str
    title: str
    channel: str
    provider: str
    until: float  # its window's end (unix): tried until then
    reason: str = ""  # why its last try failed, translated ("": no stream was free)


@dataclass(frozen=True)
class Place:
    path: str
    default: bool  # new recordings go here
    builtin: bool = False  # QiTV's own folder (the system's Movies or Videos)
    free: Optional[int] = None  # bytes free on its disk; None: not known (yet)
    count: int = 0
    size: int = 0
    reachable: bool = True


@dataclass(frozen=True)
class Plan:
    """A recording's window on a provider's stream (unix seconds, the margins included)."""

    item_id: str
    title: str
    provider: str
    channel: dict
    start: float
    stop: float


@dataclass(frozen=True)
class Clash:
    at: float  # when the most recordings run at once
    count: int
    limit: int
    provider: str


@dataclass(frozen=True)
class Use:
    kind: str  # "watch", "pip", "tile", "background", "scheduled", "probe", "other"
    label: str
    recording: bool = False


@dataclass(frozen=True)
class Streams:
    """A provider's streams: its limit (None: not known), and what holds each."""

    provider: str
    limit: Optional[int]
    uses: tuple[Use, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Next:
    """2.9.7: the next scheduled airing (the Recordings home's "Next" card)."""

    item_id: str
    title: str
    channel: str
    provider: str
    start: float
    stop: float
    kind: str


@dataclass(frozen=True)
class Hero:
    """2.9.7: the Recordings home's top row: what records now, what is next, the streams of the
    providers with a limit or a stream in use."""

    now: tuple[Recording, ...]
    next: Optional[Next]
    streams: tuple[Streams, ...]


def next_airing(
    items: Iterable[Mapping],
    now: float,
    *,
    zone: Optional[tzinfo] = None,
    guide: Optional[Callable[[dict, float], Sequence[Programme]]] = None,
    skipped: Callable[[str, float], bool] = lambda item_id, start: False,
    recording_ids: Iterable[str] = (),
) -> Optional[Next]:
    """The soonest airing still to start (not skipped, not one recording now)."""
    recording = set(recording_ids)
    best: Optional[Next] = None
    for item in items:
        item_id = str(item.get("id") or "")
        for start, stop in airings(item, now, zone=zone, guide=guide):
            if start - rs.PRE_S <= now or skipped(item_id, start):
                continue
            if item_id in recording and start <= now:
                continue
            if best is None or start < best.start:
                best = Next(
                    item_id,
                    _title(item),
                    str((item.get("channel") or {}).get("name") or ""),
                    str(item.get("provider") or ""),
                    start,
                    stop,
                    str(item.get("kind") or ""),
                )
            break
    return best


def button(name: str, style: str = RAISED, enabled: bool = True) -> Button:
    return Button(name, translate(BUTTON_TEXTS[name]), style, enabled)


def status(name: str) -> str:
    return translate(STATUS_TEXTS[name])


def clock(moment: float) -> str:
    """A time of day, 24-hour ("21:00"), by this computer's clock."""
    return datetime.fromtimestamp(moment).strftime("%H:%M")


# ---- airings and clashes ---------------------------------------------------------------------


def airings(
    item: Mapping,
    now: float,
    *,
    days: int = DAYS,
    zone: Optional[tzinfo] = None,
    guide: Optional[Callable[[dict, float], Sequence[Programme]]] = None,
) -> list[tuple[float, float]]:
    """The programme times (start, stop; unix) `item` records from `now` on, for `days`: those
    whose window has not ended. A series' come from `guide(channel, since)` (none without)."""
    horizon = now + days * 86400
    found: list[tuple[float, float]] = []
    kind = item.get("kind")
    if kind == rs.ONCE:
        found = [(float(item.get("start") or 0), float(item.get("stop") or 0))]
    elif kind == rs.TIMER:
        today = datetime.fromtimestamp(now, zone).date()
        begin, minutes = int(item.get("begin") or 0), int(item.get("minutes") or 0)
        weekdays = set(item.get("days") or [])
        for offset in range(-1, days + 1):
            day = today + timedelta(days=offset)
            if day.weekday() in weekdays:
                start = rs.local_time(day, begin, zone).timestamp()
                stop = rs.local_time(day, begin + minutes, zone).timestamp()
                found.append((start, stop))
    elif kind == rs.SERIES and guide is not None:
        title = " ".join(str(item.get("title") or "").casefold().split())
        try:
            programmes = guide(dict(item.get("channel") or {}), now - rs.POST_S)
        except Exception:  # a guide that fails lists no airing
            programmes = []
        for programme in programmes:
            if " ".join(programme.title.casefold().split()) == title:
                found.append((programme.start.timestamp(), programme.stop.timestamp()))
    kept = [
        (start, stop)
        for start, stop in found
        if stop + rs.POST_S > now and (kind == rs.ONCE or start < horizon)
    ]
    return sorted(set(kept))


def clashes(plans: Iterable[Plan], limits: Mapping[str, Optional[int]]) -> dict[str, Clash]:
    """The items (by id) whose windows run when their provider would need more streams than
    `limits` allows (a provider without a known limit never clashes). Windows on one channel
    that meet or overlap share one stream. The first such moment is each one's."""
    by_provider: dict[str, list[Plan]] = {}
    for plan in plans:
        by_provider.setdefault(plan.provider, []).append(plan)
    found: dict[str, Clash] = {}
    for provider, own in by_provider.items():
        limit = limits.get(provider)
        if not limit or limit <= 0:
            continue
        streams = _streams(own)
        moments = sorted({start for start, _stop, _ids in streams})
        for moment in moments:
            running = [s for s in streams if s[0] <= moment < s[1]]
            if len(running) <= limit:
                continue
            for _start, _stop, ids in running:
                for item_id in ids:
                    if item_id not in found:
                        found[item_id] = Clash(moment, len(running), limit, provider)
    return found


def _streams(plans: list[Plan]) -> list[tuple[float, float, set[str]]]:
    """The provider's streams: each channel's windows that meet or overlap, merged."""
    streams: list[tuple[dict, float, float, set[str]]] = []
    for plan in sorted(plans, key=lambda p: (p.start, p.stop)):
        for k, (channel, start, stop, ids) in enumerate(streams):
            if rs.same_channel(channel, plan.channel) and plan.start <= stop:
                streams[k] = (channel, start, max(stop, plan.stop), ids | {plan.item_id})
                break
        else:
            streams.append((plan.channel, plan.start, plan.stop, {plan.item_id}))
    return [(start, stop, ids) for _channel, start, stop, ids in streams]


def plans(item: Mapping, times: Iterable[tuple[float, float]]) -> list[Plan]:
    """`item`'s airings as windows on its provider's stream."""
    return [
        Plan(
            str(item.get("id") or ""),
            str(item.get("title") or ""),
            str(item.get("provider") or ""),
            dict(item.get("channel") or {}),
            start - rs.PRE_S,
            stop + rs.POST_S,
        )
        for start, stop in times
    ]


# ---- the sections ----------------------------------------------------------------------------


def now_entries(recordings: Iterable[Recording], waiting: Iterable[Waiting]) -> list[Entry]:
    """What records now (the oldest first), then what waits for a stream."""
    entries = []
    for rec in sorted(recordings, key=lambda r: r.since):
        if rec.role == "scheduled":
            detail = f"{rec.channel} · " + translate(SCHEDULED_UNTIL, until=clock(rec.until))
            title = rec.title or rec.channel
        else:
            shown = rec.role != "background"  # on screen: large, small or a tile
            detail = translate(WATCHING_TEXT if shown else BACKGROUND_TEXT)
            title = rec.channel
        buttons: tuple[Button, ...] = (button(WATCH), button(STOP, DANGER))
        if rec.role not in ("background", "scheduled"):
            buttons = (button(STOP, DANGER),)  # it shows already: nothing to go back to
        entries.append(
            Entry(
                key=f"rec:{rec.name}",
                section=NOW,
                title=title,
                detail=detail,
                tone=REC,
                status=status("rec"),
                buttons=buttons,
                since=rec.since,
                sort=rec.since,
            )
        )
    for wait in waiting:
        until = clock(wait.until)
        if wait.reason:
            note = translate(RETRYING_TEXT, why=wait.reason, until=until)
        else:
            note = translate(WAITING_TEXT, provider=wait.provider, until=until)
        entries.append(
            Entry(
                key=f"wait:{wait.key}",
                section=NOW,
                title=wait.title,
                detail=wait.channel,
                note=note,
                tone=WARN,
                status=status("waiting"),
                buttons=(button(TRY_NOW, ACCENT), button(SKIP)),
                sort=wait.until,
            )
        )
    return entries


def coming_entries(
    items: Iterable[Mapping],
    now: float,
    *,
    limits: Mapping[str, Optional[int]],
    recording_ids: Iterable[str] = (),
    zone: Optional[tzinfo] = None,
    guide: Optional[Callable[[dict, float], Sequence[Programme]]] = None,
    days: int = DAYS,
    skipped: Callable[[str, float], bool] = lambda item_id, start: False,
    name_providers: bool = False,
) -> list[Entry]:
    """2.9.7: one row per airing in the next `days` (a series' as its guide says, a timer's on
    its days), soonest first, skipped ones (`skipped(item_id, start)`) left out, with clashes;
    the last row adds one (Record at a time, the guide)."""
    recording = set(recording_ids)
    timed: list[tuple[Mapping, list[tuple[float, float]]]] = []
    windows: list[Plan] = []
    for item in items:
        item_id = str(item.get("id") or "")
        times = [
            (start, stop)
            for start, stop in airings(item, now, days=days, zone=zone, guide=guide)
            if not skipped(item_id, start)
        ]
        timed.append((item, times))
        windows += plans(item, times)
    found = clashes(windows, limits)
    entries = []
    for item, times in timed:
        records_now = str(item.get("id")) in recording
        for start, stop in times:
            entries.append(
                _airing_entry(item, start, stop, now, found, records_now, name_providers)
            )
    entries.sort(key=lambda e: e.sort)
    entries.append(
        Entry(
            key=f"add:{COMING}",
            section=COMING,
            title="",
            buttons=(button(ADD_TIMER, ACCENT), button(OPEN_GUIDE)),
            sort=float("inf"),
        )
    )
    return entries


def _airing_entry(
    item: Mapping,
    start: float,
    stop: float,
    now: float,
    found: Mapping[str, Clash],
    records_now: bool,
    name_providers: bool = False,
) -> Entry:
    kind = item.get("kind")
    item_id = str(item.get("id") or "")
    channel = str((item.get("channel") or {}).get("name") or "")
    provider = str(item.get("provider") or "")
    title = _title(item)
    on_now = records_now and start - rs.PRE_S <= now < stop + rs.POST_S
    # One that records now is not changed under its recording (Stop it in Recording now).
    edit = button(EDIT, enabled=not on_now)
    buttons: tuple[Button, ...]
    if kind == rs.ONCE:
        pill, buttons = "once", (edit, button(CANCEL, DANGER))
    elif kind == rs.TIMER:
        pill, buttons = "repeats", (edit, button(SKIP, enabled=not on_now))
    else:
        pill, buttons = "series", (button(SKIP, enabled=not on_now),)
    detail = f"{short_when(start)}–{clock(stop)} · {channel}"
    if name_providers and provider and provider not in detail:  # one provider: none named
        detail = f"{detail} · {provider}"
    notes: list[str] = []
    if on_now:
        notes.append(translate(ON_NOW_TEXT, until=clock(stop)))
    clash = found.get(item_id)
    tone = OK
    if clash is not None and start - rs.PRE_S <= clash.at < stop + rs.POST_S:
        tone = WARN
        notes.append(
            translate(
                CLASH_TEXT,
                when=short_when(clash.at + rs.PRE_S),
                n=number(clash.count),
                provider=clash.provider,
                limit=number(clash.limit),
            )
        )
    return Entry(
        key=f"airing:{item_id}@{int(start)}",
        section=COMING,
        title=title,
        detail=detail,
        note=" · ".join(notes),
        tone=tone,
        status=status(pill),
        buttons=buttons,
        sort=start,
    )


def _title(item: Mapping) -> str:
    title = str(item.get("title") or "")
    if item.get("kind") == rs.TIMER and not title:
        return rs.slot_text(int(item.get("begin") or 0), int(item.get("minutes") or 0))
    return title


def rule_entries(
    items: Iterable[Mapping],
    now: float,
    *,
    zone: Optional[tzinfo] = None,
    guide: Optional[Callable[[dict, float], Sequence[Programme]]] = None,
    days: int = DAYS,
    skipped: Callable[[str, float], bool] = lambda item_id, start: False,
    name_providers: bool = False,
) -> list[Entry]:
    """2.9.7: Series & timers: each series and each timer once, with its next airing, Edit… (a
    timer) and Remove; the last row adds a timer."""
    entries = []
    for item in items:
        kind = item.get("kind")
        if kind not in (rs.SERIES, rs.TIMER):
            continue
        channel = str((item.get("channel") or {}).get("name") or "")
        provider = str(item.get("provider") or "")
        item_id = str(item.get("id") or "")
        upcoming = [
            start
            for start, _stop in airings(item, now, days=days, zone=zone, guide=guide)
            if start - rs.PRE_S > now and not skipped(item_id, start)
        ]
        buttons: tuple[Button, ...]
        if kind == rs.SERIES:
            detail = translate(EVERY_AIRING_TEXT, channel=channel)
            pill, buttons = "series", (button(REMOVE_RULE, DANGER),)
        else:
            detail = timer_text(dict(item))
            pill, buttons = "repeats", (button(EDIT), button(REMOVE_RULE, DANGER))
        if name_providers and provider and provider not in detail:  # one provider: none named
            detail = f"{detail} · {provider}"
        if upcoming:
            note = translate(NEXT_TEXT, when=short_when(upcoming[0]))
        else:
            note = translate(NO_AIRING_TEXT)
        entries.append(
            Entry(
                key=f"rule:{item.get('id')}",
                section=RULES,
                title=_title(item),
                detail=detail,
                note=note,
                status=status(pill),
                buttons=buttons,
                sort=upcoming[0] if upcoming else now + 365 * 86400,
            )
        )
    entries.sort(key=lambda e: e.sort)
    entries.append(
        Entry(
            key=f"add:{RULES}",
            section=RULES,
            title="",
            buttons=(button(ADD_TIMER, ACCENT), button(OPEN_GUIDE)),
            sort=float("inf"),
        )
    )
    return entries


def problem_reason(outcome: Mapping, failure: Callable[[str], str]) -> str:
    """Why a recording was missed or failed, and what helps; `failure(why)` says a failure's
    code (`ui.recording_scheduler.failure_reason`)."""
    why = str(outcome.get("why") or "")
    if outcome.get("state") == rs.MISSED:
        if why == "asleep":
            return translate(ASLEEP_TEXT)
        if why == "stream":
            return translate(NO_STREAM_TEXT)
        return translate(CLOSED_TEXT)
    if why == "stream":
        return translate(NO_STREAM_TEXT)
    reason = failure(why) if why else ""
    reason = reason[:1].upper() + reason[1:] if reason else ""
    if outcome.get("path"):
        return translate(FAILED_KEPT, why=reason)
    return f"{reason}." if reason and not reason.endswith(".") else reason


def outcome_key(outcome: Mapping) -> str:
    return f"out:{float(outcome.get('at') or 0)!r}"


def attention_entries(
    outcomes: Iterable[Mapping], failure: Callable[[str], str], exists: Callable[[str], bool]
) -> list[Entry]:
    """The missed and failed recordings not dismissed, newest first; with two or more, a last
    row dismisses them all. `exists(path)`: a part's file is still there (it plays)."""
    entries = []
    for outcome in outcomes:
        state = outcome.get("state")
        if state not in (rs.MISSED, rs.FAILED) or outcome.get("seen"):
            continue
        start = float(outcome.get("start") or 0)
        channel = str(outcome.get("channel") or "")
        path = str(outcome.get("path") or "")
        buttons = [button(DISMISS)]
        if path and exists(path):
            buttons.insert(0, button(PLAY, ACCENT))
        entries.append(
            Entry(
                key=outcome_key(outcome),
                section=ATTENTION,
                title=str(outcome.get("title") or channel),
                detail=" · ".join(part for part in (short_when(start), channel) if part),
                note=problem_reason(outcome, failure),
                tone=ERROR if state == rs.FAILED else WARN,
                status=status("failed" if state == rs.FAILED else "missed"),
                buttons=tuple(buttons),
                sort=-float(outcome.get("at") or 0),
            )
        )
    if len(entries) >= 2:
        entries.append(
            Entry(
                key=f"add:{ATTENTION}",
                section=ATTENTION,
                title="",
                buttons=(button(DISMISS_ALL),),
                sort=float("inf"),
            )
        )
    return entries


def recorded_entries(files: Iterable[Mapping], many_places: bool = False) -> list[Entry]:
    """The recordings' files (the Library's titles of them), as listed: newest first. With
    `many_places`, each says its folder."""
    entries = []
    for item in files:
        if not item.get("_recording"):
            continue
        path = str(item.get("path") or "")
        writing = bool(item.get("_writing"))
        detail = str(item.get("description") or "")
        if many_places and path:
            detail = f"{detail} · {os.path.dirname(path)}"
        entries.append(
            Entry(
                key=f"file:{path}",
                section=RECORDED,
                title=str(item.get("name") or ""),
                detail=detail,
                tone=REC if writing else QUIET,
                status=status("writing") if writing else "",
                buttons=(
                    button(PLAY, ACCENT),
                    button(FOLDER),
                    button(DELETE, DANGER, enabled=not writing),
                ),
                sort=-float(item.get("added") or 0),
            )
        )
    return entries


def place_entries(places: Iterable[Place], home: str = "") -> list[Entry]:
    """The folders recordings are listed from (the one new recordings go to first), then a
    row that adds one. `home`: the user's home folder, shown as ~."""
    entries = []
    for place in sorted(places, key=lambda p: (not p.default, p.path.casefold())):
        parts = []
        if place.builtin:
            parts.append(translate(DEFAULT_PLACE_TEXT))
        if place.reachable:
            # i18n: a place's line on the Recordings page: how many recordings it holds
            parts.append(
                tr_n("{n} recording", "{n} recordings", place.count, n=number(place.count))
            )
            if place.size:
                parts.append(size_text(place.size))
            if place.free is not None:
                parts.append(translate(FREE_TEXT, free=size_text(place.free)))
        note, tone = "", OK if place.default else QUIET
        if not place.reachable:
            note, tone = translate(UNREACHABLE_TEXT), WARN
        elif place.free is not None and place.free < LOW_SPACE and place.default:
            note, tone = translate(LOW_SPACE_TEXT, free=size_text(place.free)), WARN
        buttons = [button(OPEN)]
        if not place.default:
            buttons += [button(DEFAULT, enabled=place.reachable), button(REMOVE)]
        entries.append(
            Entry(
                key=f"place:{place.path}",
                section=PLACES,
                title=shown_path(place.path, home),
                detail=" · ".join(parts),
                note=note,
                tone=tone,
                status=status("default") if place.default else "",
                buttons=tuple(buttons),
            )
        )
    entries.append(
        Entry(key=f"add:{PLACES}", section=PLACES, title="", buttons=(button(ADD_PLACE),))
    )
    return entries


def shown_path(path: str, home: str = "") -> str:
    """A folder as the page shows it: under the home folder, from ~."""
    if not home:
        return path
    try:
        inside = os.path.commonpath([os.path.abspath(path), os.path.abspath(home)])
    except ValueError:  # another drive (Windows)
        return path
    if inside != os.path.abspath(home):
        return path
    rest = os.path.relpath(os.path.abspath(path), os.path.abspath(home))
    return "~" if rest == "." else os.path.join("~", rest)


def use_of(purpose: str, slot_role: Optional[str], channel: str, recording: bool) -> Use:
    """What a stream held for `purpose` is used for: a player's (its `slot_role`: "large",
    "small", "tile", "background"), a scheduled recording's, the archive's probe."""
    if purpose.startswith("sched:"):
        return Use("scheduled", channel, True)
    if purpose == "probe":
        return Use("probe", translate(USE_TEXTS["probe"]))
    kind = {"large": "watch", "small": "pip", "tile": "tile", "background": "background"}.get(
        slot_role or "", "other"
    )
    label = translate(USE_TEXTS[kind])
    if channel:
        label = f"{label}: {channel}"
    return Use(kind, label, recording or kind == "background")


def streams_text(streams: Streams) -> str:
    """ "1 of 2 streams", or "1 stream" when the provider's limit is not known."""
    used = len(streams.uses)
    if streams.limit:
        # i18n: a provider's streams in use on the Recordings page ({n} of {limit})
        return tr_n(
            "{n} of {limit} stream",
            "{n} of {limit} streams",
            streams.limit,
            n=number(used),
            limit=number(streams.limit),
        )
    # i18n: a provider's streams in use on the Recordings page (its limit not known)
    return tr_n("{n} stream in use", "{n} streams in use", used, n=number(used))
