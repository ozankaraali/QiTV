"""Dev build (2026-11-19): how our Turkish Moonshine reads, stepped in the Subtitles panel so the
owner hears the night's language model on real channels and sees its cost on his own computer.

- greedy: the model's best token each step (MoonshineOrt.decode);
- beam: 2.10's 4-path beam alone (moon_beam.Settings());
- lm: the beam with the TV LSTM LM (lstm_lm.LstmLM, lm.u8mm.onnx: no signed int8), λ 0.1, β 0.41;
- lm_int8: the same with lm.int8.onnx (signed int8 LSTM weights: the pattern that looped the
  released decoder on CPUs without VNNI), λ 0.1, β 0.41;
- ilme: the u8mm LM less the decoder's own language habit (lstm_lm.DecoderIlm), λ 0.2, β 0.62,
  μ 0.1 (tuned with the int8 LSTM; u8mm's dev mean is the same within noise).

The LM files are in LM_FOLDER beside the other caption models; a reading whose file isn't there is
not offered, and an LM that fails to load leaves Beam for the process's life. A "fast" reading
(the picture faster than real time, or translating on a slow computer) stays 2.10's: the best path
alone, no LM. One reading at a time holds the model (its lock); the LM is loaded once, its first
reading or warm()."""

from __future__ import annotations

import logging
from pathlib import Path
import threading
from typing import Any, Callable, Optional, Sequence

from services.captions.lstm_lm import DecoderIlm, Fused, LstmLM
from services.captions.moon_beam import BeamReader, Settings, search
from services.captions.moon_ort import token_cap

logger = logging.getLogger(__name__)

READINGS = ("greedy", "beam", "lm", "lm_int8", "ilme")
DEFAULT = "beam"
TEXTS = {
    "greedy": "Greedy",
    "beam": "Beam",
    "lm": "Beam + LM",
    "lm_int8": "Beam + LM (int8)",
    "ilme": "Beam + LM + ILME",
}
LM_FOLDER = "tr-moon-lm-tv"
LM_FILES = {"lm": "lm.u8mm.onnx", "lm_int8": "lm.int8.onnx", "ilme": "lm.u8mm.onnx"}
_TUNED = Settings(lm_weight=0.1, length_bonus=0.41)
SETTINGS = {
    "beam": Settings(),
    "lm": _TUNED,
    "lm_int8": _TUNED,
    "ilme": Settings(lm_weight=0.2, length_bonus=0.62),
}
ILME_MU = 0.1


def available(folder: Optional[Path]) -> list[str]:
    """The readings whose LM file is in `folder` (greedy and beam always), in READINGS' order."""
    return [
        name
        for name in READINGS
        if name not in LM_FILES or (folder is not None and (folder / LM_FILES[name]).is_file())
    ]


def stepped(current: str, delta: int, choices: Sequence[str]) -> str:
    """The choice after (`delta` > 0) or before `current`, round the list; one not among them
    steps from DEFAULT's place, else from the first."""
    if not choices:
        return DEFAULT
    if current in choices:
        at = list(choices).index(current)
    else:
        at = list(choices).index(DEFAULT) if DEFAULT in choices else 0
    return choices[(at + (1 if delta > 0 else -1)) % len(choices)]


def _lstm(path: Path, api: Any) -> Any:
    return LstmLM(path, api, threads=1)


class TurkishReader:
    """Our Turkish Moonshine (a moon_beam.BeamReader) read the way `reading` says; no Qt."""

    def __init__(
        self,
        beam: BeamReader,
        folder: Optional[Path],
        reading: str = DEFAULT,
        lm_factory: Optional[Callable[[Path, Any], Any]] = None,
    ) -> None:
        self.beam = beam
        self.folder = folder
        self._factory = lm_factory or _lstm
        self._lock = threading.Lock()  # the LMs' loading
        self._lms: dict[str, Any] = {}
        self._failed: set[str] = set()
        self._reading = DEFAULT
        self.set_reading(reading)

    @property
    def reading(self) -> str:
        return self._reading

    @property
    def model(self) -> Any:
        return self.beam.model

    def choices(self) -> list[str]:
        """The readings offered here: their file is there and didn't fail to load."""
        return [name for name in available(self.folder) if LM_FILES.get(name) not in self._failed]

    def set_reading(self, name: str) -> None:
        """From the next reading on; one not offered here is Beam."""
        self._reading = name if name in self.choices() else DEFAULT
        logger.info("AI captions: Turkish is read by %s", TEXTS[self._reading])

    def _lm(self, name: str) -> Any:
        """The LM in file `name`, loaded once; None (and Beam from now on) when it won't load."""
        with self._lock:
            if name in self._lms:
                return self._lms[name]
            if name in self._failed or self.folder is None:
                return None
            try:
                lm = self._factory(self.folder / name, self.model._api)
            except Exception as error:
                self._failed.add(name)
                self._reading = DEFAULT
                logger.warning(
                    "AI captions: the Turkish LM %s did not load (%s): Beam from now on",
                    name,
                    type(error).__name__,
                )
                return None
            self._lms[name] = lm
            return lm

    def transcribe(self, samples: Any, fast: bool = False) -> tuple[str, str]:
        """A piece's text (16 kHz mono floats) and the reading that wrote it ("lm (fast)": the
        best path alone). Beam, and any fast reading, are BeamReader.transcribe as 2.10 reads."""
        reading = self._reading  # once: a switch meanwhile is the next reading's
        if reading == "greedy":
            return self._greedy(samples), reading
        lm = None
        if not fast and reading in LM_FILES:
            lm = self._lm(LM_FILES[reading])
            if lm is None:
                reading = DEFAULT
        if lm is None:
            label = f"{reading} (fast)" if fast else reading
            return str(self.beam.transcribe(samples, width=1 if fast else None)), label
        model = self.model
        encoded = model.encode(samples)
        try:
            if encoded.silent:
                return "", reading
            cap = token_cap(encoded.frames)
            if cap <= 0:
                return "", reading
            settings = SETTINGS[reading]
            with model.steps(encoded) as steps:
                if reading == "ilme":
                    assert encoded.value is not None  # (not silent)
                    shape = encoded.value.shape
                    ilm = DecoderIlm(model, shape[1], shape[2])
                    try:
                        fused: Any = Fused(lm, ilm, ILME_MU / settings.lm_weight)
                        return model.text(search(steps, cap, settings, fused).tokens), reading
                    finally:
                        ilm.close()
                return model.text(search(steps, cap, settings, lm).tokens), reading
        finally:
            encoded.release()

    def _greedy(self, samples: Any) -> str:
        model = self.model
        encoded = model.encode(samples)
        try:
            return str(model.text(model.decode(encoded).tokens))
        finally:
            encoded.release()

    def warm(self) -> None:
        """The beam's first reading, and the chosen reading's LM loaded."""
        self.beam.warm()
        if self._reading in LM_FILES:
            self._lm(LM_FILES[self._reading])

    def close(self) -> None:
        with self._lock:
            lms, self._lms = self._lms, {}
        for lm in lms.values():
            lm.close()
        self.beam.close()
