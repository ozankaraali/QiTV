"""2.12.1: how our Turkish Moonshine reads. With its language model here (models.TURKISH_LM, an
LSTM over the model's pieces: services.captions.lstm_lm), the 4-path beam fuses it in:
a hypothesis scores log p + λ·log p_LM + β·its tokens at λ 0.1 and β 0.41 (LM_SETTINGS). Without
it, as 2.10 to 2.12.0 read: the beam alone at β 0.5 (moon_beam.BeamReader, its own Settings()).

Measured on the shipped model against the beam alone (2026-11-19, 12 Turkish test sets): 0.60
points fewer words wrong on average, 9 sets better and none worse, most on news and read speech
(FLEURS 10.31 → 9.37 %), drama about the same; about 7 % more time on one thread. The int8 LM
reads exactly on an x86 CPU without VNNI too, at the beam's own speed (2026-11-20).

The LM never costs Turkish its model: one that won't load, or fails in a reading, is logged once
and the beam reads alone for the process's life (the piece it failed on read again by it). A
reading narrowed to its best path (`width` 1: the picture faster than real time, or translating
on a slow computer) stays the beam's alone, without the LM, as before. The LM loads once (warm(),
else its first reading); an LM downloaded after the load comes by use_lm(). A reading holds the
model's lock (moon_ort.MoonshineOrt.steps), so the LM is read by one reading at a time. No Qt."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Optional, Union

from services.captions.lstm_lm import LstmLM
from services.captions.moon_beam import Settings, search
from services.captions.moon_ort import token_cap

logger = logging.getLogger(__name__)

LM_SETTINGS = Settings(lm_weight=0.1, length_bonus=0.41)  # λ, β with the LM (2026-11-19)


def _lstm(path: Path, api: Any) -> Any:
    return LstmLM(path, api, threads=1)


class TurkishReader:
    """moon_beam.BeamReader's reading (transcribe, warm, close) with the LM at `lm` (None: none
    here, the beam alone); `lm_factory(path, the model's onnxruntime)` loads it."""

    def __init__(
        self,
        beam: Any,
        lm: Union[str, os.PathLike[str], None] = None,
        lm_factory: Optional[Callable[[Path, Any], Any]] = None,
    ) -> None:
        self.beam = beam
        self._path = Path(lm) if lm else None
        self._factory = lm_factory or _lstm
        self._lock = threading.Lock()  # the LM's loading
        self._lm: Any = None
        self._off = False  # it failed: the beam alone from now on

    @property
    def model(self) -> Any:
        return self.beam.model

    def use_lm(self, path: Union[str, os.PathLike[str]]) -> None:
        """The LM downloaded since the load: read with it from the next piece (one given at
        the load stays)."""
        with self._lock:
            if self._path is None:
                self._path = Path(path)

    def _loaded(self) -> Any:
        """The LM, loaded once; None without one, or once it failed."""
        with self._lock:
            if self._lm is not None or self._off or self._path is None:
                return self._lm
            try:
                self._lm = self._factory(self._path, self.beam.model._api)
            except Exception as error:
                self._fail("did not load", error)
                return None
            logger.info("AI captions: Turkish is read with its language model")
            return self._lm

    def _fail(self, what: str, error: BaseException) -> None:
        """The beam alone from now on, said once."""
        if self._off:
            return
        self._off = True
        self._lm = None
        logger.warning(
            "AI captions: the Turkish language model %s (%s: %s); the beam reads Turkish alone",
            what,
            type(error).__name__,
            error,
        )

    def transcribe(self, samples: Any, width: Optional[int] = None) -> str:
        """A piece's text (16 kHz mono floats), as BeamReader.transcribe writes it: with the LM
        when it's here; `width` narrows the beam for this reading (1: its best path), without
        the LM."""
        lm = self._loaded() if width is None else None
        if lm is not None:
            try:
                return self._read(samples, lm)
            except Exception as error:  # read again by the beam alone (its failure is its own)
                self._fail("failed", error)
        return str(self.beam.transcribe(samples, width=width))

    def _read(self, samples: Any, lm: Any) -> str:
        model = self.beam.model
        encoded = model.encode(samples)
        try:
            if encoded.silent:
                return ""
            cap = token_cap(encoded.frames)
            if cap <= 0:
                return ""
            with model.steps(encoded) as steps:
                return str(model.text(search(steps, cap, LM_SETTINGS, lm).tokens))
        finally:
            encoded.release()

    def warm(self) -> None:
        """The beam's first reading, then the LM loaded (its first run, the start's, with it)."""
        self.beam.warm()
        self._loaded()

    def close(self) -> None:
        with self._lock:
            lm, self._lm = self._lm, None
        if lm is not None:
            lm.close()
        self.beam.close()
