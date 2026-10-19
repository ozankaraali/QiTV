"""The player's sound (2.2): its tracks and its sync with the picture, on the Sound panel.

The transport's ♫ opens the Sound panel (ui/overlays/track_panel.py) above it: the sound's
tracks to pick from (with two or more) and a Sync row that moves the sound against the
picture in steps of 50 ms, up to 5 s either way: "Sound 150 ms later" plays it later (for a
sound that comes too early), "earlier" the other way, and Reset puts it back in sync. J and K
do the same as − and + from anywhere on the player, with a toast (the panel follows while it
shows). While the panel shows, the keys are its own: ↑/↓ move between its rows, Enter
chooses, ←/→ step the sync and Esc closes it; any other key closes it and does what it does.
The player controls stay up while it shows, and it closes with them.

Everything acts on the large picture. A live channel's sync is kept for that channel
(`services.sound_sync`, written off the GUI thread): it applies whenever a player opens the
channel, large or small (`prepare`, `prepare_small`), and the player keeps it through each
reopen of the same channel (a reconnect, a time-shift seek, a go-live). A swap moves
nothing: each player keeps playing its channel with its sync, so the sync follows the
channel. A film's or an episode's stays with its player while that title plays (a retry of
it keeps it too) and is never stored: the next title starts in sync. Without a panel
(tests), ♫ cycles the tracks with a toast, as before 2.2.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt

from services.sound_sync import LIMIT_MS, STEP_MS, SoundSync, clamp
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.track_panel import PanelModel, PanelRow, PanelSync

logger = logging.getLogger(__name__)

SOUND = "Sound"
IN_SYNC = "In sync"
NO_AUDIO_TRACKS = "No audio tracks"
_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
_SOUND_KEYS = (Action.SOUND_EARLIER, Action.SOUND_LATER)


def sync_text(noun: str, ms: int) -> str:
    """The Sync row's words: "In sync", "Sound 150 ms later" or "Sound 1,050 ms earlier"."""
    if not ms:
        return IN_SYNC
    return f"{noun} {abs(ms):,} ms {'later' if ms > 0 else 'earlier'}"


def toast_text(noun: str, ms: int) -> str:
    """A sync key's toast: "Sound in sync", else as the Sync row says it."""
    return f"{noun} in sync" if not ms else sync_text(noun, ms)


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
        parent: Optional[QObject] = None,
    ) -> None:
        """`sound_panel` is the TrackPanel (None: toasts); `session()` the large picture's
        player, `current()` what it plays and `playing()` whether it has media; `channel_key`
        a live channel's key in `store`."""
        super().__init__(parent)
        self._panel = sound_panel
        self._transport = transport
        self._session = session
        self._current = current
        self._playing = playing
        self._key = channel_key
        self._store = store
        self._toast = toast
        self._show_transport = show_transport

    def wire(self) -> None:
        transport = self._transport
        transport.audioRequested.connect(self.open_sound)
        transport.bandChanged.connect(self._follow_band)  # its controls moved (the bar came)
        transport.installEventFilter(self)  # its hide closes the panel
        panel = self._panel
        if panel is not None:
            panel.rowChosen.connect(self._on_row)
            panel.syncStepped.connect(self._step)
            panel.syncReset.connect(self._reset)
            panel.closed.connect(self._on_closed)

    # ---- what opens ---------------------------------------------------------------------------

    def prepare(self, session, target: Target, previous: Optional[Target]) -> None:
        """`target` is about to open in `session` (the large picture's player, its old stream
        stopped): its sound as the viewer set it last. The same title or channel again (a
        retry) keeps what the player has."""
        if previous is not None and previous.same_as(target):
            return
        live = target.live
        session.set_audio_delay(self._store.audio_ms(self._key(target.item)) if live else 0)

    def prepare_small(self, session, channel: dict) -> None:
        """A channel opens in the small picture's player: its sound as set last."""
        session.set_audio_delay(self._store.audio_ms(self._key(channel)))

    # ---- the keys -----------------------------------------------------------------------------

    def visible(self) -> bool:
        panel = self._panel
        return panel is not None and panel.isVisible()

    def handle(self, action: Action, key: Optional[int] = None) -> bool:
        """The keys while the panel shows (False: not its own, or it is hidden). A key that is
        not the panel's closes it and goes on (the sync keys leave it open)."""
        panel = self._panel
        if panel is None or not panel.isVisible():
            return False
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
            if action not in _SOUND_KEYS:
                panel.dismiss()
            return False
        return True

    def nudge_sound(self, steps: int) -> None:
        """J (-1) and K (+1): the large picture's sound 50 ms earlier or later, with a toast
        (the panel shows it instead while it is open)."""
        if not self._playing() or self._current() is None:
            return
        ms = self._set_sound(self._session().audio_delay() + steps * STEP_MS)
        if self.visible():
            self.open_sound()
        else:
            self._toast(toast_text(SOUND, ms))

    # ---- the panel ----------------------------------------------------------------------------

    def open_sound(self) -> None:
        """The transport's ♫: the Sound panel above it (or, without one, the next track)."""
        if not self._playing():
            return
        panel = self._panel
        if panel is None:
            self._cycle_audio()
            return
        self._transport.hold("audio")
        panel.show_panel(self._sound_model(), self._anchor("audio"), self._floor())

    def refresh(self) -> None:
        """What the panel shows changed (the pictures swapped): it shows the large one's."""
        if self.visible():
            if self._playing():
                self.open_sound()
            else:
                self.close()

    def close(self) -> None:
        if self._panel is not None:
            self._panel.dismiss()

    def _sound_model(self) -> PanelModel:
        session = self._session()
        tracks = [t for t in session.audio_tracks() if t[0] != -1]  # -1: no sound
        playing = session.audio_track()
        rows = tuple(PanelRow(i, name, i == playing) for i, name in tracks)
        ms = session.audio_delay()
        sync = PanelSync(
            sync_text(SOUND, ms),
            sync_text(SOUND, -LIMIT_MS),
            earlier=ms > -LIMIT_MS,
            later=ms < LIMIT_MS,
            reset=ms != 0,
            earlier_tip=key_hint(f"{SOUND} {STEP_MS} ms earlier", "J"),
            later_tip=key_hint(f"{SOUND} {STEP_MS} ms later", "K"),
        )
        return PanelModel(SOUND, rows if len(rows) > 1 else (), sync)

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

    def _on_row(self, track_id) -> None:
        if self._playing():
            self._session().set_audio_track(int(track_id))
            self.open_sound()

    def _step(self, steps: int) -> None:
        if self._playing():
            self._set_sound(self._session().audio_delay() + steps * STEP_MS)
            self.open_sound()

    def _reset(self) -> None:
        if self._playing():
            self._set_sound(0)
            self.open_sound()

    def _on_closed(self) -> None:
        self._transport.hold("")

    def _follow_band(self) -> None:
        panel = self._panel
        if panel is not None and panel.isVisible():
            panel.show_panel(panel.model(), self._anchor("audio"), self._floor())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._transport and event.type() == QEvent.Type.Hide:
            self.close()  # the panel belongs to the player controls
        return False

    # ---- the sync -----------------------------------------------------------------------------

    def _set_sound(self, ms: int) -> int:
        """The large picture's sound `ms` later (clamped): its player keeps it while the
        title plays, and a live channel's is kept for the channel."""
        ms = clamp(ms)
        self._session().set_audio_delay(ms)
        target = self._current()
        if target is not None and target.live:
            self._store.set_audio_ms(self._key(target.item), ms)
        return ms

    def _cycle_audio(self) -> None:
        session = self._session()
        tracks = [t for t in session.audio_tracks() if t[0] != -1]  # mute disables audio
        if not tracks:
            self._toast(NO_AUDIO_TRACKS)
            return
        ids = [t[0] for t in tracks]
        playing = session.audio_track()
        index = ids.index(playing) if playing in ids else -1
        track_id, name = tracks[(index + 1) % len(tracks)]
        session.set_audio_track(track_id)
        self._toast(f"Audio: {name}")
