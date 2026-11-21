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
("viewer", position), ("language", name), ("translate", target[, folder]) (2.10: the language
lines are translated into, MiLMMT's name, None off; the translation model's folder when it came
after the load; the load's "translate_to" and "translation" start them), ("levels", on) (Plan 4:
a film's run sends its sound's band levels too, for the audio screen's Stage), ("turkish_lm",
path) (2.12.1: the Turkish model's language model, downloaded since the load; the load's
"turkish_lm" names one already here), ("translate_lead", seconds) (2.13.1: the lead a line's
translation has; the load's "translate_lead" too), ("stop",), ("quit",).
From it: ("ready",), ("failed", reason), ("caption", run, start, end, text[, parts[, original]])
(parts, a line of more than one: [(text, start, end), ...], services.captions.parts; a
translated line, `translated_caption`: the translation as its text, its parts as heard ([] for
a line of one), and the line as heard, the original, last),
("progress", run, newest, rtf, written_to, qwen_writes), ("resync", run, newest),
("ended", run, reason),
("levels", run, start_ms, frame_ms, blob) while levels are on, from a film's sound as it is read
(services.levels.LevelStream: the visualizer decodes nothing these captions read),
("detected", run, language) once the pieces agree on one (none was given),
("translate_speed", seconds, threads) (2.13.1: the translator's medium line, timed as it loaded,
on its threads),
("translate_late", run, lead) (a line late for its translation that had the lead),
("log", level, text); loaded without a model (`load` with "model": None, the packaged smoke
test's probe), ("piece", run, start, end, speech_share) for each piece instead. No traceback
or URL crosses: URLs carry the session token.
"""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass, field
import logging
import multiprocessing
from multiprocessing.connection import Connection, wait
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Optional, Sequence
import urllib.request

from services.captions import guards, numbers_tr, text_language
from services.captions.growing import GrowingWriter
from services.captions.languages import COHERE, EUROPEAN, QWEN_LANGUAGES, iso_code
from services.captions.mt_languages import needs_context, source_for
from services.captions.parts import parts_of
from services.captions.pieces import END_PAUSE_S, MAX_PIECE_S, Cutter, Piece
from services.captions.stream_clock import StreamClock, TsTimes
from services.captions.streaming import (
    FIRST_WORD_S,
    HELD_AFTER_S,
    TRANSLATE_LEAD_S,
    TRANSLATE_LEAD_STEP_S,
    time_left,
)

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
    """The process's log to the owner's, from the level `captions_main` sets (DEBUG for
    Settings' detailed log: each caption line, LINE_LEVEL)."""

    def __init__(self, link: _Link) -> None:
        super().__init__()
        self._link = link

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._link.send(("log", record.levelno, record.getMessage()))
        except Exception:
            pass


TURKISH = "Turkish"  # Qwen's name for it: the language our Turkish Moonshine writes (2.10)
_COHERE, _EUROPEAN = "cohere", "european"  # 2.9.3: the specialists (their spec keys)
# ... and the others' parts, by which the log names the model that wrote a line (2026-11-08)
_QWEN, _TURKISH_ROLE = "qwen", "turkish"
_NAMES = {  # ... when its folder says nothing
    _QWEN: "Qwen3-ASR",
    _TURKISH_ROLE: "the Turkish Moonshine",
    _COHERE: "Cohere Transcribe",
    _EUROPEAN: "Parakeet",
}
_LOAD_WAIT_S = 60.0  # a language only a specialist writes waits this long for it to load
# 2.9.5: the voice detector again, without its hold: it says where the pauses between words
# and phrases are (pieces.Cutter's `is_voiced`)
_VOICED_MIN_S = 0.05
_UPPER = {"i": "İ", "ı": "I"}  # Turkish's own capitals of the dotted and the dotless i
_IDLE_S = 0.25  # the growing writer looks at the picture this often when no sound comes
# 2026-11-09, the owner: buffered channels watched at 1.5x or 2x, so 2 s of sound to read a second;
# from this rate on, our Moonshine beam reads its best path alone (about 4x cheaper)
FAST_RATE = 1.25
LINE_LEVEL = logging.DEBUG  # each caption line is logged at this level (2026-11-08)
DEADLINE_MARGIN_S = 0.3  # 2.10: a line's translation is done this long before the line shows
# 2.13.1: a line is translated with the one before (into a target with he and she) when that
# one ended at most this long before it started: the same scene
CONTEXT_GAP_S = 10.0
CONTEXT_MIN_WORDS = 3  # ... and has at least this many words (the owner: "Yes." leaked)


def _folder_name(folder: Any) -> str:
    """A model's folder as the log names the model ("qwen3-asr-0.6b"); "" without one."""
    return Path(str(folder)).name if folder else ""


_SENTENCE_ENDS = (".", "!", "?", "…")
SENTENCE_GAP_S = 1.0  # a pause this long before a line: it starts a sentence


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
    for Turkish QiTV's own Turkish Moonshine (2.10; 2.9.2 to 2.9.10: ufakzeka-kulak, far better
    Turkish than Qwen's, at a twenty-fifth of its time) once the files are given."""

    moonshine = False  # our Turkish Moonshine writes Turkish (2.10)
    # ... read with our beam (moon_beam.BeamReader); 2.12.1: and its language model, once here
    # (moon_reading.TurkishReader round the beam)
    beam: Any = None
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
        # 2.10: a computer too slow to translate at full quality reads Turkish with its best
        # path alone while translating (`_Run.fast`); an older GUI doesn't say
        self.slow_to_translate = bool(spec.get("slow_to_translate"))
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
            # 4-path beam alone (no n-gram), as its model card's beam numbers were read; one
            # that does not load leaves Turkish to Qwen (the final review: it failed the whole
            # process, every language). 2.12.1: with its LSTM language model when the load names
            # it (models.TURKISH_LM); one that won't load or fails leaves the beam alone, never
            # Qwen
            folder = Path(turkish)
            self.names[_TURKISH_ROLE] = _folder_name(folder)
            try:
                from services.captions.moon_beam import BeamReader
                from services.captions.moon_reading import TurkishReader

                lm = spec.get("turkish_lm")
                beam = BeamReader(folder, None, threads=min(2, threads))
                self.beam = self.turkish = TurkishReader(beam, Path(lm) if lm else None)
                self.moonshine = True
            except Exception as error:
                self.drop_turkish("did not load", error)
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
                try:
                    self.beam.warm()  # (silence is skipped: a quiet tone; 2.12.1: the LM loads)
                except Exception as error:
                    self.drop_turkish("did not load", error)

    def use_turkish_lm(self, path: str) -> None:
        """2.12.1: the Turkish model's language model, downloaded since the load: Turkish is read
        with it from the next piece."""
        use = getattr(self.beam, "use_lm", None)
        if callable(use):
            use(path)

    def drop_turkish(self, what: str, error: BaseException) -> None:
        """Our Turkish Moonshine writes no more, for the process's life: Qwen writes Turkish."""
        logger.warning(
            "AI captions: the Turkish model %s (%s: %s); Qwen writes Turkish",
            what,
            type(error).__name__,
            error,
        )
        self.beam = self.turkish = None
        self.moonshine = False

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
        beam = self.beam  # (once: a failure on the stream's thread lets it go, `drop_turkish`)
        if language == TURKISH and beam is not None:
            self._wrote.name = self.name(_TURKISH_ROLE)
            width = 1 if fast else None
            return str(beam.transcribe(samples, width=width)).strip()  # as sherpa-onnx's
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

    translation: Optional[_Translation] = None  # 2.10: its lines go out through it while on

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
        translation: Optional[_Translation] = None,
    ) -> None:
        self.link = link
        self.model = model
        self.run = run
        self.url = url
        self.translation = translation
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
        captions show a long line a part at a time as each is said. 2.10: while translation is on,
        the line goes to it instead (it sends the line, translated or as written)."""
        parts = parts_of(shown, start, end, voiced, start)
        self._logged(name, heard, shown)
        said = [(part.text, part.start, part.end) for part in parts]
        message: tuple = ("caption", self.run, start, max(end, until), shown)
        message += (said,) if len(said) > 1 else ()
        translation = self.translation
        if translation is None or not translation.take(
            _Line(self, self.language, shown, start, message, end)
        ):
            self.link.send(message)

    def fast(self) -> bool:
        """The picture plays faster than FAST_RATE (the reader reads ahead at its speed), or
        2.10: this computer is slow to translate and translation is on (the same "on" as
        `_Translation.reserve`; off again, the beam comes back at the next reading)."""
        viewer = getattr(self, "viewer", None)
        if viewer is not None and len(viewer) > 1 and float(viewer[1]) >= FAST_RATE:
            return True
        translation = getattr(self, "translation", None)
        slow = getattr(getattr(self, "model", None), "slow_to_translate", False)
        return bool(slow and translation is not None and translation.reserve() > 0)

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
        """How long a piece may be and still be written before the picture gets to it, the
        translator's time before that while translating (`_reserve`)."""
        lead = float(self.newest) - float(self.viewer[0]) - self._reserve()
        return (lead - END_PAUSE_S - _MARGIN_S) / (1.0 + float(self.rtf))

    def _reserve(self) -> float:
        """Seconds a line must be out before the picture gets to it, for its translation (2.10:
        the translation's lead while translating, `_Translation.reserve`; else 0)."""
        translation = self.translation
        return 0.0 if translation is None else translation.reserve()

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
        """A Turkish line with a capital where a sentence starts (the run's first line, after a
        full stop, after a pause of SENTENCE_GAP_S), else as the model wrote it: it writes no
        full stops at times, and each line began with a capital mid-sentence (2.9.2, the owner on
        ufak: "capitalization is kinda weird since we are not finalizing sentences with dots")."""
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
            writer.reserve = self._reserve()  # (translation turned on or off mid-run)
            started = time.perf_counter()
            seconds = 0.0
            try:
                for at, floats in sound:
                    seconds += len(floats) / 16000
                    writer.push(at, floats)
                lines = writer.work()
            except Exception as error:
                # pieces again, Qwen's (the final review: the writer stayed, and the sound piled
                # up in `blocks`, read by nothing)
                drop = getattr(self.model, "drop_turkish", None)
                if callable(drop):
                    drop("failed", error)
                else:
                    logger.warning(
                        "AI captions: the Turkish model failed: %s: %s", type(error).__name__, error
                    )
                self.writer = None
                return
            if seconds:
                took = time.perf_counter() - started
                self.rtf = 0.95 * self.rtf + 0.05 * (took / max(0.01, seconds))
            for text, start, end in lines:
                text = guards.clean(text)
                if text and text_language.written_in(text, TURKISH):
                    # a capital after a sentence end or a pause (the model starts a sentence
                    # after a pause in lower case at times)
                    shown = numbers_tr.written(self.sentence_case(text, start, end))
                    # (the owner's field tests compare what it heard with what shows)
                    self._write(name, text, shown, start, end, until=start + 0.5)
            if None in batch:
                return

    def qwen_writes(self) -> bool:
        """Qwen writes this run's language for good: no specialist (the Turkish Moonshine, Cohere,
        Parakeet) writes it, or none is known yet (2.9.6, the real-use review: Automatic stepped
        Qwen down while it only wrote English until Cohere was loaded, and the step down's
        restart made Cohere load again: the first caption came 101 s after captions were on)."""
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

    # Plan 4: the film's sound as band levels too, while the process's [on] says so
    levels_on: Sequence = (False,)
    levels: Any = None

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
        translation: Optional[_Translation] = None,
        levels_on: Optional[list] = None,
    ) -> None:
        super().__init__(
            link, model, run, "", start_s, audio_track, language, viewer, translation=translation
        )
        from services.captions.film import FilmReader
        from services.levels import LevelStream

        if levels_on is not None:
            self.levels_on = levels_on
        self.levels = LevelStream(lambda *chunk: link.send(("levels", run) + chunk))
        self.reader = FilmReader(
            location, start_s, self._take, audio_track=audio_track, options=options
        )

    def _take(self, at: float, floats: array) -> None:
        """The film's sound (libVLC's thread): its band levels too while asked (the audio
        screen's Stage reads them instead of decoding it again), then the captions'."""
        if self.levels is not None and self.levels_on[0] and not self.stopped.is_set():
            self.levels.push(at, floats)
        super()._take(at, floats)

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
                if self.levels is not None and self.levels_on[0]:
                    self.levels.flush()  # (libVLC is done: its last levels go)
                self.pieces.put(None)
                self._end("ended")
                return


def translated_caption(message: tuple, text: str) -> tuple:
    """The caption `message`, as the line was written, with `text`, its translation, as what
    shows: its parts stay (as heard: each sentence's time; [] for a line of one, so a reader of
    the shape before reads it as it read any line) and the line as heard comes after them (Plan
    4: the audio screen's Stage shows both)."""
    parts = list(message[5]) if len(message) > 5 else []
    return message[:4] + (text, parts, message[4])


@dataclass
class _Line:
    """A caption line for the translator (2.10): its run, the language the run wrote it in, what
    shows, where its sound starts, and the message that sends it as written; 2.13.1: where its
    sound ends, and when it was written (time.monotonic())."""

    run: _Run
    language: str
    shown: str
    start: float
    message: tuple
    end: float = 0.0
    written: float = field(default_factory=time.monotonic)


@dataclass
class _Said:
    """2.13.1: the last line that went out translated, for the next one's context: its run, its
    language and target, its sound's span, the line and its translation."""

    run: _Run
    language: str
    target: str
    start: float
    end: float
    shown: str
    text: str


class _Translation:
    """2.10, caption translation (MiLMMT-46 1B, services.captions.translate), one for the
    process. The model loads on its own thread as soon as a target and the model's folder are
    known, and stays while there is a target (turned off, it goes: some 700 MB; a target again
    loads it again). While it is loaded, every line goes through one queue, in order, to that
    thread: a line in a language MiLMMT knows, not the target, goes out translated when that is
    done DEADLINE_MARGIN_S before the picture gets to it, else as written; the others go out as
    written, in their turn. Until it is loaded, or if it could not be, lines go out as written at
    once: none waits for the load. A line of a run no longer the current one (stopped, or another
    started) is dropped. The speech threads only queue: translation's time is never theirs
    (progress, rtf, the step down). The translator is imported on that thread alone (the GUI's
    process imports this module).

    2.13.1: the translator runs on this computer's threads (tier.translator_threads); its
    warm-up's medium line is timed and told to the GUI ("translate_speed"), which keeps the lead
    (services.captions.streaming.TranslationLead) and tells it back ("translate_lead", and the
    load's "translate_lead": the reserve the speech side leaves it). A line late for its
    translation that had that lead (less a step) from being written to its deadline says so
    ("translate_late"): the lead is short. Into a target with he and she
    (mt_languages.needs_context) a line goes with the one before and its translation when that
    one went out translated, in the same run and language, at most CONTEXT_GAP_S before it, and
    was CONTEXT_MIN_WORDS words or more. A line translated again alone after the line before
    leaked (Translator.second_try) is not late for the lead."""

    def __init__(self, link: _Link, viewer: list) -> None:
        self.link = link
        self.viewer = viewer  # [position, rate], shared with the command loop
        # when the command loop last saw the picture move (time.monotonic()); a run starts with
        # it waiting, as the growing writer counts on
        self.moved = time.monotonic() - HELD_AFTER_S
        self._lock = threading.Condition()  # over all below
        self._lines: deque[_Line] = deque()
        self._busy: Optional[_Line] = None  # the line the thread has, until it goes out
        self._current: Optional[_Run] = None  # the run whose lines go out
        self._target: Optional[str] = None
        self._folder: Optional[Path] = None
        self._translator: Any = None  # the model, loaded from the folder `_loaded`
        self._loaded: Optional[Path] = None
        self._failed: set[Path] = set()  # folders whose model did not load: never tried again
        self._thread: Optional[threading.Thread] = None
        self._told = False  # a target without the model's folder: said once
        self._lead = TRANSLATE_LEAD_S  # 2.13.1: the GUI's lead (an older GUI's: 2.10's)
        self._before: Optional[_Said] = None  # 2.13.1: the last line out translated

    def follow(self, run: Optional[_Run]) -> None:
        """`run`'s lines go out from now on (None: no run's), from its start: the picture waits."""
        with self._lock:
            self._current = run
            self._before = None  # (a zap, a tune, a seek: no line before)
            self.moved = time.monotonic() - HELD_AFTER_S

    def set_lead(self, seconds: Any) -> None:
        """2.13.1: the GUI's lead for a line's translation (None: the one it has stays)."""
        if seconds is not None:
            with self._lock:
                self._lead = max(0.0, float(seconds))

    def set(self, target: Optional[str], folder: Any = None) -> None:
        """Lines are translated into `target` (MiLMMT's name; None: they are not) by the model in
        `folder` (when given, else the one known), which loads now. Until it has, and once off,
        the lines waiting go out as written at once."""
        with self._lock:
            if folder:
                self._folder = Path(folder)
            self._target = target or None
            self._before = None
            if self._target and self._folder is None and not self._told:
                self._told = True
                logger.info(
                    "AI captions: no translation into %s: the translation model is not here",
                    self._target,
                )
            if not self._ready():
                self._flush("translation off" if not self._on() else "the model is loading")
            if self._target is None:
                # its memory back (the final review: it stayed till captions went off); a line
                # being translated keeps it till that is done (the thread's own reference)
                self._translator = self._loaded = None
            if self._to_load() is not None:
                if self._thread is None:
                    self._thread = threading.Thread(
                        target=self._work, name="qitv-captions-translate", daemon=True
                    )
                    self._thread.start()
                self._lock.notify()

    def take(self, line: _Line) -> bool:
        """`line` queued while the model is here to translate it: the thread sends it. False: the
        caller does, at once."""
        with self._lock:
            if not self._ready():
                return False
            thread = self._thread
            if thread is None or not thread.is_alive():
                # (the final review's insurance: a thread gone would keep every line queued)
                self._flush("the translator's thread is gone")
                return False
            self._lines.append(line)
            self._lock.notify()
        return True

    def reserve(self) -> float:
        """Seconds the speech side leaves the translator before a line shows: the lead (the
        GUI's, else TRANSLATE_LEAD_S) while lines are to be translated (a target, and a model's
        folder that did not fail, loaded or loading), else 0."""
        with self._lock:
            on = self._on() and self._folder not in self._failed
            return self._lead if on else 0.0

    def _on(self) -> bool:
        return bool(self._target) and self._folder is not None

    def _ready(self) -> bool:
        """On, and the model of the folder loaded."""
        return self._on() and self._translator is not None and self._loaded == self._folder

    def _to_load(self) -> Optional[Path]:
        """The folder whose model is to load now (the lock is held), else None."""
        folder = self._folder
        wanted = self._on() and folder != self._loaded and folder not in self._failed
        return folder if wanted else None

    def _flush(self, why: str) -> None:
        """The line the thread has and those waiting out as written, those of a run no longer
        the current one dropped (the lock is held)."""
        waiting = list(self._lines) if self._busy is None else [self._busy, *self._lines]
        self._busy = None
        self._lines.clear()
        self._before = None
        for line in waiting:
            if line.run is self._current:
                logger.log(LINE_LEVEL, "AI captions: shown as written, %s: %r", why, line.shown)
                self.link.send(line.message)

    # ---- the thread ----------------------------------------------------------------------
    def _work(self) -> None:
        while True:
            with self._lock:
                while not self._lines and self._to_load() is None:
                    self._lock.wait()
                folder = self._to_load()
                if folder is not None:
                    self._translator = self._loaded = None  # the one before goes (some 700 MB)
            if folder is not None:
                self._load(folder)
            else:
                self._next()

    def _load(self, folder: Path) -> None:
        """The model in `folder`, loaded and kept while it is still the one wanted; one that does
        not load, never tried again."""
        began = time.monotonic()
        translator: Any = None
        failure = ""  # (services.captions.translate logs its own)
        try:
            from services.captions import tier
            from services.captions.translate import Translator

            threads = tier.translator_threads()
            translator = Translator(folder, threads=threads)
            loaded = bool(translator.warm())
            cause = "" if loaded else str(getattr(translator, "error", "") or "")
            if cause:
                failure = f": {cause}"  # (the owner's log gets a record's message alone)
            elif loaded:
                self._timed(translator, threads)
        except Exception as error:
            loaded, failure = False, f": {type(error).__name__}: {error}"
        with self._lock:
            if loaded and not (self._on() and folder == self._folder):
                logger.info(
                    "AI captions: the translation model (%s) is not kept: no longer wanted",
                    _folder_name(folder),
                )
            elif loaded:
                self._translator, self._loaded = translator, folder
                logger.info(
                    "AI captions: loading the translation model (%s) took %.1f s",
                    _folder_name(folder),
                    time.monotonic() - began,
                )
            else:
                self._failed.add(folder)
                logger.warning(
                    "AI captions: caption translation is unavailable: the translation model "
                    "(%s) did not load%s",
                    _folder_name(folder),
                    failure,
                )

    def _timed(self, translator: Any, threads: int) -> None:
        """2.13.1: the loaded translator's medium line, timed (its warm-up), told to the GUI,
        which sets the lead by it; nothing when it gave none (the GUI's lead stays)."""
        speed = getattr(translator, "speed", None)
        seconds = speed() if callable(speed) else None
        if seconds is None:
            return
        logger.info(
            "AI captions: the translator does a medium line in %.2f s (%d threads)",
            seconds,
            threads,
        )
        self.link.send(("translate_speed", round(float(seconds), 3), threads))

    def _next(self) -> None:
        """The next line waiting out, translated or as written."""
        with self._lock:
            if not self._lines:
                return  # sent as written meanwhile
            line = self._busy = self._lines.popleft()
            target, translator = self._target, self._translator
            context = self._context_for(line, target)
        try:
            text, why = self._translated(line, target, translator, context)
        except Exception as error:  # the thread lives on: every line goes through it
            logger.warning("AI captions: a translation failed: %s", type(error).__name__)
            text, why = None, "its translation failed"
        with self._lock:
            if self._busy is not line:
                return  # sent as written meanwhile (turned off, another model)
            self._busy = None
            self._before = None
            if line.run is not self._current:
                return  # its run ended (a seek, another channel, captions off): dropped
            if text is not None and target != self._target:
                text, why = None, "the target changed"
            if text is not None and target is not None:
                logger.log(LINE_LEVEL, "AI captions: translated %r: %r", line.shown, text)
                self.link.send(translated_caption(line.message, text))
                self._before = _Said(
                    line.run, line.language, target, line.start, line.end, line.shown, text
                )
                return
            if why:
                logger.log(LINE_LEVEL, "AI captions: shown as written, %s: %r", why, line.shown)
            self.link.send(line.message)

    def _context_for(self, line: _Line, target: Optional[str]) -> Optional[tuple[str, str]]:
        """2.13.1: the line before `line` and its translation, for a target with he and she
        (the lock is held): the last line out translated, of the same run, language and target,
        ended at most CONTEXT_GAP_S before this one starts; else None (translated alone)."""
        before = self._before
        if before is None or not target or not needs_context(target):
            return None
        if before.run is not line.run or before.language != line.language:
            return None
        if before.target != target or line.start < before.start:
            return None
        if line.start - before.end > CONTEXT_GAP_S:
            return None  # another scene
        if len(before.shown.split()) < CONTEXT_MIN_WORDS:
            return None  # ("Yes.": it leaked into the next, translated again alone)
        return before.shown, before.text

    def _translated(
        self,
        line: _Line,
        target: Optional[str],
        translator: Any,
        context: Optional[tuple[str, str]] = None,
    ) -> tuple[Optional[str], str]:
        """`line` in `target` when it is to be translated and that is done in time (2.13.1: with
        `context`, the line before, when given); else None (it goes out as written) and why, ""
        for a line not to translate."""
        source = source_for(line.language)  # None: not known yet ("") or not MiLMMT's
        if translator is None or not target or source is None or source == target:
            return None, ""
        with self._lock:
            current = line.run is self._current
            lead = self._lead
        if not current:
            return None, ""  # dropped
        if line.run.stopped.is_set():
            return None, "its run reads no more"  # read again past a jump: its lines still show
        deadline = self._deadline(line)
        began = time.monotonic()
        if began >= deadline:
            self._late(line, deadline, lead)
            return None, f"{began - deadline:.1f} s late for its translation"
        text: Optional[str] = translator.translate(
            line.shown, source, target, deadline, context=context
        )
        if text is not None:
            return text, ""
        if time.monotonic() > deadline:
            if not getattr(translator, "second_try", False):  # (the first try's time alone)
                self._late(line, deadline, lead)
            return None, f"not translated in the {deadline - began:.1f} s it had"
        return None, "no usable translation"

    def _late(self, line: _Line, deadline: float, lead: float) -> None:
        """2.13.1: `line` missed its deadline. One that had the lead (less a step) from being
        written to its deadline tells the GUI the lead is short; one written later (a run's
        first lines after a seek, the speech side behind) is not the translator's."""
        if deadline - line.written >= lead - TRANSLATE_LEAD_STEP_S:
            self.link.send(("translate_late", getattr(line.run, "run", 0), lead))

    def _deadline(self, line: _Line) -> float:
        """When `line`'s translation must be done (a time.monotonic() value): DEADLINE_MARGIN_S
        before the picture gets to its first word, at the pace the growing writer counts on
        (still since the command loop last saw it move), sooner as it plays faster than 1x."""
        now, position = time.monotonic(), float(self.viewer[0])
        still = now - self.moved
        left = time_left(line.start + FIRST_WORD_S, position, still, float(line.run.newest))
        return now + left / max(1.0, float(self.viewer[1])) - DEADLINE_MARGIN_S


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
    translation = _Translation(link, viewer)  # 2.10: one for the process, across its runs
    levels_on = [False]  # Plan 4: a film's run sends its band levels too
    while True:
        try:
            message: Any = link.conn.recv()
        except EOFError, OSError:
            break
        kind = message[0]
        if kind == "quit":
            break
        if kind == "load":
            spec = message[1]
            try:
                model = _Model(spec)
                link.send(("ready",))
            except Exception as error:
                logger.warning("AI captions: the model did not load: %s", error)
                link.send(("failed", type(error).__name__))
                continue
            # after the speech model: the translator finds onnxruntime in its runtime (sys.path)
            translation.set_lead(spec.get("translate_lead"))  # 2.13.1 (an older GUI's: none)
            translation.set(spec.get("translate_to"), spec.get("translation"))
        elif kind == "start":
            _, number, url, segment_start, audio_track, language = message[:6]
            guessed = bool(message[6]) if len(message) > 6 else False
            read_on = bool(message[7]) if len(message) > 7 else False
            if run is not None:
                run.stop()
                run = None
            translation.follow(None)
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
                translation=translation,
            )
            translation.follow(run)
            run.start()
        elif kind == "film":
            _, number, location, start_s, audio_track, language, options = message
            if run is not None:
                run.stop()
                run = None
            translation.follow(None)
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
                translation=translation,
                levels_on=levels_on,
            )
            translation.follow(run)
            run.start()
        elif kind == "levels":
            levels_on[0] = bool(message[1])
        elif kind == "viewer":
            position = float(message[1])
            if position != viewer[0]:
                translation.moved = time.monotonic()  # it moved: the translator's pace
            viewer[0] = position
            _rate_seen(viewer, seen, time.monotonic())
        elif kind == "language":
            language = message[1]
            if run is not None:
                run.language = language
                run._maybe_stream()
        elif kind == "translate":
            translation.set(message[1], message[2] if len(message) > 2 else None)
        elif kind == "translate_lead":  # 2.13.1: the GUI's lead for a line's translation
            translation.set_lead(message[1])
        elif kind == "turkish_lm":  # 2.12.1: downloaded since the load
            if model is not None:
                model.use_turkish_lm(str(message[1]))
        elif kind == "stop":
            if run is not None:
                run.stop()
                run = None
            translation.follow(None)
    if run is not None:
        run.stop()
