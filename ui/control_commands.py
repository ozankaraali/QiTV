"""What the control port (services.control) asks of the running window (2.8.3), on the GUI
thread: its status, a key's action, a channel by name, quitting, and (from source) Python.
"""

from __future__ import annotations

import contextlib
import io
import os
from typing import Any

from PySide6.QtCore import QTimer

from services.config_manager import get_app_version
from services.provider_ids import provider_of
from services.text_fold import fold
from ui.keymap import Action

OUTPUT_CHARS = 20_000  # the most of an answer's text sent back


class WindowControl:
    """The commands of one window; `python`: whether Python sent to the port runs."""

    def __init__(self, window: Any, *, python: bool) -> None:
        self._window = window
        self._python = python
        self._timeline: dict = {}  # stay-live's latest state, as the player bar has it
        self.scope: dict[str, Any] = {"window": window}  # Python's names, kept between runs
        live = getattr(window, "live_session", None)
        if live is not None:
            live.timelineChanged.connect(self._on_timeline)

    def _on_timeline(self, state: dict) -> None:
        self._timeline = dict(state)

    def handle(self, command: str, args: dict) -> dict:
        method = getattr(self, f"_cmd_{command}", None)
        if method is None:
            return {"error": f"unknown command: {command}"}
        return dict(method(args))

    # ---- the commands -------------------------------------------------------------------------

    def _cmd_status(self, args: dict) -> dict:
        window = self._window
        current = window.controller.now_playing()
        playing = None
        if current is not None:
            provider = provider_of(current.item) or window.catalog.current_provider_name()
            playing = {
                "title": current.title,
                "provider": provider,
                "live": current.live,
                "section": current.section,
            }
        timeline = {k: v for k, v in self._timeline.items() if k != "parts"}
        return {
            "version": get_app_version(),
            "pid": os.getpid(),
            "playing": playing,
            "stay_live": timeline,
            "library_shown": bool(window.screens.library_shown()),
            "fullscreen": bool(window.isFullScreen()),
            "source": window.catalog.source(),
            "providers": list(window.catalogs.names()),
            "python": self._python,
        }

    def _cmd_actions(self, args: dict) -> dict:
        return {"actions": [action.value for action in Action]}

    def _cmd_action(self, args: dict) -> dict:
        """A key's action, as the keyboard or the phone remote sends it."""
        name = str(args.get("name") or "")
        action = next((a for a in Action if name in (a.value, a.name)), None)
        if action is None:
            return {"error": f"no such action: {name!r} (see actions)"}
        self._window.controller.handle(action, str(args.get("payload") or ""))
        return {"done": action.value}

    def _cmd_tune(self, args: dict) -> dict:
        """A live channel by its name (the same name, else one holding it), at `provider`
        when given: it plays as one picked from All."""
        wanted = fold(str(args.get("name") or "")).strip()
        provider = str(args.get("provider") or "")
        if not wanted:
            return {"error": "a channel name is needed"}
        channels = [
            item
            for item in self._window.nav.all_live_items()
            if not provider or provider_of(item) == provider
        ]
        names = [fold(str(item.get("name") or "")) for item in channels]
        found = [c for c, n in zip(channels, names) if n == wanted] or [
            c for c, n in zip(channels, names) if wanted in n
        ]
        if not found:
            return {"error": "no channel of that name", "searched": len(channels)}
        channel = found[0]
        self._window.controller.tune_live(channel)
        return {
            "tuned": channel.get("name"),
            "provider": provider_of(channel),
            "others": [c.get("name") for c in found[1:10]],
        }

    def _cmd_quit(self, args: dict) -> dict:
        QTimer.singleShot(0, self._window.close)  # after the answer has gone
        return {"quitting": True}

    def _cmd_python(self, args: dict) -> dict:
        """Python in the running QiTV (from source only): an expression's value, or a block's
        output. `window` is the window; names set stay for the next call."""
        if not self._python:
            return {"error": "Python runs only in QiTV run from source"}
        code = str(args.get("code") or "")
        out = io.StringIO()
        value: Any = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                compiled = compile(code, "<control>", "eval")
            except SyntaxError:
                exec(compile(code, "<control>", "exec"), self.scope)  # noqa: S102 (dev only)
            else:
                value = eval(compiled, self.scope)  # noqa: S307 (dev only)
        return {"value": repr(value)[:OUTPUT_CHARS], "output": out.getvalue()[:OUTPUT_CHARS]}
