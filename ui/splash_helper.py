"""The splash: QiTV's mark, drawn by a small process of its own from the moment QiTV starts
until its window opens into it.

main() builds the window on its GUI thread before the event loop runs, so it can't animate
anything meanwhile. Splash.start() spawns run_splash (multiprocessing, "spawn"), which loops the
mark (ui.opening_mark) in a frameless, translucent window that stays on top, centred on the
primary screen. Once the shown window is on screen, open_into(window, overlay) either tells it
"open" (the mark completes its draw and opens out, and the window fades in as it does) or, on a
file's start, where the player page draws the same mark on the same loop, "handover": the
splash glides onto the player's mark as the window fades in, and the player's own mark takes
over in place, one mark from the double-click into the player. close() tells it "quit": a
start that handed over, or QiTV's end.

The pipe carries ("open", frame), ("handover", rect), ("bare",) and ("quit",) to the helper
(rectangles as (x, y, width, height)), and ("shown", loop origin), ("opening",) and
("arrived", rect) back. In QiTV, a watcher
thread is the pipe's only user: it sends what the GUI thread queued, calls `on_shown` (on that
thread) at "shown", passes "opening" on to the GUI thread, and joins the helper, killing it
KILL_AFTER_S after its first command if it hasn't ended by then. The helper ends by itself once
it finds the pipe closed (QiTV died, or ended before its window showed).

Over PyInstaller's bootloader splash (the Windows and Linux one-file apps show the build's
still, scripts/render_splash.py, while they unpack), start(lit=True) has the helper's mark begin
on that held, lit frame, in the same 240 px square centred as Tk centres it: once "shown", QiTV
closes the bootloader's (main.BootloaderSplash, through `on_shown`, or `on_end` if the helper
ends unshown).

The helper process imports PySide6's GUI modules and the mark only (and ui.macos_window on
macOS, where it takes no Dock icon or menu bar).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
import logging
import math
import multiprocessing
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
import os
import queue
import signal
import sys
import threading
import time
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
    Slot,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from ui.opening_mark import LOOP_S, STAGE_PX, STILL_T, OpeningMark

logger = logging.getLogger(__name__)

POLL_MS = 20  # how often the helper reads its pipe; the watcher waits as long at most
KILL_AFTER_S = 3.0  # after the first command: a helper still running then is killed
FADE_IN_MS = 350  # the window's fade as the mark opens into it, and the mark's glide
OPENING_WAIT_MS = 600  # after "open": the window fades even if "opening" never came
WINDOW_WAIT_MS = 500  # the most the open waits for the window to be on screen
# The swap onto the player's mark: once the player's mark has painted (PAINT_WAIT_MS at most),
# the splash's stops drawing its light ("bare"), and quits HANDOVER_OVERLAP_MS later. Meanwhile
# the pipe is read every SWAP_POLL_MS at both ends, so the light is drawn twice a moment only.
PAINT_WAIT_MS = 100
SWAP_POLL_MS = 5
HANDOVER_OVERLAP_MS = 50
HANDOVER_LIMIT_MS = 1000  # after its glide: a helper never told to quit ends by itself
GLIDE_MAX_PX = 160  # farther (another screen, a window off the middle): it opens out instead
FADING_PLATFORMS = ("xcb", "windows", "cocoa")  # Qt platforms that fade a window
# NSApplicationActivationPolicyAccessory: no Dock icon and no menu bar, windows allowed.
NS_ACTIVATION_POLICY_ACCESSORY = 1
# Without it, Qt makes a plain executable a Dock app as it starts (macOS).
_MAC_NO_FOREGROUND = "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"
# How QiTV's end stops its children (multiprocessing's exit handler, tv_window's): no failure.
_TERMINATED = (-signal.SIGTERM, 0x10000)

Geometry = tuple[int, int, int, int]  # x, y, width, height


# ---- in QiTV ---------------------------------------------------------------------------------


class Splash:
    """The splash as QiTV drives it, from its GUI thread: start(), then open_into(window) or
    close(). Each does nothing once the splash didn't start; none of them waits.

    `on_shown` is called once, on the watcher thread (it must not touch Qt), when the helper's
    window has painted; never when it doesn't show. `on_end` is called once there too, when the
    helper has ended, whether it showed or not.
    """

    def __init__(
        self,
        on_shown: Optional[Callable[[], None]] = None,
        on_end: Optional[Callable[[], None]] = None,
    ) -> None:
        self._on_shown = on_shown
        self._on_end = on_end
        self._commands: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._relay = _Relay(self._heard)
        self._process: Optional[BaseProcess] = None
        self._watcher: Optional[threading.Thread] = None
        # The helper's mark's loop origin (time.monotonic), from its "shown": the watcher
        # writes it, the GUI thread reads it.
        self._origin: Optional[float] = None
        self._opened = False
        self._closed = False
        self._ended = False  # the helper has ended
        self._transparent: Optional[QWidget] = None  # the window before_show made transparent
        self._layers: list[QWidget] = []  # it and its own top-levels, fading in as one
        self._place: Optional[QRect] = None  # where the helper's window shows
        self._window: Optional[QWidget] = None  # the window, once on screen (_drawn)
        self._overlay: Any = None  # the player's mark (ui.overlays.opening.OpeningOverlay)
        self._fading = False
        self._faded_in = False  # the window's fade has ended
        self._handing = False  # the splash's mark glides onto the player's
        self._target: Optional[QRect] = None  # where it glides: the player's mark on screen
        self._arrived = False  # the helper said its glide reached _target
        self._swapping = False

    def start(self, geometry: Optional[QRect] = None, *, lit: bool = False) -> bool:
        """Spawns the helper, its window at `geometry` (by default 240 px in the middle of the
        primary screen, where the bootloader's splash is), its mark's loop beginning on the
        held, lit frame when `lit` (it shows over the bootloader's splash, that frame). False,
        and logged, when it couldn't: QiTV starts without a splash."""
        if self._process is not None:
            return False
        conn = child = process = None
        try:
            rect = geometry if geometry is not None else centred_on_primary()
            place = (rect.x(), rect.y(), rect.width(), rect.height())
            context = multiprocessing.get_context("spawn")
            conn, child = context.Pipe(duplex=True)
            process = context.Process(
                target=run_splash, args=(child, place, lit), name="qitv-splash", daemon=True
            )
            process.start()
            child.close()  # the helper's end of the pipe is its own: its exit shows up as EOF
            watcher = threading.Thread(
                target=self._watch, args=(process, conn), name="qitv-splash", daemon=True
            )
            watcher.start()
        except Exception as error:  # never a reason for QiTV not to start
            logger.warning("The splash could not be started (%s).", type(error).__name__)
            if process is not None and process.pid is not None:
                process.kill()
            for end in (conn, child):
                if end is not None:
                    end.close()
            return False
        self._process, self._watcher, self._place = process, watcher, QRect(rect)
        return True

    def before_show(self, window: QWidget) -> None:
        """Before the window first shows: it starts transparent, with the top-levels that show
        with it (the player page's OverlayHost: its title, controls and toasts), to fade in as
        one as the splash opens into it (open_into). Where the platform fades no window, they
        show as they are."""
        if self._process is not None and self._transparent is None and _fades_windows():
            self._layers = [window, *glued_layers(window)]
            for layer in self._layers:
                layer.setWindowOpacity(0.0)
            self._transparent = window

    def open_into(self, window: QWidget, overlay: Any = None) -> None:
        """The window shows. Once it is on screen (_WhenDrawn: its first paint can hold the
        event loop a quarter of a second), the splash either opens, and the window fades in as
        it does ("opening"), or, while `overlay` (the player page's OpeningOverlay) draws a
        file's mark on the splash's loop, glides onto that mark as the window fades in, and
        the player's own mark takes over: one mark from the double-click into the player.

        Until then the player's mark draws unseen, on the splash's loop (same frame)."""
        if self._process is None:
            return
        if overlay is not None:
            self._overlay = overlay
            overlay.loop_origin = lambda: self._origin
            overlay.hold_mark(True)
        _WhenDrawn(window, lambda: self._drawn(window))

    def close(self) -> None:
        """The splash ends at once."""
        if self._process is not None and not self._closed:
            self._closed = True
            self._commands.put(("quit",))

    # ---- the window's opening (GUI thread) ----

    def _drawn(self, window: QWidget) -> None:
        self._window = window
        target = self._hand_over_to()
        if self._ended or self._closed:  # no splash to open (any more)
            self._let_go()
            self._fade()
        elif target is not None:
            self._handing = True
            self._glide_to(target)
            self._fade()  # the window fades in as the mark glides
        else:
            # The splash's mark opens out; the player's stays unseen until it has gone (the
            # watcher's "ended"): one mark at a time.
            self._send_open()
            # The window fades as the mark opens out ("opening", after its draw completes),
            # never later than this.
            QTimer.singleShot(OPENING_WAIT_MS, window, self._fade)

    def _hand_over_to(self) -> Optional[QRect]:
        """Where the splash's mark glides to, the player's on screen; None to open out: the
        player's mark draws no file's opening on the splash's loop, the system asks for less
        motion (nothing moves: the open is a plain fade), it is farther than GLIDE_MAX_PX, or
        the splash's window can't place itself (Wayland)."""
        overlay = self._overlay
        if (
            overlay is None
            or not overlay.opening()
            or self._origin is None
            or overlay.mark.origin != self._origin
            or OpeningMark.reduced_motion()
            or _platform_name().startswith("wayland")
        ):
            return None
        target = self._mark_on_screen()
        return target if self._near(target) else None

    def _mark_on_screen(self) -> QRect:
        mark = self._overlay.mark
        return QRect(mark.mapToGlobal(QPoint(0, 0)), mark.size())

    def _near(self, target: QRect) -> bool:
        place = self._place
        if place is None:
            return False
        return math.hypot(target.x() - place.x(), target.y() - place.y()) <= GLIDE_MAX_PX

    def _glide_to(self, target: QRect) -> None:
        self._target, self._arrived = target, False
        self._commands.put(("handover", (target.x(), target.y(), target.width(), target.height())))

    def _follow(self) -> None:
        """The player's mark moved (its OverlayHost follows the window as the window manager
        places it, after the window was on screen): the splash glides on to where it is now;
        too far, it opens out instead."""
        target = self._mark_on_screen()
        if target == self._target:
            return
        if self._near(target):
            self._glide_to(target)
        else:
            self._handing = False
            self._send_open()

    def _send_open(self) -> None:
        window = self._window
        if window is not None and not self._opened and not self._closed:
            self._opened = True
            frame = window.frameGeometry()
            self._commands.put(("open", (frame.x(), frame.y(), frame.width(), frame.height())))

    def _let_go(self) -> None:
        """The player's mark shows again (where it still draws), and a later file's draws from
        its beginning, as before the splash."""
        if self._overlay is not None:
            self._overlay.hold_mark(False)
            self._overlay.loop_origin = lambda: None

    def _fade(self) -> None:
        """The window fades in, once; where it doesn't fade, its fade's time passes."""
        window = self._window
        if self._fading or window is None:
            return
        self._fading = True
        if self._transparent is window:
            self._transparent = None
            fade_in(self._layers, step=lambda _level: self._fade_step(), done=self._faded)
        else:
            QTimer.singleShot(FADE_IN_MS, window, self._faded)

    def _fade_step(self) -> None:
        """Through the hand-over: the player's mark still draws (or the splash opens out),
        and the splash glides to where it is."""
        overlay = self._overlay
        if not self._handing or overlay is None:
            return
        if not overlay.opening():
            # The file showed its picture (or failed) during the glide: the splash's mark
            # opens out where it is; the player's has opened out unseen (it shows again only
            # once the splash has gone).
            self._handing = False
            self._send_open()
        elif not self._swapping:
            self._follow()

    def _faded(self) -> None:
        self._faded_in = True
        self._fade_step()
        self._swap_when_ready()

    def _swap_when_ready(self) -> None:
        """Once the window has faded in and the splash's mark stands on the player's: the
        player's shows under it (same place and frame); once that is on screen, the splash's
        draws its opaque tile and glyph only, so the light is the player's, never missing (a
        moment on the shared clock for both, a real start showed, blinked it off a frame:
        QiTV's loop is the busier) and doubled a moment only."""
        self._fade_step()
        if self._handing and self._faded_in and self._arrived and not self._swapping:
            self._swapping = True
            self._let_go()
            _AfterPaint(self._overlay.mark, self._relay, self._swap)

    def _swap(self) -> None:
        self._fade_step()  # the player's mark may have gone meanwhile: the splash opens out
        if self._handing:
            self._handing = False
            self._commands.put(("bare",))
            QTimer.singleShot(HANDOVER_OVERLAP_MS, self._relay, self.close)

    def _heard(self, message: tuple[Any, ...]) -> None:
        """What the watcher heard, on the GUI thread."""
        if message[0] == "opening":
            self._fade()
        elif message[0] == "arrived":
            target = self._target
            if target is not None and message[1] == (
                target.x(),
                target.y(),
                target.width(),
                target.height(),
            ):
                self._arrived = True
                self._swap_when_ready()
        elif message[0] == "ended":
            self._ended = True
            if self._window is not None:  # nothing glides or opens any more
                self._handing = False
                self._let_go()  # the player's mark, if it draws, now the only one
                self._fade()

    # ---- the watcher thread ----

    def _watch(self, process: BaseProcess, conn: Connection) -> None:
        """The pipe's only user, until the helper has ended."""
        deadline: Optional[float] = None
        shown = False
        try:
            while True:
                deadline = self._send_queued(conn, deadline)
                timeout = (SWAP_POLL_MS if self._handing else POLL_MS) / 1000
                if deadline is not None:
                    timeout = max(0.0, min(timeout, deadline - time.monotonic()))
                ready = wait([conn, process.sentinel], timeout)
                if conn in ready:
                    try:
                        message = conn.recv()
                    except EOFError, OSError:
                        break  # the helper closed its end: it is ending
                    name = message[0] if isinstance(message, tuple) and message else None
                    if name == "shown" and not shown:
                        shown = True
                        if len(message) > 1 and isinstance(message[1], float):
                            self._origin = message[1]
                        self._call(self._on_shown, "shown")
                    elif name == "opening":
                        self._relay.tell(("opening",))
                    elif name == "arrived" and len(message) > 1:
                        self._relay.tell(("arrived", tuple(message[1])))
                if process.sentinel in ready:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
            self._reap(process, deadline)
        except Exception:
            logger.warning("The splash's watcher failed.", exc_info=True)
            if process.is_alive():
                process.kill()
        finally:
            conn.close()
            self._call(self._on_end, "end")
            self._relay.tell(("ended",))

    def _send_queued(self, conn: Connection, deadline: Optional[float]) -> Optional[float]:
        """Sends what the GUI thread queued; the kill's deadline from the first of it."""
        while True:
            try:
                message = self._commands.get_nowait()
            except queue.Empty:
                return deadline
            try:
                conn.send(message)
            except OSError, ValueError:
                pass  # the helper is gone: its join comes next
            if deadline is None:
                deadline = time.monotonic() + KILL_AFTER_S

    @staticmethod
    def _call(hook: Optional[Callable[[], None]], name: str) -> None:
        if hook is None:
            return
        try:
            hook()
        except Exception:
            logger.warning("The splash's %s hook failed.", name, exc_info=True)

    @staticmethod
    def _reap(process: BaseProcess, deadline: Optional[float]) -> None:
        """Waits for the helper's end; one still running at the deadline (KILL_AFTER_S after
        its first command, or after it closed its pipe) is killed. Its end is read from its
        sentinel, whoever reaps it: QiTV's exit may end it too (multiprocessing's exit handler,
        tv_window._end_child_processes), which is also why the Process is left unclosed."""
        if deadline is None:
            deadline = time.monotonic() + KILL_AFTER_S
        killed = not wait([process.sentinel], max(0.0, deadline - time.monotonic()))
        if killed:
            logger.warning("The splash did not end; it is stopped.")
            process.kill()
        # Reaped here, unless QiTV's exit did: its sentinel closes a moment before its exit.
        process.join(1.0)
        code = process.exitcode
        if not killed and code not in (None, 0, *_TERMINATED):
            # Its own lines went to its stderr, which a packaged QiTV has not.
            logger.warning("The splash ended with exit code %s.", code)


class _Relay(QObject):
    """Carries what the watcher thread hears to the GUI thread (a queued signal: the thread
    makes no Qt object and starts no timer)."""

    heard = Signal(object)

    def __init__(self, handler: Callable[[tuple[Any, ...]], None]) -> None:
        super().__init__()
        self._handler = handler
        self.heard.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def tell(self, message: tuple[Any, ...]) -> None:
        try:
            self.heard.emit(message)
        except RuntimeError:  # QiTV's end took it already
            pass

    @Slot(object)
    def _deliver(self, message: tuple[Any, ...]) -> None:
        self._handler(message)


class _WhenDrawn(QObject):
    """Calls `then` once, when `window` is on screen with its first paint done: the event
    loop's turn after its expose (that paint can hold the loop a quarter of a second), or
    after WINDOW_WAIT_MS whatever came (a platform that holds back a transparent window's)."""

    def __init__(self, window: QWidget, then: Callable[[], None]) -> None:
        super().__init__(window)
        self._then: Optional[Callable[[], None]] = then
        self._handle = window.windowHandle()
        # Counted from the loop's first turn: main()'s work after show() comes first.
        QTimer.singleShot(0, self, lambda: QTimer.singleShot(WINDOW_WAIT_MS, self, self._go))
        if self._handle is None or self._handle.isExposed():
            QTimer.singleShot(0, self, self._go)
        else:
            self._handle.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Expose and self._handle is not None:
            if self._handle.isExposed():
                QTimer.singleShot(0, self, self._go)  # after the paint this expose brings
        return False

    def _go(self) -> None:
        then, self._then = self._then, None
        if then is None:
            return
        if self._handle is not None:
            self._handle.removeEventFilter(self)
        self.deleteLater()
        then()


class _AfterPaint(QObject):
    """Calls `then` once, on the event loop's turn after `widget` paints (its frame is out by
    then), or PAINT_WAIT_MS on whatever came (a widget that doesn't paint: it went)."""

    def __init__(self, widget: QWidget, owner: QObject, then: Callable[[], None]) -> None:
        super().__init__(owner)
        self._widget = widget
        self._then: Optional[Callable[[], None]] = then
        widget.installEventFilter(self)
        widget.update()
        QTimer.singleShot(PAINT_WAIT_MS, self, self._go)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Paint:
            QTimer.singleShot(0, self, self._go)
        return False

    def _go(self) -> None:
        then, self._then = self._then, None
        if then is None:
            return
        if isValid(self._widget):
            self._widget.removeEventFilter(self)
        self.deleteLater()
        then()


def centred_on_primary(size: int = STAGE_PX) -> QRect:
    """A `size` square in the middle of the primary screen, rounded as PyInstaller's splash
    script places its image there (int(x + 0.5 * (width - size)); QRect.moveCenter lands a
    pixel off it on a screen of odd size): the helper's window covers the bootloader's."""
    rect = QRect(0, 0, size, size)
    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        area = screen.geometry()
        rect.moveTopLeft(
            QPoint(
                area.x() + int(0.5 * (area.width() - size)),
                area.y() + int(0.5 * (area.height() - size)),
            )
        )
    return rect


def ease_in_out() -> QEasingCurve:
    """CSS's ease-in-out, as the mark's own open eases."""
    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.42, 0.0), QPointF(0.58, 1.0), QPointF(1.0, 1.0))
    return curve


def glued_layers(window: QWidget) -> list[QWidget]:
    """The window's own top-levels that show with it: its Tool windows (the player page's
    OverlayHost, glued over the video)."""
    return [
        child
        for child in window.findChildren(QWidget)
        if child.isWindow() and child.windowType() == Qt.WindowType.Tool
    ]


def fade_in(
    windows: Sequence[QWidget],
    duration_ms: int = FADE_IN_MS,
    *,
    step: Optional[Callable[[float], None]] = None,
    done: Optional[Callable[[], None]] = None,
) -> None:
    """The windows' opacity from 0 to 1 together over `duration_ms`, eased as the mark's own
    open; they always end at 1. `step` is called after each change, `done` at the end."""

    def change(level: Any) -> None:
        for window in windows:
            if isValid(window):
                window.setWindowOpacity(float(level))
        if step is not None:
            step(float(level))

    def end() -> None:
        change(1.0)
        if done is not None:
            done()

    animation = QVariantAnimation(windows[0])
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setDuration(duration_ms)
    animation.setEasingCurve(ease_in_out())
    animation.valueChanged.connect(change)
    animation.finished.connect(end)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)


def _fades_windows() -> bool:
    """Whether this Qt platform fades a top-level window (X11's needs a compositor for it to
    show, and shows the window as it is without one); offscreen, Wayland and the others don't."""
    return _platform_name() in FADING_PLATFORMS


# ---- the helper process ----------------------------------------------------------------------


def run_splash(conn: Connection, geometry: Geometry, lit: bool = False) -> None:
    """The helper process (multiprocessing's spawn target): the mark at `geometry` until "open"
    has played, or "quit" or a closed pipe ends it; `lit`, its loop begins on the held, lit
    frame. It ends with os._exit, so nothing in the interpreter's teardown keeps it going once
    its window is hidden."""
    code = 0
    try:
        if sys.platform == "darwin":
            os.environ[_MAC_NO_FOREGROUND] = "1"  # it becomes an accessory app instead
        application = QApplication.instance() or QApplication(["qitv-splash"])
        window = _SplashWindow(conn, geometry, application.quit, lit=lit)
        if window.begin():
            application.exec()
    except Exception:
        logger.exception("The splash failed")
        code = 1
    finally:
        try:
            conn.close()
        except OSError:
            pass
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                try:
                    stream.flush()
                except OSError, ValueError:
                    pass
    os._exit(code)


class _SplashWindow(OpeningMark):
    """The helper's window: the mark, and the pipe it reads every POLL_MS (its GUI thread).

    begin() shows it, unless a command or the pipe's end came first (nothing shows then).
    ("shown", origin) goes back once it has painted: its loop's origin (time.monotonic), which
    the player's mark follows. "open" finishes the mark (the draw completes, then it opens out)
    and ends; ("opening",) goes back as it opens out. ("handover", rect) glides the window onto
    rect (the player's mark, on the same loop) over FADE_IN_MS, looping on, reading its pipe
    every SWAP_POLL_MS; ("arrived", rect) goes back once there. Another "handover" glides it
    on from where it is (the player's mark moved). "bare" (the player's mark shows under it):
    once there, it draws only its tile and glyph. It then waits for "quit", or "open" (the
    player's mark went), and ends by itself HANDOVER_LIMIT_MS after its first glide. "quit" and
    a closed pipe end it at once. `on_end` quits the app. `lit`: its loop begins on the held,
    lit frame (STILL_T), the bootloader's splash it shows over.
    """

    def __init__(
        self,
        conn: Connection,
        geometry: Geometry,
        on_end: Callable[[], None],
        *,
        lit: bool = False,
    ) -> None:
        super().__init__()
        self._conn = conn
        self._on_end = on_end
        self._lit = lit
        self._opening = False
        self._gliding = False
        self._arrived = False  # its glide has ended
        self._bare_due = False  # the player's mark shows under it
        self._glide: Optional[QVariantAnimation] = None
        self._limit_set = False  # its own end, HANDOVER_LIMIT_MS after its first glide
        self._ended = False
        self._painted = False
        self.setWindowFlags(
            Qt.WindowType.SplashScreen
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # macOS: a splash screen is a panel, which an app that is never active would hide.
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setGeometry(QRect(*geometry))
        self._poller = QTimer(self)
        self._poller.setInterval(POLL_MS)
        self._poller.timeout.connect(self._poll)

    def begin(self) -> bool:
        """Shows the splash and starts its loop; False when it ended before showing."""
        self._poll()
        if self._ended:
            return False
        if _platform_name() == "cocoa":
            _hide_from_dock()  # before the window shows: no Dock icon, no menu bar
        self.show()
        # Over the bootloader's still: from that frame on (held lit, gone, drawn again).
        self.start(time.monotonic() - STILL_T * LOOP_S if self._lit else None)
        self._poller.start()
        return True

    def _poll(self) -> None:
        try:
            while not self._ended and self._conn.poll():
                self._handle(self._conn.recv())
        except EOFError, OSError:
            # QiTV closed its end or died (Windows can report that from poll() too)
            self._quit_splash()

    def _handle(self, message: Any) -> None:
        name = message[0] if isinstance(message, tuple) and message else None
        if name == "quit" or (name in ("open", "handover") and not self.isVisible()):
            self._quit_splash()
        elif name == "open" and not self._opening:
            self._opening = True
            self.finish(self._quit_splash)
            # It opens out once its draw has completed (OpeningMark.finish's catch-up).
            catch_up = self._finish.catch_up_s if self._finish is not None else 0.0
            QTimer.singleShot(round(catch_up * 1000), self, lambda: self._say(("opening",)))
        elif name == "handover" and not self._opening:
            # (again: the player's mark moved, as the window manager placed its window)
            self._gliding = True
            self._poller.setInterval(SWAP_POLL_MS)  # the swap's "bare" is read at once
            self._glide_to(QRect(*message[1]))
        elif name == "bare" and self._gliding:
            self._bare_due = True
            if self._arrived:
                self._go_bare()

    def _glide_to(self, target: QRect) -> None:
        """From where it is to `target` over FADE_IN_MS; a glide under way gives way."""
        if self._glide is not None and isValid(self._glide):
            self._glide.stop()  # it deletes itself
        self._arrived = False
        glide = QVariantAnimation(self)
        glide.setStartValue(self.geometry())
        glide.setEndValue(target)
        glide.setDuration(FADE_IN_MS)
        glide.setEasingCurve(ease_in_out())
        glide.valueChanged.connect(self.setGeometry)
        glide.finished.connect(lambda: self._glided(target))
        glide.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._glide = glide

    def _glided(self, target: QRect) -> None:
        self._arrived = True
        self._say(("arrived", (target.x(), target.y(), target.width(), target.height())))
        if self._bare_due:
            self._go_bare()
        if not self._limit_set:
            self._limit_set = True
            QTimer.singleShot(HANDOVER_LIMIT_MS, self, self._quit_splash)

    def _go_bare(self) -> None:
        """On the player's mark, which shows under it: only its tile and glyph from now on,
        opaque and the same as the player's, so the light and the shadow are the player's."""
        self.bare = True
        self.update()

    def _quit_splash(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._poller.stop()
        self.hide()
        self._on_end()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        if not self._painted:
            self._painted = True
            # once this frame is on screen
            QTimer.singleShot(0, self, lambda: self._say(("shown", self.origin)))

    def _say(self, message: tuple[Any, ...]) -> None:
        try:
            self._conn.send(message)
        except OSError, ValueError:
            pass  # QiTV is gone: the next poll ends the splash


def _platform_name() -> str:
    return QGuiApplication.platformName()


def _hide_from_dock() -> bool:
    """macOS: [[NSApplication sharedApplication] setActivationPolicy:Accessory], so the
    helper has no Dock icon and no menu bar. False when that couldn't be done."""
    try:
        from ui import macos_window

        objc = macos_window._objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        application = macos_window._send(objc.objc_getClass(b"NSApplication"), b"sharedApplication")
        if not application:
            return False
        policy = (ctypes.c_long, NS_ACTIVATION_POLICY_ACCESSORY)
        return bool(macos_window._send(application, b"setActivationPolicy:", ctypes.c_bool, policy))
    except Exception as e:  # never a reason for the splash not to show
        logger.debug("The splash could not leave the Dock: %s", e)
        return False
