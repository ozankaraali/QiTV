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
from services.zapping import ZapContext
from ui.link_lanes import Target

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
        settle_links: Callable[[bool], Optional[Target]],
        request: Callable[[Target], None],
        has_media: Callable[[], bool],
        links_busy: Callable[[], bool],
        drop_links: Callable[[], None],
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
        self._settle_links = settle_links
        self._request = request
        self._has_media = has_media
        self._links_busy = links_busy
        self._drop_links = drop_links
        self._tune = tune
        self._open_drawer = open_drawer
        self._live_section_loaded = live_section_loaded
        self._error = error
        self._drop_autoplay = drop_autoplay
        self._autoplay_hint = autoplay_hint
        self._provider: Optional[str] = None
        self._await_live = False  # after providerReady: the first live section decides
        self._loading = False  # the "Loading <provider>…" hint is up
        self._autotune: Optional[tuple[dict, str]] = None
        self._in_library = False  # the window shows the Library: nothing live starts
        self._live_on_ready = False  # back from the Library during a setup: tune once ready

    def wire(self) -> None:
        catalog = self.catalog
        catalog.providerReady.connect(self._on_provider_ready)
        catalog.providerFailed.connect(self._on_provider_failed)
        catalog.sectionReady.connect(self._on_section_ready)
        catalog.sectionFailed.connect(self._on_section_failed)
        self.nav.providerRequested.connect(self._on_provider_requested)

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

    def in_library(self) -> bool:
        """The window shows the Library (from enter_library until the player is back)."""
        return self._in_library

    def leave_library(self) -> None:
        """Something plays (a movie, a channel, a file): the player is back."""
        self._in_library = False

    def drop_autotune(self) -> None:
        """A choice made before the live section loaded comes first."""
        self._autotune = None

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
        self._open_drawer()

    def shutdown(self) -> None:
        self._autotune = None
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
        # A provider setup makes the catalog drop link results requested before it, so a
        # request still in flight may never settle: ask again for the newest target, unless
        # the provider changed and its channels belong to the old one.
        pending = self._settle_links(self._provider is not None and name != self._provider)
        self._provider = name
        self.overlays.welcome.dismiss()
        if pending is not None:
            self._request(pending)
        back_to_live, self._live_on_ready = self._live_on_ready, False
        last = self._last_channel(name)
        idle = pending is None and not self._has_media()
        wanted = (
            idle and not self._in_library and (back_to_live or self.config.start_with_last_channel)
        )
        self._autotune = last if wanted else None
        self._await_live = True
        if last is not None:
            self.nav.reset_to("live", last[1])
        else:
            self.nav.reset_to("live")

    def _last_channel(self, provider: str) -> Optional[tuple[dict, str]]:
        """The last live channel watched on `provider` and its group, from the config."""
        record = self.config.last_live_channel
        if (
            isinstance(record, dict)
            and record.get("provider_name") == provider
            and isinstance(record.get("item"), dict)
        ):
            return record["item"], str(record.get("group_id") or ALL_GROUP_ID)
        return None

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed():
            return
        self._drop_links()
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
