"""What AI captions download, where it goes, and whether it's in place. No Qt.

Nothing ships with QiTV: the first time AI captions are turned on, the viewer downloads a
speech model tier, the Silero VAD, the Turkish model (2.10: QiTV's own Turkish Moonshine medium,
278 MB, read with our beam search; 2.12.1: with its language model, 39 MB more; 2.9.2 to 2.9.10
used ufakzeka-kulak) and (unless this Python already has it) the sherpa-onnx runtime, all pinned
below by size and sha256. They live in `captions_root()`:

    ai-captions/
        runtime-2.0.1-cp314/   the sherpa-onnx and sherpa-onnx-core wheels, as pip lays them
                               out in site-packages (put on the captions process's sys.path)
        qwen3-asr-0.6b/        conv_frontend.onnx, encoder.int8.onnx, decoder.int8.onnx,
        qwen3-asr-1.7b/        tokenizer/ (both tiers alike)
        qitv-asr-tr-moonshine-mid/  encoder_model_quantized.onnx,
                               decoder_model_merged_quantized.onnx, tokens.txt (Moonshine v2 files)
            lm/lm.int8.onnx    (2.12.1) its LSTM language model (TURKISH_LM)
        qitv-mt-milmmt-46-1b/  model.onnx, model.onnx.data, tokenizer.json, config.json
                               (2.10: translation)
            site/              the tokenizers wheel, unpacked (put on sys.path by the translator)
        silero_vad.onnx
        verified.json          what was downloaded whole and checked (sha256), by path

A download is checked once, when it arrives; `installed()` then trusts verified.json and the
files' sizes. The functions here read the disk a little (a few stats and one small file).
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import importlib.util
import json
import logging
import os
from pathlib import Path
import platform
import shutil
import sys
import threading
from typing import Optional

from services.atomic_write import atomic_write

logger = logging.getLogger(__name__)

FOLDER = "ai-captions"
VERIFIED_FILE = "verified.json"
VERIFIED_VERSION = 1

SHERPA_VERSION = "2.0.1"  # the Qwen3-ASR tiers were tested on it
PYTHON_TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"

EXTRACT_TAR_BZ2 = "tar.bz2"  # into the folder, without the archive's top folder
EXTRACT_WHEEL = "wheel"  # a zip, into the runtime folder as pip would install it


class Unsupported(Exception):
    """No sherpa-onnx runtime is pinned for this system, processor or Python."""


@dataclass(frozen=True)
class RemoteFile:
    """One download. `path` is where it goes, relative to its target folder (an archive's
    name while it is downloaded; it's removed once extracted)."""

    url: str
    path: str
    size: int
    sha256: str
    extract: str = ""  # "", EXTRACT_TAR_BZ2 or EXTRACT_WHEEL


@dataclass(frozen=True)
class Tier:
    """A speech model size. `contents` is what an installed tier holds: (path in `folder`,
    size). `label` is the model's size, the same in every language ("0.6B")."""

    name: str
    folder: str
    files: tuple[RemoteFile, ...]
    contents: tuple[tuple[str, int], ...]
    label: str

    @property
    def download_bytes(self) -> int:
        return sum(file.size for file in self.files)


@dataclass(frozen=True)
class Download:
    """A file to fetch into `folder`, and what must be there once it's in place (path in
    `folder`, size or None for any size)."""

    folder: Path
    file: RemoteFile
    contents: tuple[tuple[str, Optional[int]], ...] = ()

    @property
    def target(self) -> Path:
        return self.folder / self.file.path

    def key(self, root: Path) -> str:
        """Its name in verified.json: the target's path under the captions folder."""
        return self.target.relative_to(root).as_posix()


@dataclass(frozen=True)
class ModelPaths:
    """What the captions process loads (sherpa-onnx's OfflineQwen3ASRModelConfig and the
    VAD's model)."""

    conv_frontend: Path
    encoder: Path
    decoder: Path
    tokenizer: Path  # a folder
    vad: Path
    turkish: Path  # the Turkish Moonshine's folder (TURKISH_FILES)
    european: Path  # 2.9.3: Parakeet TDT 0.6B v3's folder (EUROPEAN_FILES)
    cohere: Path  # 2.9.3: Cohere Transcribe's folder (COHERE_FILE, unpacked)
    translation: Path  # 2.10: the translator's folder (TRANSLATION_FILES, and site/)
    turkish_lm: Path  # 2.12.1: the Turkish Moonshine's language model (TURKISH_LM), in its folder


# ---- what is downloaded -------------------------------------------------------------------

_SHERPA_RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
_MODELSCOPE = "https://modelscope.cn/models/zengshuishui/Qwen3-ASR-onnx/resolve/master/"
_PYPI_FILES = "https://files.pythonhosted.org/packages/"

_SMALL_ARCHIVE = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2"
SKIPPED_IN_ARCHIVE = ("test_wavs",)  # the release's sample recordings: not needed

SMALL = Tier(
    name="small",
    folder="qwen3-asr-0.6b",
    files=(
        RemoteFile(
            _SHERPA_RELEASE + _SMALL_ARCHIVE,
            _SMALL_ARCHIVE,
            878702423,
            "393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96",
            EXTRACT_TAR_BZ2,
        ),
    ),
    # As the archive above unpacks (measured 2026-11-02).
    contents=(
        ("conv_frontend.onnx", 44148281),
        ("encoder.int8.onnx", 182491662),
        ("decoder.int8.onnx", 755914231),
        ("tokenizer/merges.txt", 1671853),
        ("tokenizer/tokenizer_config.json", 12487),
        ("tokenizer/vocab.json", 2776833),
    ),
    label="0.6B",
)


def _modelscope(path: str, size: int, sha256: str) -> RemoteFile:
    """A file of the ModelScope repository; model_1.7B/x lands as x, tokenizer/ as itself."""
    return RemoteFile(_MODELSCOPE + path, path.removeprefix("model_1.7B/"), size, sha256)


# Sizes and sha256 from ModelScope's API (repo/files?Recursive=true), 2026-11-03.
_LARGE_FILES = (
    _modelscope(
        "model_1.7B/conv_frontend.onnx",
        48080441,
        "fa894a4ba53da6a4238f2a6ca0b09362e505d39cecbd646051b033e2e8d7e2fb",
    ),
    _modelscope(
        "model_1.7B/encoder.int8.onnx",
        314222162,
        "436fbd910a0c8914851e5ac1354e807be9f283d08a5da728adaa609731c41469",
    ),
    _modelscope(
        "model_1.7B/decoder.int8.onnx",
        2037458645,
        "c43c853fa6e97d08365cb8a5502b360b595cd43c00dc60e4d8ca7cc18cad460b",
    ),
    _modelscope(
        "tokenizer/chat_template.json",
        1161,
        "75a8cfca24f00de72d796fbfed6858fc9614ef3dabd8696684cc3bc03a9c58ff",
    ),
    _modelscope(
        "tokenizer/config.json",
        6193,
        "76d3ae4601ce939830b2517f4a6cadb86cc51316c3900af6b020b051c21a478c",
    ),
    _modelscope(
        "tokenizer/merges.txt",
        1671853,
        "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    ),
    _modelscope(
        "tokenizer/preprocessor_config.json",
        330,
        "45e120a4eda2c20c5d7f2ea9354e63536bf35e27aa573fb7cdf78017b378770d",
    ),
    _modelscope(
        "tokenizer/tokenizer_config.json",
        12487,
        "4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c",
    ),
    _modelscope(
        "tokenizer/vocab.json",
        2776833,
        "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    ),
)

LARGE = Tier(
    name="large",
    folder="qwen3-asr-1.7b",
    files=_LARGE_FILES,
    contents=tuple((file.path, file.size) for file in _LARGE_FILES),
    label="1.7B",
)

TIERS: dict[str, Tier] = {SMALL.name: SMALL, LARGE.name: LARGE}

VAD = RemoteFile(
    _SHERPA_RELEASE + "silero_vad.onnx",
    "silero_vad.onnx",
    643854,
    "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
)

# 2.10: QiTV's own Turkish model, qitv-asr-tr-moonshine-mid (CC BY 4.0; Moonshine Streaming Medium
# trained further on Turkish), its 8-bit sherpa-onnx Moonshine v2 files, read with our beam search
# (services.captions.moon_beam). Its top-level config.json comes too (1.8 KB, unused here): the Hub
# counts a model's downloads by that file. Revision d81f33cb stores the decoder's weights unsigned,
# so processors without VNNI read it exactly (the signed int8 weights looped on AVX2 kernels).
_TURKISH_REVISION = (
    "https://huggingface.co/ozankaraali/qitv-asr-tr-moonshine-mid/resolve/"
    "d81f33cbdc992b78b7dd9e4a949f2862bd47f62c/"
)
_TURKISH_REPO = _TURKISH_REVISION + "onnx/"
TURKISH_FOLDER = "qitv-asr-tr-moonshine-mid"
TURKISH_FILES = (
    RemoteFile(
        _TURKISH_REPO + "encoder_model_quantized.onnx",
        "encoder_model_quantized.onnx",
        142363625,
        "39e7e9a74bbe8152021fa8a926c8a51845546359e31984a903314ec4fe0ea920",
    ),
    RemoteFile(
        _TURKISH_REPO + "decoder_model_merged_quantized.onnx",
        "decoder_model_merged_quantized.onnx",
        135617948,
        "0e90f98f754053dd13cc39594f64a85ec92cce71ef5c8e1063b2caddb7b46322",
    ),
    RemoteFile(
        _TURKISH_REPO + "tokens.txt",
        "tokens.txt",
        179159,
        "8766e1592373d47ea3f7e52975840a96c51f8d0b4b8619c7bdb951f650eb21ba",
    ),
    RemoteFile(
        _TURKISH_REVISION + "config.json",
        "config.json",
        1856,
        "0a8950a8ea8b41c43413921beff70f8d9655278fa9ad2cb07dc5609e138c604c",
    ),
)
# 2.12.1: its language model, an LSTM over the model's pieces with 8-bit weights, trained on
# Turkish text and then TV transcripts; our beam reads Turkish with it (services.captions.
# moon_reading). It is in the same repository (lm/), added in revision ed8d5011 (the model's
# files, pinned at d81f33cb above, are the same there). It downloads with the model (last, and
# counted in what the question says), but the model runs without it: installed() doesn't wait for
# it, and an install from before it gets it alone (TURKISH_LM_KEY).
_TURKISH_LM_REVISION = (
    "https://huggingface.co/ozankaraali/qitv-asr-tr-moonshine-mid/resolve/"
    "ed8d50119886bd96f0437d49b84b58551ddb886d/"
)
TURKISH_LM = RemoteFile(
    _TURKISH_LM_REVISION + "lm/lm.int8.onnx",
    "lm/lm.int8.onnx",
    38648607,
    "440f7dba99f592de1b1ce7d47e28bd5d68652ae606cc70fd94c7c5bddb13bbd4",
)


# 2.9.3: NVIDIA's Parakeet TDT 0.6B v3 (CC-BY-4.0), 25 European languages, sherpa-onnx's int8
# export. Sizes and sha256 as Hugging Face served them on 2026-11-04 (revision 2bda32ec, the
# files of sherpa-onnx's asr-models release).
_PARAKEET = (
    "https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/resolve/"
    "2bda32ec70b097a55adaa07d9a7173915b43cc78/"
)
EUROPEAN_FOLDER = "parakeet-tdt-0.6b-v3"
EUROPEAN_FILES = (
    RemoteFile(
        _PARAKEET + "encoder.int8.onnx",
        "encoder.int8.onnx",
        652184281,
        "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247",
    ),
    RemoteFile(
        _PARAKEET + "decoder.int8.onnx",
        "decoder.int8.onnx",
        11845275,
        "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e",
    ),
    RemoteFile(
        _PARAKEET + "joiner.int8.onnx",
        "joiner.int8.onnx",
        6355277,
        "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3",
    ),
    RemoteFile(
        _PARAKEET + "tokens.txt",
        "tokens.txt",
        93939,
        "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d",
    ),
)


# 2.9.3: Cohere Labs' Cohere Transcribe (Apache-2.0), 14 languages, sherpa-onnx's int8 export,
# one archive of its asr-models release. Size and sha256 as GitHub gives them (2026-11-04).
_COHERE_ARCHIVE = "sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01.tar.bz2"
COHERE_FOLDER = "cohere-transcribe"
COHERE_FILE = RemoteFile(
    _SHERPA_RELEASE + _COHERE_ARCHIVE,
    _COHERE_ARCHIVE,
    1699791751,
    "bd582588d50685a795dcd2807ab77e11361b8312d96c53884682def45ab4206d",
    EXTRACT_TAR_BZ2,
)
# As the archive above unpacks (measured 2026-11-04).
COHERE_CONTENTS: tuple[tuple[str, Optional[int]], ...] = (
    ("encoder.int8.onnx", 3090822),
    ("encoder.int8.onnx.data", 2731503072),
    ("decoder.int8.onnx", 153250705),
    ("tokens.txt", 207437),
)


# 2.10: caption translation, MiLMMT-46-1B (Xiaomi) as an int4 ONNX export with the greedy pick
# inside the graph. Sizes and sha256 as Hugging Face serves them (revision 73356cb2). Its top-level
# config.json comes too (1.5 KB, unused here): the Hub counts a model's downloads by that file.
_TRANSLATION_REPO = (
    "https://huggingface.co/ozankaraali/qitv-mt-milmmt-46-1b/resolve/"
    "73356cb2ee11aeb7be2b2bbe14e37126612bc66b/"
)
TRANSLATION_FOLDER = "qitv-mt-milmmt-46-1b"
TRANSLATION_FILES = (
    RemoteFile(
        _TRANSLATION_REPO + "model.onnx",
        "model.onnx",
        295402,
        "e8652dbcd810fb2026255996547c64a2e872f32dcdc290f2f478e3806a4eae30",
    ),
    RemoteFile(
        _TRANSLATION_REPO + "model.onnx.data",
        "model.onnx.data",
        692490752,
        "48ed68e06bc41cd138e7bc5d513b33c1461d735e1dcd5a50d58d057d0eaa6f3d",
    ),
    RemoteFile(
        _TRANSLATION_REPO + "tokenizer.json",
        "tokenizer.json",
        33384567,
        "daab2354f8a74e70d70b4d1f804939b68a8c9624dd06cb7858e52dd8970e9726",
    ),
    RemoteFile(
        _TRANSLATION_REPO + "config.json",
        "config.json",
        1538,
        "221aeaf24f0e52f590f1fa5555366edd78906c75ef79b8bddd2fa192d358f904",
    ),
)


def _wheel(folder: str, name: str, size: int, sha256: str) -> RemoteFile:
    return RemoteFile(f"{_PYPI_FILES}{folder}/{name}", name, size, sha256, EXTRACT_WHEEL)


# The sherpa-onnx and sherpa-onnx-core 2.0.1 wheels for CPython 3.14, from PyPI's JSON API
# (pypi.org/pypi/<name>/2.0.1/json: urls[].url, size, digests.sha256), 2026-11-03.
_WHEELS: dict[tuple[str, str], tuple[RemoteFile, RemoteFile]] = {
    ("darwin", "x86_64"): (
        _wheel(
            "22/d2/a143da440aed17c3199b41f8eb6290c0f4e883d3a7bfdfd174be529c8950",
            "sherpa_onnx-2.0.1-cp314-cp314-macosx_10_15_x86_64.whl",
            2285637,
            "fa39b674e9c4b8de4a04a12de7e5119fe378f5a3abb76cfd897bc35288a08366",
        ),
        _wheel(
            "6d/69/5e25f0c3aa933d679b0e377516bf9b5649cbc892d688a504096cd68963e6",
            "sherpa_onnx_core-2.0.1-py3-none-macosx_10_15_x86_64.whl",
            10842438,
            "fe43986eff8ccdfbb7fc851ee1b40e6df9869ff14da2de6b70302872e555480d",
        ),
    ),
    ("darwin", "arm64"): (
        _wheel(
            "29/e8/fbaffc8f1965e68812137c9ddade41ee9951c1547d21b3084b42d46da822",
            "sherpa_onnx-2.0.1-cp314-cp314-macosx_11_0_arm64.whl",
            2087413,
            "26ba1d3e9ddd43853fb021999fe83648358fc6d61cd48f8150beb8c3bbc0ed10",
        ),
        _wheel(
            "ec/81/5d7f89b4c3dc64e9100590609d5c168448fc379d89a08c6498aad46b6feb",
            "sherpa_onnx_core-2.0.1-py3-none-macosx_11_0_arm64.whl",
            9518462,
            "82ab505a835e616f568ba6804555969e1b5699341d2b18eb27f0083b068b61ff",
        ),
    ),
    ("win32", "x86_64"): (
        _wheel(
            "26/b0/0d0609ef69dfd690b9b470024eedf0b418262b0d0ef465b98504854e4316",
            "sherpa_onnx-2.0.1-cp314-cp314-win_amd64.whl",
            2241664,
            "4aa2668fa24aa3223bae9d1363aa3274ef2201f2f1560542b6d3c282130a3831",
        ),
        _wheel(
            "68/ea/8f02cc34c19d956697dda2f2c470c14cd6b7af77f3ae855c514a86039b68",
            "sherpa_onnx_core-2.0.1-py3-none-win_amd64.whl",
            16663244,
            "08890e9ea23ed2f59f81d5840c63b6b5cc8674d0f6e48d9069126a848aa0e022",
        ),
    ),
    ("linux", "x86_64"): (
        _wheel(
            "f1/cd/b4b4acf8dec04661bd616f430d76a4043734dc6acf1888be354d1ced58d3",
            "sherpa_onnx-2.0.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
            4256828,
            "634b9dbdc7f789efa4a3273f8fb94ab0480c9cd5e5be0ca2d9a7f0f090c7da3d",
        ),
        _wheel(
            "9a/4e/d946cc081864c8c242e787a73dd2ea61f861445f3d4207787d873e4dc65d",
            "sherpa_onnx_core-2.0.1-py3-none-manylinux2014_x86_64.whl",
            10544878,
            "f4ee0ab91c4ae596e0e49d7de32522a946295e038a652f1c1884d89014ee4ec2",
        ),
    ),
}
_WHEEL_PYTHON_TAG = "cp314"
# The tokenizers 0.23.3 wheels the translator imports: abi3 (cp310 and later, so any Python that
# runs QiTV), one per system `_WHEELS` covers, from PyPI's JSON API
# (pypi.org/pypi/tokenizers/0.23.3/json: urls[].url, size, digests.sha256), 2026-11-15.
_TOKENIZERS_WHEELS: dict[tuple[str, str], RemoteFile] = {
    ("darwin", "x86_64"): _wheel(
        "aa/2e/4ce5b9716f26e526eff6b0502ebed4ea8d7161f03b3c77617c9f25528e97",
        "tokenizers-0.23.3-cp310-abi3-macosx_10_12_x86_64.whl",
        3148800,
        "9d2b5c97daf61688c2ad1803ca851800feaba50fb68d5821779e9ea5880d968c",
    ),
    ("darwin", "arm64"): _wheel(
        "b2/72/01e49f032bb346e5aaf06c10c74fe8aeec847173adbadd66eb7c53054bf2",
        "tokenizers-0.23.3-cp310-abi3-macosx_11_0_arm64.whl",
        3101381,
        "68649e97d5b43c44c031d8d848874a6eecae8f8fe40ea989aa777a5a83aca716",
    ),
    ("win32", "x86_64"): _wheel(
        "d1/6c/24f074c9a0efb98e61b20aafe6b2641922d5db24e447d5d6daffd9e17555",
        "tokenizers-0.23.3-cp310-abi3-win_amd64.whl",
        2872101,
        "1f0823bb00c5fdc98e487354d54dd55a03848d61a1a0bf29a68c77f24f3b26c3",
    ),
    ("linux", "x86_64"): _wheel(
        "3a/40/6706b82693715581457c6d5423eaa7faae576bb0526c5738a57085eb4449",
        "tokenizers-0.23.3-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        3396193,
        "376851d22bcf9d650a5c3090bb83e6cf9e895fbf0595369fa4cd43c1f69b5f87",
    ),
}
_TOKENIZERS_CONTENTS: tuple[tuple[str, Optional[int]], ...] = (("tokenizers/__init__.py", None),)
_MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "x64": "x86_64", "arm64": "arm64"}
# What the main wheel holds once extracted (the core wheel's libraries differ by system).
_RUNTIME_CONTENTS: tuple[tuple[str, Optional[int]], ...] = (("sherpa_onnx/__init__.py", None),)


def runtime_files(
    sys_platform: str = sys.platform,
    machine: str = platform.machine(),
    python_tag: str = PYTHON_TAG,
) -> tuple[RemoteFile, ...]:
    """The sherpa-onnx wheels for a system (`sys.platform`), a processor
    (`platform.machine()`) and a Python ("cp314"); Unsupported when none is pinned."""
    system = "linux" if sys_platform.startswith("linux") else sys_platform
    wheels = _WHEELS.get((system, _MACHINES.get(machine.lower(), machine.lower())))
    if wheels is None or python_tag != _WHEEL_PYTHON_TAG:
        raise Unsupported(f"no sherpa-onnx runtime for {sys_platform} {machine} {python_tag}")
    return wheels


def tokenizers_file(
    sys_platform: str = sys.platform, machine: str = platform.machine()
) -> RemoteFile:
    """The tokenizers wheel for a system and a processor; Unsupported when none is pinned."""
    system = "linux" if sys_platform.startswith("linux") else sys_platform
    wheel = _TOKENIZERS_WHEELS.get((system, _MACHINES.get(machine.lower(), machine.lower())))
    if wheel is None:
        raise Unsupported(f"no tokenizers wheel for {sys_platform} {machine}")
    return wheel


def runtime_folder_name(python_tag: str = PYTHON_TAG) -> str:
    """The runtime's folder, named for its version and Python: another QiTV Python, or a later
    sherpa-onnx, downloads its own beside it."""
    return f"runtime-{SHERPA_VERSION}-{python_tag}"


# ---- where ------------------------------------------------------------------------------


def captions_root() -> Path:
    """`<config dir>/ai-captions` (QITV_CONFIG_DIR, the portable folder or the system's), or
    QITV_CAPTIONS_DIR: AI captions' files kept elsewhere (a check that reuses a download)."""
    elsewhere = os.environ.get("QITV_CAPTIONS_DIR", "")
    if elsewhere:
        return Path(elsewhere)
    from services.config_manager import ConfigManager

    # Where config.json goes depends on nothing a ConfigManager loads: no config is read.
    config_path = ConfigManager.__new__(ConfigManager)._get_config_path()
    return Path(config_path).parent / FOLDER


def _root(root: Optional[Path]) -> Path:
    return captions_root() if root is None else Path(root)


@functools.cache
def sherpa_importable() -> bool:
    """This Python imports sherpa_onnx already (a source run with it installed)."""
    try:
        return importlib.util.find_spec("sherpa_onnx") is not None
    except ImportError, ValueError:
        return False


@functools.cache
def tokenizers_importable() -> bool:
    """This Python imports `tokenizers` already (a source run with it installed)."""
    try:
        return importlib.util.find_spec("tokenizers") is not None
    except ImportError, ValueError:
        return False


def runtime_dir(root: Optional[Path] = None) -> Optional[Path]:
    """The folder to put on the captions process's sys.path; None when this Python imports
    sherpa_onnx already."""
    if sherpa_importable():
        return None
    return _root(root) / runtime_folder_name()


def available() -> bool:
    """AI captions can run here: a runtime is pinned for this computer, or Python has one."""
    if sherpa_importable():
        return True
    try:
        runtime_files()
    except Unsupported:
        return False
    return True


def model_paths(tier: str, root: Optional[Path] = None) -> ModelPaths:
    base = _root(root)
    folder = base / TIERS[tier].folder
    return ModelPaths(
        conv_frontend=folder / "conv_frontend.onnx",
        encoder=folder / "encoder.int8.onnx",
        decoder=folder / "decoder.int8.onnx",
        tokenizer=folder / "tokenizer",
        vad=base / VAD.path,
        turkish=base / TURKISH_FOLDER,
        european=base / EUROPEAN_FOLDER,
        cohere=base / COHERE_FOLDER,
        translation=base / TRANSLATION_FOLDER,
        turkish_lm=base / TURKISH_FOLDER / TURKISH_LM.path,
    )


# ---- what is in place -------------------------------------------------------------------

_verified_lock = threading.Lock()


def read_verified(root: Optional[Path] = None) -> dict[str, str]:
    """verified.json's records: a download's key → the sha256 it was checked against."""
    path = _root(root) / VERIFIED_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("The AI captions record could not be read: %s", e)
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(key): str(value) for key, value in files.items()}


def mark_verified(root: Path, key: str, sha256: str) -> None:
    """Record a download as whole and checked (written whole, never half)."""
    with _verified_lock:
        files = read_verified(root)
        files[key] = sha256
        root.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": VERIFIED_VERSION, "files": files}, indent=1, sort_keys=True)
        with atomic_write(str(root / VERIFIED_FILE)) as f:
            f.write(body.encode("utf-8"))


def has_contents(folder: Path, contents: tuple[tuple[str, Optional[int]], ...]) -> bool:
    """Each (path, size) is in `folder`, at that size (None: any)."""
    for path, size in contents:
        try:
            found = (folder / path).stat()
        except OSError:
            return False
        if size is not None and found.st_size != size:
            return False
    return True


# 2.9.6: the specialists, each downloaded once a language it writes is wanted (the owner: the
# question said 43 MB, 832 MB or 2.3 GB, and all 3.3-4.8 GB came at once, whatever was watched)
TURKISH_KEY, EUROPEAN_KEY, COHERE_KEY = "turkish", "european", "cohere"
SPECIALISTS = (TURKISH_KEY, EUROPEAN_KEY, COHERE_KEY)
# 2.10: caption translation, downloaded once translating is wanted (not a speech specialist:
# `full_size` leaves it out)
TRANSLATION_KEY = "translation"
# 2.12.1: the Turkish model's language model alone (TURKISH_LM: an install from before it)
TURKISH_LM_KEY = "turkish_lm"
# 2.12.1: what a specialist's download brings beside its model, which the model runs without:
# `needed` lists it after the model's files, `installed` doesn't wait for it
EXTRAS = {TURKISH_KEY: TURKISH_LM_KEY}


def needed(key: str, root: Optional[Path] = None) -> list[Download]:
    """What `key` needs to run, in download order. A tier ("small", "large"): the runtime
    (unless this Python has one), the VAD and Qwen's files, what captions need to start. A
    specialist (`SPECIALISTS`): its own files, then its `EXTRAS`'. Unsupported without a runtime
    for this computer."""
    base = _root(root)
    if key == TRANSLATION_KEY:
        return _translation(base)
    if key == TURKISH_LM_KEY:
        return [Download(base / TURKISH_FOLDER, TURKISH_LM, ((TURKISH_LM.path, TURKISH_LM.size),))]
    if key in SPECIALISTS:
        extra = EXTRAS.get(key)
        return _specialist(key, base) + (needed(extra, base) if extra else [])
    downloads: list[Download] = []
    runtime = runtime_dir(base)
    if runtime is not None:
        main, core = runtime_files()
        downloads.append(Download(runtime, main, _RUNTIME_CONTENTS))
        downloads.append(Download(runtime, core))
    downloads.append(Download(base, VAD, ((VAD.path, VAD.size),)))
    info = TIERS[key]
    folder = base / info.folder
    for file in info.files:
        if file.extract == EXTRACT_TAR_BZ2:
            downloads.append(Download(folder, file, info.contents))
        else:
            downloads.append(Download(folder, file, ((file.path, file.size),)))
    return downloads


def full_size(tier: str, root: Optional[Path] = None) -> int:
    """Bytes the tier and every language's own model take in all, here or not (what the first
    question says AI captions may come to). Unsupported as `needed`."""
    keys = (tier, *SPECIALISTS)
    return sum(d.file.size for key in keys for d in needed(key, root))


def _specialist(key: str, base: Path) -> list[Download]:
    if key == COHERE_KEY:
        return [Download(base / COHERE_FOLDER, COHERE_FILE, COHERE_CONTENTS)]
    folder, files = (
        (TURKISH_FOLDER, TURKISH_FILES) if key == TURKISH_KEY else (EUROPEAN_FOLDER, EUROPEAN_FILES)
    )
    return [Download(base / folder, file, ((file.path, file.size),)) for file in files]


def _translation(base: Path) -> list[Download]:
    """The model's files and, unless this Python imports tokenizers, its wheel unpacked into
    the model's `site` folder (the translator puts that on sys.path); Unsupported when no wheel
    suits this computer."""
    folder = base / TRANSLATION_FOLDER
    downloads = [Download(folder, file, ((file.path, file.size),)) for file in TRANSLATION_FILES]
    if not tokenizers_importable():
        downloads.append(Download(folder / "site", tokenizers_file(), _TOKENIZERS_CONTENTS))
    return downloads


def missing(tier: str, root: Optional[Path] = None) -> list[Download]:
    """What a download of the tier must still fetch (nothing once `installed`); Unsupported
    as `needed`."""
    base = _root(root)
    verified = read_verified(base)
    return [
        download
        for download in needed(tier, base)
        if verified.get(download.key(base)) != download.file.sha256
        or not has_contents(download.folder, download.contents)
    ]


def installed(tier: str, root: Optional[Path] = None) -> bool:
    """The tier, the VAD and the runtime are in place: each downloaded whole, checked, and
    each file present at its size. False where no runtime suits this computer. A specialist
    is in place without its `EXTRAS` (2.12.1: the Turkish model without its LM)."""
    try:
        left = missing(tier, root)
        extra = EXTRAS.get(tier)
        if left and extra is not None:
            beside = needed(extra, root)
            left = [download for download in left if download not in beside]
        return not left
    except Unsupported:
        return False


def download_size(tier: str, root: Optional[Path] = None) -> int:
    """Bytes the tier's download still fetches (what the question card says): a file already
    here whole (an archive a cancel left before it was unpacked) or in part (its ".part", which
    resumes) isn't fetched again. 0 says nothing is fetched, not that the tier is `installed`.
    Unsupported as `needed`."""
    total = 0
    for download in missing(tier, root):
        size, target = download.file.size, download.target
        have = _file_size(target)
        if have != size:
            have = min(size, _file_size(target.with_name(target.name + ".part")))
        total += size - have
    return total


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def space_used(root: Optional[Path] = None) -> int:
    """Bytes AI captions take on disk, unfinished downloads included (not the CPU score the
    tier keeps there: nothing downloaded, nothing for Settings to tell or delete)."""
    total = 0
    base = _root(root)
    for folder, _dirs, files in os.walk(base):
        for name in files:
            if name == "speed.json" and Path(folder) == base:
                continue
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                pass
    return total


def delete_all(root: Optional[Path] = None) -> None:
    """Remove everything AI captions downloaded (call it while nothing downloads or runs)."""
    base = _root(root)
    with _verified_lock:
        shutil.rmtree(base, ignore_errors=True)
    if base.exists():
        logger.warning("Some AI captions files could not be deleted")
