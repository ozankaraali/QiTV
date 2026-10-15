"""The two-layer drawer (spec §5.2): the webOS-like layered list over the video."""

import logging

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate

from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavModel, NavNode
from ui.overlays.base import Overlay
from ui.overlays.channel_rows import ChannelListView, ChannelRowDelegate, ChannelRowsModel
from ui.overlays.painting import Units, draw_icon, elided, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

LEFT_FRACTION = 0.25
RIGHT_FRACTION = 0.58
FADE_U = 7.0
PAD_TOP_U = 2.4
PAD_SIDE_U = 1.6
CRUMB_H_U = 2.2
FILTER_H_U = 3.4
LOADING_TEXT = "Loading…"
EMPTY_TEXT = "Nothing here"
ERROR_TEXT = "Couldn't load — press Enter to retry"

NodeRole = Qt.UserRole + 10
CountRole = Qt.UserRole + 11
IconRole = Qt.UserRole + 12
_PINNED_ICONS = {FAVORITES_GROUP: "star", RECENT_GROUP: "recent"}


def _is_live_item(nav: NavModel, node: NavNode) -> bool:
    return node.layer == "item" and nav.section_of(node) == "live"


class _NodeModel(ChannelRowsModel):
    """Channel rows for live items; titles, counts and icons for everything else."""

    def __init__(self, logos, now_provider, parent=None) -> None:
        super().__init__(logos, now_provider, parent)
        self._nodes: list[NavNode] = []

    def set_nodes(self, nodes: list[NavNode], live: bool) -> None:
        self._nodes = list(nodes)
        rows = [
            n.payload if live and isinstance(n.payload, dict) else {"name": n.title} for n in nodes
        ]
        self.set_channels(rows)

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
                return _PINNED_ICONS.get(node.key) if node.layer == "group" else None
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
        focused = bool(option.widget.property("columnFocused")) if option.widget else True
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
        self.parent: NavNode | None = None
        self.nodes: list[NavNode] = []
        self.current: NavNode | None = None
        self.message: str | None = None
        self.rect = QRect()


class Drawer(Overlay):
    playRequested = Signal(object, object, object)
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
        nav.changed.connect(self._on_nav_changed)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_drawer(self, focus_key: str | None = None) -> None:
        self.focus_side = "right"
        self.filter_text = ""
        self._filter_key = None
        self._pending_key = focus_key
        self._columns["right"].parent = None  # force the highlight to be re-chosen
        self._relayout()
        self._refresh()
        self.show()
        self.raise_()

    def close_drawer(self) -> None:
        was_visible = self.isVisible()
        self.hide()
        if was_visible:
            self.closed.emit()

    def highlighted(self) -> NavNode | None:
        col = self._columns[self.focus_side]
        return col.current if col.current in col.nodes else None

    def message_for(self, side: str) -> str | None:
        return self._columns[side].message

    def move(self, delta: int) -> None:  # type: ignore[override]  # name fixed by the brief
        col = self._columns[self.focus_side]
        if not col.nodes:
            return
        self._pending_key = None
        try:
            row = col.nodes.index(col.current)  # type: ignore[arg-type]
        except ValueError:
            row = -1 if delta > 0 else len(col.nodes)
        self._select(col, min(len(col.nodes) - 1, max(0, row + delta)))

    def page(self, delta: int) -> None:
        col = self._columns[self.focus_side]
        row_h = col.view.sizeHintForRow(0)
        rows = max(1, col.view.height() // row_h - 1) if row_h > 0 else 8
        self.move(delta * rows)

    def left(self) -> None:
        if self.focus_side == "right":
            self.focus_side = "left"
            self._refresh()
        else:
            self._nav.up()

    def right(self) -> None:
        if self.focus_side == "left":
            self.focus_side = "right"
            self._refresh()
            return
        col = self._columns["right"]
        if self._retry(col):
            return
        node = self.highlighted()
        if node is None:
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

    def escape(self) -> bool:
        if self.filter_text:
            self.filter_text = ""
            self._refresh()
            return True
        self.close_drawer()
        return True

    # ---- state ------------------------------------------------------------------------------

    def _retry(self, col: _Column) -> bool:
        if col.message != ERROR_TEXT or col.parent is None:
            return False
        self._nav.retry(col.parent)
        return True

    def _select(self, col: _Column, row: int) -> None:
        node = col.nodes[row]
        if col.side == "left":
            if node != col.current:
                self._nav.open(node)  # the preview; _refresh re-highlights via nav.changed
            return
        col.current = node
        self._show_current(col)

    def _show_current(self, col: _Column) -> None:
        if col.current in col.nodes:
            idx = col.model.index(col.nodes.index(col.current))
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

    def _on_nav_changed(self) -> None:
        if self.isVisible():
            self._refresh()

    def _matches(self, node: NavNode) -> bool:
        return self.filter_text.casefold() in node.title.casefold()

    def _refresh(self) -> None:
        (left_parent, left_children), (right_parent, right_children) = self._nav.columns()
        focused_parent = left_parent if self.focus_side == "left" else right_parent
        key = (self.focus_side, focused_parent)
        if key != self._filter_key:
            self._filter_key = key
            self.filter_text = ""
        path = self._nav.path
        self._fill(self._columns["left"], left_parent, left_children, path[-1] if path else None)
        self._fill(self._columns["right"], right_parent, right_children, None)
        for side, col in self._columns.items():
            col.view.setProperty("columnFocused", side == self.focus_side)
            col.view.viewport().update()
        self.update()

    def _fill(
        self,
        col: _Column,
        parent: NavNode | None,
        children: list[NavNode] | None,
        current: NavNode | None,
    ) -> None:
        parent_changed = parent != col.parent
        col.parent = parent
        if not children and parent is not None and self._nav.load_error(parent):
            col.message = ERROR_TEXT
        elif children is None:
            col.message = LOADING_TEXT
        else:
            col.message = None
        nodes = children or []
        if col.side == self.focus_side and self.filter_text:
            nodes = [n for n in nodes if self._matches(n)]
        if col.message is None and not nodes:
            col.message = EMPTY_TEXT
        col.nodes = nodes
        live = bool(nodes) and _is_live_item(self._nav, nodes[0])
        col.view.setItemDelegate(col.channel_delegate if live else col.simple_delegate)
        col.model.set_nodes(nodes, live)
        if current is not None:
            col.current = current
        elif self._pending_key is not None and col.side == "right":
            match = next((n for n in nodes if n.key == self._pending_key), None)
            if match is not None:
                col.current = match
                self._pending_key = None
            elif parent_changed or col.current not in nodes:
                col.current = nodes[0] if nodes else None
        elif parent_changed or col.current not in nodes:
            col.current = nodes[0] if nodes else None
        self._show_current(col)

    # ---- layout and painting ----------------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        return False

    def _relayout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        host_w, host_h = parent.width(), parent.height()
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

    def _paint_breadcrumb(self, p: QPainter, col: _Column) -> None:
        units = self._units
        parts = [n.title for n in self._nav.path[:-1]]
        if not parts:
            return
        p.setFont(units.font(1.15))
        side = units.px(PAD_SIDE_U)
        rect = QRectF(
            col.rect.x() + side, units.px(PAD_TOP_U), col.rect.width() - 2 * side, units.px(2.2)
        )
        head = " › ".join(parts[:-1]) + (" › " if len(parts) > 1 else "")
        last = elided(p, parts[-1], rect.width())
        fm = p.fontMetrics()
        head = fm.elidedText(
            head, Qt.ElideLeft, max(0, int(rect.width() - fm.horizontalAdvance(last)))
        )
        p.setPen(qcolor(PALETTE.text, 0.6))
        p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, head)
        p.setPen(qcolor(PALETTE.accent))
        p.drawText(
            rect.adjusted(fm.horizontalAdvance(head), 0, 0, 0), Qt.AlignLeft | Qt.AlignVCenter, last
        )

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
            error = col.message == ERROR_TEXT
            p.setFont(units.font(1.35))
            p.setPen(qcolor(PALETTE.text, 0.75 if error else 0.5))
            view = col.view.geometry()
            p.drawText(
                QRectF(view.x(), view.y(), view.width(), 6.0 * u),
                Qt.AlignCenter | Qt.TextWordWrap,
                col.message,
            )
