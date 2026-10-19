"""Next episode / next movie resolution for autoplay (pure; no Qt).

A play context is what the Library (and the drawer) hand to the player with a movie or an
episode: start_over, and an episode's series, season and season episodes, or a movie's
genre_items. Next and autoplay follow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Union

Key = Callable[[dict], str]

PLAY_CONTEXT_KEYS = ("start_over", "series", "season", "episodes", "genre_items")


@dataclass(frozen=True)
class NextUp:
    season: Optional[dict]
    item: dict


@dataclass(frozen=True)
class NeedEpisodes:
    """The following season's episodes are not loaded yet: request them, then resolve again."""

    season: dict


def _index(items: list[dict], target: dict, key: Key) -> int:
    wanted = key(target)
    return next((i for i, item in enumerate(items) if key(item) == wanted), -1)


def next_episode(
    seasons: list[dict],
    episodes_of: Callable[[dict], Optional[list[dict]]],
    season: dict,
    episode: dict,
    *,
    season_key: Key,
    episode_key: Key,
) -> Union[NextUp, NeedEpisodes, None]:
    current = episodes_of(season)
    if current is None:
        return NeedEpisodes(season)  # evicted from the cache: load it again
    if not current:
        return None
    i = _index(current, episode, episode_key)
    if i < 0:
        return None
    if i < len(current) - 1:
        return NextUp(season, current[i + 1])
    wanted = season_key(season)
    if wanted in ("", "None"):  # key-less seasons: locate by identity
        s = next((n for n, candidate in enumerate(seasons) if candidate is season), -1)
    else:
        s = _index(seasons, season, season_key)
    if s < 0:
        return None
    for following in seasons[s + 1 :]:
        episodes = episodes_of(following)
        if episodes is None:
            return NeedEpisodes(following)
        if episodes:
            return NextUp(following, episodes[0])
    return None


def next_movie(items: list[dict], current: dict, *, key: Key) -> Optional[dict]:
    i = _index(items, current, key)
    return items[i + 1] if 0 <= i < len(items) - 1 else None


def play_context(values: Optional[dict] = None) -> dict:
    """A play context with every key (LibraryController._context's), `values` over the defaults."""
    context: dict = {key: None for key in PLAY_CONTEXT_KEYS}
    context["start_over"] = False
    context.update(values or {})
    return context


def has_next(kind: str, context: dict) -> bool:
    """Whether Next can follow: an episode of a known series and season, a genre's movie."""
    if kind == "episode":
        return isinstance(context.get("series"), dict) and isinstance(context.get("season"), dict)
    return kind == "movie" and bool(context.get("genre_items"))


def advanced_context(context: dict, following: NextUp, episodes: Optional[list[dict]]) -> dict:
    """The context of what follows: its season and episodes for an episode, never start over."""
    advanced = {**context, "start_over": False}
    if following.season is not None:
        advanced["season"] = following.season
        advanced["episodes"] = episodes
    return advanced


def following(
    kind: str,
    context: dict,
    item: dict,
    *,
    seasons: Callable[[dict], Optional[list[dict]]],
    episodes: Callable[[dict, dict], Optional[list[dict]]],
    key: Key,
    episode_key: Key,
) -> Union[NextUp, NeedEpisodes, None]:
    """What follows `item` (an "episode" or a "movie") in its play context.

    `seasons(series)` and `episodes(series, season)` read the loaded catalog (None: not
    loaded). Without the series' seasons only the current season is searched.
    """
    if kind == "episode":
        series, season = context.get("series"), context.get("season")
        if not isinstance(series, dict) or not isinstance(season, dict):
            return None
        return next_episode(
            seasons(series) or [season],
            lambda s: episodes(series, s),
            season,
            item,
            season_key=key,
            episode_key=episode_key,
        )
    after = next_movie(list(context.get("genre_items") or []), item, key=key)
    return None if after is None else NextUp(None, after)


def season_and_episode(season: Optional[dict], episode: dict) -> tuple[Any, Any]:
    """An episode's season number (the season's, else the episode's own) and its number, as
    the catalog has them (None when it has none)."""
    number = (season or {}).get("number") or episode.get("season")
    position = episode.get("number") or episode.get("episode_num") or episode.get("series")
    return number, position


def episode_label(season: Optional[dict], episode: dict) -> str:
    """The Next card's "S2 · E1": the season's number (else the episode's) and the episode's."""
    number, position = season_and_episode(season, episode)
    parts = [f"{tag}{value}" for tag, value in (("S", number), ("E", position)) if value]
    return " · ".join(parts)


def genre_title(item: dict, groups: Iterable[tuple[str, str]]) -> str:
    """A movie's genre: its category's title among `groups` (id, title), else its genre field."""
    category = str(item.get("category_id") or item.get("tv_genre_id") or "")
    if category:
        for group_id, title in groups:
            if str(group_id) == category:
                return str(title)
    return str(item.get("genre") or item.get("genres_str") or "")
