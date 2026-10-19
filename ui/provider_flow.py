"""Provider setups as the TV shell sees them (spec §8, R5).

A setup shows "Loading <provider>…" until the first live section (or a failure); with no
provider at all, the welcome card shows instead. When a provider becomes ready, the newest
link request is asked again (a setup drops older results), the last live channel is tuned
once its live section arrives (the "start with the last channel" setting), and otherwise the
drawer opens on an idle player. A failed setup opens the drawer on the sources with the retry
row. While the Library shows (spec §5.4), a provider change neither tunes nor opens the
drawer; back to live TV (`resume_live`) tunes the last channel, or opens the drawer.

In "All providers" the page's own setup is ALL_PROVIDERS'; each provider finishing or failing
its setup there only changes the lists' content (a failure is said, and shown under its
heading). A last channel whose provider has not listed its channels yet waits for it (as one
of a provider other than the one browsed does), never opening the drawer meanwhile; one whose
provider's setup failed is not waited for (that would sign it in again).

A failure it says (a setup, the live section, a last channel's provider) is followed by
`explain`, which asks that provider's account why (TVController._explain).

TVController owns the links, the zapping and the player, and hands this unit callbacks.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer

from services.catalog import ALL_GROUP_ID, PROVIDER_LOADING_MESSAGE
from services.failure_reasons import shown_message
from services.i18n import tr, translate
from services.navigation import FAVORITES_GROUP, RECENT_GROUP, NavNode, payloads
from services.provider_ids import ALL_PROVIDERS, ALL_PROVIDERS_TITLE, provider_of, with_provider
from services.zapping import ZapContext
from ui.item_provider import provider_type as item_provider_type

logger = logging.getLogger(__name__)

_PINNED_GROUPS = (FAVORITES_GROUP, RECENT_GROUP)


FOREIGN_WAIT_MS = 25_000  # the longest wait for another provider's channels


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
        explain: Callable[[str, str], None] = lambda provider, title: None,
        parent: Optional[QObject] = None,
    ) -> None:
        """`explain(provider, title)` asks why the failure just said happened at `provider`
        (TVController._explain)."""
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
        self._explain = explain
        self._await_live = False  # after providerReady: the first live section decides
        self._loading = False  # the "Loading <provider>…" hint is up
        self._autotune: Optional[tuple[dict, str]] = None
        self._in_library = False  # the window shows the Library: nothing live starts
        self._live_on_ready = False  # back from the Library during a setup: tune once ready
        # The last channel of a provider other than the one browsed: (provider, stamped item),
        # tuned once that provider's own catalog has its channels.
        self._foreign: Optional[tuple[str, dict]] = None
        self._foreign_asked = False
        # The catalog whose live failure is heard, by owner: one the hub rebuilds (an edit in
        # Settings) is a new object, heard when it is next waited on.
        self._foreign_listening: dict[str, object] = {}
        self._foreign_timer = QTimer(self, singleShot=True, interval=FOREIGN_WAIT_MS)
        self._foreign_timer.timeout.connect(self._on_foreign_timeout)

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
            changed = getattr(hub, "providersChanged", None)
            if changed is not None:
                changed.connect(self._forget_dropped_catalogs)

    def _forget_dropped_catalogs(self) -> None:
        """The provider list changed: catalogs the hub dropped are let go."""
        created = getattr(self._hub(), "created", None)
        if created is not None:
            alive = set(created())
            for owner in [o for o in self._foreign_listening if o not in alive]:
                del self._foreign_listening[owner]

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
        self._foreign_timer.stop()

    def shows_failure(self, name: str) -> bool:
        """Whether a failed setup of provider `name` reaches this flow, which says so: the
        list shows that provider, or every provider."""
        source = getattr(self.catalog, "source", self.catalog.current_provider_name)()
        return source in (name, ALL_PROVIDERS)

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
        self._foreign_timer.stop()

    def resume_live(self) -> None:
        """Back to live TV from the Library (R5): the last live channel again, else the drawer.

        Tuned whatever the "start with the last channel" setting says (that one is about
        launching QiTV). During a provider setup it is tuned once the provider is ready.
        """
        self._in_library = False
        if not self.catalog.provider_names():
            self.welcome()
            return
        if self.catalog.is_loading() and not self._mixed():
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
        self._foreign_timer.stop()
        self._loading = False

    # ---- hints --------------------------------------------------------------------------------

    def welcome(self) -> None:
        """Until a provider exists: the welcome card, whose button adds one (spec §8)."""
        self._drop_autoplay()
        self._loading = False
        self.overlays.toast.set_hint(None)
        self.overlays.welcome.show_card()

    def _setup_name(self, name: Optional[str]) -> str:
        """The provider a setup for `name` loads, as ProviderManager picks it (every provider
        for ALL_PROVIDERS, or for None while the page shows them all), as the hint names it."""
        if name == ALL_PROVIDERS or (name is None and self._mixed()):
            return translate(ALL_PROVIDERS_TITLE)
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
        # i18n: a hint while a provider's channels load; name is the provider's
        self.overlays.toast.set_hint(tr("Loading {name}…", name=name) if name else tr("Loading…"))

    def _end_loading(self) -> None:
        if self._loading:
            self._loading = False
            self.overlays.toast.set_hint(self._autoplay_hint())  # a wait that went on shows

    def wait_hint(self, text: Optional[str]) -> None:
        """Autoplay's "Loading next season…": a provider setup's hint comes first."""
        if not self._loading:
            self.overlays.toast.set_hint(text)

    # ---- catalog events -----------------------------------------------------------------------

    def _mixed(self) -> bool:
        """The page shows every provider ("All providers")."""
        return bool(self.catalog.current_provider_name() == ALL_PROVIDERS)

    def _on_provider_ready(self, name: str) -> None:
        if self._closed():
            return
        if name != ALL_PROVIDERS and self._mixed():
            # One provider of All: new content in the lists (the path stays); a last channel
            # waiting for this provider is tuned once its channels are listed (_try_foreign).
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
        self._foreign_timer.stop()
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
        self._foreign_timer.start()
        self._hub().ensure(foreign[0])
        self._try_foreign()
        return True

    def _try_foreign(self, *_args) -> None:
        if self._foreign is None:
            self._foreign_timer.stop()
            return
        if self._closed():
            return
        hub = self._hub()
        owner, item = self._foreign
        if not hub.is_ready(owner):
            return
        catalog = hub.catalog(owner)
        if not catalog.has_content("live"):
            if not self._foreign_asked:
                self._foreign_asked = True
                if self._foreign_listening.get(owner) is not catalog:
                    self._foreign_listening[owner] = catalog
                    catalog.sectionFailed.connect(
                        lambda section, message, owner=owner: self._on_foreign_section_failed(
                            owner, section, message
                        )
                    )
                catalog.request_section("live")
            return
        self._foreign = None
        self._foreign_timer.stop()
        if self._has_media() or self._links_busy() or self._in_library:
            return  # something else was chosen meanwhile
        channels = list(catalog.items("live", ALL_GROUP_ID) or [])
        if self._mixed():  # ↑/↓ walk every provider's channels, as in All
            channels = list(self.catalog.items("live", ALL_GROUP_ID) or channels)
        found = self._listed(channels, item)
        if found is None:
            logger.info("The last channel %r is no longer listed", item.get("name"))
            self._open_drawer_if_idle()
            return
        self._tune(found, ALL_GROUP_ID, channels)

    def _on_foreign_failed(self, name: str, _message: str) -> None:
        if self._foreign is not None and self._foreign[0] == name:
            self._give_up_foreign()

    def _on_foreign_section_failed(self, owner: str, section: str, message: str) -> None:
        if section != "live" or self._foreign is None or self._foreign[0] != owner:
            return  # not the wait of this owner (a later wait ignores an old failure)
        if not self._closed():
            self._error(tr("{name}: {message}", name=owner, message=shown_message(message)))
            self._explain(owner, owner)
        self._give_up_foreign()

    def _on_foreign_timeout(self) -> None:
        if self._foreign is not None:
            logger.info("Gave up waiting for the channels of %r", self._foreign[0])
            self._give_up_foreign()

    def _give_up_foreign(self) -> None:
        """The last channel's provider can't list it: the drawer, never a black screen."""
        self._foreign = None
        self._foreign_timer.stop()
        if not self._closed():
            self._open_drawer_if_idle()

    def _on_provider_failed(self, name: str, message: str) -> None:
        if self._closed():
            return
        self._end_loading()
        shown = shown_message(message)
        self._error(f"{name}: {shown}" if name else shown)
        self._explain(name, name)
        if name != ALL_PROVIDERS and self._mixed():
            # One provider of All: its saved channels stay listed (its error under its heading
            # when it has none), and the list stays on All providers. A last channel waiting
            # for it gives up (_on_foreign_failed).
            return
        # The sources, on the failed one, with a row to connect again: never a black screen.
        if self._in_library:
            return  # back to live TV opens the drawer on it (resume_live)
        if self._has_media() and not self.overlays.drawer.isVisible():
            return  # the saved catalog still plays; the drawer offers the retry when opened
        self.nav.focus_source(name or self.catalog.current_provider_name())
        self.overlays.drawer.open_drawer(focus_side="left")

    def _on_provider_requested(self, name: str) -> None:
        """A source chosen in the drawer started a setup (`name`: its title)."""
        if not self._closed():
            self._show_loading(translate(name) if name == ALL_PROVIDERS_TITLE else name)

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
        # i18n: a toast: the live channels failed to load, and why
        self._error(tr("Live TV: {message}", message=shown_message(message)))
        self._explain(self.catalog.current_provider_name(), tr("Live TV"))
        self._open_drawer_if_idle()  # it shows the retry row

    def _open_drawer_if_idle(self) -> None:
        drawer = self.overlays.drawer
        busy = self._has_media() or self._links_busy() or self._in_library
        if not busy and not drawer.isVisible():
            drawer.open_drawer()

    def _tune_last(self, item: dict, group: str) -> bool:
        for key in dict.fromkeys((group, ALL_GROUP_ID)):
            channels = self._group_channels(key)
            found = self._listed(channels, item)
            if found is not None:
                self._tune(found, key, channels)
                return True
        if self._start_foreign(self._waiting_owner(item)):
            return True  # All providers: its provider has not listed its channels yet
        logger.info("The last channel %r is no longer listed", item.get("name"))
        return False

    def _listed(self, channels: list[dict], item: dict) -> Optional[dict]:
        """The listed channel the last channel `item` is. An Xtream channel's address holds the
        server, user name and password, so after its provider's settings change the address
        no longer matches: its stream id (unique at that provider) still names it."""
        found = next((c for c in channels if ZapContext.same(c, item)), None)
        if found is not None or item_provider_type(self.catalog, item) != "XTREAM":
            return found
        stream_id, owner = str(item.get("id") or ""), provider_of(item)
        if not stream_id:
            return None
        return next(
            (
                c
                for c in channels
                if str(c.get("id") or "") == stream_id
                and (not owner or not provider_of(c) or provider_of(c) == owner)
            ),
            None,
        )

    def _waiting_owner(self, item: dict) -> Optional[tuple[str, dict]]:
        """All providers: (its provider, `item`) while that provider has not listed its
        channels yet (it sets up, or they load); None otherwise, and when its setup failed
        (said once already: waiting would sign it in again, and fail again)."""
        hub, owner = self._hub(), provider_of(item)
        if hub is None or not self._mixed() or owner not in self.catalog.provider_names():
            return None
        if hub.is_ready(owner) and hub.catalog(owner).has_content("live"):
            return None  # listed, and the channel is not among them
        if self.nav.provider_error(NavNode("source", owner, owner, owner)):
            return None  # its setup failed, until it is set up again
        return owner, item

    def _group_channels(self, group_key: str) -> list[dict]:
        if group_key in _PINNED_GROUPS:
            nodes = self.nav.children(NavNode("group", group_key, group_key)) or []
            return payloads(nodes)
        return list(self.catalog.items("live", group_key) or [])
