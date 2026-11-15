"""Our Turkish Moonshine (sherpa-onnx's Moonshine v2 files: encoder_model, decoder_model_merged,
tokens.txt) run through onnxruntime's C API (services.captions.onnx_c) with our own greedy
decoding, so a caller can force the start of the text (a prefix fed to the decoder at once, in one
call: far cheaper than stepping through it) and gets each token's score. With no prefix the tokens
are sherpa-onnx's own (`OfflineRecognizer.from_moonshine_v2`): the same files, settings, cap on
tokens and silence rule; only a loop (the same few tokens over and over) stops it sooner.

A prefix fed at once is not quite the same arithmetic as stepping through it: the q8 decoder
quantizes its activations per call (DynamicQuantizeLinear, one scale for all the rows in a call),
so a row's logits depend a little on the rows beside it (measured with onnxruntime itself: the
same position's log-probabilities differ by up to ~0.9, and greedy picks the same token at 99% of
positions). A forced decode can so part from the free decode's words a few tokens on;
`batched=False` steps through the prefix instead (the free decode's numbers exactly, ~9 ms a
token on two cores).

The merged decoder: the first call takes [BOS] + prefix with empty past keys and values and
use_cache_branch false, and gives the cross-attention keys and values once (present.*.encoder.*);
each later call takes one token, the previous call's present decoder keys and values and the first
call's encoder ones, with use_cache_branch true (its own present.*.encoder.* are placeholders).
They stay onnxruntime's values throughout: nothing is copied into Python but one row of logits.
`steps` hands the same calls out a hypothesis at a time (each with its own keys and values, one
call each: the arithmetic of the greedy decode's steps) for services.captions.moon_beam's beam.
"""

from __future__ import annotations

from array import array
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import logging
from math import exp, inf, log
import os
from pathlib import Path
import struct
import threading
from typing import Iterable, Iterator, Optional, Sequence, Union

from services.captions import onnx_c

logger = logging.getLogger(__name__)

BOS = 1  # the decoder's start
EOS = 2
CONTROL_MAX = 3  # ids 0..3 are control tokens, never text
FRAME = 80  # samples per frontend frame (5 ms at 16 kHz): a piece is a whole number of them
RATE = 16000
LOOP_MAX_N = 8  # a loop: the tail repeats an n-gram of 1..8 tokens,
LOOP_REPEATS_SHORT = 4  # at least 4 times for n = 1 or 2,
LOOP_REPEATS = 3  # at least 3 times for longer ones
# sherpa-onnx's silence rule: a piece whose every sample is within this (float32 1e-6) is nothing
_SILENT = struct.unpack("f", struct.pack("f", 1e-6))[0]

_ENCODER_IN = "input_values"
_ENCODER_OUT = "last_hidden_state"
_IDS = "input_ids"
_HIDDEN = "encoder_hidden_states"
_CACHE = "use_cache_branch"
_PAST = "past_key_values."
_PRESENT = "present."
_LOGITS = "logits"


def token_cap(frames: int) -> int:
    """sherpa-onnx's cap on a piece's tokens (offline-moonshine-v2-greedy-search-decoder.cc):
    frames * 384 / 16000 * 15, 18 a second for 20 ms frames."""
    return int(frames * 384 / 16000.0 * 15)


def encoder_frames(samples: int) -> int:
    """The encoder's frames (20 ms) for a padded piece of `samples` (a multiple of 80)."""
    if samples < FRAME:
        return 0
    return ((samples // FRAME - 1) // 2 + 1 - 1) // 2 + 1


def padded(samples: Union[array, Sequence[float]]) -> array:
    """The samples as float32, with zeros to a whole number of frontend frames (`samples`
    itself when it already is one)."""
    if isinstance(samples, array) and samples.typecode == "f" and not len(samples) % FRAME:
        return samples
    sound = array("f", samples)
    sound.frombytes(bytes(4 * (-len(sound) % FRAME)))
    return sound


def silent(sound: array) -> bool:
    """sherpa-onnx's silence (or nothing at all), looked at a quarter second at a time: sound
    is told apart within its first one."""
    for start in range(0, len(sound), RATE // 4):
        part = sound[start : start + RATE // 4]
        if max(part) > _SILENT or min(part) < -_SILENT:
            return False
    return True


def loop_tail(tokens: Sequence[int]) -> int:
    """The size n of the n-gram the end of `tokens` repeats like a loop (see LOOP_*), else 0."""
    size = len(tokens)
    for n in range(1, LOOP_MAX_N + 1):
        repeats = LOOP_REPEATS_SHORT if n <= 2 else LOOP_REPEATS
        if size < n * repeats:
            break  # n * repeats only grows with n
        tail = tokens[size - n :]
        if all(tokens[size - n * k : size - n * (k - 1)] == tail for k in range(2, repeats + 1)):
            return n
    return 0


class Pieces:
    """tokens.txt: a line per id, "<base64 of the piece's bytes> <id>"; text is the pieces'
    bytes joined in order, as UTF-8 (a piece starting with a space starts a word)."""

    def __init__(self, table: Sequence[bytes]) -> None:
        self._table = list(table)

    @classmethod
    def read(cls, path: Union[str, os.PathLike[str]]) -> Pieces:
        pieces: dict[int, bytes] = {}
        with open(path, encoding="utf-8") as lines:
            for number, line in enumerate(lines, 1):
                parts = line.split()
                if not parts:
                    continue
                if len(parts) > 2:
                    raise ValueError(f"{path}:{number}: not '<base64> <id>'")
                coded, index = ("", parts[0]) if len(parts) == 1 else parts
                pieces[int(index)] = base64.b64decode(coded, validate=True)
        table = [b""] * (max(pieces) + 1 if pieces else 0)
        for token, piece in pieces.items():
            table[token] = piece
        return cls(table)

    def __len__(self) -> int:
        return len(self._table)

    def piece(self, token: int) -> bytes:
        """A token's bytes (nothing for a control token or an unknown id)."""
        if CONTROL_MAX < token < len(self._table):
            return self._table[token]
        return b""

    def text(self, tokens: Iterable[int]) -> str:
        """The tokens' text as sherpa-onnx writes it: invalid UTF-8 and leading spaces dropped
        (control tokens left out)."""
        joined = b"".join(self.piece(token) for token in tokens)
        return joined.decode("utf-8", "ignore").lstrip(" ")

    def words(self, tokens: Iterable[int]) -> list[tuple[str, list[int]]]:
        """Each word (stripped) with its token ids: a word starts at a token whose bytes begin
        with a space, or at the first one; control tokens are left out."""
        groups: list[tuple[bytearray, list[int]]] = []
        for token in tokens:
            piece = self.piece(token)
            if not piece:
                continue
            if not groups or piece.startswith(b" "):
                groups.append((bytearray(), []))
            groups[-1][0].extend(piece)
            groups[-1][1].append(token)
        return [(bytes(raw).decode("utf-8", "ignore").strip(), ids) for raw, ids in groups]


@dataclass
class Encoded:
    """A piece through the encoder: `value` stays onnxruntime's (None for silence)."""

    value: Optional[onnx_c.Value]
    frames: int
    samples: int  # padded
    silent: bool = False

    def hidden(self) -> array:
        """The encoder's output, flat [frames, width] (a copy; for checks)."""
        return self.value.floats() if self.value is not None else array("f")

    def release(self) -> None:
        if self.value is not None:
            self.value.release()
            self.value = None


@dataclass
class Decoded:
    tokens: list[int] = field(default_factory=list)  # the new ids (the prefix not repeated)
    logprobs: list[float] = field(default_factory=list)  # per new token: its log-softmax
    looped: bool = False  # stopped at a loop (its repeats are kept in `tokens`)
    eos: bool = False  # the model ended it (else: the cap, or the loop)


class MoonshineOrt:
    """The model in one folder, `threads` intra-op threads per session. One decode at a time
    (a lock keeps a second thread waiting); no Qt."""

    def __init__(
        self,
        folder: Union[str, os.PathLike[str]],
        threads: int = 2,
        library: Union[str, os.PathLike[str], None] = None,
    ) -> None:
        folder = Path(folder)
        self._lock = threading.Lock()
        self._api = onnx_c.Api(onnx_c.find_library(library))
        self.pieces = Pieces.read(folder / "tokens.txt")
        self._encoder = onnx_c.Session(self._api, _model(folder, "encoder_model"), threads)
        self._decoder = onnx_c.Session(self._api, _model(folder, "decoder_model_merged"), threads)
        if self._encoder.inputs != [_ENCODER_IN] or _ENCODER_OUT not in self._encoder.outputs:
            raise ValueError(f"not a Moonshine v2 encoder: {self._encoder.inputs}")
        self._encoder_out = self._encoder.select([_ENCODER_OUT])
        inputs = self._decoder.inputs
        past = [name for name in inputs if name.startswith(_PAST)]
        unknown = set(inputs) - set(past) - {_IDS, _HIDDEN, _CACHE}
        if unknown or len(past) + 3 != len(inputs) or not past:
            raise ValueError(f"not a Moonshine v2 merged decoder: {inputs}")
        outputs = self._decoder.outputs
        present = {name: _PRESENT + name[len(_PAST) :] for name in past}
        if _LOGITS not in outputs or any(name not in outputs for name in present.values()):
            raise ValueError(f"not a Moonshine v2 merged decoder: {outputs}")
        self._ids_at = inputs.index(_IDS)
        self._hidden_at = inputs.index(_HIDDEN)
        self._cache_at = inputs.index(_CACHE)
        self._past_at = [inputs.index(name) for name in past]
        self._self_past = [inputs.index(n) for n in past if ".decoder." in n]
        self._cross_past = [inputs.index(n) for n in past if ".decoder." not in n]
        # The first call: logits, then every present, in the past inputs' order.
        self._first_out = self._decoder.select([_LOGITS] + [present[n] for n in past])
        # Later calls: logits and the decoder's own presents, in self_past's order.
        self._step_out = self._decoder.select(
            [_LOGITS] + [present[inputs[i]] for i in self._self_past]
        )
        # Empty past keys and values, [1, heads, 0, head size]: one tensor per shape.
        self._empty: dict[int, onnx_c.Value] = {}
        shapes: dict[tuple[int, ...], onnx_c.Value] = {}
        for index in self._past_at:
            shape, _ = self._decoder.input_shape(index)
            if len(shape) != 4 or shape[1] <= 0 or shape[3] <= 0:
                raise ValueError(f"{inputs[index]}: shape {shape}")
            empty = (1, shape[1], 0, shape[3])
            if empty not in shapes:
                shapes[empty] = self._api.tensor(array("f"), empty, onnx_c.FLOAT)
            self._empty[index] = shapes[empty]
        self._false = self._api.tensor(bytearray(b"\x00"), (1,), onnx_c.BOOL)
        self._true = self._api.tensor(bytearray(b"\x01"), (1,), onnx_c.BOOL)
        self._step_id = array("q", [0])  # the next token: written before each step's Run
        self._step_ids = self._api.tensor(self._step_id, (1, 1), onnx_c.INT64)
        self.vocabulary = len(self.pieces)
        logger.info(
            "Turkish Moonshine through onnxruntime %s's C API (%d threads)",
            self._api.version,
            threads,
        )

    # -- the model ----------------------------------------------------------------------------

    def encode(self, samples: Union[array, Sequence[float]]) -> Encoded:
        """A piece of 16 kHz mono sound through the encoder, padded with zeros to a whole number
        of frontend frames."""
        sound = padded(samples)
        count = len(sound)
        if silent(sound):
            return Encoded(None, encoder_frames(count), count, silent=True)  # sherpa: no text
        with self._lock:
            tensor = self._api.tensor(sound, (1, count), onnx_c.FLOAT)
            try:
                (hidden,) = self._encoder.run([tensor], self._encoder_out)
            finally:
                tensor.release()
        shape = hidden.shape
        if len(shape) != 3 or shape[0] != 1:
            hidden.release()
            raise ValueError(f"the encoder gave {shape}")
        return Encoded(hidden, shape[1], count)

    def decode(
        self,
        encoded: Encoded,
        prefix: Sequence[int] = (),
        max_new_tokens: Optional[int] = None,
        batched: bool = True,
    ) -> Decoded:
        """Greedy decoding after [BOS] + `prefix` (fed in the first call, or with `batched`
        False stepped through one token a call): new tokens until EOS, the cap or a loop (counted
        over prefix and new tokens alike, so a stepped prefix taken from a free decode continues
        exactly as that decode did). The cap by default: sherpa-onnx's for the piece's length,
        less the prefix (pass one when the prefix is not this piece's own text)."""
        if encoded.silent:
            return Decoded(eos=True)
        if encoded.value is None:
            raise ValueError("the encoded piece was released")
        cap = token_cap(encoded.frames) - len(prefix) if max_new_tokens is None else max_new_tokens
        if cap <= 0:
            return Decoded()
        start = array("q", [BOS])
        for token in prefix:
            if not 0 <= token < self.vocabulary:
                raise ValueError(f"token {token} outside 0..{self.vocabulary - 1}")
            start.append(token)
        with self._lock:
            return self._decode(encoded.value, start, cap, len(start) if batched else 1)

    def _decode(self, hidden: onnx_c.Value, start: array, cap: int, first: int) -> Decoded:
        """The first call takes start[:first]; the rest of start is stepped through."""
        decoder = self._decoder
        ids = self._api.tensor(start[:first], (1, first), onnx_c.INT64)
        feed: list[onnx_c.Value] = [self._false] * len(decoder.inputs)
        feed[self._ids_at] = ids
        feed[self._hidden_at] = hidden
        for index, empty in self._empty.items():
            feed[index] = empty
        try:
            outputs = decoder.run(feed, self._first_out)
        finally:
            ids.release()
        logits, presents = outputs[0], dict(zip(self._past_at, outputs[1:]))
        cross = [presents.pop(index) for index in self._cross_past]  # kept to the end
        own = [presents.pop(index) for index in self._self_past]
        feed[self._ids_at] = self._step_ids
        feed[self._cache_at] = self._true
        for index, value in zip(self._cross_past, cross):
            feed[index] = value
        result = Decoded()
        history = list(start[1:])
        forced = len(start)
        try:
            rows = first
            while True:
                if first < forced:
                    token = start[first]
                    first += 1
                    logits.release()
                else:
                    token, score = self._pick(logits, rows)
                    logits.release()
                    if token == EOS:
                        result.eos = True
                        break
                    result.tokens.append(token)
                    result.logprobs.append(score)
                    history.append(token)
                    if loop_tail(history):
                        result.looped = True
                        break
                    if len(result.tokens) >= cap:
                        break
                self._step_id[0] = token
                for index, value in zip(self._self_past, own):
                    feed[index] = value
                outputs = decoder.run(feed, self._step_out)
                for value in own:
                    value.release()
                logits, own, rows = outputs[0], outputs[1:], 1
        finally:
            logits.release()
            for value in own + cross:
                value.release()
            for value in presents.values():
                value.release()
        return result

    @contextmanager
    def steps(self, encoded: Encoded) -> Iterator[Steps]:
        """One decode a hypothesis at a time (services.captions.moon_beam's beam search): the
        first call's [BOS] once, then any hypothesis's next token after its own keys and values.
        The model is held for the decode (the lock); what it gave is released at the end."""
        if encoded.value is None:
            raise ValueError("no sound to decode (silence, or the piece was released)")
        with self._lock:
            steps = Steps(self, encoded.value)
            try:
                yield steps
            finally:
                steps.close()

    def _row(self, logits: onnx_c.Value) -> array:
        """The last row of a call's logits (a copy)."""
        shape = logits.shape
        width = shape[-1]
        if width != self.vocabulary or len(shape) != 3:
            raise ValueError(f"logits {shape} for a vocabulary of {self.vocabulary}")
        return logits.floats((shape[-2] - 1) * width, width)

    def _pick(self, logits: onnx_c.Value, rows: int) -> tuple[int, float]:
        """The last row's best token (the first of equals, as sherpa-onnx's max_element) and its
        log-softmax."""
        shape = logits.shape
        width = shape[-1]
        if width != self.vocabulary or shape[-2] != rows:
            raise ValueError(f"logits {shape} for {rows} rows of {self.vocabulary}")
        row = logits.floats((rows - 1) * width, width)
        top = max(row)
        try:
            total = sum(map(exp, row))  # in doubles: logits are far from exp's limits (±709)
        except OverflowError:
            total = inf
        if 0.0 < total < inf:
            return row.index(top), top - log(total)
        total = sum(exp(value - top) for value in row)
        return row.index(top), -log(total)

    # -- text -----------------------------------------------------------------------------------

    def text(self, tokens: Iterable[int]) -> str:
        """The tokens' text as sherpa-onnx writes it."""
        return self.pieces.text(tokens)

    def words(self, tokens: Iterable[int]) -> list[tuple[str, list[int]]]:
        """Each word (stripped) with its token ids."""
        return self.pieces.words(tokens)

    def transcribe(self, samples: Union[array, Sequence[float]]) -> str:
        """A piece's text with no prefix: what sherpa-onnx's whole-piece decode writes."""
        encoded = self.encode(samples)
        try:
            return self.text(self.decode(encoded).tokens)
        finally:
            encoded.release()


class Steps:
    """A decode's calls, one hypothesis at a time (`MoonshineOrt.steps`): `first` is the logits
    after [BOS] and `start` its keys and values; `step(state, token)` gives a hypothesis's next
    state and logits. A state is the decoder's own keys and values after a hypothesis's tokens:
    several hypotheses may step from one (it is only read), and the caller releases each once no
    hypothesis steps from it any more (`release`; those left are released at the end)."""

    def __init__(self, model: MoonshineOrt, hidden: onnx_c.Value) -> None:
        self._model = model
        self._states: dict[int, list[onnx_c.Value]] = {}
        self._made = 0
        self._cross: list[onnx_c.Value] = []
        decoder = model._decoder
        api = model._api
        self._feed: list[onnx_c.Value] = [model._false] * len(decoder.inputs)
        feed = self._feed
        ids = api.tensor(array("q", [BOS]), (1, 1), onnx_c.INT64)
        feed[model._ids_at] = ids
        feed[model._hidden_at] = hidden
        for index, empty in model._empty.items():
            feed[index] = empty
        try:
            outputs = decoder.run(feed, model._first_out)
        finally:
            ids.release()
        logits, presents = outputs[0], dict(zip(model._past_at, outputs[1:]))
        self._cross = [presents.pop(index) for index in model._cross_past]
        self.start = self._keep([presents.pop(index) for index in model._self_past])
        try:
            self.first = model._row(logits)
        except BaseException:
            self.close()
            raise
        finally:
            logits.release()
        feed[model._ids_at] = model._step_ids
        feed[model._cache_at] = model._true
        for index, value in zip(model._cross_past, self._cross):
            feed[index] = value

    def _keep(self, values: list[onnx_c.Value]) -> int:
        self._made += 1
        self._states[self._made] = values
        return self._made

    def step(self, state: int, token: int) -> tuple[int, array]:
        """A hypothesis whose keys and values are `state`, `token` next: its new state and the
        logits after it."""
        model = self._model
        own = self._states[state]
        model._step_id[0] = token
        for index, value in zip(model._self_past, own):
            self._feed[index] = value
        outputs = model._decoder.run(self._feed, model._step_out)
        logits = outputs[0]
        try:
            row = model._row(logits)
        except BaseException:
            for value in outputs[1:]:
                value.release()
            raise
        finally:
            logits.release()
        return self._keep(list(outputs[1:])), row

    def release(self, state: int) -> None:
        for value in self._states.pop(state, ()):
            value.release()

    def close(self) -> None:
        for state in list(self._states):
            self.release(state)
        for value in self._cross:
            value.release()
        self._cross = []


def _model(folder: Path, stem: str) -> Path:
    quantized = folder / f"{stem}_quantized.onnx"
    return quantized if quantized.exists() else folder / f"{stem}.onnx"
