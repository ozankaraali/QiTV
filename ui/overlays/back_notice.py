"""Coming back to a channel whose buffer was kept (2.6): it plays live, and a pill at the top
centre (the sleep timer's, ui/overlays/sleep_notice.py) says where it was left, "Left off at
20:10", with Resume: a click on it, or Enter, plays on from there (`keepWatching`). The shell
shows it for a minute (`ui.live_controls.BACK_NOTICE_MS`), or until the channel changes.
"""

from __future__ import annotations

from services.i18n import N_, translate
from ui.overlays.sleep_notice import SleepNotice

# i18n: a pill over a channel come back to: where it was left ("Left off at 20:10")
LEFT_OFF = N_("Left off at {time}")
# i18n: button: play the title from where it was left
RESUME = N_("Resume")


class BackNotice(SleepNotice):
    def button_text(self) -> str:
        return translate(RESUME)
