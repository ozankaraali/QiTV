"""Next episode / next movie resolution for autoplay (pure; no Qt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

Key = Callable[[dict], str]


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
