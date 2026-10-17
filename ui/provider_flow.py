"""Provider setups as the TV shell sees them (spec §8, R5).

A setup shows "Loading <provider>…" until the first live section (or a failure); with no
provider at all, the welcome card shows instead. When a provider becomes ready, the newest
link request is asked again (a setup drops older results), the last live channel is tuned
once its live section arrives (the "start with the last channel" setting), and otherwise the
drawer opens on an idle player. A failed setup opens the drawer on the sources with the retry
row. While the Library shows (spec §5.4), a provider change neither tunes nor opens the
drawer; back to live TV (`resume_live`) tunes the last channel, or opens the drawer.

TVController owns the links, the zapping and the player, and hands this unit callbacks.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject

from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode
from services.provider_ids import ALL_PROVIDERS, with_provider
from services.zapping import ZapContext

logger = logging.getLogger(__name__)

_PINNED_GROUPS = (FAVORITES_GROUP, RECENT_GROUP)


class ProviderFlow(QObject):
    def __init__(
        self,
        *,
        catalog,
        nav,
        config,
        overlays,
        closed: Callable[[], bool],
        has_media: Callable[[], bool],
        links_busy: Callable[[], bool],
        tune: Callable[[dict, str, list[dict]], None],
        open_drawer: Callable[[], None],
        live_section_loaded: Callable[[], None],
        error: Callable[[str], None],
        drop_autoplay: Callable[[], None],
        autoplay_hint: Callable[[], Optional[str]],
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.nav = nav
        self.config = config
        self.overlays = overlays
        self._closed = closed
        self._has_media = has_media
        self._links_busy = links_busy
        self._tune = tune
        self._open_drawer = open_drawer
        self._live_section_loaded = live_section_loaded
        self._error = error
        self._drop_autoplay = drop_autoplay
        self._autoplay_hint = autoplay_hint
        self._await_live = False  # after providerReady: the first live section decides
        self._loading = False  # the "Loading <provider>…" hint is up
        self._autotune: Optional[tuple[dict, str]] = None
        self._in_library = False  # the window shows the Library: nothing live starts
        self._live_on_ready = False  # back from the Library during a setup: tune once ready
        # The last channel of a provider other than the one browsed: (provider, stamped item),
        # tuned once that provider's own catalog has its channels.
        self._foreign: Optional[tuple[str, dict]] = None
        self._foreign_asked = False

    def wire(self) -> None:
        catalog = self.catalog
        catalog.providerReady.connect(self._on_provider_ready)
        catalog.providerFailed.connect(self._on_provider_failed)
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        self.nav.providerRequested.connect(self._on_provider_requested)
        hub = self._hub()
        if hub is not None:
            hub.contentChanged.connect(self._try_foreign)
            hub.providerFailed.connect(self._on_foreign_failed)

    # ---- the shell's calls --------------------------------------------------------------------

    def start(self) -> None:
        if not self.catalog.provider_names():
            # ProviderManager cannot set up without providers; the setup would only fail.
            self.welcome()
            return
        self._show_loading(self._setup_name(None))
        self.catalog.select_provider()

    def select_provider(self, name: Optional[str] = None, *, force_refresh: bool = False) -> None:
        if not self.catalog.provider_names():
            self.welcome()
            return
        self._show_loading(self._setup_name(name))
        # Pending links are settled when the provider is ready (see _on_provider_ready).
        self.catalog.select_provider(name, force_refresh=force_refresh)

    def enter_library(self) -> None:
        """The window shows the Library: nothing live is started until the player is back."""
        self._in_library = True
        self._live_on_ready = False
        self._autotune = None
        self._foreign = None

    def in_library(self) -> bool:
        """The window shows the Library (from enter_library until the player is back)."""
        return self._in_library

    def leave_library(self) -> None:
        """Something plays (a movie, a channel, a file): the player is back."""
        self._in_library = False

    def drop_autotune(self) -> None:
        """A choice made before the live section loaded comes first."""
        self._autotune = None
        self._foreign = None

    def resume_live(self) -> None:
        """Back to live TV from the Library (R5): the last live channel again, else the drawer.

        Tuned whatever the "start with the last channel" setting says (that one is about
        launching QiTV). During a provider setup it is tuned once the provider is ready.
        """
        self._in_library = False
        if not self.catalog.provider_names():
            self.welcome()
            return
        if self.catalog.is_loading():
            self._live_on_ready = True  # _on_provider_ready decides
            return
        name = self.catalog.current_provider_name()
        if self.nav.provider_error(NavNode("source", name, name, name)):
            # Its setup failed while the Library showed: the sources, with the retry row.
            self.nav.focus_source(name)
            self.overlays.drawer.open_drawer(focus_side="left")
            return
        last = self._last_channel(name)
        if self._await_live:
            self._autotune = last  # the live section is on its way: tuned when it is ready
            return
        if last is not None and self._tune_last(*last):
            return
        if last is None and self._start_foreign(self._foreign_channel(name)):
            return
        self._open_drawer()

    def shutdown(self) -> None:
        self._autotune = None
        self._foreign = None
        self._loading = False

    # ---- hints --------------------------------------------------------------------------------

    def welcome(self) -> None:
        """Until a provider exists: the welcome card, whose button adds one (spec §8)."""
        self._drop_autoplay()
        self._loading = False
        self.overlays.toast.set_hint(None)
        self.overlays.welcome.show_card()

    def _setup_name(self, name: Optional[str]) -> str:
        """The provider a setup for `name` loads, as ProviderManager picks it."""
        names = self.catalog.provider_names()
        wanted = name if name is not None else getattr(self.config, "selected_provider_name", None)
        if wanted in names:
            return str(wanted)
        return names[0] if names else ""

    def _show_loading(self, name: str) -> None:
        """From a setup's start until the first live section or a failure (Review Focus 5)."""
        self._drop_autoplay()  # a provider setup: and the hint is the setup's
        self.overlays.welcome.dismiss()  # a provider exists now
        self._loading = True
        self.overlays.toast.set_hint(f"Loading {name}…" if name else "Loading…")

    def _end_loading(self) -> None:
        if self._loading:
            self._loading = False
            self.overlays.toast.set_hint(self._autoplay_hint())  # a wait that went on shows

    def wait_hint(self, text: Optional[str]) -> None:
        """Autoplay's "Loading next season…": a provider setup's hint comes first."""
        if not self._loading:
            self.overlays.toast.set_hint(text)

    # ---- catalog events -----------------------------------------------------------------------

    def _on_provider_ready(self, name: str) -> None:
        if self._closed():
            return
        # Links the setup dropped are asked again by the controller, per provider.
        self.overlays.welcome.dismiss()
        back_to_live, self._live_on_ready = self._live_on_ready, False
        last = self._last_channel(name)
        idle = not self._has_media() and not self._links_busy()
        wanted = (
            idle and not self._in_library and (back_to_live or self.config.start_with_last_channel)
        )
        self._autotune = last if wanted else None
        self._foreign = None
        if wanted and last is None:
            self._start_foreign(self._foreign_channel(name))
        self._await_live = True
        if last is not None:
            self.nav.reset_to("live", last[1])
        else:
            self.nav.reset_to("live")

    def _last_record(self) -> Optional[tuple[str, dict, str]]:
        """(provider, item, group) of the last live channel from the config, if any."""
        record = self.config.last_live_channel
        if (
            isinstance(record, dict)
            and isinstance(record.get("item"), dict)
            and isinstance(record.get("provider_name"), str)
        ):
            return record["provider_name"], record["item"], str(record.get("group_id") or "")
        return None

    def _last_channel(self, provider: str) -> Optional[tuple[dict, str]]:
        """The last live channel watched on `provider` and its group, from the config.

        When the list mixes every provider, the last channel of any provider still in the
        list (stamped with it), looked up among all of them.
        """
        last = self._last_record()
        if last is None:
            return None
        owner, item, group = last
        if provider == ALL_PROVIDERS and owner in self.catalog.provider_names():
            return with_provider(item, owner), ALL_GROUP_ID
        if owner == provider:
            return item, group or ALL_GROUP_ID
        return None

    def _foreign_channel(self, browsed: str) -> Optional[tuple[str, dict]]:
        """The last live channel when it belongs to another provider than the one browsed."""
        last = self._last_record()
        mixed = getattr(self.catalog, "all_providers", lambda: False)()
        if last is None or self._hub() is None or mixed or browsed == ALL_PROVIDERS:
            return None
        owner, item, _group = last
        if owner == browsed or owner not in self.catalog.provider_names():
            return None
        return owner, with_provider(item, owner)

    def _hub(self):
        """The catalogs hub behind the page's view (None for a single plain catalog)."""
        return getattr(self.catalog, "catalogs", None)

    def _start_foreign(self, foreign: Optional[tuple[str, dict]]) -> bool:
        """Tune `foreign` from its own provider's catalog once it has its channels."""
        if foreign is None:
            return False
        self._foreign, self._foreign_asked = foreign, False
        self._hub().ensure(foreign[0])
        self._try_foreign()
        return True

    def _try_foreign(self, *_args) -> None:
        if self._foreign is None or self._closed():
            return
        hub = self._hub()
        owner, item = self._foreign
        if not hub.is_ready(owner):
            return
        catalog = hub.catalog(owner)
        if not catalog.has_content("live"):
            if not self._foreign_asked:
                self._foreign_asked = True
                catalog.request_section("live")
            return
        self._foreign = None
        if self._has_media() or self._in_library:
            return  # something else was chosen meanwhile
        channels = list(catalog.items("live", ALL_GROUP_ID) or [])
        found = next((c for c in channels if ZapContext.same(c, item)), None)
        if found is None:
            logger.info("The last channel %r is no longer listed", item.get("name"))
            self._open_drawer_if_idle()
            return
        self._tune(found, ALL_GROUP_ID, channels)

    def _on_foreign_failed(self, name: str, _message: str) -> None:
        if self._foreign is not None and self._foreign[0] == name:
            self._foreign = None
            self._open_drawer_if_idle()

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed():
            return
        self._end_loading()
        self._error(f"{name}: {message}" if name else message)
        # The sources, on the failed one, with a row to connect again: never a black screen.
        if self._in_library:
            return  # back to live TV opens the drawer on it (resume_live)
        if self._has_media() and not self.overlays.drawer.isVisible():
            return  # the saved catalog still plays; the drawer offers the retry when opened
        self.nav.focus_source(name or self.catalog.current_provider_name())
        self.overlays.drawer.open_drawer(focus_side="left")

    def _on_provider_requested(self, name: str) -> None:
        """A source chosen in the drawer started a setup."""
        if not self._closed():
            self._show_loading(name)

    def _on_section_ready(self, section: str) -> None:
        if self._closed() or section != "live":
            return
        self._live_section_loaded()
        if not self._await_live:
            return
        self._await_live = False
        self._end_loading()
        autotune, self._autotune = self._autotune, None
        if self._in_library:
            return  # back to live TV decides (resume_live)
        if autotune is not None and self._tune_last(*autotune):
            return
        if self._foreign is not None:
            return  # the last channel of another provider is on its way
        self._open_drawer_if_idle()

    def _on_section_failed(self, section: str, message: str) -> None:
        if self._closed() or section != "live" or not self._await_live:
            return
        if message == PROVIDER_LOADING_MESSAGE:
            return  # a setup is running; its providerReady arms this again
        if self.catalog.groups("live"):
            # NavModel keeps showing the saved catalog and posts a notice (toasted).
            self._on_section_ready(section)
            return
        self._await_live = False
        self._autotune = None
        self._end_loading()
        self._error(f"Live TV: {message}")
        self._open_drawer_if_idle()  # it shows the retry row

    def _open_drawer_if_idle(self) -> None:
        drawer = self.overlays.drawer
        busy = self._has_media() or self._links_busy() or self._in_library
        if not busy and not drawer.isVisible():
            drawer.open_drawer()

    def _tune_last(self, item: dict, group: str) -> bool:
        for key in dict.fromkeys((group, ALL_GROUP_ID)):
            channels = self._group_channels(key)
            found = next((c for c in channels if ZapContext.same(c, item)), None)
            if found is not None:
                self._tune(found, key, channels)
                return True
        logger.info("The last channel %r is no longer listed", item.get("name"))
        return False

    def _group_channels(self, group_key: str) -> list[dict]:
        if group_key in _PINNED_GROUPS:
            nodes = self.nav.children(NavNode("group", group_key, group_key)) or []
            return [n.payload for n in nodes if isinstance(n.payload, dict)]
        return list(self.catalog.items("live", group_key) or [])
