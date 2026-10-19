"""Each player's live channel and the key it opened with (2.3), for what is kept per channel.

A live channel's sound sync (ui/track_controls.py) and its picture's shape
(ui/picture_controls.py) are kept under the channel's key in their stores. A channel without
its provider's stamp is the browsed provider's, and the channel list may browse another one by
the time J, K or A is pressed: the key the channel opened with in its player stays the one its
choices are kept under while it plays there (through each reopen of the same channel). One
memory serves both, the large picture's player and the small ones' (the PiP, multiview tiles).

A player's entry goes when it opens something other than a live channel (a film, an episode,
a file), and when its picture closes (the PiP or a tile): nothing is kept for a player that
plays nothing.
"""

from __future__ import annotations

from typing import Any, Callable

from services.zapping import ZapContext


class PlayerKeys:
    def __init__(self, channel_key: Callable[[dict], str]) -> None:
        """`channel_key(channel)` is a live channel's key as it is now."""
        self._key = channel_key
        self._kept: dict[Any, tuple[dict, str]] = {}  # per player: its channel and its key

    def opens(self, player: Any, channel: dict) -> str:
        """`channel` opens in `player`: its key, kept for the choices made while it plays."""
        key = self._key(channel)
        self._kept[player] = (channel, key)
        return key

    def key_of(self, player: Any, channel: dict) -> str:
        """`channel`'s key: the one it opened with in `player`, else as it is now."""
        kept = self._kept.get(player)
        if kept is not None and (kept[0] is channel or ZapContext.same(kept[0], channel)):
            return kept[1]
        return self._key(channel)

    def forget(self, player: Any) -> None:
        """`player` plays no live channel any more (another title opens, or it closed)."""
        self._kept.pop(player, None)

    def players(self) -> list[Any]:
        """The players with a channel's key kept (tests)."""
        return list(self._kept)
