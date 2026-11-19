"""An LSTM language model over our Turkish Moonshine's pieces for services.captions.moon_beam's
shallow fusion, read through onnxruntime's C API (services.captions.onnx_c; the standard library
only, no numpy), behind the interface moon_beam.search asks of an LM: start() and
score(state, token) -> (log p(token | state) in nats, the state after it).

The model: icefall's rnn_lm (2 x 1024 LSTM, tied embeddings) over Moonshine's 10,623 pieces, its
start (1) and end (2) ids as SOS and EOS, exported with its state (icefall's export_with_state:
inputs x [1, 1] int64, h0 and c0 [layers, 1, hidden]; outputs log_softmax [1, vocabulary],
next_h0, next_c0), fp32, int8 or u8mm (its matmuls unsigned 8-bit, the LSTM in fp32).

A state is a node: the LM's h and c after its tokens and the log-probabilities of every next
token. A node that score() hands out is lazy: the LM runs on its token only when the node is asked
for a score itself, which in the beam happens only when its hypothesis goes on and is expanded, so
a hypothesis's 8 candidates cost one LM call, not eight. The values stay onnxruntime's (h and c go
straight back in); only the row of log-probabilities is copied out.

DecoderIlm is the decoder's own language habit, for internal-LM subtraction (ILME): the same
Moonshine decoder run on a zeroed encoder output, a step per expanded hypothesis like the decoder
itself. Fused puts the two together: score = log p_LSTM - ratio * log p_ILM (moon_beam multiplies
it by λ, so ratio = μ / λ).

Measured on the GPU box the night of 2026-11-18 (~/qitv-lm-moon); in QiTV only in the dev build's
Turkish reading switch (services.captions.moon_reading)."""

from __future__ import annotations

from array import array
from math import exp, inf, log
import os
from typing import Any, Optional, Union

from services.captions import onnx_c
from services.captions.moon_ort import BOS, MoonshineOrt, Steps


class _Node:
    """The LM after a hypothesis's tokens: `row` (log-probabilities of every next token) and
    `h`, `c` once computed; until then the parent node and the token after it."""

    __slots__ = ("parent", "token", "h", "c", "row", "path")

    def __init__(
        self, parent: Optional[_Node], token: int, path: Optional[tuple[int, ...]] = None
    ) -> None:
        self.parent = parent
        self.token = token
        self.h: Any = None
        self.c: Any = None
        self.row: Optional[array] = None
        self.path = path


class LstmLM:
    """An exported icefall RNN LM (one session, `threads` intra-op threads). `record` keeps every
    score it gave by the tokens before it (checks only)."""

    def __init__(
        self,
        path: Union[str, os.PathLike[str]],
        api: onnx_c.Api,
        threads: int = 1,
        sos: int = BOS,
        record: bool = False,
    ) -> None:
        self._api = api
        self._session = onnx_c.Session(api, path, threads)
        names = self._session.inputs
        if sorted(names) != ["c0", "h0", "x"]:
            raise ValueError(f"not an RNN LM with state: {names}")
        self._at = {name: names.index(name) for name in names}
        self._out = self._session.select(["log_softmax", "next_h0", "next_c0"])
        shape, _ = self._session.input_shape(self._at["h0"])
        layers, hidden = shape[0], shape[2]
        if layers <= 0 or hidden <= 0:
            raise ValueError(f"h0 shape {shape}")
        self._x = array("q", [sos])
        self._x_tensor = api.tensor(self._x, (1, 1), onnx_c.INT64)
        self._zeros = array("f", bytes(4 * layers * hidden))
        self._zero_tensor = api.tensor(self._zeros, (layers, 1, hidden), onnx_c.FLOAT)
        self.sos = sos
        self.calls = 0
        self.record = record
        self.seen: dict[tuple[int, ...], float] = {}
        root = _Node(None, sos, () if record else None)
        root.h = root.c = self._zero_tensor
        self._start = _Node(root, sos, () if record else None)
        self._ensure(self._start)  # the start row is the same for every reading: computed once

    def _ensure(self, node: _Node) -> array:
        row = node.row
        if row is None:
            parent = node.parent
            assert parent is not None and parent.h is not None
            self._x[0] = node.token
            feed: list[Any] = [None, None, None]
            feed[self._at["x"]] = self._x_tensor
            feed[self._at["h0"]] = parent.h
            feed[self._at["c0"]] = parent.c
            logprobs, h, c = self._session.run(feed, self._out)
            try:
                row = logprobs.floats()
            finally:
                logprobs.release()
            node.row, node.h, node.c = row, h, c
            node.parent = None  # its h and c are all it needs now
            self.calls += 1
        return row

    def start(self) -> _Node:
        return self._start

    def score(self, state: _Node, token: int) -> tuple[float, _Node]:
        row = self._ensure(state)
        value = row[token]
        path = None
        if self.record and state.path is not None:
            self.seen[state.path + (token,)] = value
            path = state.path + (token,)
        return value, _Node(state, token, path)

    def close(self) -> None:
        self._session.close()


def _log_softmax(row: array) -> array:
    top = max(row)
    try:
        total = sum(map(exp, row))
    except OverflowError:
        total = inf
    norm = log(total) if 0.0 < total < inf else top + log(sum(exp(v - top) for v in row))
    return array("f", (v - norm for v in row))


class _IlmNode:
    __slots__ = ("owner", "parent", "token", "state", "row", "__weakref__")

    def __init__(self, owner: DecoderIlm, parent: Optional[_IlmNode], token: int) -> None:
        self.owner = owner
        self.parent = parent
        self.token = token
        self.state: Any = None
        self.row: Optional[array] = None

    def __del__(self) -> None:
        if self.state is not None and self.owner.steps is not None:
            try:
                self.owner.steps.release(self.state)
            except Exception:
                pass


class DecoderIlm:
    """The Moonshine decoder on a zeroed encoder output of `frames` frames: its own next-token
    log-probabilities after a hypothesis's tokens (one decoder step per expanded hypothesis). One
    per reading; close() releases it all."""

    def __init__(self, model: MoonshineOrt, frames: int, width: int) -> None:
        self._zeros = array("f", bytes(4 * frames * width))
        self._hidden = model._api.tensor(self._zeros, (1, frames, width), onnx_c.FLOAT)
        self.steps: Optional[Steps] = Steps(model, self._hidden)
        self.calls = 0
        self._start = _IlmNode(self, None, BOS)
        self._start.state = self.steps.start
        self._start.row = _log_softmax(self.steps.first)

    def _ensure(self, node: _IlmNode) -> array:
        if node.row is None:
            parent = node.parent
            assert parent is not None and parent.state is not None and self.steps is not None
            node.state, logits = self.steps.step(parent.state, node.token)
            node.row = _log_softmax(logits)
            node.parent = None
            self.calls += 1
        return node.row

    def start(self) -> _IlmNode:
        return self._start

    def score(self, state: _IlmNode, token: int) -> tuple[float, _IlmNode]:
        return self._ensure(state)[token], _IlmNode(self, state, token)

    def close(self) -> None:
        steps, self.steps = self.steps, None
        if steps is not None:
            steps.close()
        self._hidden.release()


class Fused:
    """log p_LSTM - ratio * log p_ILM per token (moon_beam multiplies it by λ: ratio = μ / λ)."""

    def __init__(self, lm: LstmLM, ilm: DecoderIlm, ratio: float) -> None:
        self.lm, self.ilm, self.ratio = lm, ilm, ratio

    def start(self) -> tuple[Any, Any]:
        return self.lm.start(), self.ilm.start()

    def score(self, state: tuple[Any, Any], token: int) -> tuple[float, tuple[Any, Any]]:
        a, sa = self.lm.score(state[0], token)
        b, sb = self.ilm.score(state[1], token)
        return a - self.ratio * b, (sa, sb)
