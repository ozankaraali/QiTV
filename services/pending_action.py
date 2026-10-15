"""The one action waiting for another provider to become ready (pure, no Qt).

A Library activation for a provider that is not ready yet waits here while the catalog switches
to it. Only one action waits: a newer one replaces it. It runs once, when its provider becomes
ready, and is dropped when a different provider becomes ready (someone chose another source),
when its provider fails, or when the caller cancels it (the user left the Library or played
something else).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

RUN = "run"  # run the action now
SWITCH = "switch"  # select the action's provider; it waits
WAIT = "wait"  # the switch to its provider is already under way; it waits
LOAD = "load"  # its provider is ready: request `action.section`, then run on its arrival


@dataclass(frozen=True, eq=False)
class Pending:
    provider: str
    payload: Any  # what the caller runs
    section: Optional[str] = None  # a section to load before running


class PendingAction:
    def __init__(self) -> None:
        self.target: Optional[str] = None  # the provider a switch is under way to
        self._action: Optional[Pending] = None
        self._loading_section = False

    @property
    def action(self) -> Optional[Pending]:
        return self._action

    def activate(self, action: Pending, *, current: str, loading: bool) -> str:
        """RUN when `action` can run now, else it waits: SWITCH (select its provider) or WAIT.

        While a switch is under way (to `target`, or a setup started elsewhere: `loading`),
        every activation waits, even for the provider the catalog names: the provider
        manager names the new provider before it is ready.
        """
        if self.target is None and not loading and action.provider == current:
            self.cancel()
            return RUN
        self._action = action
        self._loading_section = False
        if action.provider == self.target:
            return WAIT
        self.target = action.provider
        return SWITCH

    def switching(self, provider: str) -> None:
        """A switch to `provider` started elsewhere (the Source menu)."""
        self.target = provider

    def cancel(self) -> None:
        """Forget the waiting action; a switch under way stays the target."""
        self._action = None
        self._loading_section = False

    def on_ready(self, provider: str) -> tuple[Optional[str], Optional[Pending]]:
        """(RUN, action), (LOAD, action) or (None, None) when `provider` became ready."""
        action = self._action
        if self._loading_section and action is not None and action.provider == provider:
            return None, None  # a repeated ready while its section loads: keep waiting
        self.target = None
        if action is None or action.provider != provider:
            self.cancel()
            return None, None
        if action.section is not None:
            self._loading_section = True
            return LOAD, action
        self.cancel()
        return RUN, action

    def on_section_ready(self, section: str, *, current: str) -> Optional[Pending]:
        """The action to run now that its section arrived for its provider."""
        action = self._action
        if (
            not self._loading_section
            or action is None
            or action.section != section
            or action.provider != current
        ):
            return None
        self.cancel()
        return action

    def on_section_failed(self, section: str) -> Optional[Pending]:
        """The dropped action, when its section failed to load."""
        action = self._action
        if not self._loading_section or action is None or action.section != section:
            return None
        self.cancel()
        return action

    def on_failed(self, provider: str) -> Optional[Pending]:
        """The dropped action, when `provider` (its provider) failed to set up."""
        self.target = None
        action = self._action
        if action is None or action.provider != provider:
            return None
        self.cancel()
        return action
