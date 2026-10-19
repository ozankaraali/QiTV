"""The Picture panel (2.3): the picture's aspect ratio, deinterlacing, a sleep timer, and the
statistics over the picture.

The transport's Picture button (or A) opens the panel above it, as ♫ opens the Sound panel
(ui/overlays/track_panel.py: the same look, keys, idle close, scrolling and click-outside):

    Aspect ratio    ‹ Auto ›     Auto, 16:9, 4:3, Fill the window
    Deinterlacing   ‹ Auto ›     Auto, On, Off
    Sleep timer     ‹ Off ›      Off, 15, 30, 60, 90 min, End of the programme
  ✓ Statistics                   shown or hidden

‹ and › (←/→ on the row, Enter, a click on the row) step a setting through its choices, round
the ends. A opens the panel on the aspect ratio, and again while it shows steps it on; Shift+I
shows or hides the statistics from anywhere on the player. While the panel shows, the keys are
its own (↑/↓, ←/→, Enter, Esc); another key closes it and goes on.

Everything acts on the large picture (a multiview's focused tile is the large one). A live
channel's aspect ratio is kept for the channel (`services.picture.ChannelPictures`, written off
the GUI thread): it applies whenever a player opens the channel, large or small (`prepare`,
`prepare_small`), and the player keeps it through each reopen of the same channel. It is kept
under the channel's key as the channel opened in that player (ui/player_keys.py, shared with
the sound's sync: the channel list may browse another provider by the time A is pressed). A
film's stays with its player while it plays; the next title starts on Auto. Fill crops to the shape of
the player's own surface, which the window reports to the player. Deinterlacing is one setting
for every player, kept in the config (`deinterlace`, optional: older builds ignore it); libVLC 3
cannot give a player Auto back once it was set on or off, which a toast says (QiTV's next start
does).

The sleep timer (`services.sleep_timer`) is never kept across a restart. While it runs, its row
shows the time left; "End of the programme" is offered while the guide knows when the programme
being watched ends (later by as much as a time-shifted channel plays behind live). A minute
before the end a notice says playback stops (ui/overlays/sleep_notice.py), with Keep watching:
a click on it, or Enter, turns the timer off. At the end QiTV stops playback as the Library's
stop does (each stream closed, so its provider's connection is freed), leaves fullscreen, and
lets the computer sleep again (`services.sleep_manager`) until something plays again.

The statistics (ui/overlays/stats.py) show the large picture's codec, size and frame rate, its
pictures, bitrates, decoding and source, and on a time-shifted channel how far it plays behind
live (`services.picture.stats_rows`), read once a second while they show.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer

from services import picture, sleep_manager
from services.i18n import N_, tr, translate
from services.picture import ASPECTS, DEINTERLACING, aspect_name, deinterlace_name
from services.sleep_timer import PRESETS_MIN, SleepTimer, clock_text
from ui.keymap import Action, key_hint
from ui.link_lanes import Target
from ui.overlays.track_panel import PanelModel, PanelRow
from ui.player_keys import PlayerKeys

logger = logging.getLogger(__name__)

PICTURE = "picture"  # the transport's button
ASPECT, DEINTERLACE, SLEEP, STATS = "aspect", "deinterlace", "sleep", "stats"  # the rows
PICTURE_TITLE = N_("Picture")  # i18n: the Picture panel's title (aspect ratio, sleep timer…)
ASPECT_LABEL = N_("Aspect ratio")  # i18n: the Picture panel's row
DEINTERLACE_LABEL = N_("Deinterlacing")  # i18n: the Picture panel's row (interlaced TV pictures)
SLEEP_LABEL = N_("Sleep timer")  # i18n: the Picture panel's row: playback stops after a while
STATS_LABEL = N_("Statistics")  # i18n: the Picture panel's row, and the statistics' title
# i18n: the sleep timer's choice: stop when the programme being watched ends
PROGRAMME_END = N_("End of the programme")
# i18n: a toast: libVLC cannot give a player Auto back once deinterlacing was on or off
AUTO_LATER = N_("Deinterlacing is back on Auto when QiTV starts again")
SLEEP_OFF = N_("Sleep timer off")  # i18n: a toast (Keep watching turned it off)
STOPPED = N_("Sleep timer: playback stopped")  # i18n: a toast once the sleep timer ended
REFRESH_MS = 1000  # the statistics, and the panel's sleep row
STOPPED_MS = 8000
_OFF, _END = "off", "end"
_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
_ASPECT_KEY, _STATS_KEY = "A", "Shift+I"


def _time_left(seconds: float) -> str:
    # i18n: the sleep timer's row while it runs: the time left ("28:41 left")
    return tr("{time} left", time=clock_text(seconds))


class PictureControls(QObject):
    def __init__(
        self,
        *,
        panel,
        transport,
        stats,
        notice,
        session: Callable[[], Any],
        current: Callable[[], Optional[Target]],
        playing: Callable[[], bool],
        players: Callable[[], Iterable[Any]],
        keys: PlayerKeys,
        store: picture.ChannelPictures,
        config: Any,
        toast: Callable[..., None],
        programme_left: Callable[[], Optional[float]],
        live_info: Callable[[], dict],
        stop: Callable[[], None],
        show_transport: Callable[[], None] = lambda: None,
        allow_sleep: Callable[[], None] = sleep_manager.allow_sleep,
        prevent_sleep: Callable[[], None] = sleep_manager.prevent_sleep,
        timer: Optional[SleepTimer] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`panel` is a TrackPanel, `stats` a StatsOverlay and `notice` a SleepNotice (each
        None-able: no panel, statistics or notice). `session()` is the large picture's player,
        `current()` what it plays and `playing()` whether it has media; `players()` every
        player (deinterlacing is theirs); `keys` each player's channel key in `store`.
        `programme_left()` the seconds until the programme watched ends (None: not known),
        `live_info()` the statistics' "source" and "lag"; `stop()` stops playback for the
        sleep timer; `show_transport()` shows the player controls (A opens the panel above
        them). `allow_sleep` and `prevent_sleep` let the computer sleep or keep it awake
        (services.sleep_manager's)."""
        super().__init__(parent)
        self._panel = panel
        self._transport = transport
        self._stats = stats
        self._notice = notice
        self._session = session
        self._current = current
        self._playing = playing
        self._players = players
        self._keys = keys  # each player's channel and its key as it opened (shared)
        self._store = store
        self._config = config
        self._toast = toast
        self._programme_left = programme_left
        self._live_info = live_info
        self._stop = stop
        self._show_transport = show_transport
        self._allow_sleep = allow_sleep
        self._prevent_sleep = prevent_sleep
        self._timer = timer if timer is not None else SleepTimer(self)
        self._sleep: object = _OFF  # the sleep timer's choice: _OFF, minutes, or _END
        self._stats_on = False
        self._slept = False  # the computer may sleep: kept awake again once something plays
        self._refresh = QTimer(self, interval=REFRESH_MS)
        self._refresh.timeout.connect(self._on_refresh)

    def wire(self) -> None:
        transport = self._transport
        transport.pictureRequested.connect(self.open_panel)
        transport.bandChanged.connect(self._follow_band)
        transport.installEventFilter(self)  # its hide closes the panel
        panel = self._panel
        if panel is not None:
            panel.set_controls(transport, transport.control_at)
            panel.rowChosen.connect(self._on_row)
            panel.rowStepped.connect(self._on_step)
            panel.closed.connect(self._on_closed)
        if self._notice is not None:
            self._notice.keepWatching.connect(self.keep_watching)
        self._timer.changed.connect(self._on_timer_changed)
        self._timer.warning.connect(self._on_warning)
        self._timer.expired.connect(self._on_expired)
        for player in self._players():
            self.add_player(player)

    def shutdown(self) -> None:
        """QiTV closes: the timer ends unsaid, nothing refreshes."""
        self._timer.cancel()
        self._refresh.stop()

    # ---- what opens ---------------------------------------------------------------------------

    def prepare(self, session, target: Target, previous: Optional[Target]) -> None:
        """`target` is about to open in `session` (the large picture's player): a channel in the
        shape the viewer chose for it last, anything else in its own. The same title or channel
        again (a retry) keeps what the player has."""
        if previous is not None and previous.same_as(target):
            return
        if not target.live:
            self._keys.forget(session)
            session.set_aspect(picture.AUTO)
        else:
            session.set_aspect(self._store.aspect(self._keys.opens(session, target.item)))

    def prepare_small(self, session, channel: dict) -> None:
        """A channel opens in a small picture's player (PiP, a tile): its shape as chosen."""
        session.set_aspect(self._store.aspect(self._keys.opens(session, channel)))

    def add_player(self, player) -> None:
        """A player (a multiview's tile made now): deinterlacing as the config says."""
        choice = self._deinterlacing()
        if choice != picture.AUTO:
            player.set_deinterlace(choice)

    def on_playing(self) -> None:
        """The large picture plays: after the sleep timer let the computer sleep, it stays awake
        again while QiTV plays."""
        if self._slept:
            self._slept = False
            self._prevent_sleep()

    # ---- the keys -----------------------------------------------------------------------------

    def visible(self) -> bool:
        return self._panel is not None and self._panel.isVisible()

    def handle(self, action: Action, key: Optional[int] = None) -> bool:
        """The keys while the panel or the sleep notice shows (False: not theirs). A key that is
        not the panel's closes it and goes on; A and Shift+I leave it open."""
        notice = self._notice
        if notice is not None and notice.isVisible() and action is Action.OK:
            self.keep_watching()
            return True
        if not self.visible():
            return False
        panel = self._panel
        if action is Action.ESCAPE:
            panel.dismiss()
        elif action is Action.CH_PREV and key == _UP:
            panel.move_focus(-1)
        elif action is Action.CH_NEXT and key == _DOWN:
            if panel.focused() is None:
                panel.focus_row(0)  # the first row, though Statistics may be ticked
            else:
                panel.move_focus(1)
        elif action in (Action.SEEK_BACK, Action.SEEK_FWD):
            panel.step(-1 if action is Action.SEEK_BACK else 1)
        elif action is Action.OK:
            panel.activate()
        elif action is Action.PICTURE:
            self.next_aspect()
        elif action is Action.STATS:
            self.toggle_stats()
        else:
            panel.dismiss()
            return False
        return True

    def open_from_key(self) -> None:
        """A: the panel, on its aspect ratio (while it shows, handle() steps it on)."""
        if self._panel is None:
            self.next_aspect()
            return
        if self._playing():
            self._show_transport()  # the panel opens above its button
        self.open_panel()
        if self.visible():
            self._panel.focus_row(0)

    def next_aspect(self) -> None:
        """The next aspect ratio for the large picture (A while the panel shows)."""
        if not self._playing() or self._current() is None:
            return
        aspect = picture.stepped(ASPECTS, self._session().aspect(), 1)
        self._set_aspect(aspect)
        if self.visible():
            self._panel.focus_row(0)
            self.open_panel()
        else:
            # i18n: a toast: the aspect ratio chosen ("Aspect ratio: 16:9")
            self._toast(tr("Aspect ratio: {name}", name=aspect_name(aspect)))

    def toggle_stats(self) -> None:
        """Shift+I and the Statistics row: the statistics over the picture, or not."""
        self._stats_on = not self._stats_on
        if self._stats_on:
            self._show_stats()
            self._refresh.start()
        else:
            if self._stats is not None:
                self._stats.hide()
            if not self.visible():
                self._refresh.stop()
        if self.visible():
            self.open_panel()

    def stats_shown(self) -> bool:
        return self._stats_on

    # ---- the panel ----------------------------------------------------------------------------

    def open_panel(self) -> None:
        """The transport's Picture button: the panel above it (updated in place if it shows)."""
        panel = self._panel
        if panel is None or not self._playing():
            return
        self._transport.hold(PICTURE)
        panel.show_panel(self._model(), self._anchor(), self._floor(), PICTURE)
        self._refresh.start()

    def refresh(self) -> None:
        """The large picture changed (a swap, another tile focused): the panel and the
        statistics show its own."""
        if self.visible():
            if self._playing():
                self.open_panel()
            else:
                self.close()
        if self._stats_on:
            self._show_stats()

    def close(self) -> None:
        if self._panel is not None:
            self._panel.dismiss()

    def _model(self) -> PanelModel:
        aspect = self._session().aspect()
        deinterlacing = self._deinterlacing()
        aspects = [aspect_name(a) for a in ASPECTS]
        modes = [deinterlace_name(d) for d in DEINTERLACING]
        sleeps = [self._sleep_name(choice) for choice in self._sleep_choices()]
        sleeps.append(_time_left(90 * 60.0))  # "1:30:00 left"
        rows = (
            PanelRow(
                ASPECT,
                translate(ASPECT_LABEL),
                value=aspect_name(aspect),
                widest=max(aspects, key=len),
                lit=aspect != picture.AUTO,
                tip=key_hint(translate(ASPECT_LABEL), _ASPECT_KEY),
            ),
            PanelRow(
                DEINTERLACE,
                translate(DEINTERLACE_LABEL),
                value=deinterlace_name(deinterlacing),
                widest=max(modes, key=len),
                lit=deinterlacing != picture.AUTO,
            ),
            PanelRow(
                SLEEP,
                translate(SLEEP_LABEL),
                value=self._sleep_value(),
                widest=max(sleeps, key=len),
                lit=self._timer.running(),
            ),
            PanelRow(
                STATS,
                translate(STATS_LABEL),
                checked=self._stats_on,
                tip=key_hint(translate(STATS_LABEL), _STATS_KEY),
            ),
        )
        return PanelModel(translate(PICTURE_TITLE), rows)

    def _anchor(self) -> Optional[QRectF]:
        """The transport's Picture button, in the host's coordinates (None: not shown)."""
        try:
            rect = self._transport.button_rect(PICTURE)
        except KeyError:
            return None
        return QRectF(rect).translated(self._transport.pos())

    def _floor(self) -> Optional[int]:
        transport = self._transport
        return transport.controls_top() if transport.isVisible() else None

    def _follow_band(self) -> None:
        if self.visible():
            self._panel.show_panel(self._panel.model(), self._anchor(), self._floor())

    def _on_row(self, key) -> None:
        if key == STATS:
            self.toggle_stats()

    def _on_step(self, key, delta: int) -> None:
        if not self._playing():
            return
        if key == ASPECT:
            self._set_aspect(picture.stepped(ASPECTS, self._session().aspect(), delta))
        elif key == DEINTERLACE:
            self._set_deinterlacing(picture.stepped(DEINTERLACING, self._deinterlacing(), delta))
        elif key == SLEEP:
            self._step_sleep(delta)
        self.open_panel()

    def _on_closed(self) -> None:
        if self._transport.held() == PICTURE:
            self._transport.hold("")
        if not self._stats_on:
            self._refresh.stop()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._transport and event.type() == QEvent.Type.Hide:
            self.close()  # the panel belongs to the player controls
        return False

    def _on_refresh(self) -> None:
        if self._stats_on:
            self._show_stats()
        elif not self.visible():
            self._refresh.stop()

    # ---- the aspect ratio and deinterlacing ---------------------------------------------------

    def _set_aspect(self, aspect: str) -> None:
        """The large picture in `aspect`'s shape; a live channel's is kept for the channel."""
        session = self._session()
        session.set_aspect(aspect)
        target = self._current()
        if target is not None and target.live:
            self._store.set_aspect(self._keys.key_of(session, target.item), aspect)

    def _deinterlacing(self) -> str:
        choice = getattr(self._config, "deinterlace", picture.AUTO)
        return choice if choice in DEINTERLACING else picture.AUTO

    def _set_deinterlacing(self, choice: str) -> None:
        """Every player's deinterlacing, kept in the config."""
        self._config.deinterlace = choice
        back = [player.set_deinterlace(choice) for player in self._players()]
        if not all(back):
            self._toast(translate(AUTO_LATER))
        try:
            self._config.save_config()
        except Exception as e:  # the setting holds for this run anyway
            logger.warning("Couldn't save the deinterlacing setting: %s", e)

    # ---- the sleep timer ----------------------------------------------------------------------

    def _sleep_choices(self) -> list[object]:
        choices: list[object] = [_OFF, *PRESETS_MIN]
        if self._programme_left() is not None:
            choices.append(_END)
        return choices

    @staticmethod
    def _sleep_name(choice: object) -> str:
        if isinstance(choice, int):
            return tr("{n} min", n=choice)
        if choice == _END:
            return translate(PROGRAMME_END)
        return tr("Off", ctx="sleep timer")  # i18n: the sleep timer does not run

    def _sleep_value(self) -> str:
        if self._timer.running():
            return _time_left(self._timer.remaining())
        return self._sleep_name(_OFF)

    def sleep_choice(self) -> object:
        """The sleep timer's choice: "off", its minutes, or "end" (the programme's end)."""
        return self._sleep if self._timer.running() else _OFF

    def _step_sleep(self, delta: int) -> None:
        choices = self._sleep_choices()
        current = self.sleep_choice()
        index = choices.index(current) if current in choices else 0
        self.set_sleep(choices[(index + delta) % len(choices)])

    def set_sleep(self, choice: object) -> None:
        """Stop playback after `choice`: "off" (none), minutes, or "end" (the programme's
        end, when the guide knows it)."""
        seconds: Optional[float]
        if choice == _END:
            seconds = self._programme_left()
        elif isinstance(choice, int) and not isinstance(choice, bool):
            seconds = choice * 60.0
        else:
            seconds = None
        if self._notice is not None:
            self._notice.hide()
        if seconds is None:
            self._sleep = _OFF
            self._timer.cancel()
            return
        self._sleep = choice
        self._timer.start(seconds)

    def keep_watching(self) -> None:
        """The notice's Keep watching (a click, Enter): the sleep timer is off."""
        if self._notice is not None:
            self._notice.hide()
        if self._timer.running():
            self.set_sleep(_OFF)
            self._toast(translate(SLEEP_OFF))

    def _on_timer_changed(self) -> None:
        notice = self._notice
        if notice is not None and self._timer.warned():  # the last minute counts down
            notice.show_notice(self._notice_text())
        if self.visible():
            self._panel.set_model(self._model())

    def _notice_text(self) -> str:
        # i18n: the sleep timer's last minute: when playback stops ("Playback stops in 0:42")
        return tr("Playback stops in {time}", time=clock_text(self._timer.remaining()))

    def _on_warning(self) -> None:
        if self._notice is not None:
            self._notice.show_notice(self._notice_text())

    def _on_expired(self) -> None:
        """The end: playback stops (its streams closed), fullscreen ends, and the computer may
        sleep again."""
        self._sleep = _OFF
        if self._notice is not None:
            self._notice.hide()
        self.close()
        try:
            self._stop()
        finally:
            self._slept = True
            try:
                self._allow_sleep()
            except Exception as e:  # a platform call: the stop stands either way
                logger.warning("Couldn't let the computer sleep: %s", e)
            self._toast(translate(STOPPED), "info", STOPPED_MS)

    # ---- the statistics -----------------------------------------------------------------------

    def _show_stats(self) -> None:
        overlay = self._stats
        if overlay is None:
            return
        if not self._playing():
            overlay.hide()  # nothing to tell; they come back with the next picture
            return
        stats = dict(self._session().video_stats())
        stats.update(self._live_info())
        overlay.show_rows(translate(STATS_LABEL), picture.stats_rows(stats))
