"""The captions process (2.9, AI captions): a live stream's sound in, captions on its timeline out.

Spawned like the time-shift helper (multiprocessing "spawn", a Pipe; services.captions.
process is the GUI's side). It loads the speech model once (`load`), then for each `start`
(a run) reads the session's stream from the helper's loopback server on the `side` route (the
helper's buffer, no second upstream connection), decodes its sound with QiTV's own libVLC
(services.captions.decoder), places it on the time-shift timeline (stream_clock), cuts it into
pieces at pauses (pieces) and has Qwen3-ASR (sherpa-onnx, the language locked when known)
write each one; what nobody said is dropped (guards). It runs at low priority: the picture
always comes first.

Messages to it: ("load", {...}), ("start", run, url, segment_start, audio_track, language,
guessed, read_on),
("film", run, location, start_s, audio_track, language, vlc_options): a film's run,
("viewer", position), ("language", name), ("stop",), ("quit",).
From it: ("ready",), ("failed", reason), ("caption", run, start, end, text[, parts]) (parts, a
line of more than one: [(text, start, end), ...], services.captions.parts),
("progress", run, newest, rtf, written_to, qwen_writes), ("resync", run, newest),
("ended", run, reason),
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
from pathlib import Path
import queue
import re
import sys
import threading
import time
from typing import Any, Optional, Sequence
import urllib.request

from services.captions import guards, numbers_tr, text_language
from services.captions.growing import GrowingWriter
from services.captions.languages import COHERE, EUROPEAN, QWEN_LANGUAGES, iso_code
from services.captions.parts import parts_of
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


TURKISH = "Turkish"  # Qwen's name for it: the language ufakzeka-kulak writes (2.9.1)
_COHERE, _EUROPEAN = "cohere", "european"  # 2.9.3: the specialists (their spec keys)
# ... and the others' parts, by which the log names the model that wrote a line (2026-11-08)
_QWEN, _TURKISH_ROLE = "qwen", "turkish"
_NAMES = {  # ... when its folder says nothing
    _QWEN: "Qwen3-ASR",
    _TURKISH_ROLE: "ufakzeka-kulak",
    _COHERE: "Cohere Transcribe",
    _EUROPEAN: "Parakeet",
}
_LOAD_WAIT_S = 60.0  # a language only a specialist writes waits this long for it to load
_TURKISH_GAIN = 8.0  # the model card's input gain: its sherpa-onnx files want the sound louder
# 2.9.5: the voice detector again, without its hold: it says where the pauses between words
# and phrases are (pieces.Cutter's `is_voiced`)
_VOICED_MIN_S = 0.05
_UPPER = {"i": "İ", "ı": "I"}  # Turkish's own capitals of the dotted and the dotless i
_MOON_FRAME = 80  # sherpa-onnx's Moonshine reads 5 ms frames: a piece is padded to whole ones
_IDLE_S = 0.25  # the growing writer looks at the picture this often when no sound comes
# 2026-11-09, the owner: buffered channels watched at 1.5x or 2x, so 2 s of sound to read a second;
# from this rate on, our Moonshine beam reads its best path alone (about 4x cheaper)
FAST_RATE = 1.25
LINE_LEVEL = logging.DEBUG  # each caption line is logged at this level (2026-11-08)


def _folder_name(folder: Any) -> str:
    """A model's folder as the log names the model ("qwen3-asr-0.6b"); "" without one."""
    return Path(str(folder)).name if folder else ""


_SENTENCE_ENDS = (".", "!", "?", "…")
SENTENCE_GAP_S = 1.0  # a pause this long before a line: it starts a sentence


# (an "i" after an apostrophe is a suffix: "Deniz'i gördüm" stays as it is, the owner 2026-11-08)
_LONE_I = re.compile(r"(?<![\w'’])i (?=\w)")


def lone_i_joined(text: str) -> str:
    """ufak's words that start with a capital İ ("İstanbul", "İşte") come as a lone "i" and
    the rest ("i stanbul": its training text lower-cased İ to i and a combining dot, which
    its tokenizer took for a word's start); a lone "i" is no Turkish word, so it joins the
    word after it (2.9.6, the real-use review: "i stanbul" three times on a news channel).
    ufak's alone: our own models write İ (2026-11-08, the owner: "model specific")."""
    return _LONE_I.sub("i", text)


def unfinished(text: str) -> str:
    """A piece cut mid-speech without the full stop a model ends every piece with (2.9.4, the
    owner on Cohere Transcribe: "puts dots ... at the end of all sub frames even though it is
    just the split"); a question, an exclamation, an ellipsis stay."""
    stripped = text.rstrip()
    if stripped.endswith(("...", "…")) or not stripped.endswith((".", "。", "।")):
        return text
    return stripped[:-1].rstrip()


def turkish_capital(text: str) -> str:
    """`text` with its first letter a capital, as Turkish writes it (i: İ)."""
    for i, char in enumerate(text):
        if char.isalpha():
            upper = _UPPER.get(char, char.upper())
            return text[:i] + upper + text[i + 1 :]
    return text


class _Model:
    """The loaded speech models and voice detector; decodes one piece at a time: Qwen3-ASR, and
    for Turkish ufak AI's ufakzeka-kulak (2.9.1: far better Turkish than Qwen's, at a
    twenty-fifth of its time) once the files are given."""

    moonshine = False  # our Turkish Moonshine writes Turkish (2.10)
    beam: Any = None  # ... read with our beam (moon_beam.BeamReader)
    # 2026-11-08, the owner: "Write to logs which one writes": each model's name in the log,
    # by its part (_QWEN, _TURKISH_ROLE, _COHERE, _EUROPEAN)
    names: dict[str, str] = {}
    _wrote = threading.local()  # the name of the model that wrote this thread's last decode

    def __init__(self, spec: dict) -> None:
        runtime = spec.get("runtime")
        if runtime and runtime not in sys.path:
            sys.path.insert(0, runtime)
        import sherpa_onnx  # the runtime the user downloaded, or a source run's own

        paths = spec.get("model")
        threads = int(spec.get("threads", 2))
        self._sherpa = sherpa_onnx
        self._threads = threads
        # Without a model (the packaged smoke test's probe): the sound, the clock and the
        # detector alone; each piece is reported instead of written.
        self.has_model = bool(paths)
        self._qwen_paths = paths
        turkish = spec.get("turkish")
        self.names = {_QWEN: _folder_name(Path(paths["encoder"]).parent if paths else None)}
        self.recognizer: Any = self._new_qwen() if paths else None
        self.turkish: Any = None
        self.moonshine = False
        self.beam = None
        if paths and turkish:
            # 2.10: QiTV's own Turkish Moonshine medium (models.TURKISH_FILES), read with our
            # 4-path beam alone (no n-gram), as its model card's beam numbers were read
            from services.captions.moon_beam import BeamReader

            folder = Path(turkish)
            self.names[_TURKISH_ROLE] = _folder_name(folder)
            self.beam = self.turkish = BeamReader(folder, None, threads=min(2, threads))
            self.moonshine = True
        # 2.9.3: the specialists, each loaded once a run's language needs it (in its own thread;
        # Qwen writes meanwhile): Cohere Transcribe for its 14 languages, told the language,
        # and NVIDIA's Parakeet TDT 0.6B v3 for the other European ones. Qwen still tells the
        # language, and writes the rest. Both at once would hold some 4 GB.
        self._folders = {
            name: Path(spec[name]) for name in (_COHERE, _EUROPEAN) if paths and spec.get(name)
        }
        self.names.update({name: _folder_name(folder) for name, folder in self._folders.items()})
        self.cohere: Any = None
        self.european: Any = None
        self._loading: dict[str, threading.Event] = {}

        def detector(shortest: Optional[float] = None) -> Any:
            config = sherpa_onnx.VadModelConfig()
            config.silero_vad.model = spec["vad"]
            config.silero_vad.threshold = float(spec.get("vad_threshold", 0.5))
            if shortest is not None:
                config.silero_vad.min_silence_duration = shortest
                config.silero_vad.min_speech_duration = shortest
            config.sample_rate = 16000
            config.num_threads = 1
            return sherpa_onnx.VadModel.create(config)

        self.vad = detector()
        self.voiced = detector(_VOICED_MIN_S)
        self._lock = threading.Lock()
        if self.has_model:
            # Warm up: the first decode is slow (graph optimisation), not the viewer's first.
            if self.recognizer is not None:
                self.decode(array("f", bytes(16000 * 4)), "")
            if self.beam is not None:
                self.beam.warm()  # (silence is skipped: a quiet tone)
            elif self.turkish is not None:
                self.decode(array("f", bytes(16000 * 4)), TURKISH)

    def name(self, part: str) -> str:
        """The model of a part (_QWEN, _TURKISH_ROLE, ...) as the log names it."""
        return self.names.get(part) or _NAMES.get(part, part)

    def wrote(self) -> str:
        """The model that wrote this thread's last decode, as the log names it."""
        return str(getattr(self._wrote, "name", "") or "")

    def writes(self, language: str) -> str:
        """The model that writes `language` now, as the log names it: for Turkish the Turkish
        one, else its specialist once loaded, else Qwen."""
        if language == TURKISH and getattr(self, "turkish", None) is not None:
            return self.name(_TURKISH_ROLE)
        special = self.writer(language) if language else None
        if special is not None and getattr(self, special, None) is not None:
            return self.name(special)
        return self.name(_QWEN)

    def _new_qwen(self) -> Any:
        paths = self._qwen_paths
        assert paths
        return self._sherpa.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=paths["conv_frontend"],
            encoder=paths["encoder"],
            decoder=paths["decoder"],
            tokenizer=paths["tokenizer"],
            num_threads=self._threads,
            max_new_tokens=guards.token_cap(MAX_PIECE_S, "Hindi"),  # each piece: its own
        )

    def has_qwen(self) -> bool:
        """Qwen is here."""
        return self.recognizer is not None

    def is_speech(self, window: array) -> bool:
        with self._lock:
            return bool(self.vad.is_speech(window))

    def is_voiced(self, window: array) -> bool:
        """`is_speech` without the detector's hold: the pauses between words show."""
        with self._lock:
            return bool(self.voiced.is_speech(window))

    def writer(self, language: str) -> Optional[str]:
        """The specialist that writes `language` (_COHERE, _EUROPEAN), None for Qwen's."""
        if language in COHERE and _COHERE in self._folders:
            return _COHERE
        if language in EUROPEAN and _EUROPEAN in self._folders:
            return _EUROPEAN
        return None

    def prepare(self, language: str) -> None:
        """The specialist for `language` loads now, in its own thread, unless it has."""
        name = self.writer(language)
        if name is None or name in self._loading:
            return
        done = self._loading[name] = threading.Event()
        threading.Thread(
            target=self._load, args=(name, done), name=f"qitv-captions-{name}", daemon=True
        ).start()

    def _load(self, name: str, done: threading.Event) -> None:
        folder, sherpa_onnx = self._folders[name], self._sherpa
        try:
            if name == _COHERE:
                recognizer = sherpa_onnx.OfflineRecognizer.from_cohere_transcribe(
                    encoder=str(folder / "encoder.int8.onnx"),
                    decoder=str(folder / "decoder.int8.onnx"),
                    tokens=str(folder / "tokens.txt"),
                    num_threads=self._threads,
                    language="en",
                    use_punct=True,
                )
            else:
                recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(folder / "encoder.int8.onnx"),
                    decoder=str(folder / "decoder.int8.onnx"),
                    joiner=str(folder / "joiner.int8.onnx"),
                    tokens=str(folder / "tokens.txt"),
                    num_threads=self._threads,
                    model_type="nemo_transducer",
                )
            stream = recognizer.create_stream()  # warm up, as the others at load
            stream.accept_waveform(16000, array("f", bytes(16000 * 4)))
            recognizer.decode_stream(stream)
            setattr(self, name, recognizer)
            logger.info("AI captions: %s is loaded", name)
        except Exception as error:  # Qwen goes on writing its languages
            logger.warning("AI captions: %s did not load: %s", name, type(error).__name__)
        finally:
            done.set()

    def _specialist(self, language: str) -> Any:
        """The loaded specialist for `language`, else None (Qwen's piece): it starts loading;
        one Qwen can't write at all (only Parakeet knows it) waits for it."""
        name = self.writer(language)
        if name is None:
            return None
        loaded = getattr(self, name)
        if loaded is not None:
            return loaded
        self.prepare(language)
        if language not in QWEN_LANGUAGES or not self.has_qwen():
            self._loading[name].wait(_LOAD_WAIT_S)
        return getattr(self, name)

    def decode(self, samples: array, language: str, fast: bool = False) -> str:
        """`samples` read by the model that writes `language` (`wrote` names it after); `fast`
        (the picture plays faster than FAST_RATE): our beam narrowed to its best path alone."""
        if language == TURKISH and self.beam is not None:
            self._wrote.name = self.name(_TURKISH_ROLE)
            width = 1 if fast else None
            return str(self.beam.transcribe(samples, width=width)).strip()  # as sherpa-onnx's
        if language == TURKISH and self.turkish is not None:
            self._wrote.name = self.name(_TURKISH_ROLE)
            stream = self.turkish.create_stream()
            if self.moonshine:
                sound = array("f", samples)
                sound.extend(array("f", bytes(4 * (-len(sound) % _MOON_FRAME))))
            else:
                sound = array("f", (v * _TURKISH_GAIN for v in samples))
            stream.accept_waveform(16000, sound)
            self.turkish.decode_stream(stream)
            text = str(stream.result.text).strip()  # its case: sentence_case
            return text if self.moonshine else lone_i_joined(text)  # ufak's "i stanbul"
        specialist = self._specialist(language) if language else None
        if specialist is None and language and language not in QWEN_LANGUAGES:
            return ""  # 2.9.6: only its own model writes it, and that isn't here (not now)
        if specialist is not None and specialist is self.cohere:
            self._wrote.name = self.name(_COHERE)
            stream = specialist.create_stream()
            stream.set_option("language", iso_code(language))
            stream.accept_waveform(16000, samples)
            specialist.decode_stream(stream)
            return str(stream.result.text).strip()
        if specialist is not None:
            self._wrote.name = self.name(_EUROPEAN)
            stream = specialist.create_stream()
            stream.accept_waveform(16000, samples)
            specialist.decode_stream(stream)
            text = str(stream.result.text).strip()  # its own capitals and full stops
            # 2.9.3: Parakeet can't be told the language. On a French broadcast with English
            # words in it, it wrote English ("And I've been at the bank", "We're deposing the
            # same sum"): such a piece is Qwen's, told the language (Qwen knows it).
            drifted = language != "English" and text_language.guess(text) == "English"
            if not drifted or not self.has_qwen() or language not in QWEN_LANGUAGES:
                return text
        recognizer = self.recognizer
        if recognizer is None:
            return ""  # Qwen did not load: no caption for this piece
        self._wrote.name = self.name(_QWEN)
        stream = recognizer.create_stream()
        if language in QWEN_LANGUAGES:  # not one only Parakeet knows (its files not given)
            stream.set_option("language", language)
        # 2.9.3: as many tokens as the piece's length and its language's script take
        cap = guards.token_cap(len(samples) / 16000, language or None)
        stream.set_option("max_new_tokens", str(cap))
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
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
        guessed: bool = False,
        read_on: bool = False,
    ) -> None:
        self.link = link
        self.model = model
        self.run = run
        self.url = url
        # 2026-11-09, the owner's channel: its clock leapt and its sound was anchored again many
        # times a second; each run read again from past the step, met the next step at once, and
        # the reader started over 25 times from the same second. A run started again past a
        # step reads on past the next ones, counting its sound from where it got to
        self.read_on = read_on
        self._steps = 0
        self.language = language
        # 2026-11-07: a language guessed from the channel's title prefix ("TR: …"): the run
        # starts in it (no Qwen vote first), and its readings may still say otherwise
        # (`_check_guess`)
        self.guessed = guessed and bool(language)
        self.checks = text_language.Votes()
        if language:
            model.prepare(language)  # 2.9.3: its specialist loads while the sound comes
        self.viewer = viewer  # [position], shared with the command loop
        self.times = TsTimes(audio_pid=audio_track)
        self.clock = StreamClock(segment_start, self.times)
        self.cutter = Cutter(model.is_speech, getattr(model, "is_voiced", None))
        self.pieces: queue.Queue[Optional[Piece]] = queue.Queue()
        self.stopped = threading.Event()
        self.newest = segment_start
        self.rtf = 0.5
        self._working: Optional[float] = None  # the start of the piece being recognized
        self._said: Optional[tuple[str, float]] = None  # the line written last, and its end
        self._writes = ""  # the model that wrote the run's last line, as the log names it
        self.votes = text_language.Votes()  # the language heard, when none is given
        # Turkish: the sound goes to the growing writer whole (no pieces), lines come back
        self.blocks: queue.Queue[Optional[tuple[float, array]]] = queue.Queue()
        self.writer: Optional[GrowingWriter] = None
        self._resynced = False
        self._ended = False
        from services.captions.decoder import AudioDecoder

        self.decoder = AudioDecoder(self._on_block, audio_track=audio_track)

    def start(self) -> None:
        self._announce()
        self.decoder.start()
        threading.Thread(target=self._read, name="qitv-captions-read", daemon=True).start()
        threading.Thread(target=self._recognize, name="qitv-captions-asr", daemon=True).start()
        self._maybe_stream()

    def _announce(self) -> None:
        """The run and the model that writes it in the log: a line left over from the model
        before (its process still ending) is told apart (2026-11-08, the owner)."""
        self._writes = self._writer_name(self.language)
        logger.info(
            "AI captions: run %d reads from %.1f s: [%s] %s",
            self.run,
            float(self.viewer[0]),
            self._writes,
            f"writes {self.language}" if self.language else "tells the language",
        )

    def _writer_name(self, language: str) -> str:
        """The model that writes `language` in this run, as the log names it."""
        writes = getattr(getattr(self, "model", None), "writes", None)
        return str(writes(language)) if callable(writes) else "?"

    def _logged(self, name: str, heard: str, shown: str) -> None:
        """A line in the log by the model that wrote it (2026-11-08, the owner: "Write to logs
        which one writes"), what it heard and what shows; the run's model changing (a language
        locked, a specialist loaded) is said once, with the run. At LINE_LEVEL (DEBUG: Settings'
        detailed log shows them)."""
        level = LINE_LEVEL
        if name != getattr(self, "_writes", ""):
            self._writes = name
            logger.log(level, "AI captions: run %d: [%s] writes now", getattr(self, "run", 0), name)
        logger.log(level, "AI captions: [%s] heard %r, shows %r", name, heard, shown)

    def _write(
        self,
        name: str,
        heard: str,
        shown: str,
        start: float,
        end: float,
        until: float = 0.0,
        voiced: Optional[Sequence[bool]] = None,
    ) -> None:
        """A line of the sound of [start, end) out (shown until `until` when later), and in the
        log. 2026-11-08, the owner: a line of more than one sentence goes in parts (parts.parts_of:
        each with its sound's span, from `voiced`, the voice detector's windows from `start`); the
        captions show a long line a part at a time as each is said."""
        parts = parts_of(shown, start, end, voiced, start)
        self._logged(name, heard, shown)
        said = [(part.text, part.start, part.end) for part in parts]
        message = ("caption", self.run, start, max(end, until), shown)
        self.link.send(message + ((said,) if len(said) > 1 else ()))

    def fast(self) -> bool:
        """The picture plays faster than FAST_RATE (the reader reads ahead at its speed)."""
        viewer = getattr(self, "viewer", None)
        return bool(viewer is not None and len(viewer) > 1 and float(viewer[1]) >= FAST_RATE)

    def stop(self) -> None:
        self.stopped.set()
        self.pieces.put(None)
        self.blocks.put(None)
        self.decoder.stop()

    def _maybe_stream(self) -> None:
        """Turkish with our Moonshine here: from now on the sound goes to the growing writer
        whole, which reads each utterance whole and, while it goes on, again as it grows, with no
        forced cuts (2026-11-07, the owner: pieces cut to the lead lost words at the cuts,
        "Burada her | yeni bir mücadele var" lost "gün")."""
        if getattr(self, "writer", None) is not None or self.language != TURKISH:
            return
        if not getattr(self.model, "moonshine", False):
            return
        self.writer = GrowingWriter(
            self._moon_reading, self.model.is_speech, self.model.is_voiced, self.viewer
        )
        threading.Thread(target=self._stream, name="qitv-captions-stream", daemon=True).start()

    def _moon_reading(self, samples: array) -> str:
        """Our Moonshine's reading of a whole stretch of sound, what nobody said left out."""
        text = guards.clean(self.model.decode(samples, TURKISH, fast=self.fast()))
        if text and getattr(self, "guessed", False):
            self._check_guess(text)
        if text and not text_language.written_in(text, TURKISH):
            return ""  # in another language's letters: not what the channel says
        return text

    def _check_guess(self, text: str) -> None:
        """A run in a language guessed from the channel's title: its readings vote on the
        language they are in. The same, it stands (and the controller hears it); another one,
        the run goes on as one whose language is not known: Qwen tells it from pieces."""
        locked = self.checks.add(text_language.guess(text))
        if locked is None:
            return
        self.guessed = False
        if locked == self.language:
            self.link.send(("detected", self.run, locked))
            return
        logger.info(
            "AI captions: not %s after all (%s): Qwen tells the language", self.language, locked
        )
        self.language = ""
        self.votes = text_language.Votes()
        self.writer = None  # pieces again (`_take`); the growing writer's thread ends

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
                        if not self.read_on:
                            self._resync()
                            return
                        self.times.jumped = False
                        self._stepped()
                    self.decoder.feed(data)
        except Exception as error:
            reason = type(error).__name__  # no message: it could hold the URL
        if not self.stopped.is_set():
            self.decoder.end()
            self._end(reason)

    def _stepped(self) -> None:
        """A step in the stream's clock a run that reads on goes past (said once in the log)."""
        self._steps += 1
        if self._steps == 1:
            logger.info("AI captions: run %d reads on past steps in the stream's clock", self.run)

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
        if at is None and self.clock.broken and self.read_on:
            self.clock.rebase(float(self.newest))
            self._stepped()
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
        if getattr(self, "writer", None) is not None:
            self.blocks.put((at, floats))
            return
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
                    (
                        "progress",
                        self.run,
                        self.newest,
                        round(self.rtf, 3),
                        self.written_to(),
                        self.qwen_writes(),
                    )
                )
            if piece is None or self.stopped.is_set():
                continue
            if not getattr(self.model, "has_model", self.model.recognizer is not None):
                self.link.send(("piece", self.run, piece.start, piece.end, piece.speech_share))
                continue
            if piece.speech_share < _MIN_SPEECH_SHARE:
                continue
            if piece.end + _PASSED_S < float(self.viewer[0]):
                continue  # the picture is past its words: never shown, the time goes to the next
            started = time.perf_counter()
            self._working = piece.start
            try:
                text = self.model.decode(piece.samples, self.language, fast=self.fast())
            except Exception as error:
                logger.warning("AI captions: a piece failed: %s", type(error).__name__)
                continue
            finally:
                self._working = None
            took = time.perf_counter() - started
            length = max(0.1, piece.end - piece.start)
            self.rtf = 0.8 * self.rtf + 0.2 * (took / length)
            wrote = getattr(self.model, "wrote", None)
            name = str(wrote()) if callable(wrote) else "?"
            text = heard = guards.clean(text)
            if not text or self.stopped.is_set():
                continue
            if getattr(self, "guessed", False):
                self._check_guess(text)
            if self.language and not text_language.written_in(text, self.language):
                continue  # in another language's letters: not what the channel says
            if not self.language and not self._heard(text, length):
                continue
            if piece.split:
                text = unfinished(text)
            if self.language == TURKISH and self.model.turkish is not None:
                text = numbers_tr.written(self.sentence_case(text, piece.start, piece.end))
            self._write(name, heard, text, piece.start, piece.end, voiced=piece.voiced)

    def sentence_case(self, text: str, start: float, end: float) -> str:
        """ufak's line with a capital where a sentence starts (the run's first line, after a
        full stop, after a pause of SENTENCE_GAP_S), else as it wrote it: it writes no full
        stops at times, and each line began with a capital mid-sentence (2.9.2, the owner:
        "capitalization is kinda weird since we are not finalizing sentences with dots")."""
        said = self._said
        begins = (
            said is None or said[0].endswith(_SENTENCE_ENDS) or start - said[1] >= SENTENCE_GAP_S
        )
        self._said = (text, end)
        return turkish_capital(text) if begins else text

    def _stream(self) -> None:
        """The growing writer's thread: placed sound in, whole lines out (no pieces, no cuts).
        The sound that came while the model was busy goes in at once: the writer's next reading
        has all of it. It looks at the picture when no sound comes for a moment: a line it is
        about to reach goes out then (the sound comes in bursts, a segment at a time)."""
        writer = self.writer
        assert writer is not None
        name = self._writer_name(TURKISH)
        while not self.stopped.is_set() and self.writer is writer:
            try:
                batch = [self.blocks.get(timeout=_IDLE_S)]
            except queue.Empty:
                batch = []  # no sound for a moment: the picture may still need a line
            while batch and batch[-1] is not None:
                try:
                    batch.append(self.blocks.get_nowait())
                except queue.Empty:
                    break
            if self.stopped.is_set():
                return
            sound = [block for block in batch if block is not None]
            started = time.perf_counter()
            seconds = 0.0
            try:
                for at, floats in sound:
                    seconds += len(floats) / 16000
                    writer.push(at, floats)
                lines = writer.work()
            except Exception as error:
                logger.warning("AI captions: the Turkish model failed: %s", type(error).__name__)
                return
            if seconds:
                took = time.perf_counter() - started
                self.rtf = 0.95 * self.rtf + 0.05 * (took / max(0.01, seconds))
            for text, start, end in lines:
                text = guards.clean(text)
                if text and text_language.written_in(text, TURKISH):
                    # a capital after a sentence end or a pause, as for ufak's lines (the model
                    # starts a sentence after a pause in lower case at times)
                    shown = numbers_tr.written(self.sentence_case(text, start, end))
                    # (the owner's field tests compare what it heard with what shows)
                    self._write(name, text, shown, start, end, until=start + 0.5)
            if None in batch:
                return

    def qwen_writes(self) -> bool:
        """Qwen writes this run's language for good: no specialist (ufak, Cohere, Parakeet)
        writes it, or none is known yet (2.9.6, the real-use review: Automatic stepped Qwen
        down while it only wrote English until Cohere was loaded, and the step down's restart
        made Cohere load again: the first caption came 101 s after captions were on)."""
        language = self.language
        if not language:
            return True
        if language == TURKISH and getattr(self.model, "turkish", None) is not None:
            return False
        writer = getattr(self.model, "writer", None)
        return writer is None or writer(language) is None

    def written_to(self) -> float:
        """How far the captions are written: to the piece being recognized, else to the oldest
        one waiting, else all the sound read (2.9, the review: the lead was the reading's,
        which a model too slow for it never shortened: no "Can't keep up", no step down)."""
        writer = getattr(self, "writer", None)
        if writer is not None:
            return float(writer.frontier())
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
            self.model.prepare(locked)  # 2.9.3: Qwen writes until its specialist is loaded
            self._maybe_stream()  # Turkish heard and our Moonshine here: it takes over
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
        self._announce()
        self.reader.start()
        threading.Thread(target=self._watch, name="qitv-captions-film", daemon=True).start()
        threading.Thread(target=self._recognize, name="qitv-captions-asr", daemon=True).start()
        self._maybe_stream()

    def stop(self) -> None:
        self.stopped.set()
        self.pieces.put(None)
        self.blocks.put(None)
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


def _rate_seen(viewer: list, seen: list, now: float) -> None:
    """The picture's playback rate (viewer[1]) from how its position moved over the last second
    or so (2026-11-09, the owner: buffered channels watched at 2x); a seek or a pause: anew."""
    position = float(viewer[0])
    if seen[0] is None:
        seen[0] = (position, now)
        return
    start, then = seen[0]
    if now - then < 1.0:
        return
    rate = (position - start) / (now - then)
    if 0.5 <= rate <= 4.0:
        viewer[1] = 0.5 * float(viewer[1]) + 0.5 * rate
    seen[0] = (position, now)


def _serve(link: _Link) -> None:
    model: Optional[_Model] = None
    run: Optional[_Run] = None
    viewer = [0.0, 1.0]  # the picture's position, and its playback rate (`_rate_seen`)
    seen: list = [None]  # (position, clock) the rate is measured from
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
            _, number, url, segment_start, audio_track, language = message[:6]
            guessed = bool(message[6]) if len(message) > 6 else False
            read_on = bool(message[7]) if len(message) > 7 else False
            if run is not None:
                run.stop()
                run = None
            if model is None:
                link.send(("ended", number, "no model"))
                continue
            viewer[0] = float(segment_start)
            run = _Run(
                link,
                model,
                number,
                url,
                segment_start,
                audio_track,
                language,
                viewer,
                guessed,
                read_on,
            )
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
            _rate_seen(viewer, seen, time.monotonic())
        elif kind == "language":
            language = message[1]
            if run is not None:
                run.language = language
                run._maybe_stream()
        elif kind == "stop":
            if run is not None:
                run.stop()
                run = None
    if run is not None:
        run.stop()
