"""The README's key tables, made from the app's own list of keys (ui/keymap.py's SHORTCUTS).

    uv run python scripts/readme_keys.py

prints the Markdown that goes between the README's `<!-- keys:start -->` and
`<!-- keys:end -->` markers. tests/test_readme.py checks that the README still shows exactly
this, so a key changed in the app without the README fails the tests.

The keys read the same on every computer: "Ctrl/⌘+F" (QiTV's Ctrl shortcuts are ⌘ on a Mac),
"Alt/⌥+P", and ⌃⌘F (Control+Command+F) for the Mac's own fullscreen key.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.keymap import SHORTCUT_TITLES, SHORTCUTS  # noqa: E402

START, END = "<!-- keys:start -->", "<!-- keys:end -->"
_NAMES = {
    "Up": "↑",
    "Down": "↓",
    "Left": "←",
    "Right": "→",
    "Return": "Enter",
    "PgUp": "Page Up",
    "PgDown": "Page Down",
    "Ctrl+Meta+F": "⌃⌘F (Mac)",
}
_MODIFIERS = {"Ctrl+": "Ctrl/⌘+", "Alt+": "Alt/⌥+"}


def key_name(key: str) -> str:
    """One key of a SHORTCUTS entry as the README writes it."""
    first, ellipsis, last = key.partition("…")
    if ellipsis and len(first) == 1 and len(last) == 1:
        return f"{first}–{last}"
    if key in _NAMES:
        return _NAMES[key]
    for prefix, name in _MODIFIERS.items():
        if key.startswith(prefix):
            return name + key[len(prefix) :]
    return key


def keys_cell(keys: str) -> str:
    """A SHORTCUTS entry's keys ("Up, Down") as a table cell: "<kbd>↑</kbd> / <kbd>↓</kbd>"."""
    return " / ".join(f"<kbd>{key_name(key)}</kbd>" for key in keys.split(", "))


def markdown() -> str:
    """One small table per screen, each under its title, as the app's Help lists them."""
    blocks = []
    for context, rows in SHORTCUTS.items():
        lines = [f"**{SHORTCUT_TITLES[context]}**", "", "| Keys | What they do |", "| --- | --- |"]
        lines += [f"| {keys_cell(keys)} | {what} |" for keys, what in rows]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def readme_block(readme: str) -> str:
    """What the README shows between the markers (empty when they are missing)."""
    _before, start, rest = readme.partition(START)
    block, end, _after = rest.partition(END)
    return block.strip("\n") + "\n" if start and end else ""


if __name__ == "__main__":
    sys.stdout.write(markdown())
