"""Turkish caption models on trial: the Subtitles panel's Test model list (a run from source).

The owner compares our own Turkish speech models live, picking one at a time. Each folder in
the AI captions folder that holds one is offered, known by its name and its files, so a newer
export shows up without a code change:

    ufakzeka-kulak/   ufak, the usual Turkish model (an offline transducer)
    tr-moon*/         a Moonshine v2 export (encoder_model[_quantized].onnx,
                      decoder_model_merged[_quantized].onnx, tokens.txt): "tr-moonm…" or
                      "tr-moon5m…" is a medium one, any other a small one
    tr-fc*/           a streaming FastConformer transducer (sherpa-onnx's online files:
                      encoder.int8.onnx, decoder.int8.onnx, joiner.int8.onnx, tokens.txt)

A folder may also say what it holds in `qitv-model.json` ({"kind": "moonshine", "stream" or
"ufak", "label": "…", "size": "small" or "medium"}); any folder name will do then. A model
picked runs as it is, on Turkish: no language detection, no other model.

With an n-gram over the Moonshine pieces here too, each Moonshine export is offered a second
time, read with a beam search and the n-gram (services.captions.moon_beam) instead of sherpa-onnx's
greedy decode: "Moonshine medium · beam + LM · tr-moonm-avg", kept as "tr-moonm-avg+beam-lm".

    tr-lm*/           the n-gram: one .qng file (services.captions.ngram), or the one its
                      manifest names ({"kind": "lm", "file": "….qng", "tokens_sha256": "…"}:
                      offered only beside the exports whose tokens.txt has that SHA-256)

No Qt; `discover` reads a folder listing, a few stats, the small manifests and the Moonshine
exports' tokens.txt (the GUI calls it from a thread).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

ENV = "QITV_TRIAL_MODELS"  # "1": a packaged build offers them too; "0": a source run doesn't
MANIFEST = "qitv-model.json"
UFAK, MOONSHINE, STREAM = "ufak", "moonshine", "stream"
LM = "lm"  # an n-gram over the Moonshine pieces (no model of its own)
GREEDY, BEAM_LM = "", "beam-lm"  # how a Moonshine export is read: sherpa-onnx's greedy, or ours
_DECODINGS = {BEAM_LM: "beam + LM"}  # each one's words in the list
_LM_PREFIX = "tr-lm"
_LM_SUFFIX = ".qng"
_KINDS = {
    UFAK: UFAK,
    MOONSHINE: MOONSHINE,
    STREAM: STREAM,
    "fastconformer-stream": STREAM,
    "online-transducer": STREAM,
}
_ORDER = (UFAK, MOONSHINE, STREAM)
UFAK_FOLDER = "ufakzeka-kulak"  # services.captions.models.TURKISH_FOLDER
TRANSDUCER_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
_MEDIUM = re.compile(r"^tr-moon\d*m")  # tr-moonm-avg, tr-moonm2-avg, tr-moon5m-avg


def enabled(env: Optional[Mapping[str, str]] = None, frozen: Optional[bool] = None) -> bool:
    """Whether the Test model list is offered: in a run from source unless QITV_TRIAL_MODELS=0,
    in a packaged build only with QITV_TRIAL_MODELS=1 (as services.control's port)."""
    env = os.environ if env is None else env
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    setting = env.get(ENV, "")
    return setting == "1" or (not frozen and setting != "0")


@dataclass(frozen=True)
class TrialModel:
    name: str  # what the config keeps: its folder's name (+"+beam-lm" for the beam's entry)
    kind: str  # UFAK, MOONSHINE or STREAM
    label: str  # as the list shows it: "Moonshine medium · tr-moonm-avg"
    folder: Path
    decoding: str = GREEDY  # a Moonshine export's: GREEDY or BEAM_LM
    lm: Optional[Path] = None  # BEAM_LM's n-gram file


def moonshine_files(folder: Path) -> tuple[Path, Path, Path]:
    """A Moonshine export's encoder, decoder and tokens (the quantized files where they are)."""

    def onnx(stem: str) -> Path:
        quantized = folder / f"{stem}_quantized.onnx"
        return quantized if quantized.exists() else folder / f"{stem}.onnx"

    return onnx("encoder_model"), onnx("decoder_model_merged"), folder / "tokens.txt"


def _has(folder: Path, kind: str) -> bool:
    files = moonshine_files(folder) if kind == MOONSHINE else [folder / n for n in TRANSDUCER_FILES]
    return all(path.is_file() for path in files)


def _manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        logger.warning("AI captions: %s's %s could not be read: %s", folder.name, MANIFEST, error)
        return {}
    return data if isinstance(data, dict) else {}


def normalize_samples(folder: Path) -> bool:
    """Whether sherpa-onnx scales a streaming export's samples to [-1, 1] (its default). An export
    whose encoder reads NeMo's own feature scale says {"normalize_samples": false} in its manifest
    (2026-11-12: sherpa's feature floor and first chunk then read as NeMo reads them, 3-5 words in
    100 fewer wrong); read the other way, such an export writes nothing at all."""
    return _manifest(folder).get("normalize_samples") is not False


STREAM_BLANK_PENALTY = 0.5  # our FastConformers: the blank's score lowered, fewer words dropped


def blank_penalty(folder: Path) -> float:
    """How much a streaming export's blank score is lowered: its manifest's "blank_penalty"
    when it gives one (another streaming model is read with its own setting, as it was
    scored), else ours."""
    value = _manifest(folder).get("blank_penalty")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return STREAM_BLANK_PENALTY


def _kind_by_name(name: str) -> Optional[str]:
    if name == UFAK_FOLDER:
        return UFAK
    if name.startswith("tr-moon"):
        return MOONSHINE
    if name.startswith("tr-fc") or "stream" in name:
        return STREAM
    return None


def _decoding_of(name: str) -> tuple[str, str]:
    """A list entry's folder name and decoding ("tr-moonm-avg+beam-lm": the beam's entry)."""
    for decoding in _DECODINGS:
        suffix = f"+{decoding}"
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], decoding
    return name, GREEDY


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def find_lm(root: Path) -> Optional[tuple[Path, Optional[str]]]:
    """The n-gram in the AI captions folder `root`, and the SHA-256 of the tokens.txt it was
    built on when its manifest says (None: any export's); None when there is none."""
    try:
        folders = sorted(entry for entry in Path(root).iterdir() if entry.is_dir())
    except OSError:
        return None
    for folder in folders:
        manifest = _manifest(folder)
        kind = str(manifest.get("kind", ""))
        if kind != LM and not (not kind and folder.name.startswith(_LM_PREFIX)):
            continue
        named = str(manifest.get("file") or "")
        if named:
            files = [folder / Path(named).name]
        else:
            try:
                files = sorted(folder.glob(f"*{_LM_SUFFIX}"))
            except OSError:
                files = []
        if files and files[0].is_file():
            tokens = manifest.get("tokens_sha256")
            return files[0], str(tokens).lower() if tokens else None
    return None


def _with_lm(model: TrialModel, lm: tuple[Path, Optional[str]]) -> Optional[TrialModel]:
    """A Moonshine export's beam + LM entry, when the n-gram was built on its tokens."""
    file, tokens = lm
    if model.kind != MOONSHINE or model.decoding != GREEDY:
        return None
    if tokens is not None and _sha256(moonshine_files(model.folder)[2]) != tokens:
        return None
    words = _DECODINGS[BEAM_LM]
    label = model.label
    tail = f" · {model.name}"
    label = (
        f"{label[: -len(tail)]} · {words}{tail}" if label.endswith(tail) else f"{label} · {words}"
    )
    return replace(model, name=f"{model.name}+{BEAM_LM}", label=label, decoding=BEAM_LM, lm=file)


def describe(folder: Path) -> Optional[TrialModel]:
    """The trial model in `folder`, or None (none, or its files are not all there). A beam
    entry's name ("tr-moonm-avg+beam-lm") names its export's folder, beside the n-gram."""
    base, decoding = _decoding_of(folder.name)
    if decoding != GREEDY:
        model = describe(folder.with_name(base))
        lm = find_lm(folder.parent)
        return _with_lm(model, lm) if model is not None and lm is not None else None
    manifest = _manifest(folder)
    kind = _KINDS.get(str(manifest.get("kind", ""))) or _kind_by_name(folder.name)
    if kind is None or not _has(folder, kind):
        return None
    name = folder.name
    label = str(manifest.get("label") or "").strip()
    if not label:
        if kind == UFAK:
            label = "ufak"
        elif kind == MOONSHINE:
            size = str(manifest.get("size") or ("medium" if _MEDIUM.match(name) else "small"))
            label = f"Moonshine {size}"
        else:
            label = "FastConformer"
    if kind != UFAK and name not in label:
        label = f"{label} · {name}"
    return TrialModel(name, kind, label, folder)


def discover(root: Path) -> list[TrialModel]:
    """Every trial model in the AI captions folder `root`: ufak, the Moonshine exports (each
    again with the beam and the n-gram, when one is here), the streaming ones, each by its
    folder's name."""
    try:
        folders = [entry for entry in Path(root).iterdir() if entry.is_dir()]
    except OSError:
        return []
    found = [model for model in map(describe, folders) if model is not None]
    lm = find_lm(root) if any(model.kind == MOONSHINE for model in found) else None
    if lm is not None:
        found += [beam for beam in (_with_lm(model, lm) for model in found) if beam is not None]
    return sorted(found, key=lambda m: (_ORDER.index(m.kind), m.folder.name, m.decoding))
