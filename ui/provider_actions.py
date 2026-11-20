"""One provider change in the drawer, and what the TV window does after it.

Settings in the drawer applies each provider change at once (`services.provider_changes`
writes it). `ProviderActions` makes the change, then runs the window's follow-ups the old
Settings window's Save ran for it, in its order:

- added: the catalogs reload (`providers_saved`), then the list shows the new provider
  (`_show_provider`, as for a dropped link's);
- a new connection (the address, the login, the SSL check): a provider other than the TV's
  loses its cached lists before the catalogs reload (one set up again at once then keeps what
  its new sign-in brings); the TV's is set up again with it once they reloaded;
- the same connection (the stream limit, the guide's offset): the catalogs reload only;
- renamed: what is kept under its old name moves (`providers_renamed`), the catalogs reload,
  and the TV's is set up again under its new name;
- removed: its scheduled recordings go (`providers_renamed`), the catalogs reload; the TV's
  gives way to the provider after it in the list (the one before it when it was the last; none
  when it was the only one: the welcome card).

Each ends with the menu bar's provider list rebuilt. Asking first (a removal, the parental
PIN for a rename) is the caller's. The TV's provider is the one Settings selects
(`selected_provider_name`), as the old Save had it.
"""

from __future__ import annotations

from typing import Any

from services.provider_changes import Change, ProviderChanges
from services.provider_rename import Renames


class ProviderActions:
    """The provider changes of `window` (the TVWindow), each followed up at once."""

    def __init__(self, window: Any) -> None:
        self._window = window
        config = window.config_manager
        self.changes = ProviderChanges(window.provider_manager, lambda: bool(config.ssl_verify))

    def add(self, entry: dict) -> str:
        """List `entry` as a new provider and show it; its name in the list."""
        name = self.changes.add(entry)
        self._window.providers_saved()  # the find, the Library and All providers hear of it
        self._window._show_provider(name)
        self._done()
        return name

    def update(self, name: str, changes: dict) -> Change:
        """Change provider `name`'s fields by `changes` (ProviderChanges.update)."""
        change = self.changes.update(name, changes)
        if not change.fields:
            return change
        selected = self._selected(name)
        if change.identity_changed and not selected:
            self._manager_of(name).invalidate_provider_cache(name)
        self._window.providers_saved()
        if change.identity_changed and selected:
            self._window.set_provider(force_update=True)
        self._done()
        return change

    def rename(self, old: str, new: str) -> str:
        """Rename provider `old` (ProviderChanges.rename: NameTaken); its name now."""
        before = frozenset(self.changes.names())
        selected = self._selected(old)
        name = self.changes.rename(old, new)
        if name == old:
            return name
        self._window.providers_renamed(Renames({old: name}, before), [])
        self._window.providers_saved()
        if selected:
            self._window.set_provider(force_update=True)
        self._done()
        return name

    def remove(self, name: str) -> None:
        """Take provider `name` off the list (KeyError when none has the name)."""
        names = self.changes.names()
        selected = self._selected(name)
        self.changes.remove(name)
        self._window.providers_renamed(Renames({}), [name])
        self._window.providers_saved()
        if selected:
            left = self.changes.names()
            index = min(names.index(name), len(left) - 1)
            self._window.config_manager.selected_provider_name = left[index] if left else ""
            self._window.save_config()
            self._window.set_provider()  # none left: the welcome card
        self._done()

    def _selected(self, name: str) -> bool:
        return name == str(self._window.config_manager.selected_provider_name or "")

    def _manager_of(self, name: str) -> Any:
        """The provider's own ProviderManager when its catalog exists (it holds the provider's
        queued cache writes), else the list's: a catalog is never made just for this."""
        catalogs = self._window.catalogs
        if name in catalogs.created():
            return catalogs.catalog(name).provider_manager
        return self._window.provider_manager

    def _done(self) -> None:
        self._window.menus.rebuild_providers()
