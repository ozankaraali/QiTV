"""Third-party modules that load on first use, so the window shows before they do.

requests, aiohttp and their dependencies took about half a second to import, before the TV
window could show. Only background jobs use them (provider and EPG downloads, images, update
checks), so the modules that need them hold `lazy_module(...)` instead: a module whose code
runs the first time one of its attributes is read, on whichever thread reads it first.

Once the window's first frame is on screen, it loads them on the GUI thread, one per pass of
the event loop (`load_module`), before the provider's catalog jobs need them: a module a
worker thread loads waits for the GIL at every file it opens, and a GUI thread that holds it
(QTest.qWait in tests) made a missing-playlist error take seconds to arrive.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from types import ModuleType

_lock = threading.Lock()
_lazy: list[str] = []  # the modules lazy_module() deferred, in order


def deferred_modules() -> list[str]:
    """The modules lazy_module() deferred, in the order they were deferred."""
    return list(_lazy)


def load_module(name: str) -> None:
    """Import a deferred module now, on this thread (left as it is when already loaded).

    One being loaded on another thread is waited for.
    """
    module = sys.modules.get(name)
    if module is not None:
        getattr(module, "__name__", None)  # any attribute read runs a lazy module


def load_lazy_modules() -> None:
    """Import every module lazy_module() deferred, now and on this thread."""
    for name in deferred_modules():
        load_module(name)


def lazy_module(name: str) -> ModuleType:
    """The module `name`, imported when one of its attributes is first read.

    An already imported module is returned as it is. Raises ModuleNotFoundError at once when
    the module cannot be found. importlib's LazyLoader runs the module once, under its own
    lock, however many threads read it first.
    """
    with _lock:
        module = sys.modules.get(name)
        if module is not None:
            return module
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        loader = importlib.util.LazyLoader(spec.loader)
        spec.loader = loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        _lazy.append(name)
        return module
