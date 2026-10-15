"""The Library's series view: header, season chips and a virtualized episodes list (spec §5.3).

Units: 1 u = 1% of the Library screen's width (this widget's width). Everything is painted;
the episodes list is a model/view that only paints the visible rows. The widget never takes
keyboard focus: the Library controller routes keys to ``move_by``/``activate``/``escape``/
``page``.

The poster is 14u wide at 2:3 (21u tall), but is capped to the header height minus 2u so it
never overflows the 34% header: at 16:9 it comes out slightly narrower than 14u.
"""

from collections.abc import Callable
import logging
import re

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QWidget

from services.library import Details
from services.navigation import episode_key, item_key
from ui.library.poster_grid import poster_url
from ui.overlays.channel_rows import ChannelListView
from ui.overlays.painting import Units, draw_logo, draw_progress, elided, fill_round
from ui.theme import FOCUS_FILL_ALPHA, PALETTE, RADIUS_CARD, qcolor

logger = logging.getLogger(__name__)

HEADER_FRACTION = 0.34
POSTER_W_U = 14.0
MARGIN_U = 3.0
TITLE_U = 2.6
META_U = 1.2
PLOT_U = 1.15
PLOT_LINES = 3
CHIP_H_U = 3.4
CHIP_FONT_U = 1.1
CHIP_PAD_U = 1.3
CHIP_GAP_U = 0.8
ROW_H_U = 5.0
SEASON_DEBOUNCE_MS = 250
WATCHED_AT = 0.9
LOADING_TEXT = "Loading…"
ERROR_TEXT = "Couldn't load — press Enter to retry"
EMPTY_SEASONS_TEXT = "No seasons"
EMPTY_EPISODES_TEXT = "No episodes"
START_OVER_TEXT = "↺ Start over"

SEASONS, EPISODES = "seasons", "episodes"

NumberRole = Qt.UserRole + 40
TitleRole = Qt.UserRole + 41
DurationRole = Qt.UserRole + 42
ProgressRole = Qt.UserRole + 43


def is_resumable(progress: float | None) -> bool:
    return progress is not None and 0 < progress < WATCHED_AT


def episode_number(episode: dict, row: int) -> str:
    for key in ("number", "episode_num", "series"):
        value = episode.get(key)
        if value not in (None, ""):
            return str(value)
    return str(row + 1)


def _minutes(value) -> int | None:
    """Minutes from STB minutes (number or numeric string) or Xtream "HH:MM:SS"/"MM:SS"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = re.fullmatch(r"(\d+)\s*(?:min|m)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    parts = text.split(":")
    if 2 <= len(parts) <= 3 and all(part.strip().isdigit() for part in parts):
        nums = [int(part) for part in parts]
        if len(nums) == 2:
            return nums[0]  # MM:SS
        return nums[0] * 60 + nums[1] + (1 if nums[2] >= 30 else 0)
    return None


def episode_duration(episode: dict) -> str:
    """ "N min", or "1h 05m" above an hour; "" when unknown."""
    sources = [episode]
    for key in ("info", "description"):
        if isinstance(episode.get(key), dict):
            sources.append(episode[key])
    minutes: int | None = None
    for source in sources:
        secs = source.get("duration_secs")
        if secs not in (None, "") and str(secs).isdigit() and int(secs):
            minutes = round(int(secs) / 60)
        else:
            minutes = _minutes(source.get("duration"))
        if minutes:
            break
    if not minutes or minutes <= 0:
        return ""
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60:02d}m"
    return f"{minutes} min"


class EpisodesModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[dict] = []
        self._provider: Callable[[dict], float | None] | None = None
        self._progress: dict[str, float | None] = {}

    def set_episodes(self, items: list[dict]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._progress.clear()
        self.endResetModel()

    def set_provider(self, provider: Callable[[dict], float | None] | None) -> None:
        self._provider = provider
        self.refresh_progress()

    def refresh_progress(self) -> None:
        self._progress.clear()
        if self._items:
            self.dataChanged.emit(self.index(0), self.index(len(self._items) - 1))

    def item(self, row: int) -> dict:
        return self._items[row]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def row_of(self, key: str) -> int:
        for row, item in enumerate(self._items):
            if episode_key(item) == key:
                return row
        return -1

    def progress(self, row: int) -> float | None:
        item = self._items[row]
        key = episode_key(item)
        if key not in self._progress:
            value: float | None = None
            if self._provider is not None:
                try:
                    value = self._provider(item)
                except Exception:
                    logger.exception("progress provider failed")
            self._progress[key] = value
        return self._progress[key]

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if not index.isValid() or not 0 <= row < len(self._items):
            return None
        item = self._items[row]
        if role == NumberRole:
            return episode_number(item, row)
        if role in (TitleRole, Qt.DisplayRole):
            return str(item.get("ename") or item.get("name") or item.get("title") or "")
        if role == DurationRole:
            return episode_duration(item)
        if role == ProgressRole:
            return self.progress(row)
        return None


class EpisodeRowDelegate(QStyledItemDelegate):
    def _units(self, widget) -> Units:
        return Units(getattr(widget, "units_width", 100) or 100)

    def sizeHint(self, option, index) -> QSize:
        return QSize(0, self._units(option.widget).px(ROW_H_U))

    def paint(self, p: QPainter, option, index) -> None:
        units = self._units(option.widget)
        u = units.u
        rect = QRectF(option.rect)
        selected = bool(option.state & QStyle.State_Selected)
        active = bool(getattr(option.widget, "focus_active", False))
        p.save()
        if selected:
            box = rect.adjusted(0.3 * u, 0.25 * u, -0.3 * u, -0.25 * u)
            if active:
                fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), 1.0 * u)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(qcolor(PALETTE.accent))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 1.0 * u, 1.0 * u)
            else:
                fill_round(p, box, qcolor(PALETTE.text, 0.06), 1.0 * u)

        progress = index.data(ProgressRole)
        watched = progress is not None and progress >= WATCHED_AT
        resuming = is_resumable(progress)
        show_button = resuming and selected and active
        left = rect.x() + 1.8 * u
        right = rect.right() - 1.8 * u

        p.setFont(units.font(1.1, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.accent))
        p.drawText(
            QRectF(left, rect.y(), 4.5 * u, rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"E{index.data(NumberRole)}",
        )

        duration = index.data(DurationRole) or ""
        dur_w = 7.0 * u
        p.setFont(units.font(1.0))
        p.setPen(qcolor(PALETTE.text, 0.5))
        p.drawText(
            QRectF(right - dur_w, rect.y(), dur_w, rect.height()),
            Qt.AlignRight | Qt.AlignVCenter,
            duration,
        )
        check_w = 2.4 * u if watched else 0.0
        if watched:
            p.setFont(units.font(1.3, QFont.Weight.Bold))
            p.setPen(qcolor(PALETTE.ok))
            p.drawText(
                QRectF(right - dur_w - check_w, rect.y(), check_w, rect.height()),
                Qt.AlignCenter,
                "✓",
            )

        btn_w = 9.5 * u if show_button else 0.0
        if show_button:
            btn = QRectF(
                right - dur_w - check_w - btn_w,
                rect.center().y() - 1.5 * u,
                btn_w - 0.8 * u,
                3.0 * u,
            )
            if getattr(option.widget, "button_active", False):
                fill_round(p, btn, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), 1.5 * u)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(qcolor(PALETTE.accent))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(btn.adjusted(0.75, 0.75, -0.75, -0.75), 1.5 * u, 1.5 * u)
            else:
                fill_round(p, btn, qcolor(PALETTE.text, 0.12), 1.5 * u)
            p.setFont(units.font(1.0, QFont.Weight.DemiBold))
            p.setPen(qcolor(PALETTE.text))
            p.drawText(btn, Qt.AlignCenter, START_OVER_TEXT)

        title_x = left + 5.0 * u
        title_w = right - dur_w - check_w - btn_w - 1.0 * u - title_x
        p.setFont(units.font(1.2))
        p.setPen(qcolor(PALETTE.text))
        shift = -0.55 * u if resuming else 0.0
        p.drawText(
            QRectF(title_x, rect.y() + shift, title_w, rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            elided(p, str(index.data(TitleRole) or ""), title_w),
        )
        if resuming:
            bar = QRectF(title_x, rect.center().y() + 1.35 * u, title_w, 0.35 * u)
            draw_progress(p, bar, float(progress))
        p.restore()


class EpisodesView(ChannelListView):
    """The virtualized list; ``focus_active`` selects the accent focus style."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.focus_active = False
        self.button_active = False  # the focused row's "Start over" button has focus


class SeriesView(QWidget):
    seasonSelected = Signal(object)  # season dict (preview/load)
    episodeActivated = Signal(object, object)  # (season, episode)
    startOverRequested = Signal(object, object)
    retryRequested = Signal()
    backRequested = Signal()  # Esc / ← from the seasons row's first chip

    def __init__(self, posters, parent=None) -> None:
        super().__init__(parent)
        self._posters = posters
        self._series: dict = {}
        self._details: Details | None = None
        self._seasons: list[dict] | None = None
        self._seasons_error: str | None = None
        self._season: dict | None = None
        self._season_idx = 0
        self._episodes: list[dict] | None = None
        self._episodes_error: str | None = None
        self._zone = SEASONS
        self._button = False  # focus is on the current row's Start over button
        self._moved = False  # the user chose a zone; don't pull focus back to the episodes
        self._poster_url = ""
        self._plot_lines_cache: tuple[int, str, list[str]] | None = None

        self._model = EpisodesModel(self)
        self._delegate = EpisodeRowDelegate(self)
        self._view = EpisodesView(self)
        self._view.setModel(self._model)
        self._view.setItemDelegate(self._delegate)
        self._view.rowClicked.connect(self._on_row_clicked)
        self._view.hide()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEASON_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._emit_season)

        if posters is not None:
            posters.logoReady.connect(self._on_poster_ready)
        self._layout()

    # ---- data ---------------------------------------------------------------------------
    def set_series(self, series: dict, details: Details) -> None:
        self._series = series
        self._details = details
        self._seasons = None
        self._seasons_error = None
        self._season = None
        self._season_idx = 0
        self._episodes = None
        self._episodes_error = None
        self._model.set_episodes([])
        self._debounce.stop()
        self._zone = SEASONS
        self._button = False
        self._moved = False
        self._plot_lines_cache = None
        self._poster_url = poster_url(series)
        if self._poster_url and self._posters is not None:
            self._posters.request([self._poster_url])
        self._sync()

    def set_seasons(
        self, seasons: list[dict] | None, error: str | None = None, *, current: dict | None = None
    ) -> None:
        """Show the seasons. `current` opens at that season, but only while the view has no
        season yet for this series: a repeated result never moves the user back."""
        self._seasons = seasons
        self._seasons_error = error
        if seasons:
            keys = [item_key(s) for s in seasons]
            shown = item_key(self._season) if self._season is not None else None
            if shown in keys:
                self._season_idx = keys.index(shown)
            else:
                wanted = item_key(current) if current is not None else None
                self._season_idx = keys.index(wanted) if wanted in keys else 0
                self._season = seasons[self._season_idx]
                self._episodes = None
                self._episodes_error = None
                self._model.set_episodes([])
        self._sync()

    def set_episodes(
        self, season: dict, episodes: list[dict] | None, error: str | None = None
    ) -> None:
        if self._season is not None and item_key(season) != item_key(self._season):
            return  # a superseded season's result
        previous = self.current_episode()
        self._season = season
        self._episodes = episodes
        self._episodes_error = error
        self._button = False
        self._model.set_episodes(episodes or [])
        if episodes:
            row = self._model.row_of(episode_key(previous)) if previous is not None else -1
            self._view.setCurrentIndex(self._model.index(max(0, row)))
            if not self._moved:
                self._zone = EPISODES
        elif self._zone == EPISODES:
            self._zone = SEASONS
        self._sync()

    def set_progress_provider(self, fn: Callable[[dict], float | None]) -> None:
        self._model.set_provider(fn)

    def current_season(self) -> dict | None:
        return self._season

    def current_episode(self) -> dict | None:
        if self._status() or not self._view.currentIndex().isValid():
            return None
        return self._model.item(self._view.currentIndex().row())

    @property
    def focus_zone(self) -> str:
        return self._zone

    def row_badges(self, row: int) -> str:
        progress = self._model.progress(row) if 0 <= row < self._model.rowCount() else None
        return "✓" if progress is not None and progress >= WATCHED_AT else ""

    # ---- state ---------------------------------------------------------------------------
    def _status(self) -> str:
        """The message replacing the episodes list, or '' when the list is shown."""
        if self._seasons_error:
            return ERROR_TEXT
        if self._episodes_error:
            return ERROR_TEXT
        if self._seasons is None and self._episodes is None:
            return LOADING_TEXT
        if self._seasons is not None and not self._seasons and self._episodes is None:
            return EMPTY_SEASONS_TEXT
        if self._episodes is None:
            return LOADING_TEXT
        if not self._episodes:
            return EMPTY_EPISODES_TEXT
        return ""

    def _is_error(self) -> bool:
        return bool(self._seasons_error or self._episodes_error)

    def _sync(self) -> None:
        self._view.setVisible(not self._status())
        self._view.focus_active = self._zone == EPISODES
        self._view.button_active = self._button and self._zone == EPISODES
        self._view.viewport().update()
        self.update()

    # ---- keys ----------------------------------------------------------------------------
    def move_by(self, dx: int, dy: int) -> None:
        if self._zone == SEASONS:
            self._move_seasons(dx, dy)
        elif dx:
            self._move_button(dx)
        else:
            self._move_episodes(dy)

    def page(self, delta: int) -> None:
        """PageUp/PageDown: move the episodes selection by whole visible pages."""
        if self._zone != EPISODES or not self._model.rowCount():
            return
        row_h = max(1, self._view.sizeHintForRow(0))
        visible = max(1, self._view.viewport().height() // row_h)
        self._select_row(self._view.currentIndex().row() + delta * visible)

    def _select_row(self, row: int) -> None:
        row = min(self._model.rowCount() - 1, max(0, row))
        index = self._model.index(row)
        self._button = False
        self._view.setCurrentIndex(index)
        self._view.scrollTo(index)
        self._sync()

    def _current_resumable(self) -> bool:
        row = self._view.currentIndex().row()
        return 0 <= row < self._model.rowCount() and is_resumable(self._model.progress(row))

    def _move_button(self, dx: int) -> None:
        if dx > 0 and self._current_resumable() and not self._button:
            self._button = True
        elif dx < 0 and self._button:
            self._button = False
        else:
            return
        self._sync()

    def _move_seasons(self, dx: int, dy: int) -> None:
        seasons = self._seasons or []
        if dx:
            if dx < 0 and self._season_idx == 0:
                self.backRequested.emit()
                return
            idx = min(len(seasons) - 1, max(0, self._season_idx + dx))
            if seasons and idx != self._season_idx:
                self._season_idx = idx
                self._season = seasons[idx]
                self._episodes = None
                self._episodes_error = None
                self._model.set_episodes([])
                self._debounce.start()
                self._sync()
        elif dy > 0 and self._episodes:
            self._moved = True
            self._zone = EPISODES
            self._sync()

    def _move_episodes(self, dy: int) -> None:
        if not dy or not self._model.rowCount():
            return
        row = self._view.currentIndex().row()
        if dy < 0 and row <= 0:
            if self._seasons:
                self._moved = True
                self._button = False
                self._zone = SEASONS
                self._sync()
            return
        self._select_row(row + dy)

    def activate(self) -> None:
        if self._is_error():
            self.retryRequested.emit()
            return
        if self._zone == SEASONS:
            if self._debounce.isActive():
                self._debounce.stop()
                self._emit_season()
            elif self._episodes:
                self._moved = True
                self._zone = EPISODES
                self._sync()
            return
        episode = self.current_episode()
        if episode is not None and self._season is not None:
            if self._button:
                self.startOverRequested.emit(self._season, episode)
            else:
                self.episodeActivated.emit(self._season, episode)

    def escape(self) -> bool:
        self.backRequested.emit()
        return True

    def _emit_season(self) -> None:
        if self._season is not None:
            self.seasonSelected.emit(self._season)

    def _on_row_clicked(self, row: int) -> None:
        self._moved = True
        self._zone = EPISODES
        self._button = False
        self._view.setCurrentIndex(self._model.index(row))
        self._sync()
        self.activate()

    def _on_poster_ready(self, url: str) -> None:
        if url == self._poster_url:
            self.update()

    # ---- geometry ------------------------------------------------------------------------
    def _units(self) -> Units:
        return Units(max(1, self.width()))

    def _header_h(self) -> float:
        return self.height() * HEADER_FRACTION

    def _chips_rect(self) -> QRectF:
        u = self._units().u
        return QRectF(
            MARGIN_U * u, self._header_h() + 1.0 * u, self.width() - 2 * MARGIN_U * u, CHIP_H_U * u
        )

    def _layout(self) -> None:
        units = self._units()
        top = round(self._header_h() + (CHIP_H_U + 2.6) * units.u)
        self._view.units_width = units.width
        self._view.setGeometry(
            units.px(MARGIN_U) - 1,
            top,
            max(1, self.width() - 2 * units.px(MARGIN_U) + 2),
            max(1, self.height() - top),
        )
        self._plot_lines_cache = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layout()
        self._view.doItemsLayout()

    # ---- painting ------------------------------------------------------------------------
    def season_label(self, season: dict, idx: int) -> str:
        name = str(season.get("name") or season.get("title") or "").strip()
        return name or f"Season {season.get('season_number') or idx + 1}"

    def _wrap(self, text: str, fm: QFontMetrics, width: int, lines: int) -> list[str]:
        key = (width, text)
        if self._plot_lines_cache and self._plot_lines_cache[:2] == key:
            return self._plot_lines_cache[2]
        words = text.split()
        out: list[str] = []
        current = ""
        i = 0
        while i < len(words) and len(out) < lines:
            trial = f"{current} {words[i]}".strip()
            if fm.horizontalAdvance(trial) <= width or not current:
                current = trial
                i += 1
            else:
                out.append(current)
                current = ""
        if current and len(out) < lines:
            out.append(current)
            current = ""
        if i < len(words) and out:  # more text than lines: elide the last line
            out[-1] = fm.elidedText(" ".join([out[-1]] + words[i:]), Qt.ElideRight, width)
        self._plot_lines_cache = (width, text, out)
        return out

    def _meta_line(self) -> str:
        d = self._details
        if d is None:
            return ""
        parts = [d.year, d.duration, d.genre]
        if d.rating:
            parts.append(f"★ {d.rating}")
        return " · ".join(str(part) for part in parts if part)

    def paintEvent(self, event) -> None:
        units = self._units()
        u = units.u
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            self._paint_header(p, units)
            self._paint_chips(p, units)
            status = self._status()
            if status:
                area = QRectF(0, self._view.y(), self.width(), self.height() - self._view.y())
                p.setFont(units.font(1.4))
                color = qcolor(PALETTE.text, 1.0 if self._is_error() else 0.5)
                p.setPen(color)
                p.drawText(area.adjusted(0, 0, 0, -6 * u), Qt.AlignCenter, status)
        finally:
            p.end()

    def _paint_header(self, p: QPainter, units: Units) -> None:
        u = units.u
        header_h = self._header_h()
        ph = min(POSTER_W_U * 1.5 * u, header_h - 2.0 * u)
        pw = ph * 2 / 3
        poster = QRectF(MARGIN_U * u, 1.0 * u, pw, ph)
        name = str((self._details.title if self._details else "") or self._series.get("name") or "")
        pixmap = (
            self._posters.pixmap(self._poster_url) if self._posters and self._poster_url else None
        )
        draw_logo(p, poster, name, pixmap, RADIUS_CARD * u / 10)
        x = poster.right() + 2.5 * u
        width = self.width() - x - MARGIN_U * u
        y = 1.0 * u
        p.setFont(units.font(TITLE_U, QFont.Weight.Bold))
        p.setPen(qcolor(PALETTE.text))
        title_h = TITLE_U * u * 1.4
        p.drawText(
            QRectF(x, y, width, title_h), Qt.AlignLeft | Qt.AlignVCenter, elided(p, name, width)
        )
        y += title_h + 0.4 * u
        meta = self._meta_line()
        p.setFont(units.font(META_U))
        p.setPen(qcolor(PALETTE.text, 0.62))
        meta_h = META_U * u * 1.5
        if meta:
            p.drawText(
                QRectF(x, y, width, meta_h), Qt.AlignLeft | Qt.AlignVCenter, elided(p, meta, width)
            )
        y += meta_h + 0.6 * u
        plot = (self._details.plot if self._details else "") or ""
        if plot:
            font = units.font(PLOT_U)
            p.setFont(font)
            p.setPen(qcolor(PALETTE.text, 0.8))
            fm = QFontMetrics(font)
            line_h = PLOT_U * u * 1.45
            for i, line in enumerate(self._wrap(plot, fm, int(width), PLOT_LINES)):
                p.drawText(
                    QRectF(x, y + i * line_h, width, line_h), Qt.AlignLeft | Qt.AlignVCenter, line
                )

    def _paint_chips(self, p: QPainter, units: Units) -> None:
        seasons = self._seasons or []
        if not seasons:
            return
        u = units.u
        area = self._chips_rect()
        font = units.font(CHIP_FONT_U, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        rects: list[QRectF] = []
        x = 0.0
        for i, season in enumerate(seasons):
            w = fm.horizontalAdvance(self.season_label(season, i)) + 2 * CHIP_PAD_U * u
            rects.append(QRectF(x, 0, w, area.height()))
            x += w + CHIP_GAP_U * u
        cur = rects[min(self._season_idx, len(rects) - 1)]
        shift = max(0.0, cur.right() - area.width())
        p.save()
        p.setClipRect(area.adjusted(-2, -4, 2, 4))
        p.setFont(font)
        radius = CHIP_H_U * u / 2
        focused = self._zone == SEASONS
        for i, (season, r) in enumerate(zip(seasons, rects)):
            box = r.translated(area.x() - shift, area.y())
            current = i == self._season_idx
            if current and focused:
                fill_round(p, box, qcolor(PALETTE.accent, FOCUS_FILL_ALPHA), radius)
                p.setPen(qcolor(PALETTE.accent))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)
            else:
                fill_round(p, box, qcolor(PALETTE.text, 0.07), radius)
            p.setPen(qcolor(PALETTE.text, 1.0 if current else 0.62))
            p.drawText(box, Qt.AlignCenter, self.season_label(season, i))
            if current and not focused:
                underline = QRectF(
                    box.x() + 1.2 * u, box.bottom() + 0.25 * u, box.width() - 2.4 * u, 0.25 * u
                )
                fill_round(p, underline, qcolor(PALETTE.accent), 0.125 * u)
        p.restore()
