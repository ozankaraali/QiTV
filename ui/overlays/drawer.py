"""The two-layer drawer (spec §5.2): the webOS-like layered list over the video.

A right-click on an item, episode or channel row asks for its context menu
(`contextRequested`); it leaves the highlight where the keys put it.

Some rows leave the drawer when chosen (Enter, →, a click), and only then: the sources
column's Settings and Add provider rows (`actionRequested`), and the Movies and Series
sections, which the Library shows (`libraryRequested`). Highlighting them previews nothing.

The left column's top row starts with ‹ (a layer up, as ← on the left column; hidden on the
sources), then names where the left column is, each a link: a name shows its node's children
in the left column, and the provider's name shows the sources, the provider highlighted, to
pick another. A head cut short ("…") goes a layer up. ⌕ (Search, as Ctrl/⌘+F), ↻ (refresh) and
⚙ (Settings) end it; each takes clicks over at least MIN_HIT_PX and names its key when hovered.
"""

from dataclasses import dataclass
import logging

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QToolTip

from services.navigation import (
    ADD_PROVIDER_ROW,
    FAVORITES_GROUP,
    LIBRARY_SECTIONS,
    OPEN_FILE_ROW,
    RECENT_GROUP,
    SETTINGS_ROW,
    NavModel,
    NavNode,
)
from services.text_fold import fold
from services.zapping import ZapContext
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.channel_rows import (
    ChannelListView,
    ChannelRowDelegate,
    ChannelRowsModel,
    column_focused,
)
from ui.overlays.painting import Units, draw_icon, elided, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

_CONTEXT_LAYERS = ("item", "episode")  # rows with a context menu (channels are items)
_RIGHT_PRESSES = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
LEFT_FRACTION = 0.25
RIGHT_FRACTION = 0.58
FADE_U = 7.0
PAD_TOP_U = 2.4
PAD_SIDE_U = 1.6
CRUMB_H_U = 2.2
FILTER_H_U = 3.4
GEAR_U = 1.9  # the header row's buttons (Search, refresh, Settings) at its right end, this wide
GEAR_GAP_U = 0.7
MIN_HIT_PX = 28  # the header's buttons take clicks over this square at least
REFRESH_ACTION = "refresh"  # the header's refresh button: the provider's channels and guide
SEARCH_ACTION = "search"  # the header's ⌕: the Library's search
BACK_BUTTON = "back"  # the header's ‹: a layer up
_CRUMB = "crumb:"  # a name in the header, by its path index ("crumb:up": the elided head)
SEPARATOR = " › "
BACK_TOOLTIP = "Back"
PROVIDER_TOOLTIP = "Choose another provider"
CRUMB_FONT_U = 1.15
PREVIEW_DEBOUNCE_MS = 200
LOADING_TEXT = "Loading…"
EMPTY_TEXT = "Nothing here"
ERROR_TEXT = "Couldn't load — press Enter to retry"
PROVIDER_ERROR_TEXT = "Couldn't connect — press Enter to retry"

NodeRole = Qt.UserRole + 10
CountRole = Qt.UserRole + 11
IconRole = Qt.UserRole + 12
_PINNED_ICONS = {FAVORITES_GROUP: "star", RECENT_GROUP: "recent"}
_ACTION_ICONS = {SETTINGS_ROW: "gear", ADD_PROVIDER_ROW: "plus", OPEN_FILE_ROW: "file"}
_HEADER_TOOLTIPS = {  # the header's buttons: what they do, and their key
    SEARCH_ACTION: key_hint("Search", "Ctrl+F"),
    REFRESH_ACTION: key_hint("Refresh channels and guide", "Ctrl+R"),
    SETTINGS_ROW: key_hint("Settings", "Ctrl+,"),
}


def _draw_refresh(p: QPainter, rect: QRectF, color: QColor) -> None:
    """A circular arrow, drawn like painting.draw_icon's glyphs (a unit box, round caps)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(rect.x(), rect.y())
    p.scale(rect.width(), rect.height())
    pen = QPen(color, 0.1)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(0.16, 0.16, 0.68, 0.68), 70 * 16, 290 * 16)  # open at the top right
    head = QPainterPath()
    head.moveTo(0.52, 0.03)
    head.lineTo(0.74, 0.17)
    head.lineTo(0.5, 0.3)
    head.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawPath(head)
    p.restore()


def _draw_back(p: QPainter, rect: QRectF, color: QColor) -> None:
    """‹: a chevron, drawn like the refresh arrow (a unit box, round caps)."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing, True)
    p.translate(rect.x(), rect.y())
    p.scale(rect.width(), rect.height())
    pen = QPen(color, 0.12)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    chevron = QPainterPath(QPointF(0.62, 0.2))
    chevron.lineTo(0.34, 0.5)
    chevron.lineTo(0.62, 0.8)
    p.drawPath(chevron)
    p.restore()


@dataclass(frozen=True)
class _Header:
    """The left column's top row (drawer coordinates)."""

    back: QRectF  # empty on the top layer (the sources)
    names: list[tuple[int | None, str, QRectF]]  # (path index or None: the elided head, …)
    text: QRectF  # all the names
    search: QRectF
    refresh: QRectF
    gear: QRectF


def _crumb_key(index: int | None) -> str:
    return f"{_CRUMB}{'up' if index is None else index}"


def _leaves_drawer(node: NavNode) -> bool:
    """An action row, or a section the Library shows: chosen, it is handed to the shell."""
    return node.layer == "action" or (node.layer == "section" and node.key in LIBRARY_SECTIONS)


def _is_live_item(nav: NavModel, node: NavNode) -> bool:
    return node.layer == "item" and nav.section_of(node) == "live"


def _numbers(group: list[NavNode], shown: list[NavNode]) -> list[str | None]:
    """The numbers of the `shown` rows of a live group, the filter aside.

    When no channel of the group has a number, its position in the group stands in, as on
    the banner and for number entry.
    """
    channels = [n.payload for n in group if isinstance(n.payload, dict)]
    if any(ZapContext.number_of(c) for c in channels):
        return [
            (ZapContext.number_of(n.payload) or None) if isinstance(n.payload, dict) else None
            for n in shown
        ]
    position = {node: str(row + 1) for row, node in enumerate(group)}
    return [position.get(node) for node in shown]


def _shown_rows(nodes: list[NavNode], live: bool, numbers: list[str | None] | None) -> tuple:
    """What a column's list shows for `nodes`: equal values show the same rows.

    A row shows its node's title, count and icon, and a live channel its item (by identity:
    a refreshed catalog brings new item dicts) and number.
    """
    rows = tuple((n.layer, n.key, n.title, n.count, id(n.payload)) for n in nodes)
    return (live, rows, tuple(numbers) if numbers is not None else None)


class _NodeModel(ChannelRowsModel):
    """Channel rows for live items; titles, counts and icons for everything else."""

    def __init__(self, logos, now_provider, parent=None) -> None:
        super().__init__(logos, now_provider, parent)
        self._nodes: list[NavNode] = []

    def set_nodes(
        self, nodes: list[NavNode], live: bool, numbers: list[str | None] | None = None
    ) -> None:
        self._nodes = list(nodes)
        rows = [
            n.payload if live and isinstance(n.payload, dict) else {"name": n.title} for n in nodes
        ]
        self.set_channels(rows, numbers if live else [None] * len(rows))

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if index.isValid() and 0 <= row < len(self._nodes):
            node = self._nodes[row]
            if role == Qt.DisplayRole:
                return node.title
            if role == NodeRole:
                return node
            if role == CountRole:
                return node.count
            if role == IconRole:
                icons = {"group": _PINNED_ICONS, "action": _ACTION_ICONS}.get(node.layer, {})
                return icons.get(node.key)
        return super().data(index, role)


class _SimpleRowDelegate(QStyledItemDelegate):
    def _units(self, widget) -> Units:
        return Units(getattr(widget, "units_width", 100) or 100)

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, self._units(option.widget).px(3.6))

    def paint(self, p: QPainter, option, index) -> None:
        units = self._units(option.widget)
        u = units.u
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        focused = column_focused(option.widget)
        p.save()
        if selected:
            box = rect.adjusted(0.3 * u, 0.2 * u, -0.3 * u, -0.2 * u)
            fill_round(
                p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA if focused else 0.1), 1.0 * u
            )
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(qcolor(PALETTE.accent, 0.7 if focused else 0.35))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 1.0 * u, 1.0 * u)
        x = rect.x() + 0.9 * u + 0.3 * u
        right = rect.right() - 0.9 * u - 0.3 * u
        title = str(index.data(Qt.DisplayRole) or "")
        icon = index.data(IconRole)
        if icon:
            size = 1.8 * u
            draw_icon(
                p,
                icon,
                QRectF(x, rect.y() + (rect.height() - size) / 2, size, size),
                qcolor(PALETTE.accent),
            )
            x += size + 0.7 * u
            title = title.lstrip("★⟲ ")
        count = index.data(CountRole)
        if count is not None:
            p.setFont(units.font(1.1))
            p.setPen(qcolor(PALETTE.text, 0.45))
            text = str(count)
            width = p.fontMetrics().horizontalAdvance(text)
            p.drawText(
                QRectF(right - width, rect.y(), width, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                text,
            )
            right -= width + 0.8 * u
        p.setFont(units.font(1.35))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(
            QRectF(x, rect.y(), max(0.0, right - x), rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, title, right - x),
        )
        p.restore()


class _Column:
    def __init__(self, side: str, drawer: "Drawer", logos, now_provider) -> None:
        self.side = side
        self.model = _NodeModel(logos, now_provider, drawer)
        self.view = ChannelListView(drawer)
        self.view.setModel(self.model)
        self.channel_delegate = ChannelRowDelegate(0.9, self.view)
        self.simple_delegate = _SimpleRowDelegate(self.view)
        self.view.setItemDelegate(self.simple_delegate)
        self.view.rowClicked.connect(lambda row: drawer._on_row_clicked(side, row))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(lambda pos: drawer._on_row_context(side, pos))
        self.view.viewport().installEventFilter(drawer)  # a right press must not select
        self.parent: NavNode | None = None
        self.nodes: list[NavNode] = []
        self.rows: dict[NavNode, int] = {}
        self.current: NavNode | None = None
        self.message: str | None = None
        self.rect = QRect()
        self.shown: tuple = ()  # what the list shows (_shown_rows), to skip refilling it


class Drawer(Overlay):
    playRequested = Signal(object, object, object)
    contextRequested = Signal(object, QPoint)  # node, global position
    # An action row (SETTINGS_ROW, ADD_PROVIDER_ROW, OPEN_FILE_ROW) or a header button
    # (SETTINGS_ROW, REFRESH_ACTION, SEARCH_ACTION).
    actionRequested = Signal(str)
    libraryRequested = Signal(str)  # "movies" or "series", for the Library
    closed = Signal()

    def __init__(self, nav: NavModel, logos, now_provider, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._nav = nav
        self._units = Units(100)
        self._columns = {
            "left": _Column("left", self, logos, now_provider),
            "right": _Column("right", self, logos, now_provider),
        }
        self.focus_side = "right"
        self.filter_text = ""
        self._filter_key: tuple | None = None
        self._pending_key: str | None = None
        self._left_choice: NavNode | None = None  # the left highlight, which may lead the path
        self._preview_node: NavNode | None = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._flush_preview)
        self._laid_out_for: tuple[int, int] | None = None  # the host size _relayout last fitted
        self._hover: str | None = None  # the header button under the pointer
        # Hover over the header buttons (a move elsewhere goes on to the host, as without it).
        self.setMouseTracking(True)
        nav.changed.connect(self._on_nav_changed)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_drawer(self, focus_key: str | None = None, focus_side: str = "right") -> None:
        """Open on the NavModel's path, focusing the right column (or the left one)."""
        self.focus_side = focus_side
        self.filter_text = ""
        self._filter_key = None
        self._left_choice = None  # the left highlight starts at the end of the path
        self._pending_key = focus_key
        self._cancel_preview()
        self._columns["right"].parent = None  # force the highlight to be re-chosen
        self._relayout()
        self._refresh()
        self.show()
        self.raise_()

    def close_drawer(self) -> None:
        self._flush_preview()
        was_visible = self.isVisible()
        self.hide()
        if was_visible:
            self.closed.emit()

    def highlighted(self) -> NavNode | None:
        col = self._columns[self.focus_side]
        return col.current if col.current in col.rows else None

    def message_for(self, side: str) -> str | None:
        return self._columns[side].message

    def move_by(self, delta: int) -> None:
        col = self._columns[self.focus_side]
        if not col.nodes:
            return
        self._pending_key = None
        row = col.rows.get(col.current, -1 if delta > 0 else len(col.nodes))  # type: ignore[arg-type]
        self._select(col, min(len(col.nodes) - 1, max(0, row + delta)))

    def page(self, delta: int) -> None:
        col = self._columns[self.focus_side]
        row_h = col.view.sizeHintForRow(0)
        rows = max(1, col.view.height() // row_h - 1) if row_h > 0 else 8
        self.move_by(delta * rows)

    def left(self) -> None:
        if self.focus_side == "right":
            self.focus_side = "left"
            self._left_choice = None
            self._refresh()
        else:
            self._cancel_preview()
            self._left_choice = None
            self._nav.up()

    def right(self) -> None:
        if self.focus_side == "left":
            if self._hand_off(self.highlighted()):
                return
            self._commit_left()
            self.focus_side = "right"
            self._refresh()
            return
        col = self._columns["right"]
        if self._retry(col):
            return
        node = self.highlighted()
        if node is None or self._hand_off(node):
            return
        if self._nav.is_playable(node):
            self.activate()
        else:
            self._nav.open(node)

    def activate(self) -> None:
        col = self._columns[self.focus_side]
        if self._retry(col):
            return
        node = self.highlighted()
        if node is None:
            return
        if self.focus_side == "right" and self._nav.is_playable(node):
            self.playRequested.emit(node, list(col.nodes), self._nav.path[-1])
        else:
            self.right()

    def type_text(self, text: str) -> None:
        self.filter_text += text
        self._refresh()

    def backspace(self) -> None:
        if self.filter_text:
            self.filter_text = self.filter_text[:-1]
            self._refresh()

    def go_up(self) -> None:
        """‹ (or the elided head): a layer up, as ← does with the focus on the left column."""
        self.focus_side = "left"
        self._cancel_preview()
        self._left_choice = None
        if not self._nav.up():
            self._refresh()

    def show_crumb(self, index: int) -> None:
        """A name in the header: the left column lists the children of the path's `index`-th
        node, the next one on the path highlighted. The provider's name (index 0) shows the
        sources instead, with that provider highlighted."""
        path = self._nav.path
        if not 0 <= index < len(path):
            return
        self.focus_side = "left"
        self._cancel_preview()
        self._left_choice = None
        if index == 0 and path[0].layer == "source":
            self._nav.focus_source(path[0].key)
            return
        while len(self._nav.path) > index + 2 and self._nav.up():
            pass
        self._refresh()

    def escape(self) -> bool:
        if self.filter_text:
            self.filter_text = ""
            self._refresh()
            return True
        self.close_drawer()
        return True

    # ---- state ------------------------------------------------------------------------------

    def _hand_off(self, node: NavNode | None) -> bool:
        """Hand a row that leaves the drawer to the shell; False for every other row."""
        if node is None or not _leaves_drawer(node):
            return False
        if node.layer == "action":
            self.actionRequested.emit(node.key)
        else:
            self.libraryRequested.emit(node.key)
        return True

    def _retry(self, col: _Column) -> bool:
        if col.parent is None:
            return False
        if col.message == PROVIDER_ERROR_TEXT:
            self._nav.open(col.parent)  # sets the failed provider up again
            return True
        if col.message != ERROR_TEXT:
            return False
        self._nav.retry(col.parent)
        return True

    def _select(self, col: _Column, row: int) -> None:
        node = col.nodes[row]
        col.current = node
        if col.side == "left":
            self._left_choice = node
            self._schedule_preview(node)
        self._show_current(col)

    def _schedule_preview(self, node: NavNode) -> None:
        path = self._nav.path
        if node.layer == "source" or _leaves_drawer(node) or (path and node == path[-1]):
            self._cancel_preview()  # these only highlight; Right/Enter opens or chooses them
            return
        self._preview_node = node
        self._preview_timer.start()

    def _cancel_preview(self) -> None:
        self._preview_timer.stop()
        self._preview_node = None

    def _flush_preview(self) -> None:
        node = self._preview_node
        self._cancel_preview()
        if node is not None and self.isVisible() and node in self._columns["left"].rows:
            self._left_choice = None
            self._nav.open(node)

    def _commit_left(self) -> None:
        """Open the left highlight (flushing a pending preview) before focus moves right.

        The end of the path opens again only when it is a source whose provider needs a
        setup (its last one failed): that is the retry.
        """
        self._flush_preview()
        node = self._left_choice
        self._left_choice = None
        path = self._nav.path
        if node is None:
            node = self._columns["left"].current
            if node is None or not self._nav.needs_setup(node):
                return
        if _leaves_drawer(node):
            return  # never opened here: right() hands it off when it is the highlight
        if not (path and node == path[-1]) or self._nav.needs_setup(node):
            self._nav.open(node)

    def _show_current(self, col: _Column) -> None:
        row = col.rows.get(col.current) if col.current is not None else None
        if row is not None:
            idx = col.model.index(row)
            col.view.setCurrentIndex(idx)
            col.view.scrollTo(idx, QAbstractItemView.EnsureVisible)
        else:
            col.view.clearSelection()
            col.view.setCurrentIndex(col.model.index(-1))

    def _on_row_clicked(self, side: str, row: int) -> None:
        col = self._columns[side]
        if not 0 <= row < len(col.nodes):
            return
        if side != self.focus_side:
            self.focus_side = side
            self._refresh()
        self._select(col, row)
        self.activate()

    def _on_row_context(self, side: str, pos: QPoint) -> None:
        """A right-click at `pos` (viewport coordinates) of a column's list."""
        col = self._columns[side]
        row = col.view.indexAt(pos).row()
        if 0 <= row < len(col.nodes) and col.nodes[row].layer in _CONTEXT_LAYERS:
            self.contextRequested.emit(col.nodes[row], col.view.viewport().mapToGlobal(pos))

    def _on_nav_changed(self) -> None:
        if self.isVisible():
            self._refresh()

    def _refresh(self) -> None:
        (left_parent, left_children), (right_parent, right_children) = self._nav.columns()
        focused_parent = left_parent if self.focus_side == "left" else right_parent
        key = (self.focus_side, focused_parent)
        if key != self._filter_key:
            self._filter_key = key
            self.filter_text = ""
        needle = fold(self.filter_text)
        self._fill(self._columns["left"], left_parent, left_children, needle)
        self._fill(self._columns["right"], right_parent, right_children, needle)
        for side, col in self._columns.items():
            col.view.setProperty("columnFocused", side == self.focus_side)
            col.view.viewport().update()
        self.update()

    def _fill(
        self,
        col: _Column,
        parent: NavNode | None,
        children: list[NavNode] | None,
        needle: str,
    ) -> None:
        parent_changed = parent != col.parent
        col.parent = parent
        loading = parent is not None and self._nav.is_loading(parent)
        if parent is not None and not loading and self._nav.provider_error(parent):
            col.message = PROVIDER_ERROR_TEXT
            children = None  # the failed provider's sections are not shown
        elif loading and (not children or parent is not None and parent.layer == "source"):
            # Still on its way (or a provider setup runs: a source's sections are not
            # known yet). Never the retry row: retrying now would fail the same way.
            col.message = LOADING_TEXT
            children = None
        elif not children and parent is not None and self._nav.load_error(parent):
            col.message = ERROR_TEXT
        elif children is None:
            col.message = LOADING_TEXT
        else:
            col.message = None
        nodes = children or []
        if col.side == self.focus_side and needle:
            nodes = [n for n in nodes if needle in fold(n.title)]
        if col.message is None and not nodes:
            col.message = EMPTY_TEXT
        live = bool(nodes) and _is_live_item(self._nav, nodes[0])
        numbers = _numbers(children or [], nodes) if live else None
        # The same rows again (e.g. the section reported ready once more): the list keeps its
        # model, so it is neither laid out nor repainted from scratch.
        shown = _shown_rows(nodes, live, numbers)
        if shown != col.shown:
            col.shown = shown
            col.nodes = nodes
            col.rows = {n: i for i, n in enumerate(nodes)}
            col.view.setItemDelegate(col.channel_delegate if live else col.simple_delegate)
            col.model.set_nodes(nodes, live, numbers)
        else:
            col.model.refresh_now()  # a refresh still looks "now" up again (the EPG loaded)
        if col.side == "left":
            if parent_changed:
                self._cancel_preview()  # a preview of the old layer must not open
            self._choose_left(col, nodes)
        else:
            matched = False
            if self._pending_key is not None:
                match = next((n for n in nodes if n.key == self._pending_key), None)
                if match is not None:
                    col.current = match
                    matched = True
            if not matched and (parent_changed or col.current not in col.rows):
                col.current = nodes[0] if nodes else None
            if children is not None:
                self._pending_key = None  # a stale focus key must not outlive the first fill
        self._show_current(col)

    def _choose_left(self, col: _Column, nodes: list[NavNode]) -> None:
        path = self._nav.path
        end = path[-1] if path else None
        if self._left_choice is not None and self._left_choice not in col.rows:
            # The filter hid it: Enter must choose what is highlighted now, not the hidden row.
            self._left_choice = None
            self._cancel_preview()
        if self._left_choice in col.rows:
            col.current = self._left_choice
        elif end in col.rows:
            col.current = end
        elif col.side == self.focus_side and self.filter_text and nodes:
            col.current = nodes[0]  # auto-highlight the first match and preview it
            self._left_choice = nodes[0]
            self._schedule_preview(nodes[0])
        else:
            col.current = None

    # ---- mouse: consumed here, so it never reaches the video behind -------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host: the shell's layer up/down
            return
        event.accept()
        button = self._header_button_at(event.position())
        if event.button() == Qt.LeftButton and button is not None:
            if button in (SETTINGS_ROW, REFRESH_ACTION, SEARCH_ACTION):
                self.actionRequested.emit(button)
            elif button in (BACK_BUTTON, _crumb_key(None)):
                self.go_up()
            else:
                self.show_crumb(int(button.removeprefix(_CRUMB)))
            self._hover_at(event.position())  # the row changed under the pointer
            return
        in_fade = event.position().x() > self._columns["right"].rect.right()
        if event.button() == Qt.LeftButton and in_fade:
            self.close_drawer()  # the fade shows the video: a click there is a click on it

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()
            return
        event.accept()  # never the video's fullscreen toggle

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._hover_at(event.position())
        event.ignore()  # the host still hears the pointer move (the transport, the cursor)

    def _hover_at(self, point: QPointF) -> None:
        over = self._header_button_at(point)
        if over != self._hover:
            self._hover = over
            self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, event: QEvent) -> None:
        if self._hover is not None:
            self._hover = None
            self.unsetCursor()
            self.update()
        super().leaveEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.ToolTip:
            text = self.tooltip_at(QPointF(event.pos()))
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def tooltip_at(self, point: QPointF) -> str:
        """What hovering `point` says: a header button's name and key, ‹, or the provider."""
        button = self._header_button_at(point)
        if button in _HEADER_TOOLTIPS:
            return _HEADER_TOOLTIPS[button]
        if button == BACK_BUTTON:
            return BACK_TOOLTIP
        if button == _crumb_key(0):
            return PROVIDER_TOOLTIP
        return ""

    def header_hit_rect(self, rect: QRectF) -> QRectF:
        """Where a header button drawn at `rect` takes clicks: a MIN_HIT_PX square (at the
        default window), but never into its neighbours' half of the gap between them."""
        if rect.isEmpty():
            return rect
        grow = (MIN_HIT_PX - min(rect.width(), rect.height())) / 2
        grow = max(0.0, min(grow, self._units.px(GEAR_GAP_U) / 2))
        return rect.adjusted(-grow, -grow, grow, grow)

    def _header_button_at(self, point: QPointF) -> str | None:
        """What of the header row is at `point`: a button, a name (_crumb_key), or None."""
        header = self._header()
        hit = self.header_hit_rect
        if hit(header.gear).contains(point):
            return SETTINGS_ROW
        if hit(header.refresh).contains(point):
            return REFRESH_ACTION
        if hit(header.search).contains(point):
            return SEARCH_ACTION
        if hit(header.back).contains(point):
            return BACK_BUTTON
        for index, _text, rect in header.names:
            if rect.contains(point):
                return _crumb_key(index)
        return None

    def wheelEvent(self, event: QWheelEvent) -> None:
        event.accept()  # a list at its end, the header or the fade: never a zap behind

    # ---- layout and painting ----------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        elif (
            event.type() in _RIGHT_PRESSES
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.RightButton
            and any(obj is col.view.viewport() for col in self._columns.values())
        ):
            return True  # not a selection: the context menu follows, the highlight stays
        return False

    def _relayout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        host_w, host_h = parent.width(), parent.height()
        if (host_w, host_h) == self._laid_out_for:
            return  # opened again at the same size: forcing a layout of the lists would be waste
        self._laid_out_for = (host_w, host_h)
        units = Units(host_w)
        self._units = units
        left_w = round(host_w * LEFT_FRACTION)
        right_w = round(host_w * RIGHT_FRACTION) - left_w
        self.setGeometry(QRect(0, 0, left_w + right_w + units.px(FADE_U), host_h))
        side = units.px(PAD_SIDE_U)
        for col, x, width, top_u in (
            (self._columns["left"], 0, left_w, PAD_TOP_U + CRUMB_H_U + FILTER_H_U + 1.0),
            (self._columns["right"], left_w, right_w, PAD_TOP_U + FILTER_H_U + 1.0),
        ):
            top = units.px(top_u)
            col.rect = QRect(x, 0, width, host_h)
            col.view.units_width = host_w
            col.view.setGeometry(x + side, top, width - 2 * side, max(0, host_h - top))
            col.view.doItemsLayout()
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        units = self._units
        left, right = self._columns["left"], self._columns["right"]
        p.fillRect(left.rect, qcolor(PALETTE.panel_raised, 0.95))
        p.fillRect(right.rect, qcolor(PALETTE.panel))
        fade = QRect(
            right.rect.right() + 1, 0, self.width() - right.rect.right() - 1, self.height()
        )
        grad = QLinearGradient(fade.left(), 0, fade.right(), 0)
        grad.setColorAt(0.0, qcolor(PALETTE.panel))
        grad.setColorAt(1.0, qcolor(PALETTE.panel, 0.0))
        p.fillRect(fade, grad)
        self._paint_breadcrumb(p, left)
        for col in (left, right):
            self._paint_column(p, col, units)

    def _header(self) -> _Header:
        """The left column's top row: ‹ at its left (but on the sources), then the names of
        the left column's path, then Search, the refresh button and the Settings gear at its
        right end. The names elide before they reach the buttons: the last one at its end, the
        others ("the head") from their start, into one name that goes a layer up."""
        units = self._units
        col = self._columns["left"]
        side = units.px(PAD_SIDE_U)
        row = QRectF(
            col.rect.x() + side,
            units.px(PAD_TOP_U),
            col.rect.width() - 2 * side,
            units.px(CRUMB_H_U),
        )
        size, gap = units.px(GEAR_U), units.px(GEAR_GAP_U)
        top = row.center().y() - size / 2
        gear = QRectF(row.right() - size, top, size, size)
        refresh = QRectF(gear.left() - gap - size, top, size, size)
        search = QRectF(refresh.left() - gap - size, top, size, size)
        path = self._nav.path
        back = QRectF(row.x(), top, size, size) if len(path) > 1 else QRectF()
        x = back.right() + gap if not back.isEmpty() else row.x()
        room = max(0.0, search.left() - gap - x)  # never under the buttons
        fm = QFontMetrics(units.font(CRUMB_FONT_U))
        titles = [n.title for n in path[:-1]]
        names: list[tuple[int | None, str, QRectF]] = []
        start = x
        if titles:
            last = fm.elidedText(titles[-1], Qt.ElideRight, int(room))
            last_w = fm.horizontalAdvance(last)
            sep_w = fm.horizontalAdvance(SEPARATOR)
            head = titles[:-1]
            if sum(fm.horizontalAdvance(t) + sep_w for t in head) <= room - last_w:
                for index, title in enumerate(head):
                    width = fm.horizontalAdvance(title)
                    names.append((index, title, QRectF(x, row.y(), width, row.height())))
                    x += width + sep_w
            elif head:
                cut = fm.elidedText(
                    SEPARATOR.join(head) + SEPARATOR, Qt.ElideLeft, max(0, int(room - last_w))
                )
                width = fm.horizontalAdvance(cut)
                if cut:
                    names.append((None, cut, QRectF(x, row.y(), width, row.height())))
                x += width
            names.append((len(titles) - 1, last, QRectF(x, row.y(), last_w, row.height())))
            x += last_w
        text = QRectF(start, row.y(), x - start, row.height())
        return _Header(back, names, text, search, refresh, gear)

    def breadcrumb_text_rect(self) -> QRectF:
        """Where the header's names are (drawer coordinates)."""
        return self._header().text

    def breadcrumb_parts(self) -> list[tuple[int | None, str, QRectF]]:
        """The header's names as shown: (the path index each names, or None for an elided
        head that goes a layer up; its text; where it takes clicks)."""
        return self._header().names

    def back_rect(self) -> QRectF:
        """‹ at the left end of the header row; empty on the sources (the top layer)."""
        return self._header().back

    def search_rect(self) -> QRectF:
        """⌕ (the Library's search), left of the refresh button (drawer coordinates)."""
        return self._header().search

    def refresh_rect(self) -> QRectF:
        """The refresh button (the provider's channels and guide), left of the gear."""
        return self._header().refresh

    def settings_gear_rect(self) -> QRectF:
        """The Settings gear at the right end of the header row (drawer coordinates)."""
        return self._header().gear

    def _paint_breadcrumb(self, p: QPainter, col: _Column) -> None:
        header = self._header()
        align = Qt.AlignLeft | Qt.AlignVCenter
        last = header.names[-1] if header.names else None
        for name in header.names:
            index, text, rect = name
            hovered = self._hover == _crumb_key(index)
            font = self._units.font(CRUMB_FONT_U)
            font.setUnderline(hovered)  # a link
            p.setFont(font)
            if name is last:
                p.setPen(qcolor(PALETTE.accent))
            else:
                p.setPen(qcolor(PALETTE.text, 1.0 if hovered else 0.6))
            p.drawText(rect, align, text)
            if index is not None and name is not last:  # " › " (an elided head ends with it)
                p.setFont(self._units.font(CRUMB_FONT_U))
                p.setPen(qcolor(PALETTE.text, 0.6))
                after = header.text.right() - rect.right()
                p.drawText(QRectF(rect.right(), rect.y(), after, rect.height()), align, SEPARATOR)
        buttons = (
            (BACK_BUTTON, header.back),
            (SEARCH_ACTION, header.search),
            (REFRESH_ACTION, header.refresh),
            (SETTINGS_ROW, header.gear),
        )
        for key, rect in buttons:
            if rect.isEmpty():
                continue
            tone = qcolor(PALETTE.accent) if self._hover == key else qcolor(PALETTE.text, 0.55)
            if key == SETTINGS_ROW:
                draw_icon(p, "gear", rect, tone)
            elif key == SEARCH_ACTION:
                draw_icon(p, "search", rect, tone)
            elif key == BACK_BUTTON:
                _draw_back(p, rect, tone)
            else:
                _draw_refresh(p, rect, tone)

    def _paint_column(self, p: QPainter, col: _Column, units: Units) -> None:
        u = units.u
        side = units.px(PAD_SIDE_U)
        if col.side == self.focus_side and self.filter_text:
            top = PAD_TOP_U + (CRUMB_H_U + 0.2 if col.side == "left" else 0.0)
            box = QRectF(
                col.rect.x() + side, top * u, col.rect.width() - 2 * side, FILTER_H_U * u
            ).adjusted(0.5, 0.5, -0.5, -0.5)
            fill_round(p, box, qcolor(PALETTE.canvas, 0.6), 0.9 * u)
            p.save()
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(qcolor(PALETTE.accent, 0.7))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box, 0.9 * u, 0.9 * u)
            p.restore()
            p.setFont(units.font(1.35))
            p.setPen(qcolor(PALETTE.text))
            text = "⌕ " + self.filter_text + "▏"
            inner = box.adjusted(1.0 * u, 0, -1.0 * u, 0)
            p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))
        if col.message:
            error = col.message in (ERROR_TEXT, PROVIDER_ERROR_TEXT)
            p.setFont(units.font(1.35))
            p.setPen(qcolor(PALETTE.text, 0.75 if error else 0.5))
            view = col.view.geometry()
            p.drawText(
                QRectF(view.x(), view.y(), view.width(), 6.0 * u),
                Qt.AlignCenter | Qt.TextWordWrap,
                col.message,
            )
