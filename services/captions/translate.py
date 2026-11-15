"""Caption translation on the device (2.10): MiLMMT-46-1B (Gemma 3 1B), an int4 ONNX export with
the greedy pick inside the graph, run through QiTV's own onnxruntime binding
(services.captions.onnx_c) with a key-value-cache loop of our own, as moon_ort runs Moonshine.

The graph takes `input_ids` [1, n], `attention_mask` [1, total] (all ones) and, per layer, the
past keys and values [1, 1, past, 256] (empty at the first call); it gives `next_token` (the
argmax at the last position, one int64) and the presents, fed back as the next call's pasts. The
prompt is the model card's; the tokenizer (Hugging Face `tokenizers`, which downloads with the
model) adds `<bos>`. It is imported only when the model is first used. No Qt, no numpy.
"""

from __future__ import annotations

from array import array
import ctypes
import logging
from pathlib import Path
import re
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
_DASH = re.compile(r"\s*—\s*")
_REPEAT = re.compile(r"\b(\w+)(?:\s+\1\b){3,}", re.IGNORECASE)
# MiLMMT's English names to the names text_language.written_in knows (Qwen's)
_SCRIPT_NAME = {"Chinese (Simplified)": "Chinese", "Chinese (Traditional)": "Chinese"}


def prompt(text: str, source: str, target: str) -> str:
    """The model card's translation prompt."""
    return f"Translate this from {source} to {target}:\n{source}: {text}\n{target}:"


def clean(source: str, output: str, target: str) -> Optional[str]:
    """What the model wrote, tidied; None when it is not a translation to show: empty, far
    longer than the source, a word said over and over, or not in the target's script."""
    text = _DASH.sub(", ", output.split("\n", 1)[0].strip()).strip()
    if not text or len(text) > 3 * len(source) + 20:
        return None
    if _REPEAT.search(text):
        return None
    if target in MT_LANGUAGES:
        language = _SCRIPT_NAME.get(target, target)
        if not text_language.written_in(text, language):
            return None
    return text


class Translator:
    """The model in one folder (model.onnx, model.onnx.data, tokenizer.json), `threads` intra-op
    threads. It loads on first use (`warm` does it ahead); one translation at a time."""

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

    def _load(self) -> None:
        from tokenizers import Tokenizer  # downloaded with the model: not a dependency

        api = onnx_c.Api(onnx_c.find_library(self._library))
        session = onnx_c.Session(api, self._folder / _MODEL, self._threads)
        past = [name for name in session.inputs if name.startswith(_PAST)]
        present = [_PRESENT + name[len(_PAST) :] for name in past]
        needed = {"input_ids", "attention_mask", *past}
        if set(session.inputs) != needed or not past:
            raise ValueError(f"not MiLMMT's graph: {session.inputs}")
        if _NEXT not in session.outputs or any(n not in session.outputs for n in present):
            raise ValueError(f"not MiLMMT's graph: {session.outputs}")
        self._tokenizer = Tokenizer.from_file(str(self._folder / _TOKENIZER))
        self._api = api
        self._session = session
        self._past_names = past
        self._present_names = present
        self._selection = session.select([_NEXT, *present])
        self._empty = api.tensor(array("f"), (1, _HEADS, 0, _HEAD_SIZE), onnx_c.FLOAT)
        logger.info(
            "MiLMMT through onnxruntime %s's C API (%d threads)", api.version, self._threads
        )

    def warm(self) -> None:
        """Loads the model and runs one call, so the first caption is not the slow one."""
        with self._lock:
            if self._session is None:
                self._load()
            self._generate([2], 1, None)

    def translate(
        self, text: str, source: str, target: str, deadline: Optional[float] = None
    ) -> Optional[str]:
        """`text` from `source` to `target` (MiLMMT's names); None when the original should
        show: a bad output, or `deadline` (a time.monotonic() value) passed between tokens."""
        with self._lock:
            if self._session is None:
                self._load()
            ids = list(self._tokenizer.encode(prompt(text, source, target)).ids)
            tokens = self._generate(ids, MAX_NEW_TOKENS, deadline)
            if tokens is None:
                return None
            return clean(text, self._tokenizer.decode(tokens, skip_special_tokens=True), target)

    def _feed(self, ids: array, mask: int, pasts: list[Any]) -> tuple[list[Any], list[Any]]:
        api = self._api
        ids_value = api.tensor(ids, (1, len(ids)), onnx_c.INT64)
        mask_value = api.tensor(array("q", [1] * mask), (1, mask), onnx_c.INT64)
        by_name = {"input_ids": ids_value, "attention_mask": mask_value}
        by_name.update(zip(self._past_names, pasts))
        return [by_name[name] for name in self._session.inputs], [ids_value, mask_value]

    def _generate(
        self, prompt_ids: list[int], limit: int, deadline: Optional[float]
    ) -> Optional[list[int]]:
        """Greedy tokens after the prompt: until EOS, a newline, `limit` or the deadline."""
        session = self._session
        total = len(prompt_ids)
        step = array("q", prompt_ids)
        pasts: list[Any] = [self._empty] * len(self._past_names)
        tokens: list[int] = []
        try:
            while True:
                feed, own = self._feed(step, total, pasts)
                try:
                    outs = session.run(feed, self._selection)
                finally:
                    for value in own:
                        value.release()
                for value in pasts:
                    if value is not self._empty:
                        value.release()
                pasts = outs[1:]
                token = ctypes.c_int64.from_address(outs[0].address()).value
                outs[0].release()
                if token in EOS_IDS:
                    break
                if "\n" in self._tokenizer.decode([token], skip_special_tokens=False):
                    break
                tokens.append(token)
                if len(tokens) >= limit:
                    break
                if deadline is not None and time.monotonic() > deadline:
                    return None
                total += 1
                step = array("q", [token])
        finally:
            for value in pasts:
                if value is not self._empty:
                    value.release()
        return tokens
