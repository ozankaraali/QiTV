"""The splash: QiTV's mark, drawn by a small process of its own from the moment QiTV starts
until its window opens into it.

main() builds the window on its GUI thread before the event loop runs, so it can't animate
anything meanwhile. Splash.start() spawns run_splash (multiprocessing, "spawn"), which loops the
mark (ui.opening_mark) in a frameless, translucent window that stays on top, centred on the
primary screen. open_into(window) tells it "open" as the shown window's fade first moves: the
mark completes its draw and opens out while the window fades in where it will stay. close()
tells it "quit": a start that handed over, or QiTV's end.

The pipe carries ("open", (x, y, width, height)) and ("quit",) to the helper, and ("shown",)
back once its window has painted. In QiTV, a watcher thread is the pipe's only user: it sends
what the GUI thread queued, calls `on_shown` (on that thread) at "shown", and joins the helper,
killing it KILL_AFTER_S after the first "open" or "quit" if it hasn't ended by then. The helper
ends by itself once it finds the pipe closed (QiTV died, or ended before its window showed).

The helper process imports PySide6's GUI modules and the mark only (and ui.macos_window on
macOS, where it takes no Dock icon or menu bar).
"""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import logging
import multiprocessing
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
import os
import queue
import sys
import threading
import time
from typing import Any, Optional

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QRect,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QWidget

from ui.opening_mark import STAGE_PX, OpeningMark

logger = logging.getLogger(__name__)

POLL_MS = 20  # how often the helper reads its pipe; the watcher waits as long at most
KILL_AFTER_S = 3.0  # after "open" or "quit": a helper still running then is killed
FADE_IN_MS = 350  # the window's fade as the mark opens into it
FADING_PLATFORMS = ("xcb", "windows", "cocoa")  # Qt platforms that fade a window
# NSApplicationActivationPolicyAccessory: no Dock icon and no menu bar, windows allowed.
NS_ACTIVATION_POLICY_ACCESSORY = 1
# Without it, Qt makes a plain executable a Dock app as it starts (macOS).
_MAC_NO_FOREGROUND = "QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM"

Geometry = tuple[int, int, int, int]  # x, y, width, height


# ---- in QiTV ---------------------------------------------------------------------------------


class Splash:
    """The splash as QiTV drives it, from its GUI thread: start(), then open_into(window) or
    close(). Each does nothing once the splash didn't start; none of them waits.

    `on_shown` is called once, on the watcher thread (it must not touch Qt), when the helper's
    window has painted; never when it doesn't show.
    """

    def __init__(self, on_shown: Optional[Callable[[], None]] = None) -> None:
        self._on_shown = on_shown
        self._commands: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._process: Optional[BaseProcess] = None
        self._watcher: Optional[threading.Thread] = None
        self._opened = False
        self._closed = False
        self._transparent: Optional[QWidget] = None  # the window before_show made transparent

    def start(self, geometry: Optional[QRect] = None) -> bool:
        """Spawns the helper, its window at `geometry` (by default 240 px in the middle of the
        primary screen). False, and logged, when it couldn't: QiTV starts without a splash."""
        if self._process is not None:
            return False
        conn = child = process = None
        try:
            rect = geometry if geometry is not None else centred_on_primary()
            place = (rect.x(), rect.y(), rect.width(), rect.height())
            context = multiprocessing.get_context("spawn")
            conn, child = context.Pipe(duplex=True)
            process = context.Process(
                target=run_splash, args=(child, place), name="qitv-splash", daemon=True
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
        self._process, self._watcher = process, watcher
        return True

    def before_show(self, window: QWidget) -> None:
        """Before the window first shows: it starts transparent, to fade in as the splash opens
        into it (open_into). Where the platform fades no window, it shows as it is."""
        if self._process is not None and self._transparent is None and _fades_windows():
            window.setWindowOpacity(0.0)
            self._transparent = window

    def open_into(self, window: QWidget) -> None:
        """The window shows: it fades in over FADE_IN_MS from the event loop's first turn, and
        the splash opens as that fade first moves (see _open)."""
        if self._process is not None:
            QTimer.singleShot(0, window, lambda: self._open(window))

    def close(self) -> None:
        """The splash ends at once."""
        if self._process is not None and not self._closed:
            self._closed = True
            self._commands.put(("quit",))

    def _open(self, window: QWidget) -> None:
        """The loop's first turn after the window showed. The window's first paint can hold
        the loop a quarter of a second more (a Linux test start), about as long as a lit mark
        takes to open: "open" goes once the fade's clock runs, at its first step above 0, so the
        mark and the window open together. A window that doesn't fade waits one more turn."""
        if self._transparent is window:
            self._transparent = None
            fade_in(window, moving=lambda: self._send_open(window))
        else:
            QTimer.singleShot(0, window, lambda: self._send_open(window))

    def _send_open(self, window: QWidget) -> None:
        if not self._opened and not self._closed:
            self._opened = True
            frame = window.frameGeometry()
            self._commands.put(("open", (frame.x(), frame.y(), frame.width(), frame.height())))

    # ---- the watcher thread ----

    def _watch(self, process: BaseProcess, conn: Connection) -> None:
        """The pipe's only user, until the helper has ended."""
        deadline: Optional[float] = None
        shown = False
        try:
            while True:
                deadline = self._send_queued(conn, deadline)
                timeout = POLL_MS / 1000
                if deadline is not None:
                    timeout = max(0.0, min(timeout, deadline - time.monotonic()))
                ready = wait([conn, process.sentinel], timeout)
                if conn in ready:
                    try:
                        message = conn.recv()
                    except EOFError, OSError:
                        break  # the helper closed its end: it is ending
                    if message == ("shown",) and not shown:
                        shown = True
                        self._call_on_shown()
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

    def _call_on_shown(self) -> None:
        if self._on_shown is None:
            return
        try:
            self._on_shown()
        except Exception:
            logger.warning("The splash's shown hook failed.", exc_info=True)

    @staticmethod
    def _reap(process: BaseProcess, deadline: Optional[float]) -> None:
        """Waits for the helper's end; one still running at the deadline (KILL_AFTER_S after
        "open" or "quit", or after it closed its pipe) is killed. Its end is read from its
        sentinel, whoever reaps it: QiTV's exit may end it too (multiprocessing's exit handler,
        tv_window._end_child_processes), which is also why the Process is left unclosed."""
        if deadline is None:
            deadline = time.monotonic() + KILL_AFTER_S
        if not wait([process.sentinel], max(0.0, deadline - time.monotonic())):
            logger.warning("The splash did not end; it is stopped.")
            process.kill()
        # Reaped here, unless QiTV's exit did: its sentinel closes a moment before its exit.
        process.join(1.0)


def centred_on_primary(size: int = STAGE_PX) -> QRect:
    """A `size` square in the middle of the primary screen."""
    rect = QRect(0, 0, size, size)
    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        rect.moveCenter(screen.geometry().center())
    return rect


def fade_in(
    window: QWidget,
    duration_ms: int = FADE_IN_MS,
    moving: Optional[Callable[[], None]] = None,
) -> None:
    """The window's opacity from 0 to 1 over `duration_ms`, eased as the mark's own open (CSS's
    ease-in-out); it always ends at 1. `moving` is called once, at its first step above 0: the
    animation's clock starts only once the event loop runs freely."""
    pending = [moving] if moving is not None else []

    def step(level: Any) -> None:
        window.setWindowOpacity(float(level))
        if pending and float(level) > 0.0:
            pending.pop()()

    curve = QEasingCurve(QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(QPointF(0.42, 0.0), QPointF(0.58, 1.0), QPointF(1.0, 1.0))
    animation = QVariantAnimation(window)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setDuration(duration_ms)
    animation.setEasingCurve(curve)
    animation.valueChanged.connect(step)
    animation.finished.connect(lambda: step(1.0))
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)


def _fades_windows() -> bool:
    """Whether this Qt platform fades a top-level window (X11's needs a compositor for it to
    show, and shows the window as it is without one); offscreen, Wayland and the others don't."""
    return _platform_name() in FADING_PLATFORMS


# ---- the helper process ----------------------------------------------------------------------


def run_splash(conn: Connection, geometry: Geometry) -> None:
    """The helper process (multiprocessing's spawn target): the mark at `geometry` until "open"
    has played, or "quit" or a closed pipe ends it. It ends with os._exit, so nothing in the
    interpreter's teardown keeps it going once its window is hidden."""
    code = 0
    try:
        if sys.platform == "darwin":
            os.environ[_MAC_NO_FOREGROUND] = "1"  # it becomes an accessory app instead
        application = QApplication.instance() or QApplication(["qitv-splash"])
        window = _SplashWindow(conn, geometry, application.quit)
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

    begin() shows it, unless "quit", "open" or the pipe's end came first (nothing shows then).
    ("shown",) goes back once it has painted. "open" finishes the mark (the draw completes,
    then it opens) and ends; "quit" and a closed pipe end it at once. `on_end` quits the app.
    """

    def __init__(self, conn: Connection, geometry: Geometry, on_end: Callable[[], None]) -> None:
        super().__init__()
        self._conn = conn
        self._on_end = on_end
        self._opening = False
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
        self.start()
        self._poller.start()
        return True

    def _poll(self) -> None:
        try:
            while not self._ended and self._conn.poll():
                self._handle(self._conn.recv())
        except EOFError, OSError:
            # QiTV closed its end or died (Windows can report that from poll() too)
            self._end()

    def _handle(self, message: Any) -> None:
        name = message[0] if isinstance(message, tuple) and message else None
        if name == "quit" or (name == "open" and not self.isVisible()):
            self._end()
        elif name == "open" and not self._opening:
            self._opening = True
            self.finish(self._end)

    def _end(self) -> None:
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
            QTimer.singleShot(0, self, self._say_shown)  # once this frame is on screen

    def _say_shown(self) -> None:
        try:
            self._conn.send(("shown",))
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
