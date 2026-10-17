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

from services.catalog import CatalogService, Group
from services.catalogs import Catalogs
from services.epg_view import Programme
from services.provider_ids import provider_of
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
        self._seen_source = self.source()
        catalogs.linkReady.connect(self.linkReady)
        catalogs.linkFailed.connect(self.linkFailed)
        catalogs.catalogCreated.connect(self._attach)
        for name in catalogs.created():
            self._attach(name)

    # --- the source ------------------------------------------------------------------
    def source(self) -> str:
        """The provider this page shows now: its setting if that provider exists, else the
        first one (as a missing selection always fell back); "" when there are none."""
        names = self._catalogs.names()
        wanted = str(getattr(self.config, self._source_key, "") or "")
        if not wanted and self._source_key != "selected_provider_name":
            wanted = str(self.config.selected_provider_name or "")
        if wanted in names:
            return wanted
        return names[0] if names else ""

    def all_providers(self) -> bool:
        """Whether the page mixes every provider (not before 2.1's mixed pages)."""
        return False

    def catalog_for(self, item: dict) -> CatalogService:
        """The catalog an item came from (an unstamped item belongs to the source); KeyError
        when that provider is no longer in the list."""
        return self._catalogs.catalog(provider_of(item) or self.source())

    @property
    def catalogs(self) -> Catalogs:
        return self._catalogs

    def _for(self, item: dict) -> Optional[CatalogService]:
        """The catalog of an item's provider, None when that provider is gone."""
        name = provider_of(item) or self.source()
        if not name or name not in self._catalogs.names():
            return None
        return self._catalogs.catalog(name)

    def _cat(self, *, ensure: bool = False) -> Optional[CatalogService]:
        name = self.source()
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
            if self.source() == name:
                getattr(self, signal_name).emit(*args)

        return forward

    # --- providers -------------------------------------------------------------------
    def provider_names(self) -> list[str]:
        return self._catalogs.names()

    def current_provider(self) -> dict:
        catalog = self._cat()
        return catalog.current_provider() if catalog is not None else {}

    def current_provider_name(self) -> str:
        catalog = self._cat()
        return catalog.current_provider_name() if catalog is not None else ""

    def provider_type(self) -> str:
        catalog = self._cat()
        return catalog.provider_type() if catalog is not None else ""

    def content_version(self) -> int:
        source = self.source()
        if source != self._seen_source:
            self._seen_source = source
            self._epoch += 1
        catalog = self._cat()
        return (self._epoch << _EPOCH_SHIFT) + (
            catalog.content_version() if catalog is not None else 0
        )

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        """Show `name` (None: the current source) and start its setup."""
        if name is not None:
            if name not in self._catalogs.names():
                logger.warning("Not a provider in the list: nothing selected")
                return
            if name != str(getattr(self.config, self._source_key, "") or ""):
                setattr(self.config, self._source_key, name)
                try:
                    self.config.save_config()
                except Exception as e:
                    # The setup still runs; the choice is saved with the next settings write.
                    logger.warning("Couldn't save the provider choice: %s", safe_message(e))
        target = self.source()
        if target:
            self._catalogs.select(target, force_refresh=force_refresh)

    def loading_provider(self) -> Optional[str]:
        catalog = self._cat()
        return catalog.loading_provider() if catalog is not None else None

    def is_loading(self) -> bool:
        catalog = self._cat()
        return catalog.is_loading() if catalog is not None else False

    def shutdown(self) -> None:
        """The window is closing: every catalog stops (the pages share them)."""
        self._catalogs.shutdown()

    # --- sections and content ------------------------------------------------------------
    def sections(self) -> list[str]:
        catalog = self._cat()
        return catalog.sections() if catalog is not None else []

    def is_live(self, section: str) -> Optional[bool]:
        catalog = self._cat()
        return catalog.is_live(section) if catalog is not None else None

    def has_content(self, section: str) -> bool:
        catalog = self._cat()
        return catalog.has_content(section) if catalog is not None else False

    def request_section(self, section: str, *, force: bool = False) -> None:
        catalog = self._cat(ensure=True)
        if catalog is not None:
            catalog.request_section(section, force=force)

    def groups(self, section: str) -> list[Group]:
        catalog = self._cat()
        return catalog.groups(section) if catalog is not None else []

    def items(self, section: str, group_id: str) -> Optional[list[dict]]:
        catalog = self._cat()
        return catalog.items(section, group_id) if catalog is not None else None

    def request_group(self, section: str, group_id: str) -> None:
        catalog = self._cat(ensure=True)
        if catalog is not None:
            catalog.request_group(section, group_id)

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

    def epg_ready(self) -> bool:
        catalog = self._cat()
        return catalog.epg_ready() if catalog is not None else False

    def max_connections(self) -> Optional[int]:
        catalog = self._cat()
        return catalog.max_connections() if catalog is not None else None
