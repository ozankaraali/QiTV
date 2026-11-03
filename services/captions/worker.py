"""The captions process (2.9, AI captions): a live stream's sound in, captions on its timeline out.

Spawned like the time-shift helper (multiprocessing "spawn", a Pipe; services.captions.
process is the GUI's side). It loads the speech model once (`load`), then for each `start`
(a run) reads the session's stream from the helper's loopback server on the `side` route (the
helper's buffer, no second upstream connection), decodes its sound with QiTV's own libVLC
(services.captions.decoder), places it on the time-shift timeline (stream_clock), cuts it into
pieces at pauses (pieces) and has Qwen3-ASR (sherpa-onnx, the language locked when known)
write each one; what nobody said is dropped (guards). It runs at low priority: the picture
always comes first.

Messages to it: ("load", {...}), ("start", run, url, segment_start, audio_track, language),
("film", run, location, start_s, audio_track, language, vlc_options): a film's run,
("viewer", position), ("language", name), ("stop",), ("quit",).
From it: ("ready",), ("failed", reason), ("caption", run, start, end, text),
("progress", run, newest, rtf, written_to), ("resync", run, newest), ("ended", run, reason),
("detected", run, language) once the pieces agree on one (none was given),
("log", level, text); loaded without a model (`load` with "model": None, the packaged smoke
test's probe), ("piece", run, start, end, speech_share) for each piece instead. No traceback
or URL crosses: URLs carry the session token.
"""

from __future__ import annotations

from array import array
import logging
import multiprocessing
from multiprocessing.connection import Connection, wait
import os
import queue
import sys
import threading
import time
from typing import Any, Optional
import urllib.request

from services.captions import guards, text_language
from services.captions.pieces import END_PAUSE_S, MAX_PIECE_S, Cutter, Piece
from services.captions.stream_clock import StreamClock, TsTimes

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_MIN_SPEECH_SHARE = 0.3  # less of a piece was speech: music the detector caught, dropped
_MARGIN_S = 0.5
_PROGRESS_S = 1.0
_PASSED_S = 1.0  # a piece the picture passed by this much is not recognized (it never shows)
_READ_TIMEOUT_S = 15.0
_OWNER_GRACE_S = 0.5
_AHEAD_MAX_S = 60.0  # read no further ahead of the picture (far behind live: the ring holds it)


class _Link:
    """The Pipe, sent to from several threads."""

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._lock = threading.Lock()

    def send(self, message: tuple) -> None:
        with self._lock:
            try:
                self.conn.send(message)
            except OSError, ValueError, EOFError:
                pass  # the owner is gone; the watchdog ends this process


class _LogToOwner(logging.Handler):
    def __init__(self, link: _Link) -> None:
        super().__init__(logging.INFO)
        self._link = link

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._link.send(("log", record.levelno, record.getMessage()))
        except Exception:
            pass


class _Model:
    """The loaded speech model and voice detector; decodes one piece at a time."""

    def __init__(self, spec: dict) -> None:
        runtime = spec.get("runtime")
        if runtime and runtime not in sys.path:
            sys.path.insert(0, runtime)
        import sherpa_onnx  # the runtime the user downloaded, or a source run's own

        paths = spec.get("model")
        threads = int(spec.get("threads", 2))
        # Without a model (the packaged smoke test's probe): the sound, the clock and the
        # detector alone; each piece is reported instead of written.
        self.recognizer: Any = None
        if paths:
            self.recognizer = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
                conv_frontend=paths["conv_frontend"],
                encoder=paths["encoder"],
                decoder=paths["decoder"],
                tokenizer=paths["tokenizer"],
                num_threads=threads,
                max_new_tokens=guards.token_cap(MAX_PIECE_S),
            )
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = spec["vad"]
        config.silero_vad.threshold = float(spec.get("vad_threshold", 0.5))
        config.sample_rate = 16000
        config.num_threads = 1
        self.vad = sherpa_onnx.VadModel.create(config)
        self._lock = threading.Lock()
        if self.recognizer is not None:
            # Warm up: the first decode is slow (graph optimisation), not the viewer's first.
            self.decode(array("f", bytes(16000 * 4)), "")

    def is_speech(self, window: array) -> bool:
        with self._lock:
            return bool(self.vad.is_speech(window))

    def decode(self, samples: array, language: str) -> str:
        stream = self.recognizer.create_stream()
        if language:
            stream.set_option("language", language)
        stream.accept_waveform(16000, samples)
        self.recognizer.decode_stream(stream)
        return str(stream.result.text)


class _Run:
    """One reading of the stream, from one segment on, until it ends or is replaced."""

    def __init__(
        self,
        link: _Link,
        model: _Model,
        run: int,
        url: str,
        segment_start: float,
        audio_track: Optional[int],
        language: str,
        viewer: list,
    ) -> None:
        self.link = link
        self.model = model
        self.run = run
        self.url = url
        self.language = language
        self.viewer = viewer  # [position], shared with the command loop
        self.times = TsTimes(audio_pid=audio_track)
        self.clock = StreamClock(segment_start, self.times)
        self.cutter = Cutter(model.is_speech)
        self.pieces: queue.Queue[Optional[Piece]] = queue.Queue()
        self.stopped = threading.Event()
        self.newest = segment_start
        self.rtf = 0.5
        self._working: Optional[float] = None  # the start of the piece being recognized
        self.votes = text_language.Votes()  # the language heard, when none is given
        self._resynced = False
        self._ended = False
        from services.captions.decoder import AudioDecoder

        self.decoder = AudioDecoder(self._on_block, audio_track=audio_track)

    def start(self) -> None:
        self.decoder.start()
        threading.Thread(target=self._read, name="qitv-captions-read", daemon=True).start()
        threading.Thread(target=self._recognize, name="qitv-captions-asr", daemon=True).start()

    def stop(self) -> None:
        self.stopped.set()
        self.pieces.put(None)
        self.decoder.stop()

    # ---- reading (its thread) --------------------------------------------------------
    def _read(self) -> None:
        reason = "ended"
        try:
            request = urllib.request.Request(self.url)
            with urllib.request.urlopen(request, timeout=_READ_TIMEOUT_S) as response:
                while not self.stopped.is_set():
                    if self.newest - float(self.viewer[0]) > _AHEAD_MAX_S:
                        self.stopped.wait(0.5)  # far enough ahead: wait for the picture
                        continue
                    data = response.read1(_CHUNK) if hasattr(response, "read1") else b""
                    if not data:
                        break
                    self.times.feed(data)
                    if self.times.jumped:
                        self._resync()
                        return
                    self.decoder.feed(data)
        except Exception as error:
            reason = type(error).__name__  # no message: it could hold the URL
        if not self.stopped.is_set():
            self.decoder.end()
            self._end(reason)

    def _resync(self) -> None:
        if not self._resynced:
            self._resynced = True
            self.link.send(("resync", self.run, self.newest))
            self.stopped.set()

    def _end(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            self.link.send(("ended", self.run, reason))

    # ---- decoded sound (libVLC's thread) ---------------------------------------------
    def _on_block(self, vlc_us: int, pcm: bytes) -> None:
        if self.stopped.is_set():
            return
        samples = len(pcm) // 2
        at = self.clock.place(vlc_us, samples, 16000)
        if at is None:
            if self.clock.broken:
                self._resync()
            return
        ints = array("h")
        ints.frombytes(pcm[: samples * 2])
        if sys.byteorder == "big":
            ints.byteswap()
        self._take(at, array("f", [v / 32768.0 for v in ints]))

    def _take(self, at: float, floats: array) -> None:
        """Placed sound (16 kHz mono floats from timeline second `at`) into pieces."""
        if self.stopped.is_set():
            return
        self.newest = at + len(floats) / 16000
        self.cutter.set_max(self._longest())
        for piece in self.cutter.push(at, floats):
            self.pieces.put(piece)

    def _longest(self) -> float:
        """How long a piece may be and still be written before the picture gets to it."""
        lead = float(self.newest) - float(self.viewer[0])
        return (lead - END_PAUSE_S - _MARGIN_S) / (1.0 + float(self.rtf))

    # ---- recognizing (its thread) ----------------------------------------------------
    def _recognize(self) -> None:
        last_progress = 0.0
        while not self.stopped.is_set():
            try:
                piece = self.pieces.get(timeout=_PROGRESS_S)
            except queue.Empty:
                piece = None
            now = time.monotonic()
            if now - last_progress >= _PROGRESS_S:
                last_progress = now
                self.link.send(
                    ("progress", self.run, self.newest, round(self.rtf, 3), self.written_to())
                )
            if piece is None or self.stopped.is_set():
                continue
            if self.model.recognizer is None:
                self.link.send(("piece", self.run, piece.start, piece.end, piece.speech_share))
                continue
            if piece.speech_share < _MIN_SPEECH_SHARE:
                continue
            if piece.end + _PASSED_S < float(self.viewer[0]):
                continue  # the picture is past its words: never shown, the time goes to the next
            started = time.perf_counter()
            self._working = piece.start
            try:
                text = self.model.decode(piece.samples, self.language)
            except Exception as error:
                logger.warning("AI captions: a piece failed: %s", type(error).__name__)
                continue
            finally:
                self._working = None
            took = time.perf_counter() - started
            length = max(0.1, piece.end - piece.start)
            self.rtf = 0.8 * self.rtf + 0.2 * (took / length)
            text = guards.clean(text)
            if not text or self.stopped.is_set():
                continue
            if self.language and not text_language.written_in(text, self.language):
                continue  # in another language's letters: not what the channel says
            if not self.language and not self._heard(text, length):
                continue
            self.link.send(("caption", self.run, piece.start, piece.end, text))

    def written_to(self) -> float:
        """How far the captions are written: to the piece being recognized, else to the oldest
        one waiting, else all the sound read (2.9, the review: the lead was the reading's,
        which a model too slow for it never shortened: no "Can't keep up", no step down)."""
        working = self._working
        if working is not None:
            return working
        with self.pieces.mutex:
            waiting = next((p for p in self.pieces.queue if p is not None), None)
        return waiting.start if waiting is not None else float(self.newest)

    def _heard(self, text: str, length: float) -> bool:
        """No language given: the pieces vote with the language their words are in; it locks
        once they agree, and the model is told it from then on. Until then only a caption in
        the language most pieces say shows (a short piece the model heard as Chinese on a
        Turkish channel does not)."""
        heard = text_language.guess(text) if length >= 1.2 else None
        locked = self.votes.add(heard)
        if locked is not None:
            self.language = locked
            self.link.send(("detected", self.run, locked))
            return True
        return heard is not None and heard == self.votes.leading()


class _FilmRun(_Run):
    """A film's run (services.captions.film): its sound played alone from `start_s`, a little
    ahead of the picture, placed on the film's own clock (the main player's time); held while
    far enough ahead, ended with the film."""

    def __init__(
        self,
        link: _Link,
        model: _Model,
        run: int,
        location: str,
        start_s: float,
        audio_track: Optional[int],
        language: str,
        viewer: list,
        options: tuple[str, ...] = (),
    ) -> None:
        super().__init__(link, model, run, "", start_s, audio_track, language, viewer)
        from services.captions.film import FilmReader

        self.reader = FilmReader(
            location, start_s, self._take, audio_track=audio_track, options=options
        )

    def start(self) -> None:
        self.reader.start()
        threading.Thread(target=self._watch, name="qitv-captions-film", daemon=True).start()
        threading.Thread(target=self._recognize, name="qitv-captions-asr", daemon=True).start()

    def stop(self) -> None:
        self.stopped.set()
        self.pieces.put(None)
        self.reader.stop()

    def _watch(self) -> None:
        held = False
        while not self.stopped.wait(0.5):
            ahead = self.newest - float(self.viewer[0])
            if not held and ahead > _AHEAD_MAX_S:
                held = True
                self.reader.hold(True)
            elif held and ahead < _AHEAD_MAX_S / 2:
                held = False
                self.reader.hold(False)
            if self.reader.finished():
                self.pieces.put(None)
                self._end("ended")
                return


def _lower_priority() -> None:
    try:
        if hasattr(os, "nice"):
            os.nice(10)
        elif sys.platform.startswith("win"):
            import ctypes

            kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    except Exception:
        pass


def _exit_with_owner() -> None:
    parent = multiprocessing.parent_process()
    if parent is not None:
        wait([parent.sentinel])
        time.sleep(_OWNER_GRACE_S)
        os._exit(0)


def captions_main(conn: Connection, detailed_log: bool = False) -> None:
    """Spawn entry point. It never returns: the process ends with `os._exit`."""
    try:
        with open(os.devnull, "wb") as discard:  # libVLC and onnxruntime write to fd 1 and 2
            os.dup2(discard.fileno(), 1)
            os.dup2(discard.fileno(), 2)
        threading.Thread(target=_exit_with_owner, name="qitv-captions-owner", daemon=True).start()
        _lower_priority()
        from services.vlc_bundle import use_bundled_vlc

        use_bundled_vlc()  # before anything imports vlc, as main.py does
        link = _Link(conn)
        root = logging.getLogger()
        root.handlers[:] = [_LogToOwner(link)]
        root.setLevel(logging.DEBUG if detailed_log else logging.INFO)
        _serve(link)
    except BaseException:
        pass
    finally:
        os._exit(0)


def _serve(link: _Link) -> None:
    model: Optional[_Model] = None
    run: Optional[_Run] = None
    viewer = [0.0]
    language = ""
    while True:
        try:
            message: Any = link.conn.recv()
        except EOFError, OSError:
            break
        kind = message[0]
        if kind == "quit":
            break
        if kind == "load":
            try:
                model = _Model(message[1])
                link.send(("ready",))
            except Exception as error:
                logger.warning("AI captions: the model did not load: %s", error)
                link.send(("failed", type(error).__name__))
        elif kind == "start":
            _, number, url, segment_start, audio_track, language = message
            if run is not None:
                run.stop()
                run = None
            if model is None:
                link.send(("ended", number, "no model"))
                continue
            viewer[0] = float(segment_start)
            run = _Run(link, model, number, url, segment_start, audio_track, language, viewer)
            run.start()
        elif kind == "film":
            _, number, location, start_s, audio_track, language, options = message
            if run is not None:
                run.stop()
                run = None
            if model is None:
                link.send(("ended", number, "no model"))
                continue
            viewer[0] = float(start_s)
            run = _FilmRun(
                link,
                model,
                number,
                location,
                start_s,
                audio_track,
                language,
                viewer,
                tuple(options),
            )
            run.start()
        elif kind == "viewer":
            viewer[0] = float(message[1])
        elif kind == "language":
            language = message[1]
            if run is not None:
                run.language = language
        elif kind == "stop":
            if run is not None:
                run.stop()
                run = None
    if run is not None:
        run.stop()
