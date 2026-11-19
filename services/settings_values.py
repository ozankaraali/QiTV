"""Reads and writes a settings row's value in the config (no Qt).

The drawer applies every step at once: a row key names a ConfigManager property, a step
writes it, saves in the background and runs the row's hook (the caller's way to make the
change take effect). `play_in` is no property of its own: it stands for the two old
exclusive keys, play_in_vlc and play_in_mpv.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from services.config_manager import ConfigManager

logger = logging.getLogger(__name__)

PLAY_IN = "play_in"


class SettingsValues:
    """A row key's value, read from and written to a ConfigManager."""

    def __init__(
        self,
        config: ConfigManager,
        hooks: Optional[Mapping[str, Callable[[], None]]] = None,
    ) -> None:
        self._config = config
        self._hooks = dict(hooks or {})

    def get(self, key: str) -> Any:
        """The row's current value; KeyError for a key the config has no property for."""
        if key == PLAY_IN:
            if self._config.play_in_vlc:
                return "vlc"
            return "mpv" if self._config.play_in_mpv else "qitv"
        self._check(key)
        return getattr(self._config, key)

    def set(self, key: str, value: Any) -> None:
        """Writes the value, saves without waiting for the disk, then runs the row's hook."""
        if key == PLAY_IN:
            self._config.play_in_vlc = value == "vlc"
            self._config.play_in_mpv = value == "mpv"
        else:
            self._check(key)
            setattr(self._config, key, value)
        try:
            self._config.save_config()
        except Exception:
            # Only a config without a background writer raises here; the value is applied
            # in memory and the next save writes it.
            logger.warning("Couldn't save the setting %s", key, exc_info=True)
        hook = self._hooks.get(key)
        if hook is not None:
            hook()

    def _check(self, key: str) -> None:
        """KeyError unless `key` is a property of the config (a typo must not make a key)."""
        if not isinstance(getattr(type(self._config), key, None), property):
            raise KeyError(key)
