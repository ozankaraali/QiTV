"""2.11.2, the owner: the language AI captions are translated into is remembered per spoken
language ("German → English", "English → Türkçe", "English → no translation"), as
{spoken language (Qwen's name, services.captions.languages): MiLMMT's name for the target
(services.captions.mt_languages), or "" for none}. A language never chosen for, or not known
yet (""), is not translated; the app's language never chooses one (no Qt)."""

from __future__ import annotations

from typing import Mapping

from services.captions import languages, mt_languages


def clean(value: object) -> dict[str, str]:
    """The rules as the config keeps them: a spoken language's only when it is one of the
    captions' languages and its target is "" or one of MiLMMT's; anything else gives {}."""
    if not isinstance(value, dict):
        return {}
    return {
        heard: target
        for heard, target in value.items()
        if heard in languages.CAPTION_LANGUAGES
        and isinstance(target, str)
        and (target == "" or target in mt_languages.MT_LANGUAGES)
    }


def target_for(rules: Mapping[str, str], heard: str) -> str:
    """What a line heard in `heard` is translated into: "" when that language isn't known yet,
    has no rule, or its rule is no translation."""
    return str(rules.get(heard, "") or "") if heard else ""


def with_rule(rules: Mapping[str, str], heard: str, target: str) -> dict[str, str]:
    """The rules, as a new dict, with `heard`'s set to `target` ("" is kept: no translation,
    remembered). A language not known yet ("") sets nothing."""
    changed = dict(rules)
    if heard:
        changed[heard] = target
    return changed


def used_targets(rules: Mapping[str, str], heard: str = "") -> list[str]:
    """The languages any rule translates into, once each, by their own names; never the one
    heard (`heard`, as MiLMMT names it): nothing is translated into the language it is in."""
    own = mt_languages.source_for(heard) if heard else None
    targets = {target for target in rules.values() if target and target != own}
    return sorted(targets, key=lambda name: (mt_languages.own_name(name).casefold(), name))
