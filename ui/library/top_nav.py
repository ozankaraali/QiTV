"""The Library's top bar: the QiTV logo, Live TV / Movies / Series, the Search and source pills,
and a fullscreen button.

The logo is about the height of the old "QiTV" wordmark's capitals ×1.6, vertically centred; the
tabs start the same gap after it as they did after the wordmark. Its name stays for assistive
technology (the accessible name) and as the logo's tooltip.

The fullscreen button (⛶) ends the bar: the Library's F11 (⌃⌘F on a Mac) for the mouse, since
Windows and Linux hide the menu bar in fullscreen (the owner's mouse-only rule). The keys never
focus it; they have F11.
"""

import sys

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QFontMetricsF, QHelpEvent, QPainter
from PySide6.QtWidgets import QToolTip, QWidget

from ui.brand import BRAND_NAME, logo_aspect, logo_available, logo_pixmap
from ui.keymap import key_hint
from ui.overlays.painting import Units, draw_icon, elided, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

HEIGHT_U = 6.0
DEFAULT_HOST_WIDTH = 1280
TAB_LABELS = {"live": "Live TV", "movies": "Movies", "series": "Series"}
ALL_TABS = ["live", "movies", "series"]
SEARCH_LABEL = "⌕ Search"
PILL_H_U = 3.2
MAX_SOURCE_U = 22.0
BRAND_FONT_U = 1.6  # the old wordmark's size: the logo is sized from its capitals
LOGO_CAP_FACTOR = 1.6
LOGO_LEFT_U = 2.0
LOGO_GAP_U = 3.0  # between the logo and the first tab
FULLSCREEN_KEYS = "Ctrl+Meta+F" if sys.platform == "darwin" else "F11"  # macOS keeps F11


class TopNav(QWidget):
    tabRequested = Signal(str)  # "live" | "movies" | "series"
    searchRequested = Signal()
    sourceRequested = Signal(QPoint)  # global position under the pill
    fullscreenRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName(BRAND_NAME)
        self._tabs = list(ALL_TABS)
        self._active = "live"
        self._source = ""
        self._focused = False
        self._focus_item = "live"
        self._pressed: str | None = None
        if parent is not None:
            parent.installEventFilter(self)
        self._relayout()

    # ---- public API -------------------------------------------------------------------------

    def set_active(self, tab: str) -> None:
        if tab not in (*ALL_TABS, "search"):
            raise ValueError(f"Unknown tab: {tab!r}")
        self._active = tab
        self._focus_item = tab
        self.update()

    def set_source_name(self, name: str) -> None:
        self._source = name
        self.update()

    def set_tabs(self, tabs: list[str]) -> None:
        self._tabs = [t for t in ALL_TABS if t in tabs]
        if self._focus_item in ALL_TABS and self._focus_item not in self._tabs:
            self._focus_item = "search"
        self.update()

    def set_focused(self, focused: bool) -> None:
        self._focused = focused
        self.update()

    def move_by(self, delta: int) -> None:
        order = self._order()
        row = order.index(self._focus_item) if self._focus_item in order else 0
        self._focus_item = order[min(len(order) - 1, max(0, row + delta))]
        self.update()

    def activate(self) -> None:
        self._trigger(self._focus_item)

    def logo_rect(self) -> QRectF:
        """Where the logo is drawn: whole pixels, so it stays crisp."""
        units = Units(self._host_width())
        brand_font = units.font(BRAND_FONT_U, QFont.Weight.Bold)
        if not logo_available():  # the asset is missing: the name, as before
            width = QFontMetrics(brand_font).horizontalAdvance(BRAND_NAME)
            return QRectF(LOGO_LEFT_U * units.u, 0, width, self.height())
        height = round(QFontMetricsF(brand_font).capHeight() * LOGO_CAP_FACTOR)
        width = round(height * logo_aspect())
        return QRectF(
            round(LOGO_LEFT_U * units.u), round((self.height() - height) / 2), width, height
        )

    # ---- internals --------------------------------------------------------------------------

    def _order(self) -> list[str]:
        return [*self._tabs, "search", "source"]

    def _trigger(self, item: str) -> None:
        if item in ALL_TABS:
            self.tabRequested.emit(item)
        elif item == "search":
            self.searchRequested.emit()
        elif item == "source":
            rect = self._item_rects()["source"]
            self.sourceRequested.emit(
                self.mapToGlobal(QPoint(int(rect.left()), int(rect.bottom())))
            )
        elif item == "fullscreen":
            self.fullscreenRequested.emit()

    def _host_width(self) -> int:
        parent = self.parentWidget()
        return parent.width() if parent is not None and parent.width() > 0 else DEFAULT_HOST_WIDTH

    def _source_text(self) -> str:
        return f"{self._source} ▾" if self._source else "Source ▾"

    def _item_rects(self) -> dict[str, QRectF]:
        units = Units(self._host_width())
        u = units.u
        height = self.height()
        rects: dict[str, QRectF] = {}
        # right-aligned pills
        pill_h = PILL_H_U * u
        top = (height - pill_h) / 2
        pad = 1.4 * u
        pill_font = units.font(1.1)
        fm = QFontMetrics(pill_font)
        source_w = (
            min(fm.horizontalAdvance(self._source_text()) + u, MAX_SOURCE_U * u - 2 * pad) + 2 * pad
        )
        search_w = fm.horizontalAdvance(SEARCH_LABEL) + u + 2 * pad
        x = self.width() - 2.0 * u - pill_h
        rects["fullscreen"] = QRectF(x, top, pill_h, pill_h)  # a square pill
        x -= 1.0 * u + source_w
        rects["source"] = QRectF(x, top, source_w, pill_h)
        x -= 1.0 * u + search_w
        rects["search"] = QRectF(x, top, search_w, pill_h)
        # tabs
        x = self.logo_rect().right() + LOGO_GAP_U * u
        tab_fm = QFontMetrics(units.font(1.3, QFont.Weight.DemiBold))
        for tab in self._tabs:
            width = tab_fm.horizontalAdvance(TAB_LABELS[tab])
            rects[tab] = QRectF(x, 0, width, height)
            x += width + 2.6 * u
        return rects

    def _item_at(self, pos) -> str | None:
        u = Units(self._host_width()).u
        for item, rect in self._item_rects().items():
            # tabs are text-wide, so widen them; pills are already padded and 1u apart
            hit = rect.adjusted(-0.8 * u, 0, 0.8 * u, 0) if item in ALL_TABS else rect
            if hit.contains(pos):
                return item
        return None

    def mousePressEvent(self, event) -> None:
        self._pressed = self._item_at(event.position())
        if self._pressed is None:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._item_at(event.position()) == "fullscreen":
            event.accept()  # a quick second click: not in and straight out again
            return
        super().mouseDoubleClickEvent(event)  # one more press

    def mouseReleaseEvent(self, event) -> None:
        item, self._pressed = self._pressed, None
        if item is None or self._item_at(event.position()) != item:
            super().mouseReleaseEvent(event)
            return
        if item in self._order():  # ⛶ is not: the keys' focus stays where it was
            self._focus_item = item
            self.update()
        self._trigger(item)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        return False

    def tooltip_at(self, pos: QPointF) -> str:
        """What hovering `pos` says: the logo's name, or the fullscreen button and its key."""
        if self.logo_rect().contains(pos):
            return BRAND_NAME
        if self._item_rects()["fullscreen"].contains(pos):
            return key_hint("Fullscreen", FULLSCREEN_KEYS)
        return ""

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            text = self.tooltip_at(QPointF(event.pos()))  # QHelpEvent has no position()
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update()

    def _relayout(self) -> None:
        self.setFixedHeight(Units(self._host_width()).px(HEIGHT_U))
        self.update()

    def _paint_focus(self, p: QPainter, rect: QRectF, radius: float) -> None:
        fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(qcolor(PALETTE.accent))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        p.restore()

    def paintEvent(self, event) -> None:
        units = Units(self._host_width())
        u = units.u
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), qcolor(PALETTE.canvas))
        p.fillRect(0, self.height() - 1, self.width(), 1, qcolor(PALETTE.text, 0.06))
        rects = self._item_rects()
        height = self.height()
        logo_rect = self.logo_rect()
        logo = logo_pixmap(logo_rect.height(), self.devicePixelRatioF())
        if logo is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(logo_rect.topLeft(), logo)
        else:
            p.setFont(units.font(BRAND_FONT_U, QFont.Weight.Bold))
            p.setPen(qcolor(PALETTE.text))
            p.drawText(logo_rect, Qt.AlignLeft | Qt.AlignVCenter, BRAND_NAME)
        for tab in self._tabs:
            rect = rects[tab]
            active = tab == self._active
            if self._focused and self._focus_item == tab:
                self._paint_focus(
                    p,
                    QRectF(
                        rect.x() - 0.8 * u, (height - 3.6 * u) / 2, rect.width() + 1.6 * u, 3.6 * u
                    ),
                    1.0 * u,
                )
            p.setFont(units.font(1.3, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.text, 1.0 if active else PALETTE.text_muted_alpha))
            p.drawText(rect, Qt.AlignCenter, TAB_LABELS[tab])
            if active:
                p.fillRect(
                    QRectF(rect.x(), height - 1.25 * u, rect.width(), 0.25 * u),
                    qcolor(PALETTE.accent),
                )
        p.setFont(units.font(1.1))
        pad = 1.4 * u
        for item, text in (("search", SEARCH_LABEL), ("source", self._source_text())):
            rect = rects[item]
            radius = 1.6 * u
            if self._focused and self._focus_item == item:
                self._paint_focus(p, rect, radius)
            else:
                p.setPen(qcolor(PALETTE.text, 0.15))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            active = item == self._active
            p.setPen(qcolor(PALETTE.accent if active else PALETTE.text, 1.0 if active else 0.85))
            inner = rect.adjusted(pad, 0, -pad, 0)
            p.drawText(inner, Qt.AlignCenter, elided(p, text, inner.width()))
        rect = rects["fullscreen"]
        p.setPen(qcolor(PALETTE.text, 0.15))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 1.6 * u, 1.6 * u)
        icon = rect.height() * 0.5
        draw_icon(
            p,
            "fullscreen",
            QRectF(rect.center().x() - icon / 2, rect.center().y() - icon / 2, icon, icon),
            qcolor(PALETTE.text, 0.85),
        )
        p.end()
