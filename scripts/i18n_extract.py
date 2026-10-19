"""Collect the app's texts into i18n/template.json and report each language's coverage.

    uv run python scripts/i18n_extract.py                 # write the template, show coverage
    uv run python scripts/i18n_extract.py --check         # only check (the tests do this)
    uv run python scripts/i18n_extract.py --missing tr    # the texts Turkish lacks
    uv run python scripts/i18n_extract.py --init de --name Deutsch   # start a language
    uv run python scripts/i18n_extract.py --update de     # add new texts, drop retired ones

It reads ui/, services/ and main.py (no code runs) for the literals given to tr(), tr_n()
and N_() (services/i18n.py). A comment `# i18n: ...` on the call's lines, or on the line
above it, is kept for the translators in the template's "@context".

A new language: `--init <code> --name <its own name>` writes i18n/<code>.json with every text
and an empty translation; fill in each one (an empty one shows English). Keep each {field}
as it is, and give a plural entry the forms the language needs ("one", "few", "many",
"other"; at least "other"). The English plural of each is in the template's "@plurals".
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import sys
import tokenize
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.i18n import META_PREFIX, PLURAL_FORMS, TEMPLATE, placeholders  # noqa: E402

CATALOGS = ROOT / "i18n"
TEMPLATE_PATH = CATALOGS / f"{TEMPLATE}.json"
SOURCES = ("ui", "services", "main.py")
ABOUT = (
    "Every text QiTV shows, in English. A new language: "
    "`uv run python scripts/i18n_extract.py --init <code> --name <its own name>`, then fill in "
    "each translation of i18n/<code>.json (an empty one shows English). Keep every {field}; "
    "a plural entry holds the forms the language needs (one, few, many, other). @context "
    "explains texts whose meaning is unclear; @plurals has each counted text's English plural."
)
_CALLS = {"tr": 1, "tr_n": 2, "N_": 1}


@dataclass
class Entry:
    text: str
    plural: Optional[str] = None
    ctx: Optional[str] = None
    comments: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.ctx}|{self.text}" if self.ctx else self.text


def source_files(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for name in SOURCES:
        path = root / name
        files += sorted(path.rglob("*.py")) if path.is_dir() else [path]
    return [path for path in files if path.is_file() and path.name != "i18n.py"]


def _comments(source: str) -> dict[int, str]:
    """`# i18n: ...` comments by line."""
    found: dict[int, str] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                text = token.string.lstrip("#").strip()
                if text.lower().startswith("i18n:"):
                    found[token.start[0]] = text[5:].strip()
    except tokenize.TokenError:
        pass
    return found


def _call_name(node: ast.Call) -> Optional[str]:
    func = node.func
    if isinstance(func, ast.Name) and func.id in _CALLS:
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _CALLS
        and isinstance(func.value, ast.Name)
        and func.value.id == "i18n"
    ):
        return func.attr
    return None


class _Scanner(ast.NodeVisitor):
    """Finds the marked texts of one file, and the calls that break the rules."""

    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.lines = source.splitlines()
        self.comments = _comments(source)
        self.entries: list[Entry] = []
        self.problems: list[str] = []
        self._deferred = False  # inside a function's body: runs when called, not at import

    def _visit_deferred(self, nodes: Iterable[ast.AST]) -> None:
        was, self._deferred = self._deferred, True
        for node in nodes:
            self.visit(node)
        self._deferred = was

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)  # defaults are evaluated at import
        if node.returns is not None:
            self.visit(node.returns)
        self._visit_deferred(node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self._visit_deferred([node.body])

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name is not None:
            self._marked(name, node)
        self.generic_visit(node)

    def _problem(self, node: ast.AST, message: str) -> None:
        self.problems.append(f"{self.rel}:{getattr(node, 'lineno', 0)}: {message}")

    def _marked(self, name: str, node: ast.Call) -> None:
        count = _CALLS[name]
        args = node.args[:count]
        texts = [a.value for a in args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if len(args) < count or len(texts) < count:
            self._problem(node, f"{name}() needs a string literal, not an expression")
            return
        if name != "N_" and not self._deferred:
            self._problem(node, f"{name}() runs at import, before the language is set")
        ctx = None
        for keyword in node.keywords:
            if keyword.arg == "ctx":
                value = keyword.value
                if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    self._problem(node, "ctx= needs a string literal")
                    return
                ctx = value.value
        entry = Entry(texts[0], texts[1] if name == "tr_n" else None, ctx)
        entry.places.append(f"{self.rel}:{node.lineno}")
        end = getattr(node, "end_lineno", node.lineno) or node.lineno
        for line in [node.lineno - 1, *range(node.lineno, end + 1)]:
            comment = self.comments.get(line)
            above_alone = line == node.lineno - 1 and self._comment_only(line)
            if comment and (line >= node.lineno or above_alone):
                entry.comments.append(comment)
        self.entries.append(entry)

    def _comment_only(self, line: int) -> bool:
        return 0 < line <= len(self.lines) and self.lines[line - 1].lstrip().startswith("#")


def collect(files: Optional[list[Path]] = None, root: Path = ROOT):
    """(entries by catalog key, problems) of the marked texts in `files`."""
    entries: dict[str, Entry] = {}
    problems: list[str] = []
    for path in files if files is not None else source_files(root):
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        scanner = _Scanner(rel, source)
        scanner.visit(ast.parse(source, filename=rel))
        problems += scanner.problems
        for entry in scanner.entries:
            known = entries.get(entry.key)
            if known is None:
                entries[entry.key] = entry
                continue
            if (known.plural is None) != (entry.plural is None) or (
                known.plural and entry.plural and known.plural != entry.plural
            ):
                problems.append(
                    f"{entry.places[0]}: {entry.text!r} is marked with two different plurals "
                    f"(also {known.places[0]})"
                )
            for comment in entry.comments:
                if comment not in known.comments:
                    known.comments.append(comment)
            known.places += entry.places
    for key, entry in entries.items():
        if entry.plural is not None and placeholders(entry.plural) - placeholders(entry.text) - {
            "n"
        }:
            problems.append(f"{entry.places[0]}: {key!r}'s plural has fields its singular lacks")
    return entries, problems


def template(entries: dict[str, Entry]) -> dict[str, Any]:
    data: dict[str, Any] = {"@about": ABOUT}
    contexts = {key: "; ".join(e.comments) for key, e in entries.items() if e.comments}
    if contexts:
        data["@context"] = contexts
    plurals = {key: e.plural for key, e in entries.items() if e.plural is not None}
    if plurals:
        data["@plurals"] = plurals
    for key, entry in entries.items():
        data[key] = {"one": "", "other": ""} if entry.plural is not None else ""
    return data


def dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def catalog_paths() -> list[Path]:
    return [p for p in sorted(CATALOGS.glob("*.json")) if p.stem != TEMPLATE]


def texts(data: dict[str, Any]) -> dict[str, Any]:
    """A catalog's or the template's entries, without its "@" data."""
    return {k: v for k, v in data.items() if not k.startswith(META_PREFIX)}


def _source_text(key: str, entries: dict[str, Entry]) -> str:
    entry = entries.get(key)
    return entry.text if entry is not None else key.split("|", 1)[-1]


def placeholder_problems(catalog: dict[str, Any], entries: dict[str, Entry]) -> list[str]:
    """Translations whose {fields} differ from their English text's."""
    problems = []
    for key, value in texts(catalog).items():
        entry = entries.get(key)
        source = _source_text(key, entries)
        if isinstance(value, str):
            if entry is not None and entry.plural is not None and value:
                problems.append(f"{key!r}: a counted text needs plural forms, not a string")
            elif value and placeholders(value) != placeholders(source):
                problems.append(
                    f"{key!r}: fields {sorted(placeholders(value))}, "
                    f"English has {sorted(placeholders(source))}"
                )
        elif isinstance(value, dict):
            plural = entry.plural if entry is not None and entry.plural else source
            allowed = placeholders(source) | placeholders(plural) | {"n"}
            for form, text in value.items():
                if form not in PLURAL_FORMS or not isinstance(text, str):
                    problems.append(f"{key!r}: {form!r} is not a plural form")
                elif text and not placeholders(text) <= allowed:
                    problems.append(f"{key!r} ({form}): fields {sorted(placeholders(text))}")
            if any(value.values()) and not value.get("other"):
                problems.append(f"{key!r}: a plural entry needs its \"other\" form")
        else:
            problems.append(f"{key!r}: not a text")
    return problems


def missing(catalog: dict[str, Any], entries: dict[str, Entry]) -> list[str]:
    """The template's texts this catalog has no translation for."""
    have = texts(catalog)
    out = []
    for key in entries:
        value = have.get(key)
        if isinstance(value, dict):
            value = value.get("other")
        if not value:
            out.append(key)
    return sorted(out)


def obsolete(catalog: dict[str, Any], entries: dict[str, Entry]) -> list[str]:
    return sorted(key for key in texts(catalog) if key not in entries)


def coverage_lines(entries: dict[str, Entry]) -> list[str]:
    lines = []
    for path in catalog_paths():
        catalog = read_json(path)
        lacking = missing(catalog, entries)
        done = len(entries) - len(lacking)
        share = 100.0 * done / len(entries) if entries else 100.0
        extra = obsolete(catalog, entries)
        lines.append(
            f"{path.stem}: {done}/{len(entries)} ({share:.1f}%)"
            + (f", {len(extra)} no longer used" if extra else "")
        )
    return lines


def updated_catalog(catalog: dict[str, Any], entries: dict[str, Entry]) -> dict[str, Any]:
    """The catalog with every template text (new ones empty) and none it no longer has."""
    out = {k: v for k, v in catalog.items() if k.startswith(META_PREFIX)}
    for key, entry in entries.items():
        value = catalog.get(key)
        if entry.plural is not None:
            out[key] = value if isinstance(value, dict) else {"other": ""}
        else:
            out[key] = value if isinstance(value, str) else ""
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="write nothing; fail when stale")
    parser.add_argument("--missing", metavar="CODE", help="list the texts a language lacks")
    parser.add_argument("--init", metavar="CODE", help="start i18n/<CODE>.json")
    parser.add_argument("--name", help="the new language's own name (with --init)")
    parser.add_argument("--update", metavar="CODE", help="bring i18n/<CODE>.json up to date")
    args = parser.parse_args(argv)

    entries, problems = collect()
    for problem in problems:
        print(problem, file=sys.stderr)
    data = template(entries)
    if args.check:
        stale = not TEMPLATE_PATH.exists() or read_json(TEMPLATE_PATH) != data
        if stale:
            print("i18n/template.json is stale: run scripts/i18n_extract.py", file=sys.stderr)
        return 1 if stale or problems else 0
    TEMPLATE_PATH.parent.mkdir(exist_ok=True)
    TEMPLATE_PATH.write_text(dumps(data), encoding="utf-8")
    if args.init:
        path = CATALOGS / f"{args.init}.json"
        if path.exists():
            print(f"{path.name} exists: use --update", file=sys.stderr)
            return 1
        meta = {"@meta": {"name": args.name or args.init}}
        path.write_text(dumps(updated_catalog(meta, entries)), encoding="utf-8")
    if args.update:
        path = CATALOGS / f"{args.update}.json"
        path.write_text(dumps(updated_catalog(read_json(path), entries)), encoding="utf-8")
    if args.missing:
        for key in missing(read_json(CATALOGS / f"{args.missing}.json"), entries):
            print(key)
        return 0
    print(f"{len(entries)} texts in the template")
    for line in coverage_lines(entries):
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
