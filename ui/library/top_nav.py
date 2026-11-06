"""The Library's top bar: the QiTV logo, Live TV / Movies / Series, the search field, the source
pill, a refresh button (2.8: the films and series shown load again) and a fullscreen button.

The search field (2.9.3, a pill before) is as wide as there is room for, up to SEARCH_MAX_U, in
the middle of the bar: the first place to type. The bar paints its look while the Library
browses; the Library's SearchField, attached here (`attach_search`), shows over it while it
searches. Where there is no room for it, the "Search" pill as before.

The logo is about the height of the old "QiTV" wordmark's capitals ×1.6, vertically centred; the
tabs start the same gap after it as they did after the wordmark. Its name stays for assistive
technology (the accessible name) and as the logo's tooltip.

The fullscreen button (⛶) ends the bar: the Library's F11 (⌃⌘F on a Mac) for the mouse, since
Windows and Linux hide the menu bar in fullscreen, and every key has a control on screen. The
keys never focus it; they have F11.
"""

import sys

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from services.i18n import N_, tr, translate
from ui.brand import BRAND_NAME, logo_aspect, logo_available, logo_pixmap
from ui.keymap import key_hint
from ui.overlays.base import TooltipWidget
from ui.overlays.painting import (
    Units,
    draw_field_icon,
    draw_icon,
    draw_refresh,
    elided,
    fill_round,
    ui_units,
)
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

HEIGHT_U = 6.0
DEFAULT_HOST_WIDTH = 1280
# The tabs' names (the search's section names), shown with translate().
TAB_LABELS = {
    "live": N_("Live TV"),
    "movies": N_("Movies"),
    "series": N_("Series"),
    # i18n: the Library's tab of everything recorded, recording and scheduled (2.9.7)
    "recordings": N_("Recordings"),
}
ALL_TABS = ["live", "movies", "series", "recordings"]
DEFAULT_TABS = ["live", "movies", "series"]  # Recordings once the Library has its page
MARK_U = 0.55  # 2.9.7: the Recordings tab's dot (red: something records; amber: a problem)
PILL_H_U = 3.2
MAX_SOURCE_U = 22.0
SEARCH_MAX_U = 44.0  # 2.9.3: the search field's widest
SEARCH_GAP_U = 2.0  # between it and the tabs, and the source pill
BRAND_FONT_U = 1.6  # the old wordmark's size: the logo is sized from its capitals
LOGO_CAP_FACTOR = 1.6
LOGO_LEFT_U = 2.0
LOGO_GAP_U = 3.0  # between the logo and the first tab
FULLSCREEN_KEYS = "Ctrl+Meta+F" if sys.platform == "darwin" else "F11"  # macOS keeps F11
REFRESH_KEYS = "Ctrl+Shift+R"  # 2.8: the films and series shown load again
HOVER_ALPHA = 0.08  # 2.9.3: a pill or button under the pointer
# i18n: tooltip of the Library's ↻ (before its key), and its entry in the source menu
REFRESH_TEXT = N_("Refresh films and series")


def _search_label() -> str:
    return tr("Search")  # i18n: pill button in the Library's top bar that opens search


def _icon_room(fm: QFontMetrics) -> float:
    """The search pill's magnifier and the gap after it (the finds' drawn icon)."""
    return fm.height() * 0.85 * 1.45


class TopNav(TooltipWidget):
    tabRequested = Signal(str)  # "live" | "movies" | "series"
    searchRequested = Signal()
    sourceRequested = Signal(QPoint)  # global position under the pill
    fullscreenRequested = Signal()
    refreshRequested = Signal()  # 2.8: ↻

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName(BRAND_NAME)
        self._tabs = list(DEFAULT_TABS)
        self._active = "live"
        self._describe()
        self._source = ""
        self._source_visible = True  # 2.9.7: hidden on the Recordings tab
        self._focused = False
        self._focus_item = "live"
        self._pressed: str | None = None
        self._hover: str | None = None  # 2.9.3: the item under the pointer is lit
        self._marks: dict[str, str] = {}  # 2.9.7: a tab's dot: "rec" or "warn"
        self.setMouseTracking(True)
        self._search_field: QWidget | None = None
        if parent is not None:
            parent.installEventFilter(self)
        self._relayout()

    # ---- public API -------------------------------------------------------------------------

    def set_active(self, tab: str) -> None:
        if tab not in (*ALL_TABS, "search"):
            raise ValueError(f"Unknown tab: {tab!r}")
        self._active = tab
        self._focus_item = tab
        self._describe()
        self.update()

    def _describe(self) -> None:
        """A screen reader says the section it shows after its name: "QiTV, Movies"."""
        label = TAB_LABELS.get(self._active)
        self.setAccessibleDescription(translate(label) if label else tr("Search"))

    def set_mark(self, tab: str, mark: str) -> None:
        """2.9.7: a dot after `tab`'s name: "rec" (red: something records), "warn" (amber:
        something needs attention), "" none."""
        if self._marks.get(tab, "") != mark:
            self._marks[tab] = mark
            self.update()

    def mark(self, tab: str) -> str:
        return self._marks.get(tab, "")

    def set_source_name(self, name: str) -> None:
        self._source = name
        self._place_search()
        self.update()

    def set_tabs(self, tabs: list[str]) -> None:
        self._tabs = [t for t in ALL_TABS if t in tabs]
        if self._focus_item in ALL_TABS and self._focus_item not in self._tabs:
            self._focus_item = "search"
        self._place_search()
        self.update()

    def attach_search(self, field: QWidget) -> None:
        """The Library's search field: placed on the bar's search, over it while it shows."""
        field.setParent(self)
        self._search_field = field
        self._place_search()

    def search_rect(self) -> QRectF:
        return self._item_rects()["search"]

    def focus_item(self) -> str:
        """What the keys' focus is on in the bar (when it has the focus)."""
        return self._focus_item

    def _place_search(self) -> None:
        if self._search_field is not None:
            self._search_field.setGeometry(self.search_rect().toAlignedRect())
            self._search_field.raise_()

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
        units = ui_units(self._host_width())
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

    def set_source_visible(self, visible: bool) -> None:
        """2.9.7: the provider's pill shows (not on the Recordings tab, which covers every
        provider): hidden, it is not laid out, painted, hovered nor in the keys' order, and
        the search takes its room."""
        if visible == self._source_visible:
            return
        self._source_visible = visible
        if not visible and self._focus_item == "source":
            self._focus_item = "search"
        self._place_search()
        self.update()

    def _order(self) -> list[str]:
        return (
            [*self._tabs, "search", "source"] if self._source_visible else [*self._tabs, "search"]
        )

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
        elif item == "refresh":
            self.refreshRequested.emit()

    def _host_width(self) -> int:
        parent = self.parentWidget()
        return parent.width() if parent is not None and parent.width() > 0 else DEFAULT_HOST_WIDTH

    def _source_text(self) -> str:
        # i18n: pill button in the Library's top bar: choose the provider shown
        return (self._source or tr("Source")) + " ▾"

    def _item_rects(self) -> dict[str, QRectF]:
        units = ui_units(self._host_width())
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
        search_w = fm.horizontalAdvance(_search_label()) + _icon_room(fm) + u + 2 * pad
        x = self.width() - 2.0 * u - pill_h
        rects["fullscreen"] = QRectF(x, top, pill_h, pill_h)  # a square pill
        x -= 1.0 * u + pill_h
        rects["refresh"] = QRectF(x, top, pill_h, pill_h)  # 2.8: ↻, after the source's pill
        if self._source_visible:
            x -= 1.0 * u + source_w
            rects["source"] = QRectF(x, top, source_w, pill_h)
        else:  # 2.9.7: no pill; the search ends where it began
            rects["source"] = QRectF(x, top, 0.0, pill_h)
        # tabs
        x = self.logo_rect().right() + LOGO_GAP_U * u
        tabs_end = x
        tab_fm = QFontMetrics(units.font(1.3, QFont.Weight.DemiBold))
        for tab in self._tabs:
            width = tab_fm.horizontalAdvance(translate(TAB_LABELS[tab]))
            rects[tab] = QRectF(x, 0, width, height)
            tabs_end = x + width
            x += width + 2.6 * u
        # the search: as wide as fits between them, in the middle of the bar
        left = tabs_end + SEARCH_GAP_U * u
        right = rects["source"].left() - SEARCH_GAP_U * u
        field_w = min(SEARCH_MAX_U * u, right - left)
        if field_w < search_w * 1.6:  # no room for a field: the pill, before the source's
            field_w = search_w
            field_x = rects["source"].left() - 1.0 * u - field_w
        else:
            field_x = max(left, min((self.width() - field_w) / 2, right - field_w))
        rects["search"] = QRectF(field_x, top, field_w, pill_h)
        return rects

    def _search_is_field(self, rect: QRectF) -> bool:
        fm = QFontMetrics(ui_units(self._host_width()).font(1.1))
        return (
            rect.width() > fm.horizontalAdvance(_search_label()) + _icon_room(fm) + 4 * fm.height()
        )

    def _item_at(self, pos) -> str | None:
        u = ui_units(self._host_width()).u
        for item, rect in self._item_rects().items():
            if item == "source" and not self._source_visible:
                continue
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
        if self._item_at(event.position()) in ("fullscreen", "refresh"):
            event.accept()  # a quick second click: not in and straight out again
            return
        super().mouseDoubleClickEvent(event)  # one more press

    def mouseMoveEvent(self, event) -> None:
        item = self._item_at(event.position())
        if item != self._hover:
            self._hover = item
            self.setCursor(Qt.PointingHandCursor if item is not None else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        item, self._pressed = self._pressed, None
        if item is None or self._item_at(event.position()) != item:
            super().mouseReleaseEvent(event)
            return
        if item in self._order():  # ⛶ and ↻ are not: the keys' focus stays where it was
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
        rects = self._item_rects()
        if rects["fullscreen"].contains(pos):
            # i18n: tooltip of the Library's fullscreen button (before its key)
            return key_hint(tr("Fullscreen"), FULLSCREEN_KEYS)
        if rects["refresh"].contains(pos):
            return key_hint(translate(REFRESH_TEXT), REFRESH_KEYS)
        return ""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_search()
        self.update()

    def _relayout(self) -> None:
        self.setFixedHeight(ui_units(self._host_width()).px(HEIGHT_U))
        self.update()

    def _paint_search_field(self, p: QPainter, units: Units, rect: QRectF) -> None:
        """The search field's look while the Library browses: a magnifier and what it finds."""
        field = self._search_field
        if field is not None and field.isVisible():
            return  # the Library's field shows over it: the query, its caret
        p.save()
        p.setFont(units.font(1.15))
        tone = qcolor(PALETTE.text, PALETTE.text_muted_alpha)
        inner = draw_field_icon(p, rect.adjusted(1.4 * units.u, 0, -1.4 * units.u, 0), tone)
        p.setPen(tone)
        # i18n: placeholder of the Library's search field
        text = tr("Search movies, series and channels")
        p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, elided(p, text, inner.width()))
        p.restore()

    def _paint_focus(self, p: QPainter, rect: QRectF, radius: float) -> None:
        fill_round(p, rect, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(qcolor(PALETTE.accent))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
        p.restore()

    def paintEvent(self, event) -> None:
        units = ui_units(self._host_width())
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
            lit = active or self._hover == tab  # 2.9.3: under the pointer, as the one shown
            p.setPen(qcolor(PALETTE.text, 1.0 if lit else PALETTE.text_muted_alpha))
            p.drawText(rect, Qt.AlignCenter, translate(TAB_LABELS[tab]))
            mark = self._marks.get(tab, "")
            if mark:  # 2.9.7: a dot at the name's top right
                color = PALETTE.live if mark == "rec" else PALETTE.warn
                size = MARK_U * u
                text_h = QFontMetrics(p.font()).capHeight()
                dot = QRectF(rect.right() + 0.25 * u, (height - text_h) / 2 - size / 2, size, size)
                fill_round(p, dot, qcolor(color), size / 2)
            if active:
                p.fillRect(
                    QRectF(rect.x(), height - 1.25 * u, rect.width(), 0.25 * u),
                    qcolor(PALETTE.accent),
                )
        p.setFont(units.font(1.1))
        pad = 1.4 * u
        for item, text in (("search", _search_label()), ("source", self._source_text())):
            if item == "source" and not self._source_visible:
                continue
            rect = rects[item]
            radius = 1.6 * u
            if self._focused and self._focus_item == item:
                self._paint_focus(p, rect, radius)
            else:
                if self._hover == item:  # 2.9.3: a faint ground under the pointer
                    fill_round(p, rect, qcolor(PALETTE.text, HOVER_ALPHA), radius)
                p.setPen(qcolor(PALETTE.text, 0.15))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
            active = item == self._active
            tone = qcolor(PALETTE.accent if active else PALETTE.text, 1.0 if active else 0.85)
            p.setPen(tone)
            inner = rect.adjusted(pad, 0, -pad, 0)
            if item == "search" and self._search_is_field(rect):
                self._paint_search_field(p, units, rect)
                continue
            if item == "search":  # the magnifier, then the word, centred together
                fm = p.fontMetrics()
                room = _icon_room(fm)
                left = inner.center().x() - (room + fm.horizontalAdvance(text)) / 2
                inner = draw_field_icon(p, QRectF(left, inner.y(), room, inner.height()), tone)
                inner.setWidth(fm.horizontalAdvance(text) + 2)
                p.setPen(tone)
                p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, text)
                continue
            p.drawText(inner, Qt.AlignCenter, elided(p, text, inner.width()))
        for name in ("refresh", "fullscreen"):
            rect = rects[name]
            if self._hover == name:
                fill_round(p, rect, qcolor(PALETTE.text, HOVER_ALPHA), 1.6 * u)
            p.setPen(qcolor(PALETTE.text, 0.15))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 1.6 * u, 1.6 * u)
            icon = rect.height() * 0.5
            box = QRectF(rect.center().x() - icon / 2, rect.center().y() - icon / 2, icon, icon)
            if name == "refresh":
                draw_refresh(p, box, qcolor(PALETTE.text, 0.85))
            else:
                draw_icon(p, "fullscreen", box, qcolor(PALETTE.text, 0.85))
        p.end()
