"""What AI captions download, where it goes, and whether it's in place. No Qt.

Nothing ships with QiTV: the first time AI captions are turned on, the viewer downloads a
speech model tier, the Silero VAD and (unless this Python already has it) the sherpa-onnx
runtime, all pinned below by size and sha256. They live in `captions_root()`:

    ai-captions/
        runtime-2.0.1-cp314/   the sherpa-onnx and sherpa-onnx-core wheels, as pip lays them
                               out in site-packages (put on the captions process's sys.path)
        qwen3-asr-0.6b/        conv_frontend.onnx, encoder.int8.onnx, decoder.int8.onnx,
        qwen3-asr-1.7b/        tokenizer/ (both tiers alike)
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


def needed(tier: str, root: Optional[Path] = None) -> list[Download]:
    """Everything the tier needs to run, in download order: the runtime (unless this Python
    has one), the VAD, the tier's files. Unsupported without a runtime for this computer."""
    base = _root(root)
    downloads: list[Download] = []
    runtime = runtime_dir(base)
    if runtime is not None:
        main, core = runtime_files()
        downloads.append(Download(runtime, main, _RUNTIME_CONTENTS))
        downloads.append(Download(runtime, core))
    downloads.append(Download(base, VAD, ((VAD.path, VAD.size),)))
    info = TIERS[tier]
    folder = base / info.folder
    for file in info.files:
        if file.extract == EXTRACT_TAR_BZ2:
            downloads.append(Download(folder, file, info.contents))
        else:
            downloads.append(Download(folder, file, ((file.path, file.size),)))
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
    each file present at its size. False where no runtime suits this computer."""
    try:
        return not missing(tier, root)
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
