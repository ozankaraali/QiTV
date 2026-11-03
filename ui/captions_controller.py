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
than none).

A film or an episode has no helper: the process plays its sound from `FILM_AHEAD_S` past the
picture (services.captions.film), on the film's own clock, from where the window's player reads
it: a file on this computer, or a Jellyfin or Emby server (no stream limit there). A provider's
film is read through a film relay (services.film_relay) instead of a second stream, which many
providers allow none of: the picture's one provider connection serves both. A film started while
captions are on plays through it from the start; turning them on during one opens it again there,
where it is (TVController.relay_film); turning them off keeps it until the title ends.
"""

from __future__ import annotations

import bisect
import logging
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.i18n import tr, translate

logger = logging.getLogger(__name__)

LEAD_S = {"small": 8.0, "large": 10.0}  # the picture's distance behind live while on
FILM_AHEAD_S = 6.0  # a film's sound is read from this far past the picture (after a seek too)
# "Catching up…" only when what has been heard is about to fall behind the picture: anything
# ahead of it is in time (2.9: a 1.7B film's captions 3-6 s ahead, all in time, said so at
# every dip under the 6 s it starts with).
LATE_LEAD_S = 1.0
HOLD_S = 0.6  # a caption stays this long after its words, unless the next one starts
MIN_SHOW_S = 1.2
# 2.9, the owner: captions came "a tiny bit" before the words, and went before the next line
# came ("hard to read"). A piece starts LEAD_IN_S (services.captions.pieces) before its first
# word: its caption shows from the word. A pause up to BRIDGE_S between two lines keeps the
# first on until the next; after a longer one it stays as long as it takes to read (READ_CPS
# characters a second), and never over the next.
FIRST_WORD_S = 0.2  # services.captions.pieces.LEAD_IN_S
BRIDGE_S = 3.0
READ_CPS = 15.0
TICK_MS = 250
SEEK_S = 2.0  # the position moved this much more than the clock: a seek, read again there
KEEP_S = 30 * 60.0  # cues kept this far behind the picture
_RESTART_MS = 1000
_SLOW_RTF = 0.9  # the model takes this long per second of speech: it can't keep up
_SLOW_FOR_S = 30.0

from ui.captions_texts import (  # noqa: E402  (the texts, with their translations)
    CANT_KEEP_UP,
    CATCHING_UP,
    COULDNT_START,
    DOWNLOAD,
    DOWNLOAD_QUESTION,
    DOWNLOADING,
    FROM_AUDIO_TRACK,
    FROM_DEFAULT,
    FROM_MODEL,
    FROM_VIEWER,
    NOT_AVAILABLE,
    NOT_NOW,
    PREPARING,
    STARTING,
    STEP_DOWN_QUESTION,
    STEPPED_DOWN,
    size_text,
)

MODEL_NAMES = ("auto", "small", "large")  # the AI captions row's ‹ › steps, in order
MODEL_SIZES = {"small": "0.6B", "large": "1.7B"}


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
        if card is not None:
            card.answered.connect(self._on_answer)
        self._enabled = False
        self._leaving_download: Any = None  # cancelled, still ending its request
        self._disk: dict[str, bool] = {}  # each model installed (see _here)
        self._start_after_leaving = False
        self._run_channel = ""  # the channel the run reads (a vote's language is its)
        self._resync_at: Optional[float] = None  # read again from past a jump here
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
        self._newest: Optional[float] = None  # the newest second the run has read
        self._written: Optional[float] = None  # the second its captions are written to
        self._rtf = 0.0
        self._slow_since: Optional[float] = None
        self._last: Optional[tuple[float, float]] = None  # (position, monotonic) last tick
        self._was_waiting = False  # the live picture waited for the captions' distance
        self._floored: Any = None  # the live session holding the captions' distance
        self._shown = ""
        self._language: tuple[str, str] = ("", FROM_MODEL)  # (qwen name or "", source)
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
            self.toggle()

    def enabled(self) -> bool:
        return self._enabled

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
            self._question = (
                translate(DOWNLOAD_QUESTION, size=size_text(missing)),
                translate(DOWNLOAD),
                translate(NOT_NOW),
            )
            self._card.ask(*self._question)

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
        from services.captions.languages import QWEN_LANGUAGES, own_name

        return [(name, own_name(name)) for name in QWEN_LANGUAGES]

    def language(self) -> tuple[str, str]:
        name, source = self._language
        return (name or "auto", translate(source))

    def set_language(self, name: str) -> None:
        """The viewer's pick for this channel ("auto": the model's own detection)."""
        key = self._channel_key()
        picks = dict(self._config_get("ai_captions_picks", {}) or {})
        if name == "auto":
            picks.pop(key, None)
        elif key:
            picks[key] = name
        self._config_set("ai_captions_picks", picks)
        self._resolve_language()
        self._process.send("language", self._language[0])
        self.changed.emit()

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
            self.toggle()
        elif self._asking and not self._asking_step and self._choose_tier() != self._fetching:
            # Asked to download another one (2.9, the review: the phone stepped to Faster
            # while the TV asked for the 1.7B, and Download fetched the 1.7B): asked again.
            self._asking = False
            if self._card is not None:
                self._card.withdraw()
            self.toggle()
        self.changed.emit()

    def _auto_tier(self) -> str:
        """What Automatic takes now: the 0.6B once the 1.7B couldn't keep up (see _step_down),
        else one already here, else by this computer's speed. Never measured here, on the GUI
        thread (2.9, the review: measured beside the benchmark's thread, each ran at half speed
        and the reference machine was given the 0.6B): until it is known, as if fast enough."""
        from services.captions import tier

        if self._stepped_down:
            return "small"
        for name in ("large", "small"):
            if self._here(name):
                return name
        score = tier.known_score()
        if score is None:
            self._warm_tier()
            score = tier.LARGE_MIN_SCORE
        return str(tier.choose_tier("auto", score=score))

    def _speed_unknown(self) -> bool:
        """Automatic would pick by this computer's speed, not measured yet."""
        from services.captions import tier

        if self.model() != "auto" or self._stepped_down:
            return False
        if self._here("large") or self._here("small"):
            return False
        return tier.known_score() is None and not self._unmeasured

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
        if download is not None:
            self._show("")  # its progress goes with it
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
            self._stepped_down = False  # Automatic by this computer's speed again
            self.changed.emit()

        done()

    # ---- on and off --------------------------------------------------------------------
    def _on_answer(self, yes: bool) -> None:
        if not self._asking:
            return
        self._asking = self._asking_step = False
        if not yes:
            self.changed.emit()  # the question is gone (a phone's closes too)
            return
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

        if reason != CANCELLED:
            self._toast(reason, "error")  # already the whole sentence ("Download failed: …")
        self.changed.emit()

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
        self._cues.clear()
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
                "threads": model_threads(),
            },
            detailed_log=bool(self._config_get("detailed_log", False)),
        )

    # ---- Automatic steps down (2.9: the owner's call) -------------------------------------
    def _behind(self) -> bool:
        """The model has been slow and late for `_SLOW_FOR_S`: it can't keep up."""
        since = self._slow_since
        return since is not None and time.monotonic() - since >= _SLOW_FOR_S

    def _steps_down(self) -> bool:
        """Automatic's 1.7B can't keep up, and nothing has been tried for it yet."""
        return (
            not self._step_tried
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

    def _switch(self, tier: str) -> None:
        """Captions on with `tier` instead, at once: the captions written so far stay (the
        timeline is the same), the picture stays where it is, and the new model reads on
        from it once loaded (`_on_ready`)."""
        logger.info("AI captions: the %s model can't keep up, the %s instead", self._tier, tier)
        self._stepped_down = True
        self._process.stop()
        self._tier = tier
        self._slow_since = None
        self._newest = self._written = None
        self._load_model()
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
        self._session_key = ""
        self._newest = self._written = None
        self._slow_since = None
        self._routed = ""  # a film relay stays until its title ends; on again, asked again
        self._drop_floor()
        self._show("")
        self.changed.emit()

    # ---- the process ---------------------------------------------------------------------
    def _on_ready(self) -> None:
        self._loading, self._ready = False, True
        if self._enabled:
            self._start_run()
        self.changed.emit()

    def _on_failed(self, reason: str) -> None:
        was = self._enabled
        logger.warning("AI captions stopped: %s", reason)
        self._loading = self._ready = False
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
        if not self._is_live():
            self._start_film()
            return
        live = self._live()
        after, self._resync_at = self._resync_at, None
        source = None
        if live is not None:
            source = live.caption_source(after) if after is not None else live.caption_source()
        if source is None:
            if after is not None:  # the segment after the jump is not there yet
                self._resync_at = after
                self._restart.start()
                return
            self._session_key = ""
            self.changed.emit()
            return
        url, _seq, start = source
        self._run += 1
        self._run_channel = self._channel_key()
        self._run_start = start
        self._run_track = self._audio_track()
        self._resolve_language()  # the language of the track it reads, as it begins
        self._session_key = url.rsplit("/side/", 1)[0]
        self._newest = self._written = None
        self._process.send("start", self._run, url, start, self._run_track, self._language[0])
        self._process.send("viewer", self._position())

    def _start_film(self) -> None:
        film = self._film()
        if film is None or not self._film_placed():
            self._session_key = ""  # the tick starts it once the film plays where it should
            self.changed.emit()
            return
        location, options = film
        start = self._position() + FILM_AHEAD_S
        self._run += 1
        self._run_channel = self._channel_key()
        self._run_start = start
        self._run_track = self._audio_track()
        self._resolve_language()  # the language of the track it reads, as it begins
        self._session_key = location
        self._newest = self._written = None
        self._process.send(
            "film", self._run, location, start, self._run_track, self._language[0], options
        )
        self._process.send("viewer", self._position())

    def _on_caption(self, run: int, start: float, end: float, text: str) -> None:
        if run != self._run or not self._enabled:
            return
        i = bisect.bisect_left(self._cues, (start, end, text))
        if i < len(self._cues) and self._cues[i][0] == start:
            self._cues[i] = (start, end, text)  # read again over a stretch: the newer words
        else:
            self._cues.insert(i, (start, end, text))

    def _on_progress(
        self, run: int, newest: float, rtf: float, written: Optional[float] = None
    ) -> None:
        if run != self._run:
            return
        self._newest, self._rtf = newest, rtf
        self._written = newest if written is None else written
        # Slow, and late for it: a short line ("Yes.") costs the model a fixed while, so a
        # dialogue's speed reads slow while the silences between its lines keep the captions
        # ahead of the picture (2.9: 1.06 while a film's were 3-60 s ahead, all in time).
        lead = self._lead()
        if rtf >= _SLOW_RTF and lead is not None and lead < LATE_LEAD_S:
            if self._slow_since is None:
                self._slow_since = time.monotonic()
        else:
            self._slow_since = None

    def _on_detected(self, run: int, language: str) -> None:
        """No language was given and the pieces agreed on one: kept for the channel while QiTV
        runs (the next run there starts with it), and the panel says it."""
        if run != self._run:
            return
        key = self._run_channel  # the run's channel, not the one a zap just tuned
        if key:
            self._detected[key] = language
        if self._language[1] == FROM_MODEL:
            self._language = (language, FROM_MODEL)
            self.changed.emit()

    def _on_resync(self, run: int, newest: float) -> None:
        """The stream's stamps jumped (a provider's restart): read again from the segment
        after the jump, not from the picture's (before it: the same jump again)."""
        if run == self._run and self._enabled:
            self._resync_at = newest
            self._restart.start()

    def _on_ended(self, run: int, reason: str) -> None:
        if run == self._run and self._enabled:
            self._restart.start()  # the reader fell behind or lost the stream: again, once

    # ---- every tick: the picture's place --------------------------------------------------
    def _tick(self) -> None:
        if not self._enabled:
            return
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
            self._cues.clear()  # another channel or a new session: its own captions
            self._last = None
            self._resolve_language()  # its own language too
            self._start_run()
            self.changed.emit()
            return
        if self._ready and self._track_changed():
            self._hear_track()
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
                if not self._covered(position):
                    self._start_run()
                return
        self._last = (position, now)
        self._process.send("viewer", position)
        self._show(self._cue_at(position))
        self._forget(position)

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
        self._detected.pop(self._channel_key(), None)
        self._cues.clear()
        self._last = None
        self._show("")
        self._start_run()  # in the language of the track it reads
        self.changed.emit()  # the panel's language row says the track's

    def _waiting(self) -> bool:
        """The live picture waits for the captions' distance behind live (LiveSession)."""
        if not self._is_live():
            return False
        live = self._live()
        waiting = getattr(live, "caption_waiting", None)
        return bool(waiting()) if callable(waiting) else False

    def _cue_at(self, position: float) -> str:
        i = bisect.bisect_right(self._cues, (position - FIRST_WORD_S, float("inf"), "")) - 1
        if i < 0:
            return ""
        start, end, text = self._cues[i]
        shows = start + FIRST_WORD_S
        following = self._cues[i + 1][0] + FIRST_WORD_S if i + 1 < len(self._cues) else None
        if following is not None and following - end <= BRIDGE_S:
            until = following  # a short pause: on until the next line
        else:
            until = max(end + HOLD_S, shows + MIN_SHOW_S, shows + len(text) / READ_CPS)
            if following is not None:
                until = min(until, following)
        return text if shows <= position < until else ""

    def _covered(self, position: float) -> bool:
        """The run reads on from before `position` and has not passed it by long: no need to
        read again."""
        newest = self._newest if self._newest is not None else self._run_start
        return self._run_start <= position <= newest + 1.0

    def _forget(self, position: float) -> None:
        while self._cues and self._cues[0][1] < position - KEEP_S:
            self._cues.pop(0)

    def _lead(self) -> Optional[float]:
        """How far ahead of the picture the captions are written (2.9, the review: not how far
        the sound is read, which a model too slow for it never shortened)."""
        if self._written is None:
            return None
        return float(self._written) - float(self._position())

    def add_overlay(self, overlay: Any) -> None:
        """Another place the captions show, as over the picture (2.9: the floating picture's,
        which had none: the captions showed on the fullscreen desktop left)."""
        self._overlays.append(overlay)
        if self._shown:
            overlay.show_text(self._shown)

    def _show(self, text: str) -> None:
        if text == self._shown:
            return
        self._shown = text
        from shiboken6 import isValid

        for overlay in self._overlays:
            if isinstance(overlay, QObject) and not isValid(overlay):
                continue  # the floating picture's, gone as QiTV closes
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
            live.set_caption_floor(LEAD_S.get(self._tier, 8.0))
            self._floored = live

    def _drop_floor(self, shift: bool = True) -> None:
        live, self._floored = self._floored, None
        if live is not None:
            live.set_caption_floor(0.0, shift=shift)

    def _target(self) -> Any:
        controller = getattr(self._window, "controller", None)
        return controller.now_playing() if controller is not None else None

    def _session(self) -> Any:
        """The large picture's player (the controller's: a picture-in-picture swap moves it),
        whose film the captions read."""
        controller = getattr(self._window, "controller", None)
        session = getattr(controller, "session", None)
        return session if session is not None else getattr(self._window, "session", None)

    def _channel_key(self) -> str:
        target = self._target()
        if target is None:
            return ""
        return f"{getattr(target, 'source', '')}|{getattr(target, 'title', '')}"

    def _audio_track(self) -> Optional[int]:
        session = self._session()
        try:
            track = int(session.audio_track()) if session is not None else -1
        except Exception:
            track = -1
        return track if track >= 0 else None

    def _resolve_language(self) -> None:
        """The viewer's pick for the channel, else the audio track's, else Settings', else
        the model's own detection."""
        from services.captions.languages import qwen_name

        pick = (self._config_get("ai_captions_picks", {}) or {}).get(self._channel_key())
        if pick:
            self._language = (pick, FROM_VIEWER)
            return
        heard = self._track_language()
        if heard:
            self._language = (heard, FROM_AUDIO_TRACK)
            return
        default = self._config_get("ai_captions_language", "auto")
        if default and default != "auto":
            self._language = (str(qwen_name(default) or default), FROM_DEFAULT)
            return
        self._language = (self._detected.get(self._channel_key(), ""), FROM_MODEL)

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
