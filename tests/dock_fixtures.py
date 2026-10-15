"""A real macOS window minimized to the Dock and restored, watched by the window server.

Not a test module: tests/test_macos_window.py runs it in a child process on the cocoa platform
(``python tests/dock_fixtures.py``), only with QITV_GUI_TESTS=1, and reads the JSON it prints. A QMainWindow with a video
surface and the overlay host (a persistent hint up) is minimized with AppKit's miniaturize:,
then restored two ways: by Qt (showNormal(), how the smoke test restores) and by AppKit
(deminiaturize:, as from the Dock). Meanwhile a thread samples CGWindowListCopyWindowInfo, which
needs no permission: whether each window is on screen and its bounds, which follow the genie
animation of the TV window. The overlay host is also checked as it is shown: the TV window's
NSWindow must not be in the Dock (isMiniaturized) by then.
"""

import ctypes
import ctypes.util
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEvent, QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from ui.macos_window import _send  # noqa: E402
from ui.overlay_host import OverlayHost  # noqa: E402
from ui.overlays.toast import Toast  # noqa: E402
from ui.video_surface import VideoSurface  # noqa: E402

TIMEOUT_S = 10.0
SAMPLE_S = 0.008


class _Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_double) for name in ("x", "y", "width", "height")]


def _window_server():
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Rect)]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFNumberGetValue.restype = ctypes.c_bool
    cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_bool
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    keys = {
        name: ctypes.c_void_p.in_dll(cg, name).value
        for name in ("kCGWindowNumber", "kCGWindowIsOnscreen", "kCGWindowBounds")
    }
    sint64 = 4

    def windows(numbers):
        """{number: (on screen, [x, y, width, height])} of the windows `numbers`."""
        found = {}
        listing = cg.CGWindowListCopyWindowInfo(0, 0)  # every window, on screen or not
        try:
            for i in range(cf.CFArrayGetCount(listing)):
                info = cf.CFArrayGetValueAtIndex(listing, i)
                number = ctypes.c_int64()
                cf.CFNumberGetValue(
                    cf.CFDictionaryGetValue(info, keys["kCGWindowNumber"]),
                    sint64,
                    ctypes.byref(number),
                )
                if number.value not in numbers:
                    continue
                flag = cf.CFDictionaryGetValue(info, keys["kCGWindowIsOnscreen"])
                bounds = _Rect()
                cg.CGRectMakeWithDictionaryRepresentation(
                    cf.CFDictionaryGetValue(info, keys["kCGWindowBounds"]), ctypes.byref(bounds)
                )
                found[number.value] = (
                    bool(flag) and cf.CFBooleanGetValue(flag),
                    [bounds.x, bounds.y, bounds.width, bounds.height],
                )
        finally:
            cf.CFRelease(listing)
        return found

    return windows


def _ns_window(widget):
    return _send(int(widget.winId()), b"window")


class _ShowWatch(QObject):
    """Records, at each Show of the overlay host, whether the TV window is still in the Dock."""

    def __init__(self, parent_window):
        super().__init__()
        self.parent_window = parent_window
        self.shows = []

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show:
            miniaturized = bool(_send(self.parent_window(), b"isMiniaturized", ctypes.c_bool))
            self.shows.append({"at": time.monotonic(), "parent_miniaturized": miniaturized})
        return False


def main():
    app = QApplication(sys.argv[:1])
    window = QMainWindow()
    surface = VideoSurface()
    window.setCentralWidget(surface)
    window.setGeometry(120, 120, 640, 360)
    host = OverlayHost(window, surface)
    toast = Toast(host)
    host.add_overlay(toast)
    watch = _ShowWatch(lambda: _ns_window(window))
    host.installEventFilter(watch)
    window_server = _window_server()
    result = {"restores": []}

    def wait(predicate):
        deadline = time.monotonic() + TIMEOUT_S
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        return bool(predicate())

    def numbers():
        return (
            _send(_ns_window(window), b"windowNumber", ctypes.c_long),
            _send(_ns_window(host), b"windowNumber", ctypes.c_long),
        )

    def pause(seconds):
        until = time.monotonic() + seconds
        wait(lambda: time.monotonic() > until)

    def run():
        window.show()
        toast.set_hint("Loading Home…")
        # An overlay host shown early was on screen only while QiTV was the active app (a
        # Qt::Tool window hides on deactivation until it is attached), as after a click on it.
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        nsapp = _send(objc.objc_getClass(b"NSApplication"), b"sharedApplication")
        _send(nsapp, b"activateIgnoringOtherApps:", None, (ctypes.c_bool, True))
        result["shown"] = wait(lambda: host.isVisible())
        pause(0.3)
        parent_number, host_number = numbers()
        landed = window_server({parent_number})[parent_number][1]
        for restore in ("showNormal", "deminiaturize:"):
            _send(_ns_window(window), b"miniaturize:", None, (ctypes.c_void_p, None))
            docked = wait(
                lambda: window.isMinimized()
                and bool(_send(_ns_window(window), b"isMiniaturized", ctypes.c_bool))
            )
            pause(0.3)
            entry = {
                "restore": restore,
                "docked": docked,
                "host_hidden_in_dock": not host.isVisible(),
                "active": bool(_send(nsapp, b"isActive", ctypes.c_bool)),
            }
            samples = []
            stop = threading.Event()

            def sample():
                while not stop.is_set():
                    seen = window_server({parent_number, host_number})
                    if parent_number in seen and host_number in seen:
                        samples.append(
                            (time.monotonic(), seen[host_number][0], seen[parent_number][1])
                        )
                    time.sleep(SAMPLE_S)

            sampler = threading.Thread(target=sample, daemon=True)
            shows_before = len(watch.shows)
            sampler.start()
            if restore == "showNormal":
                window.showNormal()
            else:
                _send(_ns_window(window), b"deminiaturize:", None, (ctypes.c_void_p, None))
            entry["restored"] = wait(
                lambda: not window.isMinimized()
                and not _send(_ns_window(window), b"isMiniaturized", ctypes.c_bool)
                and host.isVisible()
            )
            pause(0.5)
            stop.set()
            sampler.join()
            entry["host_shows"] = shows = watch.shows[shows_before:]
            flying = [at for at, _host_on, bounds in samples if bounds != landed]
            entry["animated"] = bool(flying)
            entry["host_on_screen_while_animating"] = sum(
                1 for _at, host_on, bounds in samples if host_on and bounds != landed
            )
            entry["host_on_screen_at_end"] = bool(samples) and samples[-1][1]
            if flying and shows:  # from the window's landing to the overlays' return
                after = [at for at, _host_on, _bounds in samples if at > flying[-1]]
                entry["shown_after_landing_s"] = shows[0]["at"] - after[0] if after else None
            result["restores"].append(entry)
        print(json.dumps(result), flush=True)
        app.quit()

    QTimer.singleShot(0, run)
    app.exec()


if __name__ == "__main__":
    main()
