"""Welcome card: shown while no provider is configured (spec §8), centred over the video.

The QiTV logo sits above the title. Its controls are the accent Add provider button, focused:
a click on it, or Enter (OK on a remote, routed by the TV shell), emits `addRequested` and the
window opens provider setup; and the plain Free channels… button beside it, whose click emits
`freeRequested` with the global point under it, where the window pops up its menu.
1 u = 1% of the overlay host's width.
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

from services.i18n import N_, translate
from ui.brand import logo_aspect, logo_available, logo_pixmap
from ui.overlays.base import Overlay
from ui.overlays.painting import Units, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, RADIUS_PANEL, qcolor

# English (tests read them); translate() shows them.
TITLE_TEXT = N_("Welcome to QiTV")
BODY_TEXT = N_("Add your IPTV provider (STB portal, Xtream or M3U playlist) to start watching.")
BUTTON_TEXT = N_("Add provider")  # i18n: the welcome card's main button
FREE_TEXT = N_("Free channels…")  # i18n: the welcome card's second button; opens a menu
WIDTH_U = 44.0
TITLE_U = 2.4
BODY_U = 1.2
BODY_OPACITY = 0.7
PAD_U = 3.0
LOGO_U = 5.0  # the logo's height, above the title
LOGO_GAP_U = 1.6
GAP_U = 1.4
BUTTON_GAP_U = 2.4
BUTTON_H_U = 4.0
BUTTON_MIN_PX = 28  # a target a pointer can hit at small window widths
BUTTONS_GAP_U = 1.6  # between the two buttons
BUTTON_PAD_U = 2.4
BUTTON_FONT_U = 1.3
RING_GAP_U = 0.4  # between the button and its focus ring
FOCUS_RING_PX = 1.5
DEFAULT_HOST_WIDTH = 1280
_TEXT_FLAGS = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap


class WelcomeCard(Overlay):
    addRequested = Signal()
    freeRequested = Signal(QPoint)  # the global point under the Free channels… button

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def show_card(self) -> None:
        self._layout()
        self.show()
        self.raise_()
        self.update()

    def dismiss(self) -> None:
        if not self.isHidden():
            self.hide()

    def activate(self) -> bool:
        """Enter on the focused button: True when the card was shown and asked."""
        if self.isHidden():
            return False
        self.addRequested.emit()
        return True

    def logo_rect(self) -> QRectF:
        """The logo, centred above the title (empty when the asset is missing)."""
        units = Units(self._host_width())
        if not logo_available():
            return QRectF(self.width() / 2, units.px(PAD_U), 0, 0)
        height = units.px(LOGO_U)
        width = round(height * logo_aspect())
        return QRectF(round((self.width() - width) / 2), units.px(PAD_U), width, height)

    def title_rect(self) -> QRectF:
        units = Units(self._host_width())
        pad = units.px(PAD_U)
        logo = self.logo_rect()
        top = logo.bottom() + (units.px(LOGO_GAP_U) if logo.height() else 0)
        title_h, _body_h = self._text_heights(units, self.width() - 2 * pad)
        return QRectF(pad, top, self.width() - 2 * pad, title_h)

    def button_rect(self) -> QRectF:
        """Add provider: the left button of the centred pair."""
        return self._buttons()[0]

    def free_button_rect(self) -> QRectF:
        """Free channels…: the right button, the same height."""
        return self._buttons()[1]

    def _button_h(self, units: Units) -> int:
        return max(BUTTON_MIN_PX, units.px(BUTTON_H_U))

    def _button_widths(self, units: Units) -> tuple[int, int]:
        metrics = QFontMetrics(self._button_font(units))
        pad = 2 * units.px(BUTTON_PAD_U)
        return (
            metrics.horizontalAdvance(translate(BUTTON_TEXT)) + pad,
            metrics.horizontalAdvance(translate(FREE_TEXT)) + pad,
        )

    def _buttons(self) -> tuple[QRectF, QRectF]:
        units = self._units()
        first, second = self._button_widths(units)
        gap = units.px(BUTTONS_GAP_U)
        height = self._button_h(units)
        top = self.height() - units.px(PAD_U) - height
        left = (self.width() - (first + gap + second)) / 2
        return QRectF(left, top, first, height), QRectF(left + first + gap, top, second, height)

    # ---- layout -----------------------------------------------------------------------------

    def _host_width(self) -> int:
        parent = self.parentWidget()
        return parent.width() if parent is not None else DEFAULT_HOST_WIDTH

    def _units(self) -> Units:
        return Units(self._host_width())

    @staticmethod
    def _title_font(units: Units) -> QFont:
        return units.font(TITLE_U, QFont.Weight.Bold)

    @staticmethod
    def _body_font(units: Units) -> QFont:
        return units.font(BODY_U)

    @staticmethod
    def _button_font(units: Units) -> QFont:
        return units.font(BUTTON_FONT_U, QFont.Weight.DemiBold)

    def _text_heights(self, units: Units, inner: int) -> tuple[int, int]:
        box = QRect(0, 0, max(1, inner), 10_000)
        title = QFontMetrics(self._title_font(units)).boundingRect(
            box, _TEXT_FLAGS, translate(TITLE_TEXT)
        )
        body = QFontMetrics(self._body_font(units)).boundingRect(
            box, _TEXT_FLAGS, translate(BODY_TEXT)
        )
        return title.height(), body.height()

    def _layout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        units = Units(parent.width())
        pad = units.px(PAD_U)
        first, second = self._button_widths(units)
        # Wide enough for both buttons (their text has a floor size on a small host).
        width = max(units.px(WIDTH_U), first + units.px(BUTTONS_GAP_U) + second + 2 * pad)
        title_h, body_h = self._text_heights(units, width - 2 * pad)
        logo_h = units.px(LOGO_U) + units.px(LOGO_GAP_U) if logo_available() else 0
        height = (
            pad
            + logo_h
            + title_h
            + units.px(GAP_U)
            + body_h
            + units.px(BUTTON_GAP_U)
            + self._button_h(units)
            + pad
        )
        self.setGeometry(
            (parent.width() - width) // 2, (parent.height() - height) // 2, width, height
        )

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            if not self.isHidden():
                self._layout()
        return False

    # ---- painting ---------------------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:
        units = self._units()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        card = QRectF(self.rect())
        radius = float(RADIUS_PANEL)
        fill_round(p, card, qcolor(PALETTE.panel_raised, 0.96), radius)
        p.setPen(qcolor(PALETTE.text, 0.12))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        pad = units.px(PAD_U)
        inner = self.width() - 2 * pad
        _title_h, body_h = self._text_heights(units, inner)
        logo_rect = self.logo_rect()
        ratio = self.devicePixelRatioF()
        logo = logo_pixmap(logo_rect.height(), ratio) if logo_rect.height() else None
        if logo is not None:
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(logo_rect.topLeft(), logo)
        title = self.title_rect()
        p.setFont(self._title_font(units))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(title, _TEXT_FLAGS, translate(TITLE_TEXT))
        p.setFont(self._body_font(units))
        p.setPen(qcolor(PALETTE.text, BODY_OPACITY))
        body_top = title.bottom() + units.px(GAP_U)
        p.drawText(QRectF(pad, body_top, inner, body_h), _TEXT_FLAGS, translate(BODY_TEXT))
        self._paint_button(p, units)
        self._paint_free_button(p, units)
        p.end()

    def _paint_button(self, p: QPainter, units: Units) -> None:
        button = self.button_rect()
        # The focused control: the accent ring, with the 22% fill between it and the button.
        gap = units.u * RING_GAP_U
        ring = button.adjusted(-gap, -gap, gap, gap)
        ring_radius = RADIUS_CARD + gap
        fill_round(p, ring, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), ring_radius)
        fill_round(p, button, qcolor(PALETTE.accent), float(RADIUS_CARD))
        p.setPen(QPen(qcolor(PALETTE.accent), FOCUS_RING_PX))
        p.setBrush(Qt.NoBrush)
        half = FOCUS_RING_PX / 2
        p.drawRoundedRect(ring.adjusted(half, half, -half, -half), ring_radius, ring_radius)
        p.setFont(self._button_font(units))
        p.setPen(qcolor(PALETTE.canvas))
        p.drawText(button, Qt.AlignCenter, translate(BUTTON_TEXT))

    def _paint_free_button(self, p: QPainter, units: Units) -> None:
        """The secondary button: a quiet fill and outline, the text colour."""
        button = self.free_button_rect()
        fill_round(p, button, qcolor(PALETTE.text, 0.1), float(RADIUS_CARD))
        p.setPen(QPen(qcolor(PALETTE.text, 0.22), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(button.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS_CARD, RADIUS_CARD)
        p.setFont(self._button_font(units))
        p.setPen(qcolor(PALETTE.text))
        p.drawText(button, Qt.AlignCenter, translate(FREE_TEXT))

    # ---- input: consumed here, so nothing reaches the video behind -------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in NAV_BUTTONS:
            event.ignore()  # Back/Forward go on to the host
            return
        event.accept()
        if event.button() != Qt.LeftButton:
            return
        if self.button_rect().contains(event.position()):
            self.activate()
        elif self.free_button_rect().contains(event.position()):
            below = self.free_button_rect().bottomLeft().toPoint()
            self.freeRequested.emit(self.mapToGlobal(below))

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.accept()  # never the video's fullscreen toggle

    def wheelEvent(self, event) -> None:
        event.accept()  # never a zap behind
