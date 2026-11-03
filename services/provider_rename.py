"""A provider renamed in Settings keeps what QiTV stored under its name (2.8.12).

QiTV keeps much under a provider's name, never under anything else: Settings → Save calls
`carry_over()` once the provider list with the new names is saved, and every store's own hook
moves its entries to the new name, each in the format it always had (older builds read them
as before). One save may rename several providers at once, a swap among them (`Renames`).

Kept by name, in config.json: the selected provider, the Library's (`library_provider_name`,
`library_mix`), the stars (`services.favorites`), the history, the resume points, the last film
and the last channel (`services.watch_state`), the last multiview grid (here) and what parental
controls hide (`services.parental`, through the window's guard). Beside it: the recording
schedule (`services.recording_schedule`). While QiTV runs: the connection budget, the phone's
own provider choice, the scheduler's recordings under way (the window's hooks).

Kept by a hash of the name and the channel (or the film, or the series): live_tuning.json,
sound_sync.json, picture.json, catchup.json, subtitle_picks.json, track_choices.json and the
kept buffers. A hash can't be renamed by itself: `HashedKeys` hashes the renamed provider's
channels, films and series as its catalog holds them (off the GUI thread) and those stores move
their entries by that map; a provider whose lists were not loaded keeps them under the old hash
(they are only learned values: relearned).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Iterable, Mapping, Optional

from PySide6.QtCore import QObject, Signal

from services import favorites, watch_state
from services.live.tuning import channel_key
from services.provider_ids import PROVIDER_KEY, mixed_group_id, split_group_id
from services.redact import safe_message
from services.search import iter_items
from services.subtitle_picks import pick_key
from services.track_memory import title_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Renames:
    """The providers one save renamed: `names` maps each old name to its new one (a swap
    is one step: every reference moves at once). `before` is every provider's name before
    the save: it tells whose a "name:rest" id is when one name begins another's."""

    names: Mapping[str, str]
    before: frozenset[str] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return bool(self.names)

    def of(self, name: Any) -> Any:
        """`name`'s new name; anything else as it is."""
        if isinstance(name, str):
            return self.names.get(name, name)
        return name

    def prefixed(self, text: Any, sep: str = ":") -> Any:
        """`text` ("<provider><sep><rest>", a resume point's id) with its provider renamed:
        the provider is the longest known name it begins with."""
        if not isinstance(text, str):
            return text
        owner = ""
        for name in self.before | set(self.names):
            if len(name) > len(owner) and text.startswith(name + sep):
                owner = name
        if owner not in self.names:
            return text
        return self.names[owner] + text[len(owner) :]

    def item(self, item: Any) -> Any:
        """A stored item (a channel, a film) with its provider stamp renamed (a copy when it
        changes)."""
        if isinstance(item, dict) and item.get(PROVIDER_KEY) in self.names:
            return {**item, PROVIDER_KEY: self.names[item[PROVIDER_KEY]]}
        return item

    def group(self, group_id: Any) -> Any:
        """A group id that names its provider (a mixed list's) renamed."""
        if not isinstance(group_id, str):
            return group_id
        provider, group = split_group_id(group_id)
        if provider not in self.names:
            return group_id
        return mixed_group_id(self.names[provider], group)


def renames_between(before: Iterable[tuple[Any, str]], after: Iterable[Any]) -> Renames:
    """The renames from (provider entry, its name before) pairs to the entries kept now
    (`after`, the same dicts): entries are told apart by identity, never by name."""
    pairs = list(before)
    kept = {id(entry) for entry in after}
    names = {
        old: str(entry.get("name") or "")
        for entry, old in pairs
        if id(entry) in kept and str(entry.get("name") or "") != old and old
    }
    return Renames({old: new for old, new in names.items() if new}, frozenset(n for _, n in pairs))


def removed_between(before: Iterable[tuple[Any, str]], after: Iterable[Any]) -> list[str]:
    """The names (before the save) of the entries of `before` no longer in `after`: removed,
    even when a provider kept or added now took the name (what was kept for the removed one
    was its own: its channels' addresses, its account)."""
    ids = {id(entry) for entry in after}
    return [old for entry, old in before if id(entry) not in ids and old]


def rename_in_config(config: Any, renames: Renames) -> bool:
    """Every reference config.json keeps by name (but parental controls': the window's guard
    holds them) moved to the new names; True when anything changed (the caller saves)."""
    if not renames:
        return False
    changed = False
    for key in ("selected_provider_name", "library_provider_name"):
        value = getattr(config, key, None)
        if isinstance(value, str) and value in renames.names:
            setattr(config, key, renames.names[value])
            changed = True
    mix = getattr(config, "library_mix", None)
    if isinstance(mix, list) and any(name in renames.names for name in mix):
        config.library_mix = [renames.of(name) for name in mix]
        changed = True
    changed |= _rename_multiview(config, renames)
    changed |= favorites.rename_providers(config, renames)
    changed |= watch_state.rename_providers(config, renames)
    return changed


def _rename_multiview(config: Any, renames: Renames) -> bool:
    saved = getattr(config, "multiview", None)
    if not isinstance(saved, dict) or not isinstance(saved.get("tiles"), list):
        return False
    tiles = saved["tiles"]
    renamed = [watch_state.renamed_record(tile, renames, item_key="item") for tile in tiles]
    if all(new is old for new, old in zip(renamed, tiles)):
        return False
    config.multiview = {**saved, "tiles": renamed}
    return True


Hook = Callable[[Renames], Any]


def carry_over(config: Any, renames: Renames, hooks: Iterable[Hook] = ()) -> None:
    """Move everything kept under the old names to the new ones: config.json's references,
    then each store's hook (the window's: parental controls, the schedule, …); config.json is
    saved once after them. A hook that fails is logged and the others go on."""
    if not renames:
        return
    rename_in_config(config, renames)
    for hook in hooks:
        try:
            hook(renames)
        except Exception as e:  # one store's failure never stops the others
            logger.warning("A store kept its old provider names: %s", safe_message(e))
    try:
        config.save_config()
    except Exception as e:
        logger.warning("Couldn't save the renamed providers' references: %s", safe_message(e))
    logger.info("%d provider(s) renamed; their references moved with them.", len(renames.names))


# ---- the hashed keys --------------------------------------------------------------------------


def hashed_keys(
    renames: Renames, contents: Mapping[str, Any], wanted: Optional[set[str]] = None
) -> dict[str, str]:
    """Old hashed key -> new for the renamed providers' live channels (`channel_key`) and
    films (`pick_key`), as `contents` (old name -> its catalog's content) lists them; with
    `wanted`, only the keys stored somewhere (the new key is hashed for those alone)."""
    keys: dict[str, str] = {}
    for old, content in contents.items():
        new = renames.names.get(old)
        if not new or not isinstance(content, dict):
            continue
        for item in iter_items(content.get("itv")):
            key = channel_key(old, item)
            if key and (wanted is None or key in wanted):
                keys[key] = channel_key(new, item)
        for item in iter_items(content.get("vod")):
            key = pick_key(old, item, episode=False)
            if key and (wanted is None or key in wanted):
                keys[key] = pick_key(new, item, episode=False)
            key = title_key(old, item, episode=False)  # 2.9.1: its tracks
            if key and (wanted is None or key in wanted):
                keys[key] = title_key(new, item, episode=False)
        for item in iter_items(content.get("series")):
            key = title_key(old, {}, episode=True, context={"series": item})
            if key and (wanted is None or key in wanted):
                keys[key] = title_key(new, {}, episode=True, context={"series": item})
    return keys


class HashedKeys(QObject):
    """`hashed_keys()` on a worker thread (a provider may list 100,000 channels): `finished`
    carries the map (plain data), applied on the GUI thread."""

    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self, renames: Renames, contents: Mapping[str, Any], wanted: Optional[set[str]] = None
    ) -> None:
        super().__init__()
        self._renames = renames
        self._contents = dict(contents)
        self._wanted = wanted

    def run(self) -> None:
        try:
            keys = hashed_keys(self._renames, self._contents, self._wanted)
        except Exception as e:  # a list changed under it: the learned values stay where they are
            logger.info("The renamed providers' channel keys stay: %s", safe_message(e))
            self.error.emit("")
            return
        self.finished.emit(keys)
