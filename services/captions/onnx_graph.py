"""A small ONNX model written from Python, with no onnx package (the captions runtime has none):
the protobuf wire format by hand, enough for a graph of a few nodes over float tensors with
constant weights (initializers). services.captions.sortformer writes its log-mel front end with
it, and runs it through the runtime's onnxruntime (services.captions.onnx_c).

The fields are those of onnx.proto (onnx/onnx.proto3): ModelProto, GraphProto, NodeProto,
TensorProto, ValueInfoProto, TypeProto, TensorShapeProto, OperatorSetIdProto.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
import struct
import sys
from typing import Sequence, Union

IR_VERSION = 7
OPSET = 13
FLOAT = 1  # TensorProto.FLOAT

Dim = Union[int, str]  # a size, or a named free one ("frames")


def _varint(value: int) -> bytes:
    out = bytearray()
    value &= (1 << 64) - 1  # (a negative int64 as its two's complement)
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire: int) -> bytes:
    return _varint(number << 3 | wire)


def _bytes(number: int, data: bytes) -> bytes:
    return _key(number, 2) + _varint(len(data)) + data


def _text(number: int, value: str) -> bytes:
    return _bytes(number, value.encode("utf-8"))


def _int(number: int, value: int) -> bytes:
    return _key(number, 0) + _varint(value)


def _float(number: int, value: float) -> bytes:
    return _key(number, 5) + struct.pack("<f", value)


@dataclass
class Node:
    op: str
    inputs: Sequence[str]
    outputs: Sequence[str]
    floats: dict[str, float] = field(default_factory=dict)  # float attributes
    ints: dict[str, Sequence[int]] = field(default_factory=dict)  # int list attributes

    def encode(self, index: int) -> bytes:
        out = b"".join(_text(1, name) for name in self.inputs)
        out += b"".join(_text(2, name) for name in self.outputs)
        out += _text(3, f"{self.op.lower()}_{index}") + _text(4, self.op)
        for name, value in self.floats.items():  # AttributeProto: FLOAT (1)
            out += _bytes(5, _text(1, name) + _float(2, value) + _int(20, 1))
        for name, values in self.ints.items():  # AttributeProto: INTS (7)
            body = _text(1, name) + b"".join(_int(8, v) for v in values) + _int(20, 7)
            out += _bytes(5, body)
        return out


def _tensor(name: str, dims: Sequence[int], values: array) -> bytes:
    data = array("f", values)
    if sys.byteorder == "big":
        data.byteswap()
    out = b"".join(_int(1, d) for d in dims) + _int(2, FLOAT) + _text(8, name)
    return out + _bytes(9, data.tobytes())


def _value_info(name: str, dims: Sequence[Dim]) -> bytes:
    shape = b""
    for dim in dims:
        shape += _bytes(1, _text(2, dim) if isinstance(dim, str) else _int(1, dim))
    tensor_type = _int(1, FLOAT) + _bytes(2, shape)
    return _text(1, name) + _bytes(2, _bytes(1, tensor_type))


def model(
    nodes: Sequence[Node],
    inputs: Sequence[tuple[str, Sequence[Dim]]],
    outputs: Sequence[tuple[str, Sequence[Dim]]],
    weights: Sequence[tuple[str, Sequence[int], array]],
    name: str = "graph",
) -> bytes:
    """A whole ONNX model's bytes: `nodes` in order, float inputs and outputs (name, shape), and
    constant float `weights` (name, shape, values)."""
    graph = b"".join(_bytes(1, node.encode(i)) for i, node in enumerate(nodes))
    graph += _text(2, name)
    graph += b"".join(_bytes(5, _tensor(n, dims, values)) for n, dims, values in weights)
    graph += b"".join(_bytes(11, _value_info(n, dims)) for n, dims in inputs)
    graph += b"".join(_bytes(12, _value_info(n, dims)) for n, dims in outputs)
    opset = _bytes(8, _text(1, "") + _int(2, OPSET))
    return _int(1, IR_VERSION) + _text(2, "qitv") + opset + _bytes(7, graph)
