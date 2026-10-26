"""The ⚙ quality menu (2.8): a Jellyfin or Emby title's versions, each as its file is, and the
server's conversions below the version playing.

The transport's ⚙ (shown while such a title plays: TVController sets it) opens the panel above
it, as the Picture panel opens (ui/overlays/track_panel.py). A row plays the title again in
that quality, on from where it is (`choose`: TVController.change_quality). The first conversion
chosen on a server asks first, on its own question card: converting uses the server's
processor, which its owner may not want. QiTV never converts unless asked here.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRectF, Qt

from services import i18n, media_server as ms
from services.i18n import N_, tr, translate
from services.provider_ids import provider_of
from ui.keymap import Action
from ui.overlays.track_panel import PanelModel, PanelRow

logger = logging.getLogger(__name__)

_UP, _DOWN = Qt.Key.Key_Up.value, Qt.Key.Key_Down.value
QUALITY = "quality"  # the transport's button, and the panel it holds
QUALITY_TITLE = N_("Quality")  # i18n: the ⚙ panel's title: a title's versions and conversions
# i18n: a ⚙ panel row: the server converts the title to this size and rate ("720p · 4 Mbit/s")
CONVERTED_ROW = N_("{quality}, converted")
# i18n: asked before the first conversion on a Jellyfin or Emby server
CONVERT_QUESTION = N_(
    "Converting uses the server's processor. On a server someone else runs, its owner may not"
    " want that."
)
CONVERT_YES = N_("Convert")  # i18n: the answer that converts
CONVERT_NO = N_("Keep the original")  # i18n: the answer that plays the file as it is


def rate_text(bitrate: int) -> str:
    """A bitrate as a row shows it: "8.2 Mbit/s", "4 Mbit/s"."""
    number = f"{bitrate / 1_000_000:.1f}".rstrip("0").rstrip(".")
    # i18n: a bitrate, in megabits a second ("8.2 Mbit/s")
    return tr("{n} Mbit/s", n=i18n.decimal(number))


def size_text(height: int, uhd: bool = False) -> str:
    """A picture's size as a row shows it: "4K", "1080p" ("" when not known)."""
    if uhd:
        return "4K"
    return f"{height}p" if height > 0 else ""


def version_text(version: dict) -> str:
    """A version's row: its size and rate, and the server's name for it when it has one."""
    parts = [
        size_text(int(version.get("height") or 0), bool(version.get("uhd"))),
        rate_text(int(version.get("bitrate") or 0)) if version.get("bitrate") else "",
        str(version.get("name") or ""),
    ]
    return " · ".join(part for part in parts if part) or str(version.get("id") or "")


def conversion_text(quality: ms.Quality) -> str:
    inner = " · ".join(
        part
        for part in (
            size_text(quality.height),
            rate_text(quality.bitrate) if quality.bitrate else "",
        )
        if part
    )
    return translate(CONVERTED_ROW, quality=inner)


class QualityControls(QObject):
    def __init__(
        self,
        *,
        panel,
        transport,
        card,
        current: Callable[[], Optional[Any]],
        menu: Callable[[dict], Optional[dict]],
        choose: Callable[[ms.Quality], None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`panel` is a TrackPanel, `card` a QuestionCard of its own; `current()` is what plays
        (a Target), `menu(item)` what its server offers ({"versions", "quality"}: the
        catalog's media_menu), `choose(quality)` plays it again in that quality."""
        super().__init__(parent)
        self._panel = panel
        self._transport = transport
        self._card = card
        self._current = current
        self._menu = menu
        self._choose = choose
        self._warned: set[str] = set()  # servers whose question was answered yes
        self._pending: Optional[tuple[str, ms.Quality]] = None  # the conversion asked about

    def wire(self) -> None:
        self._transport.qualityRequested.connect(self.open_panel)
        self._transport.bandChanged.connect(self._follow_band)
        self._panel.rowChosen.connect(self._on_row)
        self._panel.closed.connect(self._on_closed)
        self._card.answered.connect(self._on_answer)

    def open_panel(self) -> None:
        """The transport's ⚙: the panel above it (nothing when the title offers no choice)."""
        data = self._offered()
        if data is None:
            return
        self._transport.hold(QUALITY)
        self._panel.show_panel(self._model(data), self._anchor(), self._floor(), QUALITY)

    def close(self) -> None:
        self._panel.dismiss()

    def visible(self) -> bool:
        return bool(self._panel.isVisible() and self._transport.held() == QUALITY)

    def handle(self, action: Action, key: Optional[int] = None) -> bool:
        """The keys while the question asks or the panel shows (False: not theirs). A key
        that is not the panel's closes it and goes on."""
        if not self._card.isHidden():
            return bool(self._card.handle(action))
        if not self.visible():
            return False
        panel = self._panel
        if action is Action.ESCAPE:
            panel.dismiss()
        elif action is Action.CH_PREV and key == _UP:
            panel.move_focus(-1)
        elif action is Action.CH_NEXT and key == _DOWN:
            if panel.focused() is None:
                panel.focus_row(0)
            else:
                panel.move_focus(1)
        elif action is Action.OK:
            panel.activate()
        else:
            panel.dismiss()
            return False
        return True

    # ---- the panel ------------------------------------------------------------------------

    def _offered(self) -> Optional[dict]:
        target = self._current()
        if target is None or getattr(target, "live", False):
            return None
        data = self._menu(target.item)
        return data if data and data.get("versions") else None

    def _model(self, data: dict) -> PanelModel:
        playing = data.get("quality") or ms.Quality()
        versions = list(data.get("versions") or [])
        rows = [
            PanelRow(
                key=ms.Quality(source=str(v.get("id") or "")),
                text=version_text(v),
                checked=playing.same_as(ms.Quality(source=str(v.get("id") or ""))),
            )
            for v in versions
        ]
        base = next((v for v in versions if v.get("id") == playing.source), None)
        top = int(base.get("height") or 0) if base else 0
        for quality in ms.CONVERSIONS:
            if top and quality.height >= top:
                continue  # never "converted" up to the version's own size or more
            rows.append(
                PanelRow(
                    key=quality, text=conversion_text(quality), checked=playing.same_as(quality)
                )
            )
        return PanelModel(title=translate(QUALITY_TITLE), rows=tuple(rows))

    def _anchor(self) -> Optional[QRectF]:
        try:
            rect = self._transport.button_rect(QUALITY)
        except KeyError:
            return None
        return QRectF(rect).translated(self._transport.pos())

    def _floor(self) -> Optional[int]:
        transport = self._transport
        return transport.controls_top() if transport.isVisible() else None

    def _follow_band(self) -> None:
        if self._panel.isVisible() and self._transport.held() == QUALITY:
            self._panel.show_panel(self._panel.model(), self._anchor(), self._floor())

    def _on_closed(self) -> None:
        if self._transport.held() == QUALITY:
            self._transport.hold("")

    # ---- choices ------------------------------------------------------------------------

    def _on_row(self, key: Any) -> None:
        if not isinstance(key, ms.Quality) or self._transport.held() != QUALITY:
            return
        data = self._offered()
        target = self._current()
        self.close()
        playing = data.get("quality") if data is not None else None
        if target is None or playing is None or key.same_as(playing):
            return
        server = provider_of(target.item) or ""
        if key.converted and server not in self._warned:
            self._pending = (server, key)
            self._card.ask(
                translate(CONVERT_QUESTION), translate(CONVERT_YES), translate(CONVERT_NO)
            )
            return
        self._choose(key)

    def _on_answer(self, yes: bool) -> None:
        pending, self._pending = self._pending, None
        if pending is None or not yes:
            return
        server, quality = pending
        self._warned.add(server)
        self._choose(quality)
