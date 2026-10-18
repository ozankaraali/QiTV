"""Virtualized channel rows: model, delegate and list view for the fast-zap column."""

from collections.abc import Callable, Sequence
import logging
import math

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QPersistentModelIndex,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetrics, QFontMetricsF, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QFrame, QListView, QStyle, QStyledItemDelegate

from services.provider_ids import provider_of
from services.zapping import ZapContext
from ui.overlays.painting import Units, draw_logo, draw_progress, elided, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

NumberRole = Qt.UserRole + 1
NUMBER_W_U = 3.4  # the number column: room for three digits, wider for longer numbers


def number_column_width(units, digits: int) -> float:
    """The channel number column's width for numbers of up to `digits` digits."""
    width = NUMBER_W_U * units.u
    if digits > 3:
        advance = QFontMetrics(units.font(1.5)).horizontalAdvance("0" * digits)
        width = max(width, advance + 0.2 * units.u)
    return width


LogoRole = Qt.UserRole + 2
NowRole = Qt.UserRole + 3
ProgressRole = Qt.UserRole + 4
ProviderRole = Qt.UserRole + 5  # the provider's name, in the lists that mix providers


def tag_font(units) -> QFont:
    """The provider tag's font: the transport chips' (1.2u, demi-bold)."""
    return units.font(1.2, QFont.Weight.DemiBold)


def tag_layout(
    units, name_font: QFont, tag_font_: QFont, name: str, tag: str, width: float
) -> tuple[float, float, float]:
    """(name width, tag x offset, tag chip width) for a channel's name and its provider's tag
    in `width`, measured with QFontMetricsF. The name elides first: the tag keeps its full text
    and goes right after the name, and is cut only when it alone is wider than the row. The
    name and the tag never overlap and both stay inside `width`."""
    pad, gap = 0.8 * units.u, 0.8 * units.u
    chip = min(QFontMetricsF(tag_font_).horizontalAdvance(tag) + 2 * pad, max(0.0, width))
    room = math.floor(max(0.0, width - chip - gap))
    name_w = float(min(math.ceil(QFontMetricsF(name_font).horizontalAdvance(name)), room))
    return name_w, (name_w + gap if name_w > 0 else 0.0), chip


LOGO_ROWS_MAX = 5000  # past this many rows the model does not index them by logo


class LazyNumbers:
    """The numbers of a very long list of rows, worked out when a row asks (a list of 200,000
    would cost as much to make as to show). `digits` is the longest number's length and
    `signature` tells one set of numbers from another."""

    digits = 0
    signature: tuple = ()

    def __len__(self) -> int:
        raise NotImplementedError

    def __getitem__(self, row: int) -> str | None:
        raise NotImplementedError


class ChannelRowsModel(QAbstractListModel):
    NumberRole = NumberRole
    LogoRole = LogoRole
    NowRole = NowRole
    ProgressRole = ProgressRole
    ProviderRole = ProviderRole

    def __init__(
        self,
        logos,
        now_provider: Callable[[dict], tuple[str, float] | None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._logos = logos
        self._now_provider = now_provider
        self._items: list[dict] = []
        self._numbers: Sequence[str | None] | None = None
        self.show_provider = False  # rows name their provider (a list that mixes providers)
        self.number_digits = 0  # the longest number shown (the number column's width)
        self._rows_by_logo: dict[str, list[int]] | None = {}  # None: too many rows to index
        self._now_cache: dict[int, tuple[str, float]] = {}
        if logos is not None:
            logos.logoReady.connect(self._on_logo_ready)

    def set_channels(
        self,
        items: list[dict],
        numbers: list[str | None] | None = None,
        show_provider: bool = False,
    ) -> None:
        """Show `items`; `numbers` gives each row's number (default: their own).

        When no channel has a number, the list position stands in, as on the banner and
        for number entry (ZapContext.by_number).
        """
        self.beginResetModel()
        self.show_provider = show_provider
        self._items = list(items)
        if numbers is None and self._items:
            if not any(ZapContext.number_of(item) for item in self._items):
                numbers = [str(row + 1) for row in range(len(self._items))]
        if isinstance(numbers, LazyNumbers):
            self._numbers = numbers
            self.number_digits = numbers.digits
        else:
            self._numbers = list(numbers) if numbers is not None else None
            shown = (
                self._numbers
                if self._numbers is not None
                else [i.get("number") for i in self._items]
            )
            # The number column fits the longest number shown (a provider's 9521: four digits).
            self.number_digits = max((len(str(n)) for n in shown if n is not None), default=0)
        if len(self._items) > LOGO_ROWS_MAX:
            self._rows_by_logo = None
        else:
            self._rows_by_logo = {}
            for row, item in enumerate(self._items):
                url = item.get("logo")
                if url:
                    self._rows_by_logo.setdefault(url, []).append(row)
        self._now_cache.clear()
        self.endResetModel()

    def item(self, row: int) -> dict:
        return self._items[row]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def refresh_now(self) -> None:
        self._now_cache.clear()
        if self._items:
            self.dataChanged.emit(self.index(0), self.index(len(self._items) - 1))

    def _now(self, row: int) -> tuple[str, float]:
        cached = self._now_cache.get(row)
        if cached is None:
            try:
                result = self._now_provider(self._items[row])
            except Exception:
                logger.exception("now_provider failed")
                result = None
            cached = (result[0], float(result[1])) if result else ("", -1.0)
            self._now_cache[row] = cached
        return cached

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if not index.isValid() or not 0 <= row < len(self._items):
            return None
        item = self._items[row]
        if role == Qt.DisplayRole:
            return item.get("name", "")
        if role == NumberRole:
            return self._numbers[row] if self._numbers is not None else item.get("number")
        if role == ProviderRole:
            return provider_of(item) if self.show_provider else ""
        if role == NowRole:
            return self._now(row)[0]
        if role == ProgressRole:
            return self._now(row)[1]
        if role == LogoRole:
            url = item.get("logo") or ""
            if not url or self._logos is None:
                return None
            pixmap = self._logos.pixmap(url)
            if pixmap is None:
                # Again after an eviction or a failure: LogoCache drops repeats itself.
                self._logos.request([url])
            return pixmap
        return None

    def _on_logo_ready(self, url: str) -> None:
        if self._rows_by_logo is None:  # a long list: the view repaints the rows it shows
            if self._items:
                self.dataChanged.emit(self.index(0), self.index(len(self._items) - 1), [LogoRole])
            return
        for row in self._rows_by_logo.get(url, ()):
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, [LogoRole])


def column_focused(widget) -> bool:
    """Whether a row's list has the focus: the drawer marks its two columns; a list it does
    not mark (the fast-zap column) counts as focused."""
    focused = widget.property("columnFocused") if widget is not None else None
    return True if focused is None else bool(focused)


class ChannelRowDelegate(QStyledItemDelegate):
    def __init__(self, scale: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self._scale = scale

    def _units(self, widget) -> Units:
        width = getattr(widget, "units_width", 100) or 100
        return Units(round(width * self._scale))

    def sizeHint(self, option, index) -> QSize:
        units = self._units(option.widget)
        return QSize(0, units.px(6.6))

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
            p.save()
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(qcolor(PALETTE.accent, 0.7 if focused else 0.35))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 1.0 * u, 1.0 * u)
            p.restore()

        x = rect.x() + 1.0 * u + 0.3 * u
        right = rect.right() - 1.0 * u - 0.3 * u
        # number
        number = index.data(NumberRole)
        num_w = number_column_width(units, getattr(index.model(), "number_digits", 0))
        p.setFont(units.font(1.5))
        p.setPen(qcolor(PALETTE.text, 0.6))
        if number is not None:
            p.drawText(
                QRectF(x, rect.y(), num_w, rect.height()),
                Qt.AlignRight | Qt.AlignVCenter,
                str(number),
            )
        x += num_w + 1.2 * u
        # logo
        name = str(index.data(Qt.DisplayRole) or "")
        logo_size = 3.6 * u
        logo_rect = QRectF(x, rect.y() + (rect.height() - logo_size) / 2, logo_size, logo_size)
        pixmap = index.data(LogoRole)
        draw_logo(p, logo_rect, name, pixmap if isinstance(pixmap, QPixmap) else None, 0.9 * u)
        x += logo_size + 1.2 * u
        # text column
        width = max(0.0, right - x)
        now = str(index.data(NowRole) or "")
        progress = index.data(ProgressRole)
        progress = -1.0 if progress is None else float(progress)
        name_h, now_h, bar_h, gap = 2.1 * u, 1.7 * u, 0.3 * u, 0.4 * u
        total = name_h + (now_h if now else 0)
        has_bar = progress >= 0
        if has_bar:
            total += gap + bar_h
        y = rect.y() + (rect.height() - total) / 2
        name_font = units.font(1.6, QFont.Weight.DemiBold)
        p.setFont(name_font)
        p.setPen(qcolor(PALETTE.text))
        tag = str(index.data(ProviderRole) or "")
        if tag:
            # The provider's name follows the channel's, in a chip; the name elides first.
            chip_font = tag_font(units)
            name_w, chip_dx, chip_w = tag_layout(units, name_font, chip_font, name, tag, width)
            p.drawText(
                QRectF(x, y, name_w, name_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, name, name_w),
            )
            chip_h = min(name_h, QFontMetricsF(chip_font).height() + 0.4 * u)
            chip = QRectF(x + chip_dx, y + (name_h - chip_h) / 2, chip_w, chip_h)
            fill_round(p, chip, qcolor(PALETTE.text, 0.10), chip_h / 2)
            p.setFont(chip_font)
            p.setPen(qcolor(PALETTE.text, 0.75))
            p.drawText(
                chip.adjusted(0.8 * u, 0, -0.8 * u, 0),
                Qt.AlignCenter,
                QFontMetricsF(chip_font).elidedText(tag, Qt.ElideRight, max(0.0, chip_w - 1.6 * u)),
            )
        else:
            p.drawText(
                QRectF(x, y, width, name_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, name, width),
            )
        y += name_h
        if now:
            p.setFont(units.font(1.25))
            p.setPen(qcolor(PALETTE.text, 0.65))
            p.drawText(
                QRectF(x, y, width, now_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, now, width),
            )
            y += now_h
        if has_bar:
            draw_progress(p, QRectF(x, y + gap, width, bar_h), progress)
        p.restore()


class ChannelListView(QListView):
    rowClicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.units_width = 100
        self.setFrameShape(QFrame.NoFrame)
        self.setFocusPolicy(Qt.NoFocus)
        self.setUniformItemSizes(True)
        # A single pass asks the model for every row (200,000 channels: ~1 s on the GUI
        # thread at each reset); batches lay out the rows near the top first, the rest between
        # events, and scrollTo/indexAt lay out as far as they need.
        self.setLayoutMode(QListView.LayoutMode.Batched)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)
        self.clicked.connect(lambda idx: self.rowClicked.emit(idx.row()))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward are the shell's: not a row selection
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # a quick second Back/Forward is one more press for the shell
            return
        super().mouseDoubleClickEvent(event)
