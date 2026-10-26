"""While casting (2.8): a pill at the top centre (the sleep timer's, ui/overlays/sleep_notice.py)
says "Playing on <device>", with Stop casting: a click on it brings the picture back here
(`keepWatching`, the cast controls' stop). It stays while QiTV casts.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF

from services.i18n import translate
from ui.cast_controls import STOP_CASTING
from ui.overlays.sleep_notice import SleepNotice


class CastNotice(SleepNotice):
    def button_text(self) -> str:
        return translate(STOP_CASTING)

    def tooltip_at(self, pos: QPointF) -> str:
        return self.button_text()
