"""The Recordings page's controller (2.9.7): fills `ui.library.recordings_view` from the
running QiTV and does what its buttons ask.

What it reads (`refresh`, on the GUI thread, never the disk): the players that record and
what plays (`TVController.sessions`), the scheduled recordings under way and waiting
(`RecordingScheduler`), the schedule and the outcomes kept with it (`Schedule`), each
provider's streams in use and their limit (`ConnectionBudget`), and the recordings' files and
places as `RecordingsList` last read them (on a thread of its own: `refresh_files`).
`services.recording_desk` turns that into the page's entries.

While the page shows it follows the scheduler's, the files' and the places' signals and looks
again every REFRESH_MS (the streams have no signal of their own), and reads the folders again
every FILES_MS (a disk's free space). Hidden, it does nothing.

The buttons: Watch tunes a recording's channel (as the REC sign's Go back), Stop stops it
(kept), Try now and Skip a scheduled recording waiting for a stream; Edit… opens Record at a
time on the item (a series has none), Cancel Recording takes it off the schedule; Dismiss (all)
lists a problem no more; Play, Show in Folder and Delete… a file (as the Recordings shelf's);
Open, Use for new recordings, Remove from list and Add a place… the places; Record at a time…
picks a channel (the list's), Record from the guide opens the guide over live TV.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QFileDialog

from services import recording, recording_desk as desk, recording_schedule as rs, system_open
from services.catalog import ALL_GROUP_ID
from services.connection_budget import ConnectionBudget
from services.i18n import N_, number, tr_n, translate
from services.recordings_list import size_text
from services.sessions import records
from ui.keymap import Action
from ui.library.recordings_view import RecordingsView
from ui.record_timer_dialog import RecordTimerDialog
from ui.recording_scheduler import failure_reason

logger = logging.getLogger(__name__)

REFRESH_MS = 2000
FILES_MS = 15000
# i18n: a toast: a folder was added to the Recordings page's places
PLACE_ADDED = N_("Recordings in {folder} are listed too.")
# i18n: a toast: new recordings go to this folder from now on
PLACE_USED = N_("New recordings go to {folder}.")
# i18n: the title of the folder picker of the Recordings page's Add a place…
ADD_PLACE_TITLE = N_("Choose a folder with recordings")
# i18n: a toast: Record at a time… on the Recordings page, with no channel to choose from
NO_CHANNELS = N_("Load a provider's channels first: they are what can be recorded.")
# i18n: the Recordings page's Edit…: the button that saves the change
SAVE_TEXT = N_("Save")
# i18n: a toast: a scheduled recording was changed on the Recordings page
EDITED_TEXT = N_("Recording changed.")
# i18n: a toast: a change on the Recordings page made no recording (its time is over)
EDIT_FAILED = N_("That time has passed: nothing was changed.")


class RecordingsDesk(QObject):
    # The Library's Recordings tab's dot: "rec" (something records), "warn" (a problem), "".
    markChanged = Signal(str)
    refreshed = Signal()  # 2.9.7: the page was worked out again (the Recordings home follows)

    def __init__(self, window: Any, view: RecordingsView, recordings: Any, parent=None) -> None:
        """`window`: the TVWindow (its schedule, scheduler, budget, catalogs, controller,
        screens and config); `recordings`: the Library's RecordingsList."""
        super().__init__(parent)
        self._window = window
        self._view = view
        self._recordings = recordings
        self._shown = False
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._files = QTimer(self)
        self._files.setInterval(FILES_MS)
        self._files.timeout.connect(self.refresh_files)
        self._dialog: Any = None  # Record at a time, open from here
        self._builtin: Optional[str] = None  # QiTV's own folder, looked up once
        self._mark = ""
        self._marks = QTimer(self)  # the tab's dot, while the Library shows
        self._marks.setInterval(REFRESH_MS)
        self._marks.timeout.connect(self.update_mark)
        view.buttonClicked.connect(self._on_button)
        recordings.changed.connect(self._follow)
        recordings.places_changed.connect(self._follow)

    # ---- showing ------------------------------------------------------------------------------

    def show(self) -> None:
        """The page shows: filled now, then kept up to date."""
        self._shown = True
        self.refresh_files()
        self.refresh()
        self._timer.start()
        self._files.start()

    def hide(self) -> None:
        self._shown = False
        self._timer.stop()
        self._files.stop()

    def shown(self) -> bool:
        return self._shown

    def follow_scheduler(self, scheduler: Any) -> None:
        """The scheduled recordings' changes refresh the page (2.8's scheduler, once made)."""
        scheduler.changed.connect(self._follow)

    def _follow(self) -> None:
        if self._shown:
            self.refresh()
        self.update_mark()

    def track_mark(self, library: bool) -> None:
        """The Library shows (its tab's dot is kept up to date) or not (nothing to do)."""
        if library:
            self.update_mark()
            self._marks.start()
        else:
            self._marks.stop()

    def update_mark(self) -> None:
        if self._window.closing():
            return
        recording_now, problems = self.marks()
        mark = "rec" if recording_now else ("warn" if problems else "")
        if mark != self._mark:
            self._mark = mark
            self.markChanged.emit(mark)

    def refresh_files(self) -> None:
        self._recordings.refresh()  # on a thread of its own: `changed` follows

    def marks(self) -> tuple[int, int]:
        """(what records now, the problems not dismissed): the Library's tab shows them."""
        recording_now = len(self._recording_now())
        problems = sum(
            1
            for outcome in self._schedule().outcomes()
            if outcome.get("state") in (rs.MISSED, rs.FAILED) and not outcome.get("seen")
        )
        return recording_now, problems

    # ---- what the page shows ----------------------------------------------------------------

    def _schedule(self) -> rs.Schedule:
        schedule: rs.Schedule = self._window.recording_schedule
        return schedule

    def _scheduler(self) -> Any:
        return getattr(self._window, "scheduler", None)

    def _budget(self) -> Optional[ConnectionBudget]:
        return getattr(self._window, "budget", None)

    def _sessions(self) -> Any:
        controller = getattr(self._window, "controller", None)
        return getattr(controller, "sessions", None)

    def _players(self) -> list[tuple[str, str, dict, Optional[float]]]:
        """Each player: (its slot's name, its role: "large", "small", "background", "tile" or
        "idle", its channel, since when it records or None)."""
        sessions = self._sessions()
        if sessions is None:
            return []
        try:
            background = {slot.name for slot in sessions.background()}
            large = sessions.large().name
            small = sessions.small()
            grid = bool(sessions.grid())
            found = []
            for slot in sessions.slots():
                if slot.name in background:
                    role = "background"
                elif slot.name == large:
                    role = "large"
                elif small is not None and slot.name == small.name:
                    role = "small"
                else:
                    role = "tile" if grid else "idle"
                since = slot.live.recording_since() if records(slot) else None
                found.append((slot.name, role, dict(slot.channel or {}), since))
            return found
        except Exception as e:  # a player being made or closed: the page waits for the next look
            logger.debug("The players could not be read: %s", type(e).__name__)
            return []

    def _recording_now(self) -> list[desk.Recording]:
        scheduler = self._scheduler()
        # 2.9.7: a player a scheduled recording records on is listed once, as the programme.
        hosting = scheduler.hosted_players() if scheduler is not None else set()
        found = [
            desk.Recording(name, str(channel.get("name") or ""), since, role)
            for name, role, channel, since in self._players()
            if since is not None and name not in hosting
        ]
        if scheduler is not None:
            for name, title, channel, since, until, _item in scheduler.recording_details():
                found.append(desk.Recording(name, channel, since, "scheduled", title, until))
        return found

    def _waiting(self) -> list[desk.Waiting]:
        scheduler = self._scheduler()
        if scheduler is None:
            return []
        waiting = []
        for job, why in scheduler.waiting():
            reason = "" if why == "stream" else failure_reason(why)
            waiting.append(
                desk.Waiting(
                    job.key,
                    job.item_id,
                    job.title,
                    str(job.channel.get("name") or ""),
                    job.provider,
                    rs.window(job)[1],
                    reason,
                )
            )
        return waiting

    def _limit(self, provider: str) -> Optional[int]:
        budget, catalogs = self._budget(), getattr(self._window, "catalogs", None)
        if budget is None or catalogs is None:
            return None
        try:
            entry = catalogs.provider(provider)
        except KeyError:
            return None
        try:
            return budget.limit_for(entry, catalogs.max_connections(provider))
        except Exception as e:  # an account still loading: no limit known yet
            logger.debug("A provider's stream limit is unknown: %s", type(e).__name__)
            return None

    def _streams(self, scheduled: set[str]) -> list[desk.Streams]:
        """Each provider with a stream in use or a recording scheduled: what holds its
        streams (a player's channel, a scheduled programme)."""
        budget = self._budget()
        if budget is None:
            return []
        players = {name: (role, channel, since) for name, role, channel, since in self._players()}
        titles: dict[str, tuple[str, str]] = {}
        scheduler = self._scheduler()
        if scheduler is not None:
            for name, title, channel, *_rest in scheduler.recording_details():
                titles[name] = (title, channel)
        catalog = getattr(self._window, "catalog", None)
        names = list(catalog.provider_names() if catalog is not None else [])
        names += sorted(scheduled - set(names) - {""})  # a provider's recordings, kept by name
        found = []
        for provider in names:
            holders = budget.holders(provider)
            if not holders and provider not in scheduled:
                continue
            limit = self._limit(provider)
            if not holders and not limit:
                continue  # nothing in use and no limit: no card says "0 in use, free"
            uses = []
            for purpose in holders:
                role, channel, since = players.get(purpose, (None, {}, None))
                label = str(channel.get("name") or "")
                if purpose in titles:
                    title, name = titles[purpose]
                    label = f"{title} ({name})" if title and title != name else name
                uses.append(desk.use_of(purpose, role, label, since is not None))
            found.append(desk.Streams(provider, limit, tuple(uses)))
        return found

    def _summary(self, entries: list[desk.Entry]) -> str:
        counts = {section: 0 for section in desk.SECTIONS}
        for entry in entries:
            if entry.title:
                counts[entry.section] += 1
        parts = []
        if counts[desk.NOW]:
            n = counts[desk.NOW]
            # i18n: the Recordings page's summary line: recordings under way now
            parts.append(tr_n("{n} recording now", "{n} recording now", n, n=number(n)))
        n = counts[desk.COMING]
        # i18n: the Recordings page's summary line: scheduled recordings
        parts.append(tr_n("{n} scheduled", "{n} scheduled", n, n=number(n)))
        if counts[desk.ATTENTION]:
            # i18n: the Recordings page's summary line: missed or failed recordings
            parts.append(
                tr_n(
                    "{n} needs attention",
                    "{n} need attention",
                    counts[desk.ATTENTION],
                    n=number(counts[desk.ATTENTION]),
                )
            )
        n = counts[desk.RECORDED]
        # i18n: the Recordings page's summary line: the recordings' files
        parts.append(tr_n("{n} recorded", "{n} recorded", n, n=number(n)))
        places = self._recordings.places()
        if places and places[0].free is not None:
            parts.append(translate(desk.FREE_TEXT, free=size_text(places[0].free)))
        return " · ".join(parts)

    def entries(self) -> list[desk.Entry]:
        """The page's entries, as now."""
        now = time.time()
        recording_now = self._recording_now()
        scheduled_ids = []
        scheduler = self._scheduler()
        if scheduler is not None:
            scheduled_ids = [detail[5] for detail in scheduler.recording_details()]
        items = self._schedule().items()
        providers = {str(item.get("provider") or "") for item in items}
        limits = {provider: self._limit(provider) for provider in providers}
        files = self._recordings.files()
        places = self._recordings.places()
        entries = desk.now_entries(recording_now, self._waiting())
        skipped = self._schedule().is_skipped
        entries += desk.coming_entries(
            items,
            now,
            limits=limits,
            recording_ids=scheduled_ids,
            guide=self._guide,
            skipped=skipped,
            name_providers=self._mixed(),
        )
        entries += desk.rule_entries(  # 2.9.7
            items, now, guide=self._guide, skipped=skipped, name_providers=self._mixed()
        )
        outcomes = self._schedule().outcomes()
        entries += desk.attention_entries(outcomes, failure_reason, self._listed_file)
        entries += desk.recorded_entries(files, many_places=len(places) > 1)
        entries += desk.place_entries(self._places(files, places), os.path.expanduser("~"))
        return entries

    def _mixed(self) -> bool:
        """Two providers or more: rows name their provider (one: none, as in 2.0)."""
        catalog = getattr(self._window, "catalog", None)
        try:
            return catalog is not None and len(catalog.provider_names()) > 1
        except Exception:  # a catalog being made: none named
            return False

    def entries_of(self, section: str) -> list[desk.Entry]:
        """2.9.7: one section's entries (a row of the Recordings tab's rail)."""
        return [entry for entry in self.entries() if entry.section == section]

    def hero(self) -> desk.Hero:
        """2.9.7: the Recordings home's top row: what records now (players and scheduled), the
        next airing, and the streams of the providers with a limit or a stream in use."""
        scheduler = self._scheduler()
        recording_ids = [d[5] for d in scheduler.recording_details()] if scheduler else []
        items = self._schedule().items()
        upcoming = desk.next_airing(
            items,
            time.time(),
            guide=self._guide,
            skipped=self._schedule().is_skipped,
            recording_ids=recording_ids,
        )
        scheduled = {str(item.get("provider") or "") for item in items}
        return desk.Hero(tuple(self._recording_now()), upcoming, tuple(self._streams(scheduled)))

    def _listed_file(self, path: str) -> bool:
        """A part of a failed recording plays while the list has its file (no disk read)."""
        return any(f.get("path") == path for f in self._recordings.files())

    def _guide(self, channel: dict, since: float) -> list:
        """A series' channel's programmes from `since` on (the guides QiTV has loaded)."""
        catalogs = getattr(self._window, "catalogs", None)
        if catalogs is None:
            return []
        moment = datetime.fromtimestamp(since).astimezone()
        return list(catalogs.programmes(channel, moment, 48))

    def _places(self, files: list[dict], places: list[Any]) -> list[desk.Place]:
        listed: list[Any] = list(places)
        if not listed:  # not read yet: the folders, nothing known of them
            listed = [_Unread(path) for path in recording.places(self._window.config_manager)]
        if self._builtin is None:
            self._builtin = recording.default_folder()
        builtin = self._builtin
        found = []
        for k, place in enumerate(listed):
            own = [f for f in files if _inside(str(f.get("path") or ""), place.path)]
            found.append(
                desk.Place(
                    place.path,
                    k == 0,
                    builtin=_same(place.path, builtin),
                    free=place.free,
                    count=len(own),
                    size=sum(int(f.get("_size") or 0) for f in own),
                    reachable=place.reachable,
                )
            )
        return found

    def refresh(self) -> None:
        """The page as QiTV is now."""
        if self._window.closing():
            return
        entries = self.entries()
        scheduled = {str(item.get("provider") or "") for item in self._schedule().items()}
        self._view.set_content(entries, self._streams(scheduled), self._summary(entries))
        self.update_mark()
        self.refreshed.emit()

    # ---- the buttons --------------------------------------------------------------------------

    def _on_button(self, key: str, name: str) -> None:
        if self._window.closing():
            return
        kind, _, rest = key.partition(":")
        handlers: dict[str, Callable[[str, str], None]] = {
            "rec": self._recording_button,
            "wait": self._waiting_button,
            "item": self._item_button,
            "airing": self._airing_button,  # 2.9.7
            "rule": self._item_button,  # 2.9.7
            "out": self._outcome_button,
            "file": self._file_button,
            "place": self._place_button,
            "add": self._add_button,
        }
        handler = handlers.get(kind)
        if handler is not None:
            handler(rest, name)
        self.refresh()

    def stop(self, name: str) -> None:
        """2.9.7: the recording named `name` stops (the Recordings home's ■ Stop)."""
        self._recording_button(name, desk.STOP)
        self.refresh()

    def _recording_button(self, slot: str, name: str) -> None:
        if name == desk.WATCH:
            self._window.screens.watch_recording(slot)
        elif name == desk.STOP:
            scheduler = self._scheduler()
            if slot.startswith("sched:") and scheduler is not None:
                scheduler.stop_now(slot)
                return
            sessions = self._sessions()
            for player in sessions.slots() if sessions is not None else []:
                if player.name == slot and records(player):
                    player.live.stop_recording()

    def _waiting_button(self, job_key: str, name: str) -> None:
        scheduler = self._scheduler()
        if scheduler is None:
            return
        if name == desk.TRY_NOW:
            scheduler.try_now(job_key)
        elif name == desk.SKIP:
            scheduler.skip(job_key)

    def _item_button(self, item_id: str, name: str) -> None:
        if name in (desk.CANCEL, desk.REMOVE_RULE):
            self._window.cancel_scheduled(item_id)
        elif name == desk.EDIT:
            self.edit(item_id)

    def _airing_button(self, airing: str, name: str) -> None:
        """2.9.7: an airing's row: "<item id>@<start>"."""
        item_id, _, at = airing.rpartition("@")
        if name == desk.SKIP:
            try:
                start = float(at)
            except ValueError:
                return
            scheduler = self._scheduler()
            if scheduler is not None:  # it ends a wait, or stops the airing that records
                scheduler.skip_airing(item_id, start)
            else:
                self._schedule().skip(item_id, start)
            changed = getattr(self._window, "schedule_changed", None)
            if callable(changed):
                changed()  # the guides and the scheduler follow
        else:
            self._item_button(item_id, name)

    def _outcome_button(self, at: str, name: str) -> None:
        try:
            moment = float(at)
        except ValueError:
            return
        outcome = next(
            (o for o in self._schedule().outcomes() if float(o.get("at") or 0) == moment), None
        )
        if name == desk.DISMISS:
            self._schedule().dismiss(moment)
        elif name == desk.PLAY and outcome is not None and outcome.get("path"):
            self._play_path(str(outcome["path"]))

    def _file_button(self, path: str, name: str) -> None:
        item = next((f for f in self._recordings.files() if f.get("path") == path), None)
        if item is None:
            return
        if name == desk.PLAY:
            self._window.screens.play(item, "recording", {"start_over": False})
        elif name == desk.FOLDER:
            system_open.open_folder(os.path.dirname(path))
        elif name == desk.DELETE:
            self._window.screens.delete_recording(item)

    def _place_button(self, path: str, name: str) -> None:
        config = self._window.config_manager
        if name == desk.OPEN:
            if not system_open.open_folder(path):
                self._window.screens.notify(translate(recording.NO_RECORDINGS))
            return
        if name == desk.DEFAULT:
            recording.use_place(config, path)
            self._window.screens.notify(translate(PLACE_USED, folder=path))
        elif name == desk.REMOVE:
            recording.remove_place(config, path)
        else:
            return
        self._save(config)

    def _add_button(self, section: str, name: str) -> None:
        if name == desk.DISMISS_ALL:
            self._schedule().dismiss_all()
        elif name == desk.ADD_PLACE:
            self.add_place()
        elif name == desk.ADD_TIMER:
            self.record_at()
        elif name == desk.OPEN_GUIDE:
            self._window.screens.back_to_tv()
            self._window.controller.handle(Action.GUIDE)

    def _play_path(self, path: str) -> None:
        item = next((f for f in self._recordings.files() if f.get("path") == path), None)
        if item is not None:
            self._window.screens.play(item, "recording", {"start_over": False})

    def _save(self, config: Any) -> None:
        try:
            config.save_config()
        except Exception as e:  # the next save writes it
            logger.warning("The recording places could not be saved: %s", type(e).__name__)
        self.refresh_files()

    # ---- dialogs ------------------------------------------------------------------------------

    def add_place(self) -> None:
        window = self._window
        chosen = QFileDialog.getExistingDirectory(
            window, translate(ADD_PLACE_TITLE), os.path.expanduser("~")
        )
        if not chosen or window.closing():
            return
        chosen = os.path.normpath(chosen)
        if recording.add_place(window.config_manager, chosen):
            window.screens.notify(translate(PLACE_ADDED, folder=chosen))
            self._save(window.config_manager)
        self._view.set_filter(desk.PLACES)

    def records_now(self, item_id: str) -> bool:
        """Schedule item `item_id` records now (its Edit… waits for its end)."""
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        return any(detail[5] == item_id for detail in scheduler.recording_details())

    def edit(self, item_id: str) -> None:
        """Edit… on a one-time recording or a timer: Record at a time, filled in with it."""
        item = self._schedule().item(item_id)
        if item is None or item.get("kind") not in (rs.ONCE, rs.TIMER):
            return
        if self.records_now(item_id):
            return  # as the page's Edit…: not while it records (its window is the recording's)
        channel = str((item.get("channel") or {}).get("name") or "")
        if item.get("kind") == rs.ONCE:
            start = datetime.fromtimestamp(float(item.get("start") or 0))
            stop = datetime.fromtimestamp(float(item.get("stop") or 0))
            days: tuple[int, ...] = ()
        else:
            begin, minutes = int(item.get("begin") or 0), int(item.get("minutes") or 0)
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            start = today + timedelta(minutes=begin)
            stop = start + timedelta(minutes=minutes)
            days = tuple(item.get("days") or ())
        dialog = RecordTimerDialog(
            channel,
            start=start,
            stop=stop,
            title=str(item.get("title") or ""),
            days=days,
            save_text=translate(SAVE_TEXT),
            parent=self._window,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def chosen() -> None:
            choice = dialog.choice()
            if choice is None or self._window.closing():
                return
            if choice.days:
                fields: dict[str, Any] = dict(
                    title=choice.title, begin=choice.begin, minutes=choice.minutes, days=choice.days
                )
            else:
                fields = dict(
                    title=choice.title or rs.slot_text(choice.begin, choice.minutes),
                    start=choice.start().timestamp(),
                    stop=choice.stop().timestamp(),
                )
            scheduler = self._scheduler()
            if scheduler is not None:
                edited = scheduler.edit(item_id, **fields)
            else:
                edited = self._schedule().edit(item_id, **fields)
                self._window.schedule_changed()
            self._window.screens.notify(translate(EDITED_TEXT if edited else EDIT_FAILED))
            self.refresh()

        dialog.accepted.connect(chosen)
        self._dialog = dialog
        dialog.open()

    def record_at(self) -> None:
        """Record at a time… with a channel to choose: the live channels of the list's source,
        the one playing first."""
        window = self._window
        channels = list(window.catalog.items("live", ALL_GROUP_ID) or [])
        if not channels:
            window.screens.notify(translate(NO_CHANNELS))
            return
        playing = window.controller.playing_channel()
        dialog = RecordTimerDialog(
            str((playing or {}).get("name") or ""),
            channels=channels,
            chosen=playing,
            parent=window,
        )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def chosen() -> None:
            choice, channel = dialog.choice(), dialog.chosen_channel()
            if choice is not None and channel is not None and not window.closing():
                window.schedule_timer(channel, choice)
                self.refresh()

        dialog.accepted.connect(chosen)
        self._dialog = dialog
        dialog.open()


class _Unread:
    """A place not read yet: nothing known of it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.reachable = True
        self.free: Optional[int] = None


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _inside(path: str, folder: str) -> bool:
    return bool(path) and _same(os.path.dirname(path), folder)
