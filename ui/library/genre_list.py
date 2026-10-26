"""The Library's left genre column: type-to-filter, PINNED and GENRES sections (spec §5.3)."""

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QAbstractItemView, QStyle, QStyledItemDelegate, QWidget

from services import i18n
from services.i18n import tr
from services.library import CONTINUE, MY_LIST, NEW, RECORDINGS, Genre
from ui.overlays.channel_rows import ChannelListView
from ui.overlays.painting import Units, draw_icon, elided, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

WIDTH_U = 18.0
FIELD_MARGIN_U = 1.2
FIELD_H_U = 3.2
FIELD_RADIUS_U = 0.8
ROW_H_U = 3.2
PREVIEW_DEBOUNCE_MS = 250
DEFAULT_HOST_WIDTH = 1280

KindRole = Qt.UserRole + 20
CountRole = Qt.UserRole + 21
IdRole = Qt.UserRole + 22
PINNED_ICONS = {CONTINUE: "clock", NEW: "sparkle", MY_LIST: "star", RECORDINGS: "record"}
ICON_SCALE = 1.2  # icon box / cap height: the glyphs sit inside a padded unit box
ICON_GAP_U = 0.7
HEADER = "header"
GENRE = "genre"


def format_count(count: int | None) -> str:
    return "" if count is None else i18n.number(count)


class _GenreModel(QAbstractListModel):
    """Section headers and genres in one flat list; headers are not selectable."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, str, int | None, str | None]] = []

    def set_genres(self, genres: list[Genre]) -> None:
        pinned = [g for g in genres if g.pinned]
        rest = [g for g in genres if not g.pinned]
        rows: list[tuple[str, str, int | None, str | None]] = []
        if pinned:
            # i18n: header above the Library's shelves (Continue, New, My list); shown in capitals
            rows.append((HEADER, i18n.upper(tr("Pinned")), None, None))
            rows += [(GENRE, g.title, g.count, g.id) for g in pinned]
        if rest:
            count = sum(1 for g in rest if not g.header)
            # i18n: header above the Library's genres, with their number; shown in capitals
            rows.append((HEADER, i18n.upper(tr("Genres · {n}", n=count)), None, None))
            # All providers: each provider's name heads its genres (a row that cannot be chosen)
            rows += [
                (HEADER, g.title, None, None) if g.header else (GENRE, g.title, g.count, g.id)
                for g in rest
            ]
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        kind, label, count, genre_id = self._rows[index.row()]
        if role == Qt.DisplayRole:
            return label
        if role == KindRole:
            return kind
        if role == CountRole:
            return count
        if role == IdRole:
            return genre_id
        return None

    def flags(self, index):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return Qt.NoItemFlags
        if self._rows[index.row()][0] == HEADER:
            return Qt.ItemIsEnabled
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def labels(self) -> list[str]:
        return [row[1] for row in self._rows]


class _GenreDelegate(QStyledItemDelegate):
    def _units(self, widget) -> Units:
        return Units(getattr(widget, "units_width", DEFAULT_HOST_WIDTH) or DEFAULT_HOST_WIDTH)

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, self._units(option.widget).px(ROW_H_U))

    def paint(self, p: QPainter, option, index) -> None:
        units = self._units(option.widget)
        u = units.u
        rect = QRectF(option.rect)
        pad = 1.4 * u
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        if index.data(KindRole) == HEADER:
            font = units.font(0.85)
            font.setLetterSpacing(QFont.AbsoluteSpacing, 0.08 * font.pixelSize())
            p.setFont(font)
            p.setPen(qcolor(PALETTE.text, 0.45))
            p.drawText(
                rect.adjusted(pad, 0.6 * u, -pad, 0),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(index.data(Qt.DisplayRole)),
            )
            p.restore()
            return
        focused = bool(option.widget.property("columnFocused")) if option.widget else True
        if option.state & QStyle.State_Selected:
            box = rect.adjusted(0.6 * u, 0.15 * u, -0.6 * u, -0.15 * u)
            if focused:
                fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), 0.8 * u)
                p.setPen(qcolor(PALETTE.accent))
                p.setBrush(Qt.NoBrush)
                ring = box.adjusted(0.75, 0.75, -0.75, -0.75)
                p.drawRoundedRect(ring, 0.8 * u, 0.8 * u)
            else:
                fill_round(p, box, qcolor(PALETTE.text, 0.10), 0.8 * u)
        right = rect.right() - pad
        count_text = format_count(index.data(CountRole))
        if count_text:
            p.setFont(units.font(0.95))
            p.setPen(qcolor(PALETTE.text, 0.45))
            width = p.fontMetrics().horizontalAdvance(count_text)
            p.drawText(
                QRectF(right - width, rect.y(), width, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                count_text,
            )
            right -= width + 0.8 * u
        p.setFont(units.font(1.15))
        p.setPen(qcolor(PALETTE.text))
        left = rect.x() + pad
        icon = PINNED_ICONS.get(index.data(IdRole))
        if icon:
            size = QFontMetricsF(p.font()).capHeight() * ICON_SCALE
            color = qcolor(PALETTE.accent) if icon == "star" else qcolor(PALETTE.text)
            draw_icon(p, icon, QRectF(left, rect.center().y() - size / 2, size, size), color)
            left += size + ICON_GAP_U * u
        p.drawText(
            QRectF(left, rect.y(), max(0.0, right - left), rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, str(index.data(Qt.DisplayRole) or ""), right - left),
        )
        p.restore()


class GenreList(QWidget):
    genreSelected = Signal(str)  # debounced highlight change (preview)
    genreActivated = Signal(str)  # Enter / → / click: move focus to the grid
    filterChanged = Signal(str)
    genreMenuRequested = Signal(str, QPoint)  # 2.8: a right-click on a genre (global point)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.filter_text = ""
        self._focused = False
        self._current: str | None = None
        self._ids: list[str] = []
        self._genres: list[Genre] | None = None  # what set_genres last showed
        self._rows_by_id: dict[str, int] = {}
        self._model = _GenreModel(self)
        self._view = ChannelListView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(_GenreDelegate(self._view))
        self._view.rowClicked.connect(self._on_row_clicked)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_menu_asked)
        self._view.setProperty("columnFocused", False)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._emit_preview)
        if parent is not None:
            parent.installEventFilter(self)
        self._relayout()

    def _on_menu_asked(self, pos: QPoint) -> None:
        """A right-click on a genre (never a heading): its menu (2.8: parental controls)."""
        index = self._view.indexAt(pos)
        if index.isValid() and index.data(KindRole) == GENRE:
            gid = str(index.data(IdRole))
            self.genreMenuRequested.emit(gid, self._view.viewport().mapToGlobal(pos))

    # ---- public API -------------------------------------------------------------------------

    def rows(self) -> list[str]:
        """The painted labels in order: section headers and genre titles."""
        return self._model.labels()

    def current_id(self) -> str | None:
        return self._current

    def set_genres(self, genres: list[Genre]) -> None:
        if genres != self._genres:  # the same genres again (a genre switch): keep the list
            self._genres = list(genres)
            self._ids = [g.id for g in genres if not g.header]
            self._model.set_genres(genres)
            self._rows_by_id = {
                self._model.index(i).data(IdRole): i
                for i in range(self._model.rowCount())
                if self._model.index(i).data(KindRole) == GENRE
            }
        if self._current in self._ids:
            self._show_current()
            return
        self._current = self._ids[0] if self._ids else None
        self._show_current()
        if self._current is None:
            self._cancel_preview()
        else:
            self._preview_timer.start()

    def set_current(self, genre_id: str | None) -> None:
        self._cancel_preview()
        self._current = genre_id if genre_id in self._ids else None
        self._show_current()

    def move_by(self, delta: int) -> None:
        if not self._ids:
            return
        if self._current in self._ids:
            row = self._ids.index(self._current)
        else:
            row = -1 if delta > 0 else len(self._ids)
        new = self._ids[min(len(self._ids) - 1, max(0, row + delta))]
        if new == self._current:
            return
        self._current = new
        self._show_current()
        self._preview_timer.start()

    def page(self, delta: int) -> None:
        row_h = self._view.sizeHintForRow(0)
        headers = self._model.rowCount() - len(self._ids)
        rows = max(1, self._view.height() // row_h - 1 - headers) if row_h > 0 else 8
        self.move_by(delta * rows)

    def activate(self) -> None:
        if self._current is None:
            return
        if self._preview_timer.isActive():
            self._preview_timer.stop()
            self._emit_preview()
        self.genreActivated.emit(self._current)

    def type_text(self, text: str) -> None:
        if text:
            self.filter_text += text
            self._filter_changed()

    def backspace(self) -> None:
        if self.filter_text:
            self.filter_text = self.filter_text[:-1]
            self._filter_changed()

    def escape(self) -> bool:
        if not self.filter_text:
            return False
        self.filter_text = ""
        self._filter_changed()
        return True

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self._view.setProperty("columnFocused", focused)
        self._view.viewport().update()
        self.update()

    # ---- internals --------------------------------------------------------------------------

    def _filter_changed(self) -> None:
        self.update()
        self.filterChanged.emit(self.filter_text)

    def _emit_preview(self) -> None:
        if self._current is not None:
            self.genreSelected.emit(self._current)

    def _cancel_preview(self) -> None:
        self._preview_timer.stop()

    def _show_current(self) -> None:
        row = self._rows_by_id.get(self._current, -1) if self._current is not None else -1
        if row < 0:
            self._view.clearSelection()
            self._view.setCurrentIndex(self._model.index(-1))
            return
        idx = self._model.index(row)
        self._view.setCurrentIndex(idx)
        self._view.scrollTo(idx, QAbstractItemView.EnsureVisible)

    def _on_row_clicked(self, row: int) -> None:
        genre_id = self._model.index(row).data(IdRole)
        if genre_id is None:
            return
        if genre_id != self._current:
            self._current = genre_id
            self._show_current()
            self._preview_timer.start()
        self.activate()

    # ---- layout and painting ----------------------------------------------------------------

    def _host_width(self) -> int:
        parent = self.parentWidget()
        return parent.width() if parent is not None and parent.width() > 0 else DEFAULT_HOST_WIDTH

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        return False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        units = Units(self._host_width())
        self.setFixedWidth(units.px(WIDTH_U))
        top = units.px(FIELD_MARGIN_U * 2 + FIELD_H_U - 0.4)
        self._view.units_width = units.width
        self._view.setGeometry(0, top, max(0, self.width() - 1), max(0, self.height() - top))
        self._view.doItemsLayout()
        self.update()

    def paintEvent(self, event) -> None:
        units = Units(self._host_width())
        u = units.u
        p = QPainter(self)
        p.fillRect(self.rect(), qcolor(PALETTE.panel_raised))
        p.fillRect(self.width() - 1, 0, 1, self.height(), qcolor(PALETTE.text, 0.06))
        margin = FIELD_MARGIN_U * u
        box = QRectF(margin, margin, self.width() - 1 - 2 * margin, FIELD_H_U * u)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(qcolor(PALETTE.text, 0.12))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(
            box.adjusted(0.5, 0.5, -0.5, -0.5), FIELD_RADIUS_U * u, FIELD_RADIUS_U * u
        )
        p.setFont(units.font(1.05))
        inner = box.adjusted(1.0 * u, 0, -1.0 * u, 0)
        if self.filter_text:
            p.setPen(qcolor(PALETTE.text))
            text = "⌕ " + self.filter_text + "▏"
        else:
            p.setPen(qcolor(PALETTE.text, 0.45))
            text = "⌕ " + tr("Find a genre…")  # i18n: placeholder of the Library's genre filter
        p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))
        p.end()
