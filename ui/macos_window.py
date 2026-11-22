"""The TV window's Cocoa details: the overlay attached as a child window, so macOS moves it with
the player, the keys given back to the window after a click on the video, and for the desktop
picture in picture (ui/desktop_pip.py) whether a window is on the desktop shown, whether any of
it can be seen, a window that shows on every desktop (and back on its own one), and a window
macOS resizes in one shape."""

import ctypes
import ctypes.util
import functools
import logging
from typing import Any, Optional

from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)
_NS_WINDOW_ABOVE = 1
# NSWindowCollectionBehavior: on every desktop (Space), and allowed beside a fullscreen window.
_CAN_JOIN_ALL_SPACES = 1 << 0
_FULL_SCREEN_AUXILIARY = 1 << 8
_NONACTIVATING_PANEL = 1 << 7  # NSWindowStyleMaskNonactivatingPanel
_OCCLUSION_VISIBLE = 1 << 1  # NSWindowOcclusionStateVisible: some of the window can be seen
# Each NSWindow `join_all_spaces` changed: its collection behavior and whether it was a
# non-activating panel before (`leave_all_spaces` gives them back).
_BEFORE_JOINING: dict[int, tuple[int, bool]] = {}


@functools.cache
def _objc() -> Any:
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    return objc


def _send(receiver: int, selector: bytes, restype: Any = ctypes.c_void_p, *args: Any) -> Any:
    """[receiver selector …]; each of `args` is a (ctypes type, value) pair."""
    objc = _objc()
    function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *(t for t, _ in args))(
        ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0
    )
    return function(receiver, objc.sel_registerName(selector), *(v for _, v in args))


def animates_restore() -> bool:
    """macOS: a restored window flies out of the Dock, and the platform reports it landed.

    AppKit sends windowDidDeminiaturize (Qt: a spontaneous WindowStateChange on the QWindow)
    after the genie animation, whoever restores the window. Qt reports a restore it asked
    for itself (showNormal()) at once, and isMiniaturized is already NO during the animation
    then (checked on macOS 13): only the platform's report marks the end.
    """
    return QGuiApplication.platformName() == "cocoa"


def reclaim_first_responder(window) -> bool:
    """macOS: give the keys back to `window`'s own view after a click on a video surface.

    A mouse down on the video makes libVLC's VLCVideoLayerView, which libVLC puts inside the
    surface's NSView, the window's first responder. Keys then reach VLC's view first: they
    still travel on to Qt, but macOS beeps on the way. `window` is the top-level widget; its
    winId() is Qt's QNSView, made first responder again ([[view window] makeFirstResponder:
    view]). Nothing happens elsewhere (False).
    """
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        view = int(window.winId())
        ns_window = _send(view, b"window")
        if not ns_window:
            return False
        return bool(
            _send(ns_window, b"makeFirstResponder:", ctypes.c_bool, (ctypes.c_void_p, view))
        )
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not give the keys back to the window: %s", e)
        return False


def hide_pointer_until_moved() -> bool:
    """macOS: hide the pointer until the mouse moves ([NSCursor setHiddenUntilMouseMoves:YES]).

    AppKit shows it again on the next move, over any view, libVLC's included. Nothing happens
    elsewhere (False).
    """
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        objc = _objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        cursor = objc.objc_getClass(b"NSCursor")
        if not cursor:
            return False
        _send(cursor, b"setHiddenUntilMouseMoves:", None, (ctypes.c_bool, True))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not hide the pointer: %s", e)
        return False


def attach_child_window(parent, child) -> bool:
    # Only the cocoa platform plugin hands out NSView pointers as winId()
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        parent_window = _send(int(parent.winId()), b"window")
        child_window = _send(int(child.winId()), b"window")
        if not parent_window or not child_window:
            return False
        _send(
            parent_window,
            b"addChildWindow:ordered:",
            None,
            (ctypes.c_void_p, child_window),
            (ctypes.c_long, _NS_WINDOW_ABOVE),
        )
        # Qt makes a Qt::Tool panel hide whenever QiTV is not the active app, at the
        # floating level. Overlays must show over the inactive window too (a wheel zap
        # shows the banner), and a child at the window's own level never floats over other
        # apps. AppKit's addChildWindow did both on macOS 13, but says nothing of it.
        _send(child_window, b"setHidesOnDeactivate:", None, (ctypes.c_bool, False))
        level = _send(parent_window, b"level", ctypes.c_long)
        _send(child_window, b"setLevel:", None, (ctypes.c_long, level))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not attach the overlay as a child window: %s", e)
        return False


def on_active_space(widget) -> Optional[bool]:
    """macOS: whether `widget`'s window is on a desktop (Space) shown now ([NSWindow
    isOnActiveSpace]); None elsewhere, or when it cannot tell."""
    if QGuiApplication.platformName() != "cocoa":
        return None
    try:
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return None
        return bool(_send(ns_window, b"isOnActiveSpace", ctypes.c_bool))
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not tell the window's desktop: %s", e)
        return None


def is_visible(widget) -> Optional[bool]:
    """macOS: whether any of `widget`'s window can be seen now: its [NSWindow occlusionState]
    has NSWindowOcclusionStateVisible (not covered whole by other windows, on the desktop
    shown), or one of its child windows' has (the overlays glued over it are its own); False
    when it is minimized. None elsewhere, when it cannot tell, and while QiTV is hidden (⌘H:
    its windows are away on purpose, and showing one would bring QiTV back)."""
    if QGuiApplication.platformName() != "cocoa":
        return None
    try:
        objc = _objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        app = _send(objc.objc_getClass(b"NSApplication"), b"sharedApplication")
        if not app or _send(app, b"isHidden", ctypes.c_bool):
            return None
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return None
        if _send(ns_window, b"isMiniaturized", ctypes.c_bool):
            return False
        windows = [ns_window]
        children = _send(ns_window, b"childWindows")
        if children:
            count = _send(children, b"count", ctypes.c_ulong)
            windows += [
                _send(children, b"objectAtIndex:", ctypes.c_void_p, (ctypes.c_ulong, index))
                for index in range(count)
            ]
        return any(
            _send(window, b"occlusionState", ctypes.c_ulong) & _OCCLUSION_VISIBLE
            for window in windows
            if window
        )
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not tell whether the window can be seen: %s", e)
        return None


def join_all_spaces(widget) -> bool:
    """macOS: `widget`'s window shows on every desktop, beside a fullscreen one too, stays
    when QiTV is not the active app, and (a panel: Qt::Tool) a press on it never activates
    QiTV, which would bring QiTV's fullscreen desktop (Qt leaves that style off). Nothing
    happens elsewhere (False)."""
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return False
        mask = _send(ns_window, b"styleMask", ctypes.c_ulong)
        _BEFORE_JOINING.setdefault(
            ns_window,
            (
                _send(ns_window, b"collectionBehavior", ctypes.c_ulong),
                bool(mask & _NONACTIVATING_PANEL),
            ),
        )
        behavior = _CAN_JOIN_ALL_SPACES | _FULL_SCREEN_AUXILIARY
        _send(ns_window, b"setCollectionBehavior:", None, (ctypes.c_ulong, behavior))
        _send(ns_window, b"setHidesOnDeactivate:", None, (ctypes.c_bool, False))
        if not mask & _NONACTIVATING_PANEL:
            _send(ns_window, b"setStyleMask:", None, (ctypes.c_ulong, mask | _NONACTIVATING_PANEL))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not show the window on every desktop: %s", e)
        return False


def leave_all_spaces(widget) -> bool:
    """macOS: `widget`'s window as it was before `join_all_spaces` (the overlays, over the
    floating picture for a while): its own desktop, a press on it activating QiTV again. Nothing
    happens elsewhere, or for a window never joined (False)."""
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        ns_window = _send(int(widget.winId()), b"window")
        before = _BEFORE_JOINING.pop(ns_window, None) if ns_window else None
        if before is None:
            return False
        behavior, nonactivating = before
        _send(ns_window, b"setCollectionBehavior:", None, (ctypes.c_ulong, behavior))
        mask = _send(ns_window, b"styleMask", ctypes.c_ulong)
        if not nonactivating and mask & _NONACTIVATING_PANEL:
            _send(ns_window, b"setStyleMask:", None, (ctypes.c_ulong, mask & ~_NONACTIVATING_PANEL))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not put the window back on its desktop: %s", e)
        return False


class _NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]  # CGFloat: 64-bit


def keep_shape(widget, shape: float) -> bool:
    """macOS: the edges of `widget`'s window resize it in `shape` (width / height): its
    [NSWindow contentAspectRatio], which macOS keeps as the user drags them (Qt makes a frameless
    window resizable there, NSWindowStyleMaskResizable). Told again after the window is shown: Qt
    sets its resize increments as it makes the NSWindow, which clears it. Nothing happens
    elsewhere (False)."""
    if QGuiApplication.platformName() != "cocoa" or not shape > 0:
        return False
    try:
        ns_window = _send(int(widget.winId()), b"window")
        if not ns_window:
            return False
        size = _NSSize(float(shape) * 1000.0, 1000.0)
        _send(ns_window, b"setContentAspectRatio:", None, (_NSSize, size))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not keep the window's shape: %s", e)
        return False


def activate_app() -> bool:
    """macOS: QiTV comes to the front as an app ([NSApp activateIgnoringOtherApps:YES]), as
    when another start of QiTV hands over to it (2.8.12). Nothing happens elsewhere (False)."""
    if QGuiApplication.platformName() != "cocoa":
        return False
    try:
        objc = _objc()
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        app = _send(objc.objc_getClass(b"NSApplication"), b"sharedApplication")
        if not app:
            return False
        _send(app, b"activateIgnoringOtherApps:", None, (ctypes.c_bool, True))
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("QiTV could not come to the front: %s", e)
        return False
