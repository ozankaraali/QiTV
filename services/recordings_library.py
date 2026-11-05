"""The Recordings tab's rail and library side (2.9.7).

The rail, as the Movies tab's genre column: pinned rows for the recorder's views (Tonight, the
home; Coming up; Series & timers; Needs attention; their counts from the desk), "RECORDED · n"
with All and a row per channel (the most recordings first), "SETTINGS" with Folders & streams.
The recordings themselves are the recordings list's files (services.recordings_list, each
with what services.recordings_index says of it), shown as tiles: newest first, filtered by the
rail's find, each with its length and NEW (never played) and its details for the strip, its
picture (a frame of it: services.recording_thumbs) and its caption.

The recorder's lists are tiles too (`desk_sections`): what records now, then Coming up's
airings by day, Series & timers, the problems; each tile has its channel's logo (else a card
in its channel's colour), its time and status as badges, its title and channel as a caption,
and its row's buttons in the strip (kind "desk"); a row of buttons alone (Record at a time…)
is a last tile under "Add a recording".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from services import i18n, parental, recording_desk as desk
from services.i18n import N_, tr, translate
from services.library import Details, Genre
from services.recordings_list import size_text

TONIGHT, COMING, RULES, ATTENTION = "rec:tonight", "rec:coming", "rec:rules", "rec:attention"
ALL, CHANNEL_PREFIX, SETTINGS = "rec:all", "rec:ch:", "rec:settings"
RAIL_ICONS = {TONIGHT: "record", COMING: "clock", RULES: "recent", ATTENTION: "live_dot"}
RAIL_ICONS[SETTINGS] = "gear"
# The desk's sections each rail row shows (the home is TONIGHT, the grid ALL and the channels).
SECTIONS: dict[str, Any] = {
    COMING: (desk.NOW, desk.COMING),  # what records or waits now, then the airings
    RULES: desk.RULES,
    ATTENTION: desk.ATTENTION,
}
SECTIONS[SETTINGS] = desk.SETTINGS
# i18n: the Recordings tab's first row: its home (what records now, next, tonight's guide)
TONIGHT_TEXT = N_("Tonight")
# i18n: the Recordings tab's caption over the recordings, with their number; in capitals
RECORDED_TEXT = N_("Recorded · {n}")
# i18n: the Recordings tab's caption over its settings; in capitals
SETTINGS_TEXT = N_("Settings")
# i18n: placeholder of the Recordings tab's find (its recordings and the guide's programmes)
FIND_TEXT = N_("Find a recording or programme…")
# i18n: a card's label: a recording never played
NEW_TEXT = N_("NEW")
ADD_TITLE = N_("Add a recording")  # the recorder home's caption, over its tiles of buttons
_TONES = {desk.REC: "live", desk.ERROR: "live", desk.WARN: "warn"}


def fold(text: str) -> str:
    return " ".join(text.casefold().split())


def length_text(seconds: float) -> str:
    """A recording's length as its card says it, as an episode's running time: "45 min",
    "1h 52m"."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return tr("{n} min", n=minutes)  # i18n: an episode's running time in minutes, abbreviated
    # i18n: a running time, abbreviated (1h 5m)
    return tr("{h}h {m}m", h=minutes // 60, m=f"{minutes % 60:02d}")


class RecordingsLibrary:
    def __init__(
        self,
        files: Callable[[], list[dict]],
        counts: Callable[[], Mapping[str, int]],
        watch: Any,
        config: Any,
        *,
        mixed: Callable[[], bool] = lambda: False,
        picture: Callable[[dict], str] = lambda item: "",
    ) -> None:
        """`files()`: the recordings list's files, newest first; `counts()`: the desk's rows by
        rail id; `watch`: the Library's WatchState (resume points); `mixed()`: two providers
        or more (a recording's provider is named); `picture(item)`: a recording's thumbnail
        url ("" until there is one)."""
        self._files = files
        self._counts = counts
        self._watch = watch
        self._config = config
        self._mixed = mixed
        self._picture = picture

    # ---- the rail ---------------------------------------------------------------------------

    def rail(self, filter_text: str = "") -> list[Genre]:
        counts = self._counts()
        rows = [
            Genre(TONIGHT, translate(TONIGHT_TEXT), None, pinned=True),
            Genre(
                COMING, translate(desk.SECTION_TITLES[desk.COMING]), _count(counts, COMING), True
            ),
            Genre(RULES, translate(desk.SECTION_TITLES[desk.RULES]), _count(counts, RULES), True),
            Genre(
                ATTENTION,
                translate(desk.SECTION_TITLES[desk.ATTENTION]),
                _count(counts, ATTENTION),
                True,
            ),
        ]
        files = self._shown()
        by_channel: dict[str, int] = {}
        for item in files:
            channel = _channel(item)
            by_channel[channel] = by_channel.get(channel, 0) + 1
        wanted = fold(filter_text)
        channels = sorted(by_channel.items(), key=lambda pair: (-pair[1], fold(pair[0])))
        rows.append(
            Genre("", i18n.upper(translate(RECORDED_TEXT, n=len(files))), None, header=True)
        )
        rows.append(Genre(ALL, translate(desk.ALL_TEXT), len(files)))
        rows += [
            Genre(CHANNEL_PREFIX + name, name, count)
            for name, count in channels
            if not wanted or wanted in fold(name)
        ]
        rows.append(Genre("", i18n.upper(translate(SETTINGS_TEXT)), None, header=True))
        rows.append(Genre(SETTINGS, translate(desk.SECTION_TITLES[desk.SETTINGS]), None))
        return rows

    # ---- the tiles --------------------------------------------------------------------------

    def items(self, rail_id: str, filter_text: str = "") -> list[dict]:
        """The recordings `rail_id` lists (All, a channel's), newest first, those matching
        `filter_text` (their title or channel) when given."""
        files = self._shown()
        if rail_id.startswith(CHANNEL_PREFIX):
            channel = rail_id[len(CHANNEL_PREFIX) :]
            files = [f for f in files if _channel(f) == channel]
        elif rail_id != ALL:
            return []
        wanted = fold(filter_text)
        if wanted:
            files = [f for f in files if wanted in fold(_title(f)) or wanted in fold(_channel(f))]
        return sorted(
            files, key=lambda f: float(f.get("_start") or f.get("added") or 0), reverse=True
        )

    def _shown(self) -> list[dict]:
        """The recordings, but those of a channel parental controls hide while they are locked
        (2.8's rule: a recording knows its channel by name)."""
        files = list(self._files())
        guard = parental.installed()
        if guard is None or not guard.active():
            return files
        return [f for f in files if not guard.hides_recording(_channel(f))]

    def identity(self, item: dict) -> str:
        return str(item.get("path") or item.get("id") or "")

    def content_id(self, item: dict) -> str:
        """The resume key the Library keeps a recording's point under (its id)."""
        return str(item.get("id") or "")

    def progress(self, item: dict) -> Optional[float]:
        resume = self._watch.resume_from(self.content_id(item), item)
        if resume is None:
            return None
        saved = (getattr(self._config, "playback_positions", None) or {}).get(
            self.content_id(item)
        ) or {}
        duration = int(saved.get("duration") or 0)
        return min(1.0, resume / duration) if duration > 0 else None

    # ---- the recorder's tiles ----------------------------------------------------------------

    def desk_sections(
        self, entries: list[desk.Entry], day_of: Callable[[float], str]
    ) -> list[tuple[str, list[dict]]]:
        """The desk's rows as tiles: what records now (or waits), then the airings under
        their day (`day_of(start)`), the others as one section; rows of buttons alone last,
        under "Add a recording" (with the problems: among them)."""
        sections: list[tuple[str, list[dict]]] = []

        def put(title: str, item: dict) -> None:
            if sections and sections[-1][0] == title:
                sections[-1][1].append(item)
            else:
                sections.append((title, [item]))

        actions: list[dict] = []
        for entry in entries:
            item = desk_tile(entry)
            if not entry.title:
                actions.append(item)
            elif entry.section == desk.NOW:
                put(translate(desk.SECTION_TITLES[desk.NOW]), item)
            elif entry.section == desk.COMING:
                put(day_of(entry.start) if entry.start else "", item)
            else:
                put("", item)
        if actions:
            problems = all(item["_desk"].section == desk.ATTENTION for item in actions)
            title = "" if problems else translate(ADD_TITLE)
            for item in actions:
                put(title, item)
        return sections

    def picture(self, item: dict) -> tuple[str, bool]:
        """A tile's picture: (its url, whether it is a channel's logo); a recording's frame
        (its channel's logo until there is one), a row's channel logo, ("", False) for its
        tinted card."""
        entry = item.get("_desk")
        if isinstance(entry, desk.Entry):
            return (entry.logo, True) if entry.logo else ("", False)
        frame = self._picture(item)
        if frame:
            return frame, False
        logo = str(item.get("_logo") or "")  # until its frame is made (or if none comes)
        return (logo, True) if logo else ("", False)

    def caption(self, item: dict) -> tuple[str, str]:
        """A tile's title and the line under it: its channel and when it was recorded (one
        known by its channel alone: when)."""
        entry = item.get("_desk")
        if isinstance(entry, desk.Entry):
            return str(item.get("name") or ""), entry.channel if entry.title else ""
        title, channel = _title(item), _channel(item)
        start = item.get("_start")
        when = desk.short_when(float(start)) if isinstance(start, (int, float)) else ""
        if title and title != channel:
            return title, " · ".join(part for part in (channel, when) if part)
        return channel, when

    def badges(self, item: dict) -> tuple[str, ...]:
        """A recording's (its length, NEW when it never played); a row's (its time, its
        status, the status's tone)."""
        entry = item.get("_desk")
        if isinstance(entry, desk.Entry):
            when = ""
            if entry.start and entry.section == desk.COMING:
                when = desk.clock(entry.start)  # its day is its section's
            elif entry.start:
                when = desk.short_when(entry.start)
            status = i18n.upper(entry.status) if entry.status else ""
            return when, status, _TONES.get(entry.tone, "quiet")
        start, stop = item.get("_start"), item.get("_stop")
        length = ""
        if isinstance(start, (int, float)) and isinstance(stop, (int, float)) and stop > start:
            length = length_text(stop - start)
        played = self.content_id(item) in (getattr(self._config, "playback_positions", None) or {})
        return length, "" if played else translate(NEW_TEXT)

    def details(self, item: dict) -> Details:
        entry = item.get("_desk")
        if isinstance(entry, desk.Entry):
            return _desk_details(entry, str(item.get("name") or ""))
        start = item.get("_start")
        stop = item.get("_stop")
        when = ""
        if isinstance(start, (int, float)):
            began = datetime.fromtimestamp(float(start))
            when = began.strftime("%a %d %b %H:%M")
            if isinstance(stop, (int, float)):
                when += "–" + datetime.fromtimestamp(float(stop)).strftime("%H:%M")
        length = ""
        if isinstance(start, (int, float)) and isinstance(stop, (int, float)) and stop > start:
            length = length_text(stop - start)
        size = int(item.get("_size") or 0)
        plot = " · ".join(part for part in (when, size_text(size) if size else "") if part)
        resume = self._watch.resume_from(self.content_id(item), item)
        return Details(
            title=_title(item),
            year="",
            duration=length,
            genre=_channel(item),
            rating="",
            plot=plot,
            resume_ms=resume,
            progress=self.progress(item),
            favorite=False,
            kind="recording",
            provider=str(item.get("_provider") or "") if self._mixed() else "",
        )


def desk_tile(entry: desk.Entry) -> dict:
    """A desk row as a tile; a row of buttons alone is named after its first."""
    title = entry.title or (entry.buttons[0].label if entry.buttons else "")
    tile = {"id": entry.key, "name": title, "_desk": entry, "_channel": entry.channel}
    if not entry.title:
        tile["_action"] = True  # a tile that is a button (the grid draws it so)
    return tile


def _desk_details(entry: desk.Entry, title: str) -> Details:
    """A desk tile's strip: its title, its row's facts, its note, its row's buttons."""
    buttons = []
    for each in entry.buttons:
        style = each.style if each.style in (desk.ACCENT, desk.DANGER) else "raised"
        buttons.append((each.name, each.label, style if each.enabled else "off"))
    return Details(
        title=title,
        year="",
        duration="",
        genre="",
        rating="",
        plot=entry.note,
        resume_ms=None,
        progress=None,
        favorite=False,
        kind="desk",
        provider=entry.detail,  # the strip's line of facts: when, the channel (its provider)
        buttons=tuple(buttons),
    )


def _count(counts: Mapping[str, int], rail_id: str) -> Optional[int]:
    value = counts.get(rail_id)
    return int(value) if value else None


def _channel(item: dict) -> str:
    return str(item.get("_channel") or item.get("name") or "")


def _title(item: dict) -> str:
    return str(item.get("_title") or item.get("name") or "")
