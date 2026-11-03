"""The player's sound and subtitles (2.2): tracks, files and their sync, on two panels.

The transport's ♫ opens the Sound panel (ui/overlays/track_panel.py) above it: the sound's
tracks to pick from (with two or more) and a Sync row that moves the sound against the
picture in steps of 50 ms, up to 5 s either way: "Sound 150 ms later" plays it later (for a
sound that comes too early), "earlier" the other way, and Reset puts it back in sync. J and K
do the same as − and + from anywhere on the player, with a toast (the panel follows while it
shows). The subtitles button opens the Subtitles panel the same way: Off and the subtitle
tracks, "Load a file…" (a file dialog in the folder used last), and, while a subtitle shows,
its Sync row, which Shift+J and Shift+K step. A subtitle file dropped on the window while
something plays loads the same way. While a panel shows, the keys are its own: ↑/↓ move
between its rows, Enter chooses, ←/→ step the sync and Esc closes it; any other key closes it
and does what it does (the sync keys leave it open). The player controls stay up while a
panel shows, its button lit, and it closes with them.

Everything acts on the large picture. A live channel's sound sync is kept for that channel
(`services.sound_sync`, written off the GUI thread): it applies whenever a player opens the
channel, large or small (`prepare`, `prepare_small`), and the player keeps it through each
reopen of the same channel (a reconnect, a time-shift seek, a go-live). A channel's sync is
always read from the store, never from the player: while a tune waits for the old stream to
let go, the player still has the old channel's. A swap moves nothing: each player keeps
playing its channel with its sync, so the sync follows the channel. A film's or an episode's
stays with its player while that title plays (a retry of it keeps it too) and is never
stored: the next title starts in sync. Subtitle files and the subtitles' sync are the
playback's alone: another title or channel starts without them, and a file chosen in the
dialog is dropped when a zap or a swap came while it was open.
For a film or an episode the panel also offers "Search OpenSubtitles…" (ui/subtitle_search.py,
with `online`), and at its top the subtitles downloaded for that title last, "Name.srt
(OpenSubtitles)", until they show.
Without the panels (tests), the buttons cycle the tracks with a toast, as before 2.2.

The Sound panel names each track by its language, in the language's own name ("Türkçe"), and
by its codec and channels too where two would read alike ("English · AC-3 5.1"); a track whose
stream names no language reads "Track 2 · language not given". One track shows too: what it
is, ticked.

AI captions (2.9, `captions`: the window's CaptionsController, once it has one): while they can
be offered for what plays, the Subtitles panel has an "AI captions" row, ticked while they are
on, saying at its right what they wait for ("Downloading 34%"), and while they are on a
"Language  Türkçe · audio track ›" row that opens the language list in the panel (Auto, then
each language; "‹ Subtitles" or ← goes back). One subtitle source at a time: a track, Off or a
file turns AI captions off, and AI captions turned on turn the track shown off. Turning them on
closes the panel first, as their first time asks a question (the download); it opens again once
they are on or on their way.

The phone remote's Subtitles and Sound sheets (2.9, ui/remote_controller.py) list the same
choices, named the same (`sound_choices`, `subtitle_choices`, `captions_choice`), and choose
them as the rows do (`choose_sound`, `choose_subtitles`, `set_captions`,
`choose_caption_language`); an open panel follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
import logging
import os
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer

from services import i18n
from services.captions.languages import own_name, qwen_name
from services.i18n import N_, tr, translate
from services.sound_sync import LIMIT_MS, STEP_MS, SoundSync, clamp
from services.zapping import ZapContext
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.track_panel import PanelModel, PanelRow, PanelSync
from ui.player_keys import PlayerKeys

logger = logging.getLogger(__name__)


def _same_series(previous: Optional[Target], target: Target) -> bool:
    """`target` is another episode of the series `previous` was one of (its next, mostly)."""
    if previous is None or previous.section != "series" or target.section != "series":
        return False
    before, now = (dict(t.context or {}).get("series") for t in (previous, target))
    return ZapContext.same(before, now)


AUDIO, SUBTITLES = "audio", "subtitles"  # the transport's buttons, and their panels
# The panels' titles (English: sync_text() tells them apart; translate() shows them).
SOUND = N_("Sound")  # i18n: the Sound panel's title (the audio tracks and their sync)
SUBTITLES_TITLE = N_("Subtitles")  # i18n: the Subtitles panel's title
IN_SYNC = N_("In sync")  # i18n: the Sound or Subtitles panel's sync row: no delay
OFF = N_("Off")  # i18n: the Subtitles panel's row that shows no subtitles
LOAD_FILE = "load_file"  # the Subtitles panel's "Load a file…" row
LOAD_TEXT = N_("Load a file…")  # i18n: the Subtitles panel's row that opens a file dialog
SEARCH_ONLINE = "search_online"  # its "Search OpenSubtitles…" row
SEARCH_ONLINE_TEXT = N_("Search OpenSubtitles…")  # i18n: the Subtitles panel's row
REMEMBERED = "remembered"  # the title's pick from OpenSubtitles, at its top
NO_AUDIO_TRACKS = N_("No audio tracks")  # i18n: a toast
NO_SUBTITLES = N_("No subtitles")  # i18n: a toast
SUBTITLES_OFF = N_("Subtitles are off")  # i18n: a toast
# 2.9: AI captions' rows in the Subtitles panel, and its language list.
AI_CAPTIONS = "ai_captions"  # on or off
AI_LANGUAGE = "ai_language"  # the language they hear: opens the list
AI_BACK = "ai_back"  # the list's way back to the Subtitles panel
AI_PICK = "ai_pick"  # a language of the list: (AI_PICK, "auto" or Qwen's name)
# i18n: the Subtitles panel's row: captions written on this computer by a speech model, in the
# language spoken; a Settings group's title
AI_CAPTIONS_TEXT = N_("AI captions")
LANGUAGE_TEXT = N_("Language")
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
# i18n: the file dialog's filter; keep the part in brackets as it is
SUBTITLE_FILTER = N_("Subtitles (*.srt *.ass *.ssa *.vtt *.sub)")
REFRESH_MS = 500  # a panel follows what libVLC reports meanwhile (a track that came)
ERROR_MS = 6000
_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
_SYNC_KEYS = (
    Action.SOUND_EARLIER,
    Action.SOUND_LATER,
    Action.SUBTITLES_EARLIER,
    Action.SUBTITLES_LATER,
)
_KEYS = {AUDIO: ("J", "K"), SUBTITLES: ("Shift+J", "Shift+K")}  # earlier, later


def sync_text(noun: str, ms: int) -> str:
    """The Sync row's words: "In sync", "Sound 150 ms later" or "Sound 1,050 ms earlier".

    `noun` is SOUND or SUBTITLES_TITLE (in English)."""
    if not ms:
        return translate(IN_SYNC)
    amount = i18n.number(abs(ms))
    if noun == SUBTITLES_TITLE:
        if ms > 0:
            return tr("Subtitles {ms} ms later", ms=amount)  # i18n: ms: milliseconds
        return tr("Subtitles {ms} ms earlier", ms=amount)  # i18n: ms: milliseconds
    if ms > 0:
        return tr("Sound {ms} ms later", ms=amount)  # i18n: ms: milliseconds
    return tr("Sound {ms} ms earlier", ms=amount)  # i18n: ms: milliseconds


def toast_text(noun: str, ms: int) -> str:
    """A sync key's toast: "Sound in sync", else as the Sync row says it."""
    if ms:
        return sync_text(noun, ms)
    if noun == SUBTITLES_TITLE:
        return tr("Subtitles in sync")  # i18n: a toast
    return tr("Sound in sync")  # i18n: a toast


def _subtitles_text(name: str) -> str:
    """A toast naming the subtitles that show: a file's or a track's name, or Off."""
    return tr("Subtitles: {name}", name=name)  # i18n: a toast


def subtitle_paths(paths: Iterable[str]) -> list[str]:
    """The subtitle files among `paths` (.srt, .ass, .ssa, .vtt, .sub)."""
    return [p for p in paths if p and os.path.splitext(p)[1].lower() in SUBTITLE_EXTENSIONS]


def auto_language_text() -> str:
    """AI captions' Auto: the language the audio track or the channel names, else the default
    one, else the speech model's guess."""
    return tr("Auto", ctx="a language")  # i18n: the language AI captions hear: found by itself


# libVLC's four-character codes of the usual audio codecs, as people know them.
_CODECS = {
    "mp4a": "AAC",
    "a52 ": "AC-3",
    "eac3": "E-AC-3",
    "mpga": "MP2",
    "mp3 ": "MP3",
    "dts ": "DTS",
    "opus": "Opus",
    "vorb": "Vorbis",
    "flac": "FLAC",
    "alac": "ALAC",
    "mlp ": "TrueHD",
    "trhd": "TrueHD",
    "lpcm": "PCM",
    "araw": "PCM",
    "s16l": "PCM",
    "s16b": "PCM",
    "s24l": "PCM",
    "f32l": "PCM",
}
_LAYOUTS = {3: "2.1", 4: "4.0", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}  # channels as said
# Codes that name no language: undetermined, several, none, missing; DVB's local ones
# (qaa–qtz: "original", "audio description") say no language either.
_NO_LANGUAGE = {"und", "mul", "zxx", "mis"}


def _bracketed(name: str) -> str:
    """The language libVLC puts in a track's name ("Track 2 - [English]": "English"), or ""."""
    _head, bracket, rest = name.rpartition("[")
    return rest[:-1].strip() if bracket and rest.endswith("]") else ""


def _names_nothing(code: str) -> bool:
    base = code.strip().casefold().replace("_", "-").split("-", 1)[0]
    return not base or base in _NO_LANGUAGE or (len(base) == 3 and "qaa" <= base <= "qtz")


def language_label(code: str) -> str:
    """A language's own name ("Türkçe") from its ISO 639 code or its English name (libVLC's
    "[Turkish]"); "" when it names none."""
    if not code or _names_nothing(code):
        return ""
    qwen = qwen_name(code)
    if qwen is not None:
        return own_name(qwen)
    from PySide6.QtCore import QLocale

    base = code.strip().replace("_", "-").split("-", 1)[0]
    language = QLocale.codeToLanguage(base, QLocale.LanguageCodeType.AnyLanguageCode)
    if language != QLocale.Language.AnyLanguage:
        locale = QLocale(language)
        own = str(locale.nativeLanguageName())
        if own:
            return str(locale.toUpper(own[:1])) + own[1:]
    return code.strip() if len(code.strip()) > 3 else ""  # an English name Qt has no code for


def _channels_text(channels: int) -> str:
    if channels == 1:
        return tr("mono")  # i18n: an audio track's channels: one
    if channels == 2:
        return tr("stereo")  # i18n: an audio track's channels: two
    return _LAYOUTS.get(channels, "")


def _paired(tracks: list[tuple[int, str]], infos: Optional[list]) -> dict[int, Any]:
    """Each track's AudioTrackInfo: by id where the ids agree, else in order (an MPEG-TS
    stream's media ids are not its player's); {} when they can't be told apart."""
    if not infos:
        return {}
    by_id = {info.id: info for info in infos}
    ids = [track_id for track_id, _name in tracks]
    if all(track_id in by_id for track_id in ids):
        return {track_id: by_id[track_id] for track_id in ids}
    if len(infos) == len(ids):
        return dict(zip(ids, infos))
    return {}


def _counts(texts: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for text in texts:
        counts[text] = counts.get(text, 0) + 1
    return counts


def audio_names(tracks: list[tuple[int, str]], infos: Optional[list] = None) -> dict[int, str]:
    """The Sound panel's text for each of `tracks` ((id, libVLC's name), none of them -1), from
    what `infos` (PlaybackSession.audio_track_info) says of them: the language's own name, with
    the codec and channels where two tracks would read alike; "Track 2 · language not given"
    for one whose stream names no language. With nothing to tell (a player that can't say),
    libVLC's name."""
    paired = _paired(tracks, infos)
    names: dict[int, str] = {}
    for number, (track_id, name) in enumerate(tracks, 1):
        info = paired.get(track_id)
        code = info.language_code if info is not None else ""
        language = language_label(code) or language_label(_bracketed(name))
        if language:
            names[track_id] = language
        elif info is not None:
            # i18n: an audio track's number (the Sound panel); n: 1, 2, …
            track = tr("Track {n}", n=number)
            # i18n: the Sound panel: the stream names no language for this audio track
            names[track_id] = track + " · " + tr("language not given")
        else:
            names[track_id] = name
    counts = _counts(names.values())
    for track_id in names:
        info = paired.get(track_id)
        if counts[names[track_id]] < 2 or info is None:
            continue
        codec = _CODECS.get(info.codec, info.codec.strip().upper())
        details = " ".join(part for part in (codec, _channels_text(info.channels)) if part)
        if details:
            names[track_id] = f"{names[track_id]} · {details}"
    counts = _counts(names.values())
    for number, (track_id, _name) in enumerate(tracks, 1):
        if counts[names[track_id]] > 1:
            names[track_id] = f"{names[track_id]} · " + tr("Track {n}", n=number)
    return names


@dataclass(frozen=True)
class CaptionsChoice:
    """AI captions as the Subtitles panel offers them (2.9), for the phone remote's sheet."""

    on: bool  # their row is ticked
    going: bool  # on their way: the speech model downloads (a tap calls that off)
    status: str  # what they wait for, as the row says it ("Downloading 40%"; "": nothing)
    language: str  # the language they hear: "auto", or Qwen's name
    language_text: str  # the Language row's note while on: "Türkçe · automatic"
    languages: tuple[tuple[str, str], ...]  # while on, the list: ("auto", "Auto"), (name, own)


class TrackControls(QObject):
    def __init__(
        self,
        *,
        sound_panel,
        transport,
        session: Callable[[], Any],
        current: Callable[[], Optional[Target]],
        playing: Callable[[], bool],
        keys: PlayerKeys,
        store: SoundSync,
        toast: Callable[..., None],
        show_transport: Callable[[], None],
        subtitle_panel=None,
        pick_file: Optional[Callable[[str], str]] = None,
        config: Any = None,
        online: Any = None,
        captions: Optional[Callable[[], Any]] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`sound_panel` and `subtitle_panel` are TrackPanels (None: toasts); `session()` the
        large picture's player, `current()` what it plays and `playing()` whether it has
        media; `keys` each player's channel key in `store`. `pick_file(folder)` asks for a
        subtitle file ("" when none was chosen; None: no "Load a file…"); `config` keeps its
        folder (`subtitle_folder`). `online` is the OpenSubtitles search (an OnlineSubtitles;
        None: no "Search OpenSubtitles…"). `captions()` is the window's CaptionsController
        (None: none yet, or no AI captions)."""
        super().__init__(parent)
        self._panels = {
            name: panel
            for name, panel in ((AUDIO, sound_panel), (SUBTITLES, subtitle_panel))
            if panel is not None
        }
        self._transport = transport
        self._session = session
        self._current = current
        self._playing = playing
        self._keys = keys  # each player's channel and its key as it opened (shared)
        self._store = store
        self._toast = toast
        self._show_transport = show_transport
        self._pick_file = pick_file
        self._config = config
        self._online = online
        self._captions = captions
        self._watched: Any = None  # the CaptionsController whose `changed` reaches us
        self._captions_on = False  # what it said last: turned on since, the track shown goes
        self._level = ""  # the Subtitles panel's list: "" its own, AI_LANGUAGE the languages
        self._refresh = QTimer(self, interval=REFRESH_MS)
        self._refresh.timeout.connect(self._follow_player)

    def wire(self) -> None:
        transport = self._transport
        transport.audioRequested.connect(self.open_sound)
        transport.subtitleRequested.connect(self.open_subtitles)
        transport.bandChanged.connect(self._follow_band)  # its controls moved (the bar came)
        transport.installEventFilter(self)  # its hide closes the panels
        for name, panel in self._panels.items():
            panel.set_controls(transport, transport.control_at)  # another button acts at once
            panel.rowChosen.connect(partial(self._on_row, name))
            panel.syncStepped.connect(partial(self._step, name))
            panel.syncReset.connect(partial(self._reset, name))
            panel.closed.connect(partial(self._on_closed, name))

    # ---- what opens ---------------------------------------------------------------------------

    def prepare(self, session, target: Target, previous: Optional[Target]) -> None:
        """`target` is about to open in `session` (the large picture's player, its old stream
        stopped): its sound as the viewer set it last, no subtitle file and the subtitles in
        sync. The same title or channel again (a retry, a quality change) keeps what the player
        has, the tracks chosen too; the next episode of a series keeps their language."""
        if self._online is not None:
            self._online.follow(target)  # the OpenSubtitles search was for the title before
        if previous is not None and previous.same_as(target):
            return
        if not target.live:
            self._keys.forget(session)
            session.set_audio_delay(0)
        else:
            session.set_audio_delay(self._store.audio_ms(self._keys.opens(session, target.item)))
        session.clear_subtitles()
        session.forget_tracks(keep_language=_same_series(previous, target))

    def prepare_small(self, session, channel: dict) -> None:
        """A channel opens in the small picture's player: its sound as set last."""
        session.set_audio_delay(self._store.audio_ms(self._keys.opens(session, channel)))
        session.clear_subtitles()
        session.forget_tracks()

    # ---- the keys -----------------------------------------------------------------------------

    def visible(self) -> bool:
        return self._shown() is not None

    def handle(self, action: Action, key: Optional[int] = None) -> bool:
        """The keys while a panel shows (False: not its own, or none shows). A key that is not
        the panel's closes it and goes on (the sync keys leave it open)."""
        shown = self._shown()
        if shown is None:
            return False
        panel = shown[1]
        if action is Action.ESCAPE:
            panel.dismiss()
        elif action is Action.CH_PREV and key == _UP:
            panel.move_focus(-1)
        elif action is Action.CH_NEXT and key == _DOWN:
            panel.move_focus(1)
        elif action is Action.SEEK_BACK and self._level and shown[0] == SUBTITLES:
            self._back_to_subtitles()  # the language list: back to the Subtitles panel
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            panel.step(-1 if action is Action.SEEK_BACK else 1)
        elif action is Action.OK:
            panel.activate()
        else:
            if action not in _SYNC_KEYS:
                panel.dismiss()
            return False
        return True

    def nudge_sound(self, steps: int) -> None:
        """J (-1) and K (+1): the large picture's sound 50 ms earlier or later, with a toast
        (the panel shows it instead while it is open)."""
        if self._playing() and self._current() is not None:
            ms = self._set_delay(AUDIO, self._delay(AUDIO) + steps * STEP_MS)
            self._said(AUDIO, toast_text(SOUND, ms))

    def nudge_subtitles(self, steps: int) -> None:
        """Shift+J (-1) and Shift+K (+1): the subtitles 50 ms earlier or later, while one
        shows."""
        if not self._playing() or self._current() is None:
            return
        session = self._session()
        if session.subtitle_track() == -1:
            self._toast(translate(SUBTITLES_OFF))
            return
        ms = self._set_delay(SUBTITLES, session.subtitle_delay() + steps * STEP_MS)
        self._said(SUBTITLES, toast_text(SUBTITLES_TITLE, ms))

    def _said(self, name: str, text: str) -> None:
        panel = self._panels.get(name)
        if panel is not None and panel.isVisible():
            self._open(name)  # it shows the new value
        else:
            self._toast(text)

    # ---- subtitle files -----------------------------------------------------------------------

    def load_subtitle_file(
        self, path: str, *, say: bool = True, remember_folder: bool = True
    ) -> bool:
        """Show the subtitle file at `path` with what plays (chosen); False, said, when it
        could not be loaded. With `remember_folder`, "Load a file…" opens in its folder next."""
        if not self._playing():
            return False
        name = os.path.basename(path)
        if not self._session().add_subtitle_file(path):
            self._toast(tr("Couldn't load the subtitles {name}", name=name), "error", ERROR_MS)
            return False
        if remember_folder and self._config is not None:  # the dialog opens there next time
            self._config.subtitle_folder = os.path.dirname(os.path.abspath(path))
        self._captions_off()  # one subtitle source at a time
        if say:
            self._toast(_subtitles_text(name))
        return True

    def accepts_drop(self, paths: Iterable[str]) -> bool:
        """Whether a drop of `paths` would load subtitles: one is a subtitle file and
        something plays."""
        return bool(subtitle_paths(paths)) and self._playing()

    def drop(self, paths: Iterable[str]) -> bool:
        """Files dropped on the window: their subtitle files load (the last one shows)."""
        files = subtitle_paths(paths)
        if not files or not self._playing():
            return False
        loaded = [path for path in files if self.load_subtitle_file(path, say=False)]
        if loaded:
            self._said(SUBTITLES, _subtitles_text(os.path.basename(loaded[-1])))
        return bool(loaded)

    def _choose_file(self) -> None:
        """ "Load a file…": the panel gives way to the dialog and comes back with the file."""
        pick = self._pick_file
        if pick is None:
            return
        self.close()
        folder = str(getattr(self._config, "subtitle_folder", "") or "")
        target, session = self._current(), self._session()
        path = pick(folder)  # the dialog's own event loop: a zap or a swap may come meanwhile
        moved = self._current() is not target or self._session() is not session
        if not path or not self._playing() or moved:
            return  # the file was for what played when it was asked for
        if self.load_subtitle_file(path, say=False):
            self._show_transport()  # it faded during the dialog
            self.open_subtitles()

    # ---- the panels ---------------------------------------------------------------------------

    def open_sound(self) -> None:
        """The transport's ♫: the Sound panel above it (or, without one, the next track)."""
        self._open(AUDIO)

    def open_subtitles(self) -> None:
        """The transport's subtitles button: the Subtitles panel (or the next track)."""
        self._level = ""
        self._open(SUBTITLES)

    def refresh(self) -> None:
        """What the panels show changed (the pictures swapped): the large one's."""
        if self._online is not None:
            self._online.follow()
        shown = self._shown()
        if shown is not None:
            if self._playing():
                self._open(shown[0])
            else:
                self.close()

    def close(self) -> None:
        for panel in self._panels.values():
            panel.dismiss()

    def _open(self, name: str) -> None:
        if not self._playing():
            return
        panel = self._panels.get(name)
        if panel is None:
            self._cycle(name)
            return
        for other, shown in self._panels.items():
            if other != name:
                shown.dismiss()
        self._transport.hold(name)
        panel.show_panel(self._model(name), self._anchor(name), self._floor(), name)
        self._refresh.start()

    def _shown(self) -> Optional[tuple[str, Any]]:
        for name, panel in self._panels.items():
            if panel.isVisible():
                return name, panel
        return None

    def _model(self, name: str) -> PanelModel:
        return self._sound_model() if name == AUDIO else self._subtitle_model()

    def _sync(self, name: str, ms: int) -> PanelSync:
        noun = SOUND if name == AUDIO else SUBTITLES_TITLE
        earlier, later = _KEYS[name]
        return PanelSync(
            sync_text(noun, ms),
            max(sync_text(noun, -LIMIT_MS), sync_text(noun, LIMIT_MS), key=len),  # the widest
            earlier=ms > -LIMIT_MS,
            later=ms < LIMIT_MS,
            reset=ms != 0,
            earlier_tip=key_hint(sync_text(noun, -STEP_MS), earlier),
            later_tip=key_hint(sync_text(noun, STEP_MS), later),
        )

    def _sound_model(self) -> PanelModel:
        """The tracks by their language (one track too: what it is), then the Sync row."""
        rows = tuple(PanelRow(i, name, on) for i, name, on in self.sound_choices())
        return PanelModel(translate(SOUND), rows, self._sync(AUDIO, self._delay(AUDIO)))

    def _subtitle_model(self) -> PanelModel:
        captions = self._offered()
        if self._level == AI_LANGUAGE and captions is not None and captions.enabled():
            return self._language_model(captions)
        self._level = ""
        session = self._session()
        shown = session.subtitle_track()
        tracks = self.subtitle_choices()
        online, target = self._online, self._current()
        searchable = online is not None and online.available(target)
        ai_on = captions is not None and bool(captions.enabled())
        rows = []
        if searchable:
            pick = online.remembered(target)
            if pick is not None and pick.name not in {name for _i, name, _on in tracks}:
                rows.append(
                    PanelRow(REMEMBERED, online.row_text(pick), action=True, icon="subtitles")
                )
        rows.append(PanelRow(-1, translate(OFF), shown == -1 and not ai_on))
        rows += [PanelRow(i, name, on) for i, name, on in tracks]
        if captions is not None:
            rows += self._caption_rows(captions)
        if self._pick_file is not None:
            rows.append(PanelRow(LOAD_FILE, translate(LOAD_TEXT), action=True))
        if searchable:
            text = translate(SEARCH_ONLINE_TEXT)
            rows.append(PanelRow(SEARCH_ONLINE, text, action=True, icon="search"))
        sync = None if shown == -1 else self._sync(SUBTITLES, session.subtitle_delay())
        return PanelModel(translate(SUBTITLES_TITLE), tuple(rows), sync)

    # ---- the panels' choices, for the phone remote too (2.9) ---------------------------------

    def offers(self) -> bool:
        """Something plays in the large picture: its tracks can be chosen."""
        return bool(self._playing()) and self._current() is not None

    def sound_choices(self) -> list[tuple[int, str, bool]]:
        """The Sound panel's tracks, as it names them: (id, name, playing)."""
        session = self._session()
        tracks = [t for t in session.audio_tracks() if t[0] != -1]  # -1: no sound
        playing = session.audio_track()
        info = getattr(session, "audio_track_info", None)
        names = audio_names(tracks, info() if callable(info) and tracks else None)
        return [(i, names[i], i == playing) for i, _name in tracks]

    def subtitle_choices(self) -> list[tuple[int, str, bool]]:
        """The Subtitles panel's tracks, Off aside: (id, name, shown)."""
        session = self._session()
        shown = session.subtitle_track()
        return [(i, name, i == shown) for i, name in session.subtitle_tracks() if i != -1]

    def captions_choice(self) -> Optional[CaptionsChoice]:
        """AI captions as the Subtitles panel offers them (None: it doesn't, for what plays)."""
        captions = self._offered()
        if captions is None or not self.offers():
            return None
        on = bool(captions.enabled())
        languages: tuple[tuple[str, str], ...] = ()
        if on:  # the Language row and its list, while they are on
            languages = (("auto", auto_language_text()), *captions.languages())
        return CaptionsChoice(
            on=on,
            going=bool(captions.downloading()),
            status=str(captions.status_text() or ""),
            language=str(captions.language()[0]),
            language_text=self._language_text(captions) if on else "",
            languages=languages,
        )

    def choose_sound(self, track_id: int) -> bool:
        """A Sound track by its id, as the panel's row chooses it (False: none such now)."""
        if not self.offers() or track_id not in {i for i, _name, _on in self.sound_choices()}:
            return False
        self._session().set_audio_track(track_id)
        self._follow_player()
        return True

    def choose_subtitles(self, track_id: int) -> bool:
        """A subtitle track by its id, or Off (-1), as the panel's rows choose them: AI
        captions go off (one subtitle source at a time). False: no such track now."""
        if not self.offers():
            return False
        if track_id != -1 and track_id not in {i for i, _n, _on in self.subtitle_choices()}:
            return False
        self._session().set_subtitle_track(track_id)
        self._captions_off()
        self._follow_player()
        return True

    def set_captions(self, on: bool) -> None:
        """AI captions on or off, as their row turns them, said rather than toggled (the phone
        remote's: a second "on" changes nothing, an "off" also calls off their download). The
        first time asks the download's question first; the panels give way to it."""
        captions = self._offered()
        if captions is None or not self.offers():
            return
        wanted = bool(captions.enabled() or captions.downloading())
        if on == wanted or (on and captions.asking()):  # asked already: its answer decides
            return
        captions.toggle()
        self._one_source(captions)
        if captions.asking():
            self.close()  # the question in front, as their row's own
        else:
            self._follow_player()

    def choose_caption_language(self, name: str) -> bool:
        """The language AI captions hear, while they are on ("auto": found by itself), as the
        language list picks it (False: they are off, or no such language)."""
        captions = self._offered()
        if captions is None or not self.offers() or not captions.enabled():
            return False
        if name != "auto" and name not in {qwen for qwen, _own in captions.languages()}:
            return False
        captions.set_language(name)
        self._follow_player()
        return True

    # ---- AI captions (2.9) -------------------------------------------------------------------

    def _captions_now(self) -> Any:
        """The window's CaptionsController (None: none); its `changed` reaches us from now on."""
        captions = self._captions() if self._captions is not None else None
        if captions is not None and captions is not self._watched:
            changed = getattr(captions, "changed", None)
            if changed is not None:
                changed.connect(self._captions_changed)
            self._watched = captions
            self._captions_on = bool(captions.enabled())
        return captions

    def _offered(self) -> Any:
        """The CaptionsController while AI captions can be offered for what plays, else None."""
        captions = self._captions_now()
        return captions if captions is not None and captions.available() else None

    def _caption_rows(self, captions) -> list[PanelRow]:
        """ "AI captions" (ticked while on, saying what they wait for), and while they are on
        "Language  Türkçe · audio track ›", which opens the language list."""
        on = bool(captions.enabled())
        status = str(captions.status_text() or "")
        # Ticked while their download is on its way too, as on the phone: a tap then calls it
        # off, never reads as "turn them on" (2.9: one did, and cancelled it while unpacking).
        ticked = on or bool(captions.downloading())
        rows = [PanelRow(AI_CAPTIONS, translate(AI_CAPTIONS_TEXT), ticked, note=status)]
        if on:
            note = self._language_text(captions)
            rows.append(PanelRow(AI_LANGUAGE, translate(LANGUAGE_TEXT), note=note, opens=True))
        return rows

    @staticmethod
    def _language_text(captions) -> str:
        """The Language row's note: the language's own name, and where it came from."""
        name, source = captions.language()
        shown = auto_language_text() if name == "auto" else own_name(name)
        return f"{shown} · {source}" if source else shown

    def _language_model(self, captions) -> PanelModel:
        """The language list: "‹ Subtitles", Auto, then each language by its own name; the one
        AI captions hear now ticked."""
        current = captions.language()[0]
        rows = [
            PanelRow(AI_BACK, translate(SUBTITLES_TITLE), action=True, icon="chevron_left"),
            PanelRow((AI_PICK, "auto"), auto_language_text(), current == "auto"),
        ]
        rows += [
            PanelRow((AI_PICK, name), shown, current == name)
            for name, shown in captions.languages()
        ]
        return PanelModel(translate(LANGUAGE_TEXT), tuple(rows))

    def _toggle_captions(self) -> None:
        """The AI captions row: on or off. Turning them on closes the panel first (their first
        time asks a question); it opens again once they are on or on their way."""
        captions = self._offered()
        if captions is None:
            return
        was_on = bool(captions.enabled())
        if not was_on:
            self.close()
        captions.toggle()
        self._one_source(captions)
        if self._playing() and (was_on or captions.enabled() or captions.status_text()):
            self._open(SUBTITLES)

    def _one_source(self, captions) -> None:
        """AI captions on: the subtitle track shown goes (one subtitle source at a time)."""
        on = bool(captions.enabled())
        if on and self._playing():
            session = self._session()
            if session.subtitle_track() != -1:
                session.set_subtitle_track(-1)
        self._captions_on = on

    def _captions_off(self) -> None:
        """A subtitle track, Off or a file chosen: AI captions go off."""
        captions = self._captions_now()
        if captions is not None and captions.enabled():
            captions.toggle()
            self._captions_on = False

    def _captions_changed(self) -> None:
        """The CaptionsController's rows or state changed: turned on since (a download that
        finished), the track shown goes; the Subtitles panel follows while it shows."""
        captions = self._watched
        if captions is None:
            return
        if captions.enabled() and not self._captions_on:
            self._one_source(captions)
        self._captions_on = bool(captions.enabled())
        shown = self._shown()
        if shown is not None and shown[0] == SUBTITLES and self._playing():
            shown[1].set_model(self._model(SUBTITLES))

    def _back_to_subtitles(self) -> None:
        self._level = ""
        self._open(SUBTITLES)

    def _on_caption_row(self, key) -> bool:
        """An AI captions row's or the language list's choice (False: none of theirs)."""
        if key == AI_CAPTIONS:
            self._toggle_captions()
        elif key == AI_LANGUAGE:
            self._level = AI_LANGUAGE
            self._open(SUBTITLES)
        elif key == AI_BACK:
            self._back_to_subtitles()
        elif isinstance(key, tuple) and len(key) == 2 and key[0] == AI_PICK:
            captions = self._offered()
            if captions is not None:
                captions.set_language(key[1])
            self._back_to_subtitles()
        else:
            return False
        return True

    def _anchor(self, button: str) -> Optional[QRectF]:
        """The transport's button, in the host's coordinates (None: it is not shown)."""
        try:
            rect = self._transport.button_rect(button)
        except KeyError:
            return None
        return QRectF(rect).translated(self._transport.pos())

    def _floor(self) -> Optional[int]:
        transport = self._transport
        return transport.controls_top() if transport.isVisible() else None

    def _on_row(self, name: str, key) -> None:
        if not self._playing():
            return
        if key == LOAD_FILE:
            self._choose_file()
            return
        if name == SUBTITLES and self._on_caption_row(key):
            return
        if key in (SEARCH_ONLINE, REMEMBERED):
            target = self._current()
            self.close()  # the search's page, or a toast once the file shows
            if self._online is not None and target is not None:
                if key == SEARCH_ONLINE:
                    self._online.open()
                else:
                    self._online.load_remembered(target)
            return
        session = self._session()
        if name == AUDIO:
            session.set_audio_track(int(key))
        else:
            session.set_subtitle_track(int(key))
            self._captions_off()  # one subtitle source at a time
        self._open(name)

    def _step(self, name: str, steps: int) -> None:
        if self._playing():
            self._set_delay(name, self._delay(name) + steps * STEP_MS)
            self._open(name)

    def _reset(self, name: str) -> None:
        if self._playing():
            self._set_delay(name, 0)
            self._open(name)

    def _on_closed(self, name: str) -> None:
        if name == SUBTITLES:
            self._level = ""  # it opens on its own list next time
        if self._transport.held() == name:
            self._transport.hold("")
        if self._shown() is None:
            self._refresh.stop()

    def _follow_band(self) -> None:
        shown = self._shown()
        if shown is not None:
            name, panel = shown
            panel.show_panel(panel.model(), self._anchor(name), self._floor())

    def _follow_player(self) -> None:
        """While a panel shows: what libVLC reports now (a track came, a file was named)."""
        shown = self._shown()
        if shown is None or not self._playing():
            self._refresh.stop()
            return
        name, panel = shown
        panel.set_model(self._model(name))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._transport and event.type() == QEvent.Type.Hide:
            self.close()  # the panels belong to the player controls
        return False

    # ---- the sync -----------------------------------------------------------------------------

    def _delay(self, name: str) -> int:
        """The large picture's sync now: a live channel's sound as its store keeps it (its
        player may still have the channel before's while it opens), else its player's."""
        session = self._session()
        if name == SUBTITLES:
            return int(session.subtitle_delay())
        target = self._current()
        if target is not None and target.live:
            return self._store.audio_ms(self._keys.key_of(session, target.item))
        return int(session.audio_delay())

    def _set_delay(self, name: str, ms: int) -> int:
        """The large picture's sound (or subtitles) `ms` later, clamped. Its player keeps it
        while the title plays, and a live channel's sound sync is kept for the channel."""
        ms = clamp(ms)
        session = self._session()
        if name == SUBTITLES:
            session.set_subtitle_delay(ms)
            return ms
        session.set_audio_delay(ms)
        target = self._current()
        if target is not None and target.live:
            self._store.set_audio_ms(self._keys.key_of(session, target.item), ms)
        return ms

    def _cycle(self, name: str) -> None:
        """Without a panel: the next track, said in a toast."""
        session = self._session()
        if name == AUDIO:
            tracks = [t for t in session.audio_tracks() if t[0] != -1]  # mute disables audio
            playing = session.audio_track()
        else:
            tracks, playing = session.subtitle_tracks(), session.subtitle_track()
        if not tracks:
            self._toast(translate(NO_AUDIO_TRACKS if name == AUDIO else NO_SUBTITLES))
            return
        ids = [t[0] for t in tracks]
        index = ids.index(playing) if playing in ids else -1
        track_id, track = tracks[(index + 1) % len(tracks)]
        if name == AUDIO:
            session.set_audio_track(track_id)
            self._toast(tr("Audio: {track}", track=track))  # i18n: a toast; track: its name
        else:
            session.set_subtitle_track(track_id)
            self._toast(_subtitles_text(translate(OFF) if track_id == -1 else track))
