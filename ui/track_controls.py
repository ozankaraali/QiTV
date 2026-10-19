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
Without the panels (tests), the buttons cycle the tracks with a toast, as before 2.2.
"""

from __future__ import annotations

from functools import partial
import logging
import os
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer

from services.sound_sync import LIMIT_MS, STEP_MS, SoundSync, clamp
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.track_panel import PanelModel, PanelRow, PanelSync

logger = logging.getLogger(__name__)

AUDIO, SUBTITLES = "audio", "subtitles"  # the transport's buttons, and their panels
SOUND, SUBTITLES_TITLE = "Sound", "Subtitles"
IN_SYNC = "In sync"
OFF = "Off"
LOAD_FILE = "load_file"  # the Subtitles panel's "Load a file…" row
LOAD_TEXT = "Load a file…"
NO_AUDIO_TRACKS = "No audio tracks"
NO_SUBTITLES = "No subtitles"
SUBTITLES_OFF = "Subtitles are off"
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sub")
SUBTITLE_FILTER = "Subtitles (*.srt *.ass *.ssa *.vtt *.sub)"
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
    """The Sync row's words: "In sync", "Sound 150 ms later" or "Sound 1,050 ms earlier"."""
    if not ms:
        return IN_SYNC
    return f"{noun} {abs(ms):,} ms {'later' if ms > 0 else 'earlier'}"


def toast_text(noun: str, ms: int) -> str:
    """A sync key's toast: "Sound in sync", else as the Sync row says it."""
    return f"{noun} in sync" if not ms else sync_text(noun, ms)


def subtitle_paths(paths: Iterable[str]) -> list[str]:
    """The subtitle files among `paths` (.srt, .ass, .ssa, .vtt, .sub)."""
    return [p for p in paths if p and os.path.splitext(p)[1].lower() in SUBTITLE_EXTENSIONS]


class TrackControls(QObject):
    def __init__(
        self,
        *,
        sound_panel,
        transport,
        session: Callable[[], Any],
        current: Callable[[], Optional[Target]],
        playing: Callable[[], bool],
        channel_key: Callable[[dict], str],
        store: SoundSync,
        toast: Callable[..., None],
        show_transport: Callable[[], None],
        subtitle_panel=None,
        pick_file: Optional[Callable[[str], str]] = None,
        config: Any = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`sound_panel` and `subtitle_panel` are TrackPanels (None: toasts); `session()` the
        large picture's player, `current()` what it plays and `playing()` whether it has
        media; `channel_key` a live channel's key in `store`. `pick_file(folder)` asks for a
        subtitle file ("" when none was chosen; None: no "Load a file…"); `config` keeps its
        folder (`subtitle_folder`)."""
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
        self._key = channel_key
        self._store = store
        self._toast = toast
        self._show_transport = show_transport
        self._pick_file = pick_file
        self._config = config
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
        sync. The same title or channel again (a retry) keeps what the player has."""
        if previous is not None and previous.same_as(target):
            return
        live = target.live
        session.set_audio_delay(self._store.audio_ms(self._key(target.item)) if live else 0)
        session.clear_subtitles()

    def prepare_small(self, session, channel: dict) -> None:
        """A channel opens in the small picture's player: its sound as set last."""
        session.set_audio_delay(self._store.audio_ms(self._key(channel)))
        session.clear_subtitles()

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
            self._toast(SUBTITLES_OFF)
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

    def load_subtitle_file(self, path: str, *, say: bool = True) -> bool:
        """Show the subtitle file at `path` with what plays (chosen); False, said, when it
        could not be loaded."""
        if not self._playing():
            return False
        name = os.path.basename(path)
        if not self._session().add_subtitle_file(path):
            self._toast(f"Couldn't load the subtitles {name}", "error", ERROR_MS)
            return False
        if self._config is not None:  # the dialog opens there next time
            self._config.subtitle_folder = os.path.dirname(os.path.abspath(path))
        if say:
            self._toast(f"Subtitles: {name}")
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
            self._said(SUBTITLES, f"Subtitles: {os.path.basename(loaded[-1])}")
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
        self._open(SUBTITLES)

    def refresh(self) -> None:
        """What the panels show changed (the pictures swapped): the large one's."""
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
            sync_text(noun, -LIMIT_MS),
            earlier=ms > -LIMIT_MS,
            later=ms < LIMIT_MS,
            reset=ms != 0,
            earlier_tip=key_hint(f"{noun} {STEP_MS} ms earlier", earlier),
            later_tip=key_hint(f"{noun} {STEP_MS} ms later", later),
        )

    def _sound_model(self) -> PanelModel:
        session = self._session()
        tracks = [t for t in session.audio_tracks() if t[0] != -1]  # -1: no sound
        playing = session.audio_track()
        rows = tuple(PanelRow(i, name, i == playing) for i, name in tracks)
        return PanelModel(
            SOUND, rows if len(rows) > 1 else (), self._sync(AUDIO, self._delay(AUDIO))
        )

    def _subtitle_model(self) -> PanelModel:
        session = self._session()
        shown = session.subtitle_track()
        rows = [PanelRow(-1, OFF, shown == -1)]
        rows += [PanelRow(i, name, i == shown) for i, name in session.subtitle_tracks() if i != -1]
        if self._pick_file is not None:
            rows.append(PanelRow(LOAD_FILE, LOAD_TEXT, action=True))
        sync = None if shown == -1 else self._sync(SUBTITLES, session.subtitle_delay())
        return PanelModel(SUBTITLES_TITLE, tuple(rows), sync)

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
        session = self._session()
        if name == AUDIO:
            session.set_audio_track(int(key))
        else:
            session.set_subtitle_track(int(key))
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
            return self._store.audio_ms(self._key(target.item))
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
            self._store.set_audio_ms(self._key(target.item), ms)
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
            self._toast(NO_AUDIO_TRACKS if name == AUDIO else NO_SUBTITLES)
            return
        ids = [t[0] for t in tracks]
        index = ids.index(playing) if playing in ids else -1
        track_id, track = tracks[(index + 1) % len(tracks)]
        if name == AUDIO:
            session.set_audio_track(track_id)
            self._toast(f"Audio: {track}")
        else:
            session.set_subtitle_track(track_id)
            self._toast(f"Subtitles: {OFF if track_id == -1 else track}")
