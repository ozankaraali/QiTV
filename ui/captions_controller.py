"""AI captions (2.9): what the subtitle panel's "AI captions" row turns on, and how it shows.

The speech model runs in its own process (services.captions.process) and reads the live
channel from the time-shift helper's buffer, ahead of the picture. While captions are on the
picture is held `LEAD_S` behind the live edge (LiveSession.set_caption_floor: stay-live
builds it at 0.97x while it plays, and gives it back at 1.03x once they are off), so each
caption is written before the picture gets to its words and shows when they start. The
first time, the model is downloaded (services.captions.download) once the viewer says so;
the tier (0.6B or 1.7B) follows the computer's speed (services.captions.tier) or Settings.
Turned on from a phone remote, the question shows on the TV and on that phone: either answer
goes for both (`answer`, ui/remote_controller.py).

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

from services.i18n import translate

logger = logging.getLogger(__name__)

LEAD_S = {"small": 8.0, "large": 10.0}  # the picture's distance behind live while on
FILM_AHEAD_S = 6.0  # a film's sound is read from this far past the picture (after a seek too)
HOLD_S = 0.6  # a caption stays this long after its words, unless the next one starts
MIN_SHOW_S = 1.2
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
    size_text,
)


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
        self._process.detected.connect(self._on_detected)
        if card is not None:
            card.answered.connect(self._on_answer)
        self._enabled = False
        self._leaving_download: Any = None  # cancelled, still ending its request
        self._start_after_leaving = False
        self._run_channel = ""  # the channel the run reads (a vote's language is its)
        self._resync_at: Optional[float] = None  # read again from past a jump here
        self._supported: Optional[bool] = None
        self._asking = False
        self._question = ("", "", "")  # the download's question and its answers, as asked
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
        self._detected: dict[str, str] = {}  # channel: the language the model's pieces agreed on
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
        if target is None:
            return False
        if not getattr(target, "live", False) and self._film() is None:
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
        if self._source() is None:
            return translate(NOT_AVAILABLE)
        if self._slow_since is not None and time.monotonic() - self._slow_since >= _SLOW_FOR_S:
            return translate(CANT_KEEP_UP)
        lead = self._lead()
        wanted = LEAD_S.get(self._tier, 8.0) if self._is_live() else FILM_AHEAD_S
        if lead is not None and lead < wanted - 1.0:
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
            self._question = (
                translate(DOWNLOAD_QUESTION, size=size_text(missing)),
                translate(DOWNLOAD),
                translate(NOT_NOW),
            )
            self._card.ask(*self._question)
            return
        self._enable(tier)

    def downloading(self) -> bool:
        """The speech model downloads: they are on their way (the row's tap calls them off)."""
        return self._download is not None

    def asking(self) -> bool:
        """The download's question is up: on the TV, and on a phone that turned them on."""
        return self._asking

    def question(self) -> tuple[str, str, str]:
        """The download's question as asked, and its answers' words (yes, no)."""
        return self._question

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
        if download is not None and download is not self._leaving_download:
            download.cancel()
            self._leaving_download = download

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

        done()

    # ---- on and off --------------------------------------------------------------------
    def _on_answer(self, yes: bool) -> None:
        if not self._asking:
            return
        self._asking = False
        if not yes:
            self.changed.emit()  # the question is gone (a phone's closes too)
            return
        self._percent = 0
        self._unpacking = False
        leaving = self._leaving_download
        if leaving is not None and leaving.running():
            # A cancelled one still ends its current request: this one starts after it (two
            # would write the same file).
            self._download = leaving
            self._start_after_leaving = True
            self.changed.emit()
            return
        self._begin_download()

    def _begin_download(self) -> None:
        from services.captions.download import CaptionsDownload

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
        download.start(self._tier)
        self.changed.emit()

    def _on_download_progress(self, download: Any, done: Any, total: Any) -> None:
        if download is not self._download:
            return
        percent = int(100 * done / total) if total > 0 else 0
        if percent != self._percent:
            self._percent = percent
            self.changed.emit()

    def _on_download_preparing(self, download: Any) -> None:
        if download is not self._download:
            return
        self._percent = 100
        self._unpacking = True
        self.changed.emit()

    def _on_downloaded(self, download: Any) -> None:
        if self._left(download):
            return
        self._unpacking = False
        self._download = None
        self._enable(self._tier)

    def _on_download_failed(self, download: Any, reason: str) -> None:
        if self._left(download):
            return
        self._download = None
        self._unpacking = False
        from services.captions.download import CANCELLED

        if reason != CANCELLED:
            self._toast(reason, "error")  # already the whole sentence ("Download failed: …")
        self.changed.emit()

    def _left(self, download: Any) -> bool:
        """A cancelled download ended: nothing of it shows, and one asked for meanwhile
        starts now. True when `download` is not the current one."""
        if download is self._leaving_download:
            self._leaving_download = None
            if self._start_after_leaving:
                self._begin_download()
            return True
        return download is not self._download

    def _enable(self, tier: str) -> None:
        from services.captions import models

        self._tier = tier
        self._enabled = True
        self._cues.clear()
        self._resolve_language()
        live = self._live()
        if live is not None and self._is_live():
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
        self._cancel_download()
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
        if reason != "gone":
            self._process.stop()  # it did not load: no child left without a model
        if was:
            self._enabled = False
            self._timer.stop()
            live = self._live()
            if live is not None:
                live.set_caption_floor(0.0)
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
        self._session_key = url.rsplit("/side/", 1)[0]
        self._newest = None
        self._process.send("start", self._run, url, start, self._audio_track(), self._language[0])
        self._process.send("viewer", self._position())

    def _start_film(self) -> None:
        film = self._film()
        if film is None:
            self._session_key = ""
            self.changed.emit()
            return
        location, options = film
        start = self._position() + FILM_AHEAD_S
        self._run += 1
        self._run_channel = self._channel_key()
        self._run_start = start
        self._session_key = location
        self._newest = None
        self._process.send(
            "film", self._run, location, start, self._audio_track(), self._language[0], options
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

    def _on_progress(self, run: int, newest: float, rtf: float) -> None:
        if run != self._run:
            return
        self._newest, self._rtf = newest, rtf
        if rtf >= _SLOW_RTF:
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
        key = self._source()
        if key is None:
            self._show("")
            return
        if self._ready and key != self._session_key:
            self._cues.clear()  # another channel or a new session: its own captions
            self._last = None
            self._resolve_language()  # its own language too
            self._start_run()
            self.changed.emit()
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
        if self._newest is None:
            return None
        return float(self._newest) - float(self._position())

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

    # ---- live or a film ----------------------------------------------------------------------
    def _is_live(self) -> bool:
        target = self._target()
        return target is None or bool(getattr(target, "live", False))

    def _position(self) -> float:
        """Where the picture is: the time-shift timeline's second (live), the film's (a film)."""
        if self._is_live():
            live = self._live()
            return float(live.position()) if live is not None else 0.0
        session = getattr(self._window, "session", None)
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
        on this computer, or a film from Jellyfin or Emby. None for a provider's film: reading
        it again would be a second stream from the provider, which many allow none of."""
        target = self._target()
        session = getattr(self._window, "session", None)
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
        return None

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

        session = getattr(self._window, "session", None)
        if session is None or not hasattr(session, "audio_track_info"):
            return ""
        try:
            tracks = [t for t in session.audio_tracks() if t[0] != -1]
            info = session.audio_track_info()
            playing = self._audio_track()
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
