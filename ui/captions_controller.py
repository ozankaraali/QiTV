"""AI captions (2.9): what the subtitle panel's "AI captions" row turns on, and how it shows.

The speech model runs in its own process (services.captions.process) and reads the live
channel from the time-shift helper's buffer, ahead of the picture. While captions are on the
picture is held `LEAD_S` behind the live edge (LiveSession.set_caption_floor: the picture
waits there once, "Starting captions…", then plays at 1x; Go live and a tune land there when
the buffer holds it; turned off, the picture stays where it is, time-shifted, with Go live),
so each caption is written before the picture gets to its words and shows when they start. The
first time, the model is downloaded (services.captions.download) once the viewer says so;
the tier (0.6B or 1.7B) follows the computer's speed (services.captions.tier) or Settings.
Automatic's 1.7B that can't keep up (slow and late for `_SLOW_FOR_S`) steps down to the 0.6B
while QiTV runs, its download's question first (once) when it isn't here: the captions written
so far stay, and the picture stays where it is. The viewer's own choice of a model never changes.
Turned on from a phone remote, the question shows on the TV and on that phone: either answer
goes for both (`answer`, ui/remote_controller.py).

Cues are kept on the time-shift timeline: going back over a stretch shows its captions
again, and a seek reads again from where the picture went. Another audio track (the Sound
panel, a key, a phone) is read from where the picture is, in its own language. A caption
that comes after its words started shows for what is left of it (the picture's own pace
decides); one that comes after they ended never shows (captions in the next scene are worse
than none). A caption too long for two lines shows in parts, sentence by sentence, each as it is
said by its line's parts' times (`_page_at`; 2026-11-08, the owner).

A film or an episode has no helper: the process plays its sound from `FILM_AHEAD_S` past the
picture (services.captions.film), on the film's own clock, from where the window's player reads
it: a file on this computer, or a Jellyfin or Emby server (no stream limit there). A provider's
film is read through a film relay (services.film_relay) instead of a second stream, which many
providers allow none of: the picture's one provider connection serves both. A film started while
captions are on plays through it from the start; turning them on during one opens it again there,
where it is (TVController.relay_film); turning them off keeps it until the title ends.

2.10: the captions show translated into the language the viewer picks from the "Translate to"
list (`set_translate_to`; MiLMMT-46 1B in the captions process, services.captions.translate). Its
model is downloaded once its question is answered (asked while they are on; not now: none).
While they are translated the picture is held TRANSLATE_LEAD_S further back (a film's sound read
that much further ahead), on every channel. On a computer too slow for the 1.7B and the
translator both, Automatic takes the 0.6B (services.captions.tier.TRANSLATE_SMALL_BELOW) and
Turkish is read with its best path alone while translating (the load's "slow_to_translate").

Plan 4 (Words on the audio screen): a place that shows lines (a sink with `show_lines`: the
audio screen's Stage) gets the line before, in its own language, the line said (as heard, part
by part as said) and its translation ("" when it isn't), not the caption's text; the others get
the caption as before. While a Stage takes the words (`takes_words`), the captions over its page
give them to it (`_show`, `words_moved`). While the audio screen's visualizer asks
(`want_levels`), a film's run sends its sound's band levels too (`levels`, `levels_from`): the
Stage's bars are read from the sound the captions read, never decoded twice (ui.visualizer).
"""

from __future__ import annotations

import bisect
import logging
import os
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.captions.streaming import TRANSLATE_LEAD_S
from services.i18n import tr, translate

logger = logging.getLogger(__name__)

LEAD_S = {"small": 8.0, "large": 10.0}  # the picture's distance behind live while on
FILM_AHEAD_S = 6.0  # a film's sound is read from this far past the picture (after a seek too)
# "Catching up…" only when what has been heard is about to fall behind the picture: anything
# ahead of it is in time (2.9: a 1.7B film's captions 3-6 s ahead, all in time, said so at
# every dip under the 6 s it starts with).
LATE_LEAD_S = 1.0
# 2.10.1: turned on, the picture says "Starting captions…" while the model loads and until the
# first run has written this far past it (its first words are written), or this long after the
# model was ready; the picture plays on meanwhile (it waits only for their distance)
START_LEAD_S = 1.5
START_WAIT_S = 6.0
HOLD_S = 0.6  # a caption stays this long after its words, unless the next one starts
MIN_SHOW_S = 1.2
# 2.9, the owner: captions came "a tiny bit" before the words, and went before the next line
# came ("hard to read"). A piece starts LEAD_IN_S (services.captions.pieces) before its first
# word: its caption shows from the word. A pause up to BRIDGE_S between two lines keeps the
# first on until the next; after a longer one it stays as long as it takes to read (READ_CPS
# characters a second), and never over the next.
FIRST_WORD_S = 0.3  # services.captions.pieces.LEAD_IN_S
BRIDGE_S = 3.0
READ_CPS = 15.0
# 2.9.5, the owner: "long sentences ... superseded with a short sentence after, not giving time
# to read". A line stayed only until the next one's first word, and the next was often the
# end of the same sentence, cut off on its own ("ötesine geçiyor"). A line that follows the one
# before with no pause (it was cut, or ended at a phrase's pause) and is short joins it when
# both still fit two lines; and a line waits for the one before to be read (READ_CPS), by at
# most LAG_S after its own first word, so the captions stay with the picture.
JOIN_GAP_S = 0.3
# a line spoken in this long at most joins the one before (2.9.6, the real-use review: "I think
# that goes away", 2.9 s, came alone at 2.5 s); it may join one shown in parts when it adds no
# part, and never one already on screen (it would wrap again under the viewer's eyes)
JOIN_LONGEST_S = 3.5
JOIN_CHARS = 84  # without an overlay to ask: what two lines hold
LAG_S = 1.0
PAGE_S = 1.0  # a part of a long caption stays at least this long (2026-11-08: none flashes by)
# 2026-11-09, the owner: buffered channels watched at 1.5x or 2x; the times a caption stays to be
# read are the viewer's, so they stretch by the picture's rate (from FAST_FROM, at most RATE_CAP)
FAST_FROM = 1.2
RATE_CAP = 2.0
_LAG_LOOK = 32  # cues looked back at for a wait that reaches the one asked about
TICK_MS = 250
SEEK_S = 2.0  # the position moved this much more than the clock: a seek, read again there
KEEP_S = 30 * 60.0  # cues kept this far behind the picture
_RESTART_MS = 1000
NO_LINES = ("", "", "")  # a Stage's (previous, current, translation) with none to show
# a Stage's dimmed line above is the cue before only when it ended this close before the line
# said started (ruling P4-10: not one from before a long pause or an instrumental)
PREVIOUS_LINE_S = 10.0
# 2026-11-09, the owner's channel: its clock leapt, every run read again from the same second
# (25 runs in 27 s), and the second it waited for stayed past the next channel's tune (no run
# there for minutes, "Can't keep up" for a run that never read). A run read again past a jump
# starts at least this far past the last one's start and reads on past the next steps; a jump's
# place is the session's alone; a jump whose segment is not there after RESYNC_TRIES restarts
# reads from the picture instead; and a run that has heard nothing NO_SOUND_S after it began
# reads again from the picture
RESYNC_STEP_S = 0.1
RESYNC_TRIES = 5
NO_SOUND_S = 12.0
_SLOW_RTF = 0.9  # the model takes this long per second of speech: it can't keep up
# 2.9.6: the first question's yes (it said every language's model may come): they download as
# their languages play, unasked
_LANGUAGE_MODELS_OK = "ai_captions_language_models"
_BACKLOG_S = 2.0  # ... or this much sound read waits for it (2.9.1)
_SLOW_FOR_S = 30.0
_TRANSLATE_TO = "ai_captions_translate_to"  # 2.10: the language they are translated into
_LOCAL_ON = "ai_captions_local_on"  # turned on (or off) during the last video file

from ui.captions_texts import (  # noqa: E402  (the texts, with their translations)
    CANT_KEEP_UP,
    CATCHING_UP,
    COULDNT_START,
    DOWNLOAD,
    DOWNLOAD_QUESTION,
    DOWNLOADING,
    FALLBACK_QUESTION,
    FROM_AUDIO_TRACK,
    FROM_DEFAULT,
    FROM_MODEL,
    FROM_VIEWER,
    NOT_AVAILABLE,
    NOT_NOW,
    PREPARING,
    SPECIALIST_QUESTION,
    STARTING,
    STEP_DOWN_QUESTION,
    STEPPED_DOWN,
    TRANSLATE_QUESTION,
    size_text,
)

MODEL_NAMES = ("auto", "small", "large")  # the AI captions row's ‹ › steps, in order
MODEL_SIZES = {"small": "0.6B", "large": "1.7B"}


def _words(text: str) -> int:
    """A caption's words (a lone dash is none)."""
    return sum(1 for word in text.split() if word != "-")


def _word_time(timing: list[tuple[int, float, float]], k: int) -> float:
    """When the caption's word `k` (from 0) is said, by its part's times: (words, start, end)
    each, the words spread evenly over their part."""
    for words, start, end in timing:
        if k < words:
            return start + (end - start) * k / max(1, words)
        k -= words
    return timing[-1][2] if timing else 0.0


def _alive(sink: Any) -> bool:
    """A place the captions show is still there (the floating picture's goes as QiTV closes)."""
    from shiboken6 import isValid

    return not isinstance(sink, QObject) or isValid(sink)


def _shows_lines(sink: Any) -> bool:
    """A place that shows lines (Plan 4: the audio screen's Stage), not the caption's text."""
    return callable(getattr(sink, "show_lines", None))


def _page_of(sink: Any) -> Any:
    """The page (overlay host) a place the captions show is on; None when it says none."""
    parent = getattr(sink, "parentWidget", None)
    return parent() if callable(parent) else None


def model_texts() -> dict[str, str]:
    """The speech models' names, as Settings names them."""
    return {
        # i18n: Settings → AI captions → Speech model, and the Subtitles panel's list: QiTV
        # picks it by this computer's speed
        "auto": tr("Automatic", ctx="a speech model"),
        # i18n: Settings → AI captions → Speech model: the smaller one; keep "0.6B"
        "small": tr("Faster (0.6B)"),
        # i18n: Settings → AI captions → Speech model: the larger one; keep "1.7B"
        "large": tr("More accurate (1.7B)"),
    }


class CaptionsController(QObject):
    """The subtitle panel's AI captions, for the large picture's live channel."""

    changed = Signal()  # the row's state or text changed: the panel shows it again
    _scored = Signal()  # this computer's speed is measured (from the benchmark's thread)
    # Plan 4: a chunk of the film's band levels (its location, start_ms, frame_ms, the chunk),
    # while asked (want_levels); and a film's run began or they went off (levels_from moved)
    levels = Signal(str, int, int, object)
    soundRead = Signal()

    def __init__(self, window: Any, card: Any = None, overlay: Any = None) -> None:
        super().__init__(window)
        from services.captions.process import CaptionsProcess

        self._window = window
        self._card = card  # a QuestionCard of its own: download the model?
        # Where the captions show (ui.overlays.captions): over the picture, and over the floating
        # picture while it floats on another desktop (add_overlay; ui/desktop_pip.py).
        self._overlays: list[Any] = [overlay] if overlay is not None else []
        self._process = CaptionsProcess(self)
        self._process.ready.connect(self._on_ready)
        self._process.failed.connect(self._on_failed)
        self._process.caption.connect(self._on_caption)
        self._process.progress.connect(self._on_progress)
        self._process.resync.connect(self._on_resync)
        self._process.ended.connect(self._on_ended)
        self._process.detected.connect(self._on_detected)
        self._process.levels.connect(self._on_levels)
        self._levels_wanted = False  # the audio screen's bars read a film run's sound
        if card is not None:
            card.answered.connect(self._on_answer)
        self._enabled = False
        self._leaving_download: Any = None  # cancelled, still ending its request
        self._disk: dict[str, bool] = {}  # each model installed (see _here)
        self._start_after_leaving = False
        self._run_channel = ""  # the channel the run reads (a vote's language is its)
        self._resync_at: Optional[float] = None  # read again from past a jump here
        self._resync_tries = 0  # restarts that found no segment past it yet
        self._read_on = False  # the next run reads on past steps of the stream's clock
        self._run_began: Optional[float] = None  # when the run began (its sound awaited)
        self._deaf_runs = 0  # runs in a row read again for hearing nothing (twice at most)
        self._rate = 1.0  # the picture's playback rate, as its position moves
        self._rate_from: Optional[tuple[float, float]] = None  # (position, clock) measured from
        self._supported: Optional[bool] = None
        self._asking = False
        self._question = ("", "", "")  # the download's question and its answers, as asked
        self._asking_step = False  # the question is Automatic's step down's (see _step_down)
        self._step_tried = False  # Automatic stepped down, or asked to, while QiTV runs
        self._stepped_down = False  # Automatic takes the 0.6B while QiTV runs
        self._fetching = ""  # the tier downloaded
        self._warming = False  # the speed benchmark started (once)
        self._unmeasured = False  # it failed: Automatic doesn't wait for it
        self._toggle_waits = False  # turned on before the speed was known: on once it is
        # on by themselves for a video file (local_video_playing): off again before anything but
        # a video file opens (target_changing), until the viewer turns them on or off
        self._auto_on = False
        # the video file they come back on for once this computer's speed is known (_on_scored)
        self._auto_pending: Any = None
        self._scored.connect(self._on_scored)
        self._download: Any = None
        self._percent = 0
        self._unpacking = False
        self._loading = False
        self._ready = False
        self._tier = ""
        self._run = 0
        self._session_key = ""  # the stream base the run reads (another session: read again)
        self._run_start = 0.0  # the timeline second the run reads from
        self._run_track: Optional[int] = None  # the audio track the run reads (None: default)
        self._cues: list[tuple[float, float, str]] = []  # (start, end, text) by start
        # each cue's words' times, by its start: (words, start, end) of each part said in it
        self._timing: dict[float, list[tuple[int, float, float]]] = {}
        # a translated cue's line as heard and its words' times, by its start (Plan 4: the
        # audio screen's Stage shows it, its translation under it)
        self._originals: dict[float, tuple[str, list[tuple[int, float, float]]]] = {}
        self._gone = float("-inf")  # the start of the latest cue whose time on screen ended
        self._newest: Optional[float] = None  # the newest second the run has read
        self._written: Optional[float] = None  # the second its captions are written to
        self._rtf = 0.0
        self._slow_since: Optional[float] = None
        self._qwen_writes = True  # Qwen writes the run's language (no specialist does)
        self._offered: set[str] = set()  # 2.9.6: the specialists asked for while QiTV runs
        self._first_question = False  # the question up is the first one (all the sizes)
        self._last: Optional[tuple[float, float]] = None  # (position, monotonic) last tick
        self._was_waiting = False  # the live picture waited for the captions' distance
        self._floored: Any = None  # the live session holding the captions' distance
        # turned on, till the first run writes (`_started`): since when (the model's ready, once
        # it is)
        self._starting: Optional[float] = None
        self._shown = ""
        self._lines = NO_LINES  # what the sinks with show_lines show (_show)
        self._staged: tuple = ()  # the pages whose captions give the words to a Stage (_show)
        self._language: tuple[str, str] = ("", FROM_MODEL)  # (qwen name or "", source)
        self._guessed = False  # the language is a guess from the channel's title prefix
        self._detected: dict[str, str] = {}  # channel: the language the model's pieces agreed on
        self._routed = ""  # the provider's film last sent through a film relay while on
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._restart = QTimer(self)
        self._restart.setSingleShot(True)
        self._restart.setInterval(_RESTART_MS)
        self._restart.timeout.connect(self._start_run)

    # ---- the panel's questions ---------------------------------------------------------
    def available(self) -> bool:
        """A live channel or a film the captions can read plays, and this computer can run the
        model (a runtime is pinned)."""
        target = self._target()
        if target is None:
            return False
        if not getattr(target, "live", False) and self._film() is None:
            if not self._relay_wanted():
                return False
        if self._supported is None:
            from services.captions import models

            try:
                models.download_size("small")
                self._supported = True
            except models.Unsupported:
                self._supported = False
            if self._supported:
                self._warm_tier()
        return bool(self._supported)

    def _warm_tier(self) -> None:
        """The speed benchmark (~0.2 s of CPU, and speed.json) off the GUI thread, before the
        row is pressed; once it is known, the row says what Automatic takes (`_scored`)."""
        if self._warming:
            return
        self._warming = True
        import threading

        from services.captions import tier

        def measure() -> None:
            try:
                tier.machine_score()
            except Exception as error:  # no speed: Automatic goes by memory alone
                logger.warning("AI captions: the speed was not measured: %s", error)
                self._unmeasured = True
            try:
                self._scored.emit()  # (a signal: the GUI thread hears it)
            except RuntimeError:
                pass  # QiTV closed meanwhile

        threading.Thread(target=measure, name="qitv-captions-tier", daemon=True).start()

    def _on_scored(self) -> None:
        self.changed.emit()  # "Automatic (1.7B)" or "(0.6B)": known now
        if self._toggle_waits:
            self._toggle_waits = False
            self._toggle()
        pending, self._auto_pending = self._auto_pending, None
        if pending is not None and pending is self._target():
            self._come_back_on()  # the same video file still plays: on, if all still holds

    def enabled(self) -> bool:
        return self._enabled

    def wanted(self) -> bool:
        """They are on, or on their way: asked for (their question up), downloading, or
        waiting for this computer's speed (the audio screen's Words pill reads it)."""
        return bool(
            self._enabled or self._download is not None or self._asking or self._toggle_waits
        )

    def want_levels(self, on: bool) -> None:
        """The audio screen's visualizer reads a film run's band levels (on), or no longer."""
        if on != self._levels_wanted:
            self._levels_wanted = on
            self._process.send("levels", on)

    def levels_from(self, location: str) -> Optional[float]:
        """The film second the run reading `location` reads it from, while it sends its band
        levels (want_levels); None while none does (off, still loading, live, another title)."""
        if not (self._enabled and self._ready and self._levels_wanted and location):
            return None
        if self._is_live() or self._session_key != location or not self._process.running():
            return None
        return self._run_start

    def _on_levels(self, run: int, start_ms: int, frame_ms: int, blob: Any) -> None:
        if run == self._run and self._enabled and self._session_key and not self._is_live():
            self.levels.emit(self._session_key, start_ms, frame_ms, blob)

    def status_text(self) -> str:
        if self._download is not None:
            if self._unpacking:
                return translate(PREPARING)
            return translate(DOWNLOADING, percent=self._percent)
        if not self._enabled:
            return ""
        if self._loading or not self._ready:
            return translate(PREPARING)
        if self._source() is None:
            return translate(NOT_AVAILABLE)
        if self._waiting():
            return translate(STARTING)
        if self._behind():
            return translate(CANT_KEEP_UP)
        lead = self._lead()
        if lead is not None and lead < LATE_LEAD_S:
            return translate(CATCHING_UP)
        return ""

    def toggle(self) -> None:
        """The viewer turns them on or off (their row, a phone's). During a video file, what
        they asked for is kept for the next one: on while on their way too (asked for,
        downloading, waiting for the speed). Either way they are the viewer's from now on, not
        on by themselves (`local_video_playing`)."""
        if self._plays_video_file():
            self._config_set(_LOCAL_ON, not self.wanted())
        self._auto_on = False
        self._auto_pending = None
        self._toggle()

    def give_way(self, *, off: bool = False) -> None:
        """A subtitle track, Off or a subtitle file chosen (TrackControls): they go off, a
        download on its way too, and none waiting for this computer's speed comes on after it.
        A track or a file is a choice for this file: what the viewer asked for video files
        stays as it was. Off (`off`) chosen during a video file is the viewer's no to every
        subtitle: kept for the next one, as their own toggle's off is, so ones that came on by
        themselves don't come back for the next episode."""
        if off and self._plays_video_file():
            self._config_set(_LOCAL_ON, False)
        self._auto_on = False
        self._auto_pending = None
        self._toggle_waits = False
        if self._enabled or self._download is not None:
            self._disable()

    def _toggle(self) -> None:
        """On or off, as the viewer's toggle, for QiTV's own turning them on again (another
        model, a translation, the speed known): what the viewer asked for stays as it was."""
        if self._enabled or self._download is not None:
            self._disable()
            return
        if self._toggle_waits:
            self._toggle_waits = False  # pressed again before the speed was known: off
            return
        from services.captions import models

        if self._speed_unknown():
            self._toggle_waits = True  # Automatic picks once the benchmark is done
            self._warm_tier()
            return
        tier = self._choose_tier()
        if models.installed(tier):
            self._enable(tier)
            return
        missing = models.download_size(tier)
        if missing == 0:  # all here already, only to be checked and unpacked: nothing to ask
            self._tier = self._fetching = tier
            self._start_download()
            return
        if self._card is not None:
            self._asking = True
            self._tier = self._fetching = tier
            self._first_question = True  # its yes is for every language's model too (2.9.6)
            self._question = (
                translate(
                    DOWNLOAD_QUESTION,
                    size=size_text(missing),
                    total=size_text(models.full_size(tier)),
                ),
                translate(DOWNLOAD),
                translate(NOT_NOW),
            )
            self._card.ask(*self._question)

    # ---- video files ---------------------------------------------------------------------
    def local_video_playing(self) -> None:
        """A video file plays (TVController, once per file): turned on during the last one,
        they come on by themselves when nothing would be asked or downloaded first (their model
        here) and the film shows none of its own subtitles; else nothing happens."""
        self._auto_pending = None
        self._come_back_on()

    def _come_back_on(self) -> None:
        """On by themselves for the video file playing, as `local_video_playing` says. Automatic
        going by this computer's speed first (translated) while it isn't known yet, with a model
        here: it is measured in the background (no wait shown, nothing asked), and they come on
        once it is, while the same file plays and all this still holds (`_on_scored`). Whether
        a model is here is the one looked at last (`_here`: no disk on the GUI thread)."""
        if not self._config_get(_LOCAL_ON, False):
            return
        if self._enabled or self._download is not None or self._asking or self._toggle_waits:
            return
        if self._own_subtitles_shown():
            return
        if self._speed_unknown():  # (before the tier: Automatic's would start the benchmark)
            if self._here("large") or self._here("small"):  # what it takes once known is here
                self._auto_pending = self._target()
                self._warm_tier()
            return
        tier = self._choose_tier()
        if not self._here(tier):
            return
        logger.info("AI captions: on again for a video file, as for the last one")
        self._enable(tier)
        self._auto_on = True

    def target_changing(self, local_video: bool) -> None:
        """Something new is about to replace what the large picture shows (TVController, before
        it opens; False too when a phone or an outside player takes it): captions that came on
        by themselves for a video file go off first, unless it is a video file too, so that no
        film relay and no hold behind live follow them. Asked again, nothing more happens. One
        waiting for this computer's speed no longer comes on (the new one is asked anew). What
        they left on their way goes with them: a download's question QiTV asked for them again
        (another model chosen), or their wait for the speed."""
        self._auto_pending = None
        if self._auto_on and not local_video:
            self._auto_on = False
            self._toggle_waits = False
            if self._asking:
                self._asking = self._asking_step = False
                if self._card is not None:
                    self._card.withdraw()
            self._disable()

    def _plays_video_file(self) -> bool:
        """The large picture plays a video file (a song is no video)."""
        return bool(getattr(self._target(), "video_file", False))

    def _own_subtitles_shown(self) -> bool:
        """The large picture shows one of its own subtitle tracks (TVController)."""
        controller = getattr(self._window, "controller", None)
        shown = getattr(controller, "own_subtitles_shown", None)
        return callable(shown) and bool(shown())

    def downloading(self) -> bool:
        """The speech model downloads: they are on their way (the row's tap calls them off)."""
        return self._download is not None

    def asking(self) -> bool:
        """The download's question is up: on the TV, and on a phone that turned them on."""
        return self._asking

    def question(self) -> tuple[str, str, str]:
        """The download's question as asked, and its answers' words (yes, no)."""
        return self._question

    def asked_unprompted(self) -> bool:
        """The question is QiTV's own (Automatic's step down, not a viewer's turning them on):
        every phone remote hears it (ui/remote_controller.py)."""
        return self._asking and self._asking_step

    def answer(self, yes: bool) -> None:
        """The question answered elsewhere (2.9: on a phone remote): the TV's card goes, and
        it is as if it had been answered there."""
        if not self._asking:
            return
        if self._card is not None:
            self._card.withdraw()
        self._on_answer(yes)

    def handle(self, action: Any) -> bool:
        """Keys go to the question while it shows."""
        card = self._card
        if card is not None and self._asking and not card.isHidden():
            return bool(card.handle(action))
        return False

    def languages(self) -> list[tuple[str, str]]:
        from services.captions.languages import CAPTION_LANGUAGES, own_name

        return [(name, own_name(name)) for name in CAPTION_LANGUAGES]

    def language(self) -> tuple[str, str]:
        name, source = self._language
        return (name or "auto", translate(source))

    def set_language(self, name: str) -> None:
        """The viewer's pick for this channel, film or video file, and for a song's folder
        (`_pick_key`; "auto": the model's own detection). 2.11.1 (ruling I1): Auto on the
        viewer's own file also forgets what was heard on it, and its run reads it again with
        no language (a run's vote, once it agreed, stays): Auto hears it anew."""
        key = self._pick_key()
        picks = dict(self._config_get("ai_captions_picks", {}) or {})
        if name == "auto":
            picks.pop(key, None)
        elif key:
            picks[key] = name
        self._config_set("ai_captions_picks", picks)
        again = name == "auto" and self._own_file(self._target()) is not None
        if again:
            self._detected.pop(self._heard_key(), None)
        self._resolve_language()
        if again and self._enabled and self._ready:
            self._read_again()
        else:
            self._process.send("language", self._language[0])
        self.changed.emit()
        self._offer_specialist()

    # ---- 2.10: translated into the language the viewer picks ------------------------------
    def translate_to(self) -> str:
        """The language AI captions are translated into, as the viewer chose it (MiLMMT's
        name; "": none). Its model may not be here yet (asked for, or on its way)."""
        from services.captions.mt_languages import MT_LANGUAGES

        name = str(self._config_get(_TRANSLATE_TO, "") or "")
        return name if name in MT_LANGUAGES else ""

    def translate_languages(self) -> list[tuple[str, str]]:
        """The Translate to list's languages, Off aside, as (MiLMMT's name, its own name): the
        app language's first when MiLMMT has it, then the rest by their own names (as Settings
        sorts the captions' languages)."""
        from services import i18n
        from services.captions.mt_languages import MT_LANGUAGES, app_default, own_name

        first = app_default(i18n.language())
        rest = sorted(
            (name for name in MT_LANGUAGES if name != first),
            key=lambda name: own_name(name).casefold(),
        )
        return [(name, own_name(name)) for name in ([first] if first else []) + rest]

    def set_translate_to(self, name: str) -> None:
        """The language AI captions are translated into ("": none), kept as Settings keep
        theirs. A language whose model isn't here asks its download's question first, while
        they are on (else as they start; not now: none); "" takes back that question, or calls
        off that download."""
        from services.captions.models import TRANSLATION_KEY
        from services.captions.mt_languages import MT_LANGUAGES

        if name and name not in MT_LANGUAGES:
            return
        before = self._translation()
        if not name and self._fetching == TRANSLATION_KEY:
            if self._download is not None:
                self._cancel_download()
            elif self._asking:
                self._asking = self._asking_step = False
                if self._card is not None:
                    self._card.withdraw()
        self._config_set(_TRANSLATE_TO, name)
        logger.info("AI captions: translated into %s", name or "nothing")
        self._follow_translation(before)
        self._offer_translation()
        self.changed.emit()

    def _translation(self) -> str:
        """What AI captions are translated into now: the language chosen, once its model is
        here ("": nothing; then no hold, no step to the 0.6B, nothing for the process)."""
        from services.captions.models import TRANSLATION_KEY

        target = self.translate_to()
        return target if target and self._here(TRANSLATION_KEY) else ""

    @staticmethod
    def _translation_folder() -> str:
        from services.captions import models

        return str(models.captions_root() / models.TRANSLATION_FOLDER)

    def _translate_message(self) -> tuple:
        """The process's ("translate", ...): the language and its model's folder, or None."""
        now = self._translation()
        return ("translate", now, self._translation_folder()) if now else ("translate", None)

    def _follow_translation(self, before: str) -> None:
        """What they are translated into changed from `before` ("": nothing). Turned on or off,
        Automatic's speech model may change: turned on with it here, it takes over while they go
        on (R25, `_switch`); else they come on again with it, as with a model chosen.
        Else the process translates from now on, and the picture keeps its distance for it as it
        plays: raised, it waits there once; lowered, stay-live steers it back. A film's sound is
        read again from further ahead (turned off, it reads on, ahead as it is)."""
        now = self._translation()
        if now == before:
            return
        turned = bool(now) != bool(before)
        switched = False
        if turned and (self._enabled or self._download is not None):
            tier = self._choose_tier()
            if tier != self._tier and now and self._enabled and self._here(tier):
                # R25: Automatic's 0.6B, here, takes over while they go on (the captions written
                # stay, the picture too); the load carries the translation
                self._switch(tier, stepped=False)
                switched = True
            elif tier != self._tier:
                self._disable()
                self._toggle()
                return
        if self._process.running() and not switched:
            self._process.send(*self._translate_message())
        if not turned:
            return  # another language: held as it is
        if self._floored is not None:
            self._floored.set_caption_floor(self._picture_lead(), shift=False)
        if now and self._enabled and self._ready and self._session_key and not self._is_live():
            self._start_run()

    def _offer_translation(self) -> None:
        """A language to translate into whose model isn't here: its download's question, while
        they are on and nothing else asks or downloads (else when the next run starts), as a
        language's own speech model's (QiTV's own: every phone hears it)."""
        from services.captions import models

        if not self.translate_to() or not self._enabled:
            return
        if self._here(models.TRANSLATION_KEY) or self._asking or self._download is not None:
            return
        if self._card is None:
            return
        try:
            missing = models.download_size(models.TRANSLATION_KEY)
        except models.Unsupported:
            logger.warning("AI captions: no translation model suits this computer")
            self._config_set(_TRANSLATE_TO, "")
            self.changed.emit()  # the row (and the phones) say Off
            return
        self._fetching = models.TRANSLATION_KEY
        if missing == 0:  # here already, only to be checked
            self._start_download()
            return
        self._asking = self._asking_step = True
        self._question = (
            translate(TRANSLATE_QUESTION, size=size_text(missing)),
            translate(DOWNLOAD),
            translate(NOT_NOW),
        )
        self._card.ask(*self._question)
        self.changed.emit()  # the phones ask too (asked_unprompted)

    # ---- the speech model (2.9: on the AI captions row, a game's ‹ quality ›; Settings' too) --
    def model(self) -> str:
        """The viewer's speech model: "auto" (by this computer's speed), "small" or "large"."""
        name = str(self._config_get("ai_captions_model", "auto") or "auto")
        return name if name in MODEL_NAMES else "auto"

    def model_choices(self) -> list[tuple[str, str]]:
        """The row's ‹ › steps, in order, as (name, text): "Automatic (1.7B)" (the one it takes
        now), "Faster (0.6B)", "More accurate (1.7B)"."""
        texts = model_texts()
        auto = f"{texts['auto']} ({MODEL_SIZES[self._auto_tier()]})"
        return [("auto", auto), ("small", texts["small"]), ("large", texts["large"])]

    def model_text(self) -> str:
        """The model chosen, as the row's ‹ › shows it."""
        return dict(self.model_choices())[self.model()]

    def step_model(self, delta: int) -> None:
        """The row's ‹ or ›: the model before or after the one chosen (round the three)."""
        names = list(MODEL_NAMES)
        self.set_model(names[(names.index(self.model()) + (1 if delta > 0 else -1)) % len(names)])

    def set_model(self, name: str) -> None:
        """The viewer's speech model, kept as Settings keeps it. While they are on (or on their
        way) with another model, they come on again with this one: its download's question
        first, when it isn't here yet."""
        if name not in MODEL_NAMES:
            return
        self._config_set("ai_captions_model", name)
        if (self._enabled or self._download is not None) and self._choose_tier() != self._tier:
            self._disable()
            self._toggle()
        elif self._asking and not self._asking_step and self._choose_tier() != self._fetching:
            # Asked to download another one (2.9, the review: the phone stepped to Faster
            # while the TV asked for the 1.7B, and Download fetched the 1.7B): asked again.
            self._asking = False
            if self._card is not None:
                self._card.withdraw()
            self._toggle()
        self.changed.emit()

    def _auto_tier(self) -> str:
        """What Automatic takes now: the 0.6B once the 1.7B couldn't keep up (see _step_down),
        else one already here, else by this computer's speed. Never measured here, on the GUI
        thread (2.9, the review: measured beside the benchmark's thread, each ran at half speed
        and the reference machine was given the 0.6B): until it is known, as if fast enough."""
        from services.captions import tier

        if self._stepped_down:
            return "small"
        score = tier.known_score()
        translating = bool(self._translation())
        # 2.10: translated on a computer too slow for the 1.7B and the translator both: the
        # 0.6B once it is here (R25: else the 1.7B here goes on, and the step down asks for the
        # 0.6B if it can't keep up; with neither here, choose_tier gives the 0.6B below)
        if translating and score is not None and tier.slow_to_translate(score):
            if self._here("small"):
                return "small"
        for name in ("large", "small"):
            if self._here(name):
                return name
        if score is None:
            self._warm_tier()
            return str(tier.choose_tier("auto", score=tier.LARGE_MIN_SCORE))
        return str(tier.choose_tier("auto", score=score, translating=translating))

    def _speed_unknown(self) -> bool:
        """Automatic would pick by this computer's speed, not measured yet."""
        from services.captions import tier

        if self.model() != "auto" or self._stepped_down:
            return False
        if (self._here("large") or self._here("small")) and not self._translation():
            return False  # one here: Automatic takes it (translated: by the speed first)
        return tier.known_score() is None and not self._unmeasured

    def _picture_lead(self) -> float:
        """The picture's distance behind live: the model's, and TRANSLATE_LEAD_S more while
        they are translated (2.10: on every channel, one in the language chosen too)."""
        lead = LEAD_S.get(self._tier, 8.0)
        return lead + TRANSLATE_LEAD_S if self._translation() else lead

    def _film_ahead(self) -> float:
        """How far past the picture a film's sound is read: FILM_AHEAD_S, and TRANSLATE_LEAD_S
        more while they are translated."""
        return FILM_AHEAD_S + TRANSLATE_LEAD_S if self._translation() else FILM_AHEAD_S

    def _here(self, name: str) -> bool:
        """The model is installed, looked at once until a download or Settings' Delete changes
        it: the panel and the phones ask often."""
        from services.captions import models

        known = self._disk.get(name)
        if known is None:
            try:
                known = models.installed(name)
            except models.Unsupported:
                known = False
            self._disk[name] = known
        return known

    def shutdown(self) -> None:
        """QiTV closes: no model left running, no download going on."""
        self._timer.stop()
        self._restart.stop()
        self._cancel_download()
        self._process.stop()
        self._process.wait(4500)  # its quit, else the supervisor's kill (3 s + 1 s)

    def _cancel_download(self) -> None:
        download, self._download = self._download, None
        self._start_after_leaving = False
        if download is not None and not self._enabled:
            self._show("")  # its progress goes with it (on, the captions show: they stay)
        if download is not None and download is not self._leaving_download:
            download.cancel()
            self._leaving_download = download

    def _show_download(self) -> None:
        """The download's progress where the captions will be ("Downloading 12%", then
        "Preparing…"): answered, the question went, and nothing else said they were coming.
        Automatic's step down downloads while the captions show: only the row says it."""
        if self._download is not None and not self._enabled:
            self._show(self.status_text())

    def release(self, then: Any) -> None:
        """Settings deletes the files: captions off, the download cancelled, and `then()`
        once no process or download holds them (Windows keeps open files)."""
        self._disable()

        def done() -> None:
            leaving = self._leaving_download
            if self._process.busy() or (leaving is not None and leaving.running()):
                QTimer.singleShot(200, done)
                return
            then()
            self._disk.clear()  # the files went
            self._config_set(_LANGUAGE_MODELS_OK, False)  # the first question again, sizes and all
            self._stepped_down = False  # Automatic by this computer's speed again
            self.changed.emit()

        done()

    # ---- on and off --------------------------------------------------------------------
    def _on_answer(self, yes: bool) -> None:
        if not self._asking:
            return
        self._asking = self._asking_step = False
        first, self._first_question = self._first_question, False
        if not yes:
            from services.captions.models import TRANSLATION_KEY

            if self._fetching == TRANSLATION_KEY:
                self._config_set(_TRANSLATE_TO, "")  # 2.10: not now: not translated
            self.changed.emit()  # the question is gone (a phone's closes too)
            self._offer_translation()  # 2.10: one picked while this one was up
            return
        if first:  # 2.9.6, the owner: asked once, the sizes said; each language's comes alone
            self._config_set(_LANGUAGE_MODELS_OK, True)
        self._start_download()

    def _start_download(self) -> None:
        self._percent = 0
        self._unpacking = False
        leaving = self._leaving_download
        if leaving is not None and leaving.running():
            # A cancelled one still ends its current request: this one starts after it (two
            # would write the same file).
            self._download = leaving
            self._start_after_leaving = True
            self._show_download()
            self.changed.emit()
            return
        self._begin_download()

    def _begin_download(self) -> None:
        from services.captions.download import CaptionsDownload

        self._disk.clear()  # what is here changes from now on
        download = CaptionsDownload(self)
        # Each download's signals are its own: a cancelled one's late answer changes nothing.
        download.progress.connect(
            lambda done, total: self._on_download_progress(download, done, total)
        )
        download.preparing.connect(lambda: self._on_download_preparing(download))
        download.finished.connect(lambda: self._on_downloaded(download))
        download.failed.connect(lambda reason: self._on_download_failed(download, reason))
        self._download = download
        self._leaving_download = None
        self._start_after_leaving = False
        download.start(self._fetching)
        self._show_download()
        self.changed.emit()

    def _on_download_progress(self, download: Any, done: Any, total: Any) -> None:
        if download is not self._download:
            return
        percent = int(100 * done / total) if total > 0 else 0
        if percent != self._percent:
            self._percent = percent
            self._show_download()
            self.changed.emit()

    def _on_download_preparing(self, download: Any) -> None:
        if download is not self._download:
            return
        self._percent = 100
        self._unpacking = True
        self._show_download()
        self.changed.emit()

    def _on_downloaded(self, download: Any) -> None:
        if self._left(download):
            return
        self._disk.clear()
        self._unpacking = False
        self._download = None
        from services.captions.models import SPECIALISTS, TRANSLATION_KEY

        if self._fetching == TRANSLATION_KEY:  # 2.10: translated from now on
            self._follow_translation("")
            self._offer_specialist()  # one wanted while this one downloaded (every phone hears)
            self.changed.emit()
            return
        if self._fetching in SPECIALISTS:  # 2.9.6: a language's own model is here
            if self._enabled:
                self._reload()
            else:
                self.changed.emit()
            return
        if self._enabled:  # Automatic's step down, fetched while the 1.7B captioned
            if self.model() == "auto" and self._fetching != self._tier:
                self._switch(self._fetching)
            else:
                self.changed.emit()  # the viewer chose a model meanwhile: theirs stays
            return
        self._show("")
        self._enable(self._fetching)

    def _on_download_failed(self, download: Any, reason: str) -> None:
        if self._left(download):
            return
        self._disk.clear()
        self._download = None
        self._unpacking = False
        if not self._enabled:  # else the captions show on (a step down's download failed)
            self._show("")
        from services.captions.download import CANCELLED
        from services.captions.models import TRANSLATION_KEY

        if reason != CANCELLED:
            self._toast(reason, "error")  # already the whole sentence ("Download failed: …")
            if self._fetching == TRANSLATION_KEY:
                self._config_set(_TRANSLATE_TO, "")  # 2.10: not translated (a pick asks again)
            self._offer_faster()
        self.changed.emit()

    def _offer_faster(self) -> None:
        """Automatic's 1.7B didn't download (2.9.1: it comes from a community export, which can
        go): the 0.6B instead, its question first, as QiTV's own (every phone hears it)."""
        if self._fetching != "large" or self.model() != "auto" or self._enabled:
            return
        from services.captions import models

        try:
            missing = models.download_size("small")
        except models.Unsupported:
            return
        if self._card is None:
            return
        self._asking = self._asking_step = True
        self._tier = self._fetching = "small"
        self._question = (
            translate(FALLBACK_QUESTION, size=size_text(missing)),
            translate(DOWNLOAD),
            translate(NOT_NOW),
        )
        self._card.ask(*self._question)

    def _left(self, download: Any) -> bool:
        """A cancelled download ended: nothing of it shows, and one asked for meanwhile
        starts now. True when `download` is not the current one."""
        if download is self._leaving_download:
            self._leaving_download = None
            self._disk.clear()  # what it left (a ".part", an archive not unpacked)
            if self._start_after_leaving:
                self._begin_download()
            return True
        return download is not self._download

    def _enable(self, tier: str) -> None:
        self._tier = tier
        self._enabled = True
        self._starting = time.monotonic()  # "Starting captions…" till their first words
        self._forget_cues()
        self._resolve_language()
        self._hold_floor()
        self._route_film()  # a provider's film: through the film relay while the model loads
        if not self._process.running():
            self._load_model()
        elif self._ready:
            self._start_run()
        self._timer.start()
        self.changed.emit()

    def _load_model(self) -> None:
        """The captions process, loading the model of `_tier` (then `_on_ready`)."""
        from services.captions import models
        from services.captions.tier import model_threads

        paths = models.model_paths(self._tier)
        runtime = models.runtime_dir()
        translation = self._translation()
        self._loading, self._ready = True, False
        self._process.start(
            {
                "runtime": str(runtime) if runtime is not None else None,
                "model": {
                    "conv_frontend": str(paths.conv_frontend),
                    "encoder": str(paths.encoder),
                    "decoder": str(paths.decoder),
                    "tokenizer": str(paths.tokenizer),
                },
                "vad": str(paths.vad),
                # the specialists that are here (2.9.6: each comes once its language is wanted)
                "turkish": self._folder("turkish", paths.turkish),  # 2.10: our Turkish Moonshine
                "european": self._folder("european", paths.european),  # 2.9.3: Parakeet
                "cohere": self._folder("cohere", paths.cohere),  # 2.9.3: Cohere Transcribe
                # 2.10: the language they are translated into, and its model once it is here
                "translate_to": translation or None,
                "translation": self._translation_folder() if translation else None,
                "slow_to_translate": self._slow_to_translate(),  # 2.10 (an older child ignores it)
                "threads": model_threads(),
            },
            detailed_log=bool(self._config_get("detailed_log", False)),
        )

    def _slow_to_translate(self) -> bool:
        """This computer is slow to translate (tier.slow_to_translate, by the score already
        known; unknown counts as not slow): the child reads Turkish plainly while translating."""
        from services.captions import tier

        score = tier.known_score()
        return score is not None and tier.slow_to_translate(score)

    # ---- Automatic steps down (2.9: the owner's call) -------------------------------------
    def _behind(self) -> bool:
        """The model has been slow and late for `_SLOW_FOR_S`: it can't keep up."""
        since = self._slow_since
        return since is not None and time.monotonic() - since >= _SLOW_FOR_S

    def _steps_down(self) -> bool:
        """Automatic's 1.7B can't keep up, and nothing has been tried for it yet."""
        return (
            not self._step_tried
            and self._qwen_writes
            and self._behind()
            and self._ready
            and self._tier == "large"
            and self._download is None
            and not self._asking
            and self.model() == "auto"
        )

    def _step_down(self) -> None:
        """The 0.6B instead, from now on while QiTV runs; its download's question first, once,
        when it isn't here ("Not now": the 1.7B goes on, "Can't keep up")."""
        self._step_tried = True
        if self._here("small"):
            self._switch("small")
            return
        from services.captions import models

        try:
            missing = models.download_size("small")
        except models.Unsupported:
            return
        self._fetching = "small"
        if missing == 0:  # all here already, only to be checked and unpacked
            self._start_download()
            return
        if self._card is None:
            return
        self._asking = self._asking_step = True
        self._question = (
            translate(STEP_DOWN_QUESTION, size=size_text(missing)),
            translate(DOWNLOAD),
            translate(NOT_NOW),
        )
        self._card.ask(*self._question)
        self.changed.emit()  # the phones ask too (asked_unprompted)

    def _folder(self, key: str, folder: Any) -> Optional[str]:
        return str(folder) if self._here(key) else None

    def _offer_specialist(self) -> None:
        """2.9.6: the run's language is written best by a model that isn't here: asked for
        once while QiTV runs, on the TV and every phone (Qwen writes the language meanwhile,
        where it knows it)."""
        from services.captions import models
        from services.captions.languages import own_name, specialist_for

        key = specialist_for(self._language[0])
        if key is None or key in self._offered or not self._enabled or self._here(key):
            return
        if self._asking or self._download is not None or self._card is None:
            return
        try:
            missing = models.download_size(key)
        except models.Unsupported:
            return
        self._offered.add(key)
        self._fetching = key
        if missing == 0 or self._config_get(_LANGUAGE_MODELS_OK, False):
            # here already (only to be checked and unpacked), or the first question's yes was
            # for it too: it downloads while Qwen writes, the row says how far
            self._start_download()
            return
        self._asking = self._asking_step = True  # QiTV's own question: every phone hears it
        self._question = (
            translate(
                SPECIALIST_QUESTION, language=own_name(self._language[0]), size=size_text(missing)
            ),
            translate(DOWNLOAD),
            translate(NOT_NOW),
        )
        self._card.ask(*self._question)

    def _reload(self) -> None:
        """The captions process again, with the models that are here now: what it wrote stays,
        and it reads on from the picture once loaded (`_on_ready`)."""
        logger.info("AI captions: %s's model is here; loading it", self._fetching)
        self._process.stop()
        self._slow_since = None
        self._newest = self._written = None
        self._load_model()
        self.changed.emit()

    def _switch(self, tier: str, stepped: bool = True) -> None:
        """Captions on with `tier` instead, at once: the captions written so far stay (the
        timeline is the same), the picture stays where it is, and the new model reads on
        from it once loaded (`_on_ready`). `stepped`: Automatic's step down (kept while QiTV
        runs, and said); else translation turned on (2.10, R25: nothing said, nothing kept)."""
        if stepped:
            logger.info("AI captions: the %s model can't keep up, the %s instead", self._tier, tier)
            self._stepped_down = True
        else:
            logger.info("AI captions: translated: the %s model instead of the %s", tier, self._tier)
        self._process.stop()
        self._tier = tier
        self._slow_since = None
        self._newest = self._written = None
        self._load_model()
        if stepped:
            self._toast(translate(STEPPED_DOWN))
        self.changed.emit()

    def _disable(self) -> None:
        self._cancel_download()
        if self._asking_step:  # Automatic's question goes with them
            self._asking = self._asking_step = False
            if self._card is not None:
                self._card.withdraw()
        self._enabled = False
        self._timer.stop()
        self._restart.stop()
        self._process.stop()  # the model's memory back (a GB or two)
        self._loading = self._ready = False
        self._starting = None
        self._session_key = ""
        self._newest = self._written = None
        self._forget_run()
        self._run_began = None
        self._routed = ""  # a film relay stays until its title ends; on again, asked again
        self._drop_floor()
        self._show("")
        self.changed.emit()
        self.soundRead.emit()  # (no run reads a film now: the visualizer reads it)

    # ---- the process ---------------------------------------------------------------------
    def _on_ready(self) -> None:
        self._loading, self._ready = False, True
        if self._starting is not None:
            self._starting = time.monotonic()  # START_WAIT_S counts from here
        if self._enabled:
            self._start_run()
        self.changed.emit()

    def _on_failed(self, reason: str) -> None:
        was = self._enabled
        logger.warning("AI captions stopped: %s", reason)
        self._loading = self._ready = False
        self._starting = None
        if reason != "gone":
            self._process.stop()  # it did not load: no child left without a model
        if was:
            self._enabled = False
            self._cancel_download()  # a step down's: captions off, nothing comes on by itself
            if self._asking_step:
                self._asking = self._asking_step = False
                if self._card is not None:
                    self._card.withdraw()
            self._routed = ""
            self._timer.stop()
            self._drop_floor()
            self._show("")
            self._toast(translate(COULDNT_START), "error")
        self.changed.emit()

    def _start_run(self) -> None:
        self._restart.stop()  # this is the start a pending restart was for
        if not self._enabled or not self._ready:
            return
        self._offer_translation()  # 2.10: a language chosen whose model isn't here (any more)
        if not self._is_live():
            self._start_film()
            return
        live = self._live()
        after, self._resync_at = self._resync_at, None
        read_on, self._read_on = self._read_on, False
        source = None
        if live is not None:
            source = live.caption_source(after) if after is not None else live.caption_source()
            if source is None and after is not None and self._resync_tries >= RESYNC_TRIES:
                logger.info("AI captions: past the jump is not there: read from the picture")
                after, source = None, live.caption_source()
        if source is None:
            if after is not None:  # the segment after the jump is not there yet
                self._resync_at, self._read_on = after, read_on
                self._resync_tries += 1
                self._restart.start()
                return
            self._session_key = ""
            self.changed.emit()
            return
        self._resync_tries = 0
        url, _seq, start = source
        self._run += 1
        self._run_began = time.monotonic()
        self._run_channel = self._heard_key()
        self._run_start = start
        self._run_track = self._audio_track()
        self._resolve_language()  # the language of the track it reads, as it begins
        self._session_key = url.rsplit("/side/", 1)[0]
        self._newest = self._written = None
        self._process.send(
            "start",
            self._run,
            url,
            start,
            self._run_track,
            self._language[0],
            self._guessed,
            read_on,
        )
        self._process.send("viewer", self._position())
        self._offer_specialist()

    def _start_film(self) -> None:
        film = self._film()
        if film is None or not self._film_placed():
            self._session_key = ""  # the tick starts it once the film plays where it should
            self.changed.emit()
            return
        location, options = film
        start = self._position() + self._film_ahead()
        self._run += 1
        self._run_channel = self._heard_key()
        self._run_start = start
        self._run_track = self._audio_track()
        self._resolve_language()  # the language of the track it reads, as it begins
        self._session_key = location
        self._newest = self._written = None
        if self._levels_wanted:  # (a process started anew has them off)
            self._process.send("levels", True)
        self._process.send(
            "film", self._run, location, start, self._run_track, self._language[0], options
        )
        self._process.send("viewer", self._position())
        self.soundRead.emit()  # the visualizer reads no further than where this run reads from
        if self._language[0] == "Turkish" and self._own_file(self._target()) is not None:
            # 2.11.1 (ruling M1): a file of the viewer's that starts in Turkish (its track's,
            # or heard on it before) asks for our Turkish model as a channel's run does; one
            # asked while another question showed comes now. A provider's film as before.
            self._offer_specialist()

    def _on_caption(
        self,
        run: int,
        start: float,
        end: float,
        text: str,
        parts: Any = (),
        original: str = "",
    ) -> None:
        """A line written. A line of more than one part (`parts`: (text, start, end) each, its
        sentences and the stretches between long pauses) shows its parts as each is said, by
        their times (`_page_at`). A translated one (`original`: the line as heard; its parts
        are the original's) shows its translation as a line of one part did before, and keeps
        the original, timed by its parts, for a Stage (`_lines_of`)."""
        if run != self._run or not self._enabled:
            return
        said = [tuple(part) for part in parts] or [(original or text, start, end)]
        heard = " ".join(str(part[0]) for part in said)
        heard_timing = [(_words(str(part[0])), float(part[1]), float(part[2])) for part in said]
        if original:
            timing = [(_words(text), start, end)]
        else:
            text, timing = heard, heard_timing
        i = bisect.bisect_left(self._cues, (start, end, text))
        if i < len(self._cues) and self._cues[i][0] == start:
            self._cues[i] = (start, end, text)  # read again over a stretch: the newer words
        elif i > 0 and self._cues[i - 1][0] < start and self._cues[i - 1][1] == end:
            return  # read again: it is the end of the line before, joined to it
        else:
            self._cues.insert(i, (start, end, text))
        self._timing[start] = timing
        if original:
            self._originals[start] = (heard, heard_timing)
        else:
            self._originals.pop(start, None)
        if (
            i > 0
            and self._shows(i - 1) > self._position()
            and self._joins(self._cues[i - 1], self._cues[i])
        ):
            before = self._cues[i - 1]
            self._cues[i - 1] = (before[0], end, f"{before[2]} {text}")
            del self._cues[i]
            known = self._timing.pop(before[0], [(_words(before[2]), before[0], before[1])])
            self._timing[before[0]] = known + self._timing.pop(start, timing)
            first = self._originals.pop(before[0], None)
            second = self._originals.pop(start, None)
            if first is not None or second is not None:
                first = first or (before[2], known)
                second = second or (text, timing)
                self._originals[before[0]] = (f"{first[0]} {second[0]}", first[1] + second[1])

    def _joins(self, before: tuple[float, float, str], line: tuple[float, float, str]) -> bool:
        """Whether `line` is the end of `before`, on its own (2.9.5, JOIN_GAP_S)."""
        start, end, text = line
        if not 0.0 <= start - before[1] <= JOIN_GAP_S or end - start > JOIN_LONGEST_S:
            return False
        together = f"{before[2]} {text}"
        overlay = next((o for o in self._overlays if hasattr(o, "pages")), None)
        if overlay is None:
            return len(together) <= JOIN_CHARS
        return len(overlay.pages(together)) <= max(1, len(overlay.pages(before[2])))

    def _on_progress(
        self,
        run: int,
        newest: float,
        rtf: float,
        written: Optional[float] = None,
        qwen_writes: bool = True,
    ) -> None:
        if run != self._run:
            return
        self._newest, self._rtf = newest, rtf
        if float(newest) > self._run_start + 0.5:
            self._deaf_runs = 0  # it hears
        self._qwen_writes = qwen_writes  # 2.9.6: only then does a step down help
        self._written = newest if written is None else written
        self._started()
        # Late, and for the model: a short line ("Yes.") costs the model a fixed while, so a
        # dialogue's speed reads slow while the silences between its lines keep the captions
        # ahead of the picture (2.9: 1.06 while a film's were 3-60 s ahead, all in time). A
        # model with sound waiting for it can't keep up whatever its speed reads (2.9.1, the
        # owner: the 1.7B at about 0.85 on live wrote each line after its words, none showed,
        # and nothing stepped down; a pause let it get ahead). Only being in time again ends
        # it: a dip of the speed doesn't.
        lead = self._lead()
        backlog = float(newest) - float(self._written)
        slow = rtf >= _SLOW_RTF or backlog > _BACKLOG_S
        if lead is not None and lead < LATE_LEAD_S and slow:
            if self._slow_since is None:
                self._slow_since = time.monotonic()
        elif lead is None or lead >= LATE_LEAD_S:
            self._slow_since = None

    def _on_detected(self, run: int, language: str) -> None:
        """No language was given and the pieces agreed on one: kept for the channel (the file:
        `_heard_key`) while QiTV runs (the next run there starts with it), and the panel says
        it."""
        if run != self._run:
            return
        key = self._run_channel  # the run's channel, not the one a zap just tuned
        if key:
            self._detected[key] = language
        if self._language[1] == FROM_MODEL:
            self._language = (language, FROM_MODEL)
            self.changed.emit()
        self._offer_specialist()

    def _on_resync(self, run: int, newest: float) -> None:
        """The stream's stamps jumped (a provider's restart): read again from the segment
        after the jump, not from the picture's (before it: the same jump again)."""
        if run == self._run and self._enabled:
            self._resync_at = max(float(newest), self._run_start + RESYNC_STEP_S)
            self._read_on = True  # once read again, it reads on past the next steps
            self._restart.start()

    def _forget_run(self) -> None:
        """What a run left for the next (where past a jump to read, how slow it was) let go: a
        new session's, or a run that heard nothing."""
        self._resync_at, self._resync_tries, self._read_on = None, 0, False
        self._slow_since = None

    def _deaf(self) -> bool:
        """The live run has placed no sound NO_SOUND_S after it began (the picture playing)."""
        began = self._run_began
        if began is None or self._deaf_runs >= 2 or not self._is_live() or self._waiting():
            return False
        if self._newest is not None and float(self._newest) > self._run_start + 0.5:
            return False
        return time.monotonic() - began >= NO_SOUND_S

    def _on_ended(self, run: int, reason: str) -> None:
        if run == self._run and self._enabled:
            self._restart.start()  # the reader fell behind or lost the stream: again, once

    # ---- every tick: the picture's place --------------------------------------------------
    def _tick(self) -> None:
        if not self._enabled:
            return
        self._started()  # (no progress from a run that did not start: START_WAIT_S anyway)
        if self._steps_down():
            self._step_down()
        if self._is_live() and self._live() is not self._floored:
            self._hold_floor()  # a swap or the multiview's focus: the large picture's own
        key = self._source()
        if key is None:
            self._route_film()  # a provider's film started without one (once per title)
            self._show("")
            return
        if not self._is_live() and not self._film_placed():
            self._show("")
            return  # a film opens (again) or goes to where it resumes: read from there after
        if self._ready and key != self._session_key:
            self._forget_cues()  # another channel or a new session: its own captions
            self._forget_run()  # ... read from its own picture, its speed judged anew
            self._deaf_runs = 0
            self._last = None
            self._resolve_language()  # its own language too
            self._start_run()
            self.changed.emit()
            return
        if self._ready and self._track_changed():
            self._hear_track()
            return
        if self._ready and self._deaf():
            logger.info(
                "AI captions: run %d heard nothing in %.0f s: read again from the picture",
                self._run,
                NO_SOUND_S,
            )
            self._forget_run()
            self._deaf_runs += 1
            self._start_run()
            return
        waiting = self._waiting()
        if waiting != self._was_waiting:
            self._was_waiting = waiting
            self.changed.emit()  # the row reads "Starting captions…", then as before
        if waiting:
            self._last = None  # the picture stands still on purpose: no seek, no caption
            self._show(translate(STARTING))
            return
        position = self._position()
        now = time.monotonic()
        if self._last is not None:
            moved = position - self._last[0]
            elapsed = now - self._last[1]
            # A jump either way (a pause or 1.03x is none): read again where the picture went,
            # unless the run there has read it already.
            if (moved > elapsed + SEEK_S or moved < -SEEK_S) and self._ready:
                self._last = (position, now)
                self._rate_from = None
                if not self._covered(position):
                    self._start_run()
                return
        self._last = (position, now)
        self._measure_rate(position, now)
        self._process.send("viewer", position)
        self._show(*self._showing(position))
        self._forget(position)

    def _showing(self, position: float) -> tuple[str, Optional[tuple[str, str, str]]]:
        """What shows at `position`: 2.10.1, "Starting captions…" until their first words are
        written (the picture playing), else the caption there and a Stage's lines."""
        if self._starting is not None:
            return translate(STARTING), None
        return self._caption_at(position)

    def _track_changed(self) -> bool:
        """Another audio track plays than the run reads (2.9: the owner switched a film's
        sound to another language, and the captions stayed in the first one's)."""
        track = self._audio_track()
        if track is None:
            return False  # the player is busy (an opening, a seek): looked at again next tick
        # A run begun while the player named none reads the media's default, which may not be
        # the one it plays (2.9, the review: a reopen chooses the viewer's track again): read
        # again with the one it names.
        return track != self._run_track

    def _hear_track(self) -> None:
        """The audio track the viewer chose: read from where the picture is, in its own
        language (the one the model heard was the other track's); what was written from the
        other track goes."""
        logger.info("AI captions: audio track %s, read again", self._audio_track())
        self._detected.pop(self._heard_key(), None)
        self._read_again()  # in the language of the track it reads
        self.changed.emit()  # the panel's language row says the track's

    def _read_again(self) -> None:
        """A new run from where the picture is, in the language resolved now; what was
        written goes."""
        self._forget_cues()
        self._last = None
        self._show("")
        self._start_run()

    def _waiting(self) -> bool:
        """The live picture waits for the captions' distance behind live (LiveSession)."""
        if not self._is_live():
            return False
        live = self._live()
        waiting = getattr(live, "caption_waiting", None)
        return bool(waiting()) if callable(waiting) else False

    def _cue_at(self, position: float) -> str:
        return self._caption_at(position)[0]

    def _caption_at(self, position: float) -> tuple[str, tuple[str, str, str]]:
        """What shows at `position`: the caption's text (its page said by then; "" for none),
        and a Stage's lines (`_lines_of`)."""
        i = bisect.bisect_right(self._cues, (position - FIRST_WORD_S, float("inf"), "")) - 1
        while i >= 0 and self._shows(i) > position:
            i -= 1  # it waits for the one before to be read
        if i < 0:
            return "", NO_LINES
        start, end, text = self._cues[i]
        if start <= self._gone:
            return "", NO_LINES  # it went: a later line that came late can't bring it back
        shows = self._shows(i)
        following = self._shows(i + 1) if i + 1 < len(self._cues) else None
        if following is not None and following - end <= BRIDGE_S:
            until = following  # a short pause: on until the next line
        else:
            until = max(end + HOLD_S * self._scale(), shows + self._reading(text))
            if following is not None:
                until = min(until, following)
        if position >= until:
            self._gone = max(self._gone, start)  # 2.9.6, the real-use review: it came back
            return "", NO_LINES
        if position < shows:
            return "", NO_LINES
        page = self._page_at(i, position, shows)
        return page, self._lines_of(i, position, shows, page)

    def _lines_of(self, i: int, position: float, shows: float, page: str) -> tuple[str, str, str]:
        """A Stage's lines while cue `i` shows `page` (Plan 4): the cue before, as heard, when it
        ended at most PREVIOUS_LINE_S before this one started ("" otherwise, and for none); the
        line said, as heard, its part said by then (by its parts' times, as the captions page
        theirs); and the translation shown ("" when it isn't one)."""
        if not page:
            return NO_LINES
        start, end, _text = self._cues[i]
        recent = i > 0 and self._cues[i - 1][1] >= start - PREVIOUS_LINE_S
        previous = self._heard(i - 1) if recent else ""
        original = self._originals.get(start)
        if original is None:
            return previous, page, ""
        heard, timing = original
        return previous, self._paged(heard, timing, start, end, position, shows), page

    def _heard(self, i: int) -> str:
        """Cue `i` as heard: its original when it was translated."""
        start, _end, text = self._cues[i]
        original = self._originals.get(start)
        return original[0] if original is not None else text

    def _forget_cues(self) -> None:
        self._cues.clear()
        self._timing.clear()
        self._originals.clear()
        self._gone = float("-inf")

    def _shows(self, i: int) -> float:
        """When cue `i` shows: at its first word, or once the one before has been read, by at
        most LAG_S later (2.9.5)."""
        cues = self._cues
        lag = LAG_S * self._scale()  # (as long for the viewer at any rate)
        j = i
        while j > 0 and i - j < _LAG_LOOK:
            before_start, _, before_text = cues[j - 1]
            if before_start + lag + self._reading(before_text) <= cues[j][0]:
                break  # the one before is read before this one's word: it starts on time
            j -= 1
        shows = cues[j][0] + FIRST_WORD_S
        for k in range(j + 1, i + 1):
            earliest = cues[k][0] + FIRST_WORD_S
            shows = max(earliest, min(shows + self._reading(cues[k - 1][2]), earliest + lag))
        return shows

    def _measure_rate(self, position: float, now: float) -> None:
        """The picture's playback rate over the last second or so (a seek or a wait: anew)."""
        if self._rate_from is None:
            self._rate_from = (position, now)
            return
        start, then = self._rate_from
        if now - then < 1.0:
            return
        rate = (position - start) / (now - then)
        if 0.0 <= rate <= 4.0:
            self._rate = 0.5 * self._rate + 0.5 * rate
        self._rate_from = (position, now)

    def _scale(self) -> float:
        """How much longer, in the picture's seconds, a caption stays to be read: the playback
        rate from FAST_FROM on, at most RATE_CAP (1x, or stay-live's gentle steering: as it is)."""
        return 1.0 if self._rate < FAST_FROM else min(self._rate, RATE_CAP)

    def _reading(self, text: str) -> float:
        return max(MIN_SHOW_S, len(text) / READ_CPS) * self._scale()

    def _page_at(self, i: int, position: float, shows: float) -> str:
        """Cue `i`, shown since `shows`, at `position`: the whole of it, or the part of it said
        by then when it doesn't fit two lines (2.9.1, the owner: a long one ended in "…"). Its
        parts end at sentences' ends first (the overlay's `pages`), and each shows when its first
        words are said, as the line's parts' times tell (2026-11-08, the owner: "after the dot
        we can split and show the other part later"), on screen for the one before's reading
        time (by at most LAG_S past its words) and PAGE_S at least; with no times, each for its
        share of the words by its letters. The last one is held."""
        start, end, text = self._cues[i]
        return self._paged(text, self._timing.get(start, []), start, end, position, shows)

    def _paged(
        self,
        text: str,
        timing: list[tuple[int, float, float]],
        start: float,
        end: float,
        position: float,
        shows: float,
    ) -> str:
        """`text` of a cue of [start, end), its words' times `timing`, as `_page_at` shows it
        (a translated cue's original too: `_lines_of`)."""
        overlay = next((o for o in self._overlays if hasattr(o, "pages")), None)
        pages = overlay.pages(text) if overlay is not None else [text]
        if len(pages) <= 1:
            return text
        if sum(n for n, _a, _b in timing) != sum(_words(page) for page in pages):
            return self._share_at(pages, position - shows, end - start - FIRST_WORD_S)
        on, said = shows, 0
        for k, page in enumerate(pages):
            if k:
                spoken = _word_time(timing, said)
                read = min(on + self._reading(pages[k - 1]), spoken + LAG_S * self._scale())
                on = max(spoken, on + PAGE_S * self._scale(), read)
            if position < on:
                return pages[k - 1] if k else page
            said += _words(page)
        return pages[-1]

    @staticmethod
    def _share_at(pages: list[str], elapsed: float, words_s: float) -> str:
        """Of a caption's `pages`, the one said `elapsed` into its words (lasting `words_s`),
        each for its share of the words by its letters."""
        total = sum(len(page) for page in pages)
        said, span = 0, max(words_s, MIN_SHOW_S)
        for page in pages[:-1]:
            said += len(page)
            if elapsed < span * said / total:
                return page
        return pages[-1]

    def _covered(self, position: float) -> bool:
        """The run reads on from before `position` and has not passed it by long: no need to
        read again."""
        newest = self._newest if self._newest is not None else self._run_start
        return self._run_start <= position <= newest + 1.0

    def _forget(self, position: float) -> None:
        while self._cues and self._cues[0][1] < position - KEEP_S:
            start = self._cues.pop(0)[0]
            self._timing.pop(start, None)
            self._originals.pop(start, None)

    def _lead(self) -> Optional[float]:
        """How far ahead of the picture the captions are written (2.9, the review: not how far
        the sound is read, which a model too slow for it never shortened)."""
        if self._written is None:
            return None
        return float(self._written) - float(self._position())

    def add_overlay(self, overlay: Any) -> None:
        """Another place the captions show, as over the picture (2.9: the floating picture's,
        which had none: the captions showed on the fullscreen desktop left). Plan 4: one that
        shows lines (`show_lines`: the audio screen's Stage) gets those instead."""
        self._overlays.append(overlay)
        if _shows_lines(overlay):
            if self._lines != NO_LINES:
                overlay.show_lines(*self._lines)
            self.words_moved()  # its Stage may take the words already
        elif self._shown:
            overlay.show_text(self._shown)

    def words_moved(self) -> None:
        """A Stage took the words or gave them back (AudioScreen.stageShown: its Stage shown or
        gone): the captions show where they go now, at once (each tick looks again too)."""
        self._show(self._shown, self._lines)

    def _staging(self) -> tuple:
        """The pages (overlay hosts) where a sink shows the words itself now (`takes_words`: the
        audio screen while its Stage shows): the captions over the picture there give way."""
        pages = []
        for sink in self._overlays:
            takes = getattr(sink, "takes_words", None)
            if callable(takes) and _alive(sink) and takes():
                page = _page_of(sink)
                if page is not None:
                    pages.append(page)
        return tuple(pages)

    def _show(self, text: str, lines: Optional[tuple[str, str, str]] = None) -> None:
        """`text` over the picture, and `lines` (previous, current, translation) where lines
        show: `text` as the line said by default (what the captions say of themselves:
        "Starting captions…", a download's progress). The captions on a page whose Stage takes
        the words are cleared, and show what is said again once it gives them back."""
        if lines is None:
            lines = ("", text, "") if text else NO_LINES
        staged = self._staging()
        if text == self._shown and lines == self._lines and staged == self._staged:
            return
        shown, was_staged, new_lines = self._shown, self._staged, lines != self._lines
        self._shown, self._lines, self._staged = text, lines, staged
        for overlay in self._overlays:
            if not _alive(overlay):
                continue  # the floating picture's, gone as QiTV closes
            if not _shows_lines(overlay):
                self._show_text(overlay, (shown, text), (was_staged, staged))
            elif new_lines:
                overlay.show_lines(*lines)

    @staticmethod
    def _show_text(overlay: Any, texts: tuple[str, str], staged: tuple[tuple, tuple]) -> None:
        """The caption over the picture on `overlay` as it changes (`texts`: before, now);
        while a Stage on its page takes the words (`staged`: the pages whose Stage took them,
        before and now), cleared, and the caption again once it gives them back."""
        shown, text = texts
        page = _page_of(overlay)
        was_given, given = page in staged[0], page in staged[1]
        if given:
            if not was_given and shown:
                overlay.clear()  # the Stage has them
        elif was_given:
            if text:
                overlay.show_text(text)  # given back
        elif text != shown:
            if text:
                overlay.show_text(text)
            else:
                overlay.clear()

    # ---- live or a film ----------------------------------------------------------------------
    def _is_live(self) -> bool:
        target = self._target()
        return target is None or bool(getattr(target, "live", False))

    def _position(self) -> float:
        """Where the picture is: the time-shift timeline's second (live), the film's (a film)."""
        if self._is_live():
            live = self._live()
            return float(live.position()) if live is not None else 0.0
        session = self._session()
        try:
            return max(0.0, int(session.time_ms()) / 1000.0) if session is not None else 0.0
        except Exception:
            return 0.0

    def _source(self) -> Optional[str]:
        """What the captions read, as a key (another one: read again, its own captions);
        None where they can't read what plays."""
        if self._is_live():
            live = self._live()
            source = live.caption_source() if live is not None else None
            return source[0].rsplit("/side/", 1)[0] if source is not None else None
        film = self._film()
        return film[0] if film is not None else None

    def _film(self) -> Optional[tuple[str, tuple[str, ...]]]:
        """The film playing, as the captions process can open it, and its VLC options: a file
        on this computer, a film from Jellyfin or Emby, or a provider's film through the film
        relay it plays through (the picture's one stream, shared: reading the provider again
        would be a second stream, which many allow none of). None: nothing to read (a
        provider's film not through a relay yet, `_relay_wanted`; or one the relay can't carry)."""
        target = self._target()
        session = self._session()
        if target is None or getattr(target, "live", False) or session is None:
            return None
        location = str(getattr(session, "url", "") or "")
        if not location:
            return None
        if getattr(target, "local", False) or location.startswith("file:"):
            return location, ()
        from services.provider_ids import provider_of
        from services.provider_types import is_media_server

        name = provider_of(getattr(target, "item", None) or {})
        catalogs = getattr(self._window, "catalogs", None)
        try:
            entry = catalogs.provider(name) if catalogs is not None and name else None
        except KeyError:
            entry = None
        if entry is not None and is_media_server(entry.get("type")):
            return location, ()
        relay = self._relay()
        if relay is not None and relay.source == location and relay.followable():
            return relay.follower_url, ()
        return None

    def _film_placed(self) -> bool:
        """The film plays (or is paused) where it is meant to: opened, and gone to where it
        resumes (until then its time is an opening's, not where the captions should read)."""
        session = self._session()
        placed = getattr(session, "placed", None)
        return not callable(placed) or bool(placed())

    def _relay(self) -> Any:
        """The film relay the window's player opens its film through (None: none)."""
        session = self._session()
        relay = getattr(session, "relay", None)
        return relay() if callable(relay) else None

    def _relay_wanted(self) -> bool:
        """A provider's film plays that AI captions can read only through a film relay, and it
        does not play through one yet."""
        controller = getattr(self._window, "controller", None)
        wanted = getattr(controller, "film_relay_wanted", None)
        return callable(wanted) and bool(wanted()) and self._relay() is None

    def _route_film(self) -> None:
        """Captions are on and a provider's film plays without a relay: it plays on through one
        (opened again where it is), asked once per title while captions stay on."""
        if not self._enabled or not self._relay_wanted():
            return
        session = self._session()
        url = str(getattr(session, "url", "") or "")
        if not url or url == self._routed:
            return
        self._routed = url
        controller = getattr(self._window, "controller", None)
        try:
            controller.relay_film()
        except Exception as error:
            logger.warning("AI captions: the film relay did not start: %s", type(error).__name__)

    # ---- what it reads from the window ------------------------------------------------------
    def _live(self) -> Any:
        """The large picture's live session (after a swap or in the multiview, not the main
        slot's)."""
        controller = getattr(self._window, "controller", None)
        live = getattr(controller, "live", None) if controller is not None else None
        return live if live is not None else getattr(self._window, "live_session", None)

    def _hold_floor(self) -> None:
        """The large picture's live session holds the captions' distance behind live; one that
        held it before and is no longer large lets it go, steered back (no time shift for a
        picture the viewer did not turn captions off on)."""
        live = self._live() if self._is_live() else None
        if live is self._floored:
            return
        self._drop_floor(shift=False)
        if live is not None:
            live.set_caption_floor(self._picture_lead())
            self._floored = live

    def _drop_floor(self, shift: bool = True) -> None:
        live, self._floored = self._floored, None
        if live is not None:
            live.set_caption_floor(0.0, shift=shift)

    def _started(self) -> None:
        """Turned on, the first run after the model loaded has written START_LEAD_S past the
        picture (or START_WAIT_S went by since it was ready): "Starting captions…" goes, and the
        captions show (2.10.1: the picture played on meanwhile; 2.10.0 held it)."""
        if self._starting is None or not self._ready:
            return
        lead = self._lead()
        if (lead is not None and lead >= START_LEAD_S) or (
            time.monotonic() - self._starting >= START_WAIT_S
        ):
            self._starting = None

    def _target(self) -> Any:
        controller = getattr(self._window, "controller", None)
        return controller.now_playing() if controller is not None else None

    def _session(self) -> Any:
        """The large picture's player (the controller's: a picture-in-picture swap moves it),
        whose film the captions read."""
        controller = getattr(self._window, "controller", None)
        session = getattr(controller, "session", None)
        return session if session is not None else getattr(self._window, "session", None)

    def _pick_key(self) -> str:
        """What the viewer's pick (`ai_captions_picks`) is kept under: a channel, a film or a
        recording by its source and title (2.9's "<source>|<title>"); 2.11.1: the viewer's own
        video file by its path (ruling I2: two S01E01.mkv in two shows are two files), a song by
        its folder ("|album|<folder>": one pick for the folder's songs)."""
        target = self._target()
        if target is None:
            return ""
        path = self._own_file(target)
        if path:
            from services.folder_shelf import shelf_key
            from services.open_paths import is_music

            if is_music(path):
                return f"|album|{shelf_key(os.path.dirname(path))}"
            return f"|file|{shelf_key(path)}"
        return self._title_key(target)

    def _heard_key(self) -> str:
        """What the language a run heard (`_detected`) is kept under, while QiTV runs: a
        channel, a film or a recording as its pick is; 2.11.1 (ruling I1): the viewer's own
        file, a song too, by its path (a folder may mix languages). "|album|" and "|file|" keys
        are no channel's: no file's name holds both | and /, and a channel's source is never
        empty."""
        target = self._target()
        if target is None:
            return ""
        path = self._own_file(target)
        if path:
            from services.folder_shelf import shelf_key

            return f"|file|{shelf_key(path)}"
        return self._title_key(target)

    @staticmethod
    def _title_key(target: Any) -> str:
        return f"{getattr(target, 'source', '')}|{getattr(target, 'title', '')}"

    @staticmethod
    def _own_file(target: Any) -> Optional[str]:
        """The path of the viewer's own file `target` plays (a song, a video file), else None:
        a channel, a provider's or a media server's film, and a recording, which is a
        channel's broadcast (2.11.1, ruling I3: it keeps the channel's rule). "" for one whose
        address names no path."""
        if not getattr(target, "local", False):
            return None
        item = getattr(target, "item", None)
        if not isinstance(item, dict) or item.get("_recording"):
            return None
        from services.song_facts import local_path

        return local_path(str(item.get("cmd") or "")) or ""

    def _audio_track(self) -> Optional[int]:
        session = self._session()
        try:
            track = int(session.audio_track()) if session is not None else -1
        except Exception:
            track = -1
        return track if track >= 0 else None

    def _resolve_language(self) -> None:
        """The viewer's pick for the channel, else the audio track's, else Settings', else
        the model's own detection: what it detected on the channel before, else a guess from a
        prefix on the channel's title or group ("TR: …", 2026-11-07), which the captions
        process may still correct (`_guessed`).

        2.11.1 (the owner: Words on an English song started in Settings' Turkish): Settings'
        default is for channels, their recordings and provider films (a media server's too).
        The viewer's own file, a song or a video file, has the viewer's pick (a song's: its
        folder's), else its audio track's, else what was heard on that file before (while QiTV
        runs), else the model's own detection, with no guess from its name."""
        from services.captions.languages import language_from_title, qwen_name

        self._guessed = False
        pick = (self._config_get("ai_captions_picks", {}) or {}).get(self._pick_key())
        if pick:
            self._language = (pick, FROM_VIEWER)
            return
        heard = self._track_language()
        if heard:
            self._language = (heard, FROM_AUDIO_TRACK)
            return
        target = self._target()
        own_file = self._own_file(target) is not None
        default = self._config_get("ai_captions_language", "auto")
        if default and default != "auto" and not own_file:
            self._language = (str(qwen_name(default) or default), FROM_DEFAULT)
            return
        detected = self._detected.get(self._heard_key(), "")
        if not detected and not own_file:
            item = getattr(target, "item", None)
            group = item.get("group") or item.get("category_name") if isinstance(item, dict) else ""
            detected = language_from_title(getattr(target, "title", ""), group)
            self._guessed = bool(detected)
        self._language = (detected, FROM_MODEL)

    def _track_language(self) -> str:
        """The language the playing audio track names, as Qwen's name ("" when none). The
        media's track ids are not the player's for a TS stream (1, 2 against the PIDs): the
        two lists pair in order (PlaybackSession.audio_track_info)."""
        from services.captions.languages import qwen_name

        session = self._session()
        if session is None or not hasattr(session, "audio_track_info"):
            return ""
        try:
            tracks = [t for t in session.audio_tracks() if t[0] != -1]
            info = session.audio_track_info()
            playing = self._audio_track()
            if playing is None:
                return ""  # the player busy: no track known, not the first one's language
            index = next((i for i, t in enumerate(tracks) if t[0] == playing), 0)
            track = next((t for t in info if t.id == playing), None)
            if track is None and index < len(info):
                track = info[index]
            code = track.language_code if track is not None else ""
            return str(qwen_name(code) or "") if code else ""
        except Exception as error:
            logger.debug("AI captions: no track language: %s", error)
        return ""

    def _choose_tier(self) -> str:
        override = self.model()
        return override if override in ("small", "large") else self._auto_tier()

    def _config_get(self, name: str, default: Any) -> Any:
        config = getattr(self._window, "config_manager", None)
        return getattr(config, name, default) if config is not None else default

    def _config_set(self, name: str, value: Any) -> None:
        config = getattr(self._window, "config_manager", None)
        if config is not None:
            try:
                setattr(config, name, value)
            except Exception as error:
                logger.debug("AI captions: couldn't keep %s: %s", name, error)

    def _toast(self, text: str, kind: str = "info") -> None:
        controller = getattr(self._window, "controller", None)
        toast = getattr(getattr(controller, "overlays", None), "toast", None)
        if toast is not None:
            toast.show_message(text, kind, 4000)
