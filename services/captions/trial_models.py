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
picked runs as it is, on Turkish: no language detection, no other model. No Qt; `discover`
reads a folder listing, a few stats and the small manifests (the GUI calls it from a thread).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    name: str  # its folder's name: what the config keeps
    kind: str  # UFAK, MOONSHINE or STREAM
    label: str  # as the list shows it: "Moonshine medium · tr-moonm-avg"
    folder: Path


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


def _kind_by_name(name: str) -> Optional[str]:
    if name == UFAK_FOLDER:
        return UFAK
    if name.startswith("tr-moon"):
        return MOONSHINE
    if name.startswith("tr-fc") or "stream" in name:
        return STREAM
    return None


def describe(folder: Path) -> Optional[TrialModel]:
    """The trial model in `folder`, or None (none, or its files are not all there)."""
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
    """Every trial model in the AI captions folder `root`: ufak, the Moonshine exports, the
    streaming ones, each by its folder's name."""
    try:
        folders = [entry for entry in Path(root).iterdir() if entry.is_dir()]
    except OSError:
        return []
    found = [model for model in map(describe, folders) if model is not None]
    return sorted(found, key=lambda m: (_ORDER.index(m.kind), m.name))
