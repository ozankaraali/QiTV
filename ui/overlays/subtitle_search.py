"""The OpenSubtitles search over the player (2.2): a page for the API key, then the results.

"Search OpenSubtitles…" on the Subtitles panel opens it (ui/subtitle_search.py). It covers the
player with a dimmed backdrop and a panel in the middle; a click on the backdrop, × or Esc
closes it.

Without an API key it shows the key page: what OpenSubtitles needs in plain words, the key's
field (masked but for its last four characters), and Get a free key (the OpenSubtitles page
where one signs up and makes a key, in the browser), Paste and Save. With a key it shows the
search: the field with what is searched for ("Title (2019)", "Series S01E02") and Search,
a line that says what is going on (searching, how many were found, the downloads left today,
or what went wrong), and two lists: the languages that have subtitles on the left, each with
how many, and the selected language's subtitles on the right, best first, each with its
release, its downloads and marks for the hearing impaired and machine translations. A click
on a language shows its subtitles; a click on one (or Enter) downloads it.

The keys reach it through the shell (the "subtitles" key context, ui/keymap.py): typed text
and Ctrl/⌘+V go into the field, Backspace deletes, ↑/↓ move in a list (up from its top to
the field), ←/→ go between the lists (and the field and the buttons beside it: Paste and
Search, or the key page's), Enter chooses and Esc closes. 1 u = 1% of the overlay host's
width.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Optional, Sequence

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QHelpEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QToolTip, QWidget

from services import i18n
from services.i18n import N_, tr_n, translate
from services.opensubtitles import SubtitleFile, shown_release
from ui.keymap import Action, key_hint
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, draw_icon, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

KEY_PAGE, RESULTS_PAGE = "key", "results"
# The texts are kept in English (N_) and shown translated (translate).
TITLE_TEXT = N_("Subtitles from OpenSubtitles")  # i18n: the OpenSubtitles page's title
# i18n: the OpenSubtitles page without an API key: its first words, before the two steps
KEY_INTRO = N_(
    "QiTV finds subtitles on OpenSubtitles.com. It needs a free OpenSubtitles account and an "
    "API key, which takes a minute:"
)
KEY_STEPS = (
    # i18n: a step on the OpenSubtitles key page; "Get a free key" is its button's label
    N_(
        "1.  Get a free key opens the OpenSubtitles website: sign up there, then create an API "
        "key on its API consumers page."
    ),
    # i18n: a step on the OpenSubtitles key page; "Paste" and "Save" are its buttons' labels
    N_("2.  Copy the key, Paste it below and Save."),
)
KEY_LATER = N_("You can change it later in Settings → Subtitles.")  # i18n: the key page's end
KEY_PLACEHOLDER = N_("Paste your API key here")  # i18n: the key's empty field
KEY_EMPTY_TEXT = N_("Paste the key first.")  # i18n: Save was pressed with no key
QUERY_PLACEHOLDER = N_("Film or series title")  # i18n: the OpenSubtitles search's empty field
# i18n: the OpenSubtitles page's list titles, shown in capitals; max ~16 letters
LANGUAGES_TITLE = N_("Languages")
FILES_TITLE = N_("Subtitles")  # i18n: shown in capitals; max ~16 letters
HI_TEXT = N_("Hearing impaired")  # i18n: a mark on a subtitle (for the hearing impaired)
MACHINE_TEXT = N_("Machine translated")  # i18n: a mark on a subtitle
TRUSTED_TEXT = N_("Trusted uploader")  # i18n: a mark on a subtitle
# The buttons: their label, what they do and their key (ui.keymap.SHORTCUTS["subtitles"]).
BUTTONS = {
    # i18n: the key page's button that opens the OpenSubtitles website, then its tooltip
    "get_key": (N_("Get a free key"), N_("Open the OpenSubtitles website"), ""),
    "paste": (N_("Paste"), N_("Paste"), "Ctrl+V"),  # i18n: a button and its tooltip (verb)
    "save": (N_("Save"), N_("Save the key"), "Return"),  # i18n: a button, then its tooltip
    "search": (N_("Search"), N_("Search"), "Return"),  # i18n: a button and its tooltip (verb)
}
KEY_BUTTONS = ("get_key", "paste", "save")
SEARCH_ROW = ("field", "paste", "search")  # the search's top row, left to right
CLOSE_TIP = (N_("Close"), "Esc")  # i18n: a tooltip (verb)
CLEAR_TIP = (N_("Clear"), "Backspace")  # i18n: a tooltip: empties the field (verb)

_EDGE_U = 2.0  # from the host's sides
_WIDTH_U = 66.0
_MIN_WIDTH_PX = 300
_HEIGHT_U = 48.0
_PAD_U = 1.6
_GAP_U = 0.8
_HEADER_U = 3.0
_CLOSE_U = 2.4
_FIELD_U = 3.0
_BUTTON_U = 3.0
_BUTTON_PAD_U = 1.4
_STATUS_U = 2.4
_LIST_TITLE_U = 2.0
_LANG_ROW_U = 2.8
_FILE_ROW_U = 4.4
_LANG_SHARE = 0.32  # of the lists' width
_ICON_U = 1.4
_WHEEL_STEP = 120
_MASK = "•"
_MASK_MAX = 24


def key_words() -> str:
    """The key page's words, in the app's language."""
    steps = (translate(step) for step in KEY_STEPS)
    return "\n".join((translate(KEY_INTRO), *steps, "", translate(KEY_LATER)))


def masked(key: str) -> str:
    """A key as the page shows it: dots, but for its last four characters."""
    if len(key) <= 4:
        return _MASK * len(key)
    return _MASK * min(len(key) - 4, _MASK_MAX) + key[-4:]


@dataclass(frozen=True)
class LanguageRow:
    code: str
    name: str
    count: int


@dataclass
class _Rects:
    panel: QRectF
    title: QRectF
    close: QRectF
    entry: QRectF = field(default_factory=QRectF)  # the text field
    clear: QRectF = field(default_factory=QRectF)
    buttons: dict[str, QRectF] = field(default_factory=dict)
    text: QRectF = field(default_factory=QRectF)  # the key page's words
    message: QRectF = field(default_factory=QRectF)  # the key page's problem
    status: QRectF = field(default_factory=QRectF)
    lists: dict[str, QRectF] = field(default_factory=dict)  # "languages", "files": their rows
    titles: dict[str, QRectF] = field(default_factory=dict)  # above each list


class SubtitleSearchPage(Overlay):
    saveKeyRequested = Signal(str)
    getKeyRequested = Signal()
    searchRequested = Signal(str)
    fileChosen = Signal(object)  # a SubtitleFile
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._page = RESULTS_PAGE
        self._key = ""
        self._message = ""  # the key page's problem
        self._query = ""
        self._status = ""
        self._status_kind = "info"  # "info", "error" or "busy"
        self._busy = False  # a download is on its way: nothing more is chosen
        self._languages: list[LanguageRow] = []
        self._files: dict[str, list[SubtitleFile]] = {}
        self._language = 0  # the language whose subtitles show
        self._file = 0  # the subtitle the keys are on
        self._focus = "field"
        self._scroll = {"languages": 0, "files": 0}
        self._hover: Optional[tuple[str, int]] = None
        self._wheel = 0
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- what it shows ------------------------------------------------------------------------

    def show_key_page(self, message: str = "", key: str = "") -> None:
        """Ask for an API key; `message` says what went wrong with the last one."""
        self._page, self._message, self._key = KEY_PAGE, message, key
        self._busy = False
        self._focus = "field"
        self._open()

    def show_search(self, query: str, status: str = "") -> None:
        """The search for `query`, waiting for its results (`status`: "Searching…")."""
        self._page, self._query = RESULTS_PAGE, query
        self._languages, self._files = [], {}
        self._language = self._file = 0
        self._scroll = {"languages": 0, "files": 0}
        self._busy = False
        self._focus = "field"
        self.set_status(status, "busy" if status else "info")
        self._open()

    def set_results(
        self,
        languages: Sequence[LanguageRow],
        files: dict[str, list[SubtitleFile]],
        status: str,
        *,
        kind: str = "info",
    ) -> None:
        """What the search found: `languages` in order (the first selected), each language's
        subtitles best first."""
        self._languages, self._files = list(languages), dict(files)
        self._language = self._file = 0
        self._scroll = {"languages": 0, "files": 0}
        self._busy = False
        self._focus = "files" if self.files_shown() else "field"
        self.set_status(status, kind)

    def set_status(self, text: str, kind: str = "info") -> None:
        """What the line under the field says ("busy": something is on its way, in full
        colour; "error": what went wrong). A download on its way is over."""
        self._status, self._status_kind, self._busy = text, kind, False
        self.update()

    def set_busy(self, text: str) -> None:
        """A download is on its way: it says so, and nothing more is chosen meanwhile."""
        self._status, self._status_kind, self._busy = text, "busy", True
        self.update()

    def dismiss(self) -> None:
        if not self.isHidden():
            self.hide()
            self.closed.emit()

    def _open(self) -> None:
        self._fit()
        if self.isHidden():
            self.show()
        self.raise_()
        self.update()

    # ---- what it holds (tests, the controller) ----------------------------------------------

    def page(self) -> str:
        return self._page

    def key_text(self) -> str:
        return self._key

    def query(self) -> str:
        return self._query

    def status(self) -> str:
        return self._status

    def message(self) -> str:
        return self._message

    def focus(self) -> str:
        """ "field", "languages", "files" or a button's name."""
        return self._focus

    def languages(self) -> list[LanguageRow]:
        return list(self._languages)

    def selected_language(self) -> str:
        if not self._languages:
            return ""
        return self._languages[self._language].code

    def files_shown(self) -> list[SubtitleFile]:
        return list(self._files.get(self.selected_language(), []))

    def highlighted_file(self) -> Optional[SubtitleFile]:
        files = self.files_shown()
        return files[self._file] if 0 <= self._file < len(files) else None

    def busy(self) -> bool:
        return self._busy

    # ---- the keys -----------------------------------------------------------------------------

    def handle(self, action: Action, payload: str = "") -> bool:
        """A key while it shows (False: not its own)."""
        if self.isHidden():
            return False
        if action is Action.ESCAPE:
            self.dismiss()
        elif action is Action.NAV_TEXT:
            self.type_text(payload)
        elif action is Action.NAV_BACKSPACE:
            self.backspace()
        elif action is Action.PASTE:
            self.paste()
        elif action in (Action.NAV_UP, Action.NAV_DOWN):
            self.move_focus(-1 if action is Action.NAV_UP else 1)
        elif action in (Action.NAV_PAGE_UP, Action.NAV_PAGE_DOWN):
            page = max(1, self._visible_rows(self._focus) - 1)
            self.move_focus(-page if action is Action.NAV_PAGE_UP else page, page=True)
        elif action in (Action.NAV_LEFT, Action.NAV_RIGHT):
            self.side(-1 if action is Action.NAV_LEFT else 1)
        elif action is Action.NAV_ACTIVATE:
            self.activate()
        else:
            return False
        return True

    def type_text(self, text: str) -> None:
        if self._page == KEY_PAGE:
            self._key += text.strip()
            self._message = ""
        else:
            self._query += text
        self._focus = "field"
        self.update()

    def backspace(self) -> None:
        if self._page == KEY_PAGE:
            self._key = self._key[:-1]
        else:
            self._query = self._query[:-1]
        self._focus = "field"
        self.update()

    def clear_field(self) -> None:
        if self._page == KEY_PAGE:
            self._key = ""
        else:
            self._query = ""
        self._focus = "field"
        self.update()

    def paste(self) -> None:
        """The clipboard's first line into the field (a key loses its spaces)."""
        clipboard = QGuiApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        line = next((part.strip() for part in text.splitlines() if part.strip()), "")
        if not line:
            return
        if self._page == KEY_PAGE:
            self._key = "".join(line.split())  # pasting replaces: one key at a time
            self._message = ""
            self._focus = "save"
        else:
            self._query += line
            self._focus = "field"
        self.update()

    def move_focus(self, delta: int, *, page: bool = False) -> None:
        """↑/↓: in a list, or between the field and what is below it."""
        focus = self._focus
        if self._page == KEY_PAGE:
            if delta > 0 and focus == "field":
                self._focus = "save" if self._key else "get_key"
            elif delta < 0 and focus in KEY_BUTTONS:
                self._focus = "field"
        elif focus in SEARCH_ROW:
            if delta > 0:
                self._focus = "files" if self.files_shown() else "field"
                if self._focus == "files":
                    self._set_file(0)
                elif self._languages:
                    self._focus = "languages"
        elif focus == "languages":
            if delta < 0 and self._language == 0 and not page:
                self._focus = "field"
            else:
                self._set_language(self._language + delta)
        elif focus == "files":
            if delta < 0 and self._file == 0 and not page:
                self._focus = "field"
            else:
                self._set_file(self._file + delta)
        self.update()

    def side(self, delta: int) -> None:
        """←/→: between the languages and the subtitles, the field and Search, or the buttons."""
        focus = self._focus
        if self._page == KEY_PAGE:
            if focus in KEY_BUTTONS:
                index = KEY_BUTTONS.index(focus) + delta
                self._focus = KEY_BUTTONS[max(0, min(len(KEY_BUTTONS) - 1, index))]
        elif focus in SEARCH_ROW:
            index = SEARCH_ROW.index(focus) + delta
            self._focus = SEARCH_ROW[max(0, min(len(SEARCH_ROW) - 1, index))]
        elif focus == "languages" and delta > 0 and self.files_shown():
            self._focus = "files"
            self._set_file(self._file)
        elif focus == "files" and delta < 0 and self._languages:
            self._focus = "languages"
            self._scroll_to("languages", self._language)
        self.update()

    def activate(self) -> None:
        """Enter: what the keys are on."""
        focus = self._focus
        if focus in ("field", "save", "search") or focus in BUTTONS:
            self._press(focus)
        elif focus == "languages":
            if self.files_shown():
                self._focus = "files"
                self._set_file(0)
                self.update()
        elif focus == "files":
            self._choose(self._file)

    def _press(self, name: str) -> None:
        """A button (or Enter in the field)."""
        if self._page == KEY_PAGE:
            if name in ("field", "save"):
                key = "".join(self._key.split())
                if key:
                    self.saveKeyRequested.emit(key)
                else:
                    self._message = translate(KEY_EMPTY_TEXT)
                    self.update()
            elif name == "get_key":
                self.getKeyRequested.emit()
            elif name == "paste":
                self.paste()
        elif name == "paste":
            self.paste()
        elif name in ("field", "search"):
            query = " ".join(self._query.split())
            if query and not self._busy:
                self.searchRequested.emit(query)

    def _choose(self, index: int) -> None:
        files = self.files_shown()
        if self._busy or not 0 <= index < len(files):
            return
        self._file = index
        self.fileChosen.emit(files[index])
        self.update()

    def _set_language(self, index: int) -> None:
        if not self._languages:
            return
        index = max(0, min(len(self._languages) - 1, index))
        if index != self._language:
            self._language, self._file = index, 0
            self._scroll["files"] = 0
        self._scroll_to("languages", index)

    def _set_file(self, index: int) -> None:
        files = self.files_shown()
        if not files:
            return
        self._file = max(0, min(len(files) - 1, index))
        self._scroll_to("files", self._file)

    # ---- layout -------------------------------------------------------------------------------

    def _units(self) -> Units:
        parent = self.parentWidget()
        return Units(parent.width() if parent is not None else 1280)

    @staticmethod
    def _fonts(un: Units) -> dict[str, QFont]:
        return {
            "title": un.font(1.5, QFont.Weight.Bold),
            "body": un.font(1.15),
            "field": un.font(1.25),
            "button": un.font(1.15, QFont.Weight.DemiBold),
            "status": un.font(1.05),
            "list_title": un.font(0.95, QFont.Weight.DemiBold),
            "row": un.font(1.15),
            "meta": un.font(0.95),
        }

    def _fit(self) -> None:
        parent = self.parentWidget()
        if parent is not None and self.geometry() != parent.rect():
            self.setGeometry(parent.rect())

    def _panel(self, un: Units) -> QRectF:
        """The panel, in the middle: the key page as tall as its words, the search taller."""
        edge = un.px(_EDGE_U)
        width = max(1.0, float(min(self.width() - 2 * edge, max(un.px(_WIDTH_U), _MIN_WIDTH_PX))))
        height = float(un.px(_HEIGHT_U))
        if self._page == KEY_PAGE:
            pad, gap = un.px(_PAD_U), un.px(_GAP_U)
            words = self._words_height(un, width - 2 * pad)
            height = 2 * pad + un.px(_HEADER_U) + words + 3 * gap + un.px(_FIELD_U)
            height += un.px(_STATUS_U) + un.px(_BUTTON_U)
        height = max(1.0, min(float(self.height() - 2 * edge), height))
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _words_height(self, un: Units, width: float) -> float:
        """How tall the key page's words are, wrapped to `width`."""
        flags = int(Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap)
        bound = QFontMetricsF(self._fonts(un)["body"]).boundingRect(
            QRectF(0, 0, max(1.0, width), 10_000), flags, key_words()
        )
        return math.ceil(bound.height())

    def _button_width(self, un: Units, name: str) -> float:
        label = translate(BUTTONS[name][0])
        advance = QFontMetricsF(self._fonts(un)["button"]).horizontalAdvance(label)
        return advance + 2 * un.px(_BUTTON_PAD_U)

    def _rects(self) -> _Rects:
        un = self._units()
        pad, gap = float(un.px(_PAD_U)), float(un.px(_GAP_U))
        panel = self._panel(un)
        inner = panel.adjusted(pad, pad, -pad, -pad)
        close = float(un.px(_CLOSE_U))
        header = float(un.px(_HEADER_U))
        rects = _Rects(
            panel,
            QRectF(inner.left(), inner.top(), inner.width() - close - gap, header),
            QRectF(inner.right() - close, inner.top() + (header - close) / 2, close, close),
        )
        y = inner.top() + header + gap
        field_h, button_h = float(un.px(_FIELD_U)), float(un.px(_BUTTON_U))
        if self._page == KEY_PAGE:
            words = self._words_height(un, inner.width())
            buttons_h = button_h + gap + field_h + gap
            message_h = float(un.px(_STATUS_U))
            room = max(0.0, inner.bottom() - y - buttons_h - message_h)
            rects.text = QRectF(inner.left(), y, inner.width(), min(words, room))
            y = rects.text.bottom() + gap
            rects.entry = QRectF(inner.left(), y, inner.width(), field_h)
            y += field_h
            rects.message = QRectF(inner.left(), y, inner.width(), message_h)
            y += message_h
            x = inner.right()
            for name in reversed(KEY_BUTTONS):
                width = self._button_width(un, name)
                x -= width
                rects.buttons[name] = QRectF(x, y, width, button_h)
                x -= gap
        else:
            search_w = self._button_width(un, "search")
            paste_w = self._button_width(un, "paste")
            rects.buttons["search"] = QRectF(inner.right() - search_w, y, search_w, button_h)
            paste_x = inner.right() - search_w - gap - paste_w
            rects.buttons["paste"] = QRectF(paste_x, y, paste_w, button_h)
            rects.entry = QRectF(inner.left(), y, max(0.0, paste_x - gap - inner.left()), field_h)
            y += max(field_h, button_h)
            status_h = float(un.px(_STATUS_U))
            rects.status = QRectF(inner.left(), y, inner.width(), status_h)
            y += status_h
            title_h = float(un.px(_LIST_TITLE_U))
            lang_w = math.floor(inner.width() * _LANG_SHARE)
            columns = {
                "languages": (inner.left(), lang_w),
                "files": (inner.left() + lang_w + gap, inner.width() - lang_w - gap),
            }
            for name, (x, width) in columns.items():
                rects.titles[name] = QRectF(x, y, width, title_h)
                rects.lists[name] = QRectF(
                    x, y + title_h, width, max(0.0, inner.bottom() - y - title_h)
                )
        if not rects.entry.isEmpty() and (self._key if self._page == KEY_PAGE else self._query):
            side = rects.entry.height()
            rects.clear = QRectF(rects.entry.right() - side, rects.entry.top(), side, side)
        return rects

    def _row_height(self, name: str) -> float:
        return float(self._units().px(_LANG_ROW_U if name == "languages" else _FILE_ROW_U))

    def _count(self, name: str) -> int:
        return len(self._languages) if name == "languages" else len(self.files_shown())

    def _visible_rows(self, name: str, rects: Optional[_Rects] = None) -> int:
        area = (rects or self._rects()).lists.get(name)
        if area is None or area.isEmpty():
            return 1
        return max(1, math.floor(area.height() / self._row_height(name)))

    def _shown(self, name: str, rects: _Rects) -> range:
        """The rows of a list on show."""
        if name not in rects.lists:
            return range(0)
        top = self._scroll[name]
        return range(top, min(self._count(name), top + self._visible_rows(name, rects)))

    def _row(self, name: str, index: int, rects: _Rects) -> QRectF:
        area, row_h = rects.lists[name], self._row_height(name)
        top = area.top() + (index - self._scroll[name]) * row_h
        return QRectF(area.left(), top, area.width(), row_h)

    def _scroll_to(self, name: str, index: int) -> None:
        shown = self._visible_rows(name)
        top = self._scroll[name]
        if index < top:
            top = index
        elif index >= top + shown:
            top = index - shown + 1
        self._scroll[name] = max(0, min(top, max(0, self._count(name) - shown)))

    def scroll_by(self, name: str, rows: int) -> None:
        shown = self._visible_rows(name)
        top = max(0, min(self._scroll[name] + rows, max(0, self._count(name) - shown)))
        if top != self._scroll[name]:
            self._scroll[name] = top
            self._hover = None
            self.update()

    def row_rect(self, name: str, index: int) -> QRectF:
        """Row `index` of "languages" or "files"; empty while it is scrolled out of sight."""
        rects = self._rects()
        if index not in self._shown(name, rects):
            return QRectF()
        return self._row(name, index, rects)

    def control_rect(self, name: str) -> QRectF:
        """ "close", "field", "clear", "status", or a button's name; empty when not shown."""
        rects = self._rects()
        if name in rects.buttons:
            return QRectF(rects.buttons[name])
        value = {
            "close": rects.close,
            "field": rects.entry,
            "clear": rects.clear,
            "status": rects.status,
        }.get(name)
        return QRectF(value) if value is not None else QRectF()

    # ---- painting -----------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        un = self._units()
        fonts = self._fonts(un)
        rects = self._rects()
        text, muted = qcolor(PALETTE.text), qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), qcolor("#000000", 0.45))  # the backdrop
        radius = un.u * 1.4
        p.setPen(QPen(qcolor(PALETTE.text, 0.14), 1.0))
        p.setBrush(qcolor(PALETTE.panel_raised, 0.97))
        p.drawRoundedRect(rects.panel.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.setFont(fonts["title"])
        p.setPen(text)
        title = self._elided(fonts["title"], translate(TITLE_TEXT), rects.title.width())
        p.drawText(rects.title, left, title)
        self._paint_icon_button(p, un, rects.close, "close", self._hover == ("close", 0))
        if self._page == KEY_PAGE:
            self._paint_key_page(p, un, fonts, rects, text, muted)
        else:
            self._paint_results(p, un, fonts, rects, text, muted)
        p.end()

    @staticmethod
    def _elided(font: QFont, text: str, width: float) -> str:
        return QFontMetricsF(font).elidedText(text, Qt.TextElideMode.ElideRight, max(0.0, width))

    def _paint_icon_button(self, p, un: Units, rect: QRectF, icon: str, hovered: bool) -> None:
        if hovered:
            fill_round(p, rect, qcolor(PALETTE.text, 0.14), rect.height() / 2)
        size = float(un.px(_ICON_U))
        box = QRectF(0, 0, size, size)
        box.moveCenter(rect.center())
        draw_icon(p, icon, box, qcolor(PALETTE.text, 0.85))

    def _paint_focus(self, p, un: Units, rect: QRectF, *, outline: bool, fill: bool = True) -> None:
        if fill:
            fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), un.u * 0.8)
        if outline:
            p.save()
            p.setPen(QPen(qcolor(PALETTE.accent), max(1.0, un.u * 0.12)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, un.u * 0.8, un.u * 0.8)
            p.restore()

    def _paint_field(self, p, un, fonts, rects: _Rects, shown: str, placeholder: str) -> None:
        box = rects.entry
        focused = self._focus == "field"
        fill_round(p, box, qcolor(PALETTE.canvas, 0.6 if focused else 0.4), un.u * 0.9)
        p.save()
        p.setPen(qcolor(PALETTE.accent, 0.8) if focused else qcolor(PALETTE.text, 0.18))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box, un.u * 0.9, un.u * 0.9)
        p.restore()
        x = box.left() + un.px(1.0)
        if self._page == RESULTS_PAGE:
            size = float(un.px(_ICON_U))
            draw_icon(
                p,
                "search",
                QRectF(x, box.center().y() - size / 2, size, size),
                qcolor(PALETTE.text, 0.7),
            )
            x += size + un.px(0.7)
        right = (rects.clear.left() if not rects.clear.isEmpty() else box.right()) - un.px(0.6)
        inner = QRectF(x, box.top(), max(0.0, right - x), box.height())
        p.setFont(fonts["field"])
        if shown:
            p.setPen(qcolor(PALETTE.text))
            label = shown + ("▏" if focused else "")
            # The end of a long text shows (where it is typed).
            fm = QFontMetricsF(fonts["field"])
            while len(label) > 1 and fm.horizontalAdvance(label) > inner.width():
                label = label[1:]
        else:
            p.setPen(qcolor(PALETTE.text, 0.45))
            label = ("▏" if focused else "") + placeholder
            label = self._elided(fonts["field"], label, inner.width())
        p.drawText(inner, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        if not rects.clear.isEmpty():
            self._paint_icon_button(p, un, rects.clear, "close", self._hover == ("clear", 0))

    def _paint_button(self, p, un, fonts, rect: QRectF, name: str, *, accent: bool) -> None:
        hovered = self._hover == (name, 0)
        if accent:
            fill_round(p, rect, qcolor(PALETTE.accent, 1.0 if hovered else 0.9), rect.height() / 2)
        else:
            tone = 0.22 if hovered else 0.14
            fill_round(p, rect, qcolor(PALETTE.text, tone), rect.height() / 2)
        if self._focus == name:
            p.save()
            p.setPen(
                QPen(qcolor(PALETTE.text if accent else PALETTE.accent), max(1.5, un.u * 0.14))
            )
            p.setBrush(Qt.BrushStyle.NoBrush)
            ring = rect.adjusted(-2, -2, 2, 2)
            p.drawRoundedRect(ring, ring.height() / 2, ring.height() / 2)
            p.restore()
        p.setFont(fonts["button"])
        p.setPen(qcolor(PALETTE.canvas) if accent else qcolor(PALETTE.text))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, translate(BUTTONS[name][0]))

    def _paint_key_page(self, p, un, fonts, rects: _Rects, text, muted) -> None:
        p.setFont(fonts["body"])
        p.setPen(qcolor(PALETTE.text, 0.85))
        flags = int(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap
        )
        p.save()
        p.setClipRect(rects.text)
        p.drawText(rects.text, flags, key_words())
        p.restore()
        self._paint_field(p, un, fonts, rects, masked(self._key), translate(KEY_PLACEHOLDER))
        if self._message:
            p.setFont(fonts["status"])
            p.setPen(qcolor(PALETTE.warn))
            p.drawText(
                rects.message,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._elided(fonts["status"], self._message, rects.message.width()),
            )
        for name in KEY_BUTTONS:
            self._paint_button(p, un, fonts, rects.buttons[name], name, accent=name == "save")

    def _paint_results(self, p, un, fonts, rects: _Rects, text, muted) -> None:
        self._paint_field(p, un, fonts, rects, self._query, translate(QUERY_PLACEHOLDER))
        self._paint_button(p, un, fonts, rects.buttons["paste"], "paste", accent=False)
        self._paint_button(p, un, fonts, rects.buttons["search"], "search", accent=True)
        if self._status:
            p.setFont(fonts["status"])
            tone = {"error": qcolor(PALETTE.warn), "busy": text}.get(self._status_kind, muted)
            p.setPen(tone)
            p.drawText(
                rects.status,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._elided(fonts["status"], self._status, rects.status.width()),
            )
        if not self._languages:
            return
        for name, title in (("languages", LANGUAGES_TITLE), ("files", FILES_TITLE)):
            p.setFont(fonts["list_title"])
            p.setPen(muted)
            p.drawText(
                rects.titles[name],
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                i18n.upper(translate(title)),
            )
        self._paint_languages(p, un, fonts, rects, text, muted)
        self._paint_files(p, un, fonts, rects, text, muted)

    def _paint_languages(self, p, un, fonts, rects: _Rects, text, muted) -> None:
        pad = float(un.px(0.8))
        for index in self._shown("languages", rects):
            row, rect = self._languages[index], self._row("languages", index, rects)
            selected = index == self._language
            hovered = self._hover == ("languages", index)
            if selected or hovered:
                self._paint_focus(
                    p, un, rect, outline=selected and self._focus == "languages", fill=True
                )
            count = i18n.number(row.count)
            p.setFont(fonts["row"])
            fm = QFontMetricsF(fonts["row"])
            count_w = fm.horizontalAdvance(count)
            p.setPen(muted)
            right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            p.drawText(rect.adjusted(pad, 0, -pad, 0), right, count)
            p.setPen(text)
            room = rect.width() - 3 * pad - count_w
            p.drawText(
                QRectF(rect.left() + pad, rect.top(), room, rect.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._elided(fonts["row"], row.name, room),
            )
        self._paint_more(p, un, "languages", rects, muted)

    def _paint_files(self, p, un, fonts, rects: _Rects, text, muted) -> None:
        pad = float(un.px(0.8))
        files = self.files_shown()
        for index in self._shown("files", rects):
            file, rect = files[index], self._row("files", index, rects)
            lit = index == self._file and self._focus == "files"
            hovered = self._hover == ("files", index)
            if lit or hovered:
                self._paint_focus(p, un, rect, outline=lit)
            half = rect.height() / 2
            top = QRectF(
                rect.left() + pad,
                rect.top() + un.px(0.3),
                rect.width() - 2 * pad,
                half - un.px(0.2),
            )
            bottom = QRectF(top.left(), top.bottom(), top.width(), half - un.px(0.4))
            p.setFont(fonts["row"])
            p.setPen(text)
            p.drawText(
                top,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self._elided(fonts["row"], shown_release(file.release), top.width()),
            )
            p.setFont(fonts["meta"])
            x = bottom.left()
            fm = QFontMetricsF(fonts["meta"])
            for piece, color in self._meta(file, muted):
                if x >= bottom.right():
                    break
                shown = self._elided(fonts["meta"], piece, bottom.right() - x)
                p.setPen(color)
                p.drawText(
                    QRectF(x, bottom.top(), bottom.right() - x, bottom.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    shown,
                )
                x += fm.horizontalAdvance(shown)
        self._paint_more(p, un, "files", rects, muted)

    @staticmethod
    def _meta(file: SubtitleFile, muted) -> list[tuple[str, object]]:
        """A subtitle's second line: its downloads, then its marks (each in its colour)."""
        count = i18n.number(file.downloads)
        downloads = tr_n("{n} download", "{n} downloads", file.downloads, n=count)
        pieces: list[tuple[str, object]] = [(downloads, muted)]
        if file.hearing_impaired:
            pieces += [("  ·  ", muted), (translate(HI_TEXT), qcolor(PALETTE.accent))]
        if file.machine_translated:
            pieces += [("  ·  ", muted), (translate(MACHINE_TEXT), qcolor(PALETTE.warn))]
        if file.trusted:
            pieces += [("  ·  ", muted), (translate(TRUSTED_TEXT), qcolor(PALETTE.ok))]
        return pieces

    def _paint_more(self, p, un, name: str, rects: _Rects, color) -> None:
        """A small ▾ at the end of a list that has more rows below."""
        shown = self._shown(name, rects)
        if not shown or shown.stop >= self._count(name):
            return
        last = self._row(name, shown.stop - 1, rects)
        size = float(un.px(0.9))
        cx, cy = last.right() - un.px(0.8) - size / 2, last.bottom() - size
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - size / 2, cy - size / 3),
                    QPointF(cx + size / 2, cy - size / 3),
                    QPointF(cx, cy + size / 3),
                ]
            )
        )
        p.restore()

    # ---- the mouse ----------------------------------------------------------------------------

    def _hit(self, pos: QPointF) -> Optional[tuple[str, int]]:
        """What is at `pos`: ("close"|"clear"|"field"|a button, 0), ("languages"|"files", row),
        ("panel", 0) elsewhere on the panel, or None on the backdrop."""
        rects = self._rects()
        for name, rect in (("close", rects.close), ("clear", rects.clear), ("field", rects.entry)):
            if not rect.isEmpty() and rect.contains(pos):
                return (name, 0)
        for name, rect in rects.buttons.items():
            if rect.contains(pos):
                return (name, 0)
        if self._page == RESULTS_PAGE:
            for name in ("languages", "files"):
                for index in self._shown(name, rects):
                    if self._row(name, index, rects).contains(pos):
                        return (name, index)
        return ("panel", 0) if rects.panel.contains(pos) else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never the video's under it
        if event.button() == Qt.MouseButton.BackButton:
            self.dismiss()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(event.position())
        if hit is None:
            self.dismiss()  # the backdrop
            return
        name, index = hit
        if name == "close":
            self.dismiss()
        elif name == "clear":
            self.clear_field()
        elif name == "field":
            self._focus = "field"
            self.update()
        elif name in BUTTONS:
            self._focus = name if self._page == KEY_PAGE or name == "paste" else self._focus
            self._press(name)
            self.update()
        elif name == "languages":
            self._focus = "languages"
            self._set_language(index)
            self.update()
        elif name == "files":
            self._focus = "files"
            self._choose(index)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._hit(event.position())
            if hit is not None and hit[0] == "files":
                event.accept()  # the first click downloads it already
                return
        self.mousePressEvent(event)  # never fullscreen

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        hit = self._hit(event.position())
        hover = hit if hit is not None and hit[0] not in ("panel", "field") else None
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
        """A list scrolls under the pointer; never the host's channel change."""
        event.accept()
        hit = self._hit(event.position())
        name = hit[0] if hit is not None and hit[0] in ("languages", "files") else None
        if name is None:
            rects = self._rects()
            name = next((n for n, r in rects.lists.items() if r.contains(event.position())), None)
        if name is None:
            return
        self._wheel += event.angleDelta().y()
        rows = int(self._wheel / _WHEEL_STEP)
        if rows:
            self._wheel -= rows * _WHEEL_STEP
            self.scroll_by(name, -rows)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            tip = self.tooltip_at(QPointF(event.pos()))
            if tip:
                QToolTip.showText(event.globalPos(), tip, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def tooltip_at(self, pos: QPointF) -> str:
        hit = self._hit(pos)
        if hit is None:
            return ""
        name = hit[0]
        if name == "close":
            return key_hint(translate(CLOSE_TIP[0]), CLOSE_TIP[1])
        if name == "clear":
            return translate(CLEAR_TIP[0])
        if name in BUTTONS:
            _label, what, keys = BUTTONS[name]
            return key_hint(translate(what), keys) if keys else translate(what)
        if name == "files":
            files = self.files_shown()
            return shown_release(files[hit[1]].release) if hit[1] < len(files) else ""
        return ""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            obj is self.parentWidget()
            and event.type() == QEvent.Type.Resize
            and not self.isHidden()
        ):
            self._fit()
            for name in self._scroll:
                self._scroll_to(name, self._language if name == "languages" else self._file)
        return False
