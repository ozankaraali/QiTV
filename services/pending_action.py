"""The one action waiting for its own provider (pure, no Qt).

A Library activation for a provider that is not ready yet waits here while that provider sets
up in the background (the Library's source and the TV page are not touched). Only one action
waits: a newer one replaces it. It runs once, when its provider becomes ready (and, when it
needs a section, once that section arrived), and is dropped when its provider fails, when its
section fails, or when the caller cancels it (the user left the Library or played something
else). Another provider becoming ready does not concern it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

RUN = "run"  # run the action now
WAIT = "wait"  # its provider is not ready yet: it waits
LOAD = "load"  # its provider is ready: request `action.section`, then run on its arrival


@dataclass(frozen=True, eq=False)
class Pending:
    provider: str
    payload: Any  # what the caller runs
    section: Optional[str] = None  # a section to load before running


class PendingAction:
    def __init__(self) -> None:
        self._action: Optional[Pending] = None
        self._loading_section = False

    @property
    def action(self) -> Optional[Pending]:
        return self._action

    def activate(self, action: Pending, *, ready: bool, loaded: bool = True) -> str:
        """RUN when `action` can run now, else it waits: WAIT for its provider, or LOAD its
        section (`ready`: its provider is ready; `loaded`: its section is already loaded)."""
        self._loading_section = False
        if ready and (action.section is None or loaded):
            self.cancel()
            return RUN
        self._action = action
        if ready:
            self._loading_section = True
            return LOAD
        return WAIT

    def cancel(self) -> None:
        """Forget the waiting action."""
        self._action = None
        self._loading_section = False

    def on_ready(self, provider: str) -> tuple[Optional[str], Optional[Pending]]:
        """(RUN, action), (LOAD, action) or (None, None) when `provider` became ready."""
        action = self._action
        if action is None or action.provider != provider:
            return None, None  # another provider's setup is none of its business
        if self._loading_section:
            return None, None  # a repeated ready while its section loads: keep waiting
        if action.section is not None:
            self._loading_section = True
            return LOAD, action
        self.cancel()
        return RUN, action

    def on_section_ready(self, provider: str, section: str) -> Optional[Pending]:
        """The action to run now that its section arrived for its provider."""
        action = self._action
        if (
            not self._loading_section
            or action is None
            or action.provider != provider
            or action.section != section
        ):
            return None
        self.cancel()
        return action

    def on_section_failed(self, provider: str, section: str) -> Optional[Pending]:
        """The dropped action, when its section failed to load."""
        action = self._action
        if (
            not self._loading_section
            or action is None
            or action.provider != provider
            or action.section != section
        ):
            return None
        self.cancel()
        return action

    def on_failed(self, provider: str) -> Optional[Pending]:
        """The dropped action, when `provider` (its provider) failed to set up."""
        action = self._action
        if action is None or action.provider != provider:
            return None
        self.cancel()
        return action
