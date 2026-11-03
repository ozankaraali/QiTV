"""AI captions (2.9): what the subtitle panel's "AI captions" row turns on, and how it shows.

The speech model runs in its own process (services.captions.process) and reads the live
channel from the time-shift helper's buffer, ahead of the picture. While captions are on the
picture is held `LEAD_S` behind the live edge (LiveSession.set_caption_floor: stay-live
builds it at 0.97x while it plays, and gives it back at 1.03x once they are off), so each
caption is written before the picture gets to its words and shows when they start. The
first time, the model is downloaded (services.captions.download) once the viewer says so;
the tier (0.6B or 1.7B) follows the computer's speed (services.captions.tier) or Settings.

Cues are kept on the time-shift timeline: going back over a stretch shows its captions
again, and a seek reads again from where the picture went. A caption that comes after its
words started shows for what is left of it (the picture's own pace decides); one that comes
after they ended never shows (owner: captions in the next scene are worse than none).
"""

from __future__ import annotations

import bisect
import logging
import os
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.i18n import N_, translate

logger = logging.getLogger(__name__)

LEAD_S = {"small": 8.0, "large": 10.0}  # the picture's distance behind live while on
HOLD_S = 0.6  # a caption stays this long after its words, unless the next one starts
MIN_SHOW_S = 1.2
TICK_MS = 250
SEEK_S = 2.0  # the position moved this much more than the clock: a seek, read again there
KEEP_S = 30 * 60.0  # cues kept this far behind the picture
_RESTART_MS = 1000
_SLOW_RTF = 0.9  # the model takes this long per second of speech: it can't keep up
_SLOW_FOR_S = 30.0

DOWNLOADING = N_("Downloading {percent}%")
PREPARING = N_("Preparing…")
CATCHING_UP = N_("Catching up…")
CANT_KEEP_UP = N_("Can't keep up")
NOT_HERE = N_("Not available for this stream")
COULD_NOT_START = N_("Couldn't start AI captions")
QUESTION = N_(
    "AI captions need a speech model on this computer: {size} to download once. " "Download it now?"
)
YES = N_("Download")
NO = N_("Not now")
DOWNLOAD_FAILED = N_("The AI captions download failed: {reason}")
FROM_AUDIO = N_("audio track")
FROM_YOU = N_("you chose")
FROM_DEFAULT = N_("default")
FROM_MODEL = N_("automatic")


def _size_text(size: int) -> str:
    if size >= 1_000_000_000:
        return f"{size / 1e9:.1f} GB"
    return f"{max(1, round(size / 1e6))} MB"


class CaptionsController(QObject):
    """The subtitle panel's AI captions, for the large picture's live channel."""

    changed = Signal()  # the row's state or text changed: the panel shows it again

    def __init__(self, window: Any, card: Any = None, overlay: Any = None) -> None:
        super().__init__(window)
        from services.captions.process import CaptionsProcess

        self._window = window
        self._card = card  # a QuestionCard of its own: download the model?
        self._overlay = overlay  # the captions overlay (ui.overlays.captions)
        self._process = CaptionsProcess(self)
        self._process.ready.connect(self._on_ready)
        self._process.failed.connect(self._on_failed)
        self._process.caption.connect(self._on_caption)
        self._process.progress.connect(self._on_progress)
        self._process.resync.connect(self._on_resync)
        self._process.ended.connect(self._on_ended)
        if card is not None:
            card.answered.connect(self._on_answer)
        self._enabled = False
        self._supported: Optional[bool] = None
        self._asking = False
        self._download: Any = None
        self._percent = 0
        self._unpacking = False
        self._loading = False
        self._ready = False
        self._tier = ""
        self._run = 0
        self._session_key = ""  # the stream base the run reads (another session: read again)
        self._run_start = 0.0  # the timeline second the run reads from
        self._cues: list[tuple[float, float, str]] = []  # (start, end, text) by start
        self._newest: Optional[float] = None
        self._rtf = 0.0
        self._slow_since: Optional[float] = None
        self._last: Optional[tuple[float, float]] = None  # (position, monotonic) last tick
        self._shown = ""
        self._language: tuple[str, str] = ("", FROM_MODEL)  # (qwen name or "", source)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._restart = QTimer(self)
        self._restart.setSingleShot(True)
        self._restart.setInterval(_RESTART_MS)
        self._restart.timeout.connect(self._start_run)

    # ---- the panel's questions ---------------------------------------------------------
    def available(self) -> bool:
        """A live channel plays, and this computer can run the model (a runtime is pinned)."""
        target = self._target()
        if target is None or not getattr(target, "live", False):
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
        """The speed benchmark (~0.2 s of CPU) off the GUI thread, before the row is pressed."""
        import threading

        from services.captions import tier

        threading.Thread(target=tier.cpu_score, name="qitv-captions-tier", daemon=True).start()

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
        if self._live() is None or self._live().caption_source() is None:
            return translate(NOT_HERE)
        if self._slow_since is not None and time.monotonic() - self._slow_since >= _SLOW_FOR_S:
            return translate(CANT_KEEP_UP)
        lead = self._lead()
        if lead is not None and lead < LEAD_S.get(self._tier, 8.0) - 1.0:
            return translate(CATCHING_UP)
        return ""

    def toggle(self) -> None:
        if self._enabled or self._download is not None:
            self._disable()
            return
        from services.captions import models

        tier = self._choose_tier()
        missing = models.download_size(tier)
        if missing > 0:
            if self._card is None:
                return
            self._asking = True
            self._tier = tier
            self._card.ask(
                translate(QUESTION, size=_size_text(missing)), translate(YES), translate(NO)
            )
            return
        self._enable(tier)

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

    def shutdown(self) -> None:
        """QiTV closes: no model left running, no download going on."""
        self._timer.stop()
        self._restart.stop()
        if self._download is not None:
            self._download.cancel()
            self._download = None
        self._process.stop()
        self._process.wait(3000)

    # ---- on and off --------------------------------------------------------------------
    def _on_answer(self, yes: bool) -> None:
        if not self._asking:
            return
        self._asking = False
        if not yes:
            return
        from services.captions.download import CaptionsDownload

        download = CaptionsDownload(self)
        download.progress.connect(self._on_download_progress)
        download.preparing.connect(self._on_download_preparing)
        download.finished.connect(self._on_downloaded)
        download.failed.connect(self._on_download_failed)
        self._download = download
        self._percent = 0
        self._unpacking = False
        download.start(self._tier)
        self.changed.emit()

    def _on_download_progress(self, done: Any, total: Any) -> None:
        percent = int(100 * done / total) if total > 0 else 0
        if percent != self._percent:
            self._percent = percent
            self.changed.emit()

    def _on_download_preparing(self) -> None:
        self._percent = 100
        self._unpacking = True
        self.changed.emit()

    def _on_downloaded(self) -> None:
        self._unpacking = False
        self._download = None
        self._enable(self._tier)

    def _on_download_failed(self, reason: str) -> None:
        self._download = None
        self._unpacking = False
        self._toast(translate(DOWNLOAD_FAILED, reason=reason), "error")
        self.changed.emit()

    def _enable(self, tier: str) -> None:
        from services.captions import models

        self._tier = tier
        self._enabled = True
        self._cues.clear()
        self._resolve_language()
        live = self._live()
        if live is not None:
            live.set_caption_floor(LEAD_S.get(tier, 8.0))
        if not self._process.running():
            paths = models.model_paths(tier)
            runtime = models.runtime_dir()
            threads = max(2, min(6, (os.cpu_count() or 4) // 2))
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
                    "threads": threads,
                },
                detailed_log=bool(self._config_get("detailed_log", False)),
            )
        elif self._ready:
            self._start_run()
        self._timer.start()
        self.changed.emit()

    def _disable(self) -> None:
        if self._download is not None:
            self._download.cancel()
            self._download = None
        self._enabled = False
        self._timer.stop()
        self._restart.stop()
        self._process.stop()  # the model's memory back (a GB or two)
        self._loading = self._ready = False
        self._session_key = ""
        self._newest = None
        self._slow_since = None
        live = self._live()
        if live is not None:
            live.set_caption_floor(0.0)
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
        if was:
            self._enabled = False
            self._timer.stop()
            live = self._live()
            if live is not None:
                live.set_caption_floor(0.0)
            self._show("")
            self._toast(translate(COULD_NOT_START), "error")
        self.changed.emit()

    def _start_run(self) -> None:
        if not self._enabled or not self._ready:
            return
        live = self._live()
        source = live.caption_source() if live is not None else None
        if source is None:
            self._session_key = ""
            self.changed.emit()
            return
        url, _seq, start = source
        self._run += 1
        self._run_start = start
        self._session_key = url.rsplit("/side/", 1)[0]
        self._newest = None
        self._process.send("start", self._run, url, start, self._audio_track(), self._language[0])
        self._process.send("viewer", live.position())

    def _on_caption(self, run: int, start: float, end: float, text: str) -> None:
        if run != self._run or not self._enabled:
            return
        i = bisect.bisect_left(self._cues, (start, end, text))
        if i < len(self._cues) and self._cues[i][0] == start:
            self._cues[i] = (start, end, text)  # read again over a stretch: the newer words
        else:
            self._cues.insert(i, (start, end, text))

    def _on_progress(self, run: int, newest: float, rtf: float) -> None:
        if run != self._run:
            return
        self._newest, self._rtf = newest, rtf
        if rtf >= _SLOW_RTF:
            if self._slow_since is None:
                self._slow_since = time.monotonic()
        else:
            self._slow_since = None

    def _on_resync(self, run: int, newest: float) -> None:
        if run == self._run and self._enabled:
            self._restart.start()

    def _on_ended(self, run: int, reason: str) -> None:
        if run == self._run and self._enabled:
            self._session_key = ""
            self._restart.start()  # the session ended or the reader fell behind: again

    # ---- every tick: the picture's place --------------------------------------------------
    def _tick(self) -> None:
        live = self._live()
        if live is None or not self._enabled:
            return
        source = live.caption_source()
        if source is None:
            self._show("")
            return
        if self._ready and source[0].rsplit("/side/", 1)[0] != self._session_key:
            self._cues.clear()  # another channel or a new session: its own captions
            self._last = None
            self._start_run()
            return
        position = live.position()
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

    def _cue_at(self, position: float) -> str:
        i = bisect.bisect_right(self._cues, (position, float("inf"), "")) - 1
        if i < 0:
            return ""
        start, end, text = self._cues[i]
        following = self._cues[i + 1][0] if i + 1 < len(self._cues) else float("inf")
        until = min(max(end + HOLD_S, start + MIN_SHOW_S), following)
        return text if start <= position < until else ""

    def _covered(self, position: float) -> bool:
        """The run reads on from before `position` and has not passed it by long: no need to
        read again."""
        newest = self._newest if self._newest is not None else self._run_start
        return self._run_start <= position <= newest + 1.0

    def _forget(self, position: float) -> None:
        while self._cues and self._cues[0][1] < position - KEEP_S:
            self._cues.pop(0)

    def _lead(self) -> Optional[float]:
        live = self._live()
        if live is None or self._newest is None:
            return None
        return float(self._newest) - float(live.position())

    def _show(self, text: str) -> None:
        if text == self._shown:
            return
        self._shown = text
        overlay = self._overlay
        if overlay is None:
            return
        if text:
            overlay.show_text(text)
        else:
            overlay.clear()

    # ---- what it reads from the window ------------------------------------------------------
    def _live(self) -> Any:
        return getattr(self._window, "live_session", None)

    def _target(self) -> Any:
        controller = getattr(self._window, "controller", None)
        return controller.now_playing() if controller is not None else None

    def _channel_key(self) -> str:
        target = self._target()
        if target is None:
            return ""
        return f"{getattr(target, 'source', '')}|{getattr(target, 'title', '')}"

    def _audio_track(self) -> Optional[int]:
        session = getattr(self._window, "session", None)
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
            self._language = (pick, FROM_YOU)
            return
        heard = self._track_language()
        if heard:
            self._language = (heard, FROM_AUDIO)
            return
        default = self._config_get("ai_captions_language", "auto")
        if default and default != "auto":
            self._language = (str(qwen_name(default) or default), FROM_DEFAULT)
            return
        self._language = ("", FROM_MODEL)

    def _track_language(self) -> str:
        from services.captions.languages import qwen_name

        session = getattr(self._window, "session", None)
        info = getattr(session, "audio_track_info", None)
        if info is None:
            return ""
        try:
            playing = self._audio_track()
            for track in info():
                if playing is None or track.id == playing:
                    return str(qwen_name(track.language_code) or "") if track.language_code else ""
        except Exception as error:
            logger.debug("AI captions: no track language: %s", error)
        return ""

    def _choose_tier(self) -> str:
        from services.captions import models, tier

        override = str(self._config_get("ai_captions_model", "auto") or "auto")
        if override in ("small", "large"):
            return override
        for name in ("large", "small"):  # one already here is the one to use
            if models.download_size(name) == 0:
                return name
        return str(tier.choose_tier("auto"))

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
