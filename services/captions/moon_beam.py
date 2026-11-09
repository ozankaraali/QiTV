"""Our Turkish Moonshine read with a beam search and shallow fusion with an n-gram over its pieces
(services.captions.ngram): the Test model list's "beam + LM" entries (a run from source), to
compare live against the greedy decode the same model gets through sherpa-onnx.

A hypothesis scores log p_model + λ · log p_LM + β · its tokens (λ = 0.1, β = 0.5; natural logs).
Each step, every hypothesis takes its 8 best next tokens by the model alone before the LM is
asked, and the 4 best of all go on. Two rules keep the beam from what it did without them
(2026-11-08, measured on our medium model: it looped, or ended early): a hypothesis may end only
where the end token is within 1.0 of its best token's log-probability (an end among its 8 best
is not enough), and one that starts to loop (moon_ort.loop_tail) is dropped, not kept as an
ending. An end counts only among the step's 4 best; the search stops once 4 have ended and the
best going on scores below the best of them, or at sherpa-onnx's cap on tokens.

Measured on medium, whole clips against greedy (2026-11-08): with a large 5-gram, FLEURS test
18.98 → 16.59 % of words wrong, a drama test set 19.33 → 16.78, news 19.87 → 18.31; the tiny
4-gram the model folder holds (38 MB) keeps most of it: FLEURS dev 20.24 → 18.24 (the 5-gram
17.99, the beam alone 18.74), a drama dev set 31.28 → 27.86 (27.81, 29.45). The decoder runs once
per hypothesis and step (moon_ort.Steps): each keeps its own keys and values, so beams that
reorder need nothing gathered.

Only the standard library and onnxruntime (no numpy in the captions runtime): a row of logits is
read in Python, its normalizer summed whole and its best tokens found by the maxima of 128-token
blocks (only the blocks that hold one are looked through).
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, replace
from itertools import compress
import logging
from math import exp, inf, log, sin, tau
import os
from typing import Any, Optional, Protocol, Sequence, Union

from services.captions.moon_ort import EOS, Decoded, Encoded, MoonshineOrt, loop_tail, token_cap
from services.captions.ngram import BOS, NgramLM

logger = logging.getLogger(__name__)

WIDTH = 4  # hypotheses that go on
PRE = 8  # each hypothesis's best next tokens by the model, before the LM is asked
LM_WEIGHT = 0.1  # λ
LENGTH_BONUS = 0.5  # β, per token
EOS_GAP = 1.0  # an end only within this of the hypothesis's best token
BANNED = (0, 1, 3)  # padding, the start, the unknown piece: never written
LN10 = log(10.0)
_BLOCK = 128
_CACHE_MAX = 1 << 18  # the LM's scores remembered (a growing utterance is read again and again)


@dataclass(frozen=True)
class Settings:
    width: int = WIDTH
    pre: int = PRE
    lm_weight: float = LM_WEIGHT
    length_bonus: float = LENGTH_BONUS
    eos_gap: float = EOS_GAP


def _log_total(row: Sequence[float]) -> float:
    """log Σ exp(row) (in doubles; shifted by the largest when a logit is beyond exp's range)."""
    try:
        total = sum(map(exp, row))
    except OverflowError:
        total = inf
    if 0.0 < total < inf:
        return log(total)
    top = max(row)
    return top + log(sum(exp(value - top) for value in row))


def best_tokens(row: array, count: int) -> list[tuple[int, float]]:
    """The `count` best tokens of a row of logits (token, log-softmax over the whole row), best
    first (of equals, the lower id); never a BANNED one. `row` is changed: those become -inf."""
    norm = _log_total(row)
    width = len(row)
    for token in BANNED:
        if token < width:
            row[token] = -inf
    starts = range(0, width, _BLOCK)
    tops = [max(row[start : start + _BLOCK]) for start in starts]
    # the count-th largest block maximum: at least `count` tokens are as good, and only the
    # blocks whose maximum reaches it can hold a better one
    floor = sorted(tops)[-count] if len(tops) > count else -inf
    picked: list[int] = []
    for start, top in zip(starts, tops):
        if top >= floor and top > -inf:
            block = row[start : start + _BLOCK]
            picked += compress(range(start, start + len(block)), map(floor.__le__, block))
    picked = [token for token in picked if row[token] > -inf]
    picked.sort(key=lambda token: (-row[token], token))
    return [(token, row[token] - norm) for token in picked[:count]]


class LanguageModel(Protocol):
    """What the search asks of an n-gram (ngram.NgramLM): log10 p(token | context), and the
    context after it."""

    def score(self, context: tuple[int, ...], token: int) -> tuple[float, tuple[int, ...]]: ...


class LmScores:
    """The n-gram's log-probabilities in nats (natural logs, as the model's), remembered."""

    def __init__(self, lm: LanguageModel, limit: int = _CACHE_MAX) -> None:
        self.lm = lm
        self._limit = limit
        self._cache: dict[tuple[tuple[int, ...], int], tuple[float, tuple[int, ...]]] = {}

    @staticmethod
    def start() -> tuple[int, ...]:
        return (BOS,)

    def score(self, state: tuple[int, ...], token: int) -> tuple[float, tuple[int, ...]]:
        """log p(token | state) and the state after it."""
        key = (state, token)
        got = self._cache.get(key)
        if got is None:
            if len(self._cache) >= self._limit:
                self._cache.clear()
            log10, after = self.lm.score(state, token)
            got = self._cache[key] = (log10 * LN10, after)
        return got


class Stepper(Protocol):
    """A decode's calls a hypothesis at a time (moon_ort.Steps)."""

    first: array
    start: Any

    def step(self, state: Any, token: int) -> tuple[Any, array]: ...

    def release(self, state: Any) -> None: ...


@dataclass
class _Hypothesis:
    score: float
    tokens: list[int]
    logprobs: list[float]  # the model's, per token
    model: float  # the model's log-probability of its tokens
    lm: float  # the LM's
    lm_state: Any
    state: Any  # its keys and values (the stepper's)
    row: list[tuple[int, float]]  # its best next tokens


def search(
    steps: Stepper, cap: int, settings: Settings = Settings(), lm: Optional[LmScores] = None
) -> Decoded:
    """The best hypothesis of a beam search with shallow fusion (`lm` None: the model alone),
    at most `cap` tokens. Each state the search made is released before it returns."""
    width, pre, bonus = settings.width, settings.pre, settings.length_bonus
    weight = settings.lm_weight if lm is not None else 0.0
    start_lm = lm.start() if lm is not None else None
    live = [
        _Hypothesis(0.0, [], [], 0.0, 0.0, start_lm, steps.start, best_tokens(steps.first, pre))
    ]
    finished: list[tuple[float, list[int], list[float], bool]] = []  # score, tokens, …, ended
    try:
        for _ in range(cap + 1):
            candidates = []
            for index, hypothesis in enumerate(live):
                row = hypothesis.row
                if not row:
                    continue
                floor = row[0][1] - settings.eos_gap
                for token, logprob in row:
                    if token == EOS and logprob < floor:
                        continue  # an end far below its best token: not here
                    model = hypothesis.model + logprob
                    if weight:
                        assert lm is not None
                        delta, lm_state = lm.score(hypothesis.lm_state, token)
                        fused = hypothesis.lm + delta
                    else:
                        lm_state, fused = None, 0.0
                    count = len(hypothesis.tokens) + (token != EOS)
                    score = model + weight * fused + bonus * count
                    candidates.append((score, index, token, logprob, model, fused, lm_state))
            candidates.sort(key=lambda candidate: -candidate[0])
            chosen: list[_Hypothesis] = []
            parents: list[_Hypothesis] = []
            for rank, (score, index, token, logprob, model, fused, lm_state) in enumerate(
                candidates
            ):
                parent = live[index]
                if token == EOS:
                    if rank < width:
                        finished.append((score, parent.tokens, parent.logprobs, True))
                    continue
                tokens = parent.tokens + [token]
                if loop_tail(tokens):
                    continue  # it loops: dropped
                logprobs = parent.logprobs + [logprob]
                if len(tokens) >= cap:
                    finished.append((score, tokens, logprobs, False))
                    continue
                chosen.append(
                    _Hypothesis(score, tokens, logprobs, model, fused, lm_state, None, [])
                )
                parents.append(parent)
                if len(chosen) == width:
                    break
            if not chosen:
                break
            if len(finished) >= width and chosen[0].score < max(f[0] for f in finished):
                break
            stepped: list[_Hypothesis] = []
            try:
                for hypothesis, parent in zip(chosen, parents):
                    hypothesis.state, logits = steps.step(parent.state, hypothesis.tokens[-1])
                    stepped.append(hypothesis)
                    hypothesis.row = best_tokens(logits, pre)
            except BaseException:
                for hypothesis in stepped:
                    steps.release(hypothesis.state)
                raise
            for hypothesis in live:
                steps.release(hypothesis.state)
            live = stepped
        if not finished:
            finished = [(h.score, h.tokens, h.logprobs, False) for h in live]
    finally:
        for hypothesis in live:
            steps.release(hypothesis.state)
    _score, tokens, logprobs, ended = max(finished, key=lambda f: f[0])
    return Decoded(tokens=list(tokens), logprobs=list(logprobs), eos=ended)


class BeamReader:
    """A Moonshine export (moon_ort) read with `search`, with the n-gram in `lm` (None: the beam
    alone). One reading at a time (the model's lock); no Qt."""

    def __init__(
        self,
        folder: Union[str, os.PathLike[str]],
        lm: Union[str, os.PathLike[str], None] = None,
        threads: int = 2,
        settings: Settings = Settings(),
        library: Union[str, os.PathLike[str], None] = None,
    ) -> None:
        self.model = MoonshineOrt(folder, threads, library)
        self.lm = NgramLM(lm) if lm else None
        self.scores = LmScores(self.lm) if self.lm is not None else None
        self.settings = settings
        logger.info(
            "Turkish Moonshine with a beam of %d%s",
            settings.width,
            f" and a {self.lm.order}-gram" if self.lm is not None else "",
        )

    def decode(
        self, encoded: Encoded, max_new_tokens: Optional[int] = None, width: Optional[int] = None
    ) -> Decoded:
        """The best hypothesis; `width` narrows the beam for this reading (1: its best path)."""
        if encoded.silent:
            return Decoded(eos=True)
        cap = token_cap(encoded.frames) if max_new_tokens is None else max_new_tokens
        if cap <= 0:
            return Decoded()
        settings = self.settings if width is None else replace(self.settings, width=width)
        with self.model.steps(encoded) as steps:
            return search(steps, cap, settings, self.scores)

    def transcribe(
        self, samples: Union[array, Sequence[float]], width: Optional[int] = None
    ) -> str:
        """A piece's text (16 kHz mono floats), as sherpa-onnx writes a decode's tokens."""
        encoded = self.model.encode(samples)
        try:
            return self.model.text(self.decode(encoded, width=width).tokens)
        finally:
            encoded.release()

    def warm(self) -> None:
        """A first reading (onnxruntime's first runs are slow): a second of a quiet tone, a
        few tokens (silence would be skipped)."""
        tone = array("f", (0.01 * sin(tau * 220 * i / 16000) for i in range(16000)))
        encoded = self.model.encode(tone)
        try:
            self.decode(encoded, max_new_tokens=3)
        finally:
            encoded.release()

    def close(self) -> None:
        if self.lm is not None:
            self.lm.close()
