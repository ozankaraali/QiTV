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
("viewer", position), ("language", name), ("speakers", diarizer folder or None, speaker
model file), ("stop",), ("quit",).
From it: ("ready",), ("failed", reason), ("caption", run, start, end, text, turn, speaker[,
parts]) (turn: 1 when another speaker than the line before's says it; speaker: 1, 2, ... in the
run, 0 untold: services.captions.speakers; parts, a line of more than one: [(text, start, end,
turn, speaker), ...]),
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

from services.captions import guards, numbers_tr, text_language, trial_models
from services.captions.growing import GrowingWriter
from services.captions.languages import COHERE, EUROPEAN, QWEN_LANGUAGES, iso_code
from services.captions.pieces import END_PAUSE_S, MAX_PIECE_S, Cutter, Piece
from services.captions.speakers import Recent, Turn, parts_of, told_parts
from services.captions.stream_clock import StreamClock, TsTimes
from services.captions.streaming import StreamWriter

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
_QWEN, _TURKISH_ROLE, _STREAM_ROLE = "qwen", "turkish", "stream"
_NAMES = {  # ... when its folder says nothing
    _QWEN: "Qwen3-ASR",
    _TURKISH_ROLE: "ufakzeka-kulak",
    _STREAM_ROLE: "the streaming Turkish model",
    _COHERE: "Cohere Transcribe",
    _EUROPEAN: "Parakeet",
}
_LOAD_WAIT_S = 60.0  # a language only a specialist writes waits this long for it to load
_TURKISH_GAIN = 8.0  # the model card's input gain: its sherpa-onnx files want the sound louder
# 2.9.5: the voice detector again, without its hold: it says where the pauses between words
# and phrases are (pieces.Cutter's `is_voiced`)
_VOICED_MIN_S = 0.05
_UPPER = {"i": "İ", "ı": "I"}  # Turkish's own capitals of the dotted and the dotless i
# A streaming Turkish model (a dev trial: our own FastConformer, sherpa-onnx's online files in
# this folder): Turkish runs feed it the sound continuously instead of cutting pieces, so no
# word is ever cut; services.captions.streaming makes the lines.
STREAM_ENV = "QITV_TR_STREAM"
# A dev trial: our own Turkish Moonshine (sherpa-onnx's Moonshine v2 files in this folder:
# encoder_model, decoder_model_merged, tokens.txt) writes Turkish in ufakzeka-kulak's place.
# It was trained on the sound as it comes (no input gain) and its frontend reads 5 ms frames
# (80 samples), so a piece is padded to a whole number of them. sherpa-onnx runs Moonshine only
# on whole pieces (no streaming, no word times), and pieces cut to the lead lost words at the
# cuts (the owner, 2026-11-07: "Burada her | yeni bir mücadele var" lost "gün"): the trial's
# Turkish goes to services.captions.growing instead, which reads each utterance whole and, while
# it goes on, again as it grows, with no forced cuts.
MOON_ENV = "QITV_TR_MOONSHINE"
_MOON_FRAME = 80
_IDLE_S = 0.25  # the growing writer looks at the picture this often when no sound comes
SEND_MARGIN_S = 1.0  # a line waiting for its speakers goes when the picture is this near it
FRESH_FROM = 1000  # the speaker numbers of cuts whose speaker is unsure (a fresh colour each)
# The trial's field test set (the owner, 2026-11-07): with a folder named here, each piece the
# Moonshine trial reads is kept as a 16 kHz WAV beside a line of what the model wrote, to label.
SAVE_ENV = "QITV_TR_SAVE_PIECES"


def line_level() -> int:
    """The level each caption line is logged at (2026-11-08, the owner): INFO in a run from
    source or with QITV_TRIAL_MODELS=1 (where test models are offered, to compare them); DEBUG
    in a released build, which logged no lines before."""
    return logging.INFO if trial_models.enabled() else logging.DEBUG


def _folder_name(folder: Any) -> str:
    """A model's folder as the log names the model ("qwen3-asr-0.6b"); "" without one."""
    return Path(str(folder)).name if folder else ""


def _label(folder: Any) -> str:
    """A test model's folder as the list names it ("FastConformer r2 · tr-fc-r2"), else its
    folder's name."""
    found = trial_models.describe(Path(str(folder)))
    return found.label if found is not None else _folder_name(folder)


def keep_piece(folder: str, start: float, end: float, samples: array, text: str) -> None:
    """One trial piece kept: `folder`/<time>-<start>.wav (16 kHz, 16-bit) and a line in pieces.tsv
    (file, start, end, what the model wrote)."""
    import wave

    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{start:09.2f}.wav"
    pcm = array("h", (max(-32768, min(32767, int(s * 32767))) for s in samples))
    with wave.open(str(path / name), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(pcm.tobytes())
    with open(path / "pieces.tsv", "a", encoding="utf-8") as listing:
        listing.write(f"{name}\t{start:.2f}\t{end:.2f}\t{' '.join(text.split())}\n")


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

    moonshine = False  # the Turkish Moonshine trial writes Turkish (MOON_ENV)
    trial = False  # a test model picked by hand writes everything, as Turkish (spec "trial")
    beam: Any = None  # ... a Moonshine read with our beam and n-gram (moon_beam.BeamReader)
    # 2026-11-08, the owner: "Write to logs which one writes": each model's name in the log,
    # by its part (_QWEN, _TURKISH_ROLE, _STREAM_ROLE, _COHERE, _EUROPEAN)
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
        self.recognizer: Any = None
        self._qwen_paths = paths
        self._qwen_done = threading.Event()
        self._qwen_started = False
        turkish = spec.get("turkish")
        stream_dir = spec.get("turkish_stream") or os.environ.get(STREAM_ENV)
        moon_dir = os.environ.get(MOON_ENV)
        # 2026-11-08, the owner: a test model picked in the Subtitles panel's Test model list
        # (services.captions.trial_models) runs as it is: it alone, on Turkish, no language
        # detection and no Qwen; the environment's trials (MOON_ENV, STREAM_ENV) step aside.
        picked = spec.get("trial")
        found = trial_models.describe(Path(picked)) if paths and picked else None
        if picked and found is None:
            logger.warning("AI captions: the test model %s isn't here; the usual models", picked)
        beam: Optional[trial_models.TrialModel] = None
        if found is not None:
            self.trial = True
            kind, trial_dir = found.kind, str(found.folder)
            turkish = trial_dir if kind == trial_models.UFAK else None
            moon_dir = trial_dir if kind == trial_models.MOONSHINE else None
            stream_dir = trial_dir if kind == trial_models.STREAM else None
            if found.decoding == trial_models.BEAM_LM:
                beam = found  # read with our beam and the n-gram, not sherpa-onnx's greedy
            logger.info("AI captions: the test model %s writes Turkish", found.label)
        self.names = {_QWEN: _folder_name(Path(paths["encoder"]).parent if paths else None)}
        # 2026-11-07: with the Moonshine trial writing Turkish, Qwen loads (and warms up) only
        # once a run needs it: its load and warm-up kept the captions from starting while the
        # picture played on (`prepare_qwen`); a test model's runs never need it
        if paths and not moon_dir and not self.trial:
            self.recognizer = self._new_qwen()
            self._qwen_done.set()
        self.turkish: Any = None
        self.moonshine = False
        self.beam = None
        if paths and beam is not None:
            from services.captions.moon_beam import BeamReader

            self.beam = self.turkish = BeamReader(beam.folder, beam.lm, threads=min(2, threads))
            self.moonshine = True
            self.names[_TURKISH_ROLE] = beam.label
        elif paths and moon_dir:
            self.names[_TURKISH_ROLE] = found.label if found is not None else _label(moon_dir)
            encoder, decoder, tokens = trial_models.moonshine_files(Path(moon_dir))
            self.turkish = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
                encoder=str(encoder),
                decoder=str(decoder),
                tokens=str(tokens),
                num_threads=min(2, threads),
            )
            self.moonshine = True
            logger.info("AI captions: the Turkish Moonshine trial writes Turkish")
        elif paths and turkish:
            folder = Path(turkish)
            self.names[_TURKISH_ROLE] = found.label if found is not None else _label(folder)
            self.turkish = sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(folder / "encoder.int8.onnx"),
                decoder=str(folder / "decoder.int8.onnx"),
                joiner=str(folder / "joiner.int8.onnx"),
                tokens=str(folder / "tokens.txt"),
                num_threads=min(2, threads),
                model_type="nemo_transducer",
            )
        self.turkish_stream: Any = None
        if stream_dir:
            folder = Path(stream_dir)
            self.names[_STREAM_ROLE] = found.label if found is not None else _label(folder)
            self.turkish_stream = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(folder / "tokens.txt"),
                encoder=str(folder / "encoder.int8.onnx"),
                decoder=str(folder / "decoder.int8.onnx"),
                joiner=str(folder / "joiner.int8.onnx"),
                num_threads=min(2, threads),
                sample_rate=16000,
                feature_dim=80,
                decoding_method="modified_beam_search",  # beam 4 and a small penalty on
                max_active_paths=4,  # "write nothing": the model drops fewer words
                blank_penalty=0.5,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=0.8,  # a pause ends what was said: its line shows
                rule3_min_utterance_length=20.0,
            )
            logger.info("AI captions: the streaming Turkish model is loaded")
        # 2.9.3: the specialists, each loaded once a run's language needs it (in its own thread;
        # Qwen writes meanwhile): Cohere Transcribe for its 14 languages, told the language,
        # and NVIDIA's Parakeet TDT 0.6B v3 for the other European ones. Qwen still tells the
        # language, and writes the rest. Both at once would hold some 4 GB.
        self._folders = {
            name: Path(spec[name]) for name in (_COHERE, _EUROPEAN) if paths and spec.get(name)
        }
        if self.trial:
            self._folders = {}  # Turkish only: no specialist
        self.names.update({name: _folder_name(folder) for name, folder in self._folders.items()})
        self.cohere: Any = None
        self.european: Any = None
        self._loading: dict[str, threading.Event] = {}
        # 2026-11-08, the owner's Speaker marks (a run from source): the diarizer (2026-11-09:
        # services.captions.sortformer), and the voice detector of the sound lines are timed by
        # (its own: its windows come in their order)
        self.diarizer: Any = None
        self.pitch: Any = None  # ... and the cut layer's (services.captions.cuts)
        self.speakers_on = False
        self._speaker_vad: Any = None
        self._speaker_lock = threading.Lock()
        self._speakers_loading = False

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
        self._detector = detector
        self._lock = threading.Lock()
        self.use_speakers(spec.get("diarizer"), spec.get("voice_prints"))
        if self.has_model:
            # Warm up: the first decode is slow (graph optimisation), not the viewer's first.
            if self.recognizer is not None:
                self.decode(array("f", bytes(16000 * 4)), "")
            if self.beam is not None:
                self.beam.warm()  # (silence is skipped: a quiet tone)
            elif self.turkish is not None:
                self.decode(array("f", bytes(16000 * 4)), TURKISH)

    def use_speakers(self, diarizer: Optional[str], prints: Optional[str] = None) -> None:
        """2026-11-08, Speaker marks: each line's speakers told by the streaming diarizer whose
        files are in the folder `diarizer`, who is who across its fresh starts by the speaker
        model in the file `prints`, and a clear flip of the voice's gender as a cut (2026-11-09;
        they load in their own thread the first time; lines go untold till then); None: no longer
        told."""
        if not diarizer or not self.has_model:
            self.speakers_on = False
            return
        self.speakers_on = True
        if self.diarizer is not None or self._speakers_loading:
            return
        self._speakers_loading = True
        threading.Thread(
            target=self._load_speakers,
            args=(diarizer, prints),
            name="qitv-captions-speakers",
            daemon=True,
        ).start()

    def _load_speakers(self, folder: str, prints: Optional[str]) -> None:
        try:
            from services.captions.cuts import Pitch
            from services.captions.sortformer import SherpaPrints, Sortformer

            self._speaker_vad = self._detector(_VOICED_MIN_S)
            voices = SherpaPrints(self._sherpa, prints) if prints else None
            self.pitch = Pitch(folder)
            self.diarizer = Sortformer(folder, prints=voices.print_ if voices else None)
            logger.info(
                "AI captions: the diarizer is loaded (%s%s)",
                Path(folder).name,
                f", voice prints by {Path(prints).name}" if prints else "",
            )
        except Exception as error:  # the lines go on, untold
            logger.warning("AI captions: the diarizer did not load: %s", type(error).__name__)
        finally:
            self._speakers_loading = False

    def speaker_voiced(self, window: array) -> bool:
        """Whether a window of the sound the speaker model reads is voiced."""
        with self._speaker_lock:
            return bool(self._speaker_vad.is_speech(window))

    def name(self, part: str) -> str:
        """The model of a part (_QWEN, _TURKISH_ROLE, ...) as the log names it."""
        return self.names.get(part) or _NAMES.get(part, part)

    def wrote(self) -> str:
        """The model that wrote this thread's last decode, as the log names it."""
        return str(getattr(self._wrote, "name", "") or "")

    def writes(self, language: str) -> str:
        """The model that writes `language` now, as the log names it: for Turkish the streaming
        model or the Turkish one, else its specialist once loaded, else Qwen."""
        if language == TURKISH and getattr(self, "turkish_stream", None) is not None:
            return self.name(_STREAM_ROLE)
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

    def prepare_qwen(self) -> None:
        """Qwen loads now, in its own thread, when it was left for later and has not begun."""
        if self.recognizer is not None or not self._qwen_paths or self._qwen_started:
            return
        if self.trial:
            return  # a test model writes everything (as Turkish)
        self._qwen_started = True
        threading.Thread(target=self._load_qwen, name="qitv-captions-qwen", daemon=True).start()

    def _load_qwen(self) -> None:
        try:
            recognizer = self._new_qwen()
            stream = recognizer.create_stream()  # warm up, as at load
            stream.accept_waveform(16000, array("f", bytes(16000 * 4)))
            recognizer.decode_stream(stream)
            self.recognizer = recognizer
            logger.info("AI captions: Qwen is loaded (a run needs it)")
        except Exception as error:  # its languages get no captions; Turkish goes on
            logger.warning("AI captions: Qwen did not load: %s", type(error).__name__)
        finally:
            self._qwen_done.set()

    def qwen(self) -> Any:
        """Qwen, once loaded; one left for later loads now (waited for, `_LOAD_WAIT_S` at
        most). None when there is none."""
        if self.recognizer is None and self.has_qwen():
            self.prepare_qwen()
            self._qwen_done.wait(_LOAD_WAIT_S)
        return self.recognizer

    def has_qwen(self) -> bool:
        """Qwen is here, or can be loaded (never beside a test model)."""
        if getattr(self, "trial", False):
            return False
        return self.recognizer is not None or bool(getattr(self, "_qwen_paths", None))

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

    def decode(self, samples: array, language: str) -> str:
        """`samples` read by the model that writes `language` (`wrote` names it after)."""
        if language == TURKISH and self.beam is not None:
            self._wrote.name = self.name(_TURKISH_ROLE)
            return str(self.beam.transcribe(samples)).strip()  # as sherpa-onnx's text
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
        recognizer = self.qwen()
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
    ) -> None:
        self.link = link
        self.model = model
        self.run = run
        self.url = url
        if getattr(model, "trial", False):
            language, guessed = TURKISH, False  # a test model: Turkish, as it is (no vote)
        self.language = language
        # 2026-11-07: a language guessed from the channel's title prefix ("TR: …"): the run
        # starts in it (no Qwen vote first), and its readings may still say otherwise
        # (`_check_guess`)
        self.guessed = guessed and bool(language)
        self.checks = text_language.Votes()
        if language:
            model.prepare(language)  # 2.9.3: its specialist loads while the sound comes
        if not (language == TURKISH and getattr(model, "moonshine", False)):
            prepare_qwen = getattr(model, "prepare_qwen", None)
            if callable(prepare_qwen):
                prepare_qwen()  # Qwen writes it, or tells its language: loading while sound comes
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
        self._line_level = line_level()
        # 2026-11-08, Speaker marks: the run's recent sound and its voiced windows (from once the
        # diarizer is loaded and on); 2026-11-09: the sound the diarizer is still to read, the
        # lines waiting for it to have told their speakers, and the last line's speaker
        self.recent: Optional[Recent] = None
        self._diary: Optional[queue.Queue[Optional[tuple[float, array]]]] = None
        self._waiting: Optional[queue.Queue[Optional[tuple]]] = None
        self._said_by: Optional[int] = None
        self._diary_token: Optional[int] = None  # the diarizer reads this run's sound (its reset)
        self._diarizer_broken = False
        self._gender: Optional[str] = None  # the last part's speaker's, when clear
        self._fresh = FRESH_FROM - 1
        self.votes = text_language.Votes()  # the language heard, when none is given
        # streaming Turkish: the sound goes to the model whole (no pieces), lines come back
        self.blocks: queue.Queue[Optional[tuple[float, array]]] = queue.Queue()
        self.writer: Optional[StreamWriter | GrowingWriter] = None
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

    def _logged(
        self, name: str, heard: str, shown: str, turns: Sequence[Optional[Turn]] = ()
    ) -> None:
        """A line in the log by the model that wrote it (2026-11-08, the owner: "Write to logs
        which one writes"), what it heard and what shows, and who says it, part by part (S1, S2,
        ...); the run's model changing (a language locked, a specialist loaded) is said once,
        with the run. At INFO only where test models are offered (`line_level`)."""
        level = getattr(self, "_line_level", None) or line_level()
        if name != getattr(self, "_writes", ""):
            self._writes = name
            logger.log(level, "AI captions: run %d: [%s] writes now", getattr(self, "run", 0), name)
        told = [turn.tag() for turn in turns if turn is not None]
        who = f" · {' | '.join(told)}" if told else ""
        logger.log(level, "AI captions: [%s] heard %r, shows %r%s", name, heard, shown, who)

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
        log. 2026-11-08, the owner: a line of more than one sentence, or where the speaker
        changes, goes in parts (speakers.parts_of: each with its sound's span, from `voiced`, the
        voice detector's windows from `start`, else the ones kept for the diarizer), each part's
        speaker told while Speaker marks are on; the captions show a long line a part at a time
        as each is said. 2026-11-09: with the diarizer, a line waits (in order) until it has told
        the line's speakers, or the picture is about to reach it (`_send_when_told`)."""
        line = (name, heard, shown, start, end, until, voiced)
        if getattr(self, "_waiting", None) is None:
            if self._diarizer() is None:
                self._send(line, None)
                return
            self._waiting = queue.Queue()  # (from now on every line goes through it, in order)
            threading.Thread(
                target=self._send_when_told,
                args=(self._waiting,),
                name="qitv-captions-speakers-out",
                daemon=True,
            ).start()
        assert self._waiting is not None
        self._waiting.put(line)

    def _diarizer(self) -> Any:
        """The diarizer while Speaker marks are on and it is loaded (and has not failed in this
        run), else None."""
        model = getattr(self, "model", None)
        if not getattr(model, "speakers_on", False) or getattr(self, "_diarizer_broken", False):
            return None
        return getattr(model, "diarizer", None)

    def _serving(self) -> Any:
        """The diarizer while it reads this run's sound, else None (not started for this run
        yet, or a newer run's already)."""
        diarizer = self._diarizer()
        token = getattr(self, "_diary_token", None)
        if diarizer is None or token is None or diarizer.token != token:
            return None
        return diarizer

    def _send_when_told(self, waiting: "queue.Queue[Optional[tuple]]") -> None:
        """The lines out in order, each once the diarizer has told its speakers, or once the
        picture is SEND_MARGIN_S from its first word (at once while Speaker marks are off)."""
        while not self.stopped.is_set():
            try:
                line = waiting.get(timeout=_IDLE_S)
            except queue.Empty:
                continue
            if line is None:
                return
            start, end = line[3], line[4]
            while not self.stopped.is_set() and self._diarizer() is not None:
                serving = self._serving()
                if serving is not None and serving.covered() >= end - 0.05:
                    break
                if start - float(self.viewer[0]) <= SEND_MARGIN_S:
                    break
                self.stopped.wait(0.05)
            if not self.stopped.is_set():
                self._send(line, self._serving())

    def _send(self, line: tuple, diarizer: Any) -> None:
        """A line in parts, each with its speaker as the diarizer told it (`diarizer` None: none
        told), to the captions and the log. 2026-11-09, the owner ("cut points especially male
        to female should be straightforward"): where a part begins another speaker's turn is
        where the cut layer hears a clear flip of the voice's gender (services.captions.cuts),
        apart from who they are."""
        from services.captions import sortformer

        name, heard, shown, start, end, until, voiced = line
        flags, at = (list(voiced), start) if voiced is not None else self._flags(start, end)
        changes = None
        if diarizer is not None:
            changes = sortformer.changes(diarizer.frames(start, end), diarizer.time_of(0, start))
        parts = parts_of(shown, start, end, flags, at, changes=changes)
        turns: list[Optional[Turn]] = [None] * len(parts)
        if diarizer is not None:
            told = []
            for part in parts:
                k = sortformer.speaker(diarizer.frames(part.start, part.end))
                told.append(None if k is None else k + 1)
            cut_before = [part.new is True for part in parts]
            for i, gender in enumerate(self._genders(parts)):
                if gender is not None and self._gender not in (None, gender):
                    cut_before[i] = True
                self._gender = gender or self._gender
            turns = told_parts(told, cut_before, self._said_by, self._fresh_speaker)
            self._said_by = next(
                (turn.speaker for turn in reversed(turns) if turn is not None), self._said_by
            )
        self._logged(name, heard, shown, turns)
        said = [
            (
                part.text,
                part.start,
                part.end,
                1 if turn is not None and turn.changed else 0,
                turn.speaker if turn is not None else 0,
            )
            for part, turn in zip(parts, turns)
        ]
        changed, speaker = said[0][3], said[0][4]
        message = ("caption", self.run, start, max(end, until), shown, changed, speaker)
        self.link.send(message + ((said,) if len(said) > 1 else ()))

    def _fresh_speaker(self) -> int:
        """A speaker number no one has: a cut whose speaker is unsure (a fresh colour)."""
        self._fresh = getattr(self, "_fresh", FRESH_FROM - 1) + 1
        return self._fresh

    def _genders(self, parts: Sequence[Any]) -> list[Optional[str]]:
        """Each part's speaker's gender when the voice's pitch says it clearly (None: unsure)."""
        pitch = getattr(getattr(self, "model", None), "pitch", None)
        recent = getattr(self, "recent", None)
        if pitch is None or recent is None:
            return [None] * len(parts)
        out: list[Optional[str]] = []
        for part in parts:
            try:
                out.append(pitch.gender(recent.voiced(part.start, part.end)))
            except Exception:
                out.append(None)
        return out

    def _flags(self, start: float, end: float) -> tuple[Optional[list[bool]], float]:
        """The voice detector's windows of [start, end) kept for the diarizer, if it is on."""
        recent = getattr(self, "recent", None)
        if recent is None:
            return None, start
        flags, at = recent.flags(start, end)
        return flags, at

    def stop(self) -> None:
        self.stopped.set()
        self.pieces.put(None)
        self.blocks.put(None)
        self._wake()
        self.decoder.stop()

    def _wake(self) -> None:
        """The diarizer's threads end with the run."""
        for waiting in (getattr(self, "_diary", None), getattr(self, "_waiting", None)):
            if waiting is not None:
                waiting.put(None)

    def _maybe_stream(self) -> None:
        """Turkish with the streaming model here: from now on the sound goes to it whole; with
        the Moonshine trial, to the growing writer (utterances read whole, no forced cuts)."""
        if getattr(self, "writer", None) is not None or self.language != TURKISH:
            return
        recognizer = getattr(self.model, "turkish_stream", None)
        if recognizer is not None:
            self.writer = StreamWriter(recognizer, self.viewer)  # lines out before the picture
        elif getattr(self.model, "moonshine", False):
            self.writer = GrowingWriter(
                self._moon_reading,
                self.model.is_speech,
                self.model.is_voiced,
                self.viewer,
                keep=self._keep_piece,
            )
        else:
            return
        threading.Thread(target=self._stream, name="qitv-captions-stream", daemon=True).start()

    def _moon_reading(self, samples: array) -> str:
        """The Moonshine trial's reading of a whole stretch of sound, what nobody said left out."""
        text = guards.clean(self.model.decode(samples, TURKISH))
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
        self.writer = None  # pieces again (`_take`); the streaming thread ends
        prepare_qwen = getattr(self.model, "prepare_qwen", None)
        if callable(prepare_qwen):
            prepare_qwen()

    @staticmethod
    def _keep_piece(start: float, end: float, samples: array, text: str) -> None:
        """The trial's field test set (SAVE_ENV): each utterance read whole, kept to label."""
        folder = os.environ.get(SAVE_ENV)
        if folder:
            keep_piece(folder, start, end, samples, text)

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
        self._hold_for_speakers(at, floats)
        if getattr(self, "writer", None) is not None:
            self.blocks.put((at, floats))
            return
        self.cutter.set_max(self._longest())
        for piece in self.cutter.push(at, floats):
            self.pieces.put(piece)

    def _hold_for_speakers(self, at: float, floats: array) -> None:
        """The sound kept while Speaker marks are on (a line finds its voiced windows there), and
        given to the diarizer, which reads it in its own thread (`_diarize`)."""
        diarizer = self._diarizer()
        if diarizer is None:
            return
        model = self.model
        if getattr(self, "recent", None) is None:
            self.recent = Recent(model.speaker_voiced)
        assert self.recent is not None
        self.recent.push(at, floats)
        if self._diary is None:
            self._diary = queue.Queue()
            threading.Thread(
                target=self._diarize,
                args=(self._diary, diarizer, at),
                name="qitv-captions-diarizer",
                daemon=True,
            ).start()
        self._diary.put((at, floats))

    def _diarize(
        self, diary: "queue.Queue[Optional[tuple[float, array]]]", diarizer: Any, at: float
    ) -> None:
        """The run's sound to the diarizer, from timeline second `at` on, a block at a time (a
        failure: the run's lines go out untold, at once)."""
        try:
            self._diary_token = diarizer.reset(at)
            while not self.stopped.is_set():
                try:
                    block = diary.get(timeout=_IDLE_S)
                except queue.Empty:
                    continue
                if block is None:
                    return
                diarizer.push(*block, token=self._diary_token)
        except Exception as error:
            self._diarizer_broken = True
            logger.warning("AI captions: the diarizer failed: %s", type(error).__name__)

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
                text = self.model.decode(piece.samples, self.language)
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
        """The streaming model's thread (or the growing writer's): placed sound in, whole lines
        out (no pieces, no cuts). The sound that came while the model was busy goes in at once:
        the growing writer's next reading has all of it. Both look at the picture when no sound
        comes for a moment: a line it is about to reach goes out then (the sound comes in
        bursts, a segment at a time)."""
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
                logger.warning("AI captions: the streaming model failed: %s", type(error).__name__)
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
        if getattr(self.model, "trial", False):
            return False  # a test model writes it: no smaller Qwen helps
        if not language:
            return True
        if language == TURKISH and (
            getattr(self.model, "turkish", None) is not None
            or getattr(self.model, "turkish_stream", None) is not None
        ):
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
            self._maybe_stream()  # Turkish heard and the streaming model here: it takes over
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
        self._wake()
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
            _, number, url, segment_start, audio_track, language = message[:6]
            guessed = bool(message[6]) if len(message) > 6 else False
            if run is not None:
                run.stop()
                run = None
            if model is None:
                link.send(("ended", number, "no model"))
                continue
            viewer[0] = float(segment_start)
            run = _Run(
                link, model, number, url, segment_start, audio_track, language, viewer, guessed
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
        elif kind == "speakers":  # 2026-11-08: Speaker marks on (the diarizer's folder) or off
            if model is not None:
                model.use_speakers(*message[1:3])
        elif kind == "language":
            if model is not None and model.trial:
                continue  # a test model's runs stay Turkish
            language = message[1]
            if model is not None and not (language == TURKISH and model.moonshine):
                model.prepare_qwen()  # Qwen writes the new language: it loads, if it was left
            if run is not None:
                run.language = language
                run._maybe_stream()
        elif kind == "stop":
            if run is not None:
                run.stop()
                run = None
    if run is not None:
        run.stop()
