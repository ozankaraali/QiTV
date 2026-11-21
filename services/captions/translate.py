"""Caption translation on the device (2.10): MiLMMT-46-1B (Gemma 3 1B), an int4 ONNX export with
the greedy pick inside the graph, run through QiTV's own onnxruntime binding
(services.captions.onnx_c) with a key-value-cache loop of our own, as moon_ort runs Moonshine.

The graph takes `input_ids` [1, n], `attention_mask` [1, total] (all ones) and, per layer, the
past keys and values [1, 1, past, 256] (empty at the first call); it gives `next_token` (the
argmax at the last position, one int64) and the presents, fed back as the next call's pasts. The
prompt is the model card's; the tokenizer (Hugging Face `tokenizers`, which downloads with the
model) is asked for no special tokens (no `<bos>`), as the model card runs it. It is imported
only when the model is first used. No Qt, no numpy.
"""

from __future__ import annotations

from array import array
import ctypes
import logging
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Optional

from services.captions import onnx_c, text_language
from services.captions.mt_languages import MT_LANGUAGES

logger = logging.getLogger(__name__)

MAX_NEW_TOKENS = 96
EOS_IDS = frozenset({1, 106})  # <eos>, <end_of_turn>

_PAST = "past_key_values."
_PRESENT = "present."
_NEXT = "next_token"
_MODEL = "model.onnx"
_TOKENIZER = "tokenizer.json"
_HEADS, _HEAD_SIZE = 1, 256
# a dash between words reads as a comma; one at the start or the end goes
_DASH = re.compile(r"(?<=\w)\s*—\s*(?=\w)")
_EDGE_DASHES = re.compile(r"^[\s—]+|[\s—]+$")
_REPEAT = re.compile(r"\b(\w+)(?:\s+\1\b){3,}", re.IGNORECASE)


def _cause(error: BaseException) -> str:
    """An error as the log names it: "OSError: no tokenizer.json"."""
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def prompt(text: str, source: str, target: str) -> str:
    """The model card's translation prompt."""
    return f"Translate this from {source} to {target}:\n{source}: {text}\n{target}:"


def prefix(source: str, target: str) -> str:
    """2.13.1: the start every line's prompt from `source` to `target` has, run once and kept
    (`Translator`): up to the source's colon (the space after it is the line's first word's)."""
    return f"Translate this from {source} to {target}:\n{source}:"


def context_prompt(text: str, source: str, target: str, before: str, translated: str) -> str:
    """2.13.1 ("f2", 2026-11-21): the card's prompt over the line before (`before`) and this one,
    joined, with the line before's translation (`translated`) already written: the model goes
    on with this line's. Into English it gave the right he or she in 16-17 of 19 TV scenes,
    against 7 alone."""
    return f"{prompt(f'{before} {text}', source, target)} {translated}"


_WORD = re.compile(r"\w+")
# the mark a translation goes on from the one before's with (", she came"); never an ellipsis
_LEADING_MARKS = re.compile(r"^(?:\s|[,;:!?]|\.(?![.…]))+")


def _grams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def leaks(shown: str, before: str, translated: str, text: str) -> bool:
    """A translation made with the line before (`context_prompt`) holds that line: four words in
    a row of its translation (`translated`) or of the line itself (`before`), or all of a
    shorter one, that the line heard (`text`) doesn't hold too (a name said in both)."""
    out = [word.casefold() for word in _WORD.findall(shown)]
    own = [word.casefold() for word in _WORD.findall(text)]
    for earlier in (translated, before):
        words = [word.casefold() for word in _WORD.findall(earlier)]
        n = min(4, len(words))
        if n and len(out) >= n and (_grams(out, n) & _grams(words, n)) - _grams(own, n):
            return True
    return False


# 2.13.1: the line the translator's warm-up is timed on (`Translator.speed`): a medium one, as
# 2026-11-21's measurements' (8 to 12 words; the growing writer's lines are at most that long)
SPEED_LINE = "Bu akşam eve geç geleceğim, yemeği bensiz yiyin lütfen."


# scripts where a character carries about a word: the cap on a translation's length counts
# each as 3 (a 10-character Chinese line translates to 50 or more English characters)
_DENSE = re.compile(
    "[\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u3040-\u30ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff]"
)


def clean(source: str, output: str, target: str) -> Optional[str]:
    """What the model wrote, tidied; None when it is not a translation to show: empty, far
    longer than the source, a word said over and over, or not in the target's script (each of
    MiLMMT's targets has its own in text_language)."""
    text = _DASH.sub(", ", _EDGE_DASHES.sub("", output.split("\n", 1)[0]))
    dense = len(_DENSE.findall(source))
    if not text or len(text) > 3 * (len(source) + 2 * dense) + 20:
        return None
    if _REPEAT.search(text):
        return None
    if target in MT_LANGUAGES and not text_language.written_in(text, target):
        return None
    return text


class Translator:
    """The model in one folder (model.onnx, model.onnx.data, tokenizer.json), `threads` intra-op
    threads. It loads on first use (`warm` does it ahead); one translation at a time. 2.13.1:
    the prompt's start (`prefix`) runs once per language pair and each line runs on from its
    keys and values (the same tokens; 2026-11-21: about 10 % sooner, its first token 100 ms
    sooner on a 4-core ARM box); a line may go with the one before (`translate`'s `context`)."""

    def __init__(self, folder: Path, threads: int = 2, library: Optional[Path] = None) -> None:
        self._folder = Path(folder)
        self._threads = threads
        self._library = library
        self._lock = threading.Lock()
        self._session: Any = None
        self._api: Any = None
        self._tokenizer: Any = None
        self._past_names: list[str] = []
        self._present_names: list[str] = []
        self._selection: Any = None
        self._empty: Any = None
        self._broken = False
        self.error = ""  # why the model is of no use ("OSError: ..."), once it is not
        # 2.13.1: the prompt's start for one language pair: its ids, its presents once run
        self._prefix_pair: Optional[tuple[str, str]] = None
        self._prefix_ids: list[int] = []
        self._prefix_kv: Optional[list[Any]] = None

    def _load(self) -> None:
        site = self._folder / "site"  # the tokenizers wheel, unpacked with the model
        if site.is_dir() and str(site) not in sys.path:
            sys.path.insert(0, str(site))
        from tokenizers import Tokenizer  # downloaded with the model: not a dependency

        api = onnx_c.Api(onnx_c.find_library(self._library))
        session = None
        try:
            session = onnx_c.Session(api, self._folder / _MODEL, self._threads)
            past = [name for name in session.inputs if name.startswith(_PAST)]
            present = [_PRESENT + name[len(_PAST) :] for name in past]
            needed = {"input_ids", "attention_mask", *past}
            if set(session.inputs) != needed or not past:
                raise ValueError(f"not MiLMMT's graph: {session.inputs}")
            if _NEXT not in session.outputs or any(n not in session.outputs for n in present):
                raise ValueError(f"not MiLMMT's graph: {session.outputs}")
            tokenizer = Tokenizer.from_file(str(self._folder / _TOKENIZER))
            selection = session.select([_NEXT, *present])
            empty = api.tensor(array("f"), (1, _HEADS, 0, _HEAD_SIZE), onnx_c.FLOAT)
        except BaseException:
            if session is not None:
                session.close()
            api.close()
            raise
        self._tokenizer = tokenizer
        self._api = api
        self._session = session
        self._past_names = past
        self._present_names = present
        self._selection = selection
        self._empty = empty
        logger.info(
            "MiLMMT through onnxruntime %s's C API (%d threads)", api.version, self._threads
        )

    def _ready(self) -> bool:
        """The model is loaded (the lock is held); False once a load has failed, for good."""
        if self._session is not None:
            return True
        if self._broken:
            return False
        try:
            self._load()
        except Exception as error:
            self._broken = True
            self.error = _cause(error)
            # (the owner's log gets the message alone: the cause goes in it)
            logger.exception(
                "caption translation: the model could not be loaded (%s); it stays off", self.error
            )
            return False
        return True

    def warm(self) -> bool:
        """Loads the model and runs one call, so the first caption is not the slow one; False
        when either failed (logged): the translator is of no use."""
        with self._lock:
            if not self._ready():
                return False
            try:
                self._generate([2], 1, None)  # (one token: its result is of no use)
            except Exception as error:
                self.error = _cause(error)
                logger.exception("caption translation: the warm-up call failed (%s)", self.error)
                return False
            return True

    def translate(
        self,
        text: str,
        source: str,
        target: str,
        deadline: Optional[float] = None,
        context: Optional[tuple[str, str]] = None,
    ) -> Optional[str]:
        """`text` from `source` to `target` (MiLMMT's names); None when the original should
        show: a bad output, one cut off at MAX_NEW_TOKENS, an error (logged), or `deadline` (a
        time.monotonic() value) passed between tokens. 2.13.1: with `context` (the line before
        and its translation), translated with them (`context_prompt`); what that gives holding
        the line before (`leaks`), or nothing to show, the line is translated alone, by the same
        deadline."""
        with self._lock:
            if not self._ready():
                return None
            try:
                if context is not None:
                    shown = self._with_context(text, source, target, deadline, context)
                    if shown is not None:
                        return shown
                    if deadline is not None and time.monotonic() > deadline:
                        return None
                decoded = self._decoded(prompt(text, source, target), source, target, deadline)
            except Exception:
                logger.exception("caption translation failed; the original shows")
                return None
            return None if decoded is None else clean(text, decoded, target)

    def speed(self) -> Optional[float]:
        """2.13.1: the faster of two timed translations of a medium line (SPEED_LINE, Turkish to
        English), in seconds: the adaptive lead's measure (services.captions.streaming
        .measured_lead); None when either gave nothing."""
        best: Optional[float] = None
        for _ in range(2):
            began = time.perf_counter()
            if self.translate(SPEED_LINE, "Turkish", "English") is None:
                return None
            took = time.perf_counter() - began
            best = took if best is None else min(best, took)
        return best

    def _with_context(
        self,
        text: str,
        source: str,
        target: str,
        deadline: Optional[float],
        context: tuple[str, str],
    ) -> Optional[str]:
        """`text` translated with the line before (`context`), as it shows; None when it gave
        nothing to show or held the line before (the lock is held)."""
        before, translated = context
        decoded = self._decoded(
            context_prompt(text, source, target, before, translated), source, target, deadline
        )
        if decoded is None:
            return None
        shown = clean(text, _LEADING_MARKS.sub("", decoded), target)
        if shown is None:
            logger.debug("caption translation: nothing usable with the line before; alone")
        elif leaks(shown, before, translated, text):
            logger.debug("caption translation: the line before leaked into %r; alone", shown)
            shown = None
        return shown

    def _decoded(
        self, text: str, source: str, target: str, deadline: Optional[float]
    ) -> Optional[str]:
        """What the model writes after the prompt `text` (from `source` to `target`), from the
        pair's kept prefix when the prompt's ids start with it; None when cut off (the lock is
        held)."""
        ids, cached = self._ids(text, source, target)
        tokens = self._generate(ids, MAX_NEW_TOKENS, deadline, cached)
        return None if tokens is None else self._tokenizer.decode(tokens, skip_special_tokens=True)

    def _ids(self, text: str, source: str, target: str) -> tuple[list[int], int]:
        """The prompt `text` as ids, and how many of them the pair's prefix covers: its ids
        when the prompt's start strictly with them, else 0 (the whole prompt runs)."""
        if self._prefix_pair != (source, target):
            self._drop_prefix()
            encoded = self._tokenizer.encode(prefix(source, target), add_special_tokens=False)
            self._prefix_ids = list(encoded.ids)
            self._prefix_pair = (source, target)
        ids = list(self._tokenizer.encode(text, add_special_tokens=False).ids)
        head = self._prefix_ids
        return ids, len(head) if 0 < len(head) < len(ids) and ids[: len(head)] == head else 0

    def _prefix_pasts(self) -> list[Any]:
        """The pair's prefix run, its presents kept (each line's pasts from here)."""
        if self._prefix_kv is None:
            head = self._prefix_ids
            empty = [self._empty] * len(self._past_names)
            feed, own = self._feed(array("q", head), len(head), empty)
            try:
                outs = self._session.run(feed, self._selection)
            finally:
                for value in own:
                    value.release()
            outs[0].release()  # (its next token: the line's own first piece follows)
            self._prefix_kv = outs[1:]
        return self._prefix_kv

    def _drop_prefix(self) -> None:
        kept, self._prefix_kv = self._prefix_kv, None
        self._prefix_pair, self._prefix_ids = None, []
        for value in kept or ():
            value.release()

    def _feed(self, ids: array, mask: int, pasts: list[Any]) -> tuple[list[Any], list[Any]]:
        api = self._api
        ids_value = api.tensor(ids, (1, len(ids)), onnx_c.INT64)
        mask_value = api.tensor(array("q", [1] * mask), (1, mask), onnx_c.INT64)
        by_name = {"input_ids": ids_value, "attention_mask": mask_value}
        by_name.update(zip(self._past_names, pasts))
        return [by_name[name] for name in self._session.inputs], [ids_value, mask_value]

    def _generate(
        self, prompt_ids: list[int], limit: int, deadline: Optional[float], cached: int = 0
    ) -> Optional[list[int]]:
        """Greedy tokens after the prompt, until EOS or a newline; None when `limit` tokens or
        the deadline came first (the final review: a translation cut off at MAX_NEW_TOKENS
        showed; the original shows instead). 2.13.1: its first `cached` ids are the pair's
        prefix (`_ids`): the rest runs from its kept presents, which are never released here."""
        session = self._session
        total = len(prompt_ids)
        step = array("q", prompt_ids[cached:])
        pasts: list[Any] = (
            list(self._prefix_pasts()) if cached else [self._empty] * len(self._past_names)
        )
        owned = False  # the pasts are this call's own (not the empties, not the prefix's)
        tokens: list[int] = []
        try:
            while True:
                feed, own = self._feed(step, total, pasts)
                try:
                    outs = session.run(feed, self._selection)
                finally:
                    for value in own:
                        value.release()
                if owned:
                    for value in pasts:
                        value.release()
                pasts, owned = outs[1:], True
                token = ctypes.c_int64.from_address(outs[0].address()).value
                outs[0].release()
                if token in EOS_IDS:
                    break
                if "\n" in self._tokenizer.decode([token], skip_special_tokens=False):
                    break
                tokens.append(token)
                if len(tokens) >= limit:
                    return None  # cut off: not the whole translation
                if deadline is not None and time.monotonic() > deadline:
                    return None
                total += 1
                step = array("q", [token])
        finally:
            if owned:
                for value in pasts:
                    value.release()
        return tokens
