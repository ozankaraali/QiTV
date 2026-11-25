"""2.15: a phone types into QiTV's text boxes (the owner: "most of the stuff can be handled from
ui but textboxes").

QiTV paints its boxes (ui/text_edit.py), so no Qt focus says which one the keys type into:
`focused_field` asks the window as a typed letter would go (TVController.phone_field), and the
box's holder answers with a `PhoneField` (Open Link's and the other cards' fields, Settings' text
edited in place, the channel list's Find box once it shows a caret). The holder takes the phone's
text in place of the box's own (`holder.phone_type(key, text)`) through the path its keys take,
so what follows a change (Open Link's found line, the find's lists) follows it too. A secret
box's text is never in a `PhoneField`: only that it is secret. GUI thread only; no widgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Optional

from services.remote.protocol import MAX_LABEL, MAX_TYPED

logger = logging.getLogger(__name__)

# What has the keys before any text box (QiTV's questions, the ⚙ quality menu, a painted menu,
# the pairing screen): while one shows, no box takes a phone's typing.
_ABOVE = (
    "pair_overlay",
    "question_card",
    "quality_card",
    "quality_panel",
    "captions_card",
    "popup_menu",
)


@dataclass(frozen=True)
class PhoneField:
    """The text box the keys type into: its holder and its key there, what the phone's typing
    sheet calls it (its label, else its placeholder, else its card's title), its text ("" for a
    secret one, never shown) and whether it is secret (a password: the phone's field is
    masked)."""

    holder: Any
    key: str
    label: str
    text: str = field(default="", repr=False)
    secret: bool = False

    def same_box(self, other: Optional[PhoneField]) -> bool:
        """`other` is this box, as it was named: the same holder's same key, label and kind
        (its text may have changed, by the phone's typing among others)."""
        return (
            other is not None
            and other.holder is self.holder
            and (other.key, other.label, other.secret) == (self.key, self.label, self.secret)
        )

    def message(self, number: int) -> dict:
        """The phone's "field" for this box, numbered `number` (what its typing carries back).
        A secret box's text is never in it, nor its length."""
        message: dict = {"t": "field", "id": number, "label": self.label[:MAX_LABEL]}
        if self.secret:
            message["secret"] = True
        else:
            message["text"] = self.text[:MAX_TYPED]
        return message


def focused_field(window: Any) -> Optional[PhoneField]:
    """The text box a typed letter would go into now, if one would; None while a question or a
    menu has the keys, or no box has them."""
    for name in _ABOVE:
        above = getattr(window, name, None)
        if above is not None and above.isVisible():
            return None
    controller = getattr(window, "controller", None)
    asked = getattr(controller, "phone_field", None)
    if not callable(asked):
        return None
    found = asked()
    return found if isinstance(found, PhoneField) else None


def field_of(holder: Any) -> Optional[PhoneField]:
    """`holder`'s text box with the keys (`phone_field`), when it has one: a card without
    fields (a question) has none."""
    asked = getattr(holder, "phone_field", None)
    if not callable(asked):
        return None
    found = asked()
    return found if isinstance(found, PhoneField) else None
