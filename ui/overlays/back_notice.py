"""Coming back to a channel whose buffer was kept (2.6): it plays live, and a pill at the top
centre (the sleep timer's, ui/overlays/sleep_notice.py) says where it was left, "Left off at
20:10", with Resume: a click on it, or Enter, plays on from there (`keepWatching`). The shell
shows it for 15 s (`ui.live_controls.BACK_NOTICE_MS`; a minute before 2.13.3: the owner, "they
will know if they want to stay in 10-15 secs"), or until the channel changes. It may be closed
(`closable`): its ✕ (`closeRequested`), Esc and the phone's Back close it, and the offer goes
with it, as at its end.
"""

from __future__ import annotations

from services.i18n import N_, translate
from ui.overlays.sleep_notice import SleepNotice

# i18n: a pill over a channel come back to: where it was left ("Left off at 20:10")
LEFT_OFF = N_("Left off at {time}")
# i18n: button: play the title from where it was left
RESUME = N_("Resume")


class BackNotice(SleepNotice):
    closable = True

    def button_text(self) -> str:
        return translate(RESUME)
