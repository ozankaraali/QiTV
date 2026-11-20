"""Cards over the picture (2.12): a form, and a question with two or three answers.

Settings shows them over the dimmed TV for what does not fit a row: adding a provider (a link
field with Paste, the fields its kind needs, "Check and add"), a provider's address and login,
and the questions before a change that cannot be taken back ("This link is another provider",
"Remove …?"). The look is the approved mockups' (providers.html, moments 3 and 4) and the
OpenSubtitles page's (ui/overlays/subtitle_search.py): the picture dimmed, and in its middle a
dark card 48 u wide with a title, then

- a FormCard's labelled fields (a secret one shows dots; one with Paste has the button beside
  it), the line that says what was found under the first one (green when good, else in the
  warning colour), the buttons at the right with the accent one filled, and the other ways to
  go on in a muted line below ("Or: M3U file…  STB portal");
- a ChoiceCard's words and its buttons;
- a RangeCard's value, a track with a thumb under it (a muted line, an accent thumb), a note
  that follows the value (the estimated rewind of a cache size), and Cancel and OK. ←/→ step,
  PgUp/PgDn move ten steps, Home/End go to the ends, Enter keeps, Esc and Back cancel
  (`answered` with the value, or None); a click or a drag on the track sets it.

The keys reach a card through `handle` (the Settings controller gives them to a shown card
before its rows), and every key is the card's while it shows, but the sound's and the window's.
↑/↓ go between the fields, the button row (between its buttons too while they are too wide for
one row and stand one under another) and the alternates; ←/→ move the caret in a field
(at the text's end, → goes on to its Paste), or go between the buttons; typing, Backspace,
Delete, Home/End and Ctrl/⌘ + A, C, X and V work in the focused field (ui/text_edit.py);
Enter presses the focused button, or the accent one from a field; Esc cancels. A click focuses
or presses, and a click on the dimmed picture (or the mouse's Back) cancels. On a button or an
alternate, Backspace (the phone's "previous" too) is Back, which in a card is Cancel; in a
field it deletes and never cancels. A mouse press that hides the card swallows a
double-click's second press, which would land on what is behind it (ui.pointer); so does a
card shown by a press (a row under it), whose second press would land on the card.

The button keyed CANCEL ("cancel") is the card's Cancel and is never disabled: on a FormCard
it does what Esc does (`cancelled`, never `pressed("cancel")`); a ChoiceCard answers "cancel"
for it, for Esc and for the dimmed picture. Either card hides itself when cancelled or
answered; a FormCard stays up after `pressed`, and its caller hides it when it is done. A card
shown over another (the guard's question over a provider's form) is the only one painted while
it shows, alone over the dimmed picture; the one under it shows again as it goes.

Paste (the button, or Ctrl/⌘+V off the field) puts the clipboard's first line in the field in
place of its text (a link in place of the old one), or in place of the part selected in it;
Ctrl/⌘+V in the field types it at the caret, as any text field does. While a FormCard is busy
(`set_busy`: a check is on its way) it says "Checking…" beside its buttons, which wait, and
its fields keep the text being checked; Cancel still works. A secret field's text is never
shown, copied, announced, given as a tooltip or put in a repr. 1 u = 1% of the overlay host's
width (ui_units); in a window too small for the card, its gaps shrink, then the middle is cut
so that the title and the buttons stay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QShowEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from services.i18n import N_, translate
from ui.keymap import Action, key_hint
from ui.overlays.base import Overlay, announce
from ui.overlays.painting import Units, fill_round, ui_units
from ui.pointer import SecondClickGuard
from ui.text_edit import (
    CARET_W,
    SELECTION_ALPHA,
    Dragging,
    TextEdit,
    clipboard_line,
    edit_key,
    paint_edit,
)
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_PANEL, qcolor

CANCEL = "cancel"  # the key of a card's Cancel button
PASTE_TEXT = N_("Paste")
CANCEL_TEXT = N_("Cancel")
OK_TEXT = N_("OK")
OK = "ok"  # the key of a RangeCard's OK button
BUSY_TEXT = N_("Checking…")  # i18n: beside a card's buttons while a provider is checked
# i18n: before a card's other ways to go on, in a muted line: "Or: M3U file…  STB portal"
OR_TEXT = N_("Or:")

_FALLBACK_HOST_WIDTH = 1280
_EDGE_U = 2.0  # from the host's sides
_WIDTH_U = 48.0
_MIN_WIDTH_PX = 400  # at least, in a small window (never past its sides)
_PAD_X_U = 2.2
_PAD_Y_U = 2.0
_TITLE_GAP_U = 1.2
_LABEL_MIN_U = 9.0  # the labels' column, at least
_LABEL_GAP_U = 1.0
_LABEL_SHARE = 0.4  # the labels' column, at most, of the card's inner width
_PASTE_SHARE = 0.35  # a Paste button, at most
_FIELD_H_U = 2.8
_FIELD_PAD_U = 0.7  # the text from a field's sides
_TEXT_PAD_U = 0.45  # a field's and a button's text from their top and bottom
_ROW_GAP_U = 0.7
_FOUND_GAP_U = 0.3
_FOOT_GAP_U = 1.6
_BUTTON_H_U = 2.8
_BUTTON_PAD_U = 1.2
_BUTTON_GAP_U = 0.6
_RADIUS_U = 0.6  # a field's and a button's corners
_ALT_GAP_U = 1.4
_ALT_RULE_U = 1.0
_ALT_PAD_U = 0.4  # around an alternate's text (its focus)
_ALT_PAD_Y_U = 0.3
_ALT_SPACE_U = 0.3  # between alternates, besides their padding
_DIM = ("#05070a", 0.62)  # the picture under the card
_MASK = "•"
_DISABLED_OPACITY = 0.4
_PLACEHOLDER_ALPHA = 0.45
_TRACK_H_U = 0.35  # the line
_TRACK_ROW_U = 3.0  # the track's row (what a press takes)
_THUMB_U = 0.95  # the thumb's radius
_TRACK_ALPHA = 0.22
_PAGE_STEPS = 10

# Where things are, by (zone, key): one rect per field, button and alternate.
_Rects = dict[tuple[str, str], QRectF]
PANEL, TITLE, BODY, MIDDLE = ("panel", ""), ("title", ""), ("body", ""), ("middle", "")
FOUND, BUSY, RULE, LEAD = ("found", ""), ("busy", ""), ("rule", ""), ("lead", "")
_FOOTER = frozenset({"button", "busy", "rule", "lead", "alt"})  # stays in a small window
# cut in a small window
_MIDDLE = frozenset({"body", "label", "field", "paste", "found", "value", "track", "note"})
_HITS = ("paste", "field", "label", "button", "alt", "track")
# Words that wrap (the question's, the found line's), from the top left.
_WRAP = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap)

# Keys that never wait for the card: the sound and the window (as the question card's).
_THROUGH = frozenset(
    {Action.MUTE, Action.VOL_UP, Action.VOL_DOWN, Action.FULLSCREEN, Action.MINI_PLAYER}
)
# A field's keys besides ←/→ (Delete is FORGET in the drawer's key context).
_FIELD_KEYS = frozenset(
    {
        Action.NAV_TEXT,
        Action.NAV_BACKSPACE,
        Action.NAV_DELETE,
        Action.FORGET,
        Action.NAV_HOME,
        Action.NAV_END,
        Action.SELECT_ALL,
        Action.COPY,
        Action.CUT,
        Action.PASTE,
    }
)
# The keys that change a field's text: none while a check is on its way.
_CHANGING = frozenset(
    {
        Action.NAV_TEXT,
        Action.NAV_BACKSPACE,
        Action.NAV_DELETE,
        Action.FORGET,
        Action.CUT,
        Action.PASTE,
    }
)


@dataclass(frozen=True)
class Field:
    """A labelled text field: `secret` shows dots, `paste` has a Paste button beside it. The
    label and placeholder are shown as they are (the caller's, in the app's language)."""

    key: str
    label: str
    text: str = field(default="", repr=False)  # a secret's too: never in a repr
    secret: bool = False
    placeholder: str = ""
    paste: bool = False


@dataclass(frozen=True)
class Button:
    """A card's button (or an alternate): `accent` is filled, `danger` is red. The key CANCEL
    makes it the card's Cancel."""

    key: str
    text: str
    accent: bool = False
    danger: bool = False


def _line_h(font: QFont) -> float:
    return float(math.ceil(QFontMetricsF(font).height()))


def _advance(font: QFont, text: str) -> float:
    return QFontMetricsF(font).horizontalAdvance(text)


def _elided(font: QFont, text: str, width: float) -> str:
    return QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, width))


class _Card(Overlay):
    """What both cards share: the dimmed picture, the card in its middle, its title, words,
    fields and buttons, the keys and the mouse. The subclasses say what pressing and
    cancelling do."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._title = ""
        self._body = ""
        self._fields: list[Field] = []
        self._edits: dict[str, TextEdit] = {}
        self._found = ""
        self._found_ok = True
        self._buttons: list[Button] = []
        self._alternates: list[Button] = []
        self._busy = False
        self._focus: tuple[str, str] = ("", "")
        self._last_button = ""  # the button the row was left on (↑ from the alternates)
        self._last_alt = ""
        self._hover: Optional[tuple[str, str]] = None
        self._drag = Dragging()
        self._second_click = SecondClickGuard(self)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- what the subclasses decide ---------------------------------------------------------

    def _pressed(self, key: str) -> None:
        raise NotImplementedError

    def _cancel(self) -> None:
        raise NotImplementedError

    def _back(self) -> None:
        """Backspace off a field, or the player's Back (PREVIOUS, in a field too)."""

    # ---- what it holds (the caller, tests) --------------------------------------------------

    def title(self) -> str:
        return self._title

    def body(self) -> str:
        """A ChoiceCard's words under its title ("" for none)."""
        return self._body

    def focus(self) -> tuple[str, str]:
        """("field" | "paste" | "button" | "alt", its key): what the keys are on."""
        return self._focus

    def busy(self) -> bool:
        return self._busy

    def card_rect(self) -> QRectF:
        return QRectF(self._layout()[PANEL])

    def control_rect(self, zone: str, key: str = "") -> QRectF:
        """Where a "field", "label", "paste", "button" or "alt" (by key), or the "title",
        "body", "found" line or "busy" text is; empty when there is none (or it is cut off in
        a small window)."""
        rects = self._layout()
        rect = rects.get((zone, key))
        if rect is None:
            return QRectF()
        if zone in _MIDDLE:
            return rect.intersected(rects[MIDDLE])
        return QRectF(rect)

    # ---- the keys -----------------------------------------------------------------------------

    def handle(self, action: Action, payload: object = "") -> bool:
        """A key while it shows: True for all but the sound's and the window's, so that none
        reaches what is under the card (False while it is hidden)."""
        if self.isHidden() or action in _THROUGH:
            return False
        text = payload if isinstance(payload, str) else ""
        zone, key = self._focus
        if action is Action.ESCAPE:
            self._cancel()
        elif action in (Action.NAV_UP, Action.NAV_DOWN):
            self._vertical(-1 if action is Action.NAV_UP else 1)
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            rows = len(self._rows())
            self._vertical(-rows if action is Action.NAV_PAGE_UP else rows)
        elif action in (Action.NAV_ACTIVATE, Action.OK):
            self._activate()
        elif action in (Action.NAV_LEFT, Action.NAV_RIGHT):
            self._horizontal(action, text)
        elif zone == "field" and action in _FIELD_KEYS:
            self._field_key(key, action, text)
        elif zone == "paste" and action in _FIELD_KEYS and action is not Action.PASTE:
            self._set_focus(("field", key))  # typed beside the field: into it
            self._field_key(key, action, text)
        elif action is Action.PASTE:
            target = key if zone == "paste" else self._paste_target()
            if target:
                self._paste_into(target)
        elif action in (Action.NAV_BACKSPACE, Action.PREVIOUS):
            self._back()
        return True

    def _field_key(self, key: str, action: Action, text: str) -> None:
        """A key in the field `key`, through ui/text_edit.py (the caret's keys always the
        field's); `changed` when its text changed."""
        f, edit = self._field(key), self._edits.get(key)
        if f is None or edit is None:
            return
        if f.secret and action in (Action.COPY, Action.CUT):
            return  # a secret is never copied out
        if self._busy and action in _CHANGING:
            return  # the text being checked stays
        if action is Action.FORGET:
            action = Action.NAV_DELETE
        before = edit.text
        edit_key(edit, action, text, arrows=True)
        self.update()
        if edit.text != before:
            self._text_changed(key, edit.text)

    def _text_changed(self, key: str, text: str) -> None:
        """A field's text changed by the keys or the mouse (FormCard: `changed`)."""

    def _horizontal(self, action: Action, payload: str) -> None:
        """←/→: the caret in a field (→ at its end: on to its Paste), back from Paste to the
        field, or between the buttons and between the alternates."""
        zone, key = self._focus
        step = -1 if action is Action.NAV_LEFT else 1
        if zone == "field":
            f, edit = self._field(key), self._edits.get(key)
            if f is None or edit is None:
                return
            at_end = edit.cursor == len(edit.text) and not edit.has_selection()
            if step > 0 and f.paste and at_end and "select" not in payload.split():
                self._set_focus(("paste", key))
                return
            edit_key(edit, action, payload, arrows=True)
            self.update()
        elif zone == "paste":
            if step < 0:
                self._set_focus(("field", key))
        elif zone in ("button", "alt"):
            keys = [b.key for b in (self._buttons if zone == "button" else self._alternates)]
            if key in keys:
                index = max(0, min(len(keys) - 1, keys.index(key) + step))
                self._set_focus((zone, keys[index]))

    def _buttons_stacked(self) -> bool:
        """Whether the buttons stand one under another (too wide for one row)."""
        rects = self._layout()
        tops = {round(rects[("button", b.key)].top()) for b in self._buttons}
        return len(tops) > 1

    def _rows(self) -> list[tuple[str, str]]:
        """What ↑/↓ go between: each field, the button row (its buttons too, while they stand
        one under another), the alternates."""
        rows = [("field", f.key) for f in self._fields]
        if self._buttons:
            rows.append(("button", ""))
        if self._alternates:
            rows.append(("alt", ""))
        return rows

    def _row_index(self) -> int:
        zone, key = self._focus
        rows = self._rows()
        if zone in ("field", "paste") and ("field", key) in rows:
            return rows.index(("field", key))
        if zone in ("button", "alt") and (zone, "") in rows:
            return rows.index((zone, ""))
        return 0

    def _vertical(self, delta: int) -> None:
        zone, key = self._focus
        keys = [b.key for b in self._buttons]
        if zone == "button" and abs(delta) == 1 and key in keys and self._buttons_stacked():
            at = keys.index(key) + delta  # one under another: ↑/↓ go between them first
            if 0 <= at < len(keys):
                self._set_focus(("button", keys[at]))
                return
        rows = self._rows()
        if not rows:
            return
        here = self._row_index()
        there = max(0, min(len(rows) - 1, here + delta))
        if there == here:
            return
        zone, key = rows[there]
        if zone == "button":
            keys = [b.key for b in self._buttons]
            if delta < 0 and self._last_button in keys:
                key = self._last_button  # back from the alternates: the one it left
            else:
                key = self._main_button()
        elif zone == "alt":
            keys = [b.key for b in self._alternates]
            key = self._last_alt if self._last_alt in keys else keys[0]
        self._set_focus((zone, key))

    def _activate(self) -> None:
        """Enter: the focused button, or the accent one from a field (else the next row)."""
        zone, key = self._focus
        if zone == "field":
            accent = next((b for b in self._buttons if b.accent), None)
            if accent is not None:
                self._press(accent.key)
            else:
                self._vertical(1)
        elif zone == "paste":
            self._paste_into(key)
        elif zone in ("button", "alt"):
            self._press(key)

    def _press(self, key: str) -> None:
        if key == CANCEL:
            self._cancel()
        elif not self._busy:
            self._pressed(key)

    def _enabled(self, zone: str, key: str) -> bool:
        """Whether a control takes a press: while busy only Cancel does."""
        return not self._busy or (zone == "button" and key == CANCEL)

    def _main_button(self) -> str:
        """The button row's own: the accent one, else the last."""
        accent = next((b.key for b in self._buttons if b.accent), None)
        if accent is not None:
            return accent
        return self._buttons[-1].key if self._buttons else ""

    def _set_focus(self, focus: tuple[str, str]) -> None:
        old = self._focus
        if old == focus:
            return
        row = (("field", old[1]), ("paste", old[1]))
        if old in row and focus not in row and old[1] in self._edits:
            self._edits[old[1]].leave()  # the caret back at the end, where the remote types
        self._focus = focus
        if focus[0] == "button":
            self._last_button = focus[1]
        elif focus[0] == "alt":
            self._last_alt = focus[1]
        self.update()
        self._say()

    def _say(self) -> None:
        """Assistive technology hears what the keys are on: a field's label (never its text,
        which may be a password or a link with one), or a button's."""
        zone, key = self._focus
        if zone == "field":
            f = self._field(key)
            text = f.label if f is not None else ""
        elif zone == "paste":
            text = translate(PASTE_TEXT)
        else:
            buttons = self._buttons if zone == "button" else self._alternates
            text = next((b.text for b in buttons if b.key == key), "")
        if text:
            announce(self, text)

    # ---- the fields ---------------------------------------------------------------------------

    def _field(self, key: str) -> Optional[Field]:
        return next((f for f in self._fields if f.key == key), None)

    def _paste_target(self) -> str:
        """Ctrl/⌘+V off a field: the first field with Paste, else the first field."""
        pasted = next((f.key for f in self._fields if f.paste), None)
        if pasted is not None:
            return pasted
        return self._fields[0].key if self._fields else ""

    def _paste_into(self, key: str) -> None:
        """Paste: the clipboard's first line in place of the field's text, or of the part
        selected in it (a click in the field never splices a link into the old one); the keys
        go on in the field."""
        edit = self._edits.get(key)
        if edit is None or self._busy:
            return
        if self._focus != ("field", key):
            self._set_focus(("field", key))
        line = clipboard_line()
        if not line:
            return
        before = edit.text
        if edit.has_selection():
            edit.insert(line)
        else:
            edit.set_text(line)
        self.update()
        if edit.text != before:
            self._text_changed(key, edit.text)

    # ---- showing ------------------------------------------------------------------------------

    def _open(self) -> None:
        self._fit()
        if self.isHidden():
            self.show()
        self.raise_()
        self.update()
        self._others_repaint()

    def _covered(self) -> bool:
        """Whether another card shows over this one (the guard's question over a provider's
        form): this one paints nothing meanwhile, so that the question stands alone over the
        dimmed picture (the mockup's), and shows again as it goes."""
        parent = self.parentWidget()
        if parent is None:
            return False
        under = False
        for child in parent.children():  # (in the order they are stacked, the top one last)
            if child is self:
                under = True
            elif under and isinstance(child, _Card) and child.isVisible():
                return True
        return False

    def _others_repaint(self) -> None:
        """The other cards paint again: one under this one hides itself, or shows again."""
        parent = self.parentWidget()
        for child in parent.children() if parent is not None else ():
            if child is not self and isinstance(child, _Card) and child.isVisible():
                child.update()

    def _fit(self) -> None:
        parent = self.parentWidget()
        if parent is not None and self.geometry() != parent.rect():
            self.setGeometry(parent.rect())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.parentWidget()
            and event.type() == QEvent.Type.Resize
            and not self.isHidden()
        ):
            self._fit()
        return False

    # ---- layout -------------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        width = parent.width() if parent is not None else 0
        return ui_units(width or _FALLBACK_HOST_WIDTH)

    @staticmethod
    def _fonts(un: Units) -> dict[str, QFont]:
        return {
            "title": un.font(1.7, QFont.Weight.DemiBold),
            "body": un.font(1.2),
            "label": un.font(1.2),
            "field": un.font(1.15),
            "found": un.font(1.1),
            "button": un.font(1.1),
            "accent": un.font(1.1, QFont.Weight.DemiBold),
            "alt": un.font(1.05),
        }

    def _layout(self) -> _Rects:
        """Where everything is, the card in the middle of the host: its natural height, its
        gaps shrunk when the host is lower, and then its middle cut (the title at the top, the
        buttons and the alternates at the bottom)."""
        un = self._units()
        fonts = self._fonts(un)
        edge = un.px(_EDGE_U)
        width = float(max(1, min(self.width() - 2 * edge, max(un.px(_WIDTH_U), _MIN_WIDTH_PX))))
        room = float(max(1, self.height() - 2 * edge))
        rects, footer = self._place(un, fonts, width, 1.0)
        height = rects[PANEL].height()
        if height > room:
            fixed = self._place(un, fonts, width, 0.0)[0][PANEL].height()
            squeeze = max(0.0, (room - fixed) / (height - fixed)) if height > fixed else 0.0
            rects, footer = self._place(un, fonts, width, squeeze)
            height = rects[PANEL].height()
            if height > room:
                shift = height - room
                rects = {
                    name: rect.translated(0, -shift) if name[0] in _FOOTER else rect
                    for name, rect in rects.items()
                }
                middle = rects[MIDDLE]
                cut = max(0.0, footer - shift - middle.top())
                rects[MIDDLE] = QRectF(middle.left(), middle.top(), middle.width(), cut)
                rects[PANEL] = QRectF(0, 0, width, room)
                height = room
        dx, dy = (self.width() - width) / 2, (self.height() - height) / 2
        return {name: rect.translated(dx, dy) for name, rect in rects.items()}

    def _place(
        self, un: Units, fonts: dict[str, QFont], width: float, squeeze: float
    ) -> tuple[_Rects, float]:
        """The rects in the card's own coordinates, its gaps times `squeeze`, and where the
        footer (the buttons) starts."""

        def gap(units: float) -> float:
            return un.px(units) * squeeze

        pad = float(un.px(_PAD_X_U))
        left, inner = pad, max(1.0, width - 2 * pad)
        right = left + inner
        rects: _Rects = {}
        y = gap(_PAD_Y_U)
        title_h = _line_h(fonts["title"])
        rects[TITLE] = QRectF(left, y, inner, title_h)
        y += title_h
        middle_top = y
        if self._body or self._fields or self._found:
            y += gap(_TITLE_GAP_U)
        if self._body:
            rects[BODY] = QRectF(
                left, y, inner, self._wrapped_height(fonts["body"], self._body, inner)
            )
            y += rects[BODY].height()
        label_w = self._label_width(un, fonts, inner)
        field_h = max(float(un.px(_FIELD_H_U)), _line_h(fonts["field"]) + 2 * un.px(_TEXT_PAD_U))
        paste_w = _advance(fonts["button"], translate(PASTE_TEXT)) + 2 * un.px(_BUTTON_PAD_U)
        paste_w = min(paste_w, inner * _PASTE_SHARE)
        button_gap = float(un.px(_BUTTON_GAP_U))
        for index, f in enumerate(self._fields):
            if index or self._body:
                y += gap(_ROW_GAP_U)
            end = right
            if f.paste:
                rects[("paste", f.key)] = QRectF(right - paste_w, y, paste_w, field_h)
                end -= paste_w + button_gap
            label = max(0.0, label_w - un.px(_LABEL_GAP_U))
            rects[("label", f.key)] = QRectF(left, y, label, field_h)
            rects[("field", f.key)] = QRectF(
                left + label_w, y, max(0.0, end - left - label_w), field_h
            )
            y += field_h
            if index == 0 and self._found:  # what the first field (the link) was found to be
                y += gap(_FOUND_GAP_U)
                found_w = max(1.0, inner - label_w)
                found_h = self._wrapped_height(fonts["found"], self._found, found_w)
                rects[FOUND] = QRectF(left + label_w, y, found_w, found_h)
                y += found_h
        if self._found and not self._fields:
            if self._body:
                y += gap(_ROW_GAP_U)
            found_h = self._wrapped_height(fonts["found"], self._found, inner)
            rects[FOUND] = QRectF(left, y, inner, found_h)
            y += found_h
        y += gap(_FOOT_GAP_U)
        footer = y
        rects[MIDDLE] = QRectF(0, middle_top, width, footer - middle_top)
        y = self._place_buttons(rects, un, fonts, left, inner, y, gap)
        y = self._place_alternates(rects, un, fonts, left, inner, y, gap)
        y += gap(_PAD_Y_U)
        rects[PANEL] = QRectF(0, 0, width, y)
        return rects, footer

    @staticmethod
    def _wrapped_height(font: QFont, text: str, width: float) -> float:
        """How tall `text` is wrapped to `width` (its own line breaks kept), one line at least."""
        bound = QFontMetricsF(font).boundingRect(QRectF(0, 0, width, 100_000), _WRAP, text)
        return max(_line_h(font), float(math.ceil(bound.height())))

    def _label_width(self, un: Units, fonts: dict[str, QFont], inner: float) -> float:
        """The labels' column: the widest label's, at least 9 u, at most 40% of the card."""
        labels = [f.label for f in self._fields if f.label]
        if not labels:
            return 0.0
        widest = max(_advance(fonts["label"], label) for label in labels)
        return max(
            float(un.px(_LABEL_MIN_U)), min(widest + un.px(_LABEL_GAP_U), inner * _LABEL_SHARE)
        )

    def _button_width(self, un: Units, fonts: dict[str, QFont], button: Button) -> float:
        font = fonts["accent" if button.accent else "button"]
        return _advance(font, button.text) + 2 * un.px(_BUTTON_PAD_U)

    def _place_buttons(
        self,
        rects: _Rects,
        un: Units,
        fonts: dict[str, QFont],
        left: float,
        inner: float,
        y: float,
        gap: Callable[[float], float],
    ) -> float:
        """The buttons at the right, in their order, on one row; when they do not fit on one,
        each on its own row, one under another, as wide as the widest (the accent, last, at
        the bottom), never one of them wrapped alone onto a second row. The busy text goes at
        the left of the last row."""
        if not self._buttons:
            return y
        height = max(float(un.px(_BUTTON_H_U)), _line_h(fonts["button"]) + 2 * un.px(0.5))
        spacing = float(un.px(_BUTTON_GAP_U))
        widths = [min(inner, self._button_width(un, fonts, b)) for b in self._buttons]
        if sum(widths) + spacing * (len(widths) - 1) <= inner + 0.5:
            lines = [list(range(len(widths)))]
        else:
            widths = [max(widths)] * len(widths)
            lines = [[index] for index in range(len(widths))]
        for number, line in enumerate(lines):
            if number:
                y += max(2.0, gap(_BUTTON_GAP_U))
            total = sum(widths[i] for i in line) + spacing * (len(line) - 1)
            x = left + inner - total
            if number == len(lines) - 1:
                rects[BUSY] = QRectF(left, y, max(0.0, x - spacing - left), height)
            for index in line:
                rects[("button", self._buttons[index].key)] = QRectF(x, y, widths[index], height)
                x += widths[index] + spacing
            y += height
        return y

    def _place_alternates(
        self,
        rects: _Rects,
        un: Units,
        fonts: dict[str, QFont],
        left: float,
        inner: float,
        y: float,
        gap: Callable[[float], float],
    ) -> float:
        """The other ways, after a thin rule: "Or:" and each one, on more lines when needed."""
        if not self._alternates:
            return y
        y += gap(_ALT_GAP_U)
        rects[RULE] = QRectF(left, y, inner, 1.0)
        y += gap(_ALT_RULE_U)
        font = fonts["alt"]
        height = _line_h(font) + 2 * un.px(_ALT_PAD_Y_U)
        lead = _advance(font, translate(OR_TEXT))
        rects[LEAD] = QRectF(left, y, lead, height)
        space, pad = float(un.px(_ALT_SPACE_U)), float(un.px(_ALT_PAD_U))
        start = min(left + lead + space, left + inner / 2)
        x, right = start, left + inner
        for index, button in enumerate(self._alternates):
            width = max(1.0, min(right - start, _advance(font, button.text) + 2 * pad))
            if index and x + width > right + 0.5:
                y += height
                x = start
            rects[("alt", button.key)] = QRectF(x, y, width, height)
            x += width + space
        return y + height

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        if self._covered():
            return  # another card over it: that one alone shows, over its own dim
        un = self._units()
        fonts = self._fonts(un)
        rects = self._layout()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), qcolor(*_DIM))
        panel = rects[PANEL]
        fill_round(p, panel, qcolor(PALETTE.panel_raised), RADIUS_PANEL)
        p.setPen(QPen(qcolor(PALETTE.text, 0.1), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_PANEL, RADIUS_PANEL)
        p.save()
        p.setClipRect(panel)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(fonts["title"])
        p.setPen(qcolor(PALETTE.text))
        title = rects[TITLE]
        p.drawText(title, left, _elided(fonts["title"], self._title, title.width()))
        p.save()
        p.setClipRect(rects[MIDDLE], Qt.ClipOperation.IntersectClip)
        self._paint_middle(p, un, fonts, rects)
        p.restore()
        self._paint_footer(p, un, fonts, rects)
        p.restore()
        p.end()

    def _paint_middle(self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: _Rects) -> None:
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if BODY in rects:
            p.setFont(fonts["body"])
            p.setPen(qcolor(PALETTE.text, 0.8))
            p.drawText(rects[BODY], _WRAP, self._body)
        for f in self._fields:
            label = rects[("label", f.key)]
            p.setFont(fonts["label"])
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(label, left, _elided(fonts["label"], f.label, label.width()))
            self._paint_field(p, un, fonts, rects[("field", f.key)], f)
            if f.paste:
                self._paint_button(
                    p,
                    un,
                    fonts,
                    rects[("paste", f.key)],
                    Button("", translate(PASTE_TEXT)),
                    focused=self._focus == ("paste", f.key),
                    hovered=self._hover == ("paste", f.key),
                    enabled=not self._busy,
                )
        if FOUND in rects and self._found:  # wrapped: a check's reason can be long
            p.setFont(fonts["found"])
            p.setPen(qcolor(PALETTE.ok if self._found_ok else PALETTE.warn))
            p.drawText(rects[FOUND], _WRAP, self._found)

    def _paint_field(
        self, p: QPainter, un: Units, fonts: dict[str, QFont], box: QRectF, f: Field
    ) -> None:
        focused = self._focus == ("field", f.key)
        radius = un.u * _RADIUS_U
        fill_round(p, box, qcolor(PALETTE.text, 0.08), radius)
        p.save()
        p.setPen(QPen(qcolor(PALETTE.accent, 0.8) if focused else qcolor(PALETTE.text, 0.18), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.restore()
        pad = un.px(_FIELD_PAD_U)
        inner = box.adjusted(pad, 0, -pad, 0)
        edit = self._edits[f.key]
        p.setFont(fonts["field"])
        paint_edit(
            p,
            inner,
            edit,
            qcolor(PALETTE.text),
            caret=focused,
            selection=qcolor(PALETTE.accent, SELECTION_ALPHA),
            shown=_MASK * len(edit.text) if f.secret else None,
        )
        if not edit.text and f.placeholder:
            shift = QFontMetricsF(fonts["field"]).height() * CARET_W + 2 if focused else 0.0
            room = inner.adjusted(shift, 0, 0, 0)
            p.setPen(qcolor(PALETTE.text, _PLACEHOLDER_ALPHA))
            p.drawText(
                room,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _elided(fonts["field"], f.placeholder, room.width()),
            )

    def _paint_footer(self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: _Rects) -> None:
        for button in self._buttons:
            self._paint_button(
                p,
                un,
                fonts,
                rects[("button", button.key)],
                button,
                focused=self._focus == ("button", button.key),
                hovered=self._hover == ("button", button.key),
                enabled=self._enabled("button", button.key),
            )
        if self._busy and BUSY in rects:
            busy = rects[BUSY]
            p.setFont(fonts["alt"])
            p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
            p.drawText(
                busy,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                _elided(fonts["alt"], translate(BUSY_TEXT), busy.width()),
            )
        if not self._alternates:
            return
        rule = rects[RULE]
        p.setPen(QPen(qcolor(PALETTE.text, 0.08), 1.0))
        p.drawLine(QPointF(rule.left(), rule.top()), QPointF(rule.right(), rule.top()))
        font = fonts["alt"]
        p.setFont(font)
        p.setPen(qcolor(PALETTE.text, 0.55))
        p.drawText(rects[LEAD], Qt.AlignmentFlag.AlignVCenter, translate(OR_TEXT))
        radius = un.u * _RADIUS_U
        for button in self._alternates:
            rect = rects[("alt", button.key)]
            p.save()
            if not self._enabled("alt", button.key):
                p.setOpacity(_DISABLED_OPACITY)
            if self._focus == ("alt", button.key):
                fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
                p.setPen(QPen(qcolor(PALETTE.accent), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            elif self._hover == ("alt", button.key):
                fill_round(p, rect, qcolor(PALETTE.text, 0.1), radius)
            p.setPen(qcolor(PALETTE.danger_text if button.danger else PALETTE.text))
            p.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                _elided(font, button.text, rect.width() - 2 * un.px(_ALT_PAD_U)),
            )
            p.restore()

    def _paint_button(
        self,
        p: QPainter,
        un: Units,
        fonts: dict[str, QFont],
        rect: QRectF,
        button: Button,
        *,
        focused: bool,
        hovered: bool,
        enabled: bool,
    ) -> None:
        """A button as the mockups draw it: outlined, the accent one filled (red when it is
        also the danger one), the focus a ring in the text's colour."""
        radius = un.u * _RADIUS_U
        p.save()
        if not enabled:
            p.setOpacity(_DISABLED_OPACITY)
            hovered = False
        if button.accent:
            base = PALETTE.live if button.danger else PALETTE.accent
            fill_round(p, rect, qcolor(base, 1.0 if hovered else 0.92), radius)
            tone = qcolor(PALETTE.text if button.danger else PALETTE.canvas)
        else:
            if hovered:
                tint = qcolor(PALETTE.live if button.danger else PALETTE.text, 0.12)
                fill_round(p, rect, tint, radius)
            edge = qcolor(PALETTE.live, 0.6) if button.danger else qcolor(PALETTE.text, 0.25)
            p.setPen(QPen(edge, 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            tone = qcolor(PALETTE.danger_text if button.danger else PALETTE.text)
        if focused:
            p.setPen(QPen(qcolor(PALETTE.text), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            ring = rect.adjusted(-2, -2, 2, 2)
            p.drawRoundedRect(ring, radius + 2, radius + 2)
        font = fonts["accent" if button.accent else "button"]
        p.setFont(font)
        p.setPen(tone)
        room = rect.width() - 2 * min(un.px(_BUTTON_PAD_U), rect.width() / 4)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, _elided(font, button.text, room))
        p.restore()

    def painted_text(self, key: str) -> str:
        """What the field `key` last painted (a secret's dots): for tests."""
        edit = self._edits.get(key)
        return edit.painted[2] if edit is not None and edit.painted is not None else ""

    # ---- the mouse ----------------------------------------------------------------------------

    def _hit(self, pos: QPointF) -> Optional[tuple[str, str]]:
        """What is at `pos`: (a zone of _HITS, its key), ("panel", "") elsewhere on the card,
        or None on the dimmed picture."""
        rects = self._layout()
        if not rects[PANEL].contains(pos):
            return None
        middle = rects[MIDDLE]
        for name, rect in rects.items():
            if name[0] not in _HITS or not rect.contains(pos):
                continue
            if name[0] in _MIDDLE and not middle.contains(pos):
                continue
            return name
        return PANEL

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._others_repaint()  # one under it: hidden behind it
        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            # Shown by a press (on a row under it: "Remove provider…"): a double-click's second
            # press would land on the card (Qt makes it a press on another widget), on the
            # dimmed picture, and cancel it. It reaches nothing.
            self._second_click.arm()

    def hideEvent(self, event: QHideEvent) -> None:
        if self.isHidden():
            # Gone by a key or its caller before a double-click's second press came: the guard
            # a press that showed it armed goes with it, so no later press is swallowed, in
            # any window. A press that hides it arms the guard again (mousePressEvent).
            self._second_click.disarm()
        super().hideEvent(event)
        self._others_repaint()  # one under it: shown again

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never the picture's under it
        self._mouse_press(event)
        if self.isHidden():  # cancelled, answered or closed by its caller: a double-click's
            self._second_click.arm()  # second press reaches nothing behind the card

    def _mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self._cancel()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        hit = self._hit(pos)
        if hit is None:
            self._cancel()  # the dimmed picture
            return
        zone, key = hit
        if zone in ("field", "label"):
            self._set_focus(("field", key))
            if zone == "field" and key in self._edits:
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._drag.press(self._edits[key], pos.x(), shift=shift)
                self.update()
        elif zone == "paste":
            self._paste_into(key)
        elif zone in ("button", "alt") and self._enabled(zone, key):
            self._set_focus((zone, key))
            self._press(key)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()
        if event.button() != Qt.MouseButton.LeftButton:
            self.mousePressEvent(event)  # a quick second Back is one more
            return
        hit = self._hit(event.position())
        if hit is None:
            self.mousePressEvent(event)
        elif hit[0] == "field" and hit[1] in self._edits:
            self._drag.double(self._edits[hit[1]], event.position().x())  # a word
            self.update()
        # on a button the first click pressed it already: once is enough

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag.release()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton and self._drag.move(event.position().x()):
            self.update()  # a drag in a field selects
        hit = self._hit(event.position())
        hover = hit if hit is not None and hit[0] in ("paste", "button", "alt") else None
        if hover != self._hover:
            self._hover = hover
            self.update()
        event.accept()

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()  # never the host's channel change

    def tooltip_at(self, pos: QPointF) -> str:
        hit = self._hit(pos)
        if hit is not None and hit[0] == "paste":
            return key_hint(translate(PASTE_TEXT), "Ctrl+V")
        return ""


class FormCard(_Card):
    """A card of fields: adding a provider, or its address and login."""

    pressed = Signal(str)  # a button's or an alternate's key (never Cancel's)
    changed = Signal(str, str)  # a field's key and its new text, as typed or pasted
    cancelled = Signal()  # Esc, Cancel, the dimmed picture or the mouse's Back: it hid

    def set_form(
        self,
        title: str,
        fields: list[Field],
        found: str,
        buttons: list[Button],
        alternates: list[Button],
    ) -> None:
        """Show the form (texts in the app's language; `found` good). Set again while it shows
        (a field appears, a button changes), the focus and each remaining field's caret stay
        where they were, a field's text is the one given (pass `values()` back for the fields
        the user may have changed), and the same `found` keeps its colour (a failure stays
        one); nothing is `changed`. Shown afresh, its first field has the keys and nothing
        waits."""
        fresh = self.isHidden()
        old = {} if fresh else self._edits
        self._title, self._body = title, ""
        self._fields = list(fields)
        edits: dict[str, TextEdit] = {}
        for f in self._fields:
            edit = old.get(f.key)
            if edit is None:
                edit = TextEdit(f.text)
            else:
                edit.set_text(f.text)  # the same text: the caret stays
            edits[f.key] = edit
        self._edits = edits
        if self._drag.active is not None and self._drag.active not in edits.values():
            self._drag.release()
        if fresh or found != self._found:
            self._found_ok = True
        self._found = found
        self._buttons, self._alternates = list(buttons), list(alternates)
        self._hover = None
        if fresh:
            self._busy = False
            self._last_button = self._last_alt = ""
        if fresh:
            self._focus = self._first_focus()
        elif not self._focus_exists():
            self._focus = self._nearest_focus()
        self._open()
        if fresh:
            announce(self, title)

    def _focus_exists(self) -> bool:
        zone, key = self._focus
        if zone == "field":
            return key in self._edits
        if zone == "paste":
            f = self._field(key)
            return f is not None and f.paste
        if zone in ("button", "alt"):
            buttons = self._buttons if zone == "button" else self._alternates
            return any(b.key == key for b in buttons)
        return False

    def _nearest_focus(self) -> tuple[str, str]:
        """The focused control went: its field (it lost its Paste), the button row's own
        button, the first alternate, else the first field."""
        zone, key = self._focus
        if zone == "paste" and key in self._edits:
            return ("field", key)
        if zone in ("button", "alt") and self._buttons:
            return ("button", self._main_button())
        return self._first_focus()

    def _first_focus(self) -> tuple[str, str]:
        if self._fields:
            return ("field", self._fields[0].key)
        if self._buttons:
            return ("button", self._main_button())
        if self._alternates:
            return ("alt", self._alternates[0].key)
        return ("", "")

    def values(self) -> dict[str, str]:
        """Each field's text, by its key."""
        return {f.key: self._edits[f.key].text for f in self._fields}

    def focus_field(self, key: str) -> None:
        """The keys on the field `key` (the one its caller asks to fill in next); a key of no
        field leaves them where they are."""
        if key in self._edits:
            self._set_focus(("field", key))

    def set_found(self, text: str, ok: bool) -> None:
        """What the link was found to be ("✓ Xtream at …"), or what went wrong (not `ok`)."""
        self._found, self._found_ok = text, ok
        self.update()

    def found(self) -> tuple[str, bool]:
        return self._found, self._found_ok

    def set_busy(self, busy: bool) -> None:
        """A check is on its way (it says "Checking…"): the buttons wait, but Cancel, and the
        fields keep their text."""
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._hover = None
            announce(self, translate(BUSY_TEXT))
        self.update()

    def _text_changed(self, key: str, text: str) -> None:
        self.changed.emit(key, text)

    def _pressed(self, key: str) -> None:
        self.pressed.emit(key)

    def _back(self) -> None:
        """Back on a button or an alternate is Cancel; the player's Back (PREVIOUS) in a field
        is nothing (Backspace there deletes)."""
        if self._focus[0] in ("button", "alt"):
            self._cancel()

    def _cancel(self) -> None:
        if self.isHidden():
            return
        self._drag.release()
        self.hide()
        self.cancelled.emit()


class ChoiceCard(_Card):
    """A question with two or three answers ("This link is another provider", "Remove …?")."""

    answered = Signal(str)  # the button's key; "cancel" for Esc, Back and the dimmed picture

    def ask(self, title: str, body: str, buttons: list[Button], default: str) -> None:
        """Show the question (a question still shown is replaced, unanswered). The keys start
        on `default`, else the accent button, else Cancel, else the first."""
        self._title, self._body = title, body
        self._fields, self._edits = [], {}
        self._found, self._found_ok = "", True
        self._buttons, self._alternates = list(buttons), []
        self._busy = False
        self._hover = None
        keys = [b.key for b in self._buttons]
        accent = next((b.key for b in self._buttons if b.accent), None)
        if default in keys:
            key = default
        elif accent is not None:
            key = accent
        elif CANCEL in keys:
            key = CANCEL
        else:
            key = keys[0] if keys else ""
        self._focus = ("button", key)
        self._last_button = key
        self._open()
        announce(self, "\n".join(part for part in (title, body) if part), assertive=True)

    def _answer(self, key: str) -> None:
        if self.isHidden():
            return
        self.hide()
        self.answered.emit(key)

    def _pressed(self, key: str) -> None:
        self._answer(key)

    def _cancel(self) -> None:
        self._answer(CANCEL)

    def _back(self) -> None:
        self._answer(CANCEL)


class RangeCard(_Card):
    """A value on a track, for what would clutter a row (a cache size, say)."""

    answered = Signal(object)  # the value kept, or None for Esc, Back, Cancel, the dimmed picture

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._value = self._min = 0
        self._max = self._step = 1
        self._text: Callable[[int], str] = str
        self._note: Callable[[int], str] = lambda value: ""
        self._dragging = False

    def ask(
        self,
        title: str,
        value: int,
        minimum: int,
        maximum: int,
        step: int,
        text: Callable[[int], str],
        note: Callable[[int], str],
    ) -> None:
        """Show the card (one still shown is replaced, unanswered), the keys on the track.
        `text` words the value ("1024 MiB"), `note` the line under the track."""
        self._title, self._body = title, ""
        self._fields, self._edits = [], {}
        self._found, self._found_ok = "", True
        self._min, self._max = int(minimum), max(int(minimum), int(maximum))
        self._step = max(1, int(step))
        self._text, self._note = text, note
        self._value = self._clamp(int(value))
        self._buttons = [
            Button(CANCEL, translate(CANCEL_TEXT)),
            Button(OK, translate(OK_TEXT), accent=True),
        ]
        self._alternates = []
        self._busy = self._dragging = False
        self._hover = None
        self._focus = ("track", "")
        self._last_button = OK
        self._open()
        announce(self, "\n".join((title, self._text(self._value), self._note(self._value))))

    def value(self) -> int:
        return self._value

    def value_text(self) -> str:
        return self._text(self._value)

    def note_text(self) -> str:
        return self._note(self._value)

    def _clamp(self, value: int) -> int:
        return max(self._min, min(self._max, value))

    def _set_value(self, value: int, *, say: bool = True) -> None:
        value = self._clamp(value)
        if value == self._value:
            return
        self._value = value
        self.update()
        if say:
            self._say()

    def _at(self, x: float) -> int:
        """The value at the card's x: on the grid of steps from the minimum."""
        track = self._track_line()
        if track.width() <= 0 or self._max == self._min:
            return self._min
        fraction = max(0.0, min(1.0, (x - track.left()) / track.width()))
        steps = round(fraction * (self._max - self._min) / self._step)
        return self._clamp(self._min + steps * self._step)

    def _track_line(self) -> QRectF:
        """Where the thumb's centre travels, in the card's coordinates."""
        row = self.control_rect("track")
        edge = self._units().px(_THUMB_U)
        return QRectF(row.left() + edge, row.center().y(), max(0.0, row.width() - 2 * edge), 0)

    # ---- the keys -------------------------------------------------------------------------

    def handle(self, action: Action, payload: object = "") -> bool:
        if not self.isHidden() and self._focus[0] == "track":
            moves = {
                Action.NAV_PAGE_UP: self._value + _PAGE_STEPS * self._step,
                Action.NAV_PAGE_DOWN: self._value - _PAGE_STEPS * self._step,
                Action.NAV_HOME: self._min,
                Action.NAV_END: self._max,
            }
            if action in moves:
                self._set_value(moves[action])
                return True
        return super().handle(action, payload)

    def _horizontal(self, action: Action, payload: str) -> None:
        if self._focus[0] != "track":
            super()._horizontal(action, payload)
            return
        self._set_value(self._value + (-self._step if action is Action.NAV_LEFT else self._step))

    def _rows(self) -> list[tuple[str, str]]:
        return [("track", ""), ("button", "")]

    def _activate(self) -> None:
        if self._focus[0] == "track":
            self._keep()
        else:
            super()._activate()

    def _say(self) -> None:
        if self._focus[0] == "track":
            announce(self, f"{self._text(self._value)}. {self._note(self._value)}")
        else:
            super()._say()

    def _keep(self) -> None:
        if self.isHidden():
            return
        self._dragging = False
        self.hide()
        self.answered.emit(self._value)

    def _pressed(self, key: str) -> None:
        self._keep()

    def _cancel(self) -> None:
        if self.isHidden():
            return
        self._dragging = False
        self.hide()
        self.answered.emit(None)

    def _back(self) -> None:
        self._cancel()

    # ---- the layout and the painting --------------------------------------------------------

    @staticmethod
    def _fonts(un: Units) -> dict[str, QFont]:
        fonts = _Card._fonts(un)
        fonts["value"] = un.font(1.6, QFont.Weight.DemiBold)
        return fonts

    def _place(
        self, un: Units, fonts: dict[str, QFont], width: float, squeeze: float
    ) -> tuple[_Rects, float]:
        def gap(units: float) -> float:
            return un.px(units) * squeeze

        pad = float(un.px(_PAD_X_U))
        left, inner = pad, max(1.0, width - 2 * pad)
        rects: _Rects = {}
        y = gap(_PAD_Y_U)
        title_h = _line_h(fonts["title"])
        rects[TITLE] = QRectF(left, y, inner, title_h)
        y += title_h
        middle_top = y + gap(_TITLE_GAP_U)
        y = middle_top
        value_h = _line_h(fonts["value"])
        rects[("value", "")] = QRectF(left, y, inner, value_h)
        y += value_h + gap(_ROW_GAP_U)
        row_h = float(un.px(_TRACK_ROW_U))
        rects[("track", "")] = QRectF(left, y, inner, row_h)
        y += row_h + gap(_ROW_GAP_U)
        note = self._note(self._value)
        note_h = self._wrapped_height(fonts["found"], note, inner)
        rects[("note", "")] = QRectF(left, y, inner, note_h)
        y += note_h + gap(_FOOT_GAP_U)
        footer = y
        rects[MIDDLE] = QRectF(0, middle_top, width, footer - middle_top)
        y = self._place_buttons(rects, un, fonts, left, inner, y, gap)
        y += gap(_PAD_Y_U)
        rects[PANEL] = QRectF(0, 0, width, y)
        return rects, footer

    def _paint_middle(self, p: QPainter, un: Units, fonts: dict[str, QFont], rects: _Rects) -> None:
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        value = rects[("value", "")]
        p.setFont(fonts["value"])
        p.setPen(qcolor(PALETTE.text))
        p.drawText(value, left, _elided(fonts["value"], self._text(self._value), value.width()))
        row = rects[("track", "")]
        edge = float(un.px(_THUMB_U))
        x0, x1, cy = row.left() + edge, row.right() - edge, row.center().y()
        thick = max(2.0, float(un.px(_TRACK_H_U)))
        line = QRectF(x0, cy - thick / 2, max(0.0, x1 - x0), thick)
        fill_round(p, line, qcolor(PALETTE.text, _TRACK_ALPHA), thick / 2)
        span = self._max - self._min
        fraction = (self._value - self._min) / span if span else 0.0
        cx = x0 + (x1 - x0) * fraction
        if self._focus == ("track", ""):
            p.save()
            p.setPen(QPen(qcolor(PALETTE.text), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), edge + 3, edge + 3)
            p.restore()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(PALETTE.accent))
        p.drawEllipse(QPointF(cx, cy), edge, edge)
        p.restore()
        note = rects[("note", "")]
        p.setFont(fonts["found"])
        p.setPen(qcolor(PALETTE.text, PALETTE.text_muted_alpha))
        p.drawText(note, _WRAP, self._note(self._value))

    # ---- the mouse --------------------------------------------------------------------------

    def _mouse_press(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit(event.position())
            if hit is not None and hit[0] == "track":
                self._set_focus(("track", ""))
                self._dragging = True
                self._set_value(self._at(event.position().x()))
                return
        super()._mouse_press(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self._set_value(self._at(event.position().x()), say=False)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self._dragging = False
            self._say()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        hit = self._hit(event.position())
        if hit is not None and hit[0] == "track":
            self.mousePressEvent(event)  # quick clicks on the track are clicks
            return
        super().mouseDoubleClickEvent(event)


def pick_folder(parent: Optional[QWidget], title: str, start: str) -> Optional[str]:
    """The system's folder picker; the folder chosen, or None. Tests patch this one place."""
    path = QFileDialog.getExistingDirectory(parent, title, start)
    return path or None


def pick_file(parent: Optional[QWidget], title: str, filters: str) -> Optional[str]:
    """The system's file picker (`filters` as Qt words them: "M3U (*.m3u *.m3u8)"); the file
    chosen, or None. Tests patch this one place."""
    path, _selected = QFileDialog.getOpenFileName(parent, title, "", filters)
    return path or None
