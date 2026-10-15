"""Third-party modules that load on first use, so the window shows before they do.

requests, aiohttp and their dependencies took about half a second to import, before the TV
window could show. Only background jobs use them (provider and EPG downloads, images, update
checks), so the modules that need them hold `lazy_module(...)` instead: a module whose code
runs the first time one of its attributes is read, on whichever thread reads it first.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from types import ModuleType

_lock = threading.Lock()


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
        return module
