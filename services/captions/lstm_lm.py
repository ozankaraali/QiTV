"""2.12.1: the Turkish model's LSTM language model (models.TURKISH_LM) for services.captions.
moon_beam's shallow fusion (services.captions.moon_reading), read through onnxruntime's C API
(services.captions.onnx_c; the standard library only, no numpy), behind the interface
moon_beam.search asks of an LM: start() and score(state, token) -> (log p(token | state) in
nats, the state after it).

The model: icefall's rnn_lm (2 x 1024 LSTM, tied embeddings, 27.7 M parameters) over our Turkish
Moonshine's 10,623 pieces, its start (1) as SOS, trained on general Turkish text and then on TV
transcripts, exported with its state (inputs x [1, 1] int64, h0 and c0 [layers, 1, hidden];
outputs log_softmax [1, vocabulary], next_h0, next_c0) and its weights quantized to 8 bits.

A state is a node: the LM after a hypothesis's tokens. A node that score() hands out is lazy:
the LM runs on its token only when the node is asked for a score itself, which in the beam happens
only when its hypothesis goes on and is expanded, so a hypothesis's 8 candidates cost one LM call,
not eight. The values stay onnxruntime's (h and c go straight back in); only the row of
log-probabilities is copied out.

What a run gave (that row, h and c) is kept by the tokens it came after, CACHE_STATES of them,
the least recently used going first: the captions read a growing utterance again and again (the
growing writer, a piece read again after a resync), and each reading follows the last one's
tokens a long way. The same tokens always give the same state, so a kept one is exactly what a
run would give.

One reading at a time: moon_reading reads within the model's lock (moon_ort.MoonshineOrt.steps),
and a node's input buffer is shared. No Qt."""

from __future__ import annotations

from array import array
from collections import OrderedDict
import os
from typing import Any, Optional, Union

from services.captions import onnx_c
from services.captions.moon_ort import BOS

# A run's row (10,623 floats), h and c (2 x 1024 floats each) come to about 60 KB: 30 MB in all,
# about three readings' expanded hypotheses
CACHE_STATES = 512


class _Node:
    """The LM after a hypothesis's tokens (`path`, from the start): `row` (log-probabilities of
    every next token), `h` and `c` once computed; until then the parent node and its token."""

    __slots__ = ("parent", "token", "path", "h", "c", "row")

    def __init__(self, parent: Optional[_Node], token: int, path: tuple[int, ...]) -> None:
        self.parent = parent
        self.token = token
        self.path = path
        self.h: Any = None
        self.c: Any = None
        self.row: Optional[array] = None


class LstmLM:
    """An exported icefall RNN LM with its state (one session, `threads` intra-op threads),
    keeping `cache` states by their tokens (0: none)."""

    def __init__(
        self,
        path: Union[str, os.PathLike[str]],
        api: onnx_c.Api,
        threads: int = 1,
        sos: int = BOS,
        cache: int = CACHE_STATES,
    ) -> None:
        self._api = api
        self._session = onnx_c.Session(api, path, threads)
        names = self._session.inputs
        if sorted(names) != ["c0", "h0", "x"]:
            raise ValueError(f"not an RNN LM with state: {names}")
        self._at = {name: names.index(name) for name in names}
        self._out = self._session.select(["log_softmax", "next_h0", "next_c0"])
        shape, _ = self._session.input_shape(self._at["h0"])
        if len(shape) != 3 or shape[0] <= 0 or shape[2] <= 0:
            raise ValueError(f"h0 shape {shape}")
        layers, hidden = shape[0], shape[2]
        self._x = array("q", [sos])
        self._x_tensor = api.tensor(self._x, (1, 1), onnx_c.INT64)
        self._zeros = array("f", bytes(4 * layers * hidden))
        self._zero_tensor = api.tensor(self._zeros, (layers, 1, hidden), onnx_c.FLOAT)
        self.sos = sos
        self.calls = 0
        self._limit = max(0, int(cache))
        self._kept: OrderedDict[tuple[int, ...], tuple[array, Any, Any]] = OrderedDict()
        root = _Node(None, sos, ())
        root.h = root.c = self._zero_tensor
        self._start = _Node(root, sos, ())
        self._ensure(self._start)  # the start's row is the same for every reading: run once

    def _ensure(self, node: _Node) -> array:
        row = node.row
        if row is not None:
            return row
        path = node.path
        kept = self._kept.get(path)
        if kept is not None:
            self._kept.move_to_end(path)
            node.row, node.h, node.c = kept
            node.parent = None
            return kept[0]
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
        if self._limit and path:
            self._kept[path] = (row, h, c)
            if len(self._kept) > self._limit:
                self._kept.popitem(last=False)
        return row

    def start(self) -> _Node:
        return self._start

    def score(self, state: _Node, token: int) -> tuple[float, _Node]:
        """log p(token | the state's tokens), in nats, and the state after it (not run yet)."""
        row = self._ensure(state)
        return row[token], _Node(state, token, state.path + (token,))

    def close(self) -> None:
        """Its states let go, then its session (their h and c came out of it)."""
        self._kept.clear()
        self._start.h = self._start.c = None
        self._session.close()
