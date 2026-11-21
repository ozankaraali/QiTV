"""Make 2.13.2's translation model.onnx from the one 2.10 shipped: the lm head runs on the last
position only. The weights (model.onnx.data) are untouched: this rewires three nodes of the
graph and writes the graph alone.

The shipped graph (MiLMMT-46-1B int4, Hugging Face revision 73356cb2) ends in

    final norm [1, n, 1152] -> /lm_head/MatMul_Q4 -> logits [1, n, 262144]
        -> nt_gather (index -1, axis 1) -> nt_row [1, 262144] -> nt_argmax -> next_token

so a prompt of n tokens runs the head (1152 x 262144, int4) n times for the one row it keeps.
The edit moves the Gather before the head:

    final norm -> nt_gather -> nt_hidden [1, 1152] -> /lm_head/MatMul_Q4 -> logits [1, 262144]
        -> nt_argmax -> next_token

The same next token comes out (the head is a matrix product: row by row, the last row is the
same), and decoding (n = 1) is unchanged. Everything else is checked to be the shipped graph's:
the initializers (with their places in model.onnx.data), the inputs and outputs, the opsets.

    uv run --no-project --python 3.12 --with onnx==1.23.2 \\
        python scripts/edit_translation_graph.py <shipped>/model.onnx <new>/model.onnx

Put model.onnx.data beside the output for onnx.checker's look at the external data (else only
the shipped folder's copy is looked at). It must be a file of its own there: onnx.checker
refuses a hard link (and a symlink, as the Hugging Face cache has). Made 2026-11-21 with onnx
1.23.2 and protobuf 7.36.2 (the shipped file serializes back to itself byte for byte with them):
295,402 bytes in, 295,408 out. Not run by QiTV; `onnx` isn't one of its dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import tempfile
from typing import Any

SHIPPED_SIZE = 295402
SHIPPED_SHA256 = "e8652dbcd810fb2026255996547c64a2e872f32dcdc290f2f478e3806a4eae30"
EDITED_SIZE = 295408
EDITED_SHA256 = "40285a45ea7569cbce83217e79d51f3a5c75afb1fac8bcc0ed2193d5f3b32a5f"  # 2.13.2 pins it
DATA = "model.onnx.data"

HEAD, GATHER, ARGMAX = "/lm_head/MatMul_Q4", "nt_gather", "nt_argmax"
HIDDEN = "nt_hidden"  # the Gather's output now: the last position's hidden state
# onnxruntime's own op, in the default domain as the shipped graph has it: onnx.checker knows
# it not, so its check runs on a copy with these nodes in onnxruntime's domain (as custom ops)
ORT_DEFAULT_DOMAIN_OPS = ("SimplifiedLayerNormalization",)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"edit_translation_graph: {message}")


def consumers(graph: Any, name: str) -> list[Any]:
    return [node for node in graph.node if name in node.input]


def attribute(onnx: Any, node: Any, name: str) -> Any:
    for found in node.attribute:
        if found.name == name:
            return onnx.helper.get_attribute_value(found)
    return None


def check_shipped(onnx: Any, model: Any) -> tuple[Any, Any, Any]:
    """The three nodes, wired as the shipped graph has them (else it stops: not that graph)."""
    graph = model.graph
    by_name = {node.name: node for node in graph.node}
    head, gather, argmax = (by_name.get(name) for name in (HEAD, GATHER, ARGMAX))
    if head is None or gather is None or argmax is None:
        fail("not the shipped graph: its head, gather or argmax node is missing")
    if (head.op_type, head.domain) != ("MatMulNBits", "com.microsoft"):
        fail(f"{HEAD} is {head.domain}:{head.op_type}, not MatMulNBits")
    logits = head.output[0]
    outputs = {value.name for value in graph.output}
    if (
        gather.op_type != "Gather"
        or gather.input[0] != logits
        or attribute(onnx, gather, "axis") != 1
    ):
        fail(f"{GATHER} is not Gather({logits}, axis=1)")
    index = {tensor.name: tensor for tensor in graph.initializer}.get(gather.input[1])
    if index is None or list(index.dims) or onnx.numpy_helper.to_array(index).item() != -1:
        fail(f"{GATHER}'s index is not the scalar -1")
    row = gather.output[0]
    if argmax.op_type != "ArgMax" or list(argmax.input) != [row]:
        fail(f"{ARGMAX} does not take {row}")
    if [node.name for node in consumers(graph, logits)] != [GATHER] or logits in outputs:
        fail(f"{logits} goes somewhere other than {GATHER}")
    if [node.name for node in consumers(graph, row)] != [ARGMAX] or row in outputs:
        fail(f"{row} goes somewhere other than {ARGMAX}")
    if HIDDEN in {name for node in graph.node for name in (*node.input, *node.output)}:
        fail(f"{HIDDEN} is already a name in the graph")
    return head, gather, argmax


def edit(onnx: Any, model: Any) -> Any:
    """A copy of `model` with the Gather before the head (the nodes in topological order)."""
    edited = onnx.ModelProto()
    edited.CopyFrom(model)
    graph = edited.graph
    head, gather, argmax = check_shipped(onnx, edited)
    hidden, logits, row = head.input[0], head.output[0], gather.output[0]
    gather.input[0], gather.output[0] = hidden, HIDDEN  # Gather(final norm, -1, axis=1)
    head.input[0] = HIDDEN  # the head on [1, 1152]
    argmax.input[0] = logits  # the argmax as before, over [1, 262144]
    moved = {HEAD, GATHER, ARGMAX}
    nodes = [node for node in graph.node if node.name not in moved] + [gather, head, argmax]
    copies = []
    for node in nodes:
        copy = onnx.NodeProto()
        copy.CopyFrom(node)
        copies.append(copy)
    del graph.node[:]
    graph.node.extend(copies)
    # shapes recorded for the old logits and row no longer hold (the shipped file has none)
    kept = [value for value in graph.value_info if value.name not in (logits, row)]
    del graph.value_info[:]
    graph.value_info.extend(kept)
    return edited


def same_but_the_edit(onnx: Any, shipped: Any, edited: Any) -> None:
    """Everything but the three nodes is the shipped graph's, byte for byte."""
    a, b = shipped.graph, edited.graph

    def serialized(items: Any) -> list[bytes]:
        return [item.SerializeToString() for item in items]

    for what in ("initializer", "input", "output", "sparse_initializer"):
        if serialized(getattr(a, what)) != serialized(getattr(b, what)):
            fail(f"the graph's {what} changed")
    if serialized(shipped.opset_import) != serialized(edited.opset_import):
        fail("the opsets changed")
    if shipped.ir_version != edited.ir_version or a.name != b.name:
        fail("the model's header changed")
    moved = {HEAD, GATHER, ARGMAX}
    rest_a = [n.SerializeToString() for n in a.node if n.name not in moved]
    rest_b = [n.SerializeToString() for n in b.node if n.name not in moved]
    if rest_a != rest_b:
        fail("a node other than the three changed")
    known = {value.name for value in (*b.input, *b.initializer)} | {""}
    for node in b.node:
        missing = [name for name in node.input if name not in known]
        if missing:
            fail(f"{node.name} runs before {missing} is made")
        known.update(node.output)
    if any(value.name not in known for value in b.output):
        fail("an output is no longer made")


def check(onnx: Any, model: Any, folder: Path) -> None:
    """onnx.checker over `model` by its path in `folder` (beside model.onnx.data, which it
    looks at), with onnxruntime's default-domain op as a custom one."""
    copy = onnx.ModelProto()
    copy.CopyFrom(model)
    for node in copy.graph.node:
        if node.domain == "" and node.op_type in ORT_DEFAULT_DOMAIN_OPS:
            node.domain = "com.microsoft"
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".onnx", delete=False) as out:
        out.write(copy.SerializeToString())
    try:
        onnx.checker.check_model(out.name)
    finally:
        Path(out.name).unlink()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("shipped", type=Path, help="the shipped model.onnx (beside its data)")
    parser.add_argument("output", type=Path, help="where the new model.onnx goes")
    args = parser.parse_args(argv)
    import onnx  # (not a QiTV dependency)

    raw = args.shipped.read_bytes()
    if len(raw) != SHIPPED_SIZE or sha256(raw) != SHIPPED_SHA256:
        fail(f"{args.shipped} is not the shipped model.onnx ({len(raw)} bytes, {sha256(raw)})")
    shipped = onnx.load(str(args.shipped), load_external_data=False)  # (the data stays put)
    if shipped.SerializeToString() != raw:
        fail("this onnx/protobuf does not write the shipped file back as it was")
    data_folder = args.shipped.resolve().parent
    if not (data_folder / DATA).is_file():
        fail(f"no {DATA} beside {args.shipped}")
    check(onnx, shipped, data_folder)
    edited = edit(onnx, shipped)
    same_but_the_edit(onnx, shipped, edited)
    body = edited.SerializeToString()
    out_folder = args.output.resolve().parent
    check(onnx, edited, out_folder if (out_folder / DATA).is_file() else data_folder)
    if args.output.resolve() == args.shipped.resolve():
        fail("the output would overwrite the shipped file")
    args.output.write_bytes(body)
    digest = sha256(body)
    print(f"{args.output}: {len(body)} bytes, sha256 {digest}")
    if EDITED_SHA256 and (len(body), digest) != (EDITED_SIZE, EDITED_SHA256):
        print("  (not the model.onnx 2.13.2 pins: another onnx or protobuf?)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
