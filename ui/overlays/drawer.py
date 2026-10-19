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

The find: typing (or a click on the ⌕ Find box above the right column) finds genres and
channels at every provider, whichever column had the focus. The right column then lists the
genres found (each with its provider's name when there are two or more, and its channel
count), the channels found, and a last row that looks for films and series in the Library. A
genre opens as if walked to (`NavModel.open_found_group`), a channel plays (`channelFound`),
the last row asks for the Library's search (`libraryFindRequested`). Esc, ✕ or ← end the find:
the right column is back where it was, its highlight and its scroll position too.

While the multiview's grid shows (`set_drag_enabled`), a channel row can be dragged out onto
a tile: once the pointer moved a drag distance with the button down, the list fades (the
tiles behind it show), the pointer's moves come as `channelDragged`, and the release as
`channelDropped` (never a click on the row); a drawer hidden meanwhile ends it
(`dragEnded`).
"""

from collections.abc import Callable, Sequence
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
    QFontMetricsF,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QGraphicsOpacityEffect,
    QStyle,
    QStyledItemDelegate,
    QToolTip,
)

from services.find import FoundChannel, FoundGenre
from services.navigation import (
    ADD_PROVIDER_ROW,
    FAVORITES_GROUP,
    FREE_CHANNELS_ROW,
    LIBRARY_SECTIONS,
    OPEN_FILE_ROW,
    RECENT_GROUP,
    SETTINGS_ROW,
    NavModel,
    NavNode,
    NodeSeq,
    item_key,
    item_title,
)
from services.provider_ids import mixed_group_id, provider_of
from services.zapping import ZapContext
from ui.find_session import FindSession
from ui.keymap import key_hint
from ui.overlays.base import Overlay
from ui.overlays.channel_rows import (
    ChannelListView,
    ChannelRowDelegate,
    ChannelRowsModel,
    LazyNumbers,
    column_focused,
)
from ui.overlays.painting import Units, draw_icon, elided, fill_round
from ui.pointer import NAV_BUTTONS
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, qcolor

logger = logging.getLogger(__name__)

_CONTEXT_LAYERS = ("item", "episode")  # rows with a context menu (channels are items)
_RIGHT_PRESSES = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
DRAG_OPACITY = 0.25  # the list while a channel is dragged out of it onto the grid
LEFT_FRACTION = 0.25
RIGHT_FRACTION = 0.58
FADE_U = 7.0
PAD_TOP_U = 2.4
PAD_SIDE_U = 1.6
CRUMB_H_U = 2.2
FOUND_GENRE_H_U = 5.0  # a genre the find found: its name, then its provider and count
FOUND_CHANNEL_SCALE = 0.7  # its channels: a long name (and a 5-digit number) fits whole
FILTER_H_U = 3.4  # the ⌕ Find box above the right column (the left column's room stays empty)
FIND_X_U = 2.2  # its ✕, this wide at its right end
GEAR_U = 1.9  # the header row's buttons (Search, refresh, Settings) at its right end, this wide
GEAR_GAP_U = 0.7
MIN_HIT_PX = 28  # the header's buttons take clicks over this square at least
REFRESH_ACTION = "refresh"  # the header's refresh button: the provider's channels and guide
SEARCH_ACTION = "search"  # the header's ⌕: the Library's search
BACK_BUTTON = "back"  # the header's ‹: a layer up
_CRUMB = "crumb:"  # a name in the header, by its path index ("crumb:up": the elided head)
SEPARATOR = " › "
BACK_TOOLTIP = key_hint("Back", "Left")  # "Back (←)"
PROVIDER_TOOLTIP = "Choose another provider"
CRUMB_FONT_U = 1.15
PREVIEW_DEBOUNCE_MS = 200
LOADING_TEXT = "Loading…"
EMPTY_TEXT = "Nothing here"
ERROR_TEXT = "Couldn't load — press Enter to retry"
PROVIDER_ERROR_TEXT = "Couldn't connect — press Enter to retry"
FIND_ACTION = "find"  # the ⌕ Find box above the right column
FIND_CLEAR = "find_clear"  # its ✕
FIND_PLACEHOLDER = "Find a genre or channel"
FIND_TOOLTIP = "Find a genre or a channel at every provider: type its name"
FIND_CLEAR_TOOLTIP = key_hint("Clear the find", "Esc")
SEARCHING_TEXT = "Searching…"
FIND_FAILED_TEXT = "Couldn't search — type again to retry"
# The find's last row: the words in the box, for the Library's search (whole at 640 px wide).
FIND_LIBRARY_TEXT = "Search films and series ›"
FOUND_GENRE = "found_genre"  # a row of the find: a genre found (any provider's)
FOUND_LIBRARY = "found_library"  # its last row: films and series in the Library's search
FIND_PARENT = NavNode("find", "find", "Find")  # the right column's parent while it finds

NodeRole = Qt.UserRole + 10
CountRole = Qt.UserRole + 11
IconRole = Qt.UserRole + 12
_PINNED_ICONS = {FAVORITES_GROUP: "star", RECENT_GROUP: "recent"}
_ACTION_ICONS = {
    SETTINGS_ROW: "gear",
    ADD_PROVIDER_ROW: "plus",
    FREE_CHANNELS_ROW: "plus",
    OPEN_FILE_ROW: "file",
}
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


@dataclass(frozen=True)
class _Place:
    """Where a column's list was before a refresh: a list that moves to the other column (a
    layer down or up) keeps its rows at the same height there, and its highlight."""

    parent: NavNode | None
    current: NavNode | None
    top: int  # the list's first row, in drawer coordinates (above the column when scrolled)


def _crumb_key(index: int | None) -> str:
    return f"{_CRUMB}{'up' if index is None else index}"


def _leaves_drawer(node: NavNode) -> bool:
    """An action row, or a section the Library shows: chosen, it is handed to the shell."""
    return node.layer == "action" or (node.layer == "section" and node.key in LIBRARY_SECTIONS)


def _is_focus_key(node: NavNode, wanted: "str | tuple[str, str]") -> bool:
    """Whether `node` is the row a focus key names: an id, or (provider, id) for a channel
    that may share its id with another provider's."""
    if isinstance(wanted, tuple):
        provider, key = wanted
        payload = node.payload
        return node.key == key and (
            not provider or (isinstance(payload, dict) and provider_of(payload) == provider)
        )
    return node.key == wanted


def _selectable(node: NavNode) -> bool:
    """Whether a row can hold the highlight: a provider's heading (or its notice) cannot."""
    return node.layer not in ("header", "notice")


def _is_live_item(nav: NavModel, node: NavNode) -> bool:
    return node.layer == "item" and nav.section_of(node) == "live"


class _SeqNumbers(LazyNumbers):
    """The numbers of a long group's rows, looked up row by row (see _numbers)."""

    def __init__(self, group: NodeSeq) -> None:
        self._group = group
        self._own = group.numbered()
        # Else positions stand in: the last row's is the longest.
        self.digits = group.number_digits() if self._own else len(str(len(group)))
        self.signature = (group.token,)

    def __len__(self) -> int:
        return len(self._group)

    def __getitem__(self, row: int) -> str | None:
        if self._own:
            return ZapContext.number_of(self._group.items[row]) or None
        return str(row + 1)


def _numbers(group):
    """The numbers of a live group's rows.

    When no channel of the group has a number, its position in the group stands in, as on
    the banner and for number entry.
    """
    if isinstance(group, NodeSeq):
        return _SeqNumbers(group)
    channels = [n.payload for n in group if isinstance(n.payload, dict)]
    if any(ZapContext.number_of(c) for c in channels):
        return [
            (ZapContext.number_of(n.payload) or None) if isinstance(n.payload, dict) else None
            for n in group
        ]
    return [str(row + 1) for row in range(len(group))]


def _shown_rows(
    nodes,
    live: bool,
    numbers,
    tagged: bool = False,
) -> tuple:
    """What a column's list shows for `nodes`: equal values show the same rows.

    A row shows its node's title, count and icon, and a live channel its item (by identity:
    a refreshed catalog brings new item dicts) and number. A genre's payload is told by its
    value: the catalog makes its Group anew at each catalog event and at each zap.
    """
    if isinstance(nodes, NodeSeq):  # a long list is told by its identity, not row by row
        return (live, ("rows", nodes.token), getattr(numbers, "signature", None), tagged)
    rows = tuple(
        (
            n.layer,
            n.key,
            n.title,
            n.count,
            id(n.payload) if isinstance(n.payload, dict) else n.payload,
        )
        for n in nodes
    )
    return (live, rows, tuple(numbers) if numbers is not None else None, tagged)


class _NodeModel(ChannelRowsModel):
    """Channel rows for live items; titles, counts and icons for everything else."""

    def __init__(self, logos, now_provider, parent=None) -> None:
        super().__init__(logos, now_provider, parent)
        self._nodes: Sequence[NavNode] = []
        self._labels = False  # a found genre names its provider (two providers or more)
        self.found_kinds: list[str] = []  # each of the find's rows: _FoundRowDelegate's kind

    def set_found(self, nodes: list[NavNode], labels: bool) -> None:
        """The find's rows: headings, genres, channels (each its provider's tag when
        `labels`), and the Library row."""
        self._nodes = list(nodes)
        self._labels = labels
        rows = [n.payload if n.layer == "item" else {"name": n.title} for n in nodes]
        self.found_kinds = [_found_kind(n) for n in nodes]
        numbers = [
            (ZapContext.number_of(n.payload) or None) if n.layer == "item" else None for n in nodes
        ]
        self.set_channels(rows, numbers, labels)

    def set_nodes(
        self,
        nodes,
        live: bool,
        numbers=None,
        tagged: bool = False,
    ) -> None:
        self._nodes = nodes if isinstance(nodes, NodeSeq) else list(nodes)
        if live and isinstance(nodes, NodeSeq):
            rows = nodes.items  # a long list shows the dicts themselves
        else:
            rows = [
                n.payload if live and isinstance(n.payload, dict) else {"name": n.title}
                for n in nodes
            ]
        self.set_channels(rows, numbers if live else [None] * len(rows), tagged and live)

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if index.isValid() and 0 <= row < len(self._nodes):
            node = self._nodes[row]
            if role == Qt.DisplayRole:
                return node.title
            if role == NodeRole:
                return node
            if role == CountRole:
                if isinstance(node.payload, FoundGenre):
                    return _genre_label(node.payload, self._labels)
                return node.count
            if role == IconRole:
                if node.layer == FOUND_LIBRARY:
                    return "search"
                icons = {"group": _PINNED_ICONS, "action": _ACTION_ICONS}.get(node.layer, {})
                return icons.get(node.key)
        return super().data(index, role)


def _genre_label(genre: FoundGenre, labels: bool) -> str | None:
    """What a found genre says beside its title: "Demo TV · 6 channels" ("6 channels" with
    one provider; no count for a genre an STB portal has not listed yet)."""
    parts = [genre.provider] if labels else []
    if genre.count is not None:
        parts.append(f"{genre.count:,} channel" + ("" if genre.count == 1 else "s"))
    return " · ".join(parts) or None


def _found_kind(node: NavNode) -> str:
    if node.layer == "item":
        return "channel"
    return "genre" if isinstance(node.payload, FoundGenre) else "title"


class _FoundRowDelegate(QStyledItemDelegate):
    """The find's rows: channels as the channel column paints them (the provider's name on
    their second line), genres on two lines, the rest as titles.

    The rows are of three heights, so the list asks each row's: one per kind and width is kept.
    """

    def __init__(
        self,
        title: QStyledItemDelegate,
        channel: QStyledItemDelegate,
        genre: QStyledItemDelegate,
        parent,
    ) -> None:
        super().__init__(parent)
        self._delegates = {"title": title, "channel": channel, "genre": genre}
        self._sizes: dict[tuple[str, int], QSize] = {}

    def _kind(self, index) -> str:
        kinds = getattr(index.model(), "found_kinds", ())
        row = index.row()
        return kinds[row] if 0 <= row < len(kinds) else "title"

    def sizeHint(self, option, index) -> QSize:
        kind = self._kind(index)
        key = (kind, getattr(option.widget, "units_width", 0))
        size = self._sizes.get(key)
        if size is None:
            size = self._sizes[key] = self._delegates[kind].sizeHint(option, index)
        return size

    def paint(self, p: QPainter, option, index) -> None:
        self._delegates[self._kind(index)].paint(p, option, index)


def _paint_highlight(p: QPainter, rect: QRectF, u: float, focused: bool) -> None:
    """A row's highlight: the accent, stronger in the focused column."""
    box = rect.adjusted(0.3 * u, 0.2 * u, -0.3 * u, -0.2 * u)
    fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA if focused else 0.1), 1.0 * u)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(qcolor(PALETTE.accent, 0.7 if focused else 0.35))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 1.0 * u, 1.0 * u)


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
        node = index.data(NodeRole)
        p.save()
        if isinstance(node, NavNode) and node.layer == "header":
            # A provider's heading: small, quiet, with a rule; it is never highlighted.
            p.setFont(units.font(1.1, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.text, 0.5))
            left = rect.x() + 1.2 * u
            width = max(0.0, rect.right() - 1.2 * u - left)
            p.drawText(
                QRectF(left, rect.y(), width, rect.height()),
                Qt.AlignLeft | Qt.AlignBottom,
                elided(p, str(index.data(Qt.DisplayRole) or "").upper(), width),
            )
            p.restore()
            return
        if isinstance(node, NavNode) and node.layer == "notice":
            # Under a provider's heading: it loads, or why it failed; never highlighted.
            p.setFont(units.font(1.2))
            p.setPen(qcolor(PALETTE.text, 0.5))
            left = rect.x() + 1.2 * u
            width = max(0.0, rect.right() - 1.2 * u - left)
            p.drawText(
                QRectF(left, rect.y(), width, rect.height()),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, str(index.data(Qt.DisplayRole) or ""), width),
            )
            p.restore()
            return
        if selected:
            _paint_highlight(p, rect, u, focused)
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


class _FoundGenreDelegate(_SimpleRowDelegate):
    """A genre the find found: its name across the row, then, smaller and grey, its provider
    and how many channels it has (CountRole), so that genres of one name at several providers,
    or with long names alike at their start, read apart."""

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, self._units(option.widget).px(FOUND_GENRE_H_U))

    def paint(self, p: QPainter, option, index) -> None:
        units = self._units(option.widget)
        u = units.u
        rect = QRectF(option.rect)
        p.save()
        if option.state & QStyle.State_Selected:
            _paint_highlight(p, rect, u, column_focused(option.widget))
        left, width = rect.x() + 1.2 * u, max(0.0, rect.width() - 2.4 * u)
        title_font, label_font = units.font(1.35), units.font(1.1)
        title_h = QFontMetricsF(title_font).height()
        label_h = QFontMetricsF(label_font).height()
        label = str(index.data(CountRole) or "")
        y = rect.y() + (rect.height() - title_h - (label_h if label else 0)) / 2
        p.setFont(title_font)
        p.setPen(qcolor(PALETTE.text))
        title = str(index.data(Qt.DisplayRole) or "")
        p.drawText(
            QRectF(left, y, width, title_h), Qt.AlignLeft | Qt.AlignVCenter, elided(p, title, width)
        )
        if label:
            p.setFont(label_font)
            p.setPen(qcolor(PALETTE.text, 0.5))
            p.drawText(
                QRectF(left, y + title_h, width, label_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                elided(p, label, width),
            )
        p.restore()


class _RowIndex:
    """Each node's row in a column; a long list of channels makes its nodes on demand."""

    def __init__(self, nodes) -> None:
        self._seq = nodes if isinstance(nodes, NodeSeq) else None
        self._map = None if self._seq is not None else {n: i for i, n in enumerate(nodes)}

    def get(self, node, default=None):
        if node is None:
            return default
        row = self._seq.row_of(node) if self._seq is not None else self._map.get(node)  # type: ignore[union-attr]
        return default if row is None else row

    def __contains__(self, node) -> bool:
        return self.get(node) is not None

    def __getitem__(self, node) -> int:
        row = self.get(node)
        if row is None:
            raise KeyError(node)
        return row


class _Column:
    def __init__(self, side: str, drawer: "Drawer", logos, now_provider) -> None:
        self.side = side
        self.model = _NodeModel(logos, now_provider, drawer)
        self.view = ChannelListView(drawer)
        self.view.setModel(self.model)
        self.channel_delegate = ChannelRowDelegate(0.9, self.view)
        self.simple_delegate = _SimpleRowDelegate(self.view)
        self.found_delegate = _FoundRowDelegate(
            self.simple_delegate,
            ChannelRowDelegate(FOUND_CHANNEL_SCALE, self.view, provider_below=True),
            _FoundGenreDelegate(self.view),
            self.view,
        )
        self.view.setItemDelegate(self.simple_delegate)
        self.view.rowClicked.connect(lambda row: drawer._on_row_clicked(side, row))
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(lambda pos: drawer._on_row_context(side, pos))
        self.view.viewport().installEventFilter(drawer)  # a right press must not select
        self.parent: NavNode | None = None
        self.nodes: Sequence[NavNode] = []
        self.rows = _RowIndex([])
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
    # A channel the find found (stamped with its provider), and the genre it is first listed in.
    channelFound = Signal(object, str)
    libraryFindRequested = Signal(str)  # the find's text, for the Library's search
    closed = Signal()
    # A channel row dragged out (the multiview): the node and the pointer (global), as it
    # moves, where it is let go; dragEnded once it is over, dropped or not.
    channelDragged = Signal(object, QPoint)
    channelDropped = Signal(object, QPoint)
    dragEnded = Signal()

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
        self.find_text = ""
        self._find_open = False  # the ⌕ Find box was clicked (or typed into): it shows a caret
        self._find_moved = False  # ↑/↓ in the find: its highlight no longer follows the results
        self._finder: FindSession | None = None  # (set_finder) without one nothing is found
        self._before_find: tuple[_Place, str] | None = None  # the right column, the focus
        self._restore: _Place | None = None  # where the right column goes back to (_refresh)
        self._pending_key: str | tuple[str, str] | None = None
        self._left_choice: NavNode | None = None  # the left highlight, which may lead the path
        self._preview_node: NavNode | None = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._flush_preview)
        self._laid_out_for: tuple[int, int] | None = None  # the host size _relayout last fitted
        self._hover: str | None = None  # the header button under the pointer
        self._drag_enabled: Callable[[], bool] = lambda: False  # set_drag_enabled
        self._drag_from: tuple[str, int, QPoint] | None = None  # a press on a row (global)
        self._dragging: NavNode | None = None  # the channel being dragged out
        # Hover over the header buttons (a move elsewhere goes on to the host, as without it).
        self.setMouseTracking(True)
        nav.changed.connect(self._on_nav_changed)
        if parent is not None:
            parent.installEventFilter(self)
        self.hide()

    # ---- public API -------------------------------------------------------------------------

    def open_drawer(
        self, focus_key: str | tuple[str, str] | None = None, focus_side: str = "right"
    ) -> None:
        """Open on the NavModel's path, focusing the right column (or the left one)."""
        self.focus_side = focus_side
        self._drop_find()
        self._left_choice = None  # the left highlight starts at the end of the path
        self._pending_key = focus_key
        self._cancel_preview()
        self._columns["right"].parent = None  # force the highlight to be re-chosen
        self._relayout()
        self._refresh()
        self.show()
        self.raise_()

    def set_drag_enabled(self, enabled: Callable[[], bool]) -> None:
        """Whether a channel row may be dragged out now (the multiview's grid shows)."""
        self._drag_enabled = enabled

    def dragging(self) -> NavNode | None:
        """The channel being dragged out of the list, else None."""
        return self._dragging

    def close_drawer(self) -> None:
        self._flush_preview()
        was_visible = self.isVisible()
        self._drop_find()  # (its index goes after a while unused)
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
        self._find_moved = self.finding()
        row = col.rows.get(col.current, -1 if delta > 0 else len(col.nodes))  # type: ignore[arg-type]
        target = self._selectable_row(col, min(len(col.nodes) - 1, max(0, row + delta)), delta)
        if target is not None:
            self._select(col, target)

    @staticmethod
    def _selectable_row(col: _Column, row: int, delta: int) -> int | None:
        """`row`, or the nearest row that can hold the highlight: onward in the direction
        of `delta`, else back (Up/Down step over a provider's heading); None if there is none."""
        step = 1 if delta > 0 else -1
        for direction in (step, -step):
            at = row
            while 0 <= at < len(col.nodes):
                if _selectable(col.nodes[at]):
                    return at
                at += direction
        return None

    def page(self, delta: int) -> None:
        col = self._columns[self.focus_side]
        # (the find's rows are of several heights: a page of rows like the highlighted one)
        row_h = col.view.sizeHintForRow(col.rows.get(col.current, 0))
        rows = max(1, col.view.height() // row_h - 1) if row_h > 0 else 8
        self.move_by(delta * rows)

    def left(self) -> None:
        if self.finding():
            self._end_find()  # ← in the find: back to where it started
            return
        self._find_open = False  # an empty Find box closes: ← goes on as without it
        if self.focus_side == "right":
            self.focus_side = "left"
            self._left_choice = None
            self._refresh()
        else:
            self._cancel_preview()
            self._left_choice = None
            self._nav.up()

    def right(self) -> None:
        if self.finding():
            self.activate()
            return
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
        if self.finding():
            self._choose_found(node)
        elif self.focus_side == "right" and self._nav.is_playable(node):
            nodes = col.nodes if isinstance(col.nodes, NodeSeq) else list(col.nodes)
            self.playRequested.emit(node, nodes, self._nav.path[-1])
        else:
            self.right()

    def type_text(self, text: str) -> None:
        """Typing finds genres and channels, whichever column has the focus."""
        self._find_open = True
        self._set_find(self.find_text + text)

    def backspace(self) -> None:
        if self.find_text:
            if len(self.find_text) == 1:
                self._find_open = False  # nothing left to find: the box closes too
            self._set_find(self.find_text[:-1])

    def start_find(self) -> None:
        """A click on the ⌕ Find box: it shows a caret; typing finds."""
        self._find_open = True
        self.update()

    def clear_find(self) -> None:
        """✕ in the Find box: the find ends, the lists are back where they were."""
        self._end_find()

    def finding(self) -> bool:
        """Whether the right column lists what the find found."""
        return bool(self.find_text.strip())

    def set_finder(self, finder: FindSession) -> None:
        """The FindSession that finds the genres and channels (services.find)."""
        self._finder = finder
        finder.changed.connect(self._on_found)

    def go_up(self) -> None:
        """‹ (or the elided head): a layer up, as ← does with the focus on the left column."""
        self._end_find(refresh=False)
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
        self._end_find(refresh=False)
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
        if not self._end_find():
            self.close_drawer()
        return True

    # ---- the find -----------------------------------------------------------------------------

    def _set_find(self, text: str) -> None:
        was_finding = self.finding()
        self.find_text = text
        self._find_moved = False
        if self.finding() and not was_finding:
            right = self._columns["right"]
            self._before_find = (self._place(right), self.focus_side)
            self.focus_side = "right"  # the find lists in the right column
            self._cancel_preview()
        elif was_finding and not self.finding():
            self._back_from_find()
        if self._finder is not None:
            self._finder.set_query(text)  # its `changed` refreshes a find that shows
        if self.isVisible() and (self._finder is None or not self.finding()):
            self._refresh()

    def _back_from_find(self) -> None:
        """The right column goes back to where it was before the find, and the focus too."""
        if self._before_find is not None:
            self._restore, self.focus_side = self._before_find
            self._before_find = None

    def _end_find(self, *, refresh: bool = True) -> bool:
        """End the find (Esc, ✕, ←, the header): False when there was none."""
        if not self.find_text and not self._find_open:
            return False
        self._find_open = False
        if self.find_text:
            self._set_find("")
        elif refresh:
            self.update()
        return True

    def _drop_find(self) -> None:
        """A new opening: no find, nothing to go back to."""
        self.find_text = ""
        self._find_open = False
        self._before_find = self._restore = None
        if self._finder is not None:
            self._finder.clear()

    def _on_found(self) -> None:
        if self.isVisible() and self.finding():
            self._refresh()

    def _choose_found(self, node: NavNode) -> None:
        """Enter, → or a click on a row of the find."""
        if isinstance(node.payload, FoundGenre):
            genre = node.payload
            if not self._nav.open_found_group(genre.provider, genre.group_id):
                if self._finder is not None:
                    self._finder.refresh()  # its provider went since: the find stays, anew
                return
            self._find_open = False
            self.find_text = ""
            self._before_find = None  # it opened where it is: nothing to go back to
            if self._finder is not None:
                self._finder.clear()
            self.focus_side = "right"
            self._refresh()
        elif node.layer == "item" and isinstance(node.payload, dict):
            group = self._finder.group_of(node.payload) if self._finder is not None else ""
            self.channelFound.emit(node.payload, group)
        elif node.layer == FOUND_LIBRARY:
            self.libraryFindRequested.emit(self.find_text.strip())

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
        if not 0 <= row < len(col.nodes) or not _selectable(col.nodes[row]):
            return  # a heading does nothing
        if side == "left" and self.finding():
            self._end_find()  # a genre of the left column: the find gives way
        if side != self.focus_side:
            self.focus_side = side
            self._refresh()
        self._select(col, row)
        self.activate()

    def row_anchor(self, key: str) -> QPoint:
        """The global point under the sources column's row `key` (where its menu opens)."""
        col = self._columns["left"]
        node = next((n for n in col.nodes if n.key == key), None)
        view = col.view
        if node is None or node not in col.rows:
            return view.mapToGlobal(QPoint(0, 0))
        rect = view.visualRect(col.model.index(col.rows[node], 0))
        return view.viewport().mapToGlobal(rect.bottomLeft())

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
        left, right = self._columns["left"], self._columns["right"]
        was = {side: self._place(col) for side, col in self._columns.items()}
        # After a find, the right column's list comes back as it was (unless the path moved).
        restore, self._restore = self._restore, None
        back = restore if restore is not None and restore.parent == right_parent else None
        self._fill(left, left_parent, left_children, was["right"])
        if self.finding():
            self._fill_found(right)
        else:
            self._fill(right, right_parent, right_children, back or was["left"])
        for side, col in self._columns.items():
            col.view.setProperty("columnFocused", side == self.focus_side)
            col.view.viewport().update()
        self.update()

    @staticmethod
    def _place(col: _Column) -> _Place:
        return _Place(col.parent, col.current, col.view.y() - col.view.verticalScrollBar().value())

    def _fill(
        self,
        col: _Column,
        parent: NavNode | None,
        children: Sequence[NavNode] | None,
        other: _Place,
    ) -> None:
        """Show `parent`'s children in `col`; `other` is where the other column was before
        (or where this one was before a find)."""
        parent_changed = parent != col.parent
        # The other column's list comes over (a layer down: the genres go left; up: right).
        moved = parent_changed and parent is not None and parent == other.parent
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
        if col.message is None and not nodes:
            col.message = EMPTY_TEXT
        live = bool(nodes) and _is_live_item(self._nav, nodes[0])
        numbers = _numbers(nodes) if live else None
        # The same rows again (e.g. the section reported ready once more): the list keeps its
        # model, so it is neither laid out nor repainted from scratch.
        tagged = live and self._nav.shows_provider(parent)
        shown = _shown_rows(nodes, live, numbers, tagged)
        if shown != col.shown:
            col.shown = shown
            col.nodes = nodes
            col.rows = _RowIndex(nodes)
            col.view.setUniformItemSizes(True)  # (a find's rows are of two heights)
            col.view.setItemDelegate(col.channel_delegate if live else col.simple_delegate)
            col.model.set_nodes(nodes, live, numbers, tagged)
            if moved:
                col.view.executeDelayedItemsLayout()  # the rows' range, before it is scrolled
                col.view.verticalScrollBar().setValue(col.view.y() - other.top)
        else:
            col.model.refresh_now()  # a refresh still looks "now" up again (the EPG loaded)
        if col.side == "left":
            if parent_changed:
                self._cancel_preview()  # a preview of the old layer must not open
            self._choose_left(col, nodes)
        else:
            matched = False
            if self._pending_key is not None:
                pending = self._pending_key
                match = (
                    nodes.find_focus(pending)
                    if isinstance(nodes, NodeSeq)
                    else next((n for n in nodes if _is_focus_key(n, pending)), None)
                )
                if match is not None:
                    col.current = match
                    matched = True
            if not matched and moved and other.current in col.rows:
                col.current = other.current  # a layer up: the row it was on stays highlighted
                matched = True
            if not matched and (parent_changed or col.current not in col.rows):
                col.current = next((n for n in nodes if _selectable(n)), None)
            if children is not None:
                self._pending_key = None  # a stale focus key must not outlive the first fill
        self._show_current(col)

    def _choose_left(self, col: _Column, nodes: Sequence[NavNode]) -> None:
        path = self._nav.path
        end = path[-1] if path else None
        if self._left_choice is not None and self._left_choice not in col.rows:
            # Gone from the list: Enter must choose what is highlighted now, not that row.
            self._left_choice = None
            self._cancel_preview()
        if self._left_choice in col.rows:
            col.current = self._left_choice
        elif end in col.rows:
            col.current = end
        else:
            col.current = None

    def _fill_found(self, col: _Column) -> None:
        """The right column lists what the find found: genres, channels, the Library row."""
        finder = self._finder
        genres: list[FoundGenre] = finder.genres if finder is not None else []
        channels: list[FoundChannel] = finder.channels if finder is not None else []
        searching = finder is not None and finder.searching
        text = self.find_text.strip()
        labels = len(self._nav.catalog.provider_names()) >= 2  # with one provider, as in 2.0
        nodes: list[NavNode] = []
        if genres:
            nodes.append(NavNode("header", "find:genres", "Genres"))
            nodes += [
                NavNode(FOUND_GENRE, mixed_group_id(g.provider, g.group_id), g.title, g)
                for g in genres
            ]
        if channels:
            nodes.append(NavNode("header", "find:channels", "Channels"))
            nodes += [
                NavNode("item", item_key(h.item), item_title(h.item), h.item) for h in channels
            ]
        if not genres and not channels:
            if searching:
                notice = SEARCHING_TEXT
            elif finder is not None and finder.failed:
                notice = FIND_FAILED_TEXT
            else:
                notice = f"No genre or channel has “{text}”"
            nodes.append(NavNode("notice", "find:notice", notice))
        nodes.append(NavNode(FOUND_LIBRARY, "find:library", FIND_LIBRARY_TEXT))
        col.parent = FIND_PARENT
        col.message = None
        shown = ("find", _shown_rows(nodes, False, None, labels))
        if shown != col.shown:
            col.shown = shown
            col.nodes = nodes
            col.rows = _RowIndex(nodes)
            col.view.setUniformItemSizes(False)
            col.view.setItemDelegate(col.found_delegate)
            col.model.set_found(nodes, labels)
        else:
            col.model.refresh_now()
        if not self._find_moved or col.current not in col.rows:
            col.current = next((n for n in nodes if _selectable(n)), None)  # the best match
        self._show_current(col)

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
            elif button == FIND_ACTION:
                self.start_find()
            elif button == FIND_CLEAR:
                self.clear_find()
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
        point = event.position()
        if event.button() == Qt.LeftButton and self._header_button_at(point) == BACK_BUTTON:
            self.go_up()  # as the guide's ‹ ›: a quick second click goes up again
            self._hover_at(point)

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
        """What hovering `point` says: a header button's name and key, ‹, the provider, or the
        Find box and its ✕."""
        button = self._header_button_at(point)
        if button in _HEADER_TOOLTIPS:
            return _HEADER_TOOLTIPS[button]
        if button == FIND_ACTION:
            return FIND_TOOLTIP
        if button == FIND_CLEAR:
            return FIND_CLEAR_TOOLTIP
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
        """What of the header row or of the Find box is at `point`: a button, a name
        (_crumb_key), the box (FIND_ACTION) or its ✕ (FIND_CLEAR), or None."""
        if self.find_clear_rect().contains(point):
            return FIND_CLEAR
        if self.find_rect().contains(point):
            return FIND_ACTION
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

    def find_rect(self) -> QRectF:
        """The ⌕ Find box above the right column (drawer coordinates)."""
        u = self._units.u
        col = self._columns["right"]
        side = self._units.px(PAD_SIDE_U)
        return QRectF(
            col.rect.x() + side, PAD_TOP_U * u, col.rect.width() - 2 * side, FILTER_H_U * u
        ).adjusted(0.5, 0.5, -0.5, -0.5)

    def find_clear_rect(self) -> QRectF:
        """The Find box's ✕ (where it takes clicks); empty when there is nothing to clear."""
        if not self.find_text and not self._find_open:
            return QRectF()
        box = self.find_rect()
        width = min(box.width(), FIND_X_U * self._units.u)
        return QRectF(box.right() - width, box.y(), width, box.height())

    # ---- layout and painting ----------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._relayout()
        elif isinstance(event, QMouseEvent) and self._drag_event(obj, event):
            return True  # a channel row dragged out: neither a selection nor a click
        elif (
            event.type() in _RIGHT_PRESSES
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.RightButton
            and any(obj is col.view.viewport() for col in self._columns.values())
        ):
            return True  # not a selection: the context menu follows, the highlight stays
        return False

    def _drag_event(self, obj: QObject, event: QMouseEvent) -> bool:
        """A list's mouse event, as a drag of a channel row out of the list; True when the
        event is the drag's."""
        side = next((s for s, c in self._columns.items() if obj is c.view.viewport()), None)
        if side is None:
            return False
        kind, left = event.type(), Qt.MouseButton.LeftButton
        where = event.globalPosition().toPoint()
        if kind == QEvent.Type.MouseButtonPress and event.button() == left:
            row = self._columns[side].view.indexAt(event.position().toPoint()).row()
            self._drag_from = (side, row, where)
            return False  # the list selects the row, as ever
        if kind == QEvent.Type.MouseMove and event.buttons() & left:
            if self._dragging is not None:
                self.channelDragged.emit(self._dragging, where)
                return True
            node = self._drag_node(where)
            if node is None:
                return False
            self._dragging = node
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(DRAG_OPACITY)  # the tiles behind the list show
            self.setGraphicsEffect(effect)
            self.channelDragged.emit(node, where)
            return True
        if kind == QEvent.Type.MouseButtonRelease and event.button() == left:
            self._drag_from = None
            node = self._dragging
            if node is None:
                return False
            self._end_drag()
            self.channelDropped.emit(node, where)
            return True
        return False

    def _drag_node(self, where: QPoint) -> NavNode | None:
        """The channel a press began on, once the pointer moved a drag distance (and the
        grid shows); else None."""
        start = self._drag_from
        if start is None or not self._drag_enabled():
            return None
        side, row, origin = start
        if (where - origin).manhattanLength() < QApplication.startDragDistance():
            return None
        nodes = self._columns[side].nodes
        node = nodes[row] if 0 <= row < len(nodes) else None
        live = self.finding() or (node is not None and self._nav.section_of(node) == "live")
        if node is None or node.layer != "item" or not isinstance(node.payload, dict) or not live:
            self._drag_from = None  # not a channel's row: a plain press and move
            return None
        return node

    def _end_drag(self) -> None:
        if self._dragging is None:
            return
        self._dragging = None
        self._drag_from = None
        self.setGraphicsEffect(None)  # type: ignore[arg-type]  # None removes it (Qt's API)
        self.dragEnded.emit()

    def hideEvent(self, event) -> None:
        self._end_drag()  # hidden mid-drag: no release comes to it
        super().hideEvent(event)

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
        self._paint_find(p, units)
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

    def _paint_find(self, p: QPainter, units: Units) -> None:
        """The ⌕ Find box: a prompt; once clicked or typed into, the text, a caret and ✕."""
        u = units.u
        box = self.find_rect()
        active = bool(self.find_text) or self._find_open
        fill_round(p, box, qcolor(PALETTE.canvas, 0.6 if active else 0.35), 0.9 * u)
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        hovered = self._hover == FIND_ACTION
        p.setPen(
            qcolor(PALETTE.accent, 0.7)
            if active
            else qcolor(PALETTE.text, 0.3 if hovered else 0.15)
        )
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(box, 0.9 * u, 0.9 * u)
        p.restore()
        size = 1.5 * u
        draw_icon(
            p,
            "search",
            QRectF(box.x() + 1.0 * u, box.center().y() - size / 2, size, size),
            qcolor(PALETTE.text, 0.8 if active else 0.5),
        )
        clear = self.find_clear_rect()
        right = (clear.left() if not clear.isEmpty() else box.right()) - 0.6 * u
        inner = QRectF(box.x() + 1.0 * u + size + 0.7 * u, box.y(), 0, box.height())
        inner.setRight(max(inner.left(), right))
        p.setFont(units.font(1.35))
        if active:
            p.setPen(qcolor(PALETTE.text))
            text = elided(p, self.find_text + "▏", inner.width())
        else:
            p.setPen(qcolor(PALETTE.text, 0.5))
            text = elided(p, FIND_PLACEHOLDER, inner.width())
        p.drawText(inner, Qt.AlignLeft | Qt.AlignVCenter, text)
        if not clear.isEmpty():
            mark = 1.1 * u
            tone = (
                qcolor(PALETTE.accent) if self._hover == FIND_CLEAR else qcolor(PALETTE.text, 0.6)
            )
            draw_icon(
                p,
                "close",
                QRectF(clear.center().x() - mark / 2, clear.center().y() - mark / 2, mark, mark),
                tone,
            )

    def _paint_column(self, p: QPainter, col: _Column, units: Units) -> None:
        u = units.u
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
