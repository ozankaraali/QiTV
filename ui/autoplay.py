"""Autoplay and Next for movies and episodes (Review Focus 3).

When a movie or an episode with a play context ends, the Next card offers what follows: the
next episode of its series, or the next title of its genre. Enter (or the countdown) plays it,
Esc leaves the player on the final frame with the transport. A following season that is not
loaded yet is requested first, with a hint; Esc ends that wait, and a failure says so. The
final episode of the final season offers nothing. External players never autoplay (as in
v1.13.8). The transport's Next plays what follows at once, without the card.

Autoplay never plays anything itself: it asks the shell's `play` (TVController.play_vod).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable, Optional

from PySide6.QtCore import QObject

from services.navigation import episode_key, item_key, item_title
from services.next_episode import (
    NeedEpisodes,
    advanced_context,
    episode_label,
    following,
    genre_title,
    has_next,
)
from ui.link_lanes import Target
from ui.overlays.next_card import NextCard

logger = logging.getLogger(__name__)

FINISHED_TEXT = "Finished"
NOTHING_NEXT_TEXT = "Nothing to play next"
NEXT_SEASON_FAILED_TEXT = "Couldn't load the next season"
LOADING_NEXT_SEASON_TEXT = "Loading next season…"


@dataclass
class _Awaiting:
    """Waiting for a season's episodes before what follows `target` can be named."""

    target: Target
    play_now: bool  # the transport's Next: play it at once, without the card
    series_id: str
    season_id: str


class Autoplay(QObject):
    def __init__(
        self,
        *,
        catalog,
        config,
        card: NextCard,
        play: Callable[[dict, str, dict], None],
        is_current: Callable[[Target], bool],
        external_mode: Callable[[], bool],
        notify: Callable[[str, str], None],
        set_hint: Callable[[Optional[str]], None],
        show_transport: Callable[[], None],
        parent: Optional[QObject] = None,
    ) -> None:
        """`play(item, kind, context)` plays; `is_current(target)`: nothing newer was chosen;
        `notify(text, kind)` toasts; `set_hint` shows (or with None clears) a lasting hint."""
        super().__init__(parent)
        self.catalog = catalog
        self.config = config
        self.card = card
        self._play = play
        self._is_current = is_current
        self._external_mode = external_mode
        self._notify = notify
        self._set_hint = set_hint
        self._show_transport = show_transport
        self._next: Optional[tuple[dict, str, dict]] = None  # what the card offers
        self._awaiting: Optional[_Awaiting] = None
        self._closed = False
        card.playNow.connect(self._on_play_now)
        card.cancelled.connect(self._on_cancelled)
        catalog.episodesReady.connect(self._on_episodes_ready)
        catalog.episodesFailed.connect(self._on_episodes_failed)

    # ---- the shell's calls --------------------------------------------------------------------

    @staticmethod
    def has_next(target: Target) -> bool:
        """Whether Next can follow `target` (the transport shows its Next button then)."""
        return not target.live and has_next(target.item_type, target.context or {})

    def on_ended(self, target: Target) -> bool:
        """`target` ended; True when autoplay takes over (it says "Finished" itself then)."""
        if self._closed or self._external_mode() or not self.has_next(target):
            return False  # external players never autoplay (as in v1.13.8)
        if not self._is_current(target):
            return False  # something else was chosen and is on its way
        episode = target.item_type == "episode"
        if not (self.config.auto_play_episodes if episode else self.config.auto_play_movies):
            return False
        self._advance(target, play_now=False)
        return True

    def next_requested(self, target: Target) -> None:
        """The transport's Next: what follows `target` plays at once."""
        if self._closed or not self.has_next(target):
            return
        self.drop()
        self._advance(target, play_now=True)

    def card_open(self) -> bool:
        return bool(self.card.isVisible())

    def waiting(self) -> bool:
        return self._awaiting is not None

    def play_now(self) -> bool:
        """Enter while the card shows: play it now."""
        if not self.card_open():
            return False
        self.card.activate()
        return True

    def cancel(self) -> bool:
        """Esc: the card closes on the final frame, or the wait for the next season ends."""
        if self.card_open():
            self.card.escape()
            return True
        if self._awaiting is not None:
            self._stop_waiting()
            return True
        return False

    def drop(self) -> None:
        """A newer choice: forget the offer and the wait; the card closes without a reply."""
        self._stop_waiting()
        if self._next is not None:
            self._next = None
            self.card.escape()

    def close(self) -> None:
        self.drop()
        self._closed = True

    # ---- what follows -------------------------------------------------------------------------

    def _advance(self, target: Target, *, play_now: bool, loaded: str = "") -> None:
        """Offer (with `play_now`, play) what follows `target`.

        `loaded` is the season whose episodes just arrived: one that still has none ends
        the wait as a failure instead of asking for it again.
        """
        context = target.context or {}
        kind = target.item_type
        result = following(
            kind,
            context,
            target.item,
            seasons=self.catalog.seasons,
            episodes=self.catalog.episodes,
            key=item_key,
            episode_key=episode_key,
        )
        if isinstance(result, NeedEpisodes):
            series = context.get("series") or {}
            season_id = str(result.season.get("id"))
            if season_id == loaded:
                logger.info("Season %s has no episodes after loading them", season_id)
                self._notify(NEXT_SEASON_FAILED_TEXT, "error")
                return
            self._awaiting = _Awaiting(target, play_now, str(series.get("id")), season_id)
            self._set_hint(LOADING_NEXT_SEASON_TEXT)
            self.catalog.request_episodes(series, result.season)
            return
        if result is None:
            self._notify(NOTHING_NEXT_TEXT if play_now else FINISHED_TEXT, "info")
            return
        episode = kind == "episode"
        episodes = self.catalog.episodes(context["series"], result.season) if episode else None
        offered = (result.item, kind, advanced_context(context, result, episodes))
        if play_now:
            self._play(*offered)
            return
        self._next = offered
        if episode:
            subtitle = episode_label(result.season, result.item)
        else:
            groups = ((group.id, group.title) for group in self.catalog.groups("movies"))
            subtitle = genre_title(result.item, groups)
        self.card.show_next(item_title(result.item, episode=episode), subtitle, kind=kind)

    def _stop_waiting(self) -> None:
        if self._awaiting is not None:
            self._awaiting = None
            self._set_hint(None)

    def _take_awaiting(self, series_id: str, season_id: str) -> Optional[_Awaiting]:
        awaiting = self._awaiting
        if self._closed or awaiting is None:
            return None
        if (awaiting.series_id, awaiting.season_id) != (series_id, season_id):
            return None
        self._stop_waiting()
        return awaiting if self._is_current(awaiting.target) else None

    def _on_episodes_ready(self, series_id: str, season_id: str) -> None:
        awaiting = self._take_awaiting(series_id, season_id)
        if awaiting is not None:
            self._advance(awaiting.target, play_now=awaiting.play_now, loaded=season_id)

    def _on_episodes_failed(self, series_id: str, season_id: str, _message: str) -> None:
        if self._take_awaiting(series_id, season_id) is not None:
            self._notify(NEXT_SEASON_FAILED_TEXT, "error")

    # ---- the card -----------------------------------------------------------------------------

    def _on_play_now(self) -> None:
        offered, self._next = self._next, None
        if offered is not None and not self._closed:
            self._play(*offered)

    def _on_cancelled(self) -> None:
        if self._closed or self._next is None:
            return  # dropped for a newer choice (see drop)
        self._next = None
        self._show_transport()  # the final frame stays, with the transport
