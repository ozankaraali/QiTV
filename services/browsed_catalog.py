"""What one page (the TV, the Library) browses: the catalog of the provider it shows.

QiTV 2.1 keeps one `CatalogService` per provider behind the `Catalogs` hub. A `BrowsedCatalog`
has the interface the screens already use and forwards it to the catalog of its page's
source, so the screens need not know there are several. Each page keeps its own source
(`selected_provider_name` for the TV, `library_provider_name` for the Library).

Signals come from the catalog of the current source only; `linkReady` and `linkFailed` come
from the hub, whichever provider an item belongs to.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from services.catalog import ALL_GROUP_ID, CatalogService, Group
from services.catalogs import Catalogs
from services.epg_view import Programme
from services.provider_ids import (
    ALL_PROVIDERS,
    mixed_group_id,
    provider_of,
    split_group_id,
)
from services.redact import safe_message

logger = logging.getLogger(__name__)

# The signals of a catalog that its views re-emit while it is their source.
FORWARDED_SIGNALS = (
    "providerReady",
    "providerFailed",
    "sectionReady",
    "sectionFailed",
    "groupReady",
    "groupFailed",
    "epgReady",
    "seasonsReady",
    "seasonsFailed",
    "episodesReady",
    "episodesFailed",
)

# content_version() mixes in how often the source changed: two catalogs count versions apart.
_EPOCH_SHIFT = 32
_VERSION_MASK = (1 << _EPOCH_SHIFT) - 1
_TYPED_PROVIDERS = ("STB", "XTREAM")  # providers with movies and series besides live TV


class BrowsedCatalog(QObject):
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    sectionReady = Signal(str)
    sectionFailed = Signal(str, str)
    groupReady = Signal(str, str)
    groupFailed = Signal(str, str, str)
    epgReady = Signal()
    seasonsReady = Signal(str)
    seasonsFailed = Signal(str, str)
    episodesReady = Signal(str, str)
    episodesFailed = Signal(str, str, str)
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)

    def __init__(
        self,
        catalogs: Catalogs,
        config,
        *,
        source_key: str,
        all_key: str,
        parent: Optional[QObject] = None,
    ):
        """`source_key` names the config setting that holds this page's provider and
        `all_key` the one that says it shows every provider (not used before 2.1's mixed
        pages). An empty `library_provider_name` means the selected provider."""
        super().__init__(parent)
        self._catalogs = catalogs
        self.config = config
        self._source_key = source_key
        self._all_key = all_key
        self._attached: dict[str, CatalogService] = {}
        self._epoch = 0
        # What all mode builds from the providers' lists, kept against their content versions:
        # ("groups"/"all", section) -> (versions, value). 200,000 channels are joined once.
        self._memo: dict[tuple[str, str], tuple[tuple, Any]] = {}
        self._seen_source = self.source()
        catalogs.linkReady.connect(self.linkReady)
        catalogs.linkFailed.connect(self.linkFailed)
        catalogs.catalogCreated.connect(self._attach)
        for name in catalogs.created():
            self._attach(name)

    # --- the source ------------------------------------------------------------------
    def source(self) -> str:
        """The provider this page shows now (ALL_PROVIDERS when it mixes every provider)."""
        return ALL_PROVIDERS if self.all_providers() else self._single_source()

    def _single_source(self) -> str:
        """The provider this page shows when it shows one: its setting if that provider
        exists, else the first one (as a missing selection always fell back); "" when there
        are none. In all mode it is the provider the page returns to."""
        names = self._catalogs.names()
        wanted = str(getattr(self.config, self._source_key, "") or "")
        if not wanted and self._source_key != "selected_provider_name":
            wanted = str(self.config.selected_provider_name or "")
        if wanted in names:
            return wanted
        return names[0] if names else ""

    def all_providers(self) -> bool:
        """Whether the page mixes every provider."""
        return bool(getattr(self.config, self._all_key, False))

    def catalog_for(self, item: dict) -> CatalogService:
        """The catalog an item came from (an unstamped item belongs to the source); KeyError
        when that provider is no longer in the list."""
        return self._catalogs.catalog(provider_of(item) or self._single_source())

    @property
    def catalogs(self) -> Catalogs:
        return self._catalogs

    def _for(self, item: dict) -> Optional[CatalogService]:
        """The catalog of an item's provider, None when that provider is gone."""
        name = provider_of(item) or self._single_source()
        if not name or name not in self._catalogs.names():
            return None
        return self._catalogs.catalog(name)

    def _cat(self, *, ensure: bool = False) -> Optional[CatalogService]:
        """The catalog of the single provider (in all mode, the one the page returns to)."""
        name = self._single_source()
        if not name:
            return None
        if ensure:
            self._catalogs.ensure(name)
        return self._catalogs.catalog(name)

    @property
    def provider_manager(self):
        """The ProviderManager of the provider shown (the registry when there is none)."""
        catalog = self._cat()
        return catalog.provider_manager if catalog is not None else self._catalogs.registry

    @property
    def epg_manager(self):
        catalog = self._cat()
        return catalog.epg_manager if catalog is not None else None

    # --- signals ---------------------------------------------------------------------
    def _attach(self, name: str) -> None:
        catalog = self._catalogs.catalog(name)
        if self._attached.get(name) is catalog:
            return
        self._attached[name] = catalog
        for signal_name in FORWARDED_SIGNALS:
            getattr(catalog, signal_name).connect(self._forwarder(name, signal_name))

    def _forwarder(self, name: str, signal_name: str) -> Callable[..., None]:
        def forward(*args: Any) -> None:
            if self.all_providers():
                args = self._mixed_args(name, signal_name, args)
            elif self.source() != name and signal_name != "epgReady":
                return  # (a guide in use is repainted whichever provider the list shows)
            getattr(self, signal_name).emit(*args)

        return forward

    @staticmethod
    def _mixed_args(name: str, signal_name: str, args: tuple) -> tuple:
        """A provider's group ids are mixed ids in a list that mixes providers."""
        if signal_name in ("groupReady", "groupFailed"):
            section, group_id, *rest = args
            if group_id != ALL_GROUP_ID:
                group_id = mixed_group_id(name, group_id)
            return (section, group_id, *rest)
        return args

    # --- providers -------------------------------------------------------------------
    def provider_names(self) -> list[str]:
        return self._catalogs.names()

    def current_provider(self) -> dict:
        if self.all_providers():
            return {"name": ALL_PROVIDERS, "type": "ALL"}
        catalog = self._cat()
        return catalog.current_provider() if catalog is not None else {}

    def current_provider_name(self) -> str:
        if self.all_providers():
            return ALL_PROVIDERS
        catalog = self._cat()
        return catalog.current_provider_name() if catalog is not None else ""

    def provider_entry(self, name: str) -> dict:
        """The settings of any provider by name (KeyError when it is not in the list)."""
        return self._catalogs.provider(name)

    def max_connections_of(self, name: str) -> Optional[int]:
        """What provider `name` reported as its simultaneous streams (None: unknown)."""
        return self._catalogs.max_connections(name)

    def sign_in_again(self, name: str) -> None:
        """Sign provider `name` in again: a link asked next waits for it (an STB retry)."""
        self._catalogs.sign_in_again(name)

    def provider_type(self) -> str:
        if self.all_providers():
            return "ALL"
        catalog = self._cat()
        return catalog.provider_type() if catalog is not None else ""

    def content_version(self) -> int:
        source = self.source()
        if source != self._seen_source:
            self._seen_source = source
            self._epoch += 1
        if self.all_providers():
            return (self._epoch << _EPOCH_SHIFT) + self._all_version()
        catalog = self._cat()
        return (self._epoch << _EPOCH_SHIFT) + (
            catalog.content_version() if catalog is not None else 0
        )

    def _all_version(self) -> int:
        """Changes whenever the content of any provider (or the provider list) does."""
        names = self._catalogs.names()
        total = len(names) * 1_000_003
        for name in names:
            total += self._catalogs.catalog(name).content_version()
        return total & _VERSION_MASK

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        """Show `name` (None: the current source). Its setup starts when it has not run (or
        failed) and `providerReady` follows when it is or becomes ready; nothing that plays
        is touched. `force_refresh` sets that provider up again. ALL_PROVIDERS shows every
        provider: the lists come from the caches at once, each setup starts beside."""
        if name == ALL_PROVIDERS:
            self._select_all(force_refresh)
            return
        if name is not None:
            if name not in self._catalogs.names():
                logger.warning("Not a provider in the list: nothing selected")
                return
            if self.all_providers():
                setattr(self.config, self._all_key, False)
                self._save_choice()
            if name != str(getattr(self.config, self._source_key, "") or ""):
                setattr(self.config, self._source_key, name)
                self._save_choice()
        if self.all_providers():
            self._select_all(force_refresh)
            return
        target = self._single_source()
        if not target:
            return
        if force_refresh:
            self._catalogs.select(target, force_refresh=True)
            return
        # Choosing a provider only moves what the page shows: its catalog is set up once
        # (never again while it is ready) and no other catalog is touched.
        self._catalogs.ensure(target)
        if self._catalogs.is_ready(target):
            self.providerReady.emit(target)  # its lists come from its cache at once

    def _save_choice(self) -> None:
        try:
            self.config.save_config()
        except Exception as e:
            # The setup still runs; the choice is saved with the next settings write.
            logger.warning("Couldn't save the provider choice: %s", safe_message(e))

    def _select_all(self, force_refresh: bool) -> None:
        if not self.all_providers():
            setattr(self.config, self._all_key, True)
            self._save_choice()
        for name in self._catalogs.names():
            if force_refresh:
                self._catalogs.select(name, force_refresh=True)
            else:
                self._catalogs.ensure(name)
        self.providerReady.emit(ALL_PROVIDERS)  # the lists come from the caches

    def loading_provider(self) -> Optional[str]:
        if self.all_providers():
            for name in self._catalogs.created():
                if name in self._catalogs.names() and self._catalogs.catalog(name).is_loading():
                    return name
            return None
        catalog = self._cat()
        return catalog.loading_provider() if catalog is not None else None

    def is_loading(self) -> bool:
        if self.all_providers():
            return self.loading_provider() is not None
        catalog = self._cat()
        return catalog.is_loading() if catalog is not None else False

    def shutdown(self) -> None:
        """The window is closing: every catalog stops (the pages share them)."""
        self._catalogs.shutdown()

    # --- sections and content ------------------------------------------------------------
    def _sections_of(self, name: str) -> list[str]:
        """The sections a provider has, from its entry in the list (known before its setup)."""
        try:
            kind = str(self._catalogs.provider(name).get("type", "")).upper()
        except KeyError:
            return []
        return ["live", "movies", "series"] if kind in _TYPED_PROVIDERS else ["live"]

    def sections(self) -> list[str]:
        if self.all_providers():
            names = self._catalogs.names()
            if not names:
                return []
            return (
                ["live", "movies", "series"]
                if any(len(self._sections_of(name)) > 1 for name in names)
                else ["live"]
            )
        catalog = self._cat()
        return catalog.sections() if catalog is not None else []

    def is_live(self, section: str) -> Optional[bool]:
        if self.all_providers():
            if section != "live":
                return False
            # A playlist's entries may be live or on demand: undecided while one is mixed in.
            typed = all(len(self._sections_of(n)) > 1 for n in self._catalogs.names())
            return True if typed else None
        catalog = self._cat()
        return catalog.is_live(section) if catalog is not None else None

    def has_content(self, section: str) -> bool:
        if self.all_providers():
            return any(
                self._catalogs.catalog(name).has_content(section) for name in self._having(section)
            )
        catalog = self._cat()
        return catalog.has_content(section) if catalog is not None else False

    def request_section(self, section: str, *, force: bool = False) -> None:
        if self.all_providers():
            for name in self._having(section):
                self._catalogs.ensure(name)
                self._catalogs.catalog(name).request_section(section, force=force)
            return
        catalog = self._cat(ensure=True)
        if catalog is not None:
            catalog.request_section(section, force=force)

    def groups(self, section: str) -> list[Group]:
        if self.all_providers():
            return list(self._mixed_groups(section))
        catalog = self._cat()
        return catalog.groups(section) if catalog is not None else []

    def items(self, section: str, group_id: str) -> Optional[list[dict]]:
        if self.all_providers():
            return self._mixed_items(section, group_id)
        catalog = self._cat()
        return catalog.items(section, group_id) if catalog is not None else None

    def request_group(self, section: str, group_id: str) -> None:
        if self.all_providers():
            name, own_id = split_group_id(group_id)
            if not name or not own_id:
                # The mixed All, or a provider's heading: its sections as a whole.
                if not name:
                    self.request_section(section)
                elif name in self._having(section):
                    self._catalogs.ensure(name)
                    self._catalogs.catalog(name).request_section(section)
                return
            if name in self._having(section):
                self._catalogs.ensure(name)
                self._catalogs.catalog(name).request_group(section, own_id)
            return
        catalog = self._cat(ensure=True)
        if catalog is not None:
            catalog.request_group(section, group_id)

    # --- all providers -------------------------------------------------------------------
    def _having(self, section: str) -> list[str]:
        """The providers (in Settings order) that have this section."""
        return [name for name in self._catalogs.names() if section in self._sections_of(name)]

    def _versions(self) -> tuple:
        return tuple(
            (name, self._catalogs.catalog(name).content_version())
            for name in self._catalogs.names()
        )

    def _mixed_groups(self, section: str) -> list[Group]:
        versions = self._versions()
        hit = self._memo.get(("groups", section))
        if hit is not None and hit[0] == versions:
            return hit[1]
        rows: list[Group] = []
        total, any_all = 0, False
        for name in self._having(section):
            rows.append(Group(section, mixed_group_id(name, ""), name, None, name, True))
            for group in self._catalogs.catalog(name).groups(section):
                if group.id == ALL_GROUP_ID:
                    any_all = True
                    total += group.count or 0
                    continue
                rows.append(
                    Group(section, mixed_group_id(name, group.id), group.title, group.count, name)
                )
        if any_all:
            rows.insert(0, Group(section, ALL_GROUP_ID, "All", total))
        self._memo[("groups", section)] = (versions, rows)
        return rows

    def _mixed_items(self, section: str, group_id: str) -> Optional[list[dict]]:
        """The mixed All is every provider's All in Settings order (one list, built once per
        change of content: treat it as read-only); a mixed id is that provider's group."""
        name, own_id = split_group_id(group_id)
        if name:
            if not own_id or name not in self._having(section):
                return None
            return self._catalogs.catalog(name).items(section, own_id)
        if group_id != ALL_GROUP_ID:
            return None
        versions = self._versions()
        hit = self._memo.get(("all", section))
        if hit is not None and hit[0] == versions:
            return hit[1]
        joined: list[dict] = []
        loaded = False
        for provider in self._having(section):
            part = self._catalogs.catalog(provider).items(section, ALL_GROUP_ID)
            if part is not None:
                loaded = True
                joined.extend(part)
        result = joined if loaded else None
        self._memo[("all", section)] = (versions, result)
        return result

    def loaded_contents(self) -> dict[str, dict]:
        """The content each provider's catalog holds now (read only), by provider name."""
        return self._catalogs.loaded_contents()

    def seasons(self, series: dict) -> Optional[list[dict]]:
        catalog = self._for(series)
        return catalog.seasons(series) if catalog is not None else None

    def request_seasons(self, series: dict) -> None:
        catalog = self._for(series)
        if catalog is not None:
            catalog.request_seasons(series)

    def episodes(self, series: dict, season: dict) -> Optional[list[dict]]:
        catalog = self._for(series)
        return catalog.episodes(series, season) if catalog is not None else None

    def request_episodes(self, series: dict, season: dict) -> None:
        catalog = self._for(series)
        if catalog is not None:
            catalog.request_episodes(series, season)

    # --- links and guide -------------------------------------------------------------------
    def request_link(self, item: dict, *, section: str, is_episode: bool = False) -> int:
        return self._catalogs.request_link(item, section=section, is_episode=is_episode)

    def programmes(self, channel: dict, now: datetime, limit: int = 8) -> list[Programme]:
        return self._catalogs.programmes(channel, now, limit)

    def refresh_epg(self, *, force: bool = False) -> None:
        catalog = self._cat()
        if catalog is not None:
            catalog.refresh_epg(force=force)
        self._catalogs.refresh_guides(skip=self.source(), force=force)

    def use_guides(self, names: set[str]) -> None:
        """The providers whose guides are in use (the others free their memory)."""
        self._catalogs.use_guides(names)

    def epg_ready(self) -> bool:
        catalog = self._cat()
        return catalog.epg_ready() if catalog is not None else False

    def max_connections(self) -> Optional[int]:
        catalog = self._cat()
        return catalog.max_connections() if catalog is not None else None
