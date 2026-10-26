"""One catalog per provider, behind a hub that routes requests by an item's provider stamp.

QiTV 2.1 mixes providers. `Catalogs` holds a `CatalogService` pinned to each provider
(created on first use, no network until `ensure()`), forwards their signals with the
provider's name, and sends link and guide requests to the catalog an item came from.

main.py's `ProviderManager` stays the registry: it owns the `providers` list for Settings;
each catalog gets its own pinned `ProviderManager` (and `EpgManager`) over that same list.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
import gc
import itertools
import logging
from typing import Any, Callable, Iterator, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services.catalog import CatalogService, WorkerFactory
from services.epg_manager import EpgManager
from services.epg_view import Programme
from services.i18n import N_
from services.provider_ids import provider_of
from services.provider_manager import ProviderManager

logger = logging.getLogger(__name__)

# A link failure's message, like the catalog's: English here, shown with translate().
UNKNOWN_PROVIDER_MESSAGE = N_("This provider is no longer in your list.")


class _LinkIds(Iterator[int]):
    """The request ids shared by every catalog; `reserve` makes the next one a given id."""

    def __init__(self) -> None:
        self._count = itertools.count(1)
        self._reserved: Optional[int] = None

    def __next__(self) -> int:
        if self._reserved is not None:
            reserved, self._reserved = self._reserved, None
            return reserved
        return next(self._count)

    def reserve(self, request_id: int) -> None:
        self._reserved = request_id


class Catalogs(QObject):
    linkReady = Signal(int, str)
    linkFailed = Signal(int, str)
    providerReady = Signal(str)
    providerFailed = Signal(str, str)
    epgReady = Signal(str)
    contentChanged = Signal(str)  # the provider whose content may have changed
    catalogCreated = Signal(str)  # a provider's catalog was made (views connect to it)
    providersChanged = Signal()  # reload(): the provider list was edited

    def __init__(
        self,
        config_manager,
        registry: ProviderManager,
        *,
        runner=None,
        factory: Optional[WorkerFactory] = None,
        make_epg: Optional[Callable[[ProviderManager], EpgManager]] = None,
        in_use: Callable[[], set[str]] = set,
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self.config = config_manager
        self.registry = registry
        self._runner = runner
        self._factory = factory
        self._make_epg = make_epg or (lambda manager: EpgManager(config_manager, manager))
        self._in_use = in_use
        self._ids = _LinkIds()
        self._by_name: dict[str, CatalogService] = {}
        self._specs: dict[str, dict] = {}  # the provider each catalog was built for
        self._started: set[str] = set()  # ensure() has begun this catalog's setup
        self._ready: set[str] = set()
        # Edited in Settings while in use: set up again with the new settings (reload()); a
        # forced setup asked meanwhile is that one.
        self._renewing: set[str] = set()
        # Link requests waiting for a catalog's setup: (request id, the request to make of it)
        self._waiting: dict[str, list[tuple[int, Callable[[CatalogService], int]]]] = {}
        # The providers whose guides are in use (None: nobody said yet, every guide loads).
        self._guides: Optional[set[str]] = None
        self._closed = False
        # 2.8: whether a screen shows 4K (the window's check; a media server's 4K version
        # plays only then, by default).
        self.screen_4k: Callable[[], bool] = lambda: False
        # 2.8: the window's parental controls (services.parental.Parental): the browsed
        # catalogs leave out what they hide while locked. None: nothing is hidden.
        self.parental: Optional[Any] = None

    # --- providers -------------------------------------------------------------------
    def names(self) -> list[str]:
        return [str(p.get("name", "")) for p in self.registry.providers]

    def provider(self, name: str) -> dict:
        for provider in self.registry.providers:
            if provider.get("name") == name:
                return provider
        raise KeyError(name)

    def catalog(self, name: str) -> CatalogService:
        """The provider's catalog, created on first use (no network)."""
        existing = self._by_name.get(name)
        if existing is not None:
            return existing
        spec = dict(self.provider(name))
        manager = ProviderManager(
            self.config, provider_name=name, providers=self.registry.providers
        )
        catalog = CatalogService(
            self.config,
            manager,
            self._make_epg(manager),
            runner=self._runner,
            factory=self._factory,
            parent=self,
            provider_name=name,
            link_ids=self._ids,
        )
        catalog.epg_enabled = self._guides is None or name in self._guides
        catalog.screen_4k = lambda: self.screen_4k()  # 2.8: asked at each media server play
        self._connect(name, catalog)
        self._by_name[name] = catalog
        self._specs[name] = spec
        self.catalogCreated.emit(name)
        return catalog

    def created(self) -> list[str]:
        """The providers whose catalogs exist now."""
        return list(self._by_name)

    def _connect(self, name: str, catalog: CatalogService) -> None:
        catalog.providerReady.connect(lambda *_: self._on_ready(name))
        catalog.providerFailed.connect(lambda _name, message: self._on_failed(name, message))
        catalog.epgReady.connect(lambda: self.epgReady.emit(name))
        catalog.linkReady.connect(self.linkReady)
        catalog.linkFailed.connect(self.linkFailed)
        for signal in (
            catalog.sectionReady,
            catalog.groupReady,
            catalog.seasonsReady,
            catalog.episodesReady,
        ):
            signal.connect(lambda *_: self.contentChanged.emit(name))

    def ensure(self, name: str) -> None:
        """Start this catalog's setup (cache load, STB sign-in) once, in its worker."""
        if self._closed or name in self._started:
            return
        catalog = self.catalog(name)
        self._started.add(name)
        catalog.select_provider()

    def select(self, name: str, *, force_refresh: bool = False) -> None:
        """(Re)start this catalog's setup, as the screens ask when a provider is chosen."""
        if self._closed:
            return
        if force_refresh and name in self._renewing:
            return  # reload() is setting it up again with its new settings already
        catalog = self.catalog(name)
        self._started.add(name)
        self._ready.discard(name)  # a link asked meanwhile waits for this setup
        catalog.select_provider(force_refresh=force_refresh)

    def sign_in_again(self, name: str) -> None:
        """Make the provider sign in anew: a link asked next is held until the new sign-in
        is ready (or fails with it)."""
        if self._closed or name not in self.names():
            return
        self._ready.discard(name)
        self._started.discard(name)
        self.ensure(name)

    _frozen = False

    @classmethod
    def _freeze_heap(cls) -> None:
        """Once, with the first provider loaded: the catalog (up to 57,000 channels a provider)
        is not scanned by the collector again. Collected first, so that no garbage cycle is
        frozen with it for good; freezing again would freeze whatever is pending each time."""
        if not cls._frozen:
            cls._frozen = True
            gc.collect()
            gc.freeze()

    def is_ready(self, name: str) -> bool:
        return name in self._ready

    def holds(self, request_id: int) -> bool:
        """Whether link request `request_id` waits for its provider's setup. It is still held
        while that provider's providerReady is emitted (it is answered right after): a view
        that drops the requests a setup dropped keeps this one."""
        return any(held[0] == request_id for waiting in self._waiting.values() for held in waiting)

    def forget(self, request_id: int) -> None:
        """Drop link request `request_id` held for its provider's setup (a newer choice was
        asked instead): it is never asked of the portal, and never answered."""
        for name, waiting in list(self._waiting.items()):
            kept = [held for held in waiting if held[0] != request_id]
            if kept:
                self._waiting[name] = kept
            else:
                del self._waiting[name]

    def loaded_contents(self) -> dict[str, dict]:
        """The content the catalogs of the ready providers hold in memory, by provider name:
        what Search reads instead of parsing every cache file again (read only)."""
        loaded: dict[str, dict] = {}
        for name in self.names():
            catalog = self._by_name.get(name)
            if catalog is not None and name in self._ready:
                content = catalog.provider_manager.current_provider_content
                if isinstance(content, dict) and content:
                    loaded[name] = content
        return loaded

    def _on_ready(self, name: str) -> None:
        self._ready.add(name)
        self._renewing.discard(name)
        self._freeze_heap()
        self.providerReady.emit(name)  # the links held for it are still held (see holds)
        self.contentChanged.emit(name)
        catalog = self._by_name.get(name)
        for request_id, ask in self._waiting.pop(name, []):
            if catalog is not None:
                self._ids.reserve(request_id)
                ask(catalog)

    def _on_failed(self, name: str, message: str) -> None:
        self._ready.discard(name)
        self._started.discard(name)  # the next request tries again
        self._renewing.discard(name)
        held = self._waiting.pop(name, [])  # no longer held: they fail with the setup
        self.providerFailed.emit(name, message)
        for request_id, *_ in held:
            self.linkFailed.emit(request_id, message)

    # --- links and guides ---------------------------------------------------------------
    def _route(self, item: dict) -> str:
        """The provider an item belongs to; an unstamped item goes to the selected one (the
        first one when the selection names none, as a page falls back)."""
        name = provider_of(item)
        if not name:
            names = self.names()
            name = str(self.config.selected_provider_name or "")
            if name not in names and names:
                name = names[0]
            logger.debug("An unstamped item goes to the selected provider")
        return name

    def request_link(self, item: dict, *, section: str, is_episode: bool = False) -> int:
        return self._link(
            item, lambda catalog: catalog.request_link(item, section=section, is_episode=is_episode)
        )

    def request_archive_link(self, item: dict, programme: str) -> int:
        """The link of a programme's recording in the item's STB portal's archive (catch-up;
        see CatalogService.request_archive_link), asked as `request_link` asks a link."""
        return self._link(item, lambda catalog: catalog.request_archive_link(item, programme))

    def _link(self, item: dict, ask: Callable[[CatalogService], int]) -> int:
        """Make a link request (`ask`) of the item's provider's catalog: now when it is ready,
        else once its setup is done."""
        name = self._route(item)
        if name not in self._by_name and name not in self.names():
            request_id = next(self._ids)
            QTimer.singleShot(
                0, self, lambda: self.linkFailed.emit(request_id, UNKNOWN_PROVIDER_MESSAGE)
            )
            return request_id
        if name in self._ready:
            return ask(self.catalog(name))
        # Never set up (or still signing in): the request runs once the catalog is ready.
        request_id = next(self._ids)
        self._waiting.setdefault(name, []).append((request_id, ask))
        self.ensure(name)
        return request_id

    def programmes(self, channel: dict, now: datetime, limit: int = 8) -> list[Programme]:
        catalog = self._by_name.get(self._route(channel))
        return catalog.programmes(channel, now, limit) if catalog is not None else []

    def max_connections(self, name: str) -> Optional[int]:
        catalog = self._by_name.get(name)
        return catalog.max_connections() if catalog is not None else None

    def archive_zone(self, name: str) -> Optional[tzinfo]:
        """The time zone provider `name`'s archive addresses are in (None: not known)."""
        catalog = self._by_name.get(name)
        return catalog.archive_zone() if catalog is not None else None

    def xtream_zone_at(self, host: str) -> Optional[tzinfo]:
        """The time zone of a signed-in provider whose streams are at `host` (an M3U
        playlist's xc channels on an Xtream provider's server), when one knows it."""
        for catalog in self._by_name.values():
            zone = catalog.archive_zone()
            if zone is not None and host in catalog.stream_hosts():
                return zone
        return None

    def catchup_source(self, name: str) -> str:
        """Provider `name`'s playlist-wide catch-up template ("" for none)."""
        catalog = self._by_name.get(name)
        return catalog.catchup_source() if catalog is not None else ""

    def use_guides(self, names: set[str]) -> None:
        """Keep the guides of `names` loaded; the others give their memory back."""
        self._guides = set(names)
        for name in names:
            if name not in self._by_name and name not in self.names():
                continue
            self.ensure(name)
            catalog = self.catalog(name)
            catalog.epg_enabled = True
            if name in self._ready and not catalog.epg_ready() and not catalog.epg_loading():
                catalog.refresh_epg()
        for name, catalog in self._by_name.items():
            if name not in names:
                catalog.epg_enabled = False
                catalog.release_epg()

    def refresh_guides(self, *, skip: str = "", force: bool = False) -> None:
        """The guides in use load again (the periodic refresh), except `skip`'s."""
        for name in self._guides or ():
            catalog = self._by_name.get(name)
            if catalog is not None and name != skip and name in self._ready:
                catalog.refresh_epg(force=force)

    # --- Settings saved ---------------------------------------------------------------------
    def reload(self) -> None:
        """Drop the catalogs of removed providers and rebuild edited ones (on next use),
        except those in use: a removed one stays until a later reload() finds it out of use,
        and an edited one is set up again at once with its new settings (its catalog stays,
        so what it plays keeps playing; its lists come again from the new sign-in)."""
        current = {str(p.get("name", "")): p for p in self.registry.providers}
        busy = set(self._in_use())
        for name in list(self._by_name):
            if name in current and self._specs.get(name) == current[name]:
                continue
            if name not in busy:
                self._drop(name)
            elif name in current:
                self._renew(name, current[name])
        self.providersChanged.emit()

    def _renew(self, name: str, spec: dict) -> None:
        """Set provider `name` up again with its edited settings: its pinned manager reads
        the new entry, and a link asked meanwhile waits for the new sign-in. An edit that
        keeps the connection (only the stream limit, say) loads nothing again: the lists
        stay where they are and the manager holds the new entry. While a setup runs (or has not
        finished) the manager still holds the one before it: nothing is skipped then."""
        self._specs[name] = dict(spec)
        manager = self._by_name[name].provider_manager
        settled = name in self._ready and name not in self._renewing
        if settled and self._same_connection(manager, spec):
            if manager.current_provider.get("name") == name:
                manager.current_provider = spec  # the list's entry, as a setup takes it
            return
        self._ready.discard(name)
        self._renewing.discard(name)  # a setup running with the entry before: this one replaces it
        self.select(name, force_refresh=True)
        if name not in self._ready:  # a runner may finish the setup at once
            self._renewing.add(name)

    @staticmethod
    def _same_connection(manager: ProviderManager, spec: dict) -> bool:
        """Whether the lists the provider's catalog holds came over the edited entry's
        connection: the cache identity Settings compares to decide its own refresh."""
        metadata = manager.current_provider_content.get("_cache")
        if not isinstance(metadata, dict):
            return False  # nothing loaded (or a cache from before identities): load again
        return bool(metadata.get("identity") == manager.provider_cache_identity(spec))

    def _drop(self, name: str) -> None:
        catalog = self._by_name.pop(name)
        self._specs.pop(name, None)
        self._started.discard(name)
        self._ready.discard(name)
        self._renewing.discard(name)
        for request_id, *_ in self._waiting.pop(name, []):
            self.linkFailed.emit(request_id, UNKNOWN_PROVIDER_MESSAGE)
        self._stop(catalog)
        # Its guide and its lists (57,000 channels, say) go now, whoever still holds it.
        catalog.release_epg()
        catalog.provider_manager.current_provider_content = {}
        catalog.deleteLater()

    @staticmethod
    def _stop(catalog: CatalogService) -> None:
        catalog.shutdown()
        catalog.provider_manager._cache_writer.shutdown(wait=False)

    def save_epg_indexes(self) -> None:
        """The window closes: each guide manager saves its index."""
        for catalog in self._by_name.values():
            catalog.epg_manager.save_index()

    def shutdown(self) -> None:
        self._closed = True
        self._waiting.clear()
        for catalog in self._by_name.values():
            self._stop(catalog)
