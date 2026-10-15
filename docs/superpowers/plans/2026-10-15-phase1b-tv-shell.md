# Phase 1B TV Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two-window v1.13.8 UI with one TV-first window. VLC draws natively into a video surface, and a translucent overlay host glued above it carries:
- the zap banner (A1);
- the fast-zap channel column (A2);
- the two-layer drawer;
- the uosc-style transport;
- toasts.

Live zapping works end to end, and Movies/Series are browsable and playable through the drawer as plain rows. The Netflix-style library is Phase 3.

**Architecture:**
- `ui/` holds the widgets; `ui/tv_controller.py` holds the behaviour.
- The controller is a QObject wiring the Phase 1A services (`CatalogService`, `NavModel`, `ZapController`, `PlaybackSession`, `WatchState`) to the overlays.
- Overlays are dumb painters with small, explicit APIs.
- Key handling resolves to actions in `ui/keymap.py` (pure).
- `main.py` starts `TVWindow` instead of `VideoPlayer` + `ChannelList`. The old modules stay on disk, unused, until Phase 3 deletes them.

**Tech Stack:** Python 3.14, PySide6 6.11.2 (QtWidgets), python-vlc 3.0.21203 (libVLC 3 from the official installers), `unittest`, `uv`.

**Spec:** `docs/superpowers/specs/2026-10-15-tv-on-vlc-design.md`:
- §5.1 (overlays), §5.2–5.3 (layers, keys), §5.5 (transport), §5.6 (tokens), §5.7 (overlay host), §8 (startup and settings);
- spike D4 (native VLC plus a glued overlay window).

Approved mockups: `docs/superpowers/specs/assets/2026-10-15-tv-on-vlc/01-shapes.png` (A1, A2), `02-groups.png` (drawer), `03-layers.png` (layers), `04-controls.png` (transport). Phase 1A plan: `docs/superpowers/plans/2026-10-15-phase1a-tv-services.md` (the interfaces used here).

## Global Constraints

- Python `>=3.14,<3.15`. Run everything through `uv run …` from `<repo>` on branch `tv/vlc`.
- No native or compiled dependencies and no new runtime dependencies.
- **Overlay host:** VLC renders natively into `VideoSurface` (a native child widget), and every overlay is a child of `OverlayHost`, a frameless translucent tool window glued over the surface. Its **mask is the union of the visible overlays**, so the video stays visible and clickable elsewhere, and it degrades to opaque panels without a compositor.
- Every overlay widget (banner, toast, column, drawer, transport) subclasses `ui.overlays.base.Overlay` (Task 1).
- Overlays never take keyboard focus. `TVWindow` receives keys and routes them through `ui/keymap.py`.
- Sizes scale with the video area. 1 unit `u` = 1% of the overlay host width, which is the mockups' `cqw`. Colours come only from `ui.theme` (`PALETTE`, `qcolor`, `RADIUS_*`, `FOCUS_FILL_ALPHA`).
- **Timings (spec):**
  - banner visible 4 s after a tune;
  - transport fades 3 s after the last mouse move or key;
  - zap rest 400 ms, burst window 1 s, digit commit 1.2 s (from `ZapController`);
  - the column auto-closes 2.5 s after the last zap step.
- **Keys (spec §5.3, no overlay open):**

  | Key | Action |
  |---|---|
  | ↑/↓, PgUp/PgDn | channel |
  | Digits | number entry |
  | Backspace | previous channel |
  | Enter / Return | drawer |
  | ←/→ | seek −10/+10 s when seekable |
  | Space | pause |
  | `L` | Go live (a Phase 2 action; Phase 1 shows a toast) |
  | `G` | guide (Phase 3; Phase 1 shows a toast) |
  | `I` | info banner |
  | `F` / double-click | fullscreen |
  | `M` | mute |
  | `+`/`−` | volume |
  | Esc | close the top overlay, then leave fullscreen |
  | Alt+P | mini player |
  | Mouse wheel | channel (setting `mouse_wheel_action`); ⌘/Ctrl+wheel = volume |
  | Click on video | toggle the drawer |
- Never block the GUI thread on network or heavy disk work. Never build QPixmap/QIcon off the GUI thread.
- Logging uses `logging.getLogger(__name__)`; no `print`.
- Tests use `unittest` with `QT_QPA_PLATFORM=offscreen`. Lint: `uv run flake8 --select=E9,F63,F7,F82 <files>`. Format with black/isort (line length 100). New modules pass `uv run mypy <module>`.
- Commits end with these two lines; stage files explicitly and never use `git add -A`:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_011UFrnH8jUNCzkFNLKJaoRD
  ```

## Rulings carried from the controller (binding for this plan)

- **R1:** Phase 1 does **not** keep the old browser window reachable (this deviates from spec §12). Movies/Series are browsed in the drawer as plain rows until the Phase 3 library. Exports, the history dialog and Search All return in Phase 3. Cost if wrong: a feature gap on the development branch only; there is no release before Phase 3.
- **R2:** Channel logos and EPG are on by default from 2.0. The one-time migration is in Phase 1A Task 1.

## Review Focus

1. **Moving or resizing the window, entering or leaving fullscreen, minimizing and restoring:**
   - the overlay host must stay exactly over the video surface;
   - it must be hidden while the window is minimized or hidden;
   - nothing may stay floating over other apps.
   (Task 1, plus the smoke test in Task 9.)
2. **Rapid zapping on a provider limited to 1–2 streams:** at most one link request in flight. A link result for a channel the user has already left must never start playback (Task 7).
3. **Huge groups** (thousands of channels) and groups with missing logos or numbers: the column and drawer must stay responsive (virtualized list views, no per-row widgets) and fall back to initials badges (Tasks 4 and 5).
4. **Typing in the drawer filter:** non-ASCII letters (Turkish `ç ğ ı ö ş ü`) and case-insensitive matching. Esc clears the filter before closing (Task 5).
5. **First launch with no providers, or a provider that fails to load:** the window shows the welcome card or an error toast. It must not crash or sit as a black screen with no hint (Tasks 7 and 8).

---

### Task 1: Video surface, overlay host and config-dir override

**Files:**
- Modify: `config_manager.py` (environment override `QITV_CONFIG_DIR` in `_get_config_path`)
- Create: `ui/video_surface.py`, `ui/overlay_host.py`, `ui/macos_window.py`, `ui/overlays/__init__.py` (empty docstring module), `ui/overlays/base.py`
- Test: `tests/test_overlay_host.py`

**Why a base class:** Qt sends no Show event to a child whose top-level parent is hidden. An overlay shown while the host is hidden therefore cannot be noticed through an event filter. Every overlay derives from `Overlay`, whose `setVisible` override (which `show()`/`hide()` call) tells the host to recompute its mask and visibility.

**Interfaces:**
- Produces:
  - `VideoSurface(QWidget)`, a native child with:
    - signals `clicked()`, `doubleClicked()`, `mouseMoved()`, `wheelSteps(int, Qt.KeyboardModifiers)` (+1 means "next / down");
    - `native_handle() -> int`.
  - `Overlay(QWidget)` in `ui/overlays/base.py`: the base class for every overlay. Its `setVisible(visible)` calls `super().setVisible(visible)` and then `self.parentWidget().overlay_changed()` when the parent has that method. Its `mask_region() -> QRegion` defaults to `QRegion(self.geometry())`, and subclasses may override it.
  - `OverlayHost(QWidget)`, constructed as `OverlayHost(window: QWidget, surface: QWidget)`. It has:
    - `add_overlay(widget: Overlay)`;
    - `overlay_changed()`, which recomputes the mask and visibility (called by `Overlay`);
    - `sync_geometry()`;
    - `visible_overlays() -> list[QWidget]`;
    - signals `clicked()`, `doubleClicked()`, `mouseMoved()`, `wheelSteps(int, Qt.KeyboardModifiers)`, which are forwarded for clicks on the host outside any overlay that consumes them.
  - `attach_child_window(parent: QWidget, child: QWidget) -> bool`. It runs `[[parent window] addChildWindow:[child window] ordered:NSWindowAbove]` on macOS, is a no-op returning False elsewhere, and never raises.
  - `ConfigManager`: when the environment variable `QITV_CONFIG_DIR` is set, the config path is `$QITV_CONFIG_DIR/config.json` (the directory is created). This is used by the smoke test and must not change behaviour when unset.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overlay_host.py`:
```python
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

from ui.overlay_host import OverlayHost
from ui.overlays.base import Overlay
from ui.video_surface import VideoSurface


def surface_rect(surface):
    return QRect(surface.mapToGlobal(QPoint(0, 0)), surface.size())


class OverlayHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QMainWindow()
        self.surface = VideoSurface()
        self.window.setCentralWidget(self.surface)
        self.window.setGeometry(100, 100, 800, 450)
        self.window.show()
        QTest.qWait(20)
        self.host = OverlayHost(self.window, self.surface)
        self.panel = Overlay()
        self.host.add_overlay(self.panel)
        self.panel.setGeometry(0, 0, 200, 100)
        self.addCleanup(self.window.close)

    def test_hidden_until_an_overlay_is_visible(self):
        self.assertFalse(self.host.isVisible())
        self.panel.show()
        QTest.qWait(20)
        self.assertTrue(self.host.isVisible())
        self.panel.hide()
        QTest.qWait(20)
        self.assertFalse(self.host.isVisible())

    def test_glued_to_surface_through_move_and_resize(self):
        self.panel.show()
        QTest.qWait(20)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))
        self.window.move(160, 140)
        QTest.qWait(30)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))
        self.window.resize(1000, 560)
        QTest.qWait(30)
        self.assertEqual(self.host.geometry(), surface_rect(self.surface))

    def test_mask_is_union_of_visible_overlays(self):
        self.panel.show()
        QTest.qWait(20)
        self.assertEqual(self.host.mask().boundingRect(), QRect(0, 0, 200, 100))

    def test_hidden_with_window(self):
        self.panel.show()
        QTest.qWait(20)
        self.window.hide()
        QTest.qWait(30)
        self.assertFalse(self.host.isVisible())
        self.window.show()
        QTest.qWait(30)
        self.assertTrue(self.host.isVisible())

    def test_video_surface_is_native(self):
        self.assertGreater(self.surface.native_handle(), 0)


class ConfigDirOverrideTests(unittest.TestCase):
    def test_env_override(self):
        from config_manager import ConfigManager

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"QITV_CONFIG_DIR": directory}):
            path = ConfigManager._get_config_path(ConfigManager.__new__(ConfigManager))
        self.assertEqual(path, os.path.join(directory, "config.json"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify they fail.** Run: `uv run python -m unittest tests.test_overlay_host -v`. Expected: `ModuleNotFoundError: ui.overlay_host`.

- [ ] **Step 3: Implement**

1. In `config_manager.py`, make `_get_config_path` begin with:
```python
        override = os.environ.get("QITV_CONFIG_DIR")
        if override:
            os.makedirs(override, exist_ok=True)
            return os.path.join(override, "config.json")
```
2. Create `ui/overlays/base.py`:
```python
"""Base class for overlays hosted by OverlayHost."""

from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget


class Overlay(QWidget):
    def setVisible(self, visible: bool) -> None:  # show()/hide() route through here
        super().setVisible(visible)
        host = self.parentWidget()
        if host is not None and hasattr(host, "overlay_changed"):
            host.overlay_changed()

    def mask_region(self) -> QRegion:
        return QRegion(self.geometry())
```
3. Create `ui/video_surface.py`:
```python
"""Native child widget libVLC draws into; reports mouse input to the shell."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget


class VideoSurface(QWidget):
    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMouseTracking(True)
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#000000"))
        self.setPalette(palette)
        self._click = QTimer(self, singleShot=True)
        self._click.timeout.connect(self.clicked)
        self._wheel_accumulator = 0

    def native_handle(self) -> int:
        return int(self.winId())

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._click.start(QApplication.doubleClickInterval())
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self._click.stop()
        self.doubleClicked.emit()

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()
        super().mouseMoveEvent(event)

    def wheelEvent(self, event):
        self._wheel_accumulator += event.angleDelta().y()
        steps = int(self._wheel_accumulator / 120)
        if steps:
            self._wheel_accumulator -= steps * 120
            self.wheelSteps.emit(-steps, event.modifiers())  # scroll down = next
        event.accept()
```
4. Create `ui/macos_window.py`:
```python
"""Attach the overlay as a Cocoa child window so macOS moves it with the player (spec §5.7)."""

import ctypes
import ctypes.util
import logging
import platform

logger = logging.getLogger(__name__)
_NS_WINDOW_ABOVE = 1


def attach_child_window(parent, child) -> bool:
    if platform.system() != "Darwin":
        return False
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        parent_window = send(ctypes.c_void_p(int(parent.winId())), objc.sel_registerName(b"window"))
        child_window = send(ctypes.c_void_p(int(child.winId())), objc.sel_registerName(b"window"))
        if not parent_window or not child_window:
            return False
        attach = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(
            ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        )
        attach(parent_window, objc.sel_registerName(b"addChildWindow:ordered:"), child_window, _NS_WINDOW_ABOVE)
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not attach the overlay as a child window: %s", e)
        return False
```
5. Create `ui/overlay_host.py`:
```python
"""Translucent window glued over the video surface; parent of every overlay (spec §5.7)."""

import logging

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QApplication, QWidget

from ui.macos_window import attach_child_window

logger = logging.getLogger(__name__)

_TRACKED = {
    QEvent.Type.Move,
    QEvent.Type.Resize,
    QEvent.Type.Show,
    QEvent.Type.Hide,
    QEvent.Type.WindowStateChange,
}


class OverlayHost(QWidget):
    clicked = Signal()
    doubleClicked = Signal()
    mouseMoved = Signal()
    wheelSteps = Signal(int, object)  # steps, Qt.KeyboardModifier

    def __init__(self, window: QWidget, surface: QWidget):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(window, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self._window = window
        self._surface = surface
        self._overlays: list[QWidget] = []
        self._attached = False
        window.installEventFilter(self)
        surface.installEventFilter(self)

    def add_overlay(self, widget: QWidget) -> None:
        self._overlays.append(widget)
        widget.setParent(self)
        widget.hide()
        widget.installEventFilter(self)

    def overlay_changed(self) -> None:
        self._update_mask()
        self._update_visibility()

    def visible_overlays(self) -> list[QWidget]:
        return [w for w in self._overlays if w.isVisibleTo(self)]

    def sync_geometry(self) -> None:
        rect = QRect(self._surface.mapToGlobal(QPoint(0, 0)), self._surface.size())
        if rect != self.geometry():
            self.setGeometry(rect)
        self._update_mask()

    def _update_mask(self) -> None:
        region = QRegion()
        for widget in self.visible_overlays():
            part = widget.mask_region() if hasattr(widget, "mask_region") else QRegion(widget.geometry())
            region = region.united(part)
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)

    def _window_shown(self) -> bool:
        return self._window.isVisible() and not self._window.isMinimized()

    def _update_visibility(self) -> None:
        wanted = bool(self.visible_overlays()) and self._window_shown()
        if wanted:
            self.sync_geometry()
            if not self.isVisible():
                self.show()
                self.raise_()
                if not self._attached:
                    self._attached = attach_child_window(self._window, self)
        elif self.isVisible():
            self.hide()

    def eventFilter(self, obj, event):
        kind = event.type()
        if obj in (self._window, self._surface) and kind in _TRACKED:
            if self.isVisible():
                self.sync_geometry()
            self._update_visibility()
        elif obj in self._overlays and kind in (QEvent.Type.Resize, QEvent.Type.Move):
            self._update_mask()
        return False

    # Clicks that reach the host itself (on transparent parts of overlays that let them through).
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def mouseDoubleClickEvent(self, event):
        self.doubleClicked.emit()

    def mouseMoveEvent(self, event):
        self.mouseMoved.emit()

    def wheelEvent(self, event):
        steps = -int(event.angleDelta().y() / 120)
        if steps:
            self.wheelSteps.emit(steps, event.modifiers())
        event.accept()
```
   Visibility is driven by `Overlay.setVisible` calling `overlay_changed()`, not by Show/Hide events (see "Why a base class"). Remove the `QApplication` import if it is unused.

- [ ] **Step 4: Run to verify they pass, then run the suite.** Expected: 6 PASS and a green suite. On the offscreen platform `winId()` is non-zero after the window is shown. If `test_glued_to_surface_through_move_and_resize` fails only because offscreen window moves are applied asynchronously, increase the waits to `QTest.qWait(100)` and say so in the report. Do not weaken the assertion.

- [ ] **Step 5: Lint, type-check, commit** (`feat: add the video surface and glued overlay host`).

---

### Task 2: Painting kit and logo cache

**Files:**
- Create: `ui/overlays/painting.py`, `ui/logos.py` (`ui/overlays/__init__.py` exists from Task 1)
- Test: `tests/test_painting.py`

**Interfaces:**
- Produces in `ui/overlays/painting.py`:
  - `Units(width: int)`, where `.u` is 1% of the width; `px(units: float) -> int` (at least 1); `font(units: float, weight=QFont.Weight.Normal) -> QFont` (pixel size `px(units)`).
  - `fill_round(p, rect: QRectF, color: QColor, radius: float)`.
  - `draw_progress(p, rect: QRectF, fraction: float, *, track_alpha=0.18, color=None)` (accent by default).
  - `elided(p, text, width) -> str`.
  - `initials(name) -> str`, giving up to 2 letters from the words (or the first two characters).
  - `badge_color(name) -> QColor`: a stable pick from `_BADGE_COLORS = ["#2f6f7a", "#6a5f9e", "#9b6a3e", "#4d7a4a", "#a2566a", "#355f9a", "#8a4a4a", "#3a6e4a"]` by `sha1(name)`.
  - `draw_logo(p, rect, name, pixmap: QPixmap | None, radius)`: the scaled pixmap on a dark rounded tile, else an initials badge.
  - `draw_icon(p, name, rect: QRectF, color: QColor)`: vector icons for `play`, `pause`, `back10`, `fwd10`, `prev`, `next`, `volume`, `mute`, `fullscreen`, `subtitles`, `audio`, `search`, `star`, `recent`, `live_dot`. These are drawn with QPainterPath, never font glyphs (platform fonts lack them).
  - `fmt_clock(dt) -> "HH:MM"` and `fmt_duration(ms) -> "M:SS"` or `"H:MM:SS"`.
- Produces in `ui/logos.py`:
  - `LogoCache(QObject)`, constructed as `LogoCache(image_manager, config_manager, parent=None)`, with:
    - signal `logoReady(str url)`;
    - `pixmap(url) -> QPixmap | None` (cached; built on the GUI thread);
    - `request(urls: list[str])`. It batches unseen URLs into one `ImageLoader(urls, image_manager, iconified=True, verify_ssl=config.ssl_verify)` at a time, retires loaders through `ThreadCleanup` as `display_mixin` does, and does nothing when `config_manager.channel_logos` is False.

- [ ] **Step 1: Write failing tests** for:
  - `Units(1280).px(2.6) == 33`;
  - `initials("Nature One HD") == "NO"` and `initials("bbc") == "BB"`;
  - `badge_color("A")` being stable across calls;
  - `fmt_duration(37_000) == "0:37"` and `fmt_duration(3_725_000) == "1:02:05"`;
  - `draw_icon` painting every listed name onto a 64×64 `QImage` without raising, with at least one non-transparent pixel per icon;
  - `draw_logo` with `pixmap=None` painting the badge;
  - `LogoCache.request` doing nothing (no thread started) when `channel_logos` is False;
  - `LogoCache.pixmap` returning None before a load.

  Use a `QApplication` and `QImage(64, 64, QImage.Format_ARGB32_Premultiplied)` filled transparent.
- [ ] **Step 2: Run them to verify they fail. Step 3: Implement** the module per the interface. For the icons, use these paths in a unit box (0..1, scaled into `rect`):
  - `play`: triangle (0.25,0.15)-(0.85,0.5)-(0.25,0.85);
  - `pause`: two rounded rects at x 0.22 and 0.58, width 0.2, height 0.7;
  - `prev` / `next`: a bar plus a triangle;
  - `back10` / `fwd10`: a 300° arc with an arrowhead and the text "10" at 0.38 of the box height (text is fine here because it is digits);
  - `volume`: speaker body plus two arcs; `mute`: speaker plus an ×;
  - `fullscreen`: four corner brackets;
  - `subtitles`: rounded rect plus two lines; `audio`: a note glyph path;
  - `search`: circle plus handle;
  - `star`: a five-point polygon; `recent`: an arc plus an arrow;
  - `live_dot`: a filled circle.

  Use a pen width of 0.08 of the box.
- [ ] **Step 4: Verify. Step 5: Lint, type-check, commit** (`feat: add the overlay painting kit and logo cache`).

---

### Task 3: Zap banner (A1) and toast

**Files:**
- Create: `ui/overlays/zap_banner.py`, `ui/overlays/toast.py`
- Test: `tests/test_banner_toast.py`

**Interfaces:**
- Consumes: `Units`, `draw_logo`, `draw_progress`, `fmt_clock` and `elided` (Task 2); `Programme` (Phase 1A Task 2).
- Produces:
  - `BannerInfo`, a dataclass with `number: str`, `name: str`, `logo: QPixmap | None`, `now: Programme | None`, `next: Programme | None`, `upcoming: list[Programme]` (up to 2 after `next`, for the "Next" line), `is_live: bool` and `clock: datetime`.
  - `ZapBanner(QWidget)`, constructed as `ZapBanner(parent=None, visible_ms=4000)`, with:
    - `show_info(info: BannerInfo)`, which shows the banner and restarts the hide timer;
    - `show_digits(text: str)`, which shows the typed digits in the number slot and keeps the banner visible while digits exist (an empty text resumes the normal timer);
    - `dismiss()`;
    - `info -> BannerInfo | None`.
  - `Toast(QWidget)`, constructed as `Toast(parent=None)`, with `show_message(text: str, kind: str = "info", ms: int = 3000)` (`kind` is `"info"` or `"error"`).
- Layout: both widgets size themselves inside their parent (the overlay host) on each show and on parent resize, through an event filter on the parent.
  - **Banner:** occupies the bottom 48% of the host. It is transparent for mouse events (`WA_TransparentForMouseEvents`).
  - **Toast:** a top-centre pill whose width is the text width plus 3.2u and whose top edge is 2.4u from the top. Background `PALETTE.panel` at 0.9, a 1 px border (`PALETTE.text` at 0.12, or `PALETTE.live` for errors), radius 1.2u, font 1.4u.

**Banner painting** (mockup 01-shapes, frame "Zapping"; all values in `u`):
- **Scrim:** a vertical gradient over the banner rect from transparent at the top, to `rgba(5,8,12,0.55)` at 45%, to `rgba(5,8,12,0.92)` at the bottom.
- **Row:** left and right margins 3, bottom margin 3, items gap 2, vertically centred on the block.
- **Number:** font 5.4 at weight Light, `PALETTE.text` at 0.95, column width 9. While digits are pending, the digits are drawn in the accent colour.
- **Logo:** an 8×8 rounded tile with radius 0.9 (`draw_logo`).
- **Body**, from the top:
  1. **Name:** font 2.6 at weight Bold. If `is_live`, a LIVE pill follows 1u after the name: font 1.25 Bold, padding 0.25/0.7, radius 0.5, filled `PALETTE.live`, white text. The clock (`fmt_clock(info.clock)`) is right-aligned on the same line: font 2.1 at weight DemiBold, opacity 0.9.
  2. **Now line**, 0.6 below: font 1.9. `"Now"` in the accent colour, two spaces, the title at weight DemiBold, then `" · HH:MM – HH:MM"`. Without EPG, it reads `"No programme information"` at opacity 0.6 instead.
  3. **Progress bar**, 0.7 below: height 0.45, full body width, fraction `now.progress(clock)`. Omitted without EPG.
  4. **Next line**, 0.7 below: font 1.5 at opacity 0.7, `"Next  HH:MM Title  ·  HH:MM Title"` (next plus the first upcoming). Omitted without EPG.
- **Fade:** a `QGraphicsOpacityEffect` animates from 1 to 0 over 250 ms when the timer fires, then hides the banner.

- [ ] **Step 1: Write failing tests:**
  - `show_info` makes the banner visible.
  - With `visible_ms=60`, it is hidden after `QTest.qWait(400)`.
  - `show_digits("4")` keeps it visible past the timer.
  - `show_digits("")` restarts the timer.
  - `widget.grab()` returns a non-null image both with and without EPG in `BannerInfo`.
  - `Toast.show_message("x", ms=60)` is hidden after 300 ms.
  - The toast width grows with the text length.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the zap banner and toasts`).

---

### Task 4: Channel rows and the fast-zap column (A2)

**Files:**
- Create: `ui/overlays/channel_rows.py`, `ui/overlays/channel_column.py`
- Test: `tests/test_channel_rows.py`

**Interfaces:**
- Consumes: the Task 2 kit, `LogoCache`, and `Programme`.
- Produces in `channel_rows.py`:
  - `ChannelRowsModel(QAbstractListModel)`, constructed as `ChannelRowsModel(logos: LogoCache | None, now_provider: Callable[[dict], tuple[str, float] | None])`, with:
    - `set_channels(items: list[dict])`;
    - `item(row) -> dict`;
    - `refresh_now()`, which clears its now/next cache and emits `dataChanged` for all rows;
    - roles `Qt.DisplayRole` (name), `NumberRole`, `LogoRole` (QPixmap or None), `NowRole` (title or ""), `ProgressRole` (float or -1);
    - it requests logos for rows as they are first asked for `LogoRole` (lazy), and on `LogoCache.logoReady(url)` emits `dataChanged` for the matching rows.
  - `ChannelRowDelegate(QStyledItemDelegate)`, constructed as `ChannelRowDelegate(scale: float = 1.0)`. It paints a row like the mockup's `.row` and `sizeHint` returns a height of 6.6u × scale.
  - `ChannelListView(QListView)`: frameless, transparent viewport, no focus (`Qt.NoFocus`), uniform item sizes, no horizontal scrollbar, vertical scrollbar always off. It has signal `rowClicked(int)` and `units_width` set from the overlay width so the delegate can compute `u`.
- Produces in `channel_column.py`:
  - `ChannelColumn(QWidget)`, constructed as `ChannelColumn(logos, now_provider, parent=None, idle_ms=2500)`, with:
    - `open(title: str, channels: list[dict], index: int)`;
    - `highlight(index: int)` (scrolls to centre and restarts the idle timer);
    - `close()`;
    - signals `closed()` and `rowClicked(int)`.
  - Geometry: the left 40% of the host, full height.

**Row painting** at scale 1 (mockup `.row`; in `u`):
- **Padding** 0.9 vertical / 1.0 horizontal; gap 1.2.
- **Number:** a right-aligned column 3.4 wide, font 1.5 at opacity 0.6.
- **Logo:** 3.6 square, radius 0.9.
- **Text column:**
  - name at font 1.6 DemiBold, elided;
  - now title at font 1.25, opacity 0.65, elided;
  - a progress bar 0.3 high when the progress is 0 or more.
- **Selected row:**
  - background: accent at alpha `FOCUS_FILL_ALPHA` in a rounded rect with radius 1;
  - border: 1 px accent at 0.7.

**Column panel:**
- Background: a horizontal gradient `rgba(6,9,13,0.94)` from 0 to 70%, fading to transparent at 100%.
- Padding: 2.6 top, 2.4 sides.
- Header: the group title at font 1.7 Bold with `‹ ›` at opacity 0.55 on both sides, then 1u spacing before the list.

- [ ] **Step 1: Write failing tests:**
  - **Model:** `rowCount` and the roles; `NowRole` comes from `now_provider`; `LogoRole` returns None without a logo, and asking for it calls `logos.request([url])` once per URL.
  - **Speed:** `set_channels` with 5,000 items completes in under 100 ms (measured with `time.perf_counter`).
  - **Delegate:** `sizeHint` height scales with `units_width`.
  - **Column:** `open` shows and highlights; `highlight(3)` sets the current index; with `idle_ms=60`, the column closes after `QTest.qWait(300)` and emits `closed`; `widget.grab()` is non-null.

  Use a fake `LogoCache` (a QObject with a `logoReady` signal and recording `request` / `pixmap`).
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add channel rows and the fast-zap column`).

---

### Task 5: Two-layer drawer

**Files:**
- Create: `ui/overlays/drawer.py`
- Test: `tests/test_drawer.py`

**Interfaces:**
- Consumes:
  - `NavModel` and `NavNode` (Phase 1A Task 7): `children`, `open`, `up`, `columns`, `is_playable`, `section_of`, `path`, `changed`;
  - `ChannelRowsModel`, `ChannelRowDelegate` and `ChannelListView` (Task 4);
  - the Task 2 kit.
- Produces: `Drawer(QWidget)`, constructed as `Drawer(nav: NavModel, logos, now_provider, parent=None)`. It has:
  - signals `playRequested(object node, object siblings, object parent)` (NavNode, `list[NavNode]`, NavNode) and `closed()`;
  - methods `open_drawer(focus_key: str | None = None)` (shows, focuses the right column, and highlights the node with `focus_key` when present) and `close_drawer()`;
  - key methods `move(delta)`, `page(delta)`, `left()`, `right()`, `activate()`, `type_text(text)`, `backspace()`, `escape() -> bool` (True when the key was consumed by clearing the filter or closing);
  - state `focus_side -> "left" | "right"`, `filter_text -> str`, and `highlighted() -> NavNode | None`.

**Behaviour** (spec §5.2 plus the groups mockup, option 1):
- **Columns:** the drawer always shows `nav.columns()`:
  - **left:** the parent layer, with the current path node highlighted;
  - **right:** the children of the current path end.
  - A `None` children list shows a centred "Loading…" row; an empty list shows "Nothing here".
- **← (`left()`):**
  - with the right column focused, focus moves to the left column;
  - with the left column focused, it calls `nav.up()` (the layers shift right) and keeps focus on the left.
- **→ (`right()`):**
  - with the left column focused, focus moves to the right column;
  - with the right column focused on a non-playable node, it calls `nav.open(node)`, focus stays right, and the new right column shows that node's children;
  - with the right column focused on a playable node, it behaves like `activate()`.
- **Moving the highlight in the left column** calls `nav.open(sibling)`, so the right column previews that sibling's children.
- **`activate()` (Enter):**
  - in the right column on a playable node, emits `playRequested(node, visible_siblings, nav.path[-1])` (the drawer stays open for live; the controller decides);
  - on a non-playable node, behaves like `right()`;
  - in the left column, behaves like `right()`.
- **Filter:**
  - `type_text` appends to the focused column's filter; matching is case-insensitive substring through `str.casefold()` on the title.
  - The filter field is shown at the top of that column: border accent at 0.7, radius 0.9u, the font 1.35u text `"⌕ "` + filter + caret.
  - `backspace` removes one character; `escape` first clears a non-empty filter (returns True), otherwise closes the drawer (returns True).
  - The filter resets whenever the focused column's parent changes.
- **Breadcrumb** at the top of the left column: the titles of `nav.path[:-1]` joined by `" › "`, font 1.15u at opacity 0.6, with the last part in the accent colour.
- **Rows:**
  - `item` nodes in the `live` section use `ChannelRowDelegate` at scale 0.9;
  - every other node uses a simple row: title at font 1.35u, count right-aligned at 1.1u with opacity 0.45, padding 0.7u/0.9u, and the same highlight style. Groups whose key starts with `@` draw their icon (`star` / `recent`) before the title.
- **Geometry:**
  - left column: x 0–25% of the host, background `PALETTE.panel_raised` at 0.95;
  - right column: 25–58%, background `PALETTE.panel` (0.86–0.88);
  - a 7u fade beyond the right edge;
  - both columns full height, padding 2.4u top and 1.6u sides.

- [ ] **Step 1: Write failing tests** using the `FakeCatalog` from `tests/test_navigation.py` (import it) and a real `NavModel`:
  1. `open_drawer("1")` after `nav.reset_to("live", "10")` focuses right and highlights "One".
  2. `move(1)` then `activate()` emits `playRequested` with the node titled "Two", its two siblings and the group node `10`.
  3. `left()` moves focus to the left column; `move(-1)` previews another group (`nav.path[-1]` changes).
  4. `left()` twice calls `nav.up()` (the path shrinks).
  5. `right()` on a section opens it.
  6. `type_text("tw")` filters the right column to "Two"; `escape()` clears the filter and keeps the drawer open; a second `escape()` closes it and emits `closed`.
  7. `type_text("Ş")` matches a node titled "şov" (casefold).
  8. A loading layer shows the "Loading…" row without crashing; `grab()` is non-null.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the two-layer drawer`).

---

### Task 6: Transport (VOD and plain live)

**Files:**
- Create: `ui/overlays/transport.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Produces: `Transport(QWidget)`, constructed as `Transport(parent=None, idle_ms=3000)`. It has:
  - `show_vod(title: str, subtitle: str)`, `show_live(title: str, subtitle: str)`;
  - `update_position(position_ms: int, length_ms: int)`, `set_paused(bool)`, `set_speed(rate: float)`;
  - `poke()` (show and restart the idle timer), `dismiss()`;
  - signals `playPauseRequested()`, `seekRequested(int ms)`, `seekRelativeRequested(int ms)`, `speedRequested(float)`, `fullscreenRequested()`, `audioRequested()`, `subtitleRequested()`.
  - Speeds cycle 1 → 1.25 → 1.5 → 2 → 1 on the speed chip.
- Geometry: the full host, so the top title row and the bottom bar share one widget. `Transport` overrides `Overlay.mask_region()` to return only those two rects (top 22% and bottom 42%), so the middle of the video stays clickable.

**Painting** (mockup 04-controls; values in `u`):
- **Top:** a 22% scrim gradient down to transparent, then a title row at 2.4 from the top and 3 from the sides: title at font 1.8 Bold, subtitle at font 1.5 with opacity 0.75, clock right-aligned at font 1.8 DemiBold.
- **Bottom:** a 42% scrim gradient up from the bottom.
- **VOD bar** (anchored 3 from the bottom and sides):
  - **Seek line:** 1.9 tall, with a track 0.55 thick at `rgba(255,255,255,0.14)`, the played part in the accent colour, and a knob of diameter 1.7 in white with a 0.35 accent ring at 0.45.
  - **Times row:** 1.2 above the line (elapsed left, `-remaining` right) at font 1.25, opacity 0.8.
  - **Buttons row**, 1.2 below the line:
    - icons 2.2 square (`back10`, a play/pause circle 4.6 in diameter filled `PALETTE.text` with a dark icon, `fwd10`), then the speed chip;
    - on the right: subtitles, audio, fullscreen.
    - A click maps to each signal.
- **Live (Phase 1):** no seek line. The bottom right shows the LIVE pill (font 1.15 Bold, `PALETTE.live`). The left of the buttons row shows play/pause and the right shows audio, subtitles and fullscreen.
- **Dragging the seek line** emits `seekRequested(ms)` on release. While dragging, the knob follows and the elapsed text shows the target time.

- [ ] **Step 1: Write failing tests:**
  - `show_vod` plus `update_position(30_000, 120_000)` shows `0:30` and `-1:30` (assert through a `texts()` accessor returning `(elapsed, remaining)`).
  - A click at 75% of the seek line emits `seekRequested(90_000)`.
  - A click on the play button area emits `playPauseRequested`.
  - The speed chip cycles through the four speeds.
  - With `idle_ms=60`, the transport hides after 300 ms; `poke()` re-shows it.
  - In live mode, `texts()` returns `("LIVE", "")` and there is no seek handling.

  Use `QTest.mouseClick` at computed positions through `seek_rect()` and `button_rect(name)` accessors.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the transport bar`).

---

### Task 7: Keymap and TV controller

**Files:**
- Create: `ui/keymap.py`, `ui/tv_controller.py`
- Test: `tests/test_keymap.py`, `tests/test_tv_controller.py`

**Interfaces:**
- Produces in `ui/keymap.py`:
  - `Action(str, Enum)`: `CH_NEXT`, `CH_PREV`, `DIGIT`, `PREVIOUS`, `OPEN_DRAWER`, `SEEK_BACK`, `SEEK_FWD`, `PLAY_PAUSE`, `GO_LIVE`, `GUIDE`, `INFO`, `FULLSCREEN`, `MUTE`, `VOL_UP`, `VOL_DOWN`, `ESCAPE`, `MINI_PLAYER`; and for the drawer `NAV_UP`, `NAV_DOWN`, `NAV_PAGE_UP`, `NAV_PAGE_DOWN`, `NAV_LEFT`, `NAV_RIGHT`, `NAV_ACTIVATE`, `NAV_TEXT`, `NAV_BACKSPACE`.
  - `resolve(key: int, modifiers: Qt.KeyboardModifier, text: str, *, drawer_open: bool) -> tuple[Action, str] | None`. The second element is the payload: the digit or the text.
    - **Drawer open:** arrows, PgUp/PgDn, Enter/Return, Backspace and Esc map to the `NAV_*` actions (Esc becomes `ESCAPE`). Printable text (not digits-only when a modifier other than Shift is held) becomes `NAV_TEXT`. Space becomes `PLAY_PAUSE` and `F` becomes `FULLSCREEN` only with Ctrl.
    - **Drawer closed:** the spec §5.3 table in Global Constraints. ↑ and PgUp give `CH_PREV`; ↓ and PgDn give `CH_NEXT`.
- Produces in `ui/tv_controller.py`:
  - `Overlays`, a dataclass with `banner: ZapBanner`, `column: ChannelColumn`, `drawer: Drawer`, `transport: Transport` and `toast: Toast`.
  - `TVController(QObject)`, constructed as `TVController(*, config, catalog, nav, zap, session, watch, overlays, window_actions, external=None, now=lambda: datetime.now().astimezone())`, where:
    - `window_actions` is an object with `toggle_fullscreen()`, `leave_fullscreen() -> bool` and `toggle_mini_player()`;
    - `external` is a module-like object with `launch_vlc` and `launch_mpv` (default: `services.external_players`).

    Methods and attributes:
    - `start()`;
    - `handle(action: Action, payload: str = "")`;
    - `video_clicked()`, `video_double_clicked()`, `mouse_moved()`, `wheel(steps: int, modifiers)`;
    - `tune(channel: dict, *, group_key: str, channels: list[dict])`;
    - `play_item(node: NavNode, siblings: list[NavNode], parent: NavNode)`;
    - `select_provider(name: str | None = None, *, force_refresh: bool = False)`;
    - `refresh_overlays()`;
    - `now_title(channel) -> tuple[str, float] | None` (used as `now_provider` by rows);
    - `banner_info(channel) -> BannerInfo`;
    - `shutdown()` (flushes watch state, stops playback).

**Behaviour:**
- **`start()`:**
  - wire every signal;
  - call `catalog.select_provider()`;
  - if there are no providers (`catalog.provider_names() == []`), show the toast `"Add a provider in Settings to start watching"` (kind `info`, 8000 ms) and open nothing.
- **On `providerReady`:**
  - if `config.last_live_channel` names this provider, `nav.reset_to("live", record["group_id"] or "*")`; otherwise `nav.reset_to("live")`;
  - remember an autotune target when `config.start_with_last_channel` is set and such a record exists.
- **On `sectionReady("live")`:**
  - with a pending autotune target: build the channels from `catalog.items("live", group)` (falling back to `"*"`), find the target with `ZapContext.same`, and `tune` it;
  - otherwise, if nothing is playing: `drawer.open_drawer()`.
- **On `providerFailed`:** toast the message as an error.
- **`tune(channel, group_key, channels)`:**
  1. Set a new `ZapContext(group_key, channels)` when `group_key` differs from `zap.context.group_key`.
  2. Close the column.
  3. Show the banner immediately with the channel's info.
  4. Remember `(request_id, channel, group_key)` as **the only pending** link, through `catalog.request_link(channel, section="live")`.
  5. On `linkReady(rid, url)` for a different `rid`, ignore it. For the matching one:
     - if `config.play_in_vlc` or `config.play_in_mpv`, launch externally and toast on failure;
     - otherwise call `session.play(url, kind=classify(url, catalog.is_live("live")), latency=config.live_latency, user_agent=channel.get("user_agent"))`;
     - then call `watch.record_play(catalog.current_provider_name(), channel, item_type="channel", link=url, group_id=group_key)` and `zap.tuned(channel)`.
  6. On `linkFailed` for the pending id: toast `f"{channel name}: {message}"` as an error.
- **Zap signals:**
  - `tuneRequested(channel)` calls `tune(channel, group_key=zap.context.group_key, channels=zap.context.channels)`;
  - `columnRequested(True)` calls `column.open(group title, channels, current index)`;
  - `highlightChanged` calls `column.highlight(i)`;
  - `digitsChanged` calls `banner.show_digits(text)`;
  - `numberNotFound(n)` toasts `f"No channel {n}"`.
- **`play_item(node, siblings, parent)`**, from the drawer:
  - **Live section:** `tune(node.payload, group_key=parent.key, channels=[s.payload for s in siblings])`, then close the drawer.
  - **Movies or episodes:**
    1. Request a link with `section="movies"`, or `"series"` with `is_episode=True` for episodes.
    2. On ready, read `cid = watch.content_id(provider, item, item_type=...)` and `start = watch.resume_position(cid)`.
    3. Call `session.play(url, kind=classify(url, False), start_ms=start)`.
    4. If resuming, toast `f"Resumed at {fmt_duration(start)}"`.
    5. Record the play and show the VOD transport with the item name.
- **Session signals:**
  - `positionChanged(ms, length)` for VOD calls `watch.update_position(cid, ms, length)` and `transport.update_position`;
  - `errorOccurred(msg)` toasts an error;
  - `ended` for VOD toasts `"Finished"` and flushes the watch state.
- **`handle` routing:**
  - `CH_NEXT`/`CH_PREV`: `zap.step(±1)`, live only; during VOD they change the volume by ±5.
  - `DIGIT`: `zap.digit(d)`, live only.
  - `PREVIOUS`: `zap.previous()`.
  - `OPEN_DRAWER`: `nav.reset_to("live", zap.context.group_key or "*")`, then `drawer.open_drawer(focus_key=current channel key)`.
  - `SEEK_*`: `session.seek_relative(∓10000)` when `session.is_seekable()`, else toast `"Rewind arrives with time-shift"`.
  - `PLAY_PAUSE`: `session.toggle_pause()`.
  - `INFO`: show the banner for the current channel.
  - `GO_LIVE` and `GUIDE`: toast `"Coming soon"`.
  - `FULLSCREEN` and `MINI_PLAYER`: `window_actions`.
  - `MUTE` and `VOL_*`: the session, plus a toast `f"Volume {v}%"` or `"Muted"`.
  - `ESCAPE`: close the drawer if open, else the column if open, else the banner if visible, else `window_actions.leave_fullscreen()`.
  - `NAV_*` go to the drawer: `NAV_UP` is `move(-1)`, `NAV_PAGE_*` is `page(±1)`, `NAV_LEFT`/`NAV_RIGHT` are `left()`/`right()`, `NAV_ACTIVATE` is `activate()`, `NAV_TEXT` is `type_text`, `NAV_BACKSPACE` is `backspace()`.
- **Mouse:**
  - `video_clicked()` toggles the drawer (close if open, otherwise `OPEN_DRAWER`);
  - `video_double_clicked()` calls `window_actions.toggle_fullscreen()`;
  - `mouse_moved()` calls `transport.poke()` when something is playing;
  - `wheel(steps, mods)`: with Ctrl/⌘ or `config.mouse_wheel_action == "volume"`, volume ±5 per step; otherwise `zap.step(±1)` for each step, live only.
- **`now_title(channel)`:** `catalog.programmes(channel, now(), limit=1)` gives `(title, progress)` or None.

- [ ] **Step 1: Write failing tests:**
  - `tests/test_keymap.py`: one assertion per row of the key table above, plus the drawer-open variants (↑ becomes `NAV_UP`, `a` becomes `NAV_TEXT`, Esc becomes `ESCAPE`, Enter becomes `NAV_ACTIVATE`).
  - `tests/test_tv_controller.py`, using fakes:
    - a `FakeCatalog` (QObject with every signal used; `request_link` returns increasing ids and records them);
    - a `FakeSession` (QObject with `stateChanged`, `positionChanged`, `errorOccurred`, `ended`, recording `play` / `stop` / `seek_relative` and returning `is_seekable` from a flag);
    - a real `ZapController(rest_ms=20, burst_ms=1000, digits_ms=30)`;
    - a real `NavModel` over the fake catalog;
    - recording fakes for each overlay and for `window_actions`;
    - a `SimpleNamespace` config;
    - a fake watch recording calls.

    Cases:
    1. Two quick tunes: only the second `linkReady` plays, and the first's late `linkReady` is ignored.
    2. Autotune on start from `last_live_channel`.
    3. No providers gives the welcome toast.
    4. `CH_NEXT` zaps within the context.
    5. `OPEN_DRAWER` then `ESCAPE` closes the drawer.
    6. A VOD play with resume calls `session.play(..., start_ms=resume)` and toasts.
    7. `play_in_vlc` calls `external.launch_vlc(url)` and does not call `session.play`.
    8. Wheel with the volume setting changes the volume and does not zap.
    9. A link failure toasts an error.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: add the keymap and TV controller`).

---

### Task 8: TV window, startup and settings

**Files:**
- Create: `ui/tv_window.py`
- Modify: `main.py`, `options.py`
- Test: `tests/test_tv_window.py`

**Interfaces:**
- Consumes everything above.
- Produces `TVWindow(QMainWindow)`, constructed as `TVWindow(app, config_manager, provider_manager, image_manager, epg_manager, *, vlc_instance=None)`.
  - **Construction:**
    - builds the `VideoSurface` (central widget), `OverlayHost`, `LogoCache` and the overlays;
    - builds `CatalogService`, `NavModel`, `ZapController`, `PlaybackSession` (with `vlc_instance` or `create_instance()`), `WatchState` and `TVController`;
    - calls `session.set_window(surface.native_handle())` after the window is first shown;
    - adds a menu bar:
      - *QiTV*: Settings… (Ctrl+,), Quit (Ctrl+Q);
      - *View*: Fullscreen (F), Channel list (Return), Mini player (Alt+P);
      - *Playback*: Play/Pause (Space), Mute (M), Previous channel (Backspace).
      Menu actions call `controller.handle(...)`. The menu's shortcuts must not double-fire with `keyPressEvent`, so use `Qt.ApplicationShortcut` only on the menu actions and let `keyPressEvent` handle everything else.
  - `keyPressEvent` calls `resolve(...)`, then `controller.handle`.
  - `window_actions`:
    - `toggle_fullscreen` / `leave_fullscreen`;
    - `toggle_mini_player`: frameless, stays on top, 25% of the screen width at 16:9 in the bottom-right corner; toggling again restores the geometry (ported from `VideoPlayer.toggle_pip_mode`).
  - Geometry is restored from and saved to `window_positions["tv_window"]` via `config_manager.apply_window_settings` and `save_window_settings`.
  - **`closeEvent`:**
    - call `controller.shutdown()`, `image_manager.save_index()`, `epg_manager.save_index()` and save the geometry;
    - if `services.thread_cleanup.has_pending_threads()`, ignore the close and retry every 50 ms, then call `app.quit()` when none are pending (as `ChannelList.closeEvent` did);
    - release the session.
  - **Hosting `OptionsDialog`** needs `config_manager`, `provider_manager`, `epg_manager` and `image_manager` attributes plus:
    - `save_config()`: calls `config_manager.save_config()`;
    - `set_provider(force_update=False)`: calls `controller.select_provider(force_refresh=force_update)`;
    - `refresh_channels()`: calls `controller.refresh_overlays()`;
    - `refresh_content_list_size()`: a no-op.
- `main.py` builds `TVWindow` in place of `VideoPlayer` and `ChannelList`:
  - keep logging, the Windows `--replace` handling, `prevent_sleep` / `allow_sleep` and `check_for_updates`;
  - call `qdarktheme.setup_theme("dark", custom_colors={"primary": "#C96B43"})`;
  - remove the two delayed raise/activate timers;
  - import `multiprocessing` and call `multiprocessing.freeze_support()` first in `main()` (needed by Phase 2's worker).
- `options.py`:
  - remove the *Keyboard/Remote Mode* checkbox and its save line (keep the config key harmless);
  - add, in the Settings tab: *Live latency* (QComboBox Low/Normal/Stable mapping to `low`/`normal`/`stable`), *Mouse wheel* (Change channel / Volume, mapping to `channel`/`volume`) and *Start with the last channel* (QCheckBox), each loaded from and saved to the Phase 1A config properties;
  - fix `get_cache_size` to use `os.path.join(self.config_manager.get_config_dir(), "cache")` instead of `self.parent().get_cache_directory()`.

- [ ] **Step 1: Write failing tests** in `tests/test_tv_window.py`, offscreen, with `QITV_CONFIG_DIR` pointing to a temp dir and the providers patched to a single local M3U provider:
  - The fixture writes a playlist with two entries whose URLs are `file://` URLs of `tests/fixtures/smoke.mp4`, and `config.json` selects that provider. Seed `cache/provider/index.json` the way `ProviderManager._load_providers` reads it (inspect that method).
  - Pass `vlc_instance=create_instance(["--vout=dummy", "--aout=dummy", "--quiet"])`, or skip if libVLC is missing.

  Cases:
  1. The window constructs and shows, and the overlay host exists.
  2. Within 10 s the drawer opens (no last channel), and pressing Enter on the first channel starts the session (state OPENING or PLAYING).
  3. Pressing Down (`QTest.keyClick(window, Qt.Key_Down)`) tunes the second channel (the session's URL changes).
  4. `OptionsDialog`'s host methods exist: `hasattr(window, name)` for the four names.
  5. `closeEvent` saves `window_positions["tv_window"]`.
- [ ] **Steps 2–5:** run RED, implement, run GREEN, run the suite, lint/type, commit (`feat: start QiTV in the single TV window`). Also run the app for real once (`QITV_CONFIG_DIR=$(mktemp -d) uv run python main.py`, with a configured provider) and describe in the report what you saw. This is a manual check, not a test.

---

### Task 9: Smoke test and CI gate

**Files:**
- Create: `scripts/smoke_tv.py`
- Modify: `.github/workflows/main.yml` (a smoke step in each build job, after "Run tests")
- Test: the script itself, run locally

**Interfaces:**
- Produces `uv run python scripts/smoke_tv.py --out <dir> [--seconds 6]`. It exits 0 on success and non-zero with a JSON report on failure. It writes `<dir>/smoke.json` and screenshots `<dir>/01-drawer.png`, `<dir>/02-banner.png` and `<dir>/03-fullscreen.png` using `QWidget.grab()` of the overlay host composited over a black image. (OS screen capture needs permissions, so it is not used.)

**Scenario** (isolated `QITV_CONFIG_DIR`, the local M3U provider from Task 8's fixture recipe, a real `TVWindow`):
1. Wait until the drawer is open. Check that the host geometry equals the surface's global rect.
2. Activate the first channel and wait for the session to reach PLAYING (fail after 15 s).
3. Press Down and wait for the second channel to be playing. Check that the banner is visible and that `zap.context.current()` is the second channel.
4. Move the window by (60, 40) and resize it to 1100×620. Check the host geometry again after 100 ms.
5. Toggle fullscreen, wait 1 s and check the host geometry, then toggle back.
6. Close the window and wait for a clean exit (no pending threads, exit code 0 within 10 s).

**Failure output:** every check records `{"name", "ok", "detail"}` into `smoke.json`. Any failed check gives a non-zero exit.

**CI:**
- **Linux:** run under `xvfb-run -a -s "-screen 0 1920x1080x24"`. Install `xvfb` in the existing apt step.
- **macOS and Windows:** run it directly.
- Upload `smoke-out/` as an artifact named `smoke-<os>` with `if: always()`.
- **Overlay gate:** if the overlay geometry checks fail on Windows or Linux, CI marks the job failed. Per spec D4, the Qt-drawn frame fallback is then built for that platform in a follow-up task. This plan does not build it pre-emptively.

- [ ] **Steps:**
  1. Write the script.
  2. Run it locally on macOS and inspect the three screenshots (attach the paths in the report).
  3. Add the CI steps, then validate the YAML with the same PyYAML snippet used in Phase 0 Task 9 (assert that the step named `Smoke test` follows `Run tests` in every job).
  4. Commit (`test: add the TV shell smoke test and run it in CI`).
