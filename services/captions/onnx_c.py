"""A minimal ctypes binding to onnxruntime's C API, for the onnxruntime library that the
sherpa-onnx runtime already carries: there is no onnxruntime wheel for every machine QiTV runs on
(none for Intel Macs since 1.24, none for Python 3.14 there at all), and no numpy in the captions
runtime. Only what running a model on the CPU takes: sessions from model files, tensors over
Python-owned buffers, Run, and an output's shape and data. Every call's OrtStatus is checked; a
failure raises OrtError with onnxruntime's own message.

The function table: `OrtGetApiBase()->GetApi(API_VERSION)` gives a `const OrtApi*`, a struct of
function pointers that only ever grows at its end (a library serves every version from 1 to its
own), so an early function's slot never moves. The slots below were counted from `struct OrtApi`
in include/onnxruntime/core/session/onnxruntime_c_api.h at tag v1.28.2 (ORT_API_VERSION 28) and
checked against the C# binding's copy of that struct (csharp/src/Microsoft.ML.OnnxRuntime/
NativeMethods.shared.cs, same tag): all of them are among the first 131, long in API 17.

Lifetimes: a tensor made over a Python buffer keeps that buffer (and a buffer export on it, so an
array can't be resized under onnxruntime) until the tensor is released; an output keeps its
session alive; everything keeps the `Api` (its library, environment and memory info) alive.
"""

from __future__ import annotations

from array import array
import ctypes
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int,
    c_int64,
    c_size_t,
    c_uint32,
    c_void_p,
    c_wchar_p,
)
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

API_VERSION = 17  # onnxruntime 1.17 and later serve it; every slot used here is older

# ONNXTensorElementDataType
FLOAT = 1
INT64 = 7
BOOL = 9
_ITEM_SIZE = {FLOAT: 4, INT64: 8, BOOL: 1}

# GraphOptimizationLevel
DISABLE_ALL = 0
ENABLE_BASIC = 1
ENABLE_EXTENDED = 2
ENABLE_ALL = 99  # onnxruntime's own default, and sherpa-onnx's

_SEQUENTIAL = 0  # ExecutionMode
_LOG_ERROR = 3  # OrtLoggingLevel: sherpa-onnx's level
_DEVICE_ALLOCATOR = 0  # OrtAllocatorType
_MEM_DEFAULT = 0  # OrtMemType

_WINDOWS = sys.platform == "win32"
# ORT_API_CALL is __stdcall on Windows (on 64-bit Windows the same as the C convention).
_FUNC: Any = getattr(ctypes, "WINFUNCTYPE") if _WINDOWS else ctypes.CFUNCTYPE
_LOADER: Any = getattr(ctypes, "WinDLL") if _WINDOWS else ctypes.CDLL
_PATH: Any = c_wchar_p if _WINDOWS else c_char_p  # ORTCHAR_T: wchar_t on Windows, else char

_S = c_void_p  # OrtStatus*: NULL is success
_PV = POINTER(c_void_p)
_PSIZE = POINTER(c_size_t)

# name: (slot in struct OrtApi, return type, argument types)
_TABLE: dict[str, tuple[int, Any, tuple[Any, ...]]] = {
    "GetErrorCode": (1, c_int, (c_void_p,)),
    "GetErrorMessage": (2, c_char_p, (c_void_p,)),
    "CreateEnv": (3, _S, (c_int, c_char_p, _PV)),
    "CreateSession": (7, _S, (c_void_p, _PATH, c_void_p, _PV)),
    "Run": (
        9,
        _S,
        (c_void_p, c_void_p, POINTER(c_char_p), _PV, c_size_t, POINTER(c_char_p), c_size_t, _PV),
    ),
    "CreateSessionOptions": (10, _S, (_PV,)),
    "SetSessionExecutionMode": (13, _S, (c_void_p, c_int)),
    "SetSessionGraphOptimizationLevel": (23, _S, (c_void_p, c_int)),
    "SetIntraOpNumThreads": (24, _S, (c_void_p, c_int)),
    "SetInterOpNumThreads": (25, _S, (c_void_p, c_int)),
    "SessionGetInputCount": (30, _S, (c_void_p, _PSIZE)),
    "SessionGetOutputCount": (31, _S, (c_void_p, _PSIZE)),
    "SessionGetInputTypeInfo": (33, _S, (c_void_p, c_size_t, _PV)),
    "SessionGetInputName": (36, _S, (c_void_p, c_size_t, c_void_p, _PV)),
    "SessionGetOutputName": (37, _S, (c_void_p, c_size_t, c_void_p, _PV)),
    "CreateTensorWithDataAsOrtValue": (
        49,
        _S,
        (c_void_p, c_void_p, c_size_t, POINTER(c_int64), c_size_t, c_int, _PV),
    ),
    "GetTensorMutableData": (51, _S, (c_void_p, _PV)),
    "CastTypeInfoToTensorInfo": (55, _S, (c_void_p, _PV)),
    "GetTensorElementType": (60, _S, (c_void_p, POINTER(c_int))),
    "GetDimensionsCount": (61, _S, (c_void_p, _PSIZE)),
    "GetDimensions": (62, _S, (c_void_p, POINTER(c_int64), c_size_t)),
    "GetTensorTypeAndShape": (65, _S, (c_void_p, _PV)),
    "CreateCpuMemoryInfo": (69, _S, (c_int, c_int, _PV)),
    "AllocatorFree": (76, _S, (c_void_p, c_void_p)),
    "GetAllocatorWithDefaultOptions": (78, _S, (_PV,)),
    "ReleaseEnv": (92, None, (c_void_p,)),
    "ReleaseStatus": (93, None, (c_void_p,)),
    "ReleaseMemoryInfo": (94, None, (c_void_p,)),
    "ReleaseSession": (95, None, (c_void_p,)),
    "ReleaseValue": (96, None, (c_void_p,)),
    "ReleaseTypeInfo": (98, None, (c_void_p,)),
    "ReleaseTensorTypeAndShapeInfo": (99, None, (c_void_p,)),
    "ReleaseSessionOptions": (100, None, (c_void_p,)),
    "AddSessionConfigEntry": (130, _S, (c_void_p, c_char_p, c_char_p)),
}
_SLOTS = 1 + max(slot for slot, _, _ in _TABLE.values())


class _ApiBase(Structure):
    _fields_ = [
        ("GetApi", _FUNC(c_void_p, c_uint32)),
        ("GetVersionString", _FUNC(c_char_p)),
    ]


_EMPTY = (c_char * 16)()  # where a zero-length tensor points: never read, never NULL

_LIBRARY_NAMES = {
    "win32": ("onnxruntime.dll",),
    "darwin": ("libonnxruntime.dylib",),
}
_LIBRARY_GLOBS = {"win32": "onnxruntime*.dll", "darwin": "libonnxruntime*.dylib"}


class OrtError(RuntimeError):
    """An onnxruntime call failed; `code` is its OrtErrorCode (0 when not from onnxruntime)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


def find_library(explicit: Union[str, os.PathLike[str], None] = None) -> Path:
    """The onnxruntime shared library: `explicit` if given, else the one in the imported
    sherpa_onnx package's lib folder (its import is the caller's: the runtime on sys.path)."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"no onnxruntime library at {path}")
        return path
    import sherpa_onnx

    folder = Path(sherpa_onnx.__file__).parent / "lib"
    for name in _LIBRARY_NAMES.get(sys.platform, ("libonnxruntime.so",)):
        if (folder / name).is_file():
            return folder / name
    found = sorted(folder.glob(_LIBRARY_GLOBS.get(sys.platform, "libonnxruntime.so*")))
    if found:
        return found[0]
    raise FileNotFoundError(f"no onnxruntime library in {folder}")


class Api:
    """The library's C API table, one OrtEnv and a CPU memory info. Loading the very file the
    sherpa-onnx package loaded gives the same library, and onnxruntime keeps one environment per
    process (CreateEnv hands out the existing one, counted)."""

    def __init__(self, library: Union[str, os.PathLike[str]]) -> None:
        self.env: Optional[int] = None
        self.memory_info: Optional[int] = None
        self._lib = _LOADER(str(library))
        get_base = self._lib.OrtGetApiBase
        get_base.restype = POINTER(_ApiBase)
        get_base.argtypes = ()
        base = get_base()
        if not base:
            raise OrtError(f"{library}: OrtGetApiBase gave nothing")
        self.version = (base.contents.GetVersionString() or b"?").decode("ascii", "replace")
        table = base.contents.GetApi(API_VERSION)
        if not table:
            raise OrtError(f"onnxruntime {self.version} does not serve C API {API_VERSION}")
        slots = ctypes.cast(table, POINTER(c_void_p * _SLOTS)).contents
        functions = {}
        for name, (slot, restype, argtypes) in _TABLE.items():
            address = slots[slot]
            if not address:
                raise OrtError(f"onnxruntime {self.version}: no {name}")
            functions[name] = _FUNC(restype, *argtypes)(address)
        self.fn: Any = SimpleNamespace(**functions)
        out = c_void_p()
        self.check(self.fn.CreateEnv(_LOG_ERROR, b"qitv", byref(out)))
        self.env = out.value
        out = c_void_p()
        self.check(self.fn.CreateCpuMemoryInfo(_DEVICE_ALLOCATOR, _MEM_DEFAULT, byref(out)))
        self.memory_info = out.value
        out = c_void_p()
        self.check(self.fn.GetAllocatorWithDefaultOptions(byref(out)))
        self.allocator = out.value  # onnxruntime's own: never released
        logger.debug("onnxruntime %s through its C API (version %d)", self.version, API_VERSION)

    def check(self, status: Optional[int]) -> None:
        """Raises OrtError for a failed call's status (and releases it)."""
        if status:
            fn = self.fn
            try:
                message = (fn.GetErrorMessage(status) or b"").decode("utf-8", "replace")
                code = int(fn.GetErrorCode(status))
            finally:
                fn.ReleaseStatus(status)
            raise OrtError(message, code)

    def tensor(self, data: Any, shape: Sequence[int], element_type: int) -> Value:
        """A tensor over `data`'s own memory (no copy): a writable buffer (array, bytearray,
        ctypes array) of exactly the shape's size. The tensor keeps the buffer until released;
        what's written into the buffer meanwhile is what the next Run reads."""
        count = 1
        for dim in shape:
            if dim < 0:
                raise ValueError(f"a tensor's shape has no unknown sizes: {list(shape)}")
            count *= dim
        size = count * _ITEM_SIZE[element_type]
        with memoryview(data) as view:
            if view.nbytes != size:
                raise ValueError(f"{view.nbytes} bytes for a tensor of {size}: {list(shape)}")
            if size and view.readonly:
                raise ValueError("a tensor's buffer must be writable")
        keep: Any = None
        if size:
            holder = (c_char * size).from_buffer(data)  # holds an export: no resizing meanwhile
            address = ctypes.addressof(holder)
            keep = (data, holder)
        else:
            address = ctypes.addressof(_EMPTY)
        dims = (c_int64 * len(shape))(*shape)  # onnxruntime copies the shape
        out = c_void_p()
        self.check(
            self.fn.CreateTensorWithDataAsOrtValue(
                self.memory_info, address, size, dims, len(shape), element_type, byref(out)
            )
        )
        return Value(self, out.value, keep)

    def close(self) -> None:
        """Releases the memory info and this Api's hold on the environment (after every session
        and tensor made from it is released)."""
        fn = getattr(self, "fn", None)
        if fn is None:
            return
        if self.memory_info:
            fn.ReleaseMemoryInfo(self.memory_info)
            self.memory_info = None
        if self.env:
            fn.ReleaseEnv(self.env)
            self.env = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # at interpreter exit ctypes may be half gone
            pass


class Value:
    """An OrtValue, opaque: a session's output can go straight back in as the next Run's input.
    Release it when done (or let it be collected)."""

    __slots__ = ("ptr", "_api", "_keep", "_shape", "_type")

    def __init__(self, api: Api, ptr: Optional[int], keep: Any = None) -> None:
        self.ptr = ptr
        self._api = api
        self._keep = keep  # the Python buffer under it, or the session it came out of
        self._shape: Optional[tuple[int, ...]] = None
        self._type = 0

    def _info(self) -> None:
        fn, check = self._api.fn, self._api.check
        if not self.ptr:
            raise OrtError("the value was released")
        info = c_void_p()
        check(fn.GetTensorTypeAndShape(self.ptr, byref(info)))
        try:
            self._shape, self._type = _shape_and_type(self._api, info.value)
        finally:
            fn.ReleaseTensorTypeAndShapeInfo(info)

    @property
    def shape(self) -> tuple[int, ...]:
        if self._shape is None:
            self._info()
        assert self._shape is not None
        return self._shape

    @property
    def element_type(self) -> int:
        if self._shape is None:
            self._info()
        return self._type

    def address(self) -> int:
        """Where its data starts (valid while the value lives)."""
        if not self.ptr:
            raise OrtError("the value was released")
        out = c_void_p()
        self._api.check(self._api.fn.GetTensorMutableData(self.ptr, byref(out)))
        return out.value or 0

    def floats(self, start: int = 0, count: Optional[int] = None) -> array:
        """A float32 tensor's elements (flat, row-major) from `start`, copied into an array."""
        if self.element_type != FLOAT:
            raise OrtError(f"not a float tensor (element type {self._type})")
        total = 1
        for dim in self.shape:
            total *= dim
        if count is None:
            count = total - start
        if start < 0 or count < 0 or start + count > total:
            raise IndexError(f"{start}+{count} outside {total} elements")
        out = array("f")
        if count:
            out.frombytes((c_char * (4 * count)).from_address(self.address() + 4 * start))
        return out

    def release(self) -> None:
        if self.ptr:
            ptr, self.ptr = self.ptr, None
            self._api.fn.ReleaseValue(ptr)
        self._keep = None

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:  # at interpreter exit ctypes may be half gone
            pass


def _shape_and_type(api: Api, info: Optional[int]) -> tuple[tuple[int, ...], int]:
    fn, check = api.fn, api.check
    count = c_size_t()
    check(fn.GetDimensionsCount(info, byref(count)))
    dims = (c_int64 * max(1, count.value))()
    check(fn.GetDimensions(info, dims, count.value))
    kind = c_int()
    check(fn.GetTensorElementType(info, byref(kind)))
    return tuple(dims[: count.value]), kind.value


class Selection:
    """Some of a session's outputs, by name, prepared once for `Session.run`."""

    __slots__ = ("indexes", "names")

    def __init__(self, indexes: list[int], names: Any) -> None:
        self.indexes = indexes
        self.names = names


class Session:
    """A model file on the CPU: `threads` intra-op threads, one inter-op (sequential)."""

    def __init__(
        self,
        api: Api,
        path: Union[str, os.PathLike[str]],
        threads: int = 2,
        optimization: int = ENABLE_ALL,
        config: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.ptr: Optional[int] = None
        self._api = api
        fn, check = api.fn, api.check
        options = c_void_p()
        check(fn.CreateSessionOptions(byref(options)))
        try:
            check(fn.SetIntraOpNumThreads(options, int(threads)))
            check(fn.SetInterOpNumThreads(options, 1))
            check(fn.SetSessionExecutionMode(options, _SEQUENTIAL))
            check(fn.SetSessionGraphOptimizationLevel(options, optimization))
            for key, value in (config or {}).items():
                check(fn.AddSessionConfigEntry(options, key.encode(), value.encode()))
            where = str(path) if _WINDOWS else os.fsencode(path)
            session = c_void_p()
            check(fn.CreateSession(api.env, where, options, byref(session)))
        finally:
            fn.ReleaseSessionOptions(options)
        self.ptr = session.value
        self.inputs = self._names(fn.SessionGetInputCount, fn.SessionGetInputName)
        self.outputs = self._names(fn.SessionGetOutputCount, fn.SessionGetOutputName)
        self._input_names = (c_char_p * len(self.inputs))(*(n.encode() for n in self.inputs))
        self._all = self.select(self.outputs)

    def _names(self, counter: Any, namer: Any) -> list[str]:
        api = self._api
        count = c_size_t()
        api.check(counter(self.ptr, byref(count)))
        names = []
        for index in range(count.value):
            raw = c_void_p()
            api.check(namer(self.ptr, index, api.allocator, byref(raw)))
            if not raw.value:
                raise OrtError(f"no name for input or output {index}")
            try:
                names.append(ctypes.string_at(raw.value).decode("utf-8"))
            finally:
                api.check(api.fn.AllocatorFree(api.allocator, raw))
        return names

    def input_shape(self, index: int) -> tuple[tuple[int, ...], int]:
        """An input's declared shape (-1 for a named or free size) and element type."""
        api = self._api
        fn = api.fn
        type_info = c_void_p()
        api.check(fn.SessionGetInputTypeInfo(self.ptr, index, byref(type_info)))
        try:
            tensor_info = c_void_p()  # owned by type_info
            api.check(fn.CastTypeInfoToTensorInfo(type_info, byref(tensor_info)))
            if not tensor_info.value:
                raise OrtError(f"input {self.inputs[index]} is not a tensor")
            return _shape_and_type(api, tensor_info.value)
        finally:
            fn.ReleaseTypeInfo(type_info)

    def select(self, names: Sequence[str]) -> Selection:
        indexes = [self.outputs.index(name) for name in names]
        return Selection(indexes, (c_char_p * len(names))(*(n.encode() for n in names)))

    def run(self, inputs: Sequence[Value], outputs: Optional[Selection] = None) -> list[Value]:
        """Runs on `inputs`, one per session input in the session's order; returns the selected
        outputs (all by default) in the selection's order, owned by the caller."""
        if len(inputs) != len(self.inputs):
            raise ValueError(f"{len(inputs)} inputs for {len(self.inputs)}")
        chosen = outputs or self._all
        wanted = len(chosen.indexes)
        pointers = [value.ptr for value in inputs]
        if None in pointers:
            raise OrtError("an input was released")
        values = (c_void_p * len(inputs))(*pointers)
        out = (c_void_p * wanted)()
        self._api.check(
            self._api.fn.Run(
                self.ptr,
                None,
                self._input_names,
                values,
                len(inputs),
                chosen.names,
                wanted,
                out,
            )
        )
        return [Value(self._api, out[i], self) for i in range(wanted)]

    def close(self) -> None:
        """Releases the session; meant for after its outputs are released (each output holds
        the Session object, so collection alone never releases it under them)."""
        if self.ptr:
            ptr, self.ptr = self.ptr, None
            self._api.fn.ReleaseSession(ptr)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # at interpreter exit ctypes may be half gone
            pass
