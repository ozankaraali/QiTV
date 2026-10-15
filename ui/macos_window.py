"""Attach the overlay as a Cocoa child window so macOS moves it with the player (spec §5.7)."""

import ctypes
import ctypes.util
import logging
import platform

from PySide6.QtGui import QGuiApplication

logger = logging.getLogger(__name__)
_NS_WINDOW_ABOVE = 1


def attach_child_window(parent, child) -> bool:
    # Only the cocoa platform plugin hands out NSView pointers as winId()
    if platform.system() != "Darwin" or QGuiApplication.platformName() != "cocoa":
        return False
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        parent_window = send(ctypes.c_void_p(int(parent.winId())), objc.sel_registerName(b"window"))
        child_window = send(ctypes.c_void_p(int(child.winId())), objc.sel_registerName(b"window"))
        if not parent_window or not child_window:
            return False
        attach = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        )(ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value or 0)
        attach(
            parent_window,
            objc.sel_registerName(b"addChildWindow:ordered:"),
            child_window,
            _NS_WINDOW_ABOVE,
        )
        return True
    except Exception as e:  # never let a Cocoa detail break the player
        logger.debug("Could not attach the overlay as a child window: %s", e)
        return False
